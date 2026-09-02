import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
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


# How long a deploy/rollback waits for another run of the same stack to finish
# before giving up. ``0`` fails immediately. Defaults to the deploy timeout.
DEPLOY_WAIT_ENV = "DOCKER_MANAGER_DEPLOY_WAIT_SECS"
DEPLOY_WAIT_POLL_SECS = 5
DEPLOY_WAIT_NOTICE_SECS = 30
# The manager's abort handler itself waits up to 30s for the holder to let go
# of the stack, so the client must give it comfortably more than that.
ABORT_REQUEST_TIMEOUT_SECS = 45


def _manager_deploy_wait_secs(default: int) -> int:
    raw = os.getenv(DEPLOY_WAIT_ENV, "").strip()
    if not raw:
        return _manager_deploy_timeout_secs(default)
    try:
        return max(0, int(raw))
    except ValueError:
        return _manager_deploy_timeout_secs(default)


class ManagerDeploymentInProgressError(RuntimeError):
    """The manager refused an apply because another run holds this stack's deploy guard.

    Docker-Manager answers interactive apply paths (``deploy/stream``, version
    rollback) with ``409 deployment_in_progress`` instead of queueing. The
    ``active_deployment`` payload names the run: ``operation``, ``actor``,
    ``deployment_id``, ``started_at_ms``.
    """

    def __init__(self, message: str, *, active_deployment: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.active_deployment = active_deployment if isinstance(active_deployment, dict) else {}


class ManagerAbortForbiddenError(RuntimeError):
    """The caller may deploy this stack but lacks the permission to abort another run of it."""


class ManagerNoActiveDeploymentError(RuntimeError):
    """An abort found nothing running: the holder finished on its own."""


class ManagerDeployFailedError(RuntimeError):
    """A manager deploy stream ended with an ``error`` event.

    ``services`` carries the per-service outcome when the manager reports a
    partial failure (``code`` = ``deployment_partial_failure``); every parallel
    service operation has completed by then, so this is the full picture.
    """

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        deployment_id: Optional[str] = None,
        rollback_available: bool = False,
        services: Optional[list] = None,
    ):
        super().__init__(message)
        self.code = code
        self.deployment_id = deployment_id
        self.rollback_available = rollback_available
        self.services = services or []


def describe_run(active: Any) -> str:
    """Name a deploy run: who started what, when, and its deployment id if known."""
    if not isinstance(active, dict):
        return "another deployment of this stack"
    operation = str(active.get("operation") or "deployment").strip() or "deployment"
    actor = str(active.get("actor") or "").strip() or "another caller"
    parts = [f"a {operation} started by {actor}"]
    started_at_ms = active.get("started_at_ms")
    if isinstance(started_at_ms, (int, float)) and started_at_ms > 0:
        elapsed = max(0, int(time.time() - started_at_ms / 1000))
        parts.append(f"{elapsed}s ago")
    deployment_id = str(active.get("deployment_id") or "").strip()
    if deployment_id:
        parts.append(f"(deployment {deployment_id})")
    return " ".join(parts)


def describe_active_deployment(active: Any) -> str:
    """One line naming the run that holds a stack: who, what, since when."""
    return describe_run(active) + " is still running"


def same_deploy_run(shown: Any, aborted: Any) -> bool:
    """Whether the run the manager aborted is the one the user was looking at.

    ``deployment_id`` is authoritative when both sides have one; it is ``null``
    for a run that has not persisted its row yet and for non-stream deploys, so
    fall back to who started what and when.
    """
    if not isinstance(shown, dict) or not isinstance(aborted, dict):
        return True
    shown_id = str(shown.get("deployment_id") or "").strip()
    aborted_id = str(aborted.get("deployment_id") or "").strip()
    if shown_id and aborted_id:
        return shown_id == aborted_id
    keys = ("operation", "actor", "started_at_ms")
    return all(shown.get(key) == aborted.get(key) for key in keys)


def format_failed_services(services: Any) -> list:
    """Render the manager's per-service results as one line per failed service."""
    lines = []
    if not isinstance(services, list):
        return lines
    for item in services:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").strip() not in {"failed", "error"}:
            continue
        service = str(item.get("service") or item.get("logical_name") or "service").strip()
        action = str(item.get("action") or "").strip()
        label = f"{service} ({action})" if action else service
        error = str(item.get("error") or "").strip() or "failed without an error message"
        incident = str(item.get("incident_id") or "").strip()
        suffix = f" (incident {incident})" if incident else ""
        lines.append(f"  - {label}: {error}{suffix}")
    return lines


def format_deploy_error_body(body: Dict[str, Any]) -> str:
    """Expand a manager deploy error body into prose, keeping every reported cause."""
    message = str(body.get("message") or "").strip()
    code = str(body.get("code") or "").strip()
    if code == "deployment_in_progress":
        return describe_active_deployment(body.get("active_deployment")) if body.get("active_deployment") else message
    lines = [message] if message else []
    failed = format_failed_services(body.get("services"))
    if failed:
        lines.append("failed services:")
        lines.extend(failed)
    deployment_id = str(body.get("deployment_id") or "").strip()
    if body.get("rollback_available") and deployment_id:
        lines.append(f"rollback is available from the Docker-Manager stack page (deployment {deployment_id})")
    elif deployment_id:
        lines.append(f"deployment {deployment_id}")
    return "\n".join(lines)


