import pytest
import json

from docker_stack.manager_api import (
    FEATURE_MESUDIP_DOCKER_ENTERPRISE,
    FEATURE_STACK_DEPLOY,
    FEATURE_STACK_QUERY,
    ManagerApiClient,
)
from docker_stack.shell_auth import ShellAuthError


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_manager_request_preflights_and_reloads_docker_header(monkeypatch):
    client = ManagerApiClient(
        "https://manager.example.test:2378",
        skip_tls_verify=True,
        default_headers={"Authorization": "Bearer stale", "X-Default": "kept"},
    )
    order = []

    def ensure():
        order.append("ensure")

    def headers():
        order.append("headers")
        return {"Authorization": "Bearer refreshed"}

    def urlopen(request, **_kwargs):
        order.append("request")
        assert request.get_header("Authorization") == "Bearer refreshed"
        assert request.get_header("X-default") == "kept"
        return FakeResponse({"ok": True})

    monkeypatch.setattr("docker_stack.manager_api.ensure_shell_access", ensure)
    monkeypatch.setattr("docker_stack.manager_api._docker_config_headers", headers)
    monkeypatch.setattr("docker_stack.manager_api.urllib.request.urlopen", urlopen)

    assert client._request_json("/api/test") == {"ok": True}
    assert order == ["ensure", "headers", "request"]


def test_failed_preflight_prevents_original_manager_request(monkeypatch):
    client = ManagerApiClient("https://manager.example.test:2378", skip_tls_verify=True)
    monkeypatch.setattr(
        "docker_stack.manager_api.ensure_shell_access",
        lambda: (_ for _ in ()).throw(ShellAuthError("refresh timed out", reason="refresh_timeout")),
    )
    monkeypatch.setattr(
        "docker_stack.manager_api.urllib.request.urlopen",
        lambda *_args, **_kwargs: pytest.fail("manager request must not be sent"),
    )

    with pytest.raises(ShellAuthError, match="refresh timed out"):
        client._request_json("/api/test")


def test_supports_stack_deploy_without_endpoint_catalog(monkeypatch):
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)
    calls = []

    def fake_request(path, *, method="GET", payload=None):
        calls.append((method, path, payload))
        if path == "/version":
            return {"MesudipFeatures": [FEATURE_MESUDIP_DOCKER_ENTERPRISE]}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(client, "_request_json", fake_request)

    assert client.supports(FEATURE_STACK_DEPLOY) is True
    assert client.supports(FEATURE_STACK_QUERY) is True
    assert calls == [("GET", "/version", None)]


def test_feature_detection_preserves_manager_transport_error(monkeypatch):
    client = ManagerApiClient("https://manager.example.test:2378", skip_tls_verify=True)

    def fail_request(*_args, **_kwargs):
        raise RuntimeError("Manager request failed (GET /version): connection refused")

    monkeypatch.setattr(client, "_request_json", fail_request)

    with pytest.raises(RuntimeError, match="connection refused"):
        client.supports(FEATURE_STACK_DEPLOY)


def test_deploy_stack_uses_direct_stack_api(monkeypatch):
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)
    calls = []

    def fake_request(path, *, method="GET", payload=None, timeout_secs=None):
        calls.append((method, path, payload))
        return {"warnings": []}

    monkeypatch.setattr(client, "_request_json", fake_request)

    payload = client.deploy_stack(
        stack="trusted-publish-test",
        namespace="default",
        compose="services: {}",
        options={},
    )

    assert payload == {"warnings": []}
    assert calls == [
        (
            "POST",
            "/api/stacks/deploy",
            {
                "stack": "trusted-publish-test",
                "namespace": "default",
                "compose": "services: {}",
            },
        )
    ]


def test_deploy_stack_stream_uses_sse_api(monkeypatch):
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)
    calls = []
    seen_events = []

    def fake_events(path, *, method="GET", payload=None, timeout_secs=None):
        calls.append((method, path, payload, timeout_secs))
        yield {"event": "stdout", "data": {"line": "creating service api"}}
        yield {"event": "done", "data": {"warnings": [], "stdout": "ok", "stderr": ""}}

    monkeypatch.setattr(client, "_request_sse_events", fake_events)

    payload = client.deploy_stack_stream(
        stack="trusted-publish-test",
        namespace="default",
        compose="services: {}",
        options={},
        on_event=seen_events.append,
    )

    assert payload == {"warnings": [], "stdout": "ok", "stderr": ""}
    assert seen_events == [
        {"event": "stdout", "data": {"line": "creating service api"}},
        {"event": "done", "data": {"warnings": [], "stdout": "ok", "stderr": ""}},
    ]
    assert calls == [
        (
            "POST",
            "/api/stacks/deploy/stream",
            {
                "stack": "trusted-publish-test",
                "namespace": "default",
                "compose": "services: {}",
            },
            300,
        )
    ]


