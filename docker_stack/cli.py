#!/usr/bin/env python3
import argparse
import base64
from dataclasses import dataclass
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import os
import yaml
import json
from docker_stack.docker_objects import DockerConfig, DockerObjectManager, DockerSecret
from docker_stack.helpers import Command, generate_secret
from docker_stack.registry import DockerRegistry
from .envsubst import LineCheckResult, SubstitutionError, envsubst, envsubst_load_file


@dataclass
class EnvFileEntry:
    key: str
    value: str
    line_no: int
    line_content: str
    value_start_index: int
    value_inner_offset: int


class EnvFileResolutionError(Exception):
    def __init__(self, env_file: str, reason: str, results: List[LineCheckResult], template_lines: List[str]):
        self.env_file = env_file
        self.reason = reason
        self.results = results
        self.template_lines = template_lines
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        formatted_lines = [f"{self.reason} in {self.env_file}:"]
        errors_by_line = {}

        for result in self.results:
            if result.line_no not in errors_by_line:
                errors_by_line[result.line_no] = {"line_content": result.line_content, "variables": []}
            errors_by_line[result.line_no]["variables"].append({"name": result.variable_name, "start_index": result.start_index})

        for line_no in sorted(errors_by_line):
            line_chars = list(errors_by_line[line_no]["line_content"])
            for item in sorted(errors_by_line[line_no]["variables"], key=lambda x: x["start_index"], reverse=True):
                for idx in range(len(item["name"]) - 1, -1, -1):
                    line_chars.insert(item["start_index"] + idx + 1, "\u0333")
            formatted_lines.append(f"{line_no:3d}   {''.join(line_chars)}")

        return "\n".join(formatted_lines)


ENV_VAR_PATTERN = re.compile(r"\$\{([^}:\s]+)(?::-(.*?))?\}|\$([a-zA-Z_][a-zA-Z0-9_]*)")


def _strip_matching_quotes(value: str) -> Tuple[str, int]:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1], 1
    return value, 0


