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
        if path in {
            "/api/docker-stack/stacks?namespace=default",
            "/api/docker-stack/stacks?all_namespaces=true",
        }:
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
    assert client.list_stacks(namespace=None) == {"stacks": []}
    assert client.list_stack_versions("team-a", namespace="prod") == {"versions": []}
    assert client.get_stack_compose("team-a", namespace="prod", tag="latest") == {"compose": "services: {}"}
    assert client.list_nodes() == {"nodes": []}
    assert client.rollback_stack(stack="team-a", namespace="prod", version="2") == {"warnings": []}
    assert calls == [
        ("GET", "/version", None),
        ("GET", "/api/docker-stack/stacks?namespace=default", None),
        ("GET", "/api/docker-stack/stacks?all_namespaces=true", None),
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


def _sse_client(monkeypatch):
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)
    monkeypatch.setattr("docker_stack.manager_api.time.sleep", lambda _secs: None)
    return client


def test_http_409_deployment_in_progress_becomes_typed_error(monkeypatch):
    import io
    import urllib.error

    from docker_stack.manager_api import ManagerDeploymentInProgressError

    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)
    body = json.dumps(
        {
            "code": "deployment_in_progress",
            "message": "a deploy for this stack started by alice is still running",
            "active_deployment": {"operation": "deploy", "actor": "alice", "deployment_id": "dep-1", "started_at_ms": 0},
        }
    ).encode("utf-8")

    def urlopen(request, **_kwargs):
        raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, io.BytesIO(body))

    monkeypatch.setattr("docker_stack.manager_api.ensure_shell_access", lambda: None)
    monkeypatch.setattr("docker_stack.manager_api._docker_config_headers", dict)
    monkeypatch.setattr("docker_stack.manager_api.urllib.request.urlopen", urlopen)

    with pytest.raises(ManagerDeploymentInProgressError) as json_exc:
        client._request_json("/api/stacks/web/rollback", method="POST", payload={})
    assert json_exc.value.active_deployment["actor"] == "alice"
    assert "started by alice" in str(json_exc.value)
    assert "(deployment dep-1)" in str(json_exc.value)

    with pytest.raises(ManagerDeploymentInProgressError):
        list(client._request_sse_events("/api/stacks/deploy/stream", method="POST", payload={}))


def test_http_409_without_deploy_code_stays_generic(monkeypatch):
    import io
    import urllib.error

    from docker_stack.manager_api import ManagerDeploymentInProgressError

    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)

    def urlopen(request, **_kwargs):
        raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, io.BytesIO(b'{"message":"plan is stale"}'))

    monkeypatch.setattr("docker_stack.manager_api.ensure_shell_access", lambda: None)
    monkeypatch.setattr("docker_stack.manager_api._docker_config_headers", dict)
    monkeypatch.setattr("docker_stack.manager_api.urllib.request.urlopen", urlopen)

    with pytest.raises(RuntimeError, match="HTTP 409") as exc:
        client._request_json("/api/test")
    assert not isinstance(exc.value, ManagerDeploymentInProgressError)


def test_deploy_stack_stream_waits_for_active_deployment(monkeypatch):
    from docker_stack.manager_api import ManagerDeploymentInProgressError

    client = _sse_client(monkeypatch)
    attempts = []
    seen_events = []
    active = {"operation": "deploy", "actor": "alice", "deployment_id": "dep-1", "started_at_ms": 0}

    def fake_events(path, *, method="GET", payload=None, timeout_secs=None):
        attempts.append(path)
        if len(attempts) < 3:
            raise ManagerDeploymentInProgressError("HTTP 409: busy", active_deployment=active)
        yield {"event": "deployment", "data": {"deployment_id": "dep-2", "status": "running"}}
        yield {"event": "done", "data": {"warnings": [], "stdout": "ok", "stderr": ""}}

    monkeypatch.setattr(client, "_request_sse_events", fake_events)

    payload = client.deploy_stack_stream(
        stack="web",
        namespace="default",
        compose="services: {}",
        options={},
        on_event=seen_events.append,
    )

    assert payload == {"warnings": [], "stdout": "ok", "stderr": ""}
    assert attempts == ["/api/stacks/deploy/stream"] * 3
    waiting = [event for event in seen_events if event["event"] == "waiting"]
    assert len(waiting) == 1, seen_events
    assert waiting[0]["data"]["actor"] == "alice"
    assert waiting[0]["data"]["wait_budget_secs"] == 300
    assert seen_events[-1]["event"] == "done"


