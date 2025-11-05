from docker_stack.docker_objects import DockerConfig, DockerSecret
from docker_stack.helpers import Command, run_cli_command
from docker_stack import main


def test_when_create_stack_support_x_content():
    main(["deploy", "pytest_test_x_content", "./tests/docker-compose-example.yml"])