def _parse_env_file(env_file: str) -> Tuple[List[EnvFileEntry], List[str]]:
    entries: List[EnvFileEntry] = []
    with open(env_file) as f:
        template_lines = f.read().splitlines(keepends=False)

    for line_no, raw_line in enumerate(template_lines, start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        key_part, separator, value_part = raw_line.partition("=")
        if not separator:
            continue

        key = key_part.strip()
        leading_ws = len(value_part) - len(value_part.lstrip())
        value_start_index = len(key_part) + 1 + leading_ws
        value, value_inner_offset = _strip_matching_quotes(value_part.strip())
        entries.append(
            EnvFileEntry(
                key=key,
                value=value,
                line_no=line_no,
                line_content=raw_line,
                value_start_index=value_start_index,
                value_inner_offset=value_inner_offset,
            )
        )
    return entries, template_lines


def _extract_refs(value: str) -> List[Tuple[str, int]]:
    refs = []
    for match in ENV_VAR_PATTERN.finditer(value):
        var = match.group(1) if match.group(1) is not None else match.group(3)
        start = match.start(1) if match.group(1) is not None else match.start(3)
        refs.append((var, start))
    return refs


def _map_substitution_error(entry: EnvFileEntry, err: SubstitutionError) -> List[LineCheckResult]:
    mapped_results = []
    for result in err.results:
        mapped_results.append(
            LineCheckResult(
                line_no=entry.line_no,
                line_content=entry.line_content,
                variable_name=result.variable_name,
                start_index=entry.value_start_index + entry.value_inner_offset + (result.start_index or 0),
            )
        )
    return mapped_results


def _resolve_env_entries(entries: List[EnvFileEntry], env_file: str, base_env: Optional[Dict[str, str]] = None, max_cycles: int = 5) -> Dict[str, str]:
    base_env = dict(base_env or os.environ)
    current_values = {entry.key: entry.value for entry in entries}
    local_keys = set(current_values.keys())
    resolution_passes = max(max_cycles, len(entries))

    for _ in range(resolution_passes):
        next_values: Dict[str, str] = {}
        missing_results: List[LineCheckResult] = []
        changed = False

        resolution_env = {**base_env, **current_values}
        for entry in entries:
            try:
                resolved = envsubst(entry.value, env=resolution_env, on_error="throw")
            except SubstitutionError as err:
                missing_results.extend(_map_substitution_error(entry, err))
                continue

            next_values[entry.key] = resolved
            if resolved != current_values[entry.key]:
                changed = True

        if missing_results:
            missing_results.sort(key=lambda x: (x.line_no, x.start_index or 0))
            raise EnvFileResolutionError(env_file, "Missing environment variables", missing_results, [])

        current_values = next_values
        if not changed:
            break

    cyclic_results: List[LineCheckResult] = []
    for entry in entries:
        final_value = current_values[entry.key]
        for var_name, start_index in _extract_refs(final_value):
            if var_name in local_keys:
                original_refs = [(name, idx) for name, idx in _extract_refs(entry.value) if name in local_keys]
                ref_positions = original_refs or [(var_name, 0)]
                for original_name, original_index in ref_positions:
                    cyclic_results.append(
                        LineCheckResult(
                            line_no=entry.line_no,
                            line_content=entry.line_content,
                            variable_name=original_name,
                            start_index=entry.value_start_index + entry.value_inner_offset + original_index,
                        )
                    )
                break

    if cyclic_results:
        deduped = {
            (result.line_no, result.variable_name, result.start_index): result for result in cyclic_results
        }
        ordered_results = sorted(deduped.values(), key=lambda x: (x.line_no, x.start_index or 0))
        raise EnvFileResolutionError(
            env_file,
            f"Cyclic environment variable references detected after {resolution_passes} resolution passes",
            ordered_results,
            [],
        )

    return current_values


def load_env_file(env_file: str, base_env: Optional[Dict[str, str]] = None, max_cycles: int = 5) -> Dict[str, str]:
    entries, _ = _parse_env_file(env_file)
    return _resolve_env_entries(entries, env_file, base_env=base_env, max_cycles=max_cycles)


class Docker:
    def __init__(self, registries: List[str] = []):
        self.stack = DockerStack(self)
        self.node = DockerNode()
        self.config = DockerConfig()
        self.secret = DockerSecret()
        self.registry = DockerRegistry(registries)

    @staticmethod
    def load_env(env_file=".env"):
        if Path(env_file).is_file():
            os.environ.update(load_env_file(env_file))

    @staticmethod
    def check_env(example_file=".env.example"):
        if not Path(example_file).is_file():
            return

        unset_keys = []
        with open(example_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    key = line.split("=")[0].strip()
                    if not os.environ.get(key):
                        unset_keys.append(key)

        if unset_keys:
            print("The following keys are not set in the environment:")
            for key in unset_keys:
                print(f"- {key}")
            print("Exiting due to missing environment variables.")
            sys.exit(2)


class DockerStack:
    def __init__(self, docker: Docker):
        self.docker = docker
        self.commands: List[Command] = []
        self.generated_secrets: Dict[str, str] = {}  # To store newly generated secrets

    def read_compose_file(self, compose_file) -> dict:
        with open(compose_file) as f:
            return self.decode_yaml(f.read())

    def rendered_compose_file(self, compose_file, stack=None, include_build=True) -> str:
        with open(compose_file) as f:
            template_content = f.read()
        # Parse the YAML content
        compose_data = self.decode_yaml(template_content)
        if not include_build:
            services: dict = compose_data.get("services", {})
            for k, v in services.items():
                if "build" in v:
                    del v["build"]
        if stack:
            base_dir = os.path.dirname(os.path.abspath(compose_file))
            if "configs" in compose_data:
                compose_data["configs"] = self._process_x_content(
                    compose_data["configs"], self.docker.config, base_dir=base_dir, stack=stack
                )
            if "secrets" in compose_data:
                compose_data["secrets"] = self._process_x_content(
                    compose_data["secrets"], self.docker.secret, base_dir=base_dir, stack=stack
                )

        # Define the replacements for '$' to '$$' for env variables in compose files
        replacements_map = {"$": "$$"}
        return envsubst(yaml.dump(compose_data, sort_keys=False), replacements=replacements_map)

    def decode_yaml(self, data: str) -> dict:
        return yaml.safe_load(data)

    def render_compose_file(self, compose_file, stack=None, include_build=True):
        """
        Render the Docker Compose file with environment variables and create Docker configs/secrets.
        """

        # Convert the modified data back to YAML
        rendered_content = self.rendered_compose_file(compose_file, stack, include_build=include_build)

        # Write the rendered file
        rendered_filename = Path(compose_file).with_name(f"{Path(compose_file).stem}-rendered{Path(compose_file).suffix}")
        with open(rendered_filename, "w") as f:
            f.write(rendered_content)
        return (rendered_filename, rendered_content)

    def _process_x_content(self, objects, manager: DockerObjectManager, base_dir="", stack=None):
        """
        Process configs or secrets with x-content keys.
        Returns a tuple: (processed_objects, commands)
        """
        processed_objects = {}

        def add_obj(name, data, explicit_name=None, is_generated_secret=False):
            labels = []
            if is_generated_secret:
                labels.append("mesudip.secret.generated=true")

            if explicit_name:
                docker_object_name = explicit_name
            elif stack:
                docker_object_name = f"{stack}_{name}"
            else:
                docker_object_name = name

            (object_name, command) = manager.create(docker_object_name, data, labels=labels, stack=stack)
            if not command.isNop():
                self.commands.append(command)
                # If a new secret was actually created (not just reused), store it
                if is_generated_secret:
                    self.generated_secrets[name] = data
            processed_objects[name] = {"name": object_name, "external": True}

        for name, details in objects.items():
            explicit_name = details.get("name") if isinstance(details, dict) else None
            if isinstance(details, dict) and "x-content" in details:
                add_obj(name, details["x-content"], explicit_name=explicit_name)
            elif isinstance(details, dict) and "x-template" in details:
                add_obj(name, envsubst(details["x-content"], os.environ), explicit_name=explicit_name)
            elif isinstance(details, dict) and "x-template-file" in details:
                filename = os.path.join(base_dir, details["x-template-file"])
                add_obj(name, envsubst_load_file(filename, os.environ), explicit_name=explicit_name)
            elif isinstance(details, dict) and "file" in details:
                filename = os.path.join(base_dir, details["file"])
                with open(filename) as file:
                    add_obj(name, file.read(), explicit_name=explicit_name)
            elif isinstance(details, dict) and "x-generate" in details and manager.object_type == "secret":
                is_generated_secret = True
                generate_options = details["x-generate"]
                secret_content = ""

                # Determine the content to be used for the secret
                # This logic is now simplified as DockerObjectManager.create handles persistence
                if isinstance(generate_options, bool) and generate_options:
                    secret_content = generate_secret()
                elif isinstance(generate_options, int):
                    secret_content = generate_secret(length=generate_options)
                elif isinstance(generate_options, dict):
                    secret_content = generate_secret(
                        length=generate_options.get("length"),
                        numbers=generate_options.get("numbers", True),
                        special=generate_options.get("special", True),
                        uppercase=generate_options.get("uppercase", True),
                    )
                else:
                    raise ValueError(f"Invalid x-generate value for secret {name}: {generate_options}")

                # Call add_obj with the potentially new secret content and the generated flag
                add_obj(name, secret_content, explicit_name=explicit_name, is_generated_secret=True)
            else:
                processed_objects[name] = details
        return processed_objects

    def ls(self):
        cmd = ["docker", "config", "ls", "--format", "{{.ID}}\t{{.Name}}\t{{.Labels}}"]
        output = subprocess.check_output(cmd, text=True).strip().split("\n")
        stack_versions = {}

        for line in output:
            parts = line.split("\t")
            if len(parts) == 3 and "mesudip.stack.name" in parts[2]:
                labels = {k: v for k, v in (label.split("=") for label in parts[2].split(",") if "=" in label)}
                stack_name = labels.get("mesudip.stack.name")
                version = labels.get("mesudip.object.version", "unknown")

                if stack_name:
                    if stack_name not in stack_versions:
                        stack_versions[stack_name] = []
                    stack_versions[stack_name].append(version)

        # Calculate max stack name width
        max_stack_name_length = max(len(stack) for stack in stack_versions) if stack_versions else 10
        header_stack = "Stack Name".ljust(max_stack_name_length)

        print(f"{header_stack} | Versions")
        print("-" * (max_stack_name_length + 12))

        for stack, versions in sorted(stack_versions.items()):
            versions_str = ", ".join(sorted(versions, key=int))
            print(f"{stack.ljust(max_stack_name_length)} | {versions_str}")

        return stack_versions

    def cat(self, name: str, version: str):
        if version.startswith("v") or version.startswith("V"):
            version = version[1:]
        if version == "1":
            name = f"{name}"
        else:
            name = f"{name}_v{version}"

        cmd = ["docker", "config", "inspect", name]
        output = subprocess.check_output(cmd, text=True).strip()

        # Parse the JSON output
        configs = json.loads(output)
        if not configs:
            print(f"No config found for {name}")
            return None

        # Extract and decode the base64-encoded Spec.Data
        encoded_data = configs[0].get("Spec", {}).get("Data", "")
        if not encoded_data:
            print(f"No data found in config {name}")
            return None

        decoded_data = base64.b64decode(encoded_data).decode("utf-8")
        return decoded_data

    def versions(self, stack_name):
        cmd = ["docker", "config", "ls", "--format", "{{.Name}}\t{{.Labels}}"]
        output = subprocess.check_output(cmd, text=True).strip().split("\n")
        versions_list = []

        for line in output:
            parts = line.split("\t")
            if len(parts) == 2 and "mesudip.stack.name" in parts[1]:
                labels = {k: v for k, v in (label.split("=") for label in parts[1].split(",") if "=" in label)}
                stack = labels.get("mesudip.stack.name")
                version = labels.get("mesudip.object.version", "unknown")
                tag = labels.get("mesudip.stack.tag", "")

                if stack == stack_name:
                    versions_list.append((version, tag))

        # Add headers to list for proper spacing calculation
        versions_list.insert(0, ("Version", "Tag"))

        # Determine max column widths
        max_version_length = max(len(v[0]) for v in versions_list)
        max_tag_length = max(len(v[1]) for v in versions_list)

        # Print header
        print(f"{'Version'.ljust(max_version_length)} | {'Tag'.ljust(max_tag_length)}")
        print("-" * (max_version_length + max_tag_length + 3))

        # Print sorted versions (excluding header)
        for version, tag in sorted(versions_list[1:], key=lambda x: int(x[0]) if x[0].isdigit() else x[0]):
            print(f"{version.ljust(max_version_length)} | {tag.ljust(max_tag_length)}")

        return versions_list[1:]

    def checkout(self, stack_name, identifier, with_registry_auth=False):
        """
        Deploys a stack by version or tag.

        :param stack_name: Name of the stack.
        :param identifier: Version (e.g., 'v1.2', 'V3') or tag (e.g., 'stable', 'latest').
        :param with_registry_auth: Whether to use registry authentication.
        """

        # Regex to check if the identifier is a version (optional "v" or "V" at the start, followed by digits)
        version_pattern = re.compile(r"^[vV]?(\d+(\.\d+)*)$")

        match = version_pattern.match(identifier)
        if match:
            version = match.group(1)  # Extract the numeric version part
            tag = None
        else:
            tag = identifier
            versions_list = self.versions(stack_name)
            matching_versions = [v for v, t in versions_list if t == tag]
            if not matching_versions:
                raise ValueError(f"No version found for tag '{tag}' in stack '{stack_name}'")
            version = matching_versions[0]  # Use the first matching version

        compose_content = self.cat(stack_name, version)

        temp_file = f"/tmp/{stack_name}_v{version}.yml"
        with open(temp_file, "w") as f:
            f.write(compose_content)

        print(f"Deploying stack {stack_name} with version {version} (tag: {tag})...")
        self._deploy(stack_name, temp_file, compose_content, with_registry_auth=with_registry_auth, tag=tag)

    def _deploy(self, stack_name, rendered_filename, rendered_content, with_registry_auth=False, tag=None):
        labels = [f"mesudip.stack.name={stack_name}"]
        if tag:
            labels.append(f"mesudip.stack.tag={tag}")

        _, cmd = self.docker.config.increment(stack_name, rendered_content, labels=labels, stack=stack_name)
        if not cmd.isNop():
            self.commands.append(cmd)
        cmd = ["docker", "stack", "deploy", "-c", str(rendered_filename), stack_name]
        if with_registry_auth:
            cmd.insert(3, "--with-registry-auth")
        self.commands.append(Command(cmd, give_console=True))

    def deploy(self, stack_name, compose_file, with_registry_auth=False, tag=None, show_generated=True):
        self.generated_secrets = {}  # Reset for each deployment
        rendered_filename, rendered_content = self.render_compose_file(compose_file, stack=stack_name, include_build=False)
        labels = [f"mesudip.stack.name={stack_name}"]
        if tag:
            labels.append(f"mesudip.stack.tag={tag}")

        _, cmd = self.docker.config.increment(stack_name, rendered_content, labels=labels, stack=stack_name)
        if not cmd.isNop():
            self.commands.append(cmd)
        cmd = ["docker", "stack", "deploy", "-c", str(rendered_filename), stack_name]
        if with_registry_auth:
            cmd.insert(3, "--with-registry-auth")
        self.commands.append(Command(cmd, give_console=True))

        if show_generated and self.generated_secrets:
            print("\n----- Newly Generated Secrets -----")
            for name, content in self.generated_secrets.items():
                print(f"{name}: {content}")
            print("---------------------------------\n")

    def prune(self):
        """
        Removes old versions of Docker configs and secrets, keeping only the most recent.
        """
        if self.docker.config:
            self.commands.extend(self.docker.config.prune(keep=15))
        if self.docker.secret:
            self.commands.extend(self.docker.secret.prune(keep=5))

    def push(self, compose_file):
        compose_data = self.read_compose_file(compose_file)
        for service_name, service_data in compose_data.get("services", {}).items():
            if "build" in service_data:
                image = envsubst(service_data["image"])
                push_result = self.check_and_push_pull_image(image, "push")
                if push_result:
                    self.commands.append(push_result)
                else:
                    # print("No need to push: Already exists")
                    pass

    def build_and_push(self, compose_file: str, push: bool = False) -> None:
        """
        Build Docker images from a Compose file and optionally push them.

        Args:
            compose_file (str): Path to the Docker Compose file.
            push (bool): Whether to push the built images. Defaults to False.
        """
        compose_data = self.read_compose_file(compose_file)
        base_dir = os.path.dirname(os.path.abspath(compose_file))

        for service_name, service_data in compose_data.get("services", {}).items():
            if "build" in service_data:
                build_config = service_data["build"]
                image = envsubst(service_data["image"])

                build_command = ["docker", "build", "-t", image]

                dockerfile = build_config.get("dockerfile")
                context_path = os.path.normpath(os.path.join(base_dir, build_config.get("context", ".")))
                if dockerfile:
                    build_command.extend(["-f", os.path.normpath(os.path.join(context_path, dockerfile))])

                args = build_config.get("args", [])

                if isinstance(args, dict):
                    for key, val in args.items():
                        build_command.extend(["--build-arg", f"{envsubst(key)}={envsubst(val)}"])
                elif isinstance(args, list):
                    for value in args:
                        build_command.extend(["--build-arg", envsubst(value)])

                build_command.append(context_path)
                self.commands.append(Command(build_command))

                if push:
                    push_result = self.check_and_push_pull_image(image, "push")
                    if push_result:
                        self.commands.append(push_result)
                    else:
                        # print("No need to push: Already exists")
                        pass

    def check_and_push_pull_image(self, image_name: str, action: str):
        if self.docker.registry.check_image(image_name):
            return None
        if action == "push":
            cmd = self.docker.registry.push(image_name)
            if cmd:
                self.commands.append(cmd)


class DockerNode:
    @staticmethod
    def _format_labels(labels: Dict[str, str]) -> str:
        if not labels:
            return "-"
        parts = []
        for key, value in sorted(labels.items()):
            parts.append(key if value == "true" else f"{key}={value}")
        return ", ".join(parts)

    @staticmethod
    def ls():
        nodes_output = subprocess.check_output(["docker", "node", "ls", "--format", "{{json .}}"], text=True).strip()
        rows = []

        for line in nodes_output.splitlines():
            if not line:
                continue
            node = json.loads(line)
            inspect = json.loads(
                subprocess.check_output(["docker", "node", "inspect", node["ID"], "--format", "{{json .}}"], text=True).strip()
            )
            labels = inspect.get("Spec", {}).get("Labels", {})
            manager_status = node.get("ManagerStatus", "").strip()
            role = inspect.get("Spec", {}).get("Role", "-")
            role_display = f"{role} ({manager_status})" if manager_status else role
            rows.append(
                {
                    "hostname": node.get("Hostname", "-"),
                    "role": role_display,
                    "state": f"{node.get('Status', '-')} / {node.get('Availability', '-')}",
                    "address": inspect.get("Status", {}).get("Addr", "-"),
                    "labels": DockerNode._format_labels(labels),
                }
            )

        columns = [
            ("Hostname", "hostname"),
            ("Role", "role"),
            ("State", "state"),
            ("Address", "address"),
        ]

        widths = {
            key: max(len(title), max((len(str(row[key])) for row in rows), default=0))
            for title, key in columns
        }
        terminal_width = shutil.get_terminal_size((120, 20)).columns
        static_width = sum(widths.values()) + (3 * (len(columns) - 1))
        label_width = max(24, min(60, terminal_width - static_width - 3 - len("Labels")))

        header = " | ".join(title.ljust(widths[key]) for title, key in columns) + " | Labels"
        separator = "-+-".join("-" * widths[key] for _, key in columns) + "-+-" + ("-" * label_width)
        print(header)
        print(separator)
        for row in rows:
            wrapped_labels = textwrap.wrap(row["labels"], width=label_width, break_long_words=False, break_on_hyphens=False) or ["-"]
            first_line = " | ".join(str(row[key]).ljust(widths[key]) for _, key in columns)
            print(f"{first_line} | {wrapped_labels[0]}")
            continuation_prefix = " | ".join("".ljust(widths[key]) for _, key in columns)
            for label_line in wrapped_labels[1:]:
                print(f"{continuation_prefix} | {label_line}")

        return rows


def main(args: List[str] = None):
    parser = argparse.ArgumentParser(description="Deploy and manage Docker stacks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Build subcommand
    build_parser = subparsers.add_parser("build", help="Build images using docker-compose")
    build_parser.add_argument("compose_file", help="Path to the compose file")
    build_parser.add_argument("--push", action="store_true", help="Use registry authentication")

    # Push subcommand
    push_parser = subparsers.add_parser("push", help="Push images to registry")
    push_parser.add_argument("compose_file", help="Path to the compose file")

    # Deploy subcommand
    deploy_parser = subparsers.add_parser("deploy", help="Deploy stack using docker stack deploy")
    deploy_parser.add_argument("stack_name", help="Name of the stack")
    deploy_parser.add_argument("compose_file", help="Path to the compose file")
    deploy_parser.add_argument("--with-registry-auth", action="store_true", help="Use registry authentication")
    deploy_parser.add_argument("-t", "--tag", help="Tag the current deployment for later checkout", required=False)
    deploy_parser.add_argument("--show-generated", action="store_true", default=True, help="Show newly generated secrets after deployment")

    # Remove subcommand
    rm_parser = subparsers.add_parser("rm", help="Remove a deployed stack")
    rm_parser.add_argument("stack_name", help="Name of the stack")

    # Prune command
    subparsers.add_parser("prune", help="Remove old versions of configs and secrets")

    # Ls command
    subparsers.add_parser("ls", help="List docker-stacks")

    node_parser = subparsers.add_parser("node", help="Inspect Docker Swarm nodes")
    node_subparsers = node_parser.add_subparsers(dest="node_command", required=True)
    node_subparsers.add_parser("ls", help="List Docker Swarm nodes and their labels")

    cat_parser = subparsers.add_parser(
        "cat", help="Print the docker compose of specific version. Defaults to latest version if not specified."
    )
    cat_parser.add_argument("stack_name", help="Name of the stack")
    cat_parser.add_argument("version", nargs="?", help="Stack version to cat. Defaults to latest if omitted.")

    checkout_parser = subparsers.add_parser("checkout", help="Deploy specific version of the stack")
    checkout_parser.add_argument("stack_name", help="Name of the stack")
    checkout_parser.add_argument("version", help="Stack version to cat")

    # version_parser = subparsers.add_parser("version",help="Deploy specific version of the stack")
    # version_parser.add_argument("stack_name", help="Name of the stack")
    # version_parser.add_argument("version","versions", help="Stack version to cat")

    version_parser = subparsers.add_parser("version", aliases=["versions"], help="Deploy specific version of the stack")
    version_parser.add_argument("stack_name", help="Name of the stack")

    parser.add_argument(
        "-u", "--user", help="Registry credentials in format hostname:username:password", action="append", required=False, default=[]
    )
    parser.add_argument("-t", "--tag", help="Tag the current deployment for later checkout", required=False)
    parser.add_argument(
        "-ro", "-r", "--ro", "--r", "--dry-run", action="store_true", help="Print commands, don't execute them", required=False
    )
    parser.add_argument("--show-generated", action="store_true", default=True, help="Show newly generated secrets after deployment")

    args = parser.parse_args(args if args else sys.argv[1:])

    docker = Docker(registries=args.user)
    docker.load_env()

    if args.command == "build":
        docker.stack.build_and_push(args.compose_file, push=args.push)
    elif args.command == "push":
        docker.stack.push(args.compose_file)
    elif args.command == "deploy":
        docker.stack.deploy(args.stack_name, args.compose_file, args.with_registry_auth, tag=args.tag, show_generated=args.show_generated)
    elif args.command == "ls":
        docker.stack.ls()
    elif args.command == "node":
        if args.node_command == "ls":
            docker.node.ls()

    elif args.command == "rm":
        docker.stack.rm(args.stack_name)
    elif args.command == "prune":
        docker.stack.prune()
    elif args.command == "cat":
        version_to_cat = args.version
        if version_to_cat is None:
            versions_list = docker.stack.versions(args.stack_name)
            if versions_list:
                # Assuming versions are integers, find the maximum
                latest_version = max(int(v[0]) for v in versions_list if v[0].isdigit())
                version_to_cat = str(latest_version)
            else:
                print(f"No versions found for stack '{args.stack_name}'.")
                sys.exit(1)
        print(docker.stack.cat(args.stack_name, version_to_cat))
    elif args.command == "checkout":
        docker.stack.checkout(args.stack_name, args.version)
    elif args.command == "versions" or args.command == "version":
        docker.stack.versions(args.stack_name)
    if args.ro:
        print("Following commands were not executed:")
        [print(" >> " + str(x)) for x in docker.stack.commands if x]
    else:
        [x.execute() for x in docker.stack.commands]


if __name__ == "__main__":
    main([""])