def test_deploy_stack_stream_gives_up_when_wait_budget_is_zero(monkeypatch):
    from docker_stack.manager_api import ManagerDeploymentInProgressError

    client = _sse_client(monkeypatch)
    monkeypatch.setenv("DOCKER_MANAGER_DEPLOY_WAIT_SECS", "0")
    attempts = []

    def fake_events(path, *, method="GET", payload=None, timeout_secs=None):
        attempts.append(path)
        raise ManagerDeploymentInProgressError(
            "HTTP 409: a deploy started by alice is still running",
            active_deployment={"operation": "deploy", "actor": "alice"},
        )
        yield  # pragma: no cover - makes this a generator like the real method

    monkeypatch.setattr(client, "_request_sse_events", fake_events)

    with pytest.raises(ManagerDeploymentInProgressError, match="started by alice is still running"):
        client.deploy_stack_stream(stack="web", namespace="default", compose="services: {}", options={})
    assert attempts == ["/api/stacks/deploy/stream"]


def test_deploy_stack_stream_gives_up_after_wait_budget(monkeypatch):
    from docker_stack.manager_api import ManagerDeploymentInProgressError

    client = _sse_client(monkeypatch)
    monkeypatch.setenv("DOCKER_MANAGER_DEPLOY_WAIT_SECS", "12")
    clock = {"now": 1000.0}

    def sleep(secs):
        clock["now"] += secs

    monkeypatch.setattr("docker_stack.manager_api.time.sleep", sleep)
    monkeypatch.setattr("docker_stack.manager_api.time.monotonic", lambda: clock["now"])
    attempts = []

    def fake_events(path, *, method="GET", payload=None, timeout_secs=None):
        attempts.append(path)
        raise ManagerDeploymentInProgressError(
            "HTTP 409: a deploy started by alice is still running",
            active_deployment={"operation": "deploy", "actor": "alice"},
        )
        yield  # pragma: no cover

    monkeypatch.setattr(client, "_request_sse_events", fake_events)

    with pytest.raises(ManagerDeploymentInProgressError, match="gave up after waiting 10s") as exc:
        client.deploy_stack_stream(stack="web", namespace="default", compose="services: {}", options={})
    assert "DOCKER_MANAGER_DEPLOY_WAIT_SECS" in str(exc.value)
    assert len(attempts) == 3  # t=0, t=5, t=10; the next poll would overshoot the 12s budget


def test_deploy_stack_stream_reports_every_failed_service(monkeypatch):
    from docker_stack.manager_api import ManagerDeployFailedError

    client = _sse_client(monkeypatch)

    def fake_events(path, *, method="GET", payload=None, timeout_secs=None):
        yield {"event": "deployment", "data": {"deployment_id": "dep-7", "status": "running"}}
        yield {
            "event": "error",
            "data": {
                "code": "deployment_partial_failure",
                "message": "one or more services failed; all parallel service operations have completed",
                "deployment_id": "dep-7",
                "rollback_available": True,
                "services": [
                    {"service": "api", "action": "update", "status": "failed", "error": "image pull failed", "incident_id": "abc123"},
                    {"service": "worker", "action": "create", "status": "failed", "error": "port 8080 in use", "incident_id": None},
                    {"service": "db", "action": "unchanged", "status": "succeeded", "error": None},
                ],
            },
        }

    monkeypatch.setattr(client, "_request_sse_events", fake_events)

    with pytest.raises(ManagerDeployFailedError) as exc:
        client.deploy_stack_stream(stack="web", namespace="default", compose="services: {}", options={})

    message = str(exc.value)
    assert "one or more services failed" in message
    assert "  - api (update): image pull failed (incident abc123)" in message
    assert "  - worker (create): port 8080 in use" in message
    assert "db" not in message.replace("deployment", "")
    assert "rollback is available" in message and "dep-7" in message
    assert exc.value.code == "deployment_partial_failure"
    assert exc.value.deployment_id == "dep-7"
    assert exc.value.rollback_available is True


