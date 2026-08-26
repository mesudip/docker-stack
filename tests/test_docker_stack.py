import base64
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from docker_stack.cli import (
    DOCKER_SHELL_ENDPOINT_ENV_VARS,
    Docker,
    _format_runtime_error,
    _exec_process,
    _managed_docker,
    _read_selected_node,
    _select_node,
    active_shell_config_dir,
    open_context_shell,
)
from docker_stack.helpers import Command
from docker_stack.login import UnknownShellContextError
from docker_stack import main


def decode_x_files(compose_content):
    data = yaml.safe_load(compose_content)
    return {item["path"]: base64.b64decode(item["content"]).decode("utf-8") for item in data.get("x-files", [])}


def test_node_use_updates_only_managed_shell_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"HttpHeaders": {"Authorization": "Bearer token"}}), encoding="utf-8")
    node_state = tmp_path / "node-selection"
    monkeypatch.setenv("DOCKER_STACK_SHELL_SOCKET", str(tmp_path / "auth.sock"))
    monkeypatch.setenv("DOCKER_STACK_SHELL_SECRET", "secret")
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    monkeypatch.setenv("DOCKER_STACK_SHELL_NODE_STATE", str(node_state))
    client = SimpleNamespace(
        list_nodes=lambda: {
            "nodes": [
                {"id": "node-worker-123", "hostname": "worker-02", "state": "Ready"},
            ]
        }
    )
    monkeypatch.setattr("docker_stack.cli._manager_with_cluster_cli", lambda: client)

    assert _select_node("worker-02") == "worker-02"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["HttpHeaders"]["Authorization"] == "Bearer token"
    assert payload["HttpHeaders"]["X-Docker-Manager-Node"] == "node-worker-123"
    assert _read_selected_node() == "node-worker-123"
    assert node_state.read_text(encoding="utf-8").strip() == "worker-02"

    assert _select_node("cluster") == "cluster"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert "X-Docker-Manager-Node" not in payload["HttpHeaders"]


def test_managed_docker_ps_renders_node_column(monkeypatch, capsys):
    client = SimpleNamespace(
        supports=lambda feature: feature == "cluster_container_cli_v1",
        list_containers=lambda **_kwargs: {
            "containers": [
                {
                    "node_id": "node-1",
                    "node_name": "worker-02",
                    "docker": {
                        "Id": "abcdef1234567890",
                        "Image": "nginx:1.27",
                        "Command": "nginx",
                        "Created": 1,
                        "Status": "Up 1 hour",
                        "Ports": [{"PrivatePort": 80, "Type": "tcp"}],
                        "Names": ["/web.1.test"],
                    },
                }
            ],
            "node_errors": [],
        }
    )
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda: client)

    assert _managed_docker(["ps"]) == 0
    output = capsys.readouterr().out
    assert "CONTAINER ID" in output
    assert "NODE" in output
    assert "worker-02" in output
    assert "abcdef123456" in output


def test_managed_docker_ps_matches_docker_columns(monkeypatch, capsys):
    client = SimpleNamespace(
        supports=lambda feature: feature == "cluster_container_cli_v1",
        list_containers=lambda **_kwargs: {
            "containers": [
                {
                    "node_id": "node-1",
                    "node_name": "worker-02",
                    "docker": {
                        "Id": "467fce208a05dddd",
                        "Image": "registry.example.com/app/backend:v1",
                        "Command": "sh -c 'java $JAVA_OPTS -jar /app/app.jar'",
                        "Created": int(time.time()) - 17 * 60,
                        "Status": "Up 17 minutes",
                        "Ports": [
                            {"PrivatePort": 8080, "Type": "tcp"},
                            {"PrivatePort": 8081, "Type": "tcp"},
                            {"IP": "0.0.0.0", "PrivatePort": 443, "PublicPort": 8443, "Type": "tcp"},
                        ],
                        "Names": ["/app_backend.1.qtfngt97xvabnhj0ehk4us6kk"],
                    },
                }
            ],
            "node_errors": [],
        },
    )
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda: client)

    assert _managed_docker(["ps"]) == 0
    header, row = capsys.readouterr().out.splitlines()

    assert header.split() == [
        "CONTAINER", "ID", "IMAGE", "COMMAND", "CREATED", "STATUS", "PORTS", "NAMES", "NODE",
    ]
    assert row.startswith("467fce208a05   ")
    assert '"sh -c \'java $JAVA_O\u2026"' in row
    assert "17 minutes ago" in row
    assert "8080-8081/tcp, 0.0.0.0:8443->443/tcp" in row
    assert row.endswith("worker-02")
    assert row == row.rstrip()


@pytest.mark.parametrize(
    "image,expected,expected_no_trunc",
    [
        (
            "registry.example.com/app/design-system:af037b33@sha256:15d623cfa2f856c8101f9f3c47003d412f9b2bf72c69c95d5dff07a74e5f7b60",
            "registry.example.com/app/design-system:af037b33",
            "registry.example.com/app/design-system:af037b33@sha256:15d623cfa2f856c8101f9f3c47003d412f9b2bf72c69c95d5dff07a74e5f7b60",
        ),
        (
            "docker.io/library/alpine@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b",
            "alpine",
            "docker.io/library/alpine@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b",
        ),
        ("nginx:1.27", "nginx:1.27", "nginx:1.27"),
        (
            "sha256:0e901e68141fd02f237cf63eb842529f8a9500636a9419e3cf4fb986b8fe3d5d",
            "0e901e68141f",
            "sha256:0e901e68141fd02f237cf63eb842529f8a9500636a9419e3cf4fb986b8fe3d5d",
        ),
    ],
)
def test_managed_docker_ps_drops_pinned_digest_like_docker(monkeypatch, capsys, image, expected, expected_no_trunc):
    client = SimpleNamespace(
        supports=lambda feature: feature == "cluster_container_cli_v1",
        list_containers=lambda **_kwargs: {
            "containers": [
                {
                    "node_name": "worker-02",
                    "docker": {
                        "Id": "abcdef1234567890",
                        "Image": image,
                        "Command": "sleep",
                        "Created": int(time.time()) - 60,
                        "Status": "Up 1 minute",
                        "Ports": [],
                        "Names": ["/svc.1.abc"],
                    },
                }
            ],
            "node_errors": [],
        },
    )
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda: client)

    assert _managed_docker(["ps"]) == 0
    assert capsys.readouterr().out.splitlines()[1].split()[1] == expected

    assert _managed_docker(["ps", "--no-trunc"]) == 0
    assert capsys.readouterr().out.splitlines()[1].split()[1] == expected_no_trunc


def test_managed_docker_ps_forwards_global_latest_and_limit(monkeypatch):
    calls = []
    client = SimpleNamespace(
        supports=lambda feature: feature == "cluster_container_cli_v1",
        list_containers=lambda **kwargs: calls.append(kwargs)
        or {"containers": [], "node_errors": []},
    )
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda: client)

    assert _managed_docker(["ps", "--latest"]) == 0
    assert _managed_docker(["ps", "--last", "3"]) == 0

    assert calls[0]["latest"] is True
    assert calls[0]["limit"] is None
    assert calls[1]["latest"] is False
    assert calls[1]["limit"] == 3


