#!/usr/bin/env python3
import argparse
import base64
from dataclasses import dataclass
import subprocess
import re
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import os
import yaml
import json
from docker_stack.docker_objects import DockerConfig, DockerObjectManager, DockerSecret
from docker_stack.helpers import CallbackCommand, Command, generate_secret, run_cli_command
from docker_stack.login import (
    ensure_isolated_login,
    format_expiry,
    login as docker_manager_login,
    resolve_login_config,
    resolve_shell_login_config,
    setup_auth as docker_manager_setup_auth,
    switch_docker_context,
)
from docker_stack.manager_api import (
    FEATURE_STACK_DEPLOY,
    FEATURE_STACK_QUERY,
    ManagerApiClient,
    discover_manager_client,
)
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
        self.node = DockerNode(self)
        self.config = DockerConfig()
        self.secret = DockerSecret()
        self.registry = DockerRegistry(registries)
        self._manager_client_checked = False
        self._manager_client: Optional[ManagerApiClient] = None

    def manager_client(self) -> Optional[ManagerApiClient]:
        if not self._manager_client_checked:
            self._manager_client = discover_manager_client()
            self._manager_client_checked = True
        return self._manager_client

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

    def _manager_client_for_feature(self, feature_name: str) -> Optional[ManagerApiClient]:
        client = self.docker.manager_client()
        if not client:
            return None
        try:
            if client.supports(feature_name):
                return client
        except RuntimeError:
            return None
        return None

    @staticmethod
    def _normalize_version(value: str) -> str:
        if value.startswith("v") or value.startswith("V"):
            return value[1:]
        return value

    @staticmethod
    def _version_sort_key(raw: str):
        return int(raw) if raw.isdigit() else raw

    def _print_stack_listing(self, stack_versions: Dict[str, List[str]]) -> None:
        max_stack_name_length = max(len(stack) for stack in stack_versions) if stack_versions else 10
        header_stack = "Stack Name".ljust(max_stack_name_length)

        print(f"{header_stack} | Versions")
        print("-" * (max_stack_name_length + 12))

        for stack, versions in sorted(stack_versions.items()):
            versions_str = ", ".join(sorted(versions, key=self._version_sort_key))
            print(f"{stack.ljust(max_stack_name_length)} | {versions_str}")

    def _print_versions(self, versions_list: List[Tuple[str, str]]) -> None:
        rows = [("Version", "Tag")] + versions_list
        max_version_length = max(len(v[0]) for v in rows)
        max_tag_length = max(len(v[1]) for v in rows)

        print(f"{'Version'.ljust(max_version_length)} | {'Tag'.ljust(max_tag_length)}")
        print("-" * (max_version_length + max_tag_length + 3))

        for version, tag in sorted(versions_list, key=lambda x: self._version_sort_key(x[0])):
            print(f"{version.ljust(max_version_length)} | {tag.ljust(max_tag_length)}")

    def ls(self):
        client = self._manager_client_for_feature(FEATURE_STACK_QUERY)
        if client:
            try:
                payload = client.list_stacks()
                stack_versions = {
                    str(item.get("stack")): [
                        str(version)
                        for version in (
                            item.get("versions")
                            if isinstance(item.get("versions"), list)
                            else item.get("available_versions", [])
                        )
                        if str(version).strip()
                    ]
                    for item in payload.get("stacks", [])
                    if str(item.get("stack", "")).strip()
                }
                self._print_stack_listing(stack_versions)
                return stack_versions
            except RuntimeError:
                pass

        cmd = ["docker", "config", "ls", "--format", "{{.ID}}\t{{.Name}}\t{{.Labels}}"]
        raw_output = run_cli_command(cmd, log=False)
        output = raw_output.split("\n") if raw_output else []
        stack_versions: Dict[str, List[str]] = {}

        for line in output:
            parts = line.split("\t")
            if len(parts) == 3 and "mesudip.stack.name" in parts[2]:
                labels = {k: v for k, v in (label.split("=") for label in parts[2].split(",") if "=" in label)}
                stack_name = labels.get("mesudip.stack.name")
                version = labels.get("mesudip.object.version", "unknown")

                if stack_name:
                    stack_versions.setdefault(stack_name, []).append(version)

        self._print_stack_listing(stack_versions)
        return stack_versions

    def cat(self, name: str, version: str, namespace: str = "default"):
        normalized_version = self._normalize_version(version)
        client = self._manager_client_for_feature(FEATURE_STACK_QUERY)
        if client:
            try:
                payload = client.get_stack_compose(
                    name,
                    namespace=namespace,
                    version=normalized_version,
                )
                compose = payload.get("compose")
                if isinstance(compose, str):
                    return compose
            except RuntimeError:
                pass

        if normalized_version == "1":
            config_name = f"{name}"
        else:
            config_name = f"{name}_v{normalized_version}"

        cmd = ["docker", "config", "inspect", config_name]
        output = run_cli_command(cmd, log=False)

        configs = json.loads(output)
        if not configs:
            print(f"No config found for {config_name}")
            return None

        encoded_data = configs[0].get("Spec", {}).get("Data", "")
        if not encoded_data:
            print(f"No data found in config {config_name}")
            return None

        decoded_data = base64.b64decode(encoded_data).decode("utf-8")
        return decoded_data

    def versions(self, stack_name, namespace: str = "default", print_output: bool = True):
        client = self._manager_client_for_feature(FEATURE_STACK_QUERY)
        if client:
            try:
                payload = client.list_stack_versions(stack_name, namespace=namespace)
                versions_list = [
                    (str(item.get("version", "")), str(item.get("tag", "")))
                    for item in payload.get("versions", [])
                    if str(item.get("version", "")).strip()
                ]
                if print_output:
                    self._print_versions(versions_list)
                return versions_list
            except RuntimeError:
                pass

        cmd = ["docker", "config", "ls", "--format", "{{.Name}}\t{{.Labels}}"]
        raw_output = run_cli_command(cmd, log=False)
        output = raw_output.split("\n") if raw_output else []
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

        if print_output:
            self._print_versions(versions_list)
        return versions_list

    def checkout(self, stack_name, identifier, with_registry_auth=False, namespace: str = "default", dry_run: bool = False):
        """
        Deploys a stack by version or tag.

        :param stack_name: Name of the stack.
        :param identifier: Version (e.g., 'v1.2', 'V3') or tag (e.g., 'stable', 'latest').
        :param with_registry_auth: Whether to use registry authentication.
        """

        version_pattern = re.compile(r"^[vV]?(\d+(\.\d+)*)$")
        manager_query = self._manager_client_for_feature(FEATURE_STACK_QUERY)
        manager_deploy = self._manager_client_for_feature(FEATURE_STACK_DEPLOY)

        match = version_pattern.match(identifier)
        if match:
            version = match.group(1)
            tag = None
        else:
            tag = identifier
            version = None

        if manager_deploy and not with_registry_auth:
            rollback_version = version
            if not rollback_version and tag:
                try:
                    versions_payload = manager_query.list_stack_versions(stack_name, namespace=namespace) if manager_query else {}
                except RuntimeError:
                    versions_payload = {}
                for item in versions_payload.get("versions", []):
                    if str(item.get("tag", "")) == tag:
                        rollback_version = str(item.get("version", ""))
                        break

            if rollback_version:
                if dry_run:
                    print(f"[manager] rollback dry-run target: {stack_name} v{rollback_version} ({namespace})")
                    return
                self.commands.append(
                    CallbackCommand(
                        f"docker-manager stack rollback {stack_name} v{rollback_version}",
                        lambda v=rollback_version: self._rollback_via_manager(
                            manager_deploy,
                            stack_name=stack_name,
                            namespace=namespace,
                            version=v,
                        ),
                    )
                )
                return

        compose_content = None
        if manager_query:
            try:
                payload = manager_query.get_stack_compose(
                    stack_name,
                    namespace=namespace,
                    version=version if version else None,
                    tag=tag,
                )
                compose_content = payload.get("compose")
                resolved = payload.get("version")
                if isinstance(resolved, str) and resolved.strip():
                    version = resolved
            except RuntimeError:
                compose_content = None

        if compose_content is None:
            if not version:
                versions_list = self.versions(stack_name, namespace=namespace, print_output=False)
                matching_versions = [v for v, t in versions_list if t == tag]
                if not matching_versions:
                    raise ValueError(f"No version found for tag '{tag}' in stack '{stack_name}'")
                version = matching_versions[0]
            compose_content = self.cat(stack_name, version, namespace=namespace)

        temp_file = f"/tmp/{stack_name}_v{version}.yml"
        with open(temp_file, "w") as f:
            f.write(compose_content)

        print(f"Deploying stack {stack_name} with version {version} (tag: {tag})...")
        self._deploy(
            stack_name,
            temp_file,
            compose_content,
            with_registry_auth=with_registry_auth,
            tag=tag,
            namespace=namespace,
            dry_run=dry_run,
        )

    def _deploy(
        self,
        stack_name,
        rendered_filename,
        rendered_content,
        with_registry_auth=False,
        tag=None,
        namespace: str = "default",
        dry_run: bool = False,
    ):
        labels = [
            f"mesudip.stack.name={stack_name}",
            f"com.mesudip.namespace={namespace}",
            f"com.mesudip.stack={stack_name}",
        ]
        if tag:
            labels.append(f"mesudip.stack.tag={tag}")

        manager_deploy = self._manager_client_for_feature(FEATURE_STACK_DEPLOY)
        if manager_deploy and not with_registry_auth and dry_run:
            self._validate_via_manager(
                manager_deploy,
                stack_name=stack_name,
                namespace=namespace,
                rendered_content=rendered_content,
            )
            return

        _, cmd = self.docker.config.increment(stack_name, rendered_content, labels=labels, stack=stack_name)
        if not cmd.isNop():
            self.commands.append(cmd)

        if manager_deploy and not with_registry_auth:
            self.commands.append(
                CallbackCommand(
                    f"docker-manager stack deploy {stack_name}",
                    lambda: self._deploy_via_manager(
                        manager_deploy,
                        stack_name=stack_name,
                        namespace=namespace,
                        rendered_content=rendered_content,
                    ),
                )
            )
            return

        cmd = ["docker", "stack", "deploy", "-c", str(rendered_filename), stack_name]
        if with_registry_auth:
            cmd.insert(3, "--with-registry-auth")
        self.commands.append(Command(cmd, give_console=True))

    @staticmethod
    def _validate_via_manager(
        manager_client: ManagerApiClient,
        *,
        stack_name: str,
        namespace: str,
        rendered_content: str,
    ) -> Optional[str]:
        payload = manager_client.validate_stack(
            stack=stack_name,
            namespace=namespace,
            compose=rendered_content,
            options={},
        )
        warnings = payload.get("warnings") or []
        for warning in warnings:
            print(f"[manager] {warning}")
        summary = payload.get("summary") or {}
        service_count = summary.get("service_count", 0)
        config_count = summary.get("config_count", 0)
        secret_count = summary.get("secret_count", 0)
        print(
            "[manager] validation: "
            f"services={service_count}, configs={config_count}, secrets={secret_count}"
        )
        return None

    @staticmethod
    def _deploy_via_manager(
        manager_client: ManagerApiClient,
        *,
        stack_name: str,
        namespace: str,
        rendered_content: str,
    ) -> Optional[str]:
        payload = manager_client.deploy_stack(
            stack=stack_name,
            namespace=namespace,
            compose=rendered_content,
            options={},
        )
        warnings = payload.get("warnings") or []
        for warning in warnings:
            print(f"[manager] {warning}")
        stdout = payload.get("stdout")
        stderr = payload.get("stderr")
        if isinstance(stdout, str) and stdout.strip():
            print(stdout.rstrip())
        if isinstance(stderr, str) and stderr.strip():
            print(stderr.rstrip())
        return None

    @staticmethod
    def _rollback_via_manager(
        manager_client: ManagerApiClient,
        *,
        stack_name: str,
        namespace: str,
        version: str,
    ) -> Optional[str]:
        payload = manager_client.rollback_stack(
            stack=stack_name,
            namespace=namespace,
            version=version,
        )
        warnings = payload.get("warnings") or []
        for warning in warnings:
            print(f"[manager] {warning}")
        stdout = payload.get("stdout")
        stderr = payload.get("stderr")
        if isinstance(stdout, str) and stdout.strip():
            print(stdout.rstrip())
        if isinstance(stderr, str) and stderr.strip():
            print(stderr.rstrip())
        return None

    def deploy(
        self,
        stack_name,
        compose_file,
        with_registry_auth=False,
        tag=None,
        show_generated=True,
        namespace: str = "default",
        dry_run: bool = False,
    ):
        self.generated_secrets = {}  # Reset for each deployment
        rendered_filename, rendered_content = self.render_compose_file(compose_file, stack=stack_name, include_build=False)
        self._deploy(
            stack_name,
            rendered_filename,
            rendered_content,
            with_registry_auth=with_registry_auth,
            tag=tag,
            namespace=namespace,
            dry_run=dry_run,
        )

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

    def rm(self, stack_name):
        self.commands.append(Command(["docker", "stack", "rm", stack_name], give_console=True))

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
            return cmd
        return None