def test_deploy_stack_stream_raises_manager_error(monkeypatch):
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)

    def fake_events(path, *, method="GET", payload=None, timeout_secs=None):
        yield {"event": "error", "data": {"message": "deploy failed"}}

    monkeypatch.setattr(client, "_request_sse_events", fake_events)

    with pytest.raises(RuntimeError, match="deploy failed"):
        client.deploy_stack_stream(
            stack="team-a",
            namespace="default",
            compose="services: {}",
            options={},
        )


def test_deploy_stack_images_uses_sse_api(monkeypatch):
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)
    calls = []
    seen_events = []

    def fake_events(path, *, method="GET", payload=None, timeout_secs=None):
        calls.append((method, path, payload, timeout_secs))
        yield {"event": "stdout", "data": {"line": "update service api"}}
        yield {"event": "done", "data": {"warnings": [], "stdout": "ok", "stderr": ""}}

    monkeypatch.setattr(client, "_request_sse_events", fake_events)

    payload = client.deploy_stack_images(
        stack="trusted-publish-test",
        namespace="default",
        images={"api": "registry.example.com/app/api:sha"},
        options={},
        on_event=seen_events.append,
    )

    assert payload == {"warnings": [], "stdout": "ok", "stderr": ""}
    assert seen_events == [
        {"event": "stdout", "data": {"line": "update service api"}},
        {"event": "done", "data": {"warnings": [], "stdout": "ok", "stderr": ""}},
    ]
    assert calls == [
        (
            "POST",
            "/api/stacks/deploy/images",
            {
                "stack": "trusted-publish-test",
                "namespace": "default",
                "images": {"api": "registry.example.com/app/api:sha"},
            },
            300,
        )
    ]


def test_deploy_stack_images_raises_manager_stream_error(monkeypatch):
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)

    def fake_events(path, *, method="GET", payload=None, timeout_secs=None):
        yield {"event": "error", "data": {"message": "image deploy failed"}}

    monkeypatch.setattr(client, "_request_sse_events", fake_events)

    with pytest.raises(RuntimeError, match="image deploy failed"):
        client.deploy_stack_images(
            stack="team-a",
            namespace="default",
            images={"api": "nginx:latest"},
            options={},
        )


