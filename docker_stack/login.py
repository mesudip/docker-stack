import base64
import json
import os
import secrets
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional

from docker_stack.command_runner import run_command


@dataclass
class DockerManagerLoginConfig:
    manager_url: str
    context_name: str
    timeout_secs: int
    skip_tls_verify: bool = False

    @property
    def docker_context_host(self) -> str:
        host = self.manager_url.removeprefix("http://").removeprefix("https://").removeprefix("tcp://")
        return f"tcp://{host}"

    @property
    def docker_endpoint_spec(self) -> str:
        spec = f"host={self.docker_context_host}"
        if self.manager_url.startswith("https://") and self.skip_tls_verify:
            spec += ",skip-tls-verify=true"
        return spec


@dataclass
class DockerManagerLoginResult:
    access_token: str
    redirect_uri: str
    callback_port: int
    expires_at: Optional[int]


@dataclass
class DockerManagerSetupAuthResult:
    docker_config_dir: Path
    manager_url: str
    context_name: str
    skip_tls_verify: bool
    access_token: str
    expires_at: Optional[int]
    validation_skipped: bool = False


def normalize_loopback_host(url: str) -> str:
    if "127.0.0.1" in url:
        return url.replace("127.0.0.1", "localhost")
    return url


def normalize_manager_target(target: str) -> str:
    target = target.strip()
    if target.startswith("tcp://"):
        target = target[len("tcp://") :]
    return normalize_loopback_host(target)


def _has_explicit_port(parsed: urllib.parse.ParseResult) -> bool:
    try:
        return parsed.port is not None
    except ValueError:
        return False


def _candidate_urls(target: str, *, verify_ssl: bool) -> list[str]:
    normalized_target = normalize_manager_target(target)
    parsed = urllib.parse.urlparse(normalized_target)
    explicit_scheme = parsed.scheme in {"http", "https"}
    explicit_port = _has_explicit_port(parsed)

    if explicit_scheme:
        if verify_ssl and parsed.scheme == "http":
            raise RuntimeError("verify_ssl=true requires an HTTPS manager URL")
        if explicit_port:
            return [normalized_target]
        default_port = 2376 if parsed.scheme == "https" else 2375
        host = parsed.hostname or parsed.netloc or parsed.path
        return [f"{parsed.scheme}://{host}:{default_port}"]

    if ":" in normalized_target.rsplit("]", 1)[-1]:
        return [f"https://{normalized_target}", f"http://{normalized_target}"] if not verify_ssl else [f"https://{normalized_target}"]

    https_candidate = f"https://{normalized_target}:2376"
    if verify_ssl:
        return [https_candidate]
    return [https_candidate, f"http://{normalized_target}:2375"]


def probe_manager_url(url: str, *, timeout: int = 3, verify_tls: bool = True) -> tuple[bool, bool]:
    request = urllib.request.Request(f"{url.rstrip('/')}/_ping", method="GET")
    context = None
    if url.startswith("https://") and not verify_tls:
        context = ssl._create_unverified_context()

    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            response.read(1)
        return True, False
    except urllib.error.HTTPError:
        return True, False
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return False, True
        return False, False
    except ssl.SSLCertVerificationError:
        return False, True


def detect_manager_url(target: str, *, verify_ssl: bool = False) -> tuple[str, bool]:
    normalized_target = normalize_manager_target(target)
    parsed = urllib.parse.urlparse(normalized_target)
    explicit_scheme = parsed.scheme in {"http", "https"}

    candidates = _candidate_urls(normalized_target, verify_ssl=verify_ssl)
    tls_verification_failed = False

    for candidate in candidates:
        success, cert_failed = probe_manager_url(candidate, verify_tls=True)
        if success:
            return candidate, False
        if candidate.startswith("https://") and cert_failed and not verify_ssl:
            insecure_success, _ = probe_manager_url(candidate, verify_tls=False)
            if insecure_success:
                return candidate, True
            tls_verification_failed = True
        elif candidate.startswith("https://") and cert_failed:
            tls_verification_failed = True

    if explicit_scheme:
        raise RuntimeError(f"Unable to reach Docker-Manager endpoint at {normalized_target}")
    if tls_verification_failed:
        if verify_ssl:
            raise RuntimeError(f"Detected TLS endpoint at {normalized_target}, but TLS verification failed")
        raise RuntimeError(f"Detected TLS endpoint at {normalized_target}, but the HTTPS probe still failed")
    if verify_ssl:
        raise RuntimeError(f"Unable to detect a verified HTTPS Docker-Manager endpoint at {normalized_target}")
    raise RuntimeError(f"Unable to detect whether Docker-Manager at {normalized_target} expects HTTP or HTTPS")