def _wait_notifier(on_event):
    """Surface deploy-guard waits through the same event callback as the stream."""
    if on_event is None:
        return None
    return lambda active: on_event({"event": "waiting", "data": active})


ABORT_DEPLOYMENT_PATH = "/api/stacks/deploy/abort"


def _decode_http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _manager_http_error(method: str, path: str, exc: urllib.error.HTTPError) -> RuntimeError:
    """Map an HTTP failure to the most specific error the caller can act on."""
    payload = _decode_http_error_body(exc)
    body: Any = None
    if payload:
        try:
            body = json.loads(payload)
        except json.JSONDecodeError:
            body = None
    if exc.code == 409 and isinstance(body, dict) and body.get("code") == "deployment_in_progress":
        active = body.get("active_deployment")
        return ManagerDeploymentInProgressError(
            f"Manager request failed ({method} {path}): HTTP 409: {describe_active_deployment(active)}",
            active_deployment=active if isinstance(active, dict) else {},
        )
    if exc.code == 404 and isinstance(body, dict) and body.get("code") == "no_active_deployment":
        return ManagerNoActiveDeploymentError(f"Manager request failed ({method} {path}): HTTP 404: no deployment is running")
    if exc.code == 403 and path == ABORT_DEPLOYMENT_PATH:
        detail = str(body.get("message") or "").strip() if isinstance(body, dict) else ""
        return ManagerAbortForbiddenError(
            f"Manager request failed ({method} {path}): HTTP 403: {detail or 'stack deploy permission required'}"
        )
    suffix = f": {payload}" if payload else ""
    return RuntimeError(f"Manager request failed ({method} {path}): HTTP {exc.code}{suffix}")


