import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from docker_stack.cli import Docker
from docker_stack.helpers import Command
from docker_stack import main


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

    def list_stacks(self):
        return {"stacks": [{"stack": "team-a", "versions": ["1", "2"]}]}

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
        self.deploy_payloads.append(
            {"stack": stack, "namespace": namespace, "compose": compose, "options": options or {}}
        )
        return {"warnings": [], "stdout": "", "stderr": ""}

    def rollback_stack(self, *, stack, namespace, version):
        self.rollback_payloads.append(
            {"stack": stack, "namespace": namespace, "version": version}
        )
        return {"warnings": [], "stdout": "", "stderr": ""}


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
            "name": "govtool_app.conf",
            "content": "hello",
            "labels": {},
        }
    ]
    assert fake_manager.resolve_secret_payloads == [
        {
            "stack": "govtool",
            "namespace": "default",
            "name": "govtool_app-secret",
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
    assert processed_configs == {"app.conf": {"name": "govtool_app.conf_v2", "external": True}}
    assert processed_secrets == {"app-secret": {"name": "govtool_app-secret", "external": True}}
    assert docker.stack.generated_secrets == {"app-secret": "generated-from-manager"}


def test_build_uses_service_dockerfile(tmp_path):
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        """
services:
  storybook:
    image: example/storybook:test
    build:
      context: ./frontend
      dockerfile: storybook.Dockerfile
      args:
        NPMRC_TOKEN: dummy
""".strip()
    )

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
        ): json.dumps(
            [
                {
                    "Spec": {
                        "Data": base64.b64encode(b"services:\n  api:\n    image: busybox\n").decode("utf-8")
                    }
                }
            ]
        ),
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


def test_shell_reuses_named_context_and_opens_isolated_bash(monkeypatch, capsys):
    captured = {}

    def fake_resolve_shell_login_config(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            manager_url="https://172.31.0.6:2378",
            context_name="office",
            docker_context_host="tcp://172.31.0.6:2378",
            skip_tls_verify=True,
        )

    def fake_ensure_isolated_login(config):
        assert config.context_name == "office"
        return Path.home() / ".docker-stack" / "actions" / "office", None

    def fake_open_context_shell(config_dir, context_name):
        assert config_dir == Path.home() / ".docker-stack" / "actions" / "office"
        assert context_name == "office"
        return 0

    monkeypatch.setattr("docker_stack.cli.resolve_shell_login_config", fake_resolve_shell_login_config)
    monkeypatch.setattr("docker_stack.cli.ensure_isolated_login", fake_ensure_isolated_login)
    monkeypatch.setattr("docker_stack.cli.open_context_shell", fake_open_context_shell)

    with pytest.raises(SystemExit) as excinfo:
        main(["shell", "office"])

    assert excinfo.value.code == 0
    assert captured["shell_name"] == "office"
    assert captured["manager_target"] is None
    assert captured["context_name"] is None
    output = capsys.readouterr().out
    assert "Shell context=office" in output
    assert "Access token already active." in output


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
