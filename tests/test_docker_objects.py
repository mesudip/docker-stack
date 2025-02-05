from docker_stack.docker_objects import DockerConfig, DockerSecret
from docker_stack.helpers import Command, run_cli_command


def test_when_create_object_with_old_data___then_reuse_old_object():
    config_manager = DockerConfig(log=False)
    secret_manager = DockerSecret(log=False)

    run_cli_command("docker config rm pytest_config".split(" "), raise_error=False)
    run_cli_command("docker config rm pytest_config_v2".split(" "), raise_error=False)
    run_cli_command("docker secret rm pytest_secret".split(" "), raise_error=False)
    run_cli_command("docker secret rm pytest_secret_v2".split(" "), raise_error=False)

    # Create config and assert
    command = config_manager.create("pytest_config", "sudip")
    assert command[0] == "pytest_config"
    command[1].execute()

    # Create secret and assert
    command = secret_manager.create("pytest_secret", "sudip")
    assert command[0] == "pytest_secret"
    command[1].execute()

    command = config_manager.create("pytest_config", "sudip1")
    assert command[0] == "pytest_config_v2"
    command[1].execute()

    # Create secret and assert
    command = secret_manager.create("pytest_secret", "sudip1")
    assert command[0] == "pytest_secret_v2"
    command[1].execute()

    # Create config and assert
    command = config_manager.create("pytest_config", "sudip")
    assert command[0] == "pytest_config"
    assert command[1] == Command.nop

    # Create secret and assert
    command = secret_manager.create("pytest_secret", "sudip")
    assert command[0] == "pytest_secret"
    assert command[1] == Command.nop

    run_cli_command("docker config rm pytest_config".split(" "))
    run_cli_command("docker config rm pytest_config_v2".split(" "))
    run_cli_command("docker secret rm pytest_secret".split(" "))
    run_cli_command("docker secret rm pytest_secret_v2".split(" "))
