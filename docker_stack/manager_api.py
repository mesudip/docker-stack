import json
import os
import shutil
import socket
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Set

import yaml

from docker_stack.login import current_docker_context_target, resolve_login_config
from docker_stack.shell_auth import ensure_shell_access

FEATURE_MESUDIP_DOCKER_ENTERPRISE = "mesudip-docker-enterprise-v1"
FEATURE_STACK_QUERY = "docker_stack_query_v1"
FEATURE_STACK_DEPLOY = "docker_stack_deploy_v1"
FEATURE_CLUSTER_CONTAINER_CLI = "cluster_container_cli_v1"
DEFAULT_NAMESPACE = "default"


def _docker_config_headers() -> Dict[str, str]:
    payload = _read_docker_config()
    headers = payload.get("HttpHeaders")
    if not isinstance(headers, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in headers.items()
        if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip()
    }


def _docker_config_path() -> Path:
    config_root = os.getenv("DOCKER_CONFIG")
    return Path(config_root) / "config.json" if config_root else Path.home() / ".docker" / "config.json"


def _read_docker_config() -> Dict[str, Any]:
    config_path = _docker_config_path()
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _credential_helper_auth(server: str, helper: str) -> Optional[Dict[str, str]]:
    helper = helper.strip()
    if not server.strip() or not helper:
        return None
    executable = f"docker-credential-{helper}"
    if shutil.which(executable) is None:
        return None
    try:
        result = subprocess.run(
            [executable, "get"],
            input=server,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    username = str(payload.get("Username") or "").strip()
    secret = str(payload.get("Secret") or "").strip()
    if not secret:
        return None
    auth: Dict[str, str] = {"serveraddress": server}
    if username == "<token>":
        auth["identitytoken"] = secret
    elif username:
        auth["username"] = username
        auth["password"] = secret
    else:
        return None
    return auth


def _docker_config_registry_auths(registries: Set[str]) -> Dict[str, Dict[str, Any]]:
    payload = _read_docker_config()
    auths: Dict[str, Dict[str, Any]] = {}
    raw_auths = payload.get("auths")
    if isinstance(raw_auths, dict):
        for server, auth in raw_auths.items():
            if isinstance(server, str) and isinstance(auth, dict) and _registry_auth_matches(server, registries):
                auths[server] = {str(key): value for key, value in auth.items() if isinstance(key, str) and value is not None}

    cred_helpers = payload.get("credHelpers")
    if isinstance(cred_helpers, dict):
        for server, helper in cred_helpers.items():
            if not isinstance(server, str) or not isinstance(helper, str):
                continue
            if not _registry_auth_matches(server, registries):
                continue
            helper_auth = _credential_helper_auth(server, helper)
            if helper_auth:
                auths[server] = helper_auth

    creds_store = payload.get("credsStore")
    if isinstance(creds_store, str) and creds_store.strip():
        for registry in sorted(registries):
            for server in _docker_auth_server_candidates(registry):
                if _has_registry_auth_material(auths.get(server)):
                    continue
                helper_auth = _credential_helper_auth(server, creds_store)
                if helper_auth:
                    auths[server] = helper_auth
                    break

    return {server: auth for server, auth in auths.items() if _has_registry_auth_material(auth)}


def _has_registry_auth_material(auth: Optional[Dict[str, Any]]) -> bool:
    if not auth:
        return False
    return bool(auth.get("auth") or auth.get("identitytoken") or (auth.get("username") and auth.get("password")))


def _image_registry(image: str) -> str:
    image = image.split("@", 1)[0]
    first = image.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return first
    return "docker.io"


def _compose_image_registries(compose: str) -> Set[str]:
    try:
        payload = yaml.safe_load(compose) or {}
    except yaml.YAMLError:
        return set()
    if not isinstance(payload, dict):
        return set()
    services = payload.get("services")
    if not isinstance(services, dict):
        return set()
    registries: Set[str] = set()
    for service in services.values():
        if not isinstance(service, dict):
            continue
        image = service.get("image")
        if isinstance(image, str) and image.strip():
            registries.add(_image_registry(image.strip()))
    return registries


def _docker_auth_server_candidates(registry: str) -> Set[str]:
    if registry == "docker.io":
        return {"docker.io", "registry-1.docker.io", "https://index.docker.io/v1/"}
    return {registry, f"https://{registry}", f"http://{registry}"}


def _registry_auth_matches(server: str, registries: Set[str]) -> bool:
    return any(server in _docker_auth_server_candidates(registry) for registry in registries)


def _with_registry_auth_options(options: Optional[Dict[str, Any]], compose: str) -> Dict[str, Any]:
    prepared = dict(options or {})
    if prepared.get("with_registry_auth"):
        registries = _compose_image_registries(compose)
        registry_auth = _docker_config_registry_auths(registries)
        if registry_auth:
            prepared["registry_auth"] = registry_auth
    return prepared


def _public_deploy_options(options: Optional[Dict[str, Any]], compose: str) -> Optional[Dict[str, Any]]:
    prepared = _with_registry_auth_options(options, compose)
    if not prepared:
        return {}
    return prepared


def _public_image_deploy_options(options: Optional[Dict[str, Any]], images: Dict[str, str]) -> Optional[Dict[str, Any]]:
    prepared = dict(options or {})
    if prepared.get("with_registry_auth"):
        registries = {
            _image_registry(image.strip())
            for image in images.values()
            if isinstance(image, str) and image.strip()
        }
        registry_auth = _docker_config_registry_auths(registries)
        if registry_auth:
            prepared["registry_auth"] = registry_auth
    return prepared or {}


def _env_timeout_secs(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _manager_deploy_timeout_secs(default: int) -> int:
    return _env_timeout_secs(
        "DOCKER_MANAGER_DEPLOY_TIMEOUT_SECS",
        _env_timeout_secs("DOCKER_MANAGER_API_TIMEOUT_SECS", max(default, 300)),
    )


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

    def _request_headers(self) -> Dict[str, str]:
        ensure_shell_access()
        return {**self.default_headers, **_docker_config_headers()}

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Optional[Dict[str, Any]] = None,
        timeout_secs: Optional[int] = None,
    ) -> Any:
        url = f"{self.manager_url}{path}"
        headers = self._request_headers()
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, method=method, headers=headers, data=data)
        context = None
        if url.startswith("https://") and self.skip_tls_verify:
            context = ssl._create_unverified_context()
        timeout = max(1, int(timeout_secs or self.timeout_secs))
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                payload = exc.read().decode("utf-8", errors="replace").strip()
                if payload:
                    body = f": {payload}"
            except Exception:
                body = ""
            raise RuntimeError(f"Manager request failed ({method} {path}): HTTP {exc.code}{body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Manager request failed ({method} {path}): {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError(f"Manager request failed ({method} {path}): timed out after {timeout}s") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Manager response is not valid JSON ({method} {path})") from exc

    def _request_sse_events(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Optional[Dict[str, Any]] = None,
        timeout_secs: Optional[int] = None,
    ):
        url = f"{self.manager_url}{path}"
        headers = {**self._request_headers(), "Accept": "text/event-stream"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, method=method, headers=headers, data=data)
        context = None
        if url.startswith("https://") and self.skip_tls_verify:
            context = ssl._create_unverified_context()
        timeout = max(1, int(timeout_secs or self.timeout_secs))

        event_name = "message"
        data_lines = []
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        if data_lines:
                            raw_data = "\n".join(data_lines)
                            try:
                                event_data = json.loads(raw_data)
                            except json.JSONDecodeError:
                                event_data = {"raw": raw_data}
                            yield {"event": event_name, "data": event_data}
                        event_name = "message"
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    field, _, value = line.partition(":")
                    value = value[1:] if value.startswith(" ") else value
                    if field == "event":
                        event_name = value or "message"
                    elif field == "data":
                        data_lines.append(value)
                if data_lines:
                    raw_data = "\n".join(data_lines)
                    try:
                        event_data = json.loads(raw_data)
                    except json.JSONDecodeError:
                        event_data = {"raw": raw_data}
                    yield {"event": event_name, "data": event_data}
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                payload = exc.read().decode("utf-8", errors="replace").strip()
                if payload:
                    body = f": {payload}"
            except Exception:
                body = ""
            raise RuntimeError(f"Manager request failed ({method} {path}): HTTP {exc.code}{body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Manager request failed ({method} {path}): {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError(f"Manager request failed ({method} {path}): timed out after {timeout}s") from exc

    def detect_features(self) -> Set[str]:
        if self._features is not None:
            return set(self._features)
        payload = self._request_json("/version")
        values = payload.get("MesudipFeatures")
        features: Set[str] = set()
        if isinstance(values, list):
            features.update(value.strip() for value in values if isinstance(value, str) and value.strip())
        elif isinstance(values, str):
            features.update(value.strip() for value in values.split(",") if value.strip())
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
        # TODO: Support control-plane targets by carrying endpoint id/name in docker-stack requests.
        raise RuntimeError(
            "docker-stack does not support Docker-Manager control-plane targets. "
            "Point DOCKER_MANAGER_URL at a direct manager stack API instead. "
            f"Visible endpoints: {_format_control_plane_endpoints(endpoints)}"
        )

    def _endpoint_path(self, suffix: str) -> str:
        normalized = suffix if suffix.startswith("/") else f"/{suffix}"
        endpoint_id = self._resolve_endpoint_id()
        return f"/api/endpoints/{endpoint_id}{normalized}"

    def is_manager_backend(self) -> bool:
        """True when the target is a Docker-Manager stack API rather than a raw daemon."""
        return self._detect_manager_backend()

    def check_node_agent(self, selector: str) -> Dict[str, Any]:
        """Report whether one node can serve node-local Docker commands.

        Scoped to ``selector`` on purpose: an unrelated node that is drained, down, or
        missing its agent must never make a usable node look unusable.
        """
        node = urllib.parse.quote(selector, safe="")
        payload = self._request_json(f"/api/docker-stack/nodes/{node}/agent")
        if not isinstance(payload, dict):
            raise RuntimeError("Docker-Manager node agent response is invalid")
        return payload

    def supports(self, feature_name: str) -> bool:
        features = self.detect_features()
        if FEATURE_MESUDIP_DOCKER_ENTERPRISE in features and feature_name in {
            FEATURE_STACK_QUERY,
            FEATURE_STACK_DEPLOY,
        }:
            return self._detect_manager_backend()
        if feature_name not in features:
            return False
        if feature_name == FEATURE_STACK_DEPLOY:
            if not self._detect_manager_backend():
                return False
        return True

    def list_stacks(self, *, namespace: Optional[str] = DEFAULT_NAMESPACE) -> Dict[str, Any]:
        query = urllib.parse.urlencode(
            {"namespace": namespace} if namespace is not None else {"all_namespaces": "true"}
        )
        if self._detect_manager_backend():
            return self._request_json(f"/api/docker-stack/stacks?{query}")
        return self._request_json(f"{self._endpoint_path('/inventory/stacks')}?{query}")

    def list_stack_versions(self, stack_name: str, *, namespace: str = DEFAULT_NAMESPACE) -> Dict[str, Any]:
        stack = urllib.parse.quote(stack_name, safe="")
        query = urllib.parse.urlencode({"namespace": namespace})
        if self._detect_manager_backend():
            return self._request_json(f"/api/docker-stack/stacks/{stack}/versions?{query}")
        return self._request_json(f"{self._endpoint_path(f'/inventory/stacks/{stack}/versions')}?{query}")

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
        if self._detect_manager_backend():
            return self._request_json(f"/api/docker-stack/stacks/{stack}/compose{query}")
        return self._request_json(f"{self._endpoint_path(f'/inventory/stacks/{stack}/compose')}{query}")

    def list_nodes(self) -> Dict[str, Any]:
        if self._detect_manager_backend():
            return self._request_json("/api/docker-stack/nodes")
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
                    "id": str(item.get("ID") or ""),
                    "hostname": str(description.get("Hostname") or spec.get("Name") or item.get("ID") or "-"),
                    "role": role,
                    "manager_status": manager_status,
                    "state": str(status.get("State") or "-").capitalize(),
                    "availability": str(spec.get("Availability") or "-").capitalize(),
                    "address": str(status.get("Addr") or manager.get("Addr") or "-"),
                    "labels": spec.get("Labels") if isinstance(spec.get("Labels"), dict) else {},
                }
            )

        return {"nodes": nodes}

    def list_containers(
        self,
        *,
        all_containers: bool = False,
        filters: Optional[list[str]] = None,
        limit: Optional[int] = None,
        latest: bool = False,
        size: bool = False,
    ) -> Dict[str, Any]:
        docker_filters: Dict[str, list[str]] = {}
        for raw_filter in filters or []:
            key, separator, value = raw_filter.partition("=")
            key = key.strip()
            if not key or not separator:
                raise RuntimeError(f"Invalid Docker filter: {raw_filter}")
            docker_filters.setdefault(key, []).append(value)
        params: Dict[str, Any] = {
            "all": "1" if all_containers else "0",
            "size": "1" if size else "0",
        }
        if docker_filters:
            params["filters"] = json.dumps(docker_filters, separators=(",", ":"))
        if limit is not None:
            params["limit"] = str(limit)
        if latest:
            params["latest"] = "1"
        payload = self._request_json(
            f"/api/docker-stack/containers?{urllib.parse.urlencode(params)}"
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("containers"), list):
            raise RuntimeError("Docker-Manager containers response is invalid")
        return payload

    def resolve_config(
        self,
        *,
        stack: str,
        namespace: str,
        name: str,
        content: str,
        labels: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "stack": stack,
            "namespace": namespace,
            "name": name,
            "content": content,
        }
        if labels:
            payload["labels"] = labels
        return self._request_json(
            "/api/configs/resolve",
            method="POST",
            payload=payload,
        )

    def resolve_secret(
        self,
        *,
        stack: str,
        namespace: str,
        name: str,
        content: Optional[str] = None,
        generate: Optional[Dict[str, Any]] = None,
        labels: Optional[Dict[str, str]] = None,
        return_generated_value: bool = False,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "stack": stack,
            "namespace": namespace,
            "name": name,
            "return_generated_value": return_generated_value,
        }
        if content is not None:
            payload["content"] = content
        if generate is not None:
            payload["generate"] = generate
        if labels:
            payload["labels"] = labels
        return self._request_json(
            "/api/secrets/resolve",
            method="POST",
            payload=payload,
        )

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
        prepared_options = _public_deploy_options(options, compose)
        if prepared_options:
            payload["options"] = prepared_options
        return self._request_json(
            "/api/stacks/deploy",
            method="POST",
            payload=payload,
            timeout_secs=_manager_deploy_timeout_secs(self.timeout_secs),
        )

    def deploy_stack_images(
        self,
        *,
        stack: str,
        namespace: str,
        images: Dict[str, str],
        options: Optional[Dict[str, Any]] = None,
        on_event=None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "stack": stack,
            "namespace": namespace,
            "images": images,
        }
        prepared_options = _public_image_deploy_options(options, images)
        if prepared_options:
            payload["options"] = prepared_options

        done: Optional[Dict[str, Any]] = None
        for event in self._request_sse_events(
            "/api/stacks/deploy/images",
            method="POST",
            payload=payload,
            timeout_secs=_manager_deploy_timeout_secs(self.timeout_secs),
        ):
            if on_event:
                on_event(event)
            event_name = event.get("event")
            data = event.get("data")
            if event_name == "error":
                message = data.get("message") if isinstance(data, dict) else str(data)
                raise RuntimeError(message or "manager image deploy stream failed")
            if event_name == "done" and isinstance(data, dict):
                done = data
        return done or {"warnings": [], "stdout": "", "stderr": ""}

    def deploy_stack_stream(
        self,
        *,
        stack: str,
        namespace: str,
        compose: str,
        options: Optional[Dict[str, Any]] = None,
        on_event=None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"stack": stack, "namespace": namespace, "compose": compose}
        prepared_options = _public_deploy_options(options, compose)
        if prepared_options:
            payload["options"] = prepared_options

        done: Optional[Dict[str, Any]] = None
        for event in self._request_sse_events(
            "/api/stacks/deploy/stream",
            method="POST",
            payload=payload,
            timeout_secs=_manager_deploy_timeout_secs(self.timeout_secs),
        ):
            if on_event:
                on_event(event)
            event_name = event.get("event")
            data = event.get("data")
            if event_name == "error":
                message = data.get("message") if isinstance(data, dict) else str(data)
                raise RuntimeError(message or "manager deploy stream failed")
            if event_name == "done" and isinstance(data, dict):
                done = data
        return done or {"warnings": [], "stdout": "", "stderr": ""}

    def rollback_stack(self, *, stack: str, namespace: str, version: str) -> Dict[str, Any]:
        quoted_stack = urllib.parse.quote(stack, safe="")
        if self._detect_manager_backend():
            return self._request_json(
                f"/api/stacks/{quoted_stack}/rollback",
                method="POST",
                payload={"namespace": namespace, "version": version},
            )
        return self._request_json(
            self._endpoint_path(f"/inventory/stacks/{quoted_stack}/rollback"),
            method="POST",
            payload={"namespace": namespace, "version": version},
        )

    def remove_stack(self, *, stack: str, namespace: str = DEFAULT_NAMESPACE) -> Dict[str, Any]:
        quoted_stack = urllib.parse.quote(stack, safe="")
        query = urllib.parse.urlencode({"namespace": namespace})
        if self._detect_manager_backend():
            return self._request_json(
                f"/api/stacks/{quoted_stack}?{query}",
                method="DELETE",
            )
        return self._request_json(
            f"{self._endpoint_path(f'/stacks/{quoted_stack}')}?{query}",
            method="DELETE",
        )