def test_deploy_stack_images_reports_aborted_deployment(monkeypatch):
    client = _sse_client(monkeypatch)

    def fake_events(path, *, method="GET", payload=None, timeout_secs=None):
        yield {
            "event": "error",
            "data": {"code": "deployment_aborted", "message": "deployment aborted: aborted by bob", "deployment_id": "dep-3"},
        }

    monkeypatch.setattr(client, "_request_sse_events", fake_events)

    with pytest.raises(RuntimeError, match="deployment aborted: aborted by bob"):
        client.deploy_stack_images(stack="web", namespace="default", images={"api": "example/api:2"}, options={})


def test_rollback_stack_waits_for_active_deployment(monkeypatch):
    from docker_stack.manager_api import ManagerDeploymentInProgressError

    client = _sse_client(monkeypatch)
    calls = []
    notices = []

    def fake_request(path, *, method="GET", payload=None, timeout_secs=None):
        calls.append((method, path, payload))
        if path == "/version":
            return {"MesudipFeatures": [FEATURE_MESUDIP_DOCKER_ENTERPRISE]}
        if len([c for c in calls if c[1].endswith("/rollback")]) == 1:
            raise ManagerDeploymentInProgressError("HTTP 409: busy", active_deployment={"operation": "deploy", "actor": "ci"})
        return {"warnings": [], "stdout": "rolled back", "stderr": ""}

    monkeypatch.setattr(client, "_request_json", fake_request)

    payload = client.rollback_stack(stack="web", namespace="team-a", version="3", on_wait=notices.append)

    assert payload["stdout"] == "rolled back"
    assert [c for c in calls if c[1].endswith("/rollback")] == [
        ("POST", "/api/stacks/web/rollback", {"namespace": "team-a", "version": "3"}),
        ("POST", "/api/stacks/web/rollback", {"namespace": "team-a", "version": "3"}),
    ]
    assert len(notices) == 1 and notices[0]["actor"] == "ci"


# --- forcing past another run -------------------------------------------------


def _force_client(monkeypatch):
    client = _sse_client(monkeypatch)
    monkeypatch.setenv("DOCKER_MANAGER_DEPLOY_WAIT_SECS", "300")
    return client


def _busy(active=None):
    from docker_stack.manager_api import ManagerDeploymentInProgressError

    return ManagerDeploymentInProgressError(
        "HTTP 409: busy",
        active_deployment=active or {"operation": "deploy", "actor": "alice", "deployment_id": "dep-1", "started_at_ms": 0},
    )


def _stream_after_busy(attempts, *, busy_until: int, active=None):
    def fake_events(path, *, method="GET", payload=None, timeout_secs=None):
        attempts.append(path)
        if len(attempts) <= busy_until:
            raise _busy(active)
        yield {"event": "done", "data": {"warnings": [], "stdout": "ok", "stderr": ""}}

    return fake_events


def test_abort_active_deployment_posts_stack_and_namespace_with_long_timeout(monkeypatch):
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)
    calls = []

    def fake_request(path, *, method="GET", payload=None, timeout_secs=None):
        calls.append((method, path, payload, timeout_secs))
        return {"aborted": {"operation": "deploy", "actor": "alice"}, "released": True}

    monkeypatch.setattr(client, "_request_json", fake_request)

    outcome = client.abort_active_deployment(stack="web", namespace="team-a")

    assert outcome == {"aborted": {"operation": "deploy", "actor": "alice"}, "released": True}
    assert calls == [("POST", "/api/stacks/deploy/abort", {"namespace": "team-a", "stack": "web"}, 45)]


def test_abort_active_deployment_returns_none_when_nothing_runs(monkeypatch):
    import io
    import urllib.error

    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)

    def urlopen(request, **_kwargs):
        body = b'{"code":"no_active_deployment","message":"no deployment is running for this stack"}'
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(body))

    monkeypatch.setattr("docker_stack.manager_api.ensure_shell_access", lambda: None)
    monkeypatch.setattr("docker_stack.manager_api._docker_config_headers", dict)
    monkeypatch.setattr("docker_stack.manager_api.urllib.request.urlopen", urlopen)

    assert client.abort_active_deployment(stack="web", namespace="default") is None


def test_abort_forbidden_is_a_typed_error(monkeypatch):
    import io
    import urllib.error

    from docker_stack.manager_api import ManagerAbortForbiddenError

    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)

    def urlopen(request, **_kwargs):
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"message":"stack deploy permission required"}')
        )

    monkeypatch.setattr("docker_stack.manager_api.ensure_shell_access", lambda: None)
    monkeypatch.setattr("docker_stack.manager_api._docker_config_headers", dict)
    monkeypatch.setattr("docker_stack.manager_api.urllib.request.urlopen", urlopen)

    with pytest.raises(ManagerAbortForbiddenError, match="stack deploy permission required"):
        client.abort_active_deployment(stack="web", namespace="default")


