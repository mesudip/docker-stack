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


def test_create_handles_unlabeled_existing_object_name(monkeypatch):
    manager = DockerConfig(log=False)

    def fake_run_cli_command(command, stdin=None, raise_error=True, log=True, shell=False, interactive=False, cwd=None):
        if command[:3] == ["docker", "config", "ls"]:
            return ""
        if command[:3] == ["docker", "config", "inspect"] and "--format" not in command:
            return '[{"ID":"abc","Spec":{"Name":"stack_config.json"}}]'
        if command[:3] == ["docker", "config", "inspect"] and command[-1] == "{{json .Spec.Labels}}":
            return "{}"
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr("docker_stack.docker_objects.run_cli_command", fake_run_cli_command)
    monkeypatch.setattr(manager, "check", lambda _name: True)

    object_name, command = manager.create("stack_config.json", "hello")
    assert object_name == "stack_config.json_v2"
    assert command.command[:4] == ["docker", "config", "create", "--label"]
    assert command.command[-2:] == ["stack_config.json_v2", "-"]
