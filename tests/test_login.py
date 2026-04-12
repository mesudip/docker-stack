import json
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request

import pytest

from docker_stack.login import (
    clear_docker_config_authorization_header,
    DockerManagerLoginConfig,
    build_auth_url,
    browser_login,
    configure_docker_context,
    detect_manager_url,
    ensure_isolated_login,
    format_expiry,
    isolated_docker_config_dir,
    is_manager_context,
    merge_docker_config_header,
    resolve_login_config,
    resolve_shell_login_config,
    setup_auth,
    switch_docker_context,
    token_issuer_from_jwt,
)


def test_merge_docker_config_header_with_empty_config(tmp_path):
    config_path = tmp_path / "config.json"

    merge_docker_config_header("token-1", config_path)

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload == {"HttpHeaders": {"Authorization": "Bearer token-1"}}


def test_merge_docker_config_header_preserves_auths(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "auths": {
                    "registry.example.com": {"auth": "abc"}
                }
            }
        ),
        encoding="utf-8",
    )

    merge_docker_config_header("token-2", config_path)

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["auths"] == {"registry.example.com": {"auth": "abc"}}
    assert payload["HttpHeaders"]["Authorization"] == "Bearer token-2"


def test_merge_docker_config_header_preserves_existing_http_headers(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "HttpHeaders": {
                    "X-Test": "value"
                }
            }
        ),
        encoding="utf-8",
    )

    merge_docker_config_header("token-3", config_path)

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["HttpHeaders"]["X-Test"] == "value"
    assert payload["HttpHeaders"]["Authorization"] == "Bearer token-3"


def test_merge_docker_config_header_preserves_current_context(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "currentContext": "default",
                "credsStore": "osxkeychain",
            }
        ),
        encoding="utf-8",
    )

    merge_docker_config_header("token-4", config_path)

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["currentContext"] == "default"
    assert payload["credsStore"] == "osxkeychain"
    assert payload["HttpHeaders"]["Authorization"] == "Bearer token-4"


def test_clear_docker_config_authorization_header_preserves_other_headers(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "HttpHeaders": {
                    "Authorization": "Bearer token-4",
                    "X-Test": "value",
                },
                "currentContext": "default",
            }
        ),
        encoding="utf-8",
    )

    clear_docker_config_authorization_header(config_path)

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["HttpHeaders"] == {"X-Test": "value"}
    assert payload["currentContext"] == "default"


def test_is_manager_context_checks_description(monkeypatch):
    monkeypatch.setattr(
        "docker_stack.login._inspect_docker_context",
        lambda *_args, **_kwargs: {"Metadata": {"Description": "Docker-Manager proxy"}},
    )

    assert is_manager_context("office") is True