def _request_json(url: str, *, timeout: int = 5, verify_tls: bool = True) -> Dict[str, object]:
    request = urllib.request.Request(url, method="GET")
    context = None
    if url.startswith("https://") and not verify_tls:
        context = ssl._create_unverified_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return json.loads(response.read().decode("utf-8"))

def _request_manager_json(
    config: DockerManagerLoginConfig,
    path: str,
    *,
    method: str = "GET",
    payload: Optional[Dict[str, object]] = None,
    timeout: int = 5,
    urlopen: Callable = urllib.request.urlopen,
) -> Dict[str, object]:
    url = f"{config.manager_url.rstrip('/')}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    context = None
    if url.startswith("https://") and config.skip_tls_verify:
        context = ssl._create_unverified_context()
    with urlopen(request, timeout=timeout, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def _docker_env(docker_config_dir: Optional[Path] = None) -> Dict[str, str]:
    env = dict(os.environ)
    if docker_config_dir is not None:
        env["DOCKER_CONFIG"] = str(docker_config_dir)
    return env


def _inspect_docker_context(context_name: str, docker_config_dir: Optional[Path] = None) -> Optional[Dict[str, object]]:
    inspect = subprocess.run(
        ["docker", "context", "inspect", context_name],
        text=True,
        capture_output=True,
        check=False,
        env=_docker_env(docker_config_dir),
    )
    if inspect.returncode != 0:
        return None
    try:
        payload = json.loads(inspect.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or not payload:
        return None
    return payload[0] if isinstance(payload[0], dict) else None


def _docker_context_host(context_payload: Optional[Dict[str, object]]) -> Optional[str]:
    if not isinstance(context_payload, dict):
        return None
    endpoints = context_payload.get("Endpoints") or context_payload.get("endpoints") or {}
    docker_endpoint = {}
    if isinstance(endpoints, dict):
        docker_endpoint = endpoints.get("docker") or endpoints.get("Docker") or {}
    host = docker_endpoint.get("Host") or docker_endpoint.get("host")
    if not isinstance(host, str):
        return None
    host = host.strip()
    if not host.startswith(("tcp://", "http://", "https://")):
        return None
    return normalize_loopback_host(host)


def current_docker_context_target() -> tuple[Optional[str], Optional[str]]:
    show = run_command(
        ["docker", "context", "show"],
        raise_error=False,
        log=False,
        capture_output=True,
    )
    if show.returncode != 0:
        return None, None
    context_name = (show.stdout or "").strip()
    if not context_name:
        return None, None
    return context_name, _docker_context_host(_inspect_docker_context(context_name))


def docker_context_target(context_name: str, docker_config_dir: Optional[Path] = None) -> Optional[str]:
    return _docker_context_host(_inspect_docker_context(context_name, docker_config_dir))


def resolve_login_config(
    *,
    manager_url: Optional[str] = None,
    manager_target: Optional[str] = None,
    context_name: Optional[str] = None,
    timeout_secs: Optional[int] = None,
    verify_ssl: bool = False,
) -> DockerManagerLoginConfig:
    env_manager_value = os.getenv("DOCKER_MANAGER_URL")
    current_context_name = None
    current_context_target = None
    inferred_context_name = None
    if not manager_url and not manager_target and not env_manager_value:
        current_context_name, current_context_target = current_docker_context_target()

    raw_manager_value = manager_url or manager_target or env_manager_value or current_context_target or "http://localhost:8080"
    if manager_target and "://" not in manager_target and ":" not in manager_target:
        context_target = docker_context_target(manager_target)
        if context_target:
            raw_manager_value = context_target
            inferred_context_name = manager_target
    if manager_target:
        resolved_manager_url, skip_tls_verify = detect_manager_url(raw_manager_value, verify_ssl=verify_ssl)
    elif manager_url:
        resolved_manager_url = normalize_loopback_host(raw_manager_value)
        skip_tls_verify = False
        if resolved_manager_url.startswith("https://"):
            _, skip_tls_verify = detect_manager_url(resolved_manager_url, verify_ssl=verify_ssl)
        elif verify_ssl:
            raise RuntimeError("verify_ssl=true requires an HTTPS manager URL")
    elif env_manager_value and "://" not in env_manager_value:
        resolved_manager_url, skip_tls_verify = detect_manager_url(raw_manager_value, verify_ssl=verify_ssl)
    elif current_context_target:
        resolved_manager_url, skip_tls_verify = detect_manager_url(raw_manager_value, verify_ssl=verify_ssl)
    else:
        resolved_manager_url = normalize_loopback_host(raw_manager_value)
        skip_tls_verify = False

    return DockerManagerLoginConfig(
        manager_url=resolved_manager_url,
        context_name=context_name or os.getenv("DOCKER_MANAGER_CONTEXT_NAME") or inferred_context_name or current_context_name or "dm-proxy",
        timeout_secs=int(timeout_secs or os.getenv("DOCKER_MANAGER_LOGIN_TIMEOUT_SECS", "300")),
        skip_tls_verify=skip_tls_verify,
    )


def resolve_context_login_config(
    context_name: str,
    *,
    timeout_secs: Optional[int] = None,
) -> DockerManagerLoginConfig:
    target = docker_context_target(context_name)
    if not target:
        raise RuntimeError(f"Docker context '{context_name}' does not have a TCP/HTTP(S) Docker endpoint")
    resolved_manager_url, skip_tls_verify = detect_manager_url(target)
    return DockerManagerLoginConfig(
        manager_url=resolved_manager_url,
        context_name=context_name,
        timeout_secs=int(timeout_secs or os.getenv("DOCKER_MANAGER_LOGIN_TIMEOUT_SECS", "300")),
        skip_tls_verify=skip_tls_verify,
    )


def resolve_shell_login_config(
    *,
    shell_name: Optional[str],
    manager_target: Optional[str],
    context_name: Optional[str],
    timeout_secs: Optional[int] = None,
) -> DockerManagerLoginConfig:
    if manager_target:
        resolved_context_name = context_name or shell_name
        if not resolved_context_name:
            raise RuntimeError("Pass --context <name> when providing a manager target")
        resolved_manager_url, skip_tls_verify = detect_manager_url(manager_target)
        return DockerManagerLoginConfig(
            manager_url=resolved_manager_url,
            context_name=resolved_context_name,
            timeout_secs=int(timeout_secs or os.getenv("DOCKER_MANAGER_LOGIN_TIMEOUT_SECS", "300")),
            skip_tls_verify=skip_tls_verify,
        )

    resolved_context_name = context_name or shell_name
    if resolved_context_name:
        persisted_target = docker_context_target(
            resolved_context_name,
            isolated_docker_config_dir(resolved_context_name),
        )
        target = persisted_target or docker_context_target(resolved_context_name)
        if not target:
            raise RuntimeError(
                f"Unknown shell context '{resolved_context_name}'. First run "
                f"'docker-stack shell --context {resolved_context_name} <manager-host>'."
            )
        resolved_manager_url, skip_tls_verify = detect_manager_url(target)
        return DockerManagerLoginConfig(
            manager_url=resolved_manager_url,
            context_name=resolved_context_name,
            timeout_secs=int(timeout_secs or os.getenv("DOCKER_MANAGER_LOGIN_TIMEOUT_SECS", "300")),
            skip_tls_verify=skip_tls_verify,
        )

    current_context_name, current_target = current_docker_context_target()
    if current_context_name and current_target:
        resolved_manager_url, skip_tls_verify = detect_manager_url(current_target)
        return DockerManagerLoginConfig(
            manager_url=resolved_manager_url,
            context_name=current_context_name,
            timeout_secs=int(timeout_secs or os.getenv("DOCKER_MANAGER_LOGIN_TIMEOUT_SECS", "300")),
            skip_tls_verify=skip_tls_verify,
        )
    raise RuntimeError("No shell context available. Pass '--context <name> <manager-host>' the first time.")


def find_callback_port() -> int:
    for port in range(8079, 8069, -1):
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind(("127.0.0.1", port))
            probe.close()
            return port
        except OSError:
            continue
    raise RuntimeError("No free callback port found in range 8079..8070")


def build_auth_url(config: DockerManagerLoginConfig, redirect_uri: str, state: str) -> str:
    query = urllib.parse.urlencode({"redirect_uri": redirect_uri, "state": state})
    broker_path = f"/api/auth/cli/login?{query}"
    try:
        payload = _request_manager_json(config, broker_path)
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 400, 501}:
            raise RuntimeError(
                "Login requires the latest Docker-Manager broker endpoint; raw Docker daemon endpoints do not require login"
            ) from exc
        raise RuntimeError(f"Unable to request manager login from {config.manager_url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Unable to request manager login from {config.manager_url}: {exc.reason}") from exc

    auth_url = payload.get("auth_url")
    if not isinstance(auth_url, str) or not auth_url:
        raise RuntimeError("Manager broker response did not include auth_url")
    return auth_url


def exchange_authorization_code(
    config: DockerManagerLoginConfig,
    code: str,
    redirect_uri: str,
    *,
    urlopen: Callable = urllib.request.urlopen,
) -> Dict[str, object]:
    try:
        token_response = _request_manager_json(
            config,
            "/api/auth/cli/exchange",
            method="POST",
            payload={"code": code, "redirect_uri": redirect_uri},
            timeout=config.timeout_secs,
            urlopen=urlopen,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError("Manager rejected the authorization code exchange") from exc
        raise RuntimeError(f"Manager token exchange failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Manager token exchange failed: {exc.reason}") from exc
    if "access_token" not in token_response:
        raise RuntimeError("Token response missing access_token")
    return token_response


def token_exp_from_jwt(token: str) -> Optional[int]:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode((payload + padding).encode("utf-8"))
        value = json.loads(decoded.decode("utf-8"))
    except Exception:
        return None
    exp = value.get("exp")
    return exp if isinstance(exp, int) else None


def token_issuer_from_jwt(token: str) -> Optional[str]:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode((payload + padding).encode("utf-8"))
        value = json.loads(decoded.decode("utf-8"))
    except Exception:
        return None
    issuer = value.get("iss")
    return issuer if isinstance(issuer, str) else None


def format_expiry(expires_at: Optional[int]) -> Optional[str]:
    if not expires_at:
        return None
    remaining = max(0, expires_at - int(time.time()))
    days, rem = divmod(remaining, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def read_docker_config(config_path: Optional[Path] = None) -> Dict[str, object]:
    config_path = config_path or Path.home() / ".docker" / "config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in Docker config: {config_path}") from exc


def extract_docker_config_token(config_path: Optional[Path] = None) -> Optional[str]:
    config = read_docker_config(config_path)
    auth_header = config.get("HttpHeaders", {}).get("Authorization")
    if not isinstance(auth_header, str):
        return None
    auth_header = auth_header.strip()
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.removeprefix("Bearer ").strip()
    return token or None


def token_is_active(
    config: DockerManagerLoginConfig,
    *,
    access_token: Optional[str] = None,
    config_path: Optional[Path] = None,
    urlopen: Callable = urllib.request.urlopen,
) -> bool:
    token = access_token or extract_docker_config_token(config_path)
    if not token:
        return False
    request = urllib.request.Request(
        f"{config.manager_url.rstrip('/')}/api/auth/profile",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    context = None
    if config.manager_url.startswith("https://") and config.skip_tls_verify:
        context = ssl._create_unverified_context()
    try:
        with urlopen(request, timeout=5, context=context) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return False
    return bool(payload.get("authenticated"))


def merge_docker_config_header(access_token: str, config_path: Optional[Path] = None) -> Path:
    config_path = config_path or Path.home() / ".docker" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in Docker config: {config_path}") from exc
    else:
        config = {}

    headers = config.get("HttpHeaders")
    if not isinstance(headers, dict):
        headers = {}
    headers["Authorization"] = f"Bearer {access_token}"
    config["HttpHeaders"] = headers
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config_path


def clear_docker_config_authorization_header(config_path: Optional[Path] = None) -> Path:
    config_path = config_path or Path.home() / ".docker" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in Docker config: {config_path}") from exc
    else:
        config = {}

    headers = config.get("HttpHeaders")
    if isinstance(headers, dict):
        headers = dict(headers)
        headers.pop("Authorization", None)
        if headers:
            config["HttpHeaders"] = headers
        else:
            config.pop("HttpHeaders", None)

    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config_path


def isolated_docker_config_dir(context_name: str) -> Path:
    return Path.home() / ".docker-stack" / "contexts" / context_name


def ensure_isolated_docker_config(config_dir: Path) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"
    if config_path.exists():
        return config_path

    base_config = read_docker_config()
    base_config.pop("currentContext", None)
    headers = base_config.get("HttpHeaders")
    if isinstance(headers, dict):
        headers = dict(headers)
        headers.pop("Authorization", None)
        if headers:
            base_config["HttpHeaders"] = headers
        else:
            base_config.pop("HttpHeaders", None)
    config_path.write_text(json.dumps(base_config, indent=2) + "\n", encoding="utf-8")
    return config_path


def configure_docker_context_in_store(config: DockerManagerLoginConfig, docker_config_dir: Path) -> None:
    env = {**os.environ, "DOCKER_CONFIG": str(docker_config_dir)}
    inspect = subprocess.run(
        ["docker", "context", "inspect", config.context_name],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    base_command = [
        "docker",
        "context",
        "update" if inspect.returncode == 0 else "create",
        config.context_name,
        "--description",
        "Docker-Manager proxy",
        "--docker",
        config.docker_endpoint_spec,
    ]
    result = subprocess.run(base_command, text=True, capture_output=True, check=False, env=env)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "failed to configure docker context").strip())


def ensure_isolated_login(
    config: DockerManagerLoginConfig,
    *,
    docker_config_dir: Optional[Path] = None,
    urlopen: Callable = urllib.request.urlopen,
    browser_opener: Callable[[str], bool] = webbrowser.open,
    port_finder: Callable[[], int] = find_callback_port,
) -> tuple[Path, Optional[DockerManagerLoginResult]]:
    docker_config_dir = docker_config_dir or isolated_docker_config_dir(config.context_name)
    config_path = ensure_isolated_docker_config(docker_config_dir)
    configure_docker_context_in_store(config, docker_config_dir)
    if token_is_active(config, config_path=config_path, urlopen=urlopen):
        return docker_config_dir, None

    result = browser_login(
        config,
        port_finder=port_finder,
        browser_opener=browser_opener,
        urlopen=urlopen,
    )
    merge_docker_config_header(result.access_token, config_path)
    return docker_config_dir, result


def configure_docker_context(config: DockerManagerLoginConfig) -> None:
    inspect_result = run_command(
        ["docker", "context", "inspect", config.context_name],
        raise_error=False,
        log=False,
        capture_output=True,
    )
    if inspect_result.returncode == 0:
        run_command(
            [
                "docker",
                "context",
                "update",
                config.context_name,
                "--description",
                "Docker-Manager proxy",
                "--docker",
                config.docker_endpoint_spec,
            ],
            capture_output=True,
        )
    else:
        run_command(
            [
                "docker",
                "context",
                "create",
                config.context_name,
                "--description",
                "Docker-Manager proxy",
                "--docker",
                config.docker_endpoint_spec,
            ],
            capture_output=True,
        )

    run_command(["docker", "context", "use", config.context_name], capture_output=True)


def is_manager_context(context_name: str, docker_config_dir: Optional[Path] = None) -> bool:
    payload = _inspect_docker_context(context_name, docker_config_dir)
    if not isinstance(payload, dict):
        return False
    metadata = payload.get("Metadata") if isinstance(payload.get("Metadata"), dict) else {}
    description = metadata.get("Description") or payload.get("Description")
    return str(description).strip() == "Docker-Manager proxy"


def switch_docker_context(context_name: str) -> bool:
    run_command(["docker", "context", "use", context_name], capture_output=True)
    manager_context = is_manager_context(context_name)
    if not manager_context:
        clear_docker_config_authorization_header()
    return manager_context


def browser_login(
    config: DockerManagerLoginConfig,
    *,
    port_finder: Callable[[], int] = find_callback_port,
    browser_opener: Callable[[str], bool] = webbrowser.open,
    urlopen: Callable = urllib.request.urlopen,
) -> DockerManagerLoginResult:
    callback_port = port_finder()
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://localhost:{callback_port}/auth/callback"
    auth_url = build_auth_url(config, redirect_uri, state)

    result = {"code": None, "state": None, "error": None}
    callback_event = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            result["code"] = query.get("code", [None])[0]
            result["state"] = query.get("state", [None])[0]
            result["error"] = query.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if result["error"] is not None:
                self.wfile.write(b"<html><body><h2>Login failed.</h2><p>You can close this window.</p></body></html>")
            else:
                self.wfile.write(b"<html><body><h2>Login successful.</h2><p>You can close this window.</p></body></html>")
            callback_event.set()

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", callback_port), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"Open this URL if the browser does not launch:\n{auth_url}")
    browser_opener(auth_url)

    try:
        if not callback_event.wait(timeout=config.timeout_secs):
            raise RuntimeError("Timed out waiting for Keycloak callback")
        if result["error"]:
            raise RuntimeError(f"Keycloak login returned error: {result['error']}")
        if not result["code"]:
            raise RuntimeError("Missing authorization code from callback")
        if result["state"] != state:
            raise RuntimeError("State mismatch in callback")

        token_response = exchange_authorization_code(
            config,
            result["code"],
            redirect_uri,
            urlopen=urlopen,
        )
        access_token = str(token_response["access_token"])
        return DockerManagerLoginResult(
            access_token=access_token,
            redirect_uri=redirect_uri,
            callback_port=callback_port,
            expires_at=token_exp_from_jwt(access_token),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def login(config: DockerManagerLoginConfig) -> DockerManagerLoginResult:
    result = browser_login(config)
    configure_docker_context(config)
    merge_docker_config_header(result.access_token)
    return result


def setup_auth_with_token(
    config: DockerManagerLoginConfig,
    *,
    access_token: str,
    docker_config_dir: Optional[Path] = None,
    skip_validation: bool = False,
    urlopen: Callable = urllib.request.urlopen,
) -> DockerManagerSetupAuthResult:
    token = access_token.strip()
    if not token:
        raise RuntimeError("Access token is required")

    docker_config_dir = docker_config_dir or isolated_docker_config_dir(config.context_name)
    config_path = ensure_isolated_docker_config(docker_config_dir)
    configure_docker_context_in_store(config, docker_config_dir)

    validation_skipped = skip_validation
    if not skip_validation and not token_is_active(
        config,
        access_token=token,
        config_path=config_path,
        urlopen=urlopen,
    ):
        raise RuntimeError("Manager rejected the access token")

    merge_docker_config_header(token, config_path)
    return DockerManagerSetupAuthResult(
        docker_config_dir=docker_config_dir,
        manager_url=config.manager_url,
        context_name=config.context_name,
        skip_tls_verify=config.skip_tls_verify,
        access_token=token,
        expires_at=token_exp_from_jwt(token),
        validation_skipped=validation_skipped,
    )


def setup_auth(
    config: DockerManagerLoginConfig,
    *,
    access_token: Optional[str] = None,
    github_oidc_token: Optional[str] = None,
    docker_config_dir: Optional[Path] = None,
    urlopen: Callable = urllib.request.urlopen,
) -> DockerManagerSetupAuthResult:
    token = (access_token or "").strip()
    if token:
        return setup_auth_with_token(
            config,
            access_token=token,
            docker_config_dir=docker_config_dir,
            skip_validation=False,
            urlopen=urlopen,
        )

    oidc_token = (github_oidc_token or "").strip()
    if oidc_token:
        issuer = token_issuer_from_jwt(oidc_token)
        skip_validation = issuer == "https://token.actions.githubusercontent.com"
        return setup_auth_with_token(
            config,
            access_token=oidc_token,
            docker_config_dir=docker_config_dir,
            skip_validation=skip_validation,
            urlopen=urlopen,
        )

    raise RuntimeError("Provide either an access token or a GitHub OIDC token")
