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
    browser_login,
    ensure_isolated_login,
    format_expiry,
    login as docker_manager_login,
    persist_shell_context,
    resolve_login_config,
    resolve_shell_login_config,
    setup_auth as docker_manager_setup_auth,
    switch_docker_context,
    UnknownShellContextError,
)
from docker_stack.shell_auth import (
    SHELL_CONTEXT_ENV,
    ShellAuthError,
    ensure_shell_access,
    login_for_active_shell,
    run_managed_shell,
    shell_session_active,
)
from docker_stack.manager_api import (
    FEATURE_STACK_DEPLOY,
    FEATURE_STACK_QUERY,
    ManagerApiClient,
    discover_manager_client,
)
from docker_stack.registry import DockerRegistry
from .envsubst import LineCheckResult, SubstitutionError, envsubst, envsubst_load_file

DOCKER_SHELL_ENDPOINT_ENV_VARS = (
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "DOCKER_API_VERSION",
    "DOCKER_MACHINE_NAME",
)
DEFAULT_NAMESPACE = "default"


def physical_stack_name(namespace: str, stack: str) -> str:
    return stack if namespace == DEFAULT_NAMESPACE else f"{namespace}--{stack}"


def _namespace_default() -> str:
    return os.getenv("DOCKER_STACK_NAMESPACE", DEFAULT_NAMESPACE).strip() or DEFAULT_NAMESPACE


def _add_namespace_argument(parser: argparse.ArgumentParser, help_text: str = "Stack namespace") -> None:
    parser.add_argument(
        "-n",
        "--namespace",
        default=_namespace_default(),
        help=f"{help_text} (default: %(default)s)",
    )


def _add_listing_namespace_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-n",
        "--namespace",
        default=_namespace_default(),
        help="Stack namespace (default: %(default)s)",
    )
    group.add_argument(
        "-A",
        "--all-namespaces",
        "-all-namespaces",
        action="store_true",
        help="List stacks across all namespaces",
    )


def _prompt_for_manager_target(context_name: str) -> str:
    if not sys.stdin.isatty():
        raise RuntimeError(
            f"Unknown shell context '{context_name}'. Run "
            f"'docker-stack shell --context {context_name} <manager-url>' to create it."
        )
    print(f"Docker context '{context_name}' does not exist yet.")
    print("Enter the Docker-Manager URL to create it (for example https://manager.example.com:2376).")
    target = input(f"Manager URL for {context_name}: ").strip()
    if not target:
        raise RuntimeError(f"Manager URL is required to create shell context '{context_name}'")
    return target


@dataclass
class EnvFileEntry:
    key: str
    value: str
    line_no: int
    line_content: str
    value_start_index: int
    value_inner_offset: int


@dataclass
class RenderedCompose:
    clean: str
    enriched: str


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