def test_switch_docker_context_clears_auth_for_non_manager(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("docker_stack.login.run_command", fake_run)
    monkeypatch.setattr("docker_stack.login.is_manager_context", lambda *_args, **_kwargs: False)
    cleared = {"called": False}
    monkeypatch.setattr(
        "docker_stack.login.clear_docker_config_authorization_header",
        lambda *_args, **_kwargs: cleared.__setitem__("called", True),
    )

    manager_context = switch_docker_context("default")

    assert manager_context is False
    assert calls == [["docker", "context", "use", "default"]]
    assert cleared["called"] is True


def test_format_expiry_shows_remaining_duration(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1000)

    assert format_expiry(1005) == "5s"
    assert format_expiry(1065) == "1m 5s"
    assert format_expiry(4665) == "1h 1m 5s"
    assert format_expiry(91005) == "1d 1h 5s"
    assert format_expiry(92010) == "1d 1h 16m 50s"


def test_token_issuer_from_jwt_extracts_issuer():
    payload = "eyJpc3MiOiJodHRwczovL3Rva2VuLmFjdGlvbnMuZ2l0aHVidXNlcmNvbnRlbnQuY29tIn0"
    token = f"header.{payload}.signature"

    assert token_issuer_from_jwt(token) == "https://token.actions.githubusercontent.com"


def test_configure_docker_context_creates_when_missing(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if command[:3] == ["docker", "context", "inspect"] else 0, "", "")

    monkeypatch.setattr("docker_stack.login.run_command", fake_run)
    monkeypatch.setattr("docker_stack.login.current_docker_context_target", lambda: (None, None))

    configure_docker_context(resolve_login_config(context_name="agent-context"))

    assert calls == [
        ["docker", "context", "inspect", "agent-context"],
        [
            "docker",
            "context",
            "create",
            "agent-context",
            "--description",
            "Docker-Manager proxy",
            "--docker",
            "host=tcp://localhost:8080",
        ],
        ["docker", "context", "use", "agent-context"],
    ]


def test_configure_docker_context_updates_when_present(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("docker_stack.login.run_command", fake_run)
    monkeypatch.setattr("docker_stack.login.current_docker_context_target", lambda: (None, None))

    configure_docker_context(resolve_login_config(context_name="agent-context"))

    assert calls == [
        ["docker", "context", "inspect", "agent-context"],
        [
            "docker",
            "context",
            "update",
            "agent-context",
            "--description",
            "Docker-Manager proxy",
            "--docker",
            "host=tcp://localhost:8080",
        ],
        ["docker", "context", "use", "agent-context"],
    ]


def test_configure_docker_context_uses_skip_tls_verify_for_detected_tls(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if command[:3] == ["docker", "context", "inspect"] else 0, "", "")

    monkeypatch.setattr("docker_stack.login.run_command", fake_run)
    monkeypatch.setattr("docker_stack.login.current_docker_context_target", lambda: (None, None))

    configure_docker_context(
        DockerManagerLoginConfig(
            manager_url="https://agent.example.com:2378",
            context_name="office",
            timeout_secs=5,
            skip_tls_verify=True,
        )
    )

    assert calls[1][-1] == "host=tcp://agent.example.com:2378,skip-tls-verify=true"


def test_detect_manager_url_prefers_https_when_available(monkeypatch):
    def fake_probe(url, *, timeout=3, verify_tls=True):
        if url == "https://172.31.0.6:2376":
            return True, False
        return False, False

    monkeypatch.setattr("docker_stack.login.probe_manager_url", fake_probe)

    url, skip_tls_verify = detect_manager_url("172.31.0.6")

    assert url == "https://172.31.0.6:2376"
    assert skip_tls_verify is False


def test_detect_manager_url_marks_skip_verify_for_self_signed_tls(monkeypatch):
    def fake_probe(url, *, timeout=3, verify_tls=True):
        if url == "https://172.31.0.6:2376" and verify_tls:
            return False, True
        if url == "https://172.31.0.6:2376" and not verify_tls:
            return True, False
        return False, False

    monkeypatch.setattr("docker_stack.login.probe_manager_url", fake_probe)

    url, skip_tls_verify = detect_manager_url("172.31.0.6")

    assert url == "https://172.31.0.6:2376"
    assert skip_tls_verify is True


def test_detect_manager_url_uses_http_default_port_when_https_unavailable(monkeypatch):
    def fake_probe(url, *, timeout=3, verify_tls=True):
        if url == "http://172.31.0.6:2375":
            return True, False
        return False, False

    monkeypatch.setattr("docker_stack.login.probe_manager_url", fake_probe)

    url, skip_tls_verify = detect_manager_url("172.31.0.6")

    assert url == "http://172.31.0.6:2375"
    assert skip_tls_verify is False


def test_detect_manager_url_verify_ssl_requires_https(monkeypatch):
    with pytest.raises(RuntimeError, match="requires an HTTPS manager URL"):
        detect_manager_url("http://172.31.0.6", verify_ssl=True)


def test_detect_manager_url_verify_ssl_does_not_fall_back_to_http(monkeypatch):
    monkeypatch.setattr("docker_stack.login.probe_manager_url", lambda *args, **kwargs: (False, False))

    with pytest.raises(RuntimeError, match="verified HTTPS Docker-Manager endpoint"):
        detect_manager_url("172.31.0.6", verify_ssl=True)


def test_build_auth_url_uses_manager_broker_endpoint(monkeypatch):
    monkeypatch.setattr(
        "docker_stack.login._request_manager_json",
        lambda config, path, **kwargs: {"auth_url": f"https://broker.example.test{path}"},
    )

    auth_url = build_auth_url(
        DockerManagerLoginConfig(
            manager_url="https://172.31.0.6:2378",
            context_name="office",
            timeout_secs=5,
            skip_tls_verify=True,
        ),
        "http://localhost:8079/auth/callback",
        "state-1",
    )

    assert auth_url == (
        "https://broker.example.test"
        "/api/auth/cli/login?redirect_uri=http%3A%2F%2Flocalhost%3A8079%2Fauth%2Fcallback&state=state-1"
    )


def test_build_auth_url_requires_latest_manager_broker_endpoint(monkeypatch):
    def fake_request(config, path, **kwargs):
        raise urllib.error.HTTPError(path, 404, "not found", hdrs=None, fp=None)

    monkeypatch.setattr("docker_stack.login._request_manager_json", fake_request)

    with pytest.raises(RuntimeError, match="latest Docker-Manager broker endpoint"):
        build_auth_url(resolve_login_config(manager_url="http://172.31.0.6:2378"), "http://localhost:8079/auth/callback", "state-1")


def test_build_auth_url_rejects_raw_daemon(monkeypatch):
    def fake_request(config, path, **kwargs):
        raise urllib.error.HTTPError(path, 404, "not found", hdrs=None, fp=None)

    monkeypatch.setattr("docker_stack.login._request_manager_json", fake_request)

    with pytest.raises(RuntimeError, match="raw Docker daemon endpoints do not require login"):
        build_auth_url(resolve_login_config(manager_url="http://172.31.0.6:2378"), "http://localhost:8079/auth/callback", "state-1")


def test_resolve_login_config_ignores_malformed_context_host(monkeypatch):
    monkeypatch.setattr("docker_stack.login.current_docker_context_target", lambda: ("office", None))

    config = resolve_login_config(context_name="office")

    assert config.manager_url == "http://localhost:8080"
    assert config.context_name == "office"


def test_resolve_login_config_detects_tls_for_positional_target(monkeypatch):
    monkeypatch.setattr("docker_stack.login.detect_manager_url", lambda value, verify_ssl=False: ("https://172.31.0.6:2378", True))

    config = resolve_login_config(manager_target="172.31.0.6:2378", context_name="office")

    assert config.manager_url == "https://172.31.0.6:2378"
    assert config.context_name == "office"
    assert config.skip_tls_verify is True


def test_resolve_login_config_prefers_named_context_for_portless_target(monkeypatch):
    monkeypatch.setattr(
        "docker_stack.login.docker_context_target",
        lambda context_name, docker_config_dir=None: "tcp://172.31.0.6:2378" if context_name == "office" else None,
    )
    monkeypatch.setattr("docker_stack.login.detect_manager_url", lambda value, verify_ssl=False: ("https://172.31.0.6:2378", True))

    config = resolve_login_config(manager_target="office")

    assert config.manager_url == "https://172.31.0.6:2378"
    assert config.context_name == "office"
    assert config.skip_tls_verify is True


def test_resolve_login_config_uses_current_context_target(monkeypatch):
    monkeypatch.setattr(
        "docker_stack.login.current_docker_context_target",
        lambda: ("office", "tcp://172.31.0.6:2378"),
    )
    monkeypatch.setattr("docker_stack.login.detect_manager_url", lambda value, verify_ssl=False: ("https://172.31.0.6:2378", True))

    config = resolve_login_config()

    assert config.manager_url == "https://172.31.0.6:2378"
    assert config.context_name == "office"
    assert config.skip_tls_verify is True


def test_resolve_login_config_keeps_defaults_without_discovery(monkeypatch):
    monkeypatch.setattr("docker_stack.login.current_docker_context_target", lambda: (None, None))

    config = resolve_login_config(context_name="office")

    assert config.manager_url == "http://localhost:8080"
    assert config.context_name == "office"


def test_resolve_shell_login_config_uses_persisted_context(monkeypatch):
    monkeypatch.setattr(
        "docker_stack.login.docker_context_target",
        lambda context_name, docker_config_dir=None: "tcp://172.31.0.6:2378" if docker_config_dir == isolated_docker_config_dir("office") else None,
    )
    monkeypatch.setattr("docker_stack.login.detect_manager_url", lambda value: ("https://172.31.0.6:2378", True))

    config = resolve_shell_login_config(shell_name="office", manager_target=None, context_name=None)

    assert config.manager_url == "https://172.31.0.6:2378"
    assert config.context_name == "office"
    assert config.skip_tls_verify is True


def test_resolve_shell_login_config_requires_context_name_for_first_run():
    with pytest.raises(RuntimeError, match="Pass --context <name> when providing a manager target"):
        resolve_shell_login_config(shell_name=None, manager_target="172.31.0.6:2378", context_name=None)


def test_ensure_isolated_login_skips_browser_when_token_is_active(monkeypatch, tmp_path):
    config = DockerManagerLoginConfig(
        manager_url="https://172.31.0.6:2378",
        context_name="office",
        timeout_secs=5,
        skip_tls_verify=True,
    )
    monkeypatch.setattr("docker_stack.login.ensure_isolated_docker_config", lambda _: tmp_path / "config.json")
    monkeypatch.setattr("docker_stack.login.configure_docker_context_in_store", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("docker_stack.login.token_is_active", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("docker_stack.login.browser_login", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not login")))

    config_dir, result = ensure_isolated_login(config, docker_config_dir=tmp_path)

    assert config_dir == tmp_path
    assert result is None


def test_setup_auth_with_access_token_validates_and_writes_header(monkeypatch, tmp_path):
    config = DockerManagerLoginConfig(
        manager_url="https://172.31.0.6:2378",
        context_name="office",
        timeout_secs=5,
        skip_tls_verify=True,
    )
    monkeypatch.setattr("docker_stack.login.configure_docker_context_in_store", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("docker_stack.login.token_is_active", lambda *_args, **_kwargs: True)

    result = setup_auth(
        config,
        access_token="token-setup",
        docker_config_dir=tmp_path,
    )

    payload = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert payload["HttpHeaders"]["Authorization"] == "Bearer token-setup"
    assert result.docker_config_dir == tmp_path
    assert result.validation_skipped is False


def test_setup_auth_rejects_invalid_access_token(monkeypatch, tmp_path):
    config = DockerManagerLoginConfig(
        manager_url="https://172.31.0.6:2378",
        context_name="office",
        timeout_secs=5,
        skip_tls_verify=True,
    )
    monkeypatch.setattr("docker_stack.login.configure_docker_context_in_store", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("docker_stack.login.token_is_active", lambda *_args, **_kwargs: False)

    with pytest.raises(RuntimeError, match="rejected the access token"):
        setup_auth(config, access_token="bad-token", docker_config_dir=tmp_path)


def test_setup_auth_skips_profile_validation_for_github_oidc(monkeypatch, tmp_path):
    config = DockerManagerLoginConfig(
        manager_url="https://172.31.0.6:2378",
        context_name="office",
        timeout_secs=5,
        skip_tls_verify=True,
    )
    called = {"token_is_active": 0}

    def fake_token_is_active(*_args, **_kwargs):
        called["token_is_active"] += 1
        return True

    monkeypatch.setattr("docker_stack.login.configure_docker_context_in_store", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("docker_stack.login.token_is_active", fake_token_is_active)

    github_token = (
        "header."
        "eyJpc3MiOiJodHRwczovL3Rva2VuLmFjdGlvbnMuZ2l0aHVidXNlcmNvbnRlbnQuY29tIiwiZXhwIjoxOTAwMDAwMDAwfQ."
        "signature"
    )
    result = setup_auth(
        config,
        github_oidc_token=github_token,
        docker_config_dir=tmp_path,
    )

    payload = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert payload["HttpHeaders"]["Authorization"] == f"Bearer {github_token}"
    assert result.validation_skipped is True
    assert called["token_is_active"] == 0


def test_browser_login_handles_callback_and_token_exchange(monkeypatch):
    config = DockerManagerLoginConfig(
        manager_url="http://localhost:8080",
        context_name="office",
        timeout_secs=5,
        skip_tls_verify=False,
    )

    def fake_exchange(login_config, code, redirect_uri, **kwargs):
        assert code == "auth-code"
        assert redirect_uri.endswith("/auth/callback")
        return {
            "access_token": "header.eyJleHAiOjE5MDAwMDAwMDB9.signature"
        }

    def fake_browser_open(auth_url):
        parsed = urllib.parse.urlparse(auth_url)
        query = urllib.parse.parse_qs(parsed.query)
        redirect_uri = query["redirect_uri"][0]
        state = query["state"][0]

        def callback():
            urllib.request.urlopen(f"{redirect_uri}?code=auth-code&state={state}").read()

        threading.Thread(target=callback, daemon=True).start()
        return True

    monkeypatch.setattr(
        "docker_stack.login.build_auth_url",
        lambda login_config, redirect_uri, state: (
            "https://keycloak.example.com/realms/master/protocol/openid-connect/auth?"
            + urllib.parse.urlencode({"redirect_uri": redirect_uri, "state": state})
        ),
    )
    monkeypatch.setattr("docker_stack.login.exchange_authorization_code", fake_exchange)

    def port_finder():
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    result = browser_login(config, browser_opener=fake_browser_open, port_finder=port_finder)

    assert result.redirect_uri.endswith("/auth/callback")
    assert result.callback_port in range(8070, 8080)
    assert result.access_token == "header.eyJleHAiOjE5MDAwMDAwMDB9.signature"
    assert result.expires_at == 1900000000
