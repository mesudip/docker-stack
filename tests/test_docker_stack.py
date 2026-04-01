from docker_stack.cli import Docker
from docker_stack.docker_objects import DockerConfig, DockerSecret
from docker_stack.helpers import Command, run_cli_command
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
