import pytest

from docker_stack.manager_api import FEATURE_STACK_DEPLOY, ManagerApiClient


def test_supports_stack_deploy_without_endpoint_catalog(monkeypatch):
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)
    calls = []

    def fake_request(path, *, method="GET", payload=None):
        calls.append((method, path, payload))
        if path == "/version":
            return {"MesudipFeatures": [FEATURE_STACK_DEPLOY]}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(client, "_request_json", fake_request)

    assert client.supports(FEATURE_STACK_DEPLOY) is True
    assert calls == [("GET", "/version", None)]


def test_deploy_stack_uses_direct_stack_api(monkeypatch):
    client = ManagerApiClient("https://172.31.0.6:2378", skip_tls_verify=True)
    calls = []

    def fake_request(path, *, method="GET", payload=None):
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
