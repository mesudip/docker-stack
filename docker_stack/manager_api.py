import json
import os
import shutil
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Set

from docker_stack.login import current_docker_context_target, resolve_login_config

FEATURE_STACK_QUERY = "docker_stack_query_v1"
FEATURE_STACK_DEPLOY = "docker_stack_deploy_v1"
DEFAULT_NAMESPACE = "default"


def _docker_config_headers() -> Dict[str, str]:
    config_root = os.getenv("DOCKER_CONFIG")
    config_path = (
        Path(config_root) / "config.json"
        if config_root
        else Path.home() / ".docker" / "config.json"
    )
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    headers = payload.get("HttpHeaders")
    if not isinstance(headers, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in headers.items()
        if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip()
    }


def _manager_target_from_env() -> Optional[str]:
    manager_url = os.getenv("DOCKER_MANAGER_URL", "").strip()
    if manager_url:
        return manager_url
    docker_host = os.getenv("DOCKER_HOST", "").strip()
    if docker_host.startswith(("tcp://", "http://", "https://")):
        return docker_host
    return None


def _format_control_plane_endpoints(endpoints: Any) -> str:
    if not isinstance(endpoints, list) or not endpoints:
        return "none"
    values = []
    for item in endpoints:
        if not isinstance(item, dict):
            continue
        endpoint_id = item.get("id")
        name = str(item.get("name") or "").strip()
        slug = str(item.get("slug") or "").strip()
        parts = []
        if endpoint_id is not None:
            parts.append(f"id={endpoint_id}")
        if name:
            parts.append(f"name={name}")
        if slug:
            parts.append(f"slug={slug}")
        if parts:
            values.append(", ".join(parts))
    return "; ".join(values) if values else "none"


