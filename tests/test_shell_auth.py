import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from docker_stack.shell_auth import (
    PROTOCOL_VERSION,
    CredentialBroker,
    ShellAuthError,
    _read_message,
    _send_message,
    _write_shell_wrappers,
    request_refreshed_tokens,
    run_managed_shell,
    shell_auth_request,
)


def make_broker(tmp_path: Path, *, expires_at: int, refresh_expires_at=None) -> CredentialBroker:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"HttpHeaders": {"Unrelated": "kept"}}), encoding="utf-8")
    return CredentialBroker(
        # macOS limits AF_UNIX paths to roughly 104 bytes; pytest's tmp path is
        # intentionally verbose, unlike the production session path.
        socket_path=Path(tempfile.gettempdir()) / f"ds-{os.getpid()}-{id(tmp_path):x}.sock",
        status_path=tmp_path / "status",
        config_path=config_path,
        manager_url="https://manager.example.test:2378",
        skip_tls_verify=False,
        timeout_secs=5,
        shell_secret="correct-secret",
        access_token="old-access",
        refresh_token="refresh-secret",
        expires_at=expires_at,
        refresh_expires_at=refresh_expires_at,
    )


def start_broker(broker: CredentialBroker):
    thread = threading.Thread(target=broker.serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while not broker.socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert broker.socket_path.exists()
    return thread


def stop_broker(broker: CredentialBroker, thread: threading.Thread, monkeypatch):
    monkeypatch.setenv("DOCKER_STACK_SHELL_SOCKET", str(broker.socket_path))
    monkeypatch.setenv("DOCKER_STACK_SHELL_SECRET", "correct-secret")
    shell_auth_request("stop", required=True)
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_ensure_valid_access_performs_no_refresh(monkeypatch, tmp_path):
    broker = make_broker(tmp_path, expires_at=int(time.time()) + 3600)
    monkeypatch.setattr(
        "docker_stack.shell_auth.request_refreshed_tokens",
        lambda *_args, **_kwargs: pytest.fail("valid access must not refresh"),
    )

    assert broker.ensure_access()["ok"] is True
    assert not broker.status_path.exists()


def test_ensure_refreshes_on_demand_and_atomically_updates_access_header(monkeypatch, tmp_path):
    broker = make_broker(tmp_path, expires_at=int(time.time()) + 30)
    calls = []

    def refresh(manager_url, refresh_token, **kwargs):
        calls.append((manager_url, refresh_token, kwargs))
        return {"access_token": "new-access", "refresh_token": "rotated-refresh", "expires_in": 600}

    monkeypatch.setattr("docker_stack.shell_auth.request_refreshed_tokens", refresh)

    result = broker.ensure_access()

    assert result["ok"] is True
    assert len(calls) == 1
    config = json.loads(broker.config_path.read_text(encoding="utf-8"))
    assert config["HttpHeaders"] == {"Unrelated": "kept", "Authorization": "Bearer new-access"}
    assert "refresh-secret" not in broker.config_path.read_text(encoding="utf-8")
    assert "rotated-refresh" not in broker.status_path.read_text(encoding="utf-8")


def test_concurrent_ensure_requests_share_one_refresh(monkeypatch, tmp_path):
    broker = make_broker(tmp_path, expires_at=int(time.time()) - 1)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def refresh(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=2)
        return {"access_token": "new-access", "refresh_token": "rotated", "expires_in": 600}

    monkeypatch.setattr("docker_stack.shell_auth.request_refreshed_tokens", refresh)
    results = []
    workers = [threading.Thread(target=lambda: results.append(broker.ensure_access())) for _ in range(6)]
    for worker in workers:
        worker.start()
    assert entered.wait(timeout=2)
    release.set()
    for worker in workers:
        worker.join(timeout=2)

    assert len(calls) == 1
    assert len(results) == 6
    assert all(result["ok"] for result in results)


def test_refresh_failure_is_not_negatively_cached(monkeypatch, tmp_path):
    broker = make_broker(tmp_path, expires_at=int(time.time()) - 1)
    calls = []

    def refresh(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ShellAuthError("DNS resolution failed", reason="refresh_dns_failed", incident_id="incident-1")
        return {"access_token": "recovered", "refresh_token": "rotated", "expires_in": 600}

    monkeypatch.setattr("docker_stack.shell_auth.request_refreshed_tokens", refresh)

    failed = broker.ensure_access()
    recovered = broker.ensure_access()

    assert failed == {
        "ok": False,
        "reason": "refresh_dns_failed",
        "message": "DNS resolution failed (incident: incident-1)",
        "incident_id": "incident-1",
        "requires_login": False,
    }
    assert recovered["ok"] is True
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (urllib.error.URLError(TimeoutError("timed out")), "refresh_timeout"),
        (urllib.error.URLError(OSError("name resolution failed")), "refresh_dns_failed"),
        (urllib.error.URLError(OSError("certificate verify failed")), "refresh_tls_failed"),
        (urllib.error.URLError(ConnectionRefusedError("connection refused")), "refresh_connection_failed"),
    ],
)
def test_refresh_transport_failure_class_reaches_caller(monkeypatch, failure, reason):
    monkeypatch.setattr(
        "docker_stack.shell_auth.urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )
    with pytest.raises(ShellAuthError) as excinfo:
        request_refreshed_tokens(
            "https://manager.example.test:2378",
            "refresh-secret",
            skip_tls_verify=False,
            timeout_secs=5,
        )
    assert excinfo.value.reason == reason
    assert "refresh-secret" not in str(excinfo.value)


def test_structured_refresh_rejection_and_incident_reach_caller(monkeypatch):
    body = BytesIO(
        json.dumps(
            {
                "message": "identity provider rejected token refresh (401 Unauthorized)",
                "reason": "refresh_rejected",
                "incident_id": "incident-401",
            }
        ).encode("utf-8")
    )
    failure = urllib.error.HTTPError("https://manager/api/auth/cli/refresh", 401, "Unauthorized", {}, body)
    monkeypatch.setattr(
        "docker_stack.shell_auth.urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )
    with pytest.raises(ShellAuthError) as excinfo:
        request_refreshed_tokens(
            "https://manager.example.test:2378",
            "refresh-secret",
            skip_tls_verify=False,
            timeout_secs=5,
        )
    assert excinfo.value.reason == "refresh_rejected"
    assert excinfo.value.incident_id == "incident-401"
    assert excinfo.value.requires_login is True


def test_malformed_refresh_response_reaches_caller(monkeypatch):
    class MalformedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"not-json"

    monkeypatch.setattr("docker_stack.shell_auth.urllib.request.urlopen", lambda *_args, **_kwargs: MalformedResponse())
    with pytest.raises(ShellAuthError) as excinfo:
        request_refreshed_tokens(
            "https://manager.example.test:2378",
            "refresh-secret",
            skip_tls_verify=False,
            timeout_secs=5,
        )
    assert excinfo.value.reason == "invalid_refresh_response"


def raw_request(socket_path: Path, payload):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(str(socket_path))
        _send_message(connection, payload)
        return _read_message(connection)
    finally:
        connection.close()


def test_missing_and_wrong_secrets_are_rejected_identically(tmp_path, monkeypatch):
    broker = make_broker(tmp_path, expires_at=int(time.time()) + 600)
    thread = start_broker(broker)
    try:
        base = {"version": PROTOCOL_VERSION, "operation": "status"}
        missing = raw_request(broker.socket_path, base)
        wrong = raw_request(broker.socket_path, {**base, "secret": "wrong"})
        assert missing == wrong == {
            "ok": False,
            "reason": "unauthorized",
            "message": "shell authentication rejected",
        }
    finally:
        stop_broker(broker, thread, monkeypatch)


def test_socket_status_never_returns_credentials(tmp_path, monkeypatch):
    broker = make_broker(tmp_path, expires_at=int(time.time()) + 600)
    thread = start_broker(broker)
    try:
        monkeypatch.setenv("DOCKER_STACK_SHELL_SOCKET", str(broker.socket_path))
        monkeypatch.setenv("DOCKER_STACK_SHELL_SECRET", "correct-secret")
        status = shell_auth_request("status", required=True)
        serialized = json.dumps(status)
        assert "access" not in serialized
        assert "refresh-secret" not in serialized
    finally:
        stop_broker(broker, thread, monkeypatch)


def test_idle_expired_broker_and_prompt_status_do_not_refresh(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "docker_stack.shell_auth.request_refreshed_tokens",
        lambda *_args, **_kwargs: calls.append(1),
    )
    broker = make_broker(tmp_path, expires_at=int(time.time()) - 1)
    thread = start_broker(broker)
    try:
        time.sleep(0.1)
        assert broker.status_path.read_text(encoding="utf-8").startswith("expired ")
        assert calls == []
    finally:
        stop_broker(broker, thread, monkeypatch)


def test_prompt_hooks_only_read_local_status_and_docker_wrapper_preflights(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    env = _write_shell_wrappers(
        tmp_path,
        "/bin/bash",
        context_name="office",
        status_path=tmp_path / "status",
    )
    assert env == {}
    wrapper = (tmp_path / "bashrc").read_text(encoding="utf-8")
    prompt_function = wrapper.split("__docker_stack_prompt_update() {", 1)[1].split("}", 1)[0]
    assert "shell-auth" not in prompt_function
    assert "docker-stack shell-auth ensure || return $?; command docker \"$@\"" in wrapper
    assert "(docker:office)" in wrapper


def test_broker_stop_erases_in_memory_credentials_and_socket(tmp_path, monkeypatch):
    broker = make_broker(tmp_path, expires_at=int(time.time()) + 600)
    thread = start_broker(broker)
    stop_broker(broker, thread, monkeypatch)

    assert broker.access_token == ""
    assert broker.refresh_token is None
    assert broker.shell_secret == ""
    assert not broker.socket_path.exists()


def test_shell_supervisor_isolates_secret_and_removes_all_session_artifacts(tmp_path, monkeypatch):
    class CapturePipe:
        def __init__(self):
            self.value = ""

        def write(self, value):
            self.value += value

        def close(self):
            pass

    class FakeBrokerProcess:
        def __init__(self):
            self.stdin = CapturePipe()
            self.returncode = None
            self.done = threading.Event()

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if not self.done.wait(timeout=timeout):
                raise subprocess.TimeoutExpired("broker", timeout)
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self.done.set()

        def kill(self):
            self.returncode = -9
            self.done.set()

    fake_process = FakeBrokerProcess()
    captured = {}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("DOCKER_STACK_SHELL_SECRET", "outer-shell-secret")
    monkeypatch.setattr("docker_stack.shell_auth.configure_docker_context_in_store", lambda _config, path: captured.setdefault("session", path))

    def fake_popen(command, **kwargs):
        captured["broker_command"] = command
        captured["broker_env"] = kwargs["env"]
        return fake_process

    def fake_shell_run(command, *, check, env):
        captured["shell_command"] = command
        captured["shell_env"] = env
        return SimpleNamespace(returncode=7)

    def fake_auth_request(operation, **_kwargs):
        assert operation == "stop"
        fake_process.returncode = 0
        fake_process.done.set()
        return {"ok": True}

    monkeypatch.setattr("docker_stack.shell_auth.subprocess.Popen", fake_popen)
    monkeypatch.setattr("docker_stack.shell_auth.subprocess.run", fake_shell_run)
    monkeypatch.setattr("docker_stack.shell_auth._wait_for_socket", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("docker_stack.shell_auth.shell_auth_request", fake_auth_request)

    result = SimpleNamespace(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=int(time.time()) + 600,
        refresh_expires_at=int(time.time()) + 3600,
    )
    config = SimpleNamespace(
        context_name="office",
        manager_url="https://manager.example.test:2378",
        skip_tls_verify=False,
        timeout_secs=30,
    )

    assert run_managed_shell(config, result) == 7
    assert result.access_token == ""
    assert result.refresh_token is None
    assert "refresh-secret" in fake_process.stdin.value
    assert "refresh-secret" not in " ".join(captured["broker_command"])
    assert captured["broker_env"].get("DOCKER_STACK_SHELL_SECRET") is None
    assert captured["shell_env"]["DOCKER_STACK_SHELL_SECRET"] != "outer-shell-secret"
    assert captured["shell_env"]["DOCKER_MANAGER_URL"] == config.manager_url
    assert not captured["session"].exists()