def test_exec_process_replaces_the_python_process(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr("docker_stack.cli.os.execvp", lambda file, args: calls.append((file, args)))

    _exec_process(["docker", "service", "logs", "-f", "svc"])

    assert calls == [("docker", ["docker", "service", "logs", "-f", "svc"])]


def test_exec_process_passes_env_when_given(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr("docker_stack.cli.os.execvpe", lambda file, args, env: calls.append((file, args, env)))

    _exec_process(["/bin/test-shell", "-i"], env={"DOCKER_CONTEXT": "office"})

    assert calls == [("/bin/test-shell", ["/bin/test-shell", "-i"], {"DOCKER_CONTEXT": "office"})]


def test_exec_process_reports_missing_binary(monkeypatch, capsys):
    monkeypatch.setattr(os, "name", "posix")

    def missing(_file, _args):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr("docker_stack.cli.os.execvp", missing)

    assert _exec_process(["docker", "ps"]) == 127
    assert "cannot execute docker" in capsys.readouterr().err


def test_exec_process_falls_back_to_subprocess_off_posix(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr("docker_stack.cli.os.execvp", lambda *_args: pytest.fail("exec must not run off POSIX"))
    monkeypatch.setattr(
        "docker_stack.cli.subprocess.run",
        lambda command, **_kwargs: calls.append(command) or SimpleNamespace(returncode=3),
    )

    assert _exec_process(["docker", "ps"]) == 3
    assert calls == [["docker", "ps"]]


def test_main_reports_interrupt_as_shell_exit_code(monkeypatch):
    def interrupted(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr("docker_stack.cli._run", interrupted)

    assert main(["ps"]) == 130


def test_managed_docker_delegates_format_to_real_docker(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "docker_stack.cli._exec_process",
        lambda command, **_kwargs: calls.append(command) or 7,
    )

    assert _managed_docker(["ps", "--format", "{{.ID}}"]) == 7
    assert calls == [["docker", "ps", "--format", "{{.ID}}"]]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--context", "other", "ps"],
        ["--context=other", "ps"],
        ["-H", "tcp://other:2375", "ps"],
        ["-H=tcp://other:2375", "ps"],
        ["--config", "/tmp/other-docker-config", "ps"],
    ],
)
def test_managed_docker_delegates_explicit_target_overrides(monkeypatch, arguments):
    calls = []
    monkeypatch.setattr(
        "docker_stack.cli._exec_process",
        lambda command, **_kwargs: calls.append(command) or 6,
    )
    monkeypatch.setattr(
        "docker_stack.cli.discover_manager_client",
        lambda: pytest.fail("explicit Docker targets must bypass managed cluster discovery"),
    )

    assert _managed_docker(arguments) == 6
    assert calls == [["docker", *arguments]]


def test_managed_docker_ps_delegates_when_manager_lacks_cluster_feature(monkeypatch):
    calls = []
    client = SimpleNamespace(supports=lambda _feature: False)
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda: client)
    monkeypatch.setattr(
        "docker_stack.cli._exec_process",
        lambda command, **_kwargs: calls.append(command) or 9,
    )

    assert _managed_docker(["ps"]) == 9
    assert calls == [["docker", "ps"]]


class RecordingManager:
    def __init__(self):
        self.calls = []

    def create(self, object_name, object_content, labels=None, stack=None):
        self.calls.append(
            {
                "object_name": object_name,
                "object_content": object_content,
                "labels": labels or [],
                "stack": stack,
            }
        )
        return object_name, Command.nop


class FakeManagerClient:
    def __init__(self):
        self.features = {"docker_stack_query_v1", "docker_stack_deploy_v1"}
        self.deploy_payloads = []
        self.rollback_payloads = []
        self.resolve_config_payloads = []
        self.resolve_secret_payloads = []

    def supports(self, feature_name):
        return feature_name in self.features

    def list_stacks(self, *, namespace="default"):
        return {"stacks": [{"namespace": namespace or "default", "stack": "team-a", "versions": ["1", "2"]}]}

    def list_stack_versions(self, stack_name, *, namespace="default"):
        assert stack_name == "team-a"
        assert namespace == "default"
        return {
            "stack": "team-a",
            "versions": [
                {"version": "1", "tag": "stable"},
                {"version": "2", "tag": "latest"},
            ],
        }

    def get_stack_compose(self, stack_name, *, namespace="default", version=None, tag=None):
        assert stack_name == "team-a"
        assert namespace == "default"
        if tag == "latest":
            return {
                "stack": stack_name,
                "version": "2",
                "tag": "latest",
                "compose": "services:\n  api:\n    image: busybox\n",
            }
        assert version in {"2", "v2"}
        return {
            "stack": stack_name,
            "version": "2",
            "tag": "latest",
            "compose": "services:\n  api:\n    image: busybox\n",
        }

    def list_nodes(self):
        return {
            "nodes": [
                {
                    "hostname": "swarm-a",
                    "role": "manager",
                    "manager_status": "Leader",
                    "state": "Ready",
                    "availability": "Active",
                    "address": "10.0.0.1",
                    "labels": {"team-a": "true"},
                }
            ]
        }

    def resolve_config(self, *, stack, namespace, name, content, labels=None):
        self.resolve_config_payloads.append(
            {
                "stack": stack,
                "namespace": namespace,
                "name": name,
                "content": content,
                "labels": labels or {},
            }
        )
        return {
            "logical_name": name,
            "actual_name": f"{name}_v2",
            "version": 2,
            "created": True,
            "changed": True,
        }

    def resolve_secret(self, *, stack, namespace, name, content=None, generate=None, labels=None, return_generated_value=False):
        self.resolve_secret_payloads.append(
            {
                "stack": stack,
                "namespace": namespace,
                "name": name,
                "content": content,
                "generate": generate,
                "labels": labels or {},
                "return_generated_value": return_generated_value,
            }
        )
        payload = {
            "logical_name": name,
            "actual_name": name,
            "version": 1,
            "created": True,
            "changed": True,
        }
        if return_generated_value:
            payload["generated_value"] = "generated-from-manager"
        return payload

    def deploy_stack(self, *, stack, namespace, compose, options=None):
        self.deploy_payloads.append({"stack": stack, "namespace": namespace, "compose": compose, "options": options or {}})
        return {"warnings": [], "stdout": "", "stderr": ""}

    def rollback_stack(self, *, stack, namespace, version):
        self.rollback_payloads.append({"stack": stack, "namespace": namespace, "version": version})
        return {"warnings": [], "stdout": "", "stderr": ""}

    def remove_stack(self, *, stack, namespace):
        self.remove_payload = {"stack": stack, "namespace": namespace}
        return self.remove_payload


def test_when_create_stack_support_x_content():
    main(["deploy", "pytest_test_x_content", "./tests/docker-compose-example.yml"])


def test_process_x_content_prefixes_stack_name_for_unnamed_objects():
    docker = Docker()
    manager = RecordingManager()

    processed = docker.stack._process_x_content(
        {
            "config.json": {
                "x-content": "hello",
            }
        },
        manager,
        stack="govtool",
    )

    assert manager.calls == [
        {
            "object_name": "govtool_config.json",
            "object_content": "hello",
            "labels": [],
            "stack": "govtool",
        }
    ]
    assert processed == {"config.json": {"name": "govtool_config.json", "external": True}}


def test_process_x_content_preserves_explicit_name_override():
    docker = Docker()
    manager = RecordingManager()

    processed = docker.stack._process_x_content(
        {
            "config.json": {
                "name": "shared-config",
                "x-content": "hello",
            }
        },
        manager,
        stack="govtool",
    )

    assert manager.calls == [
        {
            "object_name": "shared-config",
            "object_content": "hello",
            "labels": [],
            "stack": "govtool",
        }
    ]
    assert processed == {"config.json": {"name": "shared-config", "external": True}}


def test_process_x_content_supports_x_generated_alias_for_secrets(monkeypatch):
    docker = Docker()
    manager = RecordingManager()
    manager.object_type = "secret"

    monkeypatch.setattr("docker_stack.cli.generate_secret", lambda **_kwargs: "alias-generated-secret")

    processed = docker.stack._process_x_content(
        {
            "db_password": {
                "x-generated": {
                    "length": 20,
                    "numbers": True,
                    "special": False,
                    "uppercase": True,
                }
            }
        },
        manager,
        stack="govtool",
    )

    assert manager.calls == [
        {
            "object_name": "govtool_db_password",
            "object_content": "alias-generated-secret",
            "labels": ["mesudip.secret.generated=true"],
            "stack": "govtool",
        }
    ]
    assert processed == {"db_password": {"name": "govtool_db_password", "external": True}}


def test_process_x_content_supports_secret_environment(monkeypatch):
    docker = Docker()
    manager = RecordingManager()
    manager.object_type = "secret"
    monkeypatch.setenv("API_TOKEN", "secret-from-env")

    processed = docker.stack._process_x_content(
        {"api_token": {"environment": "API_TOKEN"}},
        manager,
        stack="govtool",
    )

    assert manager.calls == [
        {
            "object_name": "govtool_api_token",
            "object_content": "secret-from-env",
            "labels": [],
            "stack": "govtool",
        }
    ]
    assert processed == {"api_token": {"name": "govtool_api_token", "external": True}}


def test_process_x_content_secret_environment_requires_value(monkeypatch):
    docker = Docker()
    manager = RecordingManager()
    manager.object_type = "secret"
    monkeypatch.delenv("API_TOKEN", raising=False)

    with pytest.raises(ValueError, match="Secret api_token references environment variable API_TOKEN, but it is not set"):
        docker.stack._process_x_content(
            {"api_token": {"environment": "API_TOKEN"}},
            manager,
            stack="govtool",
        )


def test_process_x_content_uses_manager_resolve_apis(monkeypatch):
    fake_manager = FakeManagerClient()
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: fake_manager)

    docker = Docker()
    docker.stack._manager_client = fake_manager

    processed_configs = docker.stack._process_x_content(
        {
            "app.conf": {
                "x-content": "hello",
            }
        },
        docker.config,
        stack="govtool",
    )
    processed_secrets = docker.stack._process_x_content(
        {
            "app-secret": {
                "x-generate": {
                    "length": 24,
                    "numbers": True,
                    "special": False,
                    "uppercase": True,
                }
            }
        },
        docker.secret,
        stack="govtool",
    )

    assert fake_manager.resolve_config_payloads == [
        {
            "stack": "govtool",
            "namespace": "default",
            "name": "app.conf",
            "content": "hello",
            "labels": {},
        }
    ]
    assert fake_manager.resolve_secret_payloads == [
        {
            "stack": "govtool",
            "namespace": "default",
            "name": "app-secret",
            "content": None,
            "generate": {
                "length": 24,
                "numbers": True,
                "special": False,
                "uppercase": True,
            },
            "labels": {"mesudip.secret.generated": "true"},
            "return_generated_value": True,
        }
    ]
    assert processed_configs == {"app.conf": {"name": "app.conf_v2", "external": True}}
    assert processed_secrets == {"app-secret": {"name": "app-secret", "external": True}}
    assert docker.stack.generated_secrets == {"app-secret": "generated-from-manager"}