def _resolve_env_entries(
    entries: List[EnvFileEntry], env_file: str, base_env: Optional[Dict[str, str]] = None, max_cycles: int = 5
) -> Dict[str, str]:
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
        deduped = {(result.line_no, result.variable_name, result.start_index): result for result in cyclic_results}
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

    @staticmethod
    def _b64_content(content: bytes) -> str:
        return base64.b64encode(content).decode("utf-8")

    @staticmethod
    def _strip_source_metadata(compose_content: str) -> str:
        compose_data = yaml.safe_load(compose_content) or {}
        if isinstance(compose_data, dict):
            compose_data.pop("x-files", None)
        return yaml.dump(compose_data, sort_keys=False)

    @staticmethod
    def _iter_env_refs(value: str):
        for match in ENV_VAR_PATTERN.finditer(value):
            name = match.group(1) if match.group(1) is not None else match.group(3)
            default = match.group(2) if match.group(1) is not None else None
            yield name, default

    @staticmethod
    def _secret_environment_vars(compose_data: dict) -> set:
        secret_env_vars = set()
        secrets = compose_data.get("secrets", {})
        if not isinstance(secrets, dict):
            return secret_env_vars
        for details in secrets.values():
            if isinstance(details, dict) and "environment" in details:
                env_name = str(details["environment"]).strip()
                if env_name:
                    secret_env_vars.add(env_name)
        return secret_env_vars

    @staticmethod
    def _relative_source_name(path: Path, base_dir: Path) -> str:
        return os.path.relpath(path, base_dir).replace(os.sep, "/")

    def _build_env_xfile(self, sources: List[str], excluded_names: set) -> Optional[Dict[str, str]]:
        seen = set()
        lines = []
        for source in sources:
            for name, default in self._iter_env_refs(source):
                if name in seen or name in excluded_names:
                    continue
                value = os.environ.get(name)
                if value in (None, "") and default is not None:
                    value = default
                if value in (None, ""):
                    continue
                seen.add(name)
                lines.append(f"{name}={value}")
        if not lines:
            return None
        content = ("\n".join(lines) + "\n").encode("utf-8")
        return {"path": ".env", "content": self._b64_content(content)}

    def _prepare_manager_secret_sources(self, compose_data: dict, base_dir: Path) -> None:
        secrets = compose_data.get("secrets", {})
        if not isinstance(secrets, dict):
            return
        for name, details in secrets.items():
            if not isinstance(details, dict):
                continue
            if "x-template-file" in details:
                filename = base_dir / details["x-template-file"]
                details["x-content"] = envsubst_load_file(str(filename), os.environ)
                details.pop("x-template-file", None)
            elif "file" in details:
                filename = base_dir / details["file"]
                details["x-content"] = filename.read_text(encoding="utf-8")
                details.pop("file", None)
            elif "environment" in details:
                env_name = str(details["environment"]).strip()
                env_value = os.environ.get(env_name)
                if env_value in (None, ""):
                    raise ValueError(f"Secret {name} references environment variable {env_name}, but it is not set.")
                details["x-content"] = env_value
                details.pop("environment", None)

    def _collect_config_source_files(self, compose_data: dict, base_dir: Path) -> Tuple[List[Dict[str, str]], List[str]]:
        x_files = []
        env_sources = []
        seen_paths = set()
        configs = compose_data.get("configs", {})
        if not isinstance(configs, dict):
            return x_files, env_sources
        for details in configs.values():
            if not isinstance(details, dict):
                continue
            source_path = details.get("x-template-file", details.get("file"))
            if not source_path:
                continue
            filename = (base_dir / source_path).resolve()
            relative_path = self._relative_source_name(filename, base_dir)
            content = filename.read_bytes()
            if relative_path not in seen_paths:
                x_files.append(
                    {
                        "path": relative_path,
                        "content": self._b64_content(content),
                    }
                )
                seen_paths.add(relative_path)
            env_sources.append(content.decode("utf-8", errors="ignore"))
        return x_files, env_sources

    def rendered_compose_file(
        self,
        compose_file,
        stack=None,
        include_build=True,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> RenderedCompose:
        compose_path = Path(compose_file).resolve()
        template_bytes = compose_path.read_bytes()
        template_content = template_bytes.decode("utf-8")
        # Parse the YAML content
        compose_data = self.decode_yaml(template_content)
        base_dir = compose_path.parent
        source_x_files: List[Dict[str, str]] = []
        env_sources = [template_content]
        secret_env_vars = self._secret_environment_vars(compose_data)
        manager_deploy = self._manager_client_for_feature(FEATURE_STACK_DEPLOY) if stack else None
        if manager_deploy:
            self._prepare_manager_secret_sources(compose_data, base_dir)
        if stack:
            source_x_files, config_env_sources = self._collect_config_source_files(compose_data, base_dir)
            env_sources.extend(config_env_sources)
        if not include_build:
            services: dict = compose_data.get("services", {})
            for k, v in services.items():
                if "build" in v:
                    del v["build"]
        # A manager-backed deploy must send portable source definitions to the
        # stack API. The manager owns validation, config/secret versioning and
        # materialization; resolving those objects here can mutate a dry-run and
        # can pin a service to an old physical object name. Raw daemon deploys
        # keep the legacy client-side materialization path.
        if stack and not manager_deploy:
            if "configs" in compose_data:
                compose_data["configs"] = self._process_x_content(
                    compose_data["configs"],
                    self.docker.config,
                    base_dir=str(base_dir),
                    stack=stack,
                    namespace=namespace,
                )
            if "secrets" in compose_data:
                compose_data["secrets"] = self._process_x_content(
                    compose_data["secrets"],
                    self.docker.secret,
                    base_dir=str(base_dir),
                    stack=stack,
                    namespace=namespace,
                )

        # Define the replacements for '$' to '$$' for env variables in compose files
        replacements_map = {"$": "$$"}
        clean_content = envsubst(yaml.dump(compose_data, sort_keys=False), replacements=replacements_map)
        enriched_data = yaml.safe_load(clean_content) or {}
        x_files = [
            {"path": "compose.yml", "content": self._b64_content(template_bytes)},
        ]
        env_xfile = self._build_env_xfile(env_sources, secret_env_vars)
        if env_xfile:
            x_files.append(env_xfile)
        x_files.extend(source_x_files)
        if isinstance(enriched_data, dict):
            enriched_data["x-files"] = x_files
        enriched_content = yaml.dump(enriched_data, sort_keys=False)
        return RenderedCompose(clean=clean_content, enriched=enriched_content)

    def decode_yaml(self, data: str) -> dict:
        return yaml.safe_load(data)

    def render_compose_file(self, compose_file, stack=None, include_build=True, namespace: str = DEFAULT_NAMESPACE):
        """
        Render the Docker Compose file with environment variables and create Docker configs/secrets.
        """
        return self.rendered_compose_file(
            compose_file,
            stack,
            include_build=include_build,
            namespace=namespace,
        )

    def _process_x_content(
        self,
        objects,
        manager: DockerObjectManager,
        base_dir="",
        stack=None,
        namespace: str = DEFAULT_NAMESPACE,
    ):
        """
        Process configs or secrets with x-content keys.
        Returns a tuple: (processed_objects, commands)
        """
        processed_objects = {}
        manager_client = self._manager_client_for_feature(FEATURE_STACK_DEPLOY)
        object_stack = physical_stack_name(namespace, stack) if stack else None

        def docker_object_name_for(name, explicit_name=None):
            if explicit_name:
                return explicit_name
            if object_stack:
                return f"{object_stack}_{name}"
            return name

        def normalize_labels(labels):
            normalized = {}
            for raw in labels:
                key, _, value = raw.partition("=")
                if key and _:
                    normalized[key] = value
            return normalized

        def add_obj(name, data, explicit_name=None, is_generated_secret=False, generate_options=None):
            labels = []
            if is_generated_secret:
                labels.append("mesudip.secret.generated=true")

            docker_object_name = docker_object_name_for(name, explicit_name=explicit_name)

            if manager_client and stack:
                manager_object_name = explicit_name or name
                label_map = normalize_labels(labels)
                if manager.object_type == "config":
                    response = manager_client.resolve_config(
                        stack=stack,
                        namespace=namespace,
                        name=manager_object_name,
                        content=data,
                        labels=label_map,
                    )
                else:
                    response = manager_client.resolve_secret(
                        stack=stack,
                        namespace=namespace,
                        name=manager_object_name,
                        content=None if is_generated_secret else data,
                        generate=generate_options if is_generated_secret else None,
                        labels=label_map,
                        return_generated_value=is_generated_secret,
                    )
                    if is_generated_secret and response.get("generated_value"):
                        self.generated_secrets[name] = response["generated_value"]
                processed_objects[name] = {"name": response["actual_name"], "external": True}
                return

            object_name, command = manager.create(docker_object_name, data, labels=labels, stack=object_stack)
            if not command.isNop():
                self.commands.append(command)
                if is_generated_secret:
                    self.generated_secrets[name] = data
            processed_objects[name] = {"name": object_name, "external": True}

        for name, details in objects.items():
            explicit_name = details.get("name") if isinstance(details, dict) else None
            if isinstance(details, dict) and "x-content" in details:
                add_obj(name, details["x-content"], explicit_name=explicit_name)
            elif isinstance(details, dict) and "x-template" in details:
                add_obj(name, envsubst(details["x-template"], os.environ), explicit_name=explicit_name)
            elif isinstance(details, dict) and "x-template-file" in details:
                filename = os.path.join(base_dir, details["x-template-file"])
                add_obj(name, envsubst_load_file(filename, os.environ), explicit_name=explicit_name)
            elif isinstance(details, dict) and "file" in details:
                filename = os.path.join(base_dir, details["file"])
                with open(filename) as file:
                    add_obj(name, file.read(), explicit_name=explicit_name)
            elif isinstance(details, dict) and manager.object_type == "secret" and "environment" in details:
                env_name = str(details["environment"]).strip()
                env_value = os.environ.get(env_name)
                if env_value in (None, ""):
                    raise ValueError(f"Secret {name} references environment variable {env_name}, but it is not set.")
                add_obj(name, env_value, explicit_name=explicit_name)
            elif isinstance(details, dict) and manager.object_type == "secret" and ("x-generate" in details or "x-generated" in details):
                # Support both spellings for compatibility.
                generate_options = details.get("x-generate", details.get("x-generated"))
                if isinstance(generate_options, bool) and generate_options:
                    if manager_client and stack:
                        add_obj(name, "", explicit_name=explicit_name, is_generated_secret=True, generate_options={})
                    else:
                        add_obj(name, generate_secret(), explicit_name=explicit_name, is_generated_secret=True)
                elif isinstance(generate_options, int):
                    if manager_client and stack:
                        add_obj(
                            name, "", explicit_name=explicit_name, is_generated_secret=True, generate_options={"length": generate_options}
                        )
                    else:
                        add_obj(name, generate_secret(length=generate_options), explicit_name=explicit_name, is_generated_secret=True)
                elif isinstance(generate_options, dict):
                    options = {
                        "length": generate_options.get("length"),
                        "numbers": generate_options.get("numbers", True),
                        "special": generate_options.get("special", True),
                        "uppercase": generate_options.get("uppercase", True),
                    }
                    if manager_client and stack:
                        add_obj(name, "", explicit_name=explicit_name, is_generated_secret=True, generate_options=options)
                    else:
                        add_obj(
                            name,
                            generate_secret(
                                length=options.get("length"),
                                numbers=options.get("numbers", True),
                                special=options.get("special", True),
                                uppercase=options.get("uppercase", True),
                            ),
                            explicit_name=explicit_name,
                            is_generated_secret=True,
                        )
                else:
                    raise ValueError(f"Invalid x-generate value for secret {name}: {generate_options}")
            else:
                processed_objects[name] = details
        return processed_objects

    def _manager_client_for_feature(self, feature_name: str) -> Optional[ManagerApiClient]:
        client = self.docker.manager_client()
        if not client:
            if os.getenv("DOCKER_MANAGER_URL", "").strip():
                raise RuntimeError(
                    "Docker-Manager is configured via DOCKER_MANAGER_URL, but docker-stack could not initialize a manager client"
                )
            return None
        try:
            if client.supports(feature_name):
                return client
        except RuntimeError as exc:
            if os.getenv("DOCKER_MANAGER_URL", "").strip():
                raise RuntimeError(f"Docker-Manager feature detection failed for {feature_name}: {exc}") from exc
            return None
        if os.getenv("DOCKER_MANAGER_URL", "").strip():
            raise RuntimeError(
                f"Docker-Manager is configured via DOCKER_MANAGER_URL, but /version did not advertise required feature {feature_name}"
            )
        return None

    @staticmethod
    def _raise_if_manager_is_explicit(exc: RuntimeError) -> None:
        if os.getenv("DOCKER_MANAGER_URL", "").strip():
            raise exc

    @staticmethod
    def _normalize_version(value: str) -> str:
        if value.startswith("v") or value.startswith("V"):
            return value[1:]
        return value

    @staticmethod
    def _version_sort_key(raw: str):
        return int(raw) if raw.isdigit() else raw

    @staticmethod
    def _compact_versions(versions: List[str]) -> str:
        numeric = sorted({int(version.lstrip("vV")) for version in versions if version.lstrip("vV").isdigit()})
        other = sorted({version for version in versions if not version.lstrip("vV").isdigit()})
        compact = []
        start = end = None

        def append_range(first: int, last: int) -> None:
            if last - first >= 2:
                compact.append(f"v{first}...v{last}")
            elif last == first:
                compact.append(f"v{first}")
            else:
                compact.extend((f"v{first}", f"v{last}"))

        for version in numeric:
            if start is None:
                start = end = version
            elif version == end + 1:
                end = version
            else:
                append_range(start, end)
                start = end = version
        if start is not None:
            append_range(start, end)
        compact.extend(other)
        return ", ".join(compact)

    def _print_stack_listing(self, stack_versions: Dict[Tuple[str, str], List[str]], namespace: Optional[str]) -> None:
        max_stack_name_length = max((len(stack) for _, stack in stack_versions), default=10)
        header_stack = "Stack Name".ljust(max_stack_name_length)

        if namespace is not None:
            print(f"Namespace: {namespace}")
            print(f"{header_stack} | Versions")
            print("-" * (max_stack_name_length + 12))
        else:
            max_namespace_length = max((len(item_namespace) for item_namespace, _ in stack_versions), default=9)
            header_namespace = "Namespace".ljust(max_namespace_length)
            print("Namespace: all")
            print(f"{header_namespace} | {header_stack} | Versions")
            print("-" * (max_namespace_length + max_stack_name_length + 15))

        for (item_namespace, stack), versions in sorted(stack_versions.items()):
            versions_str = self._compact_versions(versions)
            if namespace is None:
                print(f"{item_namespace.ljust(max_namespace_length)} | {stack.ljust(max_stack_name_length)} | {versions_str}")
            else:
                print(f"{stack.ljust(max_stack_name_length)} | {versions_str}")

    def _print_versions(self, versions_list: List[Tuple[str, str]]) -> None:
        rows = [("Version", "Tag")] + versions_list
        max_version_length = max(len(v[0]) for v in rows)
        max_tag_length = max(len(v[1]) for v in rows)

        print(f"{'Version'.ljust(max_version_length)} | {'Tag'.ljust(max_tag_length)}")
        print("-" * (max_version_length + max_tag_length + 3))

        for version, tag in sorted(versions_list, key=lambda x: self._version_sort_key(x[0])):
            print(f"{version.ljust(max_version_length)} | {tag.ljust(max_tag_length)}")

    def ls(self, namespace: Optional[str] = DEFAULT_NAMESPACE):
        client = self._manager_client_for_feature(FEATURE_STACK_QUERY)
        if client:
            try:
                payload = client.list_stacks(namespace=namespace)
                stack_versions = {
                    (str(item.get("namespace") or namespace or DEFAULT_NAMESPACE), str(item.get("stack"))): [
                        str(version)
                        for version in (
                            item.get("versions") if isinstance(item.get("versions"), list) else item.get("available_versions", [])
                        )
                        if str(version).strip()
                    ]
                    for item in payload.get("stacks", [])
                    if str(item.get("stack", "")).strip()
                }
                self._print_stack_listing(stack_versions, namespace)
                if namespace is not None:
                    return {stack: versions for (_, stack), versions in stack_versions.items()}
                return stack_versions
            except RuntimeError as exc:
                self._raise_if_manager_is_explicit(exc)

        cmd = ["docker", "config", "ls", "--format", "{{.ID}}\t{{.Name}}\t{{.Labels}}"]
        raw_output = run_cli_command(cmd, log=False)
        output = raw_output.split("\n") if raw_output else []
        stack_versions: Dict[Tuple[str, str], List[str]] = {}

        for line in output:
            parts = line.split("\t")
            if len(parts) == 3 and "mesudip.stack.name" in parts[2]:
                labels = {k: v for k, v in (label.split("=") for label in parts[2].split(",") if "=" in label)}
                stack_name = labels.get("mesudip.stack.name")
                version = labels.get("mesudip.object.version", "unknown")
                object_namespace = labels.get("com.mesudip.namespace", DEFAULT_NAMESPACE)

                if stack_name and (namespace is None or object_namespace == namespace):
                    stack_versions.setdefault((object_namespace, stack_name), []).append(version)

        self._print_stack_listing(stack_versions, namespace)
        if namespace is not None:
            return {stack: versions for (_, stack), versions in stack_versions.items()}
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
            except RuntimeError as exc:
                self._raise_if_manager_is_explicit(exc)

        history_name = physical_stack_name(namespace, name)
        if normalized_version == "1":
            config_name = history_name
        else:
            config_name = f"{history_name}_v{normalized_version}"

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
            except RuntimeError as exc:
                self._raise_if_manager_is_explicit(exc)

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
                object_namespace = labels.get("com.mesudip.namespace", DEFAULT_NAMESPACE)

                if stack == stack_name and object_namespace == namespace:
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
                except RuntimeError as exc:
                    self._raise_if_manager_is_explicit(exc)
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
            except RuntimeError as exc:
                self._raise_if_manager_is_explicit(exc)
                compose_content = None

        if compose_content is None:
            if not version:
                versions_list = self.versions(stack_name, namespace=namespace, print_output=False)
                matching_versions = [v for v, t in versions_list if t == tag]
                if not matching_versions:
                    raise ValueError(f"No version found for tag '{tag}' in stack '{stack_name}'")
                version = matching_versions[0]
            compose_content = self.cat(stack_name, version, namespace=namespace)

        deploy_content = self._strip_source_metadata(compose_content)
        print(f"Deploying stack {stack_name} with version {version} (tag: {tag})...")
        self._deploy(
            stack_name,
            deploy_content,
            stored_content=compose_content,
            with_registry_auth=with_registry_auth,
            tag=tag,
            namespace=namespace,
            dry_run=dry_run,
        )

    def _deploy(
        self,
        stack_name,
        rendered_content,
        stored_content=None,
        with_registry_auth=False,
        tag=None,
        namespace: str = "default",
        dry_run: bool = False,
        prune: bool = False,
        resolve_image: Optional[str] = None,
        force_update: bool = False,
    ):
        stored_content = stored_content if stored_content is not None else rendered_content
        labels = [
            f"mesudip.stack.name={stack_name}",
            f"com.mesudip.namespace={namespace}",
            f"com.mesudip.stack={stack_name}",
        ]
        if tag:
            labels.append(f"mesudip.stack.tag={tag}")

        manager_deploy = self._manager_client_for_feature(FEATURE_STACK_DEPLOY)
        if manager_deploy and tag:
            raise RuntimeError(
                "Docker-Manager stack deploy does not expose deployment tags; refusing to silently ignore --tag"
            )
        manager_options = {}
        if with_registry_auth:
            manager_options["with_registry_auth"] = True
        if prune:
            manager_options["prune"] = True
        if resolve_image:
            manager_options["resolve_image"] = resolve_image
        if force_update:
            manager_options["force_update"] = True
        if manager_deploy and dry_run:
            self._validate_via_manager(
                manager_deploy,
                stack_name=stack_name,
                namespace=namespace,
                rendered_content=stored_content,
                options=manager_options,
            )
            return

        # Manager-backed deploys must stay on the manager stack APIs. Hitting
        # direct daemon config endpoints here breaks GitHub OIDC workflows,
        # which are intentionally restricted away from generic daemon access.
        if manager_deploy:
            self.commands.append(
                CallbackCommand(
                    f"docker-manager stack deploy {stack_name}",
                    lambda: self._deploy_via_manager(
                        manager_deploy,
                        stack_name=stack_name,
                        namespace=namespace,
                        rendered_content=stored_content,
                        options=manager_options,
                    ),
                )
            )
            return

        if force_update:
            raise RuntimeError("--force-update is supported only by the Docker-Manager stack deploy API")

        physical_stack = physical_stack_name(namespace, stack_name)
        _, cmd = self.docker.config.increment(
            physical_stack,
            stored_content,
            labels=labels,
            stack=physical_stack,
        )
        if not cmd.isNop():
            self.commands.append(cmd)

        cmd = ["docker", "stack", "deploy", "-c", "-", physical_stack]
        if with_registry_auth:
            cmd.insert(3, "--with-registry-auth")
        if prune:
            cmd.insert(3, "--prune")
        if resolve_image:
            cmd[3:3] = ["--resolve-image", resolve_image]
        self.commands.append(Command(cmd, stdin=rendered_content, give_console=True))

    @staticmethod
    def _validate_via_manager(
        manager_client: ManagerApiClient,
        *,
        stack_name: str,
        namespace: str,
        rendered_content: str,
        options: Dict[str, object],
    ) -> Optional[str]:
        payload = manager_client.validate_stack(
            stack=stack_name,
            namespace=namespace,
            compose=rendered_content,
            options=options,
        )
        warnings = payload.get("warnings") or []
        for warning in warnings:
            print(f"[manager] {warning}")
        summary = payload.get("summary") or {}
        service_count = summary.get("service_count", 0)
        config_count = summary.get("config_count", 0)
        secret_count = summary.get("secret_count", 0)
        print("[manager] validation: " f"services={service_count}, configs={config_count}, secrets={secret_count}")
        return None

    @staticmethod
    def _deploy_via_manager(
        manager_client: ManagerApiClient,
        *,
        stack_name: str,
        namespace: str,
        rendered_content: str,
        options: Dict[str, object],
    ) -> Optional[str]:
        if not hasattr(manager_client, "deploy_stack_stream"):
            payload = manager_client.deploy_stack(
                stack=stack_name,
                namespace=namespace,
                compose=rendered_content,
                options=options,
            )
        else:
            try:
                payload = manager_client.deploy_stack_stream(
                    stack=stack_name,
                    namespace=namespace,
                    compose=rendered_content,
                    options=options,
                    on_event=DockerStack._print_manager_deploy_event,
                )
            except RuntimeError as exc:
                message = str(exc)
                if "POST /api/stacks/deploy/stream" not in message or "HTTP 404" not in message:
                    raise
                payload = manager_client.deploy_stack(
                    stack=stack_name,
                    namespace=namespace,
                    compose=rendered_content,
                    options=options,
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
        generated_secrets = payload.get("generated_secrets") or []
        if generated_secrets:
            print("\n----- Newly Generated Secrets -----")
            for item in generated_secrets:
                if not isinstance(item, dict):
                    continue
                logical_name = str(item.get("logical_name") or item.get("actual_name") or "secret")
                value = item.get("value")
                if value is not None:
                    print(f"{logical_name}: {value}")
            print("---------------------------------\n")
        return None

    @staticmethod
    def _print_manager_deploy_event(event: Dict[str, object]) -> None:
        event_name = event.get("event")
        data = event.get("data")
        if not isinstance(data, dict):
            data = {}
        if event_name == "stdout":
            line = str(data.get("line") or "").rstrip()
            if line:
                print(line, flush=True)
        elif event_name == "stderr":
            line = str(data.get("line") or "").rstrip()
            if line:
                print(line, file=sys.stderr, flush=True)
        elif event_name == "command":
            program = data.get("program") or "manager"
            stack = data.get("stack")
            suffix = f" {stack}" if stack else ""
            print(f"[manager] {program}{suffix}", flush=True)
        elif event_name == "resource":
            kind = data.get("kind")
            logical_name = data.get("logical_name")
            actual_name = data.get("actual_name")
            if kind and logical_name and actual_name:
                print(f"[manager] {kind} {logical_name} -> {actual_name}", flush=True)
        elif event_name == "service_progress":
            service = data.get("service") or "service"
            status = data.get("status") or "progress"
            message = data.get("message") or data.get("error") or ""
            if message:
                print(f"[manager] {service}: {status}: {message}", flush=True)

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
        prune: bool = False,
        resolve_image: Optional[str] = None,
        force_update: bool = False,
    ):
        self.generated_secrets = {}  # Reset for each deployment
        rendered = self.render_compose_file(
            compose_file,
            stack=stack_name,
            include_build=False,
            namespace=namespace,
        )
        self._deploy(
            stack_name,
            rendered.clean,
            stored_content=rendered.enriched,
            with_registry_auth=with_registry_auth,
            tag=tag,
            namespace=namespace,
            dry_run=dry_run,
            prune=prune,
            resolve_image=resolve_image,
            force_update=force_update,
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

    def rm(self, stack_name, namespace: str = "default"):
        manager = self._manager_client_for_feature(FEATURE_STACK_DEPLOY)
        if manager:
            self.commands.append(
                CallbackCommand(
                    f"docker-manager stack rm {stack_name}",
                    lambda: manager.remove_stack(stack=stack_name, namespace=namespace),
                )
            )
            return
        self.commands.append(
            Command(
                ["docker", "stack", "rm", physical_stack_name(namespace, stack_name)],
                give_console=True,
            )
        )

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

        widths = {key: max(len(title), max((len(str(row[key])) for row in rows), default=0)) for title, key in columns}
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
            except RuntimeError as exc:
                self.docker.stack._raise_if_manager_is_explicit(exc)

        nodes_output = run_cli_command(["docker", "node", "ls", "--format", "{{json .}}"], log=False)
        rows = []

        for line in nodes_output.splitlines():
            if not line:
                continue
            node = json.loads(line)
            inspect = json.loads(run_cli_command(["docker", "node", "inspect", node["ID"], "--format", "{{json .}}"], log=False))
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
    for key in DOCKER_SHELL_ENDPOINT_ENV_VARS:
        env.pop(key, None)
    env["DOCKER_CONFIG"] = str(config_dir)
    env["DOCKER_CONTEXT"] = context_name
    shell = env.get("SHELL", "").strip() or "/bin/bash"
    return subprocess.run([shell, "-i"], check=False, env=env).returncode


def active_shell_config_dir(context_name: str) -> Optional[Path]:
    docker_config = os.getenv("DOCKER_CONFIG", "").strip()
    docker_context = os.getenv("DOCKER_CONTEXT", "").strip()
    if not docker_config or docker_context != context_name:
        return None
    return Path(docker_config)


GITHUB_WORKFLOW_RESTRICTED_MESSAGE = "github workflow tokens are restricted to stack APIs and service image updates"


def _format_command(command) -> str:
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command)


def _error_output(exc: subprocess.CalledProcessError) -> str:
    values = []
    stdout = getattr(exc, "stdout", None) or getattr(exc, "output", None)
    stderr = getattr(exc, "stderr", None)
    if stdout:
        values.append(str(stdout).strip())
    if stderr:
        values.append(str(stderr).strip())
    return "\n".join(value for value in values if value)


def _manager_deploy_suggestion(message: str) -> Optional[str]:
    if GITHUB_WORKFLOW_RESTRICTED_MESSAGE in message:
        return (
            "Suggestion: this workflow is using a Docker-Manager GitHub token, but docker-stack fell back to a raw Docker CLI API. "
            "Run the mesudip/docker-stack@v2 setup action before deploy, keep DOCKER_MANAGER_URL/DOCKER_CONTEXT from that step, "
            "and make sure the manager /version endpoint advertises docker-stack deploy support."
        )
    lowered = message.lower()
    if "github workflow" in lowered and ("trusted" in lowered or "not allowed" in lowered or "forbidden" in lowered):
        return (
            "Suggestion: configure a trusted workflow/deployment rule on Docker-Manager for this repository, workflow file, "
            "stack, namespace, and service scope, then rerun the deploy."
        )
    if "external network" in lowered and "outside the stack scope" in lowered:
        return (
            "Suggestion: the compose file references an external Docker network that Docker-Manager does not consider owned "
            "by this stack. Either allow that external network in the Docker-Manager deployment rule, attach the service to "
            "a stack-owned network, or relabel/recreate the existing network with the expected stack ownership before deploying."
        )
    if "manager request failed" in lowered and "/version" in lowered:
        return (
            "Suggestion: check that the manager URL is reachable from the runner and that the setup action exported "
            "DOCKER_MANAGER_URL and the matching Docker auth headers."
        )
    if "docker-manager is configured" in lowered and "did not advertise required feature" in lowered:
        return (
            "Suggestion: deploy the current Docker-Manager server code and verify GET /version returns MesudipFeatures "
            "including mesudip-docker-enterprise-v1 or docker_stack_deploy_v1 for this manager endpoint."
        )
    return None


def _format_called_process_error(exc: subprocess.CalledProcessError) -> str:
    output = _error_output(exc)
    lines = [f"docker-stack: command failed with exit code {exc.returncode}: {_format_command(exc.cmd)}"]
    if output:
        lines.append(output)
    suggestion = _manager_deploy_suggestion(output)
    if suggestion:
        lines.extend(["", suggestion])
    return "\n".join(lines)


def _format_runtime_error(exc: RuntimeError) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    lines = [f"docker-stack: {message}"]
    suggestion = _manager_deploy_suggestion(message)
    if suggestion:
        lines.extend(["", suggestion])
    return "\n".join(lines)


def main(args: List[str] = None):
    parser = argparse.ArgumentParser(description="Deploy and manage Docker stacks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser("login", help="Authenticate Docker CLI against Docker-Manager")
    login_parser.add_argument("manager", nargs="?", help="Docker-Manager host or URL, for example 172.31.0.6:2378")
    login_parser.add_argument("--manager-url", help="Docker-Manager base URL")
    login_parser.add_argument("--context", "--context-name", dest="context_name", help="Docker context name to create or update")
    login_parser.add_argument("--timeout-secs", type=int, help="Login timeout in seconds")

    shell_parser = subparsers.add_parser("shell", help="Open an authenticated Bash/Zsh shell for a Docker-Manager context")
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

    shell_auth_parser = subparsers.add_parser("shell-auth", help=argparse.SUPPRESS)
    shell_auth_parser.add_argument("operation", choices=("ensure", "status"), help=argparse.SUPPRESS)

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
    deploy_parser.add_argument("--prune", action="store_true", help="Remove services no longer referenced by the compose file")
    deploy_parser.add_argument(
        "--resolve-image",
        choices=("always", "changed", "never"),
        help="Control image digest resolution during deploy",
    )
    deploy_parser.add_argument(
        "--force-update",
        action="store_true",
        help="Force service task updates (Docker-Manager deploys only)",
    )
    _add_namespace_argument(deploy_parser, "Deployment namespace")
    deploy_parser.add_argument("-t", "--tag", help="Tag the current deployment for later checkout", required=False)
    deploy_parser.add_argument("--show-generated", action="store_true", default=True, help="Show newly generated secrets after deployment")

    # Remove subcommand
    rm_parser = subparsers.add_parser("rm", help="Remove a deployed stack")
    rm_parser.add_argument("stack_name", help="Name of the stack")
    _add_namespace_argument(rm_parser)

    # Prune command
    subparsers.add_parser("prune", help="Remove old versions of configs and secrets")

    # Ls command
    ls_parser = subparsers.add_parser("ls", help="List docker-stacks")
    _add_listing_namespace_arguments(ls_parser)

    node_parser = subparsers.add_parser("node", help="Inspect Docker Swarm nodes")
    node_subparsers = node_parser.add_subparsers(dest="node_command", required=True)
    node_subparsers.add_parser("ls", help="List Docker Swarm nodes and their labels")

    cat_parser = subparsers.add_parser(
        "cat", help="Print the docker compose of specific version. Defaults to latest version if not specified."
    )
    cat_parser.add_argument("stack_name", help="Name of the stack")
    cat_parser.add_argument("version", nargs="?", help="Stack version to cat. Defaults to latest if omitted.")
    _add_namespace_argument(cat_parser)

    checkout_parser = subparsers.add_parser("checkout", help="Deploy specific version of the stack")
    checkout_parser.add_argument("stack_name", help="Name of the stack")
    checkout_parser.add_argument("version", help="Stack version to cat")
    _add_namespace_argument(checkout_parser)

    # version_parser = subparsers.add_parser("version",help="Deploy specific version of the stack")
    # version_parser.add_argument("stack_name", help="Name of the stack")
    # version_parser.add_argument("version","versions", help="Stack version to cat")

    version_parser = subparsers.add_parser("version", aliases=["versions"], help="Deploy specific version of the stack")
    version_parser.add_argument("stack_name", help="Name of the stack")
    _add_namespace_argument(version_parser)

    parser.add_argument(
        "-u", "--user", help="Registry credentials in format hostname:username:password", action="append", required=False, default=[]
    )
    parser.add_argument("-t", "--tag", help="Tag the current deployment for later checkout", required=False)
    parser.add_argument(
        "-ro", "-r", "--ro", "--r", "--dry-run", action="store_true", help="Print commands, don't execute them", required=False
    )
    parser.add_argument("--show-generated", action="store_true", default=True, help="Show newly generated secrets after deployment")

    args = parser.parse_args(args if args else sys.argv[1:])

    if args.command == "shell-auth":
        try:
            if args.operation == "ensure":
                ensure_shell_access(required=True)
            else:
                from docker_stack.shell_auth import shell_auth_request

                payload = shell_auth_request("status", required=True)
                print(json.dumps(payload))
        except ShellAuthError as exc:
            print(f"docker-stack shell-auth: {exc}", file=sys.stderr)
            if exc.requires_login:
                print("Run: docker-stack login", file=sys.stderr)
            return 2
        return 0

    if args.command == "login":
        try:
            config = resolve_login_config(
                manager_url=args.manager_url,
                manager_target=args.manager,
                context_name=args.context_name,
                timeout_secs=args.timeout_secs,
            )
            current_shell_config = active_shell_config_dir(config.context_name)
            if shell_session_active():
                result = login_for_active_shell(config)
                config_dir = current_shell_config
            elif current_shell_config:
                config_dir, result = ensure_isolated_login(config, docker_config_dir=current_shell_config)
            else:
                config_dir = None
                result = docker_manager_login(config)
        except RuntimeError as exc:
            print(f"docker-stack login: {exc}", file=sys.stderr)
            sys.exit(2)
        if result is None:
            print("Docker-Manager login already active.")
        else:
            print("Docker-Manager browser login successful.")
            print(f"Callback: {result.redirect_uri}")
        print(f"DOCKER_CONTEXT={config.context_name}")
        if config_dir is not None:
            print(f"DOCKER_CONFIG={config_dir}")
        print(f"Context host={config.docker_context_host}")
        if config.manager_url.startswith("https://"):
            print(f"TLS detected for manager endpoint ({'verification skipped' if config.skip_tls_verify else 'verified'})")
        if result is not None:
            expiry = format_expiry(result.expires_at)
            if expiry:
                print(f"Access token expires in {expiry}")
        print("Try: docker ps")
        return

    if args.command == "shell":
        shell_name = args.target if args.context_name is None else None
        manager_target = args.target if args.context_name is not None else None
        try:
            try:
                config = resolve_shell_login_config(
                    shell_name=shell_name,
                    manager_target=manager_target,
                    context_name=args.context_name,
                    timeout_secs=args.timeout_secs,
                )
            except UnknownShellContextError as exc:
                manager_target = _prompt_for_manager_target(exc.context_name)
                print(f"Creating Docker-Manager shell context '{exc.context_name}'...")
                config = resolve_shell_login_config(
                    shell_name=None,
                    manager_target=manager_target,
                    context_name=exc.context_name,
                    timeout_secs=args.timeout_secs,
                )
                persist_shell_context(config)
            if shell_session_active() and os.getenv(SHELL_CONTEXT_ENV) == config.context_name:
                print(f"docker-stack shell context={config.context_name} is already active")
                return
            result = browser_login(config)
        except RuntimeError as exc:
            print(f"docker-stack shell: {exc}", file=sys.stderr)
            sys.exit(2)
        print(f"Opening shell context={config.context_name}")
        print(f"Shell manager={config.manager_url}")
        if result is not None:
            expiry = format_expiry(result.expires_at)
            if expiry:
                print(f"Access token expires in {expiry}")
        else:
            print("Access token already active.")
        try:
            sys.exit(run_managed_shell(config, result))
        except ShellAuthError as exc:
            print(f"docker-stack shell: {exc}", file=sys.stderr)
            sys.exit(2)

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

    try:
        docker = Docker(registries=args.user)
        docker.load_env()

        if hasattr(args, "namespace") and args.command != "ls":
            print(f"Namespace: {args.namespace}", file=sys.stderr)

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
                prune=args.prune,
                resolve_image=args.resolve_image,
                force_update=args.force_update,
            )
        elif args.command == "ls":
            docker.stack.ls(namespace=None if args.all_namespaces else (args.namespace or DEFAULT_NAMESPACE))
        elif args.command == "node":
            if args.node_command == "ls":
                docker.node.ls()

        elif args.command == "rm":
            docker.stack.rm(args.stack_name, namespace=args.namespace)
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
    except subprocess.CalledProcessError as exc:
        print(_format_called_process_error(exc), file=sys.stderr)
        sys.exit(exc.returncode or 2)
    except RuntimeError as exc:
        print(_format_runtime_error(exc), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