def test_force_aborts_the_holder_and_deploys(monkeypatch):
    client = _force_client(monkeypatch)
    attempts, aborts, events = [], [], []
    monkeypatch.setattr(client, "_request_sse_events", _stream_after_busy(attempts, busy_until=1))

    def abort(*, stack, namespace):
        aborts.append((stack, namespace))
        return {"aborted": {"operation": "deploy", "actor": "alice", "deployment_id": "dep-1", "started_at_ms": 0}, "released": True}

    monkeypatch.setattr(client, "abort_active_deployment", abort)

    payload = client.deploy_stack_stream(
        stack="web",
        namespace="team-a",
        compose="services: {}",
        options={},
        on_event=events.append,
        wait_for_poll=lambda active, secs: "force",
    )

    assert payload["stdout"] == "ok"
    assert aborts == [("web", "team-a")]
    assert attempts == ["/api/stacks/deploy/stream"] * 2
    phases = [e["data"]["phase"] for e in events if e["event"] == "waiting"]
    assert phases == ["waiting", "forcing", "aborted"]


def test_force_reports_when_a_different_run_was_aborted(monkeypatch):
    client = _force_client(monkeypatch)
    attempts, events = [], []
    monkeypatch.setattr(client, "_request_sse_events", _stream_after_busy(attempts, busy_until=1))
    monkeypatch.setattr(
        client,
        "abort_active_deployment",
        lambda **_kw: {"aborted": {"operation": "image deploy", "actor": "ci", "deployment_id": None, "started_at_ms": 5}, "released": True},
    )

    client.deploy_stack_stream(
        stack="web", namespace="default", compose="services: {}", options={}, on_event=events.append, wait_for_poll=lambda a, s: "force"
    )

    phases = [e["data"]["phase"] for e in events if e["event"] == "waiting"]
    assert phases == ["waiting", "forcing", "aborted_other"]
    assert [e["data"]["actor"] for e in events if e["data"].get("phase") == "aborted_other"] == ["ci"]


def test_force_when_holder_already_finished_retries_immediately(monkeypatch):
    client = _force_client(monkeypatch)
    attempts, events = [], []
    monkeypatch.setattr(client, "_request_sse_events", _stream_after_busy(attempts, busy_until=1))
    monkeypatch.setattr(client, "abort_active_deployment", lambda **_kw: None)

    client.deploy_stack_stream(
        stack="web", namespace="default", compose="services: {}", options={}, on_event=events.append, wait_for_poll=lambda a, s: "force"
    )

    assert [e["data"]["phase"] for e in events if e["event"] == "waiting"] == ["waiting", "forcing", "free"]
    assert len(attempts) == 2


def test_force_keeps_waiting_when_holder_does_not_release(monkeypatch):
    client = _force_client(monkeypatch)
    attempts, aborts, events = [], [], []
    monkeypatch.setattr(client, "_request_sse_events", _stream_after_busy(attempts, busy_until=3))

    def abort(**_kw):
        aborts.append(1)
        return {"aborted": {"operation": "image update", "actor": "ci"}, "released": False}

    monkeypatch.setattr(client, "abort_active_deployment", abort)

    client.deploy_stack_stream(
        stack="web", namespace="default", compose="services: {}", options={}, on_event=events.append, wait_for_poll=lambda a, s: "force"
    )

    assert aborts == [1], "force is one-shot per run"
    assert len(attempts) == 4
    phases = [e["data"]["phase"] for e in events if e["event"] == "waiting"]
    assert phases == ["waiting", "forcing", "not_released", "force_used"]


def test_force_without_abort_permission_keeps_waiting(monkeypatch):
    from docker_stack.manager_api import ManagerAbortForbiddenError

    client = _force_client(monkeypatch)
    attempts, aborts, events = [], [], []
    monkeypatch.setattr(client, "_request_sse_events", _stream_after_busy(attempts, busy_until=2))

    def abort(**_kw):
        aborts.append(1)
        raise ManagerAbortForbiddenError("HTTP 403: stack deploy permission required")

    monkeypatch.setattr(client, "abort_active_deployment", abort)

    payload = client.deploy_stack_stream(
        stack="web", namespace="default", compose="services: {}", options={}, on_event=events.append, wait_for_poll=lambda a, s: "force"
    )

    assert payload["stdout"] == "ok"
    assert aborts == [1]
    assert [e["data"]["phase"] for e in events if e["event"] == "waiting"] == ["waiting", "forcing", "forbidden", "force_used"]