def test_rendered_compose_stores_source_metadata_without_secret_files(monkeypatch, tmp_path):
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("docker_stack.docker_objects.run_cli_command", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("docker_stack.docker_objects.DockerObjectManager.check", lambda *_args, **_kwargs: False)
    monkeypatch.setenv("APP_HOST", "app.internal")
    monkeypatch.setenv("SECRET_ENV", "do-not-store")
    (tmp_path / "configs").mkdir()
    (tmp_path / "secrets").mkdir()
    (tmp_path / "configs" / "app.conf").write_text("host=${APP_HOST}\n")
    (tmp_path / "configs" / "raw.conf").write_text("raw=$RAW_VALUE\n")
    (tmp_path / "secrets" / "token.txt").write_text("secret file content\n")
    compose_text = """services:
  api:
    image: busybox
    environment:
      - APP_HOST=${APP_HOST}
configs:
  templated:
    x-template-file: ./configs/app.conf
  raw:
    file: ./configs/raw.conf
secrets:
  file_secret:
    file: ./secrets/token.txt
  env_secret:
    environment: SECRET_ENV
"""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(compose_text)

    rendered = Docker().stack.render_compose_file(str(compose_file), stack="govtool", include_build=False)

    x_files = decode_x_files(rendered.enriched)
    assert x_files["compose.yml"] == compose_text
    assert x_files["configs/app.conf"] == "host=${APP_HOST}\n"
    assert x_files["configs/raw.conf"] == "raw=$RAW_VALUE\n"
    assert "secrets/token.txt" not in x_files
    assert x_files[".env"] == "APP_HOST=app.internal\n"
    enriched_data = yaml.safe_load(rendered.enriched)
    clean_data = yaml.safe_load(rendered.clean)
    assert "x-files" in enriched_data
    assert "x-files" not in clean_data
    assert clean_data["configs"]["templated"] == {"name": "govtool_templated", "external": True}
    assert clean_data["secrets"]["env_secret"] == {"name": "govtool_env_secret", "external": True}


def test_rendered_compose_normalizes_main_source_name(monkeypatch, tmp_path):
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("docker_stack.docker_objects.run_cli_command", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("docker_stack.docker_objects.DockerObjectManager.check", lambda *_args, **_kwargs: False)
    compose_text = "services:\n  api:\n    image: busybox\n"
    compose_file = tmp_path / "custom-stack-name.yaml"
    compose_file.write_text(compose_text)

    rendered = Docker().stack.render_compose_file(str(compose_file), stack="govtool", include_build=False)

    assert decode_x_files(rendered.enriched)["compose.yml"] == compose_text
    assert "custom-stack-name.yaml" not in decode_x_files(rendered.enriched)


def test_build_uses_service_dockerfile(tmp_path):
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("""
services:
  storybook:
    image: example/storybook:test
    build:
      context: ./frontend
      dockerfile: storybook.Dockerfile
      args:
        NPMRC_TOKEN: dummy
""".strip())

    docker = Docker()
    docker.stack.build_and_push(str(compose_file))

    assert len(docker.stack.commands) == 1
    assert docker.stack.commands[0].command == [
        "docker",
        "build",
        "-t",
        "example/storybook:test",
        "-f",
        str(tmp_path / "frontend" / "storybook.Dockerfile"),
        "--build-arg",
        "NPMRC_TOKEN=dummy",
        str(tmp_path / "frontend"),
    ]


def test_rm_enqueues_stack_remove_command():
    docker = Docker()

    docker.stack.rm("govtool")

    assert docker.stack.commands[-1].command == ["docker", "stack", "rm", "govtool"]


def test_filtered_agent_outputs_still_render(monkeypatch, capsys):
    inspect_payloads = {
        ("docker", "config", "ls", "--format", "{{.ID}}\t{{.Name}}\t{{.Labels}}"): "\n".join(
            [
                "cfg1\tteam-a\tmesudip.stack.name=team-a,mesudip.object.version=1",
                "cfg2\tteam-a_v2\tmesudip.stack.name=team-a,mesudip.object.version=2",
            ]
        ),
        ("docker", "config", "ls", "--format", "{{.Name}}\t{{.Labels}}"): "\n".join(
            [
                "team-a\tmesudip.stack.name=team-a,mesudip.object.version=1,mesudip.stack.tag=stable",
                "team-a_v2\tmesudip.stack.name=team-a,mesudip.object.version=2,mesudip.stack.tag=latest",
            ]
        ),
        (
            "docker",
            "config",
            "inspect",
            "team-a_v2",
        ): json.dumps([{"Spec": {"Data": base64.b64encode(b"services:\n  api:\n    image: busybox\n").decode("utf-8")}}]),
        ("docker", "node", "ls", "--format", "{{json .}}"): json.dumps(
            {
                "ID": "node-1",
                "Hostname": "swarm-a",
                "Status": "Ready",
                "Availability": "Active",
                "ManagerStatus": "Leader",
            }
        ),
        ("docker", "node", "inspect", "node-1", "--format", "{{json .}}"): json.dumps(
            {
                "Spec": {
                    "Role": "manager",
                    "Labels": {"team-a": "true"},
                },
                "Status": {"Addr": "10.0.0.1"},
            }
        ),
    }

    def fake_run_cli_command(command, **kwargs):
        key = tuple(command)
        if key not in inspect_payloads:
            raise AssertionError(f"Unexpected command: {command}")
        return inspect_payloads[key]

    monkeypatch.setattr("docker_stack.cli.run_cli_command", fake_run_cli_command)
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("docker_stack.cli.shutil.get_terminal_size", lambda *args, **kwargs: os.terminal_size((72, 20)))

    docker = Docker()
    assert docker.stack.ls() == {"team-a": ["1", "2"]}
    assert docker.stack.versions("team-a") == [("1", "stable"), ("2", "latest")]
    assert docker.stack.cat("team-a", "2") == "services:\n  api:\n    image: busybox\n"

    docker.node.ls()

    output = capsys.readouterr().out
    assert "Stack Name" in output
    assert "team-a" in output
    assert "Version" in output
    assert "stable" in output
    assert "swarm-a" in output
    assert "manager (Leader)" in output


def test_manager_fast_path_for_list_and_node(monkeypatch, capsys):
    fake_manager = FakeManagerClient()
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: fake_manager)
    monkeypatch.setattr("docker_stack.cli.shutil.get_terminal_size", lambda *args, **kwargs: os.terminal_size((72, 20)))

    docker = Docker()
    assert docker.stack.ls() == {"team-a": ["1", "2"]}
    assert docker.stack.versions("team-a") == [("1", "stable"), ("2", "latest")]
    assert docker.stack.cat("team-a", "2") == "services:\n  api:\n    image: busybox\n"
    assert docker.stack.cat("team-a", "v2") == "services:\n  api:\n    image: busybox\n"

    docker.node.ls()
    output = capsys.readouterr().out
    assert "team-a" in output
    assert "swarm-a" in output
    assert "manager (Leader)" in output


def test_stack_listing_compacts_consecutive_versions(capsys):
    Docker().stack._print_stack_listing(
        {("default", "api"): [str(version) for version in range(1, 61)]},
        "default",
    )

    output = capsys.readouterr().out
    assert "Namespace: default" in output
    assert "v1...v60" in output
    assert "v1, v2" not in output


def test_ls_supports_namespace_short_flag_and_all_namespaces(monkeypatch, capsys):
    class NamespaceManager(FakeManagerClient):
        def list_stacks(self, *, namespace="default"):
            if namespace is None:
                return {
                    "stacks": [
                        {"namespace": "default", "stack": "api", "versions": ["1", "2", "3"]},
                        {"namespace": "prod", "stack": "api", "versions": ["1"]},
                    ]
                }
            return {"stacks": [{"namespace": namespace, "stack": "api", "versions": ["1"]}]}

    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: NamespaceManager())

    main(["ls", "-n", "prod"])
    namespace_output = capsys.readouterr().out
    assert "Namespace: prod" in namespace_output

    main(["ls", "-A"])
    all_output = capsys.readouterr().out
    assert "Namespace: all" in all_output
    assert "default" in all_output
    assert "prod" in all_output
    assert "v1...v3" in all_output