class DockerNode:
    def __init__(self, docker: Docker):
        self.docker = docker

    @staticmethod
    def _format_labels(labels: Dict[str, str]) -> str:
        if not labels:
            return "-"
        parts = []
        for key, value in sorted(labels.items()):
            parts.append(key if value == "true" else f"{key}={value}")
        return ", ".join(parts)

    @staticmethod
    def _print_rows(rows: List[Dict[str, str]]):
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

    def ls(self):
        manager_query = self.docker.stack._manager_client_for_feature(FEATURE_STACK_QUERY)
        if manager_query:
            try:
                payload = manager_query.list_nodes()
                rows = []
                for item in payload.get("nodes", []):
                    manager_status = str(item.get("manager_status") or "").strip()
                    role = str(item.get("role") or "-")
                    role_display = f"{role} ({manager_status})" if manager_status else role
                    rows.append(
                        {
                            "hostname": str(item.get("hostname") or "-"),
                            "role": role_display,
                            "state": f"{item.get('state', '-')} / {item.get('availability', '-')}",
                            "address": str(item.get("address") or "-"),
                            "labels": DockerNode._format_labels(item.get("labels", {})),
                        }
                    )
                DockerNode._print_rows(rows)
                return rows
            except RuntimeError:
                pass

        nodes_output = run_cli_command(["docker", "node", "ls", "--format", "{{json .}}"], log=False)
        rows = []

        for line in nodes_output.splitlines():
            if not line:
                continue
            node = json.loads(line)
            inspect = json.loads(
                run_cli_command(["docker", "node", "inspect", node["ID"], "--format", "{{json .}}"], log=False)
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

        DockerNode._print_rows(rows)

        return rows


def open_context_shell(config_dir: Path, context_name: str) -> int:
    env = dict(os.environ)
    env["DOCKER_CONFIG"] = str(config_dir)
    env["DOCKER_CONTEXT"] = context_name
    shell = env.get("SHELL", "").strip() or "/bin/bash"
    return subprocess.run([shell, "-i"], check=False, env=env).returncode


def main(args: List[str] = None):
    parser = argparse.ArgumentParser(description="Deploy and manage Docker stacks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser("login", help="Authenticate Docker CLI against Docker-Manager")
    login_parser.add_argument("manager", nargs="?", help="Docker-Manager host or URL, for example 172.31.0.6:2378")
    login_parser.add_argument("--manager-url", help="Docker-Manager base URL")
    login_parser.add_argument("--context", "--context-name", dest="context_name", help="Docker context name to create or update")
    login_parser.add_argument("--timeout-secs", type=int, help="Login timeout in seconds")

    shell_parser = subparsers.add_parser("shell", help="Open an isolated bash shell for a Docker-Manager context")
    shell_parser.add_argument("target", nargs="?", help="Context name, or manager host/URL when used with --context")
    shell_parser.add_argument("--context", "--context-name", dest="context_name", help="Shell context name to create or reuse")
    shell_parser.add_argument("--timeout-secs", type=int, help="Login timeout in seconds")

    context_parser = subparsers.add_parser("context", help="Manage Docker context switching with docker-stack cleanup")
    context_subparsers = context_parser.add_subparsers(dest="context_command", required=True)
    context_use_parser = context_subparsers.add_parser("use", help="Switch Docker context and clean up manager auth when needed")
    context_use_parser.add_argument("context_name", help="Docker context name to activate")

    setup_auth_parser = subparsers.add_parser(
        "setup-auth",
        help="Configure isolated Docker auth/context for CI or non-interactive Docker-Manager access",
    )
    setup_auth_parser.add_argument("manager", nargs="?", help="Docker-Manager host or URL, for example 172.31.0.6:2378")
    setup_auth_parser.add_argument("--manager-url", help="Docker-Manager base URL")
    setup_auth_parser.add_argument("--context", "--context-name", dest="context_name", help="Docker context name to create or update")
    setup_auth_parser.add_argument("--timeout-secs", type=int, help="Setup timeout in seconds")
    setup_auth_parser.add_argument("--docker-config-dir", help="Docker config directory to create or reuse")
    setup_auth_parser.add_argument("--access-token", help="Pre-issued manager access token")
    setup_auth_parser.add_argument("--github-oidc-token", help="GitHub OIDC token for trusted publishing")
    setup_auth_parser.add_argument("--verify-ssl", action="store_true", help=argparse.SUPPRESS)

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
    deploy_parser.add_argument("--namespace", default=os.getenv("DOCKER_STACK_NAMESPACE", "default"), help="Deployment namespace")
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
    cat_parser.add_argument("--namespace", default=os.getenv("DOCKER_STACK_NAMESPACE", "default"), help="Stack namespace")

    checkout_parser = subparsers.add_parser("checkout", help="Deploy specific version of the stack")
    checkout_parser.add_argument("stack_name", help="Name of the stack")
    checkout_parser.add_argument("version", help="Stack version to cat")
    checkout_parser.add_argument("--namespace", default=os.getenv("DOCKER_STACK_NAMESPACE", "default"), help="Stack namespace")

    # version_parser = subparsers.add_parser("version",help="Deploy specific version of the stack")
    # version_parser.add_argument("stack_name", help="Name of the stack")
    # version_parser.add_argument("version","versions", help="Stack version to cat")

    version_parser = subparsers.add_parser("version", aliases=["versions"], help="Deploy specific version of the stack")
    version_parser.add_argument("stack_name", help="Name of the stack")
    version_parser.add_argument("--namespace", default=os.getenv("DOCKER_STACK_NAMESPACE", "default"), help="Stack namespace")

    parser.add_argument(
        "-u", "--user", help="Registry credentials in format hostname:username:password", action="append", required=False, default=[]
    )
    parser.add_argument("-t", "--tag", help="Tag the current deployment for later checkout", required=False)
    parser.add_argument(
        "-ro", "-r", "--ro", "--r", "--dry-run", action="store_true", help="Print commands, don't execute them", required=False
    )
    parser.add_argument("--show-generated", action="store_true", default=True, help="Show newly generated secrets after deployment")

    args = parser.parse_args(args if args else sys.argv[1:])

    if args.command == "login":
        try:
            config = resolve_login_config(
                manager_url=args.manager_url,
                manager_target=args.manager,
                context_name=args.context_name,
                timeout_secs=args.timeout_secs,
            )
            result = docker_manager_login(config)
        except RuntimeError as exc:
            print(f"docker-stack login: {exc}", file=sys.stderr)
            sys.exit(2)
        print("Docker-Manager browser login successful.")
        print(f"Callback: {result.redirect_uri}")
        print(f"DOCKER_CONTEXT={config.context_name}")
        print(f"Context host={config.docker_context_host}")
        if config.manager_url.startswith("https://"):
            print(f"TLS detected for manager endpoint ({'verification skipped' if config.skip_tls_verify else 'verified'})")
        expiry = format_expiry(result.expires_at)
        if expiry:
            print(f"Access token expires in {expiry}")
        print("Try: docker ps")
        return

    if args.command == "shell":
        shell_name = args.target if args.context_name is None else None
        manager_target = args.target if args.context_name is not None else None
        try:
            config = resolve_shell_login_config(
                shell_name=shell_name,
                manager_target=manager_target,
                context_name=args.context_name,
                timeout_secs=args.timeout_secs,
            )
            config_dir, result = ensure_isolated_login(config)
        except RuntimeError as exc:
            print(f"docker-stack shell: {exc}", file=sys.stderr)
            sys.exit(2)
        print(f"Shell context={config.context_name}")
        print(f"Shell manager={config.manager_url}")
        print(f"DOCKER_CONFIG={config_dir}")
        if result is not None:
            expiry = format_expiry(result.expires_at)
            if expiry:
                print(f"Access token expires in {expiry}")
        else:
            print("Access token already active.")
        sys.exit(open_context_shell(config_dir, config.context_name))

    if args.command == "setup-auth":
        try:
            config = resolve_login_config(
                manager_url=args.manager_url,
                manager_target=args.manager,
                context_name=args.context_name,
                timeout_secs=args.timeout_secs,
                verify_ssl=args.verify_ssl,
            )
            result = docker_manager_setup_auth(
                config,
                access_token=args.access_token,
                github_oidc_token=args.github_oidc_token,
                docker_config_dir=Path(args.docker_config_dir) if args.docker_config_dir else None,
            )
        except RuntimeError as exc:
            print(f"docker-stack setup-auth: {exc}", file=sys.stderr)
            sys.exit(2)
        print(f"DOCKER_CONFIG={result.docker_config_dir}")
        print(f"DOCKER_CONTEXT={result.context_name}")
        print(f"MANAGER_URL={result.manager_url}")
        print(f"SKIP_TLS_VERIFY={'true' if result.skip_tls_verify else 'false'}")
        expiry = format_expiry(result.expires_at)
        if expiry:
            print(f"ACCESS_TOKEN_EXPIRES_IN={expiry}")
        print(f"VALIDATION_SKIPPED={'true' if result.validation_skipped else 'false'}")
        return

    if args.command == "context":
        if args.context_command == "use":
            try:
                manager_context = switch_docker_context(args.context_name)
            except RuntimeError as exc:
                print(f"docker-stack context use: {exc}", file=sys.stderr)
                sys.exit(2)
            print(f"DOCKER_CONTEXT={args.context_name}")
            if manager_context:
                print("Docker-Manager auth header preserved.")
            else:
                print("Docker-Manager auth header cleared from ~/.docker/config.json.")
            return

    docker = Docker(registries=args.user)
    docker.load_env()

    if args.command == "build":
        docker.stack.build_and_push(args.compose_file, push=args.push)
    elif args.command == "push":
        docker.stack.push(args.compose_file)
    elif args.command == "deploy":
        docker.stack.deploy(
            args.stack_name,
            args.compose_file,
            args.with_registry_auth,
            tag=args.tag,
            show_generated=args.show_generated,
            namespace=args.namespace,
            dry_run=args.ro,
        )
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
            versions_list = docker.stack.versions(args.stack_name, namespace=args.namespace, print_output=False)
            if versions_list:
                # Assuming versions are integers, find the maximum
                latest_version = max(int(v[0]) for v in versions_list if v[0].isdigit())
                version_to_cat = str(latest_version)
            else:
                print(f"No versions found for stack '{args.stack_name}'.")
                sys.exit(1)
        print(docker.stack.cat(args.stack_name, version_to_cat, namespace=args.namespace))
    elif args.command == "checkout":
        docker.stack.checkout(
            args.stack_name,
            args.version,
            namespace=args.namespace,
            dry_run=args.ro,
        )
    elif args.command == "versions" or args.command == "version":
        docker.stack.versions(args.stack_name, namespace=args.namespace)
    if args.ro:
        print("Following commands were not executed:")
        [print(" >> " + str(x)) for x in docker.stack.commands if x]
    else:
        [x.execute() for x in docker.stack.commands]


if __name__ == "__main__":
    main()