def test_force_when_abort_itself_reports_busy_keeps_waiting(monkeypatch):
    client = _force_client(monkeypatch)
    attempts, events = [], []
    monkeypatch.setattr(client, "_request_sse_events", _stream_after_busy(attempts, busy_until=2))

    def abort(**_kw):
        raise _busy({"operation": "abort", "actor": "bob", "started_at_ms": 9})

    monkeypatch.setattr(client, "abort_active_deployment", abort)

    client.deploy_stack_stream(
        stack="web", namespace="default", compose="services: {}", options={}, on_event=events.append, wait_for_poll=lambda a, s: "force"
    )

    still_held = [e["data"] for e in events if e["data"].get("phase") == "still_held"]
    assert still_held and still_held[0]["actor"] == "bob"
    assert len(attempts) == 3


def test_force_retry_bypasses_the_wait_budget_once(monkeypatch):
    client = _force_client(monkeypatch)
    monkeypatch.setenv("DOCKER_MANAGER_DEPLOY_WAIT_SECS", "12")
    clock = {"now": 1000.0}
    monkeypatch.setattr("docker_stack.manager_api.time.monotonic", lambda: clock["now"])
    attempts = []

    def fake_events(path, *, method="GET", payload=None, timeout_secs=None):
        attempts.append(path)
        if len(attempts) <= 2:
            raise _busy()
        yield {"event": "done", "data": {"warnings": [], "stdout": "ok", "stderr": ""}}

    monkeypatch.setattr(client, "_request_sse_events", fake_events)

    def abort(**_kw):
        clock["now"] += 30  # the manager waited 30s for the holder to release
        return {"aborted": {"operation": "deploy", "actor": "alice"}, "released": True}

    monkeypatch.setattr(client, "abort_active_deployment", abort)

    def wait_for_poll(active, secs):
        clock["now"] += secs
        return "force"

    payload = client.deploy_stack_stream(stack="web", namespace="default", compose="services: {}", options={}, wait_for_poll=wait_for_poll)

    assert payload["stdout"] == "ok"
    assert len(attempts) == 3


def test_wait_for_poll_without_force_just_polls(monkeypatch):
    client = _force_client(monkeypatch)
    attempts, polls = [], []
    monkeypatch.setattr(client, "_request_sse_events", _stream_after_busy(attempts, busy_until=2))
    monkeypatch.setattr(client, "abort_active_deployment", lambda **_kw: pytest.fail("must not abort"))

    def wait_for_poll(active, secs):
        polls.append((active["actor"], secs))
        return None

    client.deploy_stack_stream(stack="web", namespace="default", compose="services: {}", options={}, wait_for_poll=wait_for_poll)

    assert polls == [("alice", 5), ("alice", 5)]
    assert len(attempts) == 3


def test_rollback_stack_threads_wait_for_poll_and_force(monkeypatch):
    client = _force_client(monkeypatch)
    calls, notices = [], []

    def fake_request(path, *, method="GET", payload=None, timeout_secs=None):
        calls.append((method, path, payload, timeout_secs))
        if path == "/version":
            return {"MesudipFeatures": [FEATURE_MESUDIP_DOCKER_ENTERPRISE]}
        if path == "/api/stacks/deploy/abort":
            return {"aborted": {"operation": "deploy", "actor": "alice", "deployment_id": "dep-1", "started_at_ms": 0}, "released": True}
        if len([c for c in calls if c[1].endswith("/rollback")]) == 1:
            raise _busy()
        return {"warnings": [], "stdout": "rolled back", "stderr": ""}

    monkeypatch.setattr(client, "_request_json", fake_request)

    payload = client.rollback_stack(stack="web", namespace="team-a", version="3", on_wait=notices.append, wait_for_poll=lambda a, s: "force")

    assert payload["stdout"] == "rolled back"
    assert ("POST", "/api/stacks/deploy/abort", {"namespace": "team-a", "stack": "web"}, 45) in calls
    assert [n["phase"] for n in notices] == ["waiting", "forcing", "aborted"]