def test_raw_daemon_namespace_uses_physical_stack_name(monkeypatch):
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: None)
    docker = Docker()
    captured = {}

    def fake_increment(object_name, content, labels=None, stack=None):
        captured.update(object_name=object_name, content=content, labels=labels, stack=stack)
        return object_name, Command.nop

    monkeypatch.setattr(docker.config, "increment", fake_increment)
    docker.stack._deploy("api", "services: {}", namespace="prod")

    assert captured["object_name"] == "prod--api"
    assert captured["stack"] == "prod--api"
    assert docker.stack.commands[-1].command[-1] == "prod--api"


def test_cat_without_explicit_version_does_not_print_versions_table(monkeypatch, capsys):
    fake_manager = FakeManagerClient()
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: fake_manager)

    main(["cat", "team-a"])

    output = capsys.readouterr().out
    assert "services:\n  api:\n    image: busybox\n" in output
    assert "Version | Tag" not in output


def test_checkout_uses_manager_fast_path_for_tag(monkeypatch):
    fake_manager = FakeManagerClient()
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: fake_manager)
    monkeypatch.setattr("docker_stack.cli.run_cli_command", lambda *args, **kwargs: "")
    docker = Docker()

    docker.stack.checkout("team-a", "latest")
    assert "docker-manager stack rollback team-a v2" in str(docker.stack.commands[-1])
    docker.stack.commands[-1].execute()
    assert fake_manager.rollback_payloads[-1]["stack"] == "team-a"
    assert fake_manager.rollback_payloads[-1]["namespace"] == "default"
    assert fake_manager.rollback_payloads[-1]["version"] == "2"