def _stream_error(data: Any, default: str) -> ManagerDeployFailedError:
    if not isinstance(data, dict):
        message = str(data).strip() if data is not None else ""
        return ManagerDeployFailedError(message or default)
    message = format_deploy_error_body(data) or default
    services = data.get("services")
    return ManagerDeployFailedError(
        message,
        code=str(data.get("code") or "").strip() or None,
        deployment_id=str(data.get("deployment_id") or "").strip() or None,
        rollback_available=bool(data.get("rollback_available")),
        services=services if isinstance(services, list) else [],
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
            raise _manager_http_error(method, path, exc) from exc
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
            raise _manager_http_error(method, path, exc) from exc
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

    def abort_active_deployment(self, *, stack: str, namespace: str) -> Optional[Dict[str, Any]]:
        """Ask the manager to abort whatever holds this stack's deploy guard.

        Returns the manager's ``{"aborted": <run>, "released": bool}`` payload, or
        ``None`` when nothing was running any more. The manager waits up to 30s
        for the holder to release before answering, hence the long timeout.
        """
        try:
            payload = self._request_json(
                ABORT_DEPLOYMENT_PATH,
                method="POST",
                payload={"namespace": namespace, "stack": stack},
                timeout_secs=max(ABORT_REQUEST_TIMEOUT_SECS, int(self.timeout_secs or 0)),
            )
        except ManagerNoActiveDeploymentError:
            return None
        if not isinstance(payload, dict):
            raise RuntimeError("Docker-Manager abort response is invalid")
        return payload

    def _force_takeover(self, *, stack: str, namespace: str, shown: Dict[str, Any], notify) -> str:
        """Abort the run holding the stack so ours can go. Returns ``"retry"`` or ``"wait"``.

        A daemon request the aborted run already made still completes; abort stops
        its orchestration, and our deploy then applies over whatever state that left.
        """
        notify("forcing", shown)
        try:
            outcome = self.abort_active_deployment(stack=stack, namespace=namespace)
        except ManagerAbortForbiddenError:
            notify("forbidden", shown)
            return "wait"
        except ManagerDeploymentInProgressError as exc:
            notify("still_held", exc.active_deployment or shown)
            return "wait"
        except KeyboardInterrupt:
            notify("interrupted", shown)
            raise
        if outcome is None:
            notify("free", shown)
            return "retry"
        aborted = outcome.get("aborted")
        aborted = aborted if isinstance(aborted, dict) else shown
        if not outcome.get("released"):
            notify("not_released", aborted)
            return "wait"
        notify("aborted" if same_deploy_run(shown, aborted) else "aborted_other", aborted)
        return "retry"

    def _run_when_stack_is_free(
        self,
        operation,
        *,
        stack: Optional[str] = None,
        namespace: Optional[str] = None,
        on_wait=None,
        wait_for_poll=None,
    ):
        """Run ``operation``, waiting out another deploy of the same stack.

        The manager's interactive apply paths answer ``409 deployment_in_progress``
        rather than queueing, because a person in the UI can choose to wait or
        abort. Nobody is there to choose on a CLI or CI run, so queue here: retry
        until the guard frees or ``DOCKER_MANAGER_DEPLOY_WAIT_SECS`` runs out,
        and tell the caller what is being waited on. A budget of ``0`` fails
        on the first collision.

        ``wait_for_poll(active, seconds)`` replaces the sleep between polls for a
        caller that can listen to a person; returning ``"force"`` aborts the
        running deployment (once per run) so this one can take the stack.
        ``on_wait`` receives the active run plus a ``phase`` for every notice.
        """
        budget = _manager_deploy_wait_secs(self.timeout_secs)
        started = time.monotonic()
        last_notice: Optional[float] = None
        force_used = False
        force_used_notified = False
        skip_budget_check = False

        def notify(phase: str, active: Any) -> None:
            if on_wait:
                base = active if isinstance(active, dict) else {}
                on_wait({**base, "phase": phase})

        while True:
            try:
                return operation()
            except ManagerDeploymentInProgressError as exc:
                waited = int(time.monotonic() - started)
                if skip_budget_check:
                    skip_budget_check = False
                elif waited + DEPLOY_WAIT_POLL_SECS > budget:
                    if budget > 0:
                        raise ManagerDeploymentInProgressError(
                            f"{exc}; gave up after waiting {waited}s "
                            f"(raise {DEPLOY_WAIT_ENV} to wait longer, or abort it from the Docker-Manager stack page)",
                            active_deployment=exc.active_deployment,
                        ) from exc
                    raise
                now = time.monotonic()
                if on_wait and (last_notice is None or now - last_notice >= DEPLOY_WAIT_NOTICE_SECS):
                    on_wait(
                        {**exc.active_deployment, "phase": "waiting", "waited_secs": waited, "wait_budget_secs": budget}
                    )
                    last_notice = now
                if wait_for_poll is None:
                    time.sleep(DEPLOY_WAIT_POLL_SECS)
                    continue
                decision = wait_for_poll(exc.active_deployment, DEPLOY_WAIT_POLL_SECS)
                if decision != "force":
                    continue
                if force_used:
                    if not force_used_notified:
                        notify("force_used", exc.active_deployment)
                        force_used_notified = True
                    continue
                force_used = True
                if stack is None or namespace is None:
                    raise RuntimeError("cannot force a deploy without knowing its stack and namespace")
                if self._force_takeover(stack=stack, namespace=namespace, shown=exc.active_deployment, notify=notify) == "retry":
                    skip_budget_check = True

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
        wait_for_poll=None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "stack": stack,
            "namespace": namespace,
            "images": images,
        }
        prepared_options = _public_image_deploy_options(options, images)
        if prepared_options:
            payload["options"] = prepared_options

        return self._run_when_stack_is_free(
            lambda: self._consume_deploy_stream(
                "/api/stacks/deploy/images",
                payload,
                on_event=on_event,
                default_error="manager image deploy stream failed",
            ),
            stack=stack,
            namespace=namespace,
            on_wait=_wait_notifier(on_event),
            wait_for_poll=wait_for_poll,
        )

    def deploy_stack_stream(
        self,
        *,
        stack: str,
        namespace: str,
        compose: str,
        options: Optional[Dict[str, Any]] = None,
        on_event=None,
        wait_for_poll=None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"stack": stack, "namespace": namespace, "compose": compose}
        prepared_options = _public_deploy_options(options, compose)
        if prepared_options:
            payload["options"] = prepared_options

        return self._run_when_stack_is_free(
            lambda: self._consume_deploy_stream(
                "/api/stacks/deploy/stream",
                payload,
                on_event=on_event,
                default_error="manager deploy stream failed",
            ),
            stack=stack,
            namespace=namespace,
            on_wait=_wait_notifier(on_event),
            wait_for_poll=wait_for_poll,
        )

    def _consume_deploy_stream(
        self,
        path: str,
        payload: Dict[str, Any],
        *,
        on_event,
        default_error: str,
    ) -> Dict[str, Any]:
        done: Optional[Dict[str, Any]] = None
        for event in self._request_sse_events(
            path,
            method="POST",
            payload=payload,
            timeout_secs=_manager_deploy_timeout_secs(self.timeout_secs),
        ):
            if on_event:
                on_event(event)
            event_name = event.get("event")
            data = event.get("data")
            if event_name == "error":
                raise _stream_error(data, default_error)
            if event_name == "done" and isinstance(data, dict):
                done = data
        return done or {"warnings": [], "stdout": "", "stderr": ""}

    def rollback_stack(
        self,
        *,
        stack: str,
        namespace: str,
        version: str,
        on_wait=None,
        wait_for_poll=None,
    ) -> Dict[str, Any]:
        quoted_stack = urllib.parse.quote(stack, safe="")
        if self._detect_manager_backend():
            return self._run_when_stack_is_free(
                lambda: self._request_json(
                    f"/api/stacks/{quoted_stack}/rollback",
                    method="POST",
                    payload={"namespace": namespace, "version": version},
                ),
                stack=stack,
                namespace=namespace,
                on_wait=on_wait,
                wait_for_poll=wait_for_poll,
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