class ManagerApiClient:
    def __init__(
        self,
        manager_url: str,
        *,
        skip_tls_verify: bool,
        timeout_secs: int = 5,
        default_headers: Optional[Dict[str, str]] = None,
    ):
        self.manager_url = manager_url.rstrip("/")
        self.skip_tls_verify = skip_tls_verify
        self.timeout_secs = max(1, int(timeout_secs))
        self.default_headers = default_headers or {}
        self._features: Optional[Set[str]] = None
        self._endpoint_id: Optional[int] = None
        self._endpoint_id_checked = False
        self._is_manager_backend = False
        self._backend_checked = False

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.manager_url}{path}"
        headers = {**self.default_headers}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, method=method, headers=headers, data=data)
        context = None
        if url.startswith("https://") and self.skip_tls_verify:
            context = ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_secs, context=context) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Manager request failed ({method} {path}): HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Manager request failed ({method} {path}): {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Manager response is not valid JSON ({method} {path})") from exc

    def detect_features(self) -> Set[str]:
        if self._features is not None:
            return set(self._features)
        try:
            payload = self._request_json("/version")
        except RuntimeError:
            self._features = set()
            return set()
        values = payload.get("MesudipFeatures")
        features: Set[str] = set()
        if isinstance(values, list):
            features.update(
                value.strip()
                for value in values
                if isinstance(value, str) and value.strip()
            )
        elif isinstance(values, str):
            features.update(
                value.strip()
                for value in values.split(",")
                if value.strip()
            )
        self._features = features
        return set(features)

    def _detect_manager_backend(self) -> bool:
        if self._backend_checked:
            return self._is_manager_backend
        self._backend_checked = True
        self._is_manager_backend = bool(self.detect_features())
        return self._is_manager_backend

    def _resolve_endpoint_id(self) -> int:
        if self._endpoint_id_checked:
            if self._endpoint_id is None:
                raise RuntimeError("Docker-Manager endpoint id is not available")
            return self._endpoint_id

        self._endpoint_id_checked = True

        payload = self._request_json("/api/endpoints")
        endpoints = payload.get("endpoints")
        raise RuntimeError(
            "docker-stack does not support Docker-Manager control-plane targets. "
            "Point DOCKER_MANAGER_URL at a direct manager stack API instead. "
            f"Visible endpoints: {_format_control_plane_endpoints(endpoints)}"
        )

    def _endpoint_path(self, suffix: str) -> str:
        normalized = suffix if suffix.startswith("/") else f"/{suffix}"
        endpoint_id = self._resolve_endpoint_id()
        return f"/api/endpoints/{endpoint_id}{normalized}"

    def supports(self, feature_name: str) -> bool:
        features = self.detect_features()
        if feature_name not in features:
            return False
        if feature_name == FEATURE_STACK_DEPLOY:
            if not self._detect_manager_backend():
                return False
        return True

    def list_stacks(self) -> Dict[str, Any]:
        return self._request_json(self._endpoint_path("/inventory/stacks"))

    def list_stack_versions(self, stack_name: str, *, namespace: str = DEFAULT_NAMESPACE) -> Dict[str, Any]:
        stack = urllib.parse.quote(stack_name, safe="")
        query = urllib.parse.urlencode({"namespace": namespace})
        return self._request_json(
            f"{self._endpoint_path(f'/inventory/stacks/{stack}/versions')}?{query}"
        )

    def get_stack_compose(
        self,
        stack_name: str,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        version: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> Dict[str, Any]:
        stack = urllib.parse.quote(stack_name, safe="")
        params = {"namespace": namespace}
        if version:
            params["version"] = version
        if tag:
            params["tag"] = tag
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        return self._request_json(
            f"{self._endpoint_path(f'/inventory/stacks/{stack}/compose')}{query}"
        )

    def list_nodes(self) -> Dict[str, Any]:
        payload = self._request_json(self._endpoint_path("/proxy/nodes"))
        if not isinstance(payload, list):
            raise RuntimeError("Docker-Manager nodes response is invalid")

        nodes = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            spec = item.get("Spec") if isinstance(item.get("Spec"), dict) else {}
            status = item.get("Status") if isinstance(item.get("Status"), dict) else {}
            description = item.get("Description") if isinstance(item.get("Description"), dict) else {}
            manager = item.get("ManagerStatus") if isinstance(item.get("ManagerStatus"), dict) else {}
            role = str(spec.get("Role") or "worker")

            manager_status = ""
            if role == "manager":
                if manager.get("Leader") is True:
                    manager_status = "Leader"
                else:
                    reachability = str(manager.get("Reachability") or "").strip()
                    manager_status = reachability.capitalize() if reachability else ""

            nodes.append(
                {
                    "hostname": str(
                        description.get("Hostname")
                        or spec.get("Name")
                        or item.get("ID")
                        or "-"
                    ),
                    "role": role,
                    "manager_status": manager_status,
                    "state": str(status.get("State") or "-").capitalize(),
                    "availability": str(spec.get("Availability") or "-").capitalize(),
                    "address": str(status.get("Addr") or manager.get("Addr") or "-"),
                    "labels": spec.get("Labels") if isinstance(spec.get("Labels"), dict) else {},
                }
            )

        return {"nodes": nodes}

    def validate_stack(
        self,
        *,
        stack: str,
        namespace: str,
        compose: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"stack": stack, "namespace": namespace, "compose": compose}
        if options:
            payload["options"] = options
        return self._request_json(
            "/api/stacks/validate",
            method="POST",
            payload=payload,
        )

    def deploy_stack(
        self,
        *,
        stack: str,
        namespace: str,
        compose: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"stack": stack, "namespace": namespace, "compose": compose}
        if options:
            payload["options"] = options
        return self._request_json(
            "/api/stacks/deploy",
            method="POST",
            payload=payload,
        )

    def rollback_stack(self, *, stack: str, namespace: str, version: str) -> Dict[str, Any]:
        quoted_stack = urllib.parse.quote(stack, safe="")
        return self._request_json(
            self._endpoint_path(f"/inventory/stacks/{quoted_stack}/rollback"),
            method="POST",
            payload={"namespace": namespace, "version": version},
        )


def discover_manager_client(timeout_secs: int = 5) -> Optional[ManagerApiClient]:
    try:
        target = _manager_target_from_env()
        if target:
            config = resolve_login_config(manager_target=target)
        elif shutil.which("docker"):
            _, context_target = current_docker_context_target()
            if not context_target or not context_target.startswith(("tcp://", "http://", "https://")):
                return None
            config = resolve_login_config(manager_target=context_target)
        else:
            return None
    except Exception:
        return None

    return ManagerApiClient(
        config.manager_url,
        skip_tls_verify=config.skip_tls_verify,
        timeout_secs=timeout_secs,
        default_headers=_docker_config_headers(),
    )