def test_deploy_enqueues_manager_callback_when_supported(monkeypatch, tmp_path):
    fake_manager = FakeManagerClient()
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: fake_manager)
    monkeypatch.setattr(
        "docker_stack.cli.run_cli_command",
        lambda *args, **kwargs: pytest.fail("manager-backed deploy should not hit direct daemon CLI"),
    )
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n  api:\n    image: busybox\n")

    docker = Docker()
    docker.stack.deploy("team-a", str(compose_file))

    assert "docker-manager stack deploy team-a" in str(docker.stack.commands[-1])
    docker.stack.commands[-1].execute()
    assert fake_manager.deploy_payloads[-1]["stack"] == "team-a"
    assert "x-files:" in fake_manager.deploy_payloads[-1]["compose"]
    assert decode_x_files(fake_manager.deploy_payloads[-1]["compose"])["compose.yml"] == "services:\n  api:\n    image: busybox\n"
    assert fake_manager.deploy_payloads[-1]["options"] == {}


def test_manager_deploy_keeps_resources_portable_for_manager_materialization(monkeypatch, tmp_path):
    fake_manager = FakeManagerClient()
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: fake_manager)
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        "configs:\n"
        "  app-config:\n"
        "    name: shared-config\n"
        "    x-content: new-content\n"
        "secrets:\n"
        "  app-token:\n"
        "    x-generate: true\n"
        "services:\n"
        "  api:\n"
        "    image: busybox\n"
        "    configs: [app-config]\n"
        "    secrets: [app-token]\n"
    )

    docker = Docker()
    docker.stack.deploy("team-a", str(compose_file))
    docker.stack.commands[-1].execute()

    deployed = yaml.safe_load(fake_manager.deploy_payloads[-1]["compose"])
    assert deployed["configs"]["app-config"] == {
        "name": "shared-config",
        "x-content": "new-content",
    }
    assert deployed["secrets"]["app-token"] == {"x-generate": True}
    assert fake_manager.resolve_config_payloads == []
    assert fake_manager.resolve_secret_payloads == []


def test_manager_deploy_embeds_config_file_and_inlines_secret_file(monkeypatch, tmp_path):
    fake_manager = FakeManagerClient()
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: fake_manager)
    (tmp_path / "app.conf").write_text("setting=true\n")
    (tmp_path / "token.txt").write_text("secret-value\n")
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        "configs:\n"
        "  app-config:\n"
        "    file: app.conf\n"
        "secrets:\n"
        "  app-token:\n"
        "    file: token.txt\n"
        "services:\n"
        "  api:\n"
        "    image: busybox\n"
        "    configs: [app-config]\n"
        "    secrets: [app-token]\n"
    )

    docker = Docker()
    docker.stack.deploy("team-a", str(compose_file))
    docker.stack.commands[-1].execute()

    embedded_files = decode_x_files(fake_manager.deploy_payloads[-1]["compose"])
    assert embedded_files["app.conf"] == "setting=true\n"
    assert "token.txt" not in embedded_files
    deployed = yaml.safe_load(fake_manager.deploy_payloads[-1]["compose"])
    assert deployed["secrets"]["app-token"] == {"x-content": "secret-value\n"}


def test_manager_deploy_forwards_all_supported_options(monkeypatch, tmp_path):
    fake_manager = FakeManagerClient()
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: fake_manager)
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n  api:\n    image: busybox\n")

    docker = Docker()
    docker.stack.deploy(
        "team-a",
        str(compose_file),
        with_registry_auth=True,
        prune=True,
        resolve_image="changed",
        force_update=True,
    )
    docker.stack.commands[-1].execute()

    assert fake_manager.deploy_payloads[-1]["options"] == {
        "with_registry_auth": True,
        "prune": True,
        "resolve_image": "changed",
        "force_update": True,
    }


def test_manager_deploy_rejects_unsupported_tag_instead_of_ignoring_it(monkeypatch, tmp_path):
    fake_manager = FakeManagerClient()
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: fake_manager)
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n  api:\n    image: busybox\n")

    with pytest.raises(RuntimeError, match="does not expose deployment tags"):
        Docker().stack.deploy("team-a", str(compose_file), tag="stable")

    assert fake_manager.deploy_payloads == []


def test_manager_deploy_prints_generated_secret_values(capsys):
    class GeneratedSecretManager(FakeManagerClient):
        def deploy_stack(self, *, stack, namespace, compose, options=None):
            return {
                "warnings": [],
                "stdout": "",
                "stderr": "",
                "generated_secrets": [
                    {"logical_name": "app-token", "actual_name": "team-a_app-token", "value": "generated-value"}
                ],
            }

    Docker().stack._deploy_via_manager(
        GeneratedSecretManager(),
        stack_name="team-a",
        namespace="default",
        rendered_content="services: {}",
        options={},
    )

    output = capsys.readouterr().out
    assert "app-token: generated-value" in output


def test_explicit_manager_query_error_does_not_fall_back_to_daemon(monkeypatch):
    class FailingManager(FakeManagerClient):
        def list_stacks(self, *, namespace="default"):
            raise RuntimeError("Manager request failed (GET /api/docker-stack/stacks): HTTP 503")

    monkeypatch.setenv("DOCKER_MANAGER_URL", "https://manager.example.test:2378")
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: FailingManager())
    monkeypatch.setattr(
        "docker_stack.cli.run_cli_command",
        lambda *_args, **_kwargs: pytest.fail("explicit manager errors must not fall back to daemon CLI"),
    )

    with pytest.raises(RuntimeError, match="HTTP 503"):
        Docker().stack.ls()


def test_manager_rm_uses_lifecycle_api(monkeypatch):
    fake_manager = FakeManagerClient()
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: fake_manager)
    docker = Docker()

    docker.stack.rm("team-a", namespace="prod")
    assert "docker-manager stack rm team-a" in str(docker.stack.commands[-1])
    docker.stack.commands[-1].execute()
    assert fake_manager.remove_payload == {"stack": "team-a", "namespace": "prod"}


def test_manager_deploy_preserves_with_registry_auth_option(monkeypatch, tmp_path):
    fake_manager = FakeManagerClient()
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: fake_manager)
    monkeypatch.setattr(
        "docker_stack.cli.run_cli_command",
        lambda *args, **kwargs: pytest.fail("manager-backed deploy should not hit direct daemon CLI"),
    )
    monkeypatch.setattr(
        "docker_stack.docker_objects.run_cli_command",
        lambda *args, **kwargs: pytest.fail("manager-backed deploy should not hit direct daemon CLI"),
    )
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n  api:\n    image: busybox\n")

    docker = Docker()
    docker.stack.deploy("team-a", str(compose_file), with_registry_auth=True)

    assert "docker-manager stack deploy team-a" in str(docker.stack.commands[-1])
    docker.stack.commands[-1].execute()
    assert fake_manager.deploy_payloads[-1]["stack"] == "team-a"
    assert fake_manager.deploy_payloads[-1]["options"] == {"with_registry_auth": True}