def test_deploy_stack_images_sends_matching_registry_auth(monkeypatch, tmp_path):
    docker_config = tmp_path / "config.json"
    docker_config.write_text(
        json.dumps(
            {
                "auths": {
                    "registry.sireto.io": {"auth": "sireto-token"},
                    "registry.other.example": {"auth": "other-token"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)
    calls = []

    def fake_events(path, *, method="GET", payload=None, timeout_secs=None):
        calls.append((method, path, payload))
        yield {"event": "done", "data": {"warnings": [], "stdout": "", "stderr": ""}}

    monkeypatch.setattr(client, "_request_sse_events", fake_events)

    client.deploy_stack_images(
        stack="govtool-preview",
        namespace="default",
        images={"backend": "registry.sireto.io/govtool/backend:abc"},
        options={"with_registry_auth": True},
    )

    assert calls == [
        (
            "POST",
            "/api/stacks/deploy/images",
            {
                "stack": "govtool-preview",
                "namespace": "default",
                "images": {"backend": "registry.sireto.io/govtool/backend:abc"},
                "options": {
                    "with_registry_auth": True,
                    "registry_auth": {"registry.sireto.io": {"auth": "sireto-token"}},
                },
            },
        )
    ]


def test_deploy_stack_sends_only_matching_registry_auth(monkeypatch, tmp_path):
    docker_config = tmp_path / "config.json"
    docker_config.write_text(
        json.dumps(
            {
                "auths": {
                    "registry.sireto.io": {"auth": "sireto-token"},
                    "registry.other.example": {"auth": "other-token"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)
    calls = []

    def fake_request(path, *, method="GET", payload=None, timeout_secs=None):
        calls.append((method, path, payload))
        return {"warnings": []}

    monkeypatch.setattr(client, "_request_json", fake_request)

    client.deploy_stack(
        stack="govtool-preview",
        namespace="default",
        compose="services:\n  backend:\n    image: registry.sireto.io/govtool/backend:abc\n",
        options={"with_registry_auth": True},
    )

    assert calls == [
        (
            "POST",
            "/api/stacks/deploy",
            {
                "stack": "govtool-preview",
                "namespace": "default",
                "compose": "services:\n  backend:\n    image: registry.sireto.io/govtool/backend:abc\n",
                "options": {
                    "with_registry_auth": True,
                    "registry_auth": {"registry.sireto.io": {"auth": "sireto-token"}},
                },
            },
        )
    ]


def test_deploy_stack_uses_creds_store_when_auth_entry_is_empty(monkeypatch, tmp_path):
    docker_config = tmp_path / "config.json"
    docker_config.write_text(
        json.dumps(
            {
                "auths": {"registry.sireto.io": {}},
                "credsStore": "teststore",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    monkeypatch.setattr(
        "docker_stack.manager_api._credential_helper_auth",
        lambda server, helper: (
            {"serveraddress": server, "username": "sudip", "password": "secret"}
            if server == "registry.sireto.io" and helper == "teststore"
            else None
        ),
    )
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)
    calls = []

    def fake_request(path, *, method="GET", payload=None, timeout_secs=None):
        calls.append((method, path, payload))
        return {"warnings": []}

    monkeypatch.setattr(client, "_request_json", fake_request)

    client.deploy_stack(
        stack="govtool-preview",
        namespace="default",
        compose="services:\n  backend:\n    image: registry.sireto.io/govtool/backend:abc\n",
        options={"with_registry_auth": True},
    )

    assert calls[0][2]["options"]["registry_auth"] == {
        "registry.sireto.io": {
            "serveraddress": "registry.sireto.io",
            "username": "sudip",
            "password": "secret",
        }
    }


def test_enterprise_query_uses_direct_manager_fast_paths(monkeypatch):
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)
    calls = []

    def fake_request(path, *, method="GET", payload=None):
        calls.append((method, path, payload))
        if path == "/version":
            return {"MesudipFeatures": [FEATURE_MESUDIP_DOCKER_ENTERPRISE]}
        if path == "/api/docker-stack/stacks":
            return {"stacks": []}
        if path == "/api/docker-stack/stacks/team-a/versions?namespace=prod":
            return {"versions": []}
        if path == "/api/docker-stack/stacks/team-a/compose?namespace=prod&tag=latest":
            return {"compose": "services: {}"}
        if path == "/api/docker-stack/nodes":
            return {"nodes": []}
        if path == "/api/stacks/team-a/rollback":
            return {"warnings": []}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(client, "_request_json", fake_request)

    assert client.list_stacks() == {"stacks": []}
    assert client.list_stack_versions("team-a", namespace="prod") == {"versions": []}
    assert client.get_stack_compose("team-a", namespace="prod", tag="latest") == {"compose": "services: {}"}
    assert client.list_nodes() == {"nodes": []}
    assert client.rollback_stack(stack="team-a", namespace="prod", version="2") == {"warnings": []}
    assert calls == [
        ("GET", "/version", None),
        ("GET", "/api/docker-stack/stacks", None),
        ("GET", "/api/docker-stack/stacks/team-a/versions?namespace=prod", None),
        ("GET", "/api/docker-stack/stacks/team-a/compose?namespace=prod&tag=latest", None),
        ("GET", "/api/docker-stack/nodes", None),
        ("POST", "/api/stacks/team-a/rollback", {"namespace": "prod", "version": "2"}),
    ]


def test_enterprise_remove_uses_manager_lifecycle_api(monkeypatch):
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)
    calls = []

    def fake_request(path, *, method="GET", payload=None):
        calls.append((method, path, payload))
        if path == "/version":
            return {"MesudipFeatures": [FEATURE_MESUDIP_DOCKER_ENTERPRISE]}
        if path == "/api/stacks/team%2Fapi?namespace=prod":
            return {"stack": "team/api", "namespace": "prod"}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(client, "_request_json", fake_request)

    assert client.remove_stack(stack="team/api", namespace="prod") == {
        "stack": "team/api",
        "namespace": "prod",
    }
    assert calls == [
        ("GET", "/version", None),
        ("DELETE", "/api/stacks/team%2Fapi?namespace=prod", None),
    ]


def test_control_plane_error_lists_visible_endpoints(monkeypatch):
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)

    def fake_request(path, *, method="GET", payload=None):
        assert path == "/api/endpoints"
        return {
            "endpoints": [
                {"id": 1, "name": "office", "slug": "office"},
                {"id": 2, "name": "lab"},
            ]
        }

    monkeypatch.setattr(client, "_request_json", fake_request)

    with pytest.raises(RuntimeError) as excinfo:
        client._resolve_endpoint_id()

    assert "control-plane targets" in str(excinfo.value)
    assert "id=1, name=office, slug=office" in str(excinfo.value)
    assert "id=2, name=lab" in str(excinfo.value)
