import json
import os

from docker_stack.cli import main


def test_node_ls_prints_nodes_and_labels(monkeypatch, capsys):
    inspect_payloads = {
        "node-1": {
            "Spec": {
                "Role": "manager",
                "Labels": {
                    "blockchain": "true",
                    "gateway": "true",
                    "newt.host": "prod1",
                },
            },
            "Status": {"Addr": "10.0.0.1"},
        },
        "node-2": {
            "Spec": {
                "Role": "worker",
                "Labels": {
                    "govtool": "true",
                },
            },
            "Status": {"Addr": "10.0.0.2"},
        },
    }

    def fake_run_cli_command(command, **kwargs):
        if command[:3] == ["docker", "node", "ls"]:
            return "\n".join(
                [
                    json.dumps(
                        {
                            "ID": "node-1",
                            "Hostname": "swarm-a",
                            "Status": "Ready",
                            "Availability": "Active",
                            "ManagerStatus": "Leader",
                        }
                    ),
                    json.dumps(
                        {
                            "ID": "node-2",
                            "Hostname": "swarm-b",
                            "Status": "Ready",
                            "Availability": "Drain",
                            "ManagerStatus": "",
                        }
                    ),
                ]
            )
        if command[:3] == ["docker", "node", "inspect"]:
            return json.dumps(inspect_payloads[command[3]])
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr("docker_stack.cli.run_cli_command", fake_run_cli_command)
    monkeypatch.setattr("docker_stack.cli.discover_manager_client", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("docker_stack.cli.shutil.get_terminal_size", lambda *args, **kwargs: os.terminal_size((72, 20)))

    main(["node", "ls"])

    output = capsys.readouterr().out
    assert "Hostname" in output
    assert "Role" in output
    assert "State" in output
    assert "Labels" in output
    assert "swarm-a" in output
    assert "manager (Leader)" in output
    assert "10.0.0.1" in output
    assert "blockchain, gateway," in output
    assert "newt.host=prod1" in output
    assert "swarm-b" in output
    assert "worker" in output
    assert "govtool" in output