def test_deploy_restricted_raw_docker_error_is_actionable(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: None)
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n  api:\n    image: busybox\n")

    def restricted_command(command, **_kwargs):
        raise subprocess.CalledProcessError(
            1,
            command,
            output="",
            stderr="Error response from daemon: github workflow tokens are restricted to stack APIs and service image updates",
        )

    monkeypatch.setattr("docker_stack.docker_objects.run_cli_command", restricted_command)

    with pytest.raises(SystemExit) as excinfo:
        main(["deploy", "team-a", str(compose_file)])

    assert excinfo.value.code == 1
    stderr = capsys.readouterr().err
    assert "docker-stack: command failed with exit code 1: docker config ls" in stderr
    assert "Suggestion: this workflow is using a Docker-Manager GitHub token" in stderr
    assert "Traceback" not in stderr


def test_deploy_with_manager_url_fails_when_feature_not_advertised(monkeypatch, tmp_path, capsys):
    fake_manager = FakeManagerClient()
    fake_manager.features = set()
    monkeypatch.setenv("DOCKER_MANAGER_URL", "https://manager.example.test:2378")
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: fake_manager)
    monkeypatch.setattr(
        "docker_stack.docker_objects.run_cli_command",
        lambda *args, **kwargs: pytest.fail("explicit manager mode must not fall back to raw Docker CLI"),
    )
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n  api:\n    image: busybox\n")

    with pytest.raises(SystemExit) as excinfo:
        main(["deploy", "team-a", str(compose_file)])

    assert excinfo.value.code == 2
    stderr = capsys.readouterr().err
    assert (
        "Docker-Manager is configured via DOCKER_MANAGER_URL, but /version did not advertise required feature docker_stack_deploy_v1"
        in stderr
    )
    assert "Suggestion: deploy the current Docker-Manager server code" in stderr
    assert "Traceback" not in stderr


def test_manager_deploy_runtime_error_is_actionable(monkeypatch, tmp_path, capsys):
    class UntrustedWorkflowManager(FakeManagerClient):
        def deploy_stack(self, *, stack, namespace, compose, options=None):
            raise RuntimeError(
                "Manager request failed (POST /api/stacks/deploy): HTTP 403: "
                '{"message":"github workflow is not trusted for this stack scope"}'
            )

    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: UntrustedWorkflowManager())
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n  api:\n    image: busybox\n")

    with pytest.raises(SystemExit) as excinfo:
        main(["deploy", "team-a", str(compose_file)])

    assert excinfo.value.code == 2
    stderr = capsys.readouterr().err
    assert "docker-stack: Manager request failed (POST /api/stacks/deploy): HTTP 403" in stderr
    assert "Suggestion: configure a trusted workflow/deployment rule" in stderr
    assert "Traceback" not in stderr


def test_manager_deploy_external_network_scope_error_is_actionable(monkeypatch, tmp_path, capsys):
    class ExternalNetworkScopeManager(FakeManagerClient):
        def deploy_stack(self, *, stack, namespace, compose, options=None):
            raise RuntimeError(
                "Manager request failed (POST /api/stacks/deploy): HTTP 403: "
                '{"message":"external network dbsync is outside the stack scope"}'
            )

    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: ExternalNetworkScopeManager())
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        "networks:\n"
        "  dbsync:\n"
        "    external: true\n"
        "services:\n"
        "  api:\n"
        "    image: busybox\n"
        "    networks:\n"
        "      - dbsync\n"
    )

    with pytest.raises(SystemExit) as excinfo:
        main(["deploy", "team-a", str(compose_file)])

    assert excinfo.value.code == 2
    stderr = capsys.readouterr().err
    assert "external network dbsync is outside the stack scope" in stderr
    assert "Suggestion: the compose file references an external Docker network" in stderr
    assert "allow that external network in the Docker-Manager deployment rule" in stderr
    assert "Traceback" not in stderr


def test_manager_checkout_with_registry_auth_uses_deploy_api(monkeypatch):
    fake_manager = FakeManagerClient()
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: fake_manager)
    monkeypatch.setattr(
        "docker_stack.cli.run_cli_command",
        lambda *args, **kwargs: pytest.fail("manager-backed checkout should not hit direct daemon CLI"),
    )
    monkeypatch.setattr(
        "docker_stack.docker_objects.run_cli_command",
        lambda *args, **kwargs: pytest.fail("manager-backed checkout should not hit direct daemon CLI"),
    )

    docker = Docker()
    docker.stack.checkout("team-a", "2", with_registry_auth=True)

    assert "docker-manager stack deploy team-a" in str(docker.stack.commands[-1])
    docker.stack.commands[-1].execute()
    assert fake_manager.rollback_payloads == []
    assert fake_manager.deploy_payloads[-1]["stack"] == "team-a"
    assert fake_manager.deploy_payloads[-1]["options"] == {"with_registry_auth": True}


def test_deploy_passes_rendered_compose_over_stdin(monkeypatch, tmp_path):
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("docker_stack.docker_objects.run_cli_command", lambda *_args, **_kwargs: "")
    project_dir = tmp_path / "New project"
    project_dir.mkdir()
    compose_file = project_dir / "docker-stack.yml"
    compose_file.write_text("services:\n  api:\n    image: busybox\n")

    monkeypatch.chdir(project_dir)
    docker = Docker()
    docker.stack.deploy("wakapi", "./docker-stack.yml")

    deploy = docker.stack.commands[-1]
    assert deploy.command == ["docker", "stack", "deploy", "-c", "-", "wakapi"]
    assert deploy.stdin == "services:\n  api:\n    image: busybox\n"
    assert "x-files:" in docker.stack.commands[0].stdin
    assert "x-files:" not in deploy.stdin
    assert not (project_dir / "docker-stack-rendered.yml").exists()


def test_checkout_strips_x_files_from_local_deploy_but_preserves_stored_compose(monkeypatch):
    enriched = yaml.dump(
        {
            "services": {"api": {"image": "busybox"}},
            "x-files": [{"path": "compose.yml", "content": base64.b64encode(b"raw").decode("utf-8")}],
        },
        sort_keys=False,
    )

    def fake_cli_run(command, **_kwargs):
        if command[:3] == ["docker", "config", "inspect"]:
            return json.dumps([{"Spec": {"Data": base64.b64encode(enriched.encode("utf-8")).decode("utf-8")}}])
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("docker_stack.cli.run_cli_command", fake_cli_run)
    monkeypatch.setattr("docker_stack.docker_objects.run_cli_command", lambda *_args, **_kwargs: "")

    docker = Docker()
    docker.stack.checkout("team-a", "2")

    assert docker.stack.commands[0].stdin == enriched
    deploy = docker.stack.commands[-1]
    assert deploy.command == ["docker", "stack", "deploy", "-c", "-", "team-a"]
    assert yaml.safe_load(deploy.stdin) == {"services": {"api": {"image": "busybox"}}}


def test_login_accepts_context_alias_and_positional_manager(monkeypatch, capsys):
    captured = {}

    def fake_resolve_login_config(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            manager_url="https://172.31.0.6:2378",
            context_name="office",
            docker_context_host="tcp://172.31.0.6:2378",
            skip_tls_verify=True,
        )

    def fake_login(config):
        return SimpleNamespace(
            redirect_uri="http://localhost:8079/auth/callback",
            expires_at=None,
        )

    monkeypatch.setattr("docker_stack.cli.resolve_login_config", fake_resolve_login_config)
    monkeypatch.setattr("docker_stack.cli.docker_manager_login", fake_login)

    main(["login", "--context", "office", "172.31.0.6:2378"])

    assert captured["manager_target"] == "172.31.0.6:2378"
    assert captured["context_name"] == "office"
    output = capsys.readouterr().out
    assert "DOCKER_CONTEXT=office" in output
    assert "TLS detected for manager endpoint (verification skipped)" in output