def discover_manager_client(
    timeout_secs: int = 5, *, strict: bool = False
) -> Optional[ManagerApiClient]:
    """Build a client for the manager behind DOCKER_MANAGER_URL or the Docker context.

    ``strict`` raises the concrete reason instead of returning ``None``. Callers that
    fall back to the plain Docker CLI want the quiet form; callers that are about to
    report a failure to the user must not discard why discovery failed.
    """
    try:
        target = _manager_target_from_env()
        if target:
            config = resolve_login_config(manager_target=target)
        elif shutil.which("docker"):
            _, context_target = current_docker_context_target()
            if not context_target or not context_target.startswith(("tcp://", "http://", "https://")):
                if strict:
                    raise RuntimeError(
                        "no Docker-Manager endpoint found: set DOCKER_MANAGER_URL, or select a "
                        f"docker context with a tcp:// endpoint (current target: {context_target or 'none'})"
                    )
                return None
            config = resolve_login_config(manager_target=context_target)
        else:
            if strict:
                raise RuntimeError(
                    "no Docker-Manager endpoint found: DOCKER_MANAGER_URL is unset and the docker CLI is not installed"
                )
            return None
    except RuntimeError:
        if strict:
            raise
        return None
    except Exception as exc:
        if strict:
            raise RuntimeError(f"failed resolving the Docker-Manager endpoint: {exc}") from exc
        return None

    return ManagerApiClient(
        config.manager_url,
        skip_tls_verify=config.skip_tls_verify,
        timeout_secs=timeout_secs,
        default_headers=_docker_config_headers(),
    )