def test_login_prefers_existing_context_for_portless_target(monkeypatch, capsys):
    captured = {}

    def fake_resolve_login_config(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            manager_url="https://172.31.0.6:2378",
            context_name="office",
            docker_context_host="tcp://172.31.0.6:2378",
            skip_tls_verify=True,
        )

    def fake_login(config):
        return SimpleNamespace(
            redirect_uri="http://localhost:8079/auth/callback",
            expires_at=None,
        )

    monkeypatch.setattr("docker_stack.cli.resolve_login_config", fake_resolve_login_config)
    monkeypatch.setattr("docker_stack.cli.docker_manager_login", fake_login)

    main(["login", "office"])

    assert captured["manager_target"] == "office"
    assert captured["context_name"] is None
    output = capsys.readouterr().out
    assert "DOCKER_CONTEXT=office" in output


def test_login_named_context_reuses_persisted_isolated_config(monkeypatch, tmp_path, capsys):
    (tmp_path / "config.json").write_text("{}\n", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(
        "docker_stack.cli.resolve_login_config",
        lambda **_kwargs: SimpleNamespace(
            manager_url="https://172.31.3.3:2376",
            context_name="saas-a",
            docker_context_host="tcp://172.31.3.3:2376",
            skip_tls_verify=False,
            docker_config_dir=tmp_path,
        ),
    )

    def fake_ensure(config, docker_config_dir=None):
        captured["context"] = config.context_name
        captured["config_dir"] = docker_config_dir
        return docker_config_dir, None

    monkeypatch.setattr("docker_stack.cli.ensure_isolated_login", fake_ensure)
    monkeypatch.setattr(
        "docker_stack.cli.docker_manager_login",
        lambda _config: pytest.fail("named persisted login must not use global Docker config"),
    )

    main(["login", "saas-a"])

    assert captured == {"context": "saas-a", "config_dir": tmp_path}
    assert f"DOCKER_CONFIG={tmp_path}" in capsys.readouterr().out


def test_login_inside_shell_updates_existing_isolated_config(monkeypatch, tmp_path, capsys):
    captured = {}

    def fake_resolve_login_config(**kwargs):
        captured["resolve"] = kwargs
        return SimpleNamespace(
            manager_url="https://172.31.0.6:2378",
            context_name="office",
            docker_context_host="tcp://172.31.0.6:2378",
            skip_tls_verify=True,
        )

    def fake_ensure_isolated_login(config, docker_config_dir=None):
        captured["ensure"] = {
            "context_name": config.context_name,
            "docker_config_dir": docker_config_dir,
        }
        return docker_config_dir, SimpleNamespace(
            redirect_uri="http://localhost:8079/auth/callback",
            expires_at=None,
        )

    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    monkeypatch.setenv("DOCKER_CONTEXT", "office")
    monkeypatch.setattr("docker_stack.cli.resolve_login_config", fake_resolve_login_config)
    monkeypatch.setattr("docker_stack.cli.ensure_isolated_login", fake_ensure_isolated_login)
    monkeypatch.setattr(
        "docker_stack.cli.docker_manager_login",
        lambda _config: pytest.fail("login inside shell should not write global Docker config"),
    )

    main(["login"])

    assert captured["ensure"] == {
        "context_name": "office",
        "docker_config_dir": tmp_path,
    }
    output = capsys.readouterr().out
    assert f"DOCKER_CONFIG={tmp_path}" in output
    assert "DOCKER_CONTEXT=office" in output


def test_login_inside_shell_reports_active_token(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    monkeypatch.setenv("DOCKER_CONTEXT", "office")
    monkeypatch.setattr(
        "docker_stack.cli.resolve_login_config",
        lambda **_kwargs: SimpleNamespace(
            manager_url="https://172.31.0.6:2378",
            context_name="office",
            docker_context_host="tcp://172.31.0.6:2378",
            skip_tls_verify=True,
        ),
    )
    monkeypatch.setattr(
        "docker_stack.cli.ensure_isolated_login",
        lambda _config, docker_config_dir=None: (docker_config_dir, None),
    )

    main(["login"])

    output = capsys.readouterr().out
    assert "Docker-Manager login already active." in output
    assert f"DOCKER_CONFIG={tmp_path}" in output


def test_shell_authenticates_and_opens_managed_shell(monkeypatch, capsys):
    captured = {}

    def fake_resolve_shell_login_config(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            manager_url="https://172.31.0.6:2378",
            context_name="office",
            docker_context_host="tcp://172.31.0.6:2378",
            skip_tls_verify=True,
            timeout_secs=30,
        )

    login_result = SimpleNamespace(
        access_token="access-token",
        refresh_token="refresh-token",
        redirect_uri="http://localhost:8079/auth/callback",
        callback_port=8079,
        expires_at=None,
        refresh_expires_at=None,
    )

    def fake_run_managed_shell(config, result):
        assert config.context_name == "office"
        assert result is login_result
        return 0

    monkeypatch.setattr("docker_stack.cli.resolve_shell_login_config", fake_resolve_shell_login_config)
    monkeypatch.setattr("docker_stack.cli.browser_login", lambda _config: login_result)
    monkeypatch.setattr("docker_stack.cli.run_managed_shell", fake_run_managed_shell)

    with pytest.raises(SystemExit) as excinfo:
        main(["shell", "office"])

    assert excinfo.value.code == 0
    assert captured["shell_name"] == "office"
    assert captured["manager_target"] is None
    assert captured["context_name"] is None
    output = capsys.readouterr().out
    assert "Opening shell context=office" in output
    assert "Shell manager=https://172.31.0.6:2378" in output


def test_shell_unknown_context_prompts_for_manager_url_and_creates_it(monkeypatch, capsys):
    calls = []

    def fake_resolve_shell_login_config(**kwargs):
        calls.append(kwargs)
        if kwargs["manager_target"] is None:
            raise UnknownShellContextError("prod1")
        return SimpleNamespace(
            manager_url="https://manager.example.com:2376",
            context_name="prod1",
            docker_context_host="tcp://manager.example.com:2376",
            skip_tls_verify=False,
            timeout_secs=30,
        )

    monkeypatch.setattr("docker_stack.cli.resolve_shell_login_config", fake_resolve_shell_login_config)
    monkeypatch.setattr("docker_stack.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "https://manager.example.com:2376")
    persisted = []
    monkeypatch.setattr("docker_stack.cli.persist_shell_context", lambda config: persisted.append(config.context_name))
    monkeypatch.setattr("docker_stack.cli.browser_login", lambda _config: None)
    monkeypatch.setattr("docker_stack.cli.run_managed_shell", lambda _config, _result: 0)

    with pytest.raises(SystemExit) as excinfo:
        main(["shell", "prod1"])

    assert excinfo.value.code == 0
    assert calls[1]["context_name"] == "prod1"
    assert calls[1]["manager_target"] == "https://manager.example.com:2376"
    assert persisted == ["prod1"]
    output = capsys.readouterr().out
    assert "Docker context 'prod1' does not exist yet" in output
    assert "Creating Docker-Manager shell context 'prod1'" in output


def test_shell_explicit_target_persists_context_before_opening(monkeypatch):
    config = SimpleNamespace(
        manager_url="https://172.31.3.3:2376",
        context_name="saas-a",
        docker_context_host="tcp://172.31.3.3:2376",
        skip_tls_verify=False,
        timeout_secs=30,
    )
    events = []
    monkeypatch.setattr("docker_stack.cli.resolve_shell_login_config", lambda **_kwargs: config)
    monkeypatch.setattr("docker_stack.cli.persist_shell_context", lambda value: events.append(("persist", value.context_name)))
    monkeypatch.setattr("docker_stack.cli.browser_login", lambda _config: events.append(("login", "saas-a")) or SimpleNamespace(expires_at=None))
    monkeypatch.setattr("docker_stack.cli.run_managed_shell", lambda _config, _result: 0)

    with pytest.raises(SystemExit) as excinfo:
        main(["shell", "--context", "saas-a", "https://172.31.3.3:2376"])

    assert excinfo.value.code == 0
    assert events[:2] == [("persist", "saas-a"), ("login", "saas-a")]


def test_shell_inside_same_context_reports_already_active(monkeypatch, capsys):
    def fake_resolve_shell_login_config(**_kwargs):
        return SimpleNamespace(
            manager_url="https://172.31.0.6:2378",
            context_name="office",
            docker_context_host="tcp://172.31.0.6:2378",
            skip_tls_verify=True,
            timeout_secs=30,
        )

    monkeypatch.setenv("DOCKER_STACK_SHELL_SOCKET", "/tmp/docker-stack-test.sock")
    monkeypatch.setenv("DOCKER_STACK_SHELL_SECRET", "secret")
    monkeypatch.setenv("DOCKER_STACK_SHELL_CONTEXT", "office")
    monkeypatch.setattr("docker_stack.cli.resolve_shell_login_config", fake_resolve_shell_login_config)
    monkeypatch.setattr("docker_stack.cli.browser_login", lambda _config: pytest.fail("must not log in"))

    main(["shell", "office"])

    output = capsys.readouterr().out
    assert "docker-stack shell context=office is already active" in output


def test_open_context_shell_clears_endpoint_overrides(monkeypatch, tmp_path):
    captured = {}
    for key in DOCKER_SHELL_ENDPOINT_ENV_VARS:
        monkeypatch.setenv(key, f"parent-{key}")
    monkeypatch.setenv("SHELL", "/bin/test-shell")

    def fake_exec(cmd, env=None):
        captured["cmd"] = cmd
        captured["env"] = env
        return 0

    monkeypatch.setattr("docker_stack.cli._exec_process", fake_exec)

    assert open_context_shell(tmp_path, "office") == 0

    assert captured["cmd"] == ["/bin/test-shell", "-i"]
    assert captured["env"]["DOCKER_CONFIG"] == str(tmp_path)
    assert captured["env"]["DOCKER_CONTEXT"] == "office"
    for key in DOCKER_SHELL_ENDPOINT_ENV_VARS:
        assert key not in captured["env"]


def test_active_shell_config_dir_requires_matching_context(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    monkeypatch.setenv("DOCKER_CONTEXT", "office")

    assert active_shell_config_dir("office") == tmp_path
    assert active_shell_config_dir("other") is None


def test_setup_auth_uses_access_token_and_prints_machine_readable_exports(monkeypatch, capsys):
    captured = {}

    def fake_resolve_login_config(**kwargs):
        captured["resolve"] = kwargs
        return SimpleNamespace(
            manager_url="https://172.31.0.6:2378",
            context_name="dm-proxy",
            docker_context_host="tcp://172.31.0.6:2378",
            skip_tls_verify=True,
        )

    def fake_setup_auth(config, **kwargs):
        captured["setup"] = kwargs
        assert config.context_name == "dm-proxy"
        return SimpleNamespace(
            docker_config_dir=Path.home() / ".docker-stack" / "actions" / "dm-proxy",
            context_name="dm-proxy",
            manager_url="https://172.31.0.6:2378",
            skip_tls_verify=True,
            expires_at=None,
            validation_skipped=False,
        )

    monkeypatch.setattr("docker_stack.cli.resolve_login_config", fake_resolve_login_config)
    monkeypatch.setattr("docker_stack.cli.docker_manager_setup_auth", fake_setup_auth)

    main(
        [
            "setup-auth",
            "172.31.0.6",
            "--access-token",
            "token-1",
            "--docker-config-dir",
            str(Path.home() / ".docker-stack" / "actions" / "dm-proxy"),
            "--verify-ssl",
        ]
    )

    assert captured["resolve"]["manager_target"] == "172.31.0.6"
    assert captured["resolve"]["context_name"] is None
    assert captured["resolve"]["verify_ssl"] is True
    assert captured["setup"]["access_token"] == "token-1"
    assert captured["setup"]["github_oidc_token"] is None
    assert captured["setup"]["docker_config_dir"] == Path.home() / ".docker-stack" / "actions" / "dm-proxy"
    output = capsys.readouterr().out
    assert f"DOCKER_CONFIG={Path.home() / '.docker-stack' / 'actions' / 'dm-proxy'}" in output
    assert "DOCKER_CONTEXT=dm-proxy" in output
    assert "MANAGER_URL=https://172.31.0.6:2378" in output
    assert "SKIP_TLS_VERIFY=true" in output


def test_context_use_clears_auth_when_switching_to_non_manager(monkeypatch, capsys):
    monkeypatch.setattr("docker_stack.cli.switch_docker_context", lambda context_name: False)

    main(["context", "use", "default"])

    output = capsys.readouterr().out
    assert "DOCKER_CONTEXT=default" in output
    assert "auth header cleared" in output


def test_context_use_preserves_auth_when_switching_to_manager(monkeypatch, capsys):
    monkeypatch.setattr("docker_stack.cli.switch_docker_context", lambda context_name: True)

    main(["context", "use", "office"])

    output = capsys.readouterr().out
    assert "DOCKER_CONTEXT=office" in output
    assert "auth header preserved" in output


def test_stack_not_found_error_does_not_suggest_ci_setup():
    formatted = _format_runtime_error(
        RuntimeError(
            "Manager request failed (GET /api/docker-stack/stacks/docker-manager/versions?namespace=infra): "
            'HTTP 404: {"message":"stack not found","incident_id":"d9d372ed","trace_id":"d9d372ed"}'
        )
    )

    assert "HTTP 404: stack not found (incident d9d372ed)" in formatted
    assert '{"message"' not in formatted
    assert "has no stack by that name in that namespace" in formatted
    assert "setup action" not in formatted


def test_version_probe_failure_still_suggests_manager_url_check():
    formatted = _format_runtime_error(RuntimeError("Manager request failed (GET /version): connection refused"))

    assert "DOCKER_MANAGER_URL points at a reachable manager" in formatted
