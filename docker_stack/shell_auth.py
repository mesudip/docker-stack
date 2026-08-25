import hmac
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from docker_stack.login import (
    DOCKER_MANAGER_SKIP_TLS_VERIFY_ENV,
    DockerManagerLoginConfig,
    DockerManagerLoginResult,
    browser_login,
    configure_docker_context_in_store,
    read_docker_config,
    token_exp_from_jwt,
)

PROTOCOL_VERSION = 1
REFRESH_WINDOW_SECS = 60
MAX_REQUEST_BYTES = 1024 * 1024

SHELL_SOCKET_ENV = "DOCKER_STACK_SHELL_SOCKET"
SHELL_SECRET_ENV = "DOCKER_STACK_SHELL_SECRET"
SHELL_CONTEXT_ENV = "DOCKER_STACK_SHELL_CONTEXT"
SHELL_STATUS_ENV = "DOCKER_STACK_SHELL_STATUS"
SHELL_NODE_STATE_ENV = "DOCKER_STACK_SHELL_NODE_STATE"
SHELL_ORIGINAL_ZDOTDIR_ENV = "DOCKER_STACK_SHELL_ORIGINAL_ZDOTDIR"
SHELL_AUTH_TIMEOUT_ENV = "DOCKER_STACK_SHELL_AUTH_TIMEOUT_SECS"


class ShellAuthError(RuntimeError):
    def __init__(self, message: str, *, reason: str = "shell_auth_failed", incident_id: Optional[str] = None):
        self.reason = reason
        self.incident_id = incident_id
        suffix = f" (incident: {incident_id})" if incident_id else ""
        super().__init__(f"{message}{suffix}")

    @property
    def requires_login(self) -> bool:
        return self.reason in {
            "missing_refresh_token",
            "refresh_rejected",
            "refresh_token_expired",
            "broker_unavailable",
        }


def shell_session_active() -> bool:
    return bool(os.getenv(SHELL_SOCKET_ENV, "").strip() and os.getenv(SHELL_SECRET_ENV, "").strip())


def _atomic_json_write(path: Path, payload: Dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, mode)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def update_access_header(config_path: Path, access_token: str) -> None:
    payload = read_docker_config(config_path)
    headers = payload.get("HttpHeaders")
    if not isinstance(headers, dict):
        headers = {}
    headers = dict(headers)
    headers["Authorization"] = f"Bearer {access_token}"
    payload["HttpHeaders"] = headers
    _atomic_json_write(config_path, payload)


def _write_status(path: Path, state: str, expires_at: Optional[int]) -> None:
    # The prompt reads this using shell builtins. It intentionally contains no credentials.
    value = f"{state} {int(expires_at or 0)}\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _peer_is_current_user(connection: socket.socket) -> bool:
    if hasattr(connection, "getpeereid"):
        try:
            peer_uid, _ = connection.getpeereid()
            return peer_uid == os.getuid()
        except OSError:
            return False
    if hasattr(socket, "SO_PEERCRED"):
        try:
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _, peer_uid, _ = struct.unpack("3i", credentials)
            return peer_uid == os.getuid()
        except OSError:
            return False
    return True


def _send_message(connection: socket.socket, payload: Dict[str, Any]) -> None:
    connection.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))


def _read_message(connection: socket.socket) -> Dict[str, Any]:
    chunks = bytearray()
    while len(chunks) <= MAX_REQUEST_BYTES:
        block = connection.recv(min(65536, MAX_REQUEST_BYTES + 1 - len(chunks)))
        if not block:
            break
        chunks.extend(block)
        if b"\n" in block:
            break
    if len(chunks) > MAX_REQUEST_BYTES:
        raise ShellAuthError("shell authentication request is too large", reason="invalid_request")
    raw = bytes(chunks).split(b"\n", 1)[0]
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShellAuthError("shell authentication request is invalid", reason="invalid_request") from exc
    if not isinstance(payload, dict):
        raise ShellAuthError("shell authentication request is invalid", reason="invalid_request")
    return payload


def _refresh_error_from_http(exc: urllib.error.HTTPError) -> ShellAuthError:
    reason = "refresh_failed"
    message = f"Docker-Manager token refresh failed: HTTP {exc.code}"
    incident_id = None
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        if isinstance(payload, dict):
            reason = str(payload.get("reason") or reason)
            message = str(payload.get("message") or message)
            incident_id = str(payload.get("incident_id") or "").strip() or None
    except (json.JSONDecodeError, OSError):
        pass
    return ShellAuthError(message, reason=reason, incident_id=incident_id)


def request_refreshed_tokens(
    manager_url: str,
    refresh_token: str,
    *,
    skip_tls_verify: bool,
    timeout_secs: int,
) -> Dict[str, Any]:
    request = urllib.request.Request(
        f"{manager_url.rstrip('/')}/api/auth/cli/refresh",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"refresh_token": refresh_token}).encode("utf-8"),
    )
    context = None
    if manager_url.startswith("https://") and skip_tls_verify:
        context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=max(1, timeout_secs), context=context) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _refresh_error_from_http(exc) from exc
    except urllib.error.URLError as exc:
        reason_value = exc.reason
        reason_text = str(reason_value)
        lowered = reason_text.lower()
        if isinstance(reason_value, TimeoutError) or "timed out" in lowered:
            reason = "refresh_timeout"
        elif "certificate" in lowered or "tls" in lowered or "ssl" in lowered:
            reason = "refresh_tls_failed"
        elif "name or service" in lowered or "nodename" in lowered or "resolve" in lowered or "resolution" in lowered:
            reason = "refresh_dns_failed"
        else:
            reason = "refresh_connection_failed"
        raise ShellAuthError(f"Docker-Manager token refresh failed: {reason_text}", reason=reason) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ShellAuthError("Docker-Manager token refresh timed out", reason="refresh_timeout") from exc
    except ssl.SSLError as exc:
        raise ShellAuthError(f"Docker-Manager token refresh TLS failure: {exc}", reason="refresh_tls_failed") from exc
    except socket.gaierror as exc:
        raise ShellAuthError(f"Docker-Manager token refresh DNS failure: {exc}", reason="refresh_dns_failed") from exc
    except OSError as exc:
        raise ShellAuthError(f"Docker-Manager token refresh connection failure: {exc}", reason="refresh_connection_failed") from exc
    except json.JSONDecodeError as exc:
        raise ShellAuthError("Docker-Manager token refresh response is invalid", reason="invalid_refresh_response") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
        raise ShellAuthError("Docker-Manager token refresh response is invalid", reason="invalid_refresh_response")
    return payload


class CredentialBroker:
    def __init__(
        self,
        *,
        socket_path: Path,
        status_path: Path,
        config_path: Path,
        manager_url: str,
        skip_tls_verify: bool,
        timeout_secs: int,
        shell_secret: str,
        access_token: str,
        refresh_token: Optional[str],
        expires_at: Optional[int],
        refresh_expires_at: Optional[int],
    ):
        self.socket_path = socket_path
        self.status_path = status_path
        self.config_path = config_path
        self.manager_url = manager_url
        self.skip_tls_verify = skip_tls_verify
        self.timeout_secs = timeout_secs
        self.shell_secret = shell_secret
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at or token_exp_from_jwt(access_token)
        self.refresh_expires_at = refresh_expires_at
        self.stop_event = threading.Event()
        self.condition = threading.Condition()
        self.refreshing = False
        self.refresh_generation = 0
        self.last_refresh_result: Optional[Dict[str, Any]] = None

    def _result_ok(self) -> Dict[str, Any]:
        return {"ok": True, "state": "active", "expires_at": self.expires_at}

    def _refresh_once(self) -> Dict[str, Any]:
        if not self.refresh_token:
            raise ShellAuthError("shell refresh credential is unavailable", reason="missing_refresh_token")
        if self.refresh_expires_at and self.refresh_expires_at <= int(time.time()):
            raise ShellAuthError("shell refresh credential has expired", reason="refresh_token_expired")
        _write_status(self.status_path, "refreshing", self.expires_at)
        payload = request_refreshed_tokens(
            self.manager_url,
            self.refresh_token,
            skip_tls_verify=self.skip_tls_verify,
            timeout_secs=self.timeout_secs,
        )
        access_token = str(payload["access_token"])
        expires_in = payload.get("expires_in")
        expires_at = token_exp_from_jwt(access_token)
        if expires_at is None and isinstance(expires_in, int):
            expires_at = int(time.time()) + max(0, expires_in)
        rotated = payload.get("refresh_token")
        if isinstance(rotated, str) and rotated.strip():
            self.refresh_token = rotated
        refresh_expires_in = payload.get("refresh_expires_in")
        if isinstance(refresh_expires_in, int) and refresh_expires_in > 0:
            self.refresh_expires_at = int(time.time()) + refresh_expires_in
        update_access_header(self.config_path, access_token)
        self.access_token = access_token
        self.expires_at = expires_at
        _write_status(self.status_path, "active", self.expires_at)
        return self._result_ok()

    def ensure_access(self) -> Dict[str, Any]:
        now = int(time.time())
        if self.expires_at and self.expires_at - now > REFRESH_WINDOW_SECS:
            return self._result_ok()
        with self.condition:
            # Another caller may have completed a refresh between the fast-path
            # expiry check above and acquiring the single-flight lock.
            now = int(time.time())
            if self.expires_at and self.expires_at - now > REFRESH_WINDOW_SECS:
                return self._result_ok()
            if self.refreshing:
                generation = self.refresh_generation
                while self.refreshing and generation == self.refresh_generation:
                    self.condition.wait()
                return dict(self.last_refresh_result or {"ok": False, "reason": "refresh_failed", "message": "refresh failed"})
            self.refreshing = True
        try:
            result = self._refresh_once()
        except ShellAuthError as exc:
            _write_status(self.status_path, "error", self.expires_at)
            result = {
                "ok": False,
                "reason": exc.reason,
                "message": str(exc),
                "incident_id": exc.incident_id,
                "requires_login": exc.requires_login,
            }
        except Exception as exc:
            _write_status(self.status_path, "error", self.expires_at)
            result = {
                "ok": False,
                "reason": "broker_refresh_failed",
                "message": f"shell credential refresh failed: {exc}",
                "incident_id": None,
                "requires_login": False,
            }
        finally:
            with self.condition:
                self.last_refresh_result = result
                self.refresh_generation += 1
                self.refreshing = False
                self.condition.notify_all()
        return result

    def replace(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise ShellAuthError("replacement access token is required", reason="invalid_request")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            raise ShellAuthError("replacement refresh token is required", reason="invalid_request")
        expires_at = payload.get("expires_at")
        refresh_expires_at = payload.get("refresh_expires_at")
        with self.condition:
            while self.refreshing:
                self.condition.wait()
            update_access_header(self.config_path, access_token)
            self.access_token = access_token
            self.refresh_token = refresh_token
            self.expires_at = int(expires_at) if isinstance(expires_at, int) else token_exp_from_jwt(access_token)
            self.refresh_expires_at = int(refresh_expires_at) if isinstance(refresh_expires_at, int) else None
        _write_status(self.status_path, "active", self.expires_at)
        return self._result_ok()

    def status(self) -> Dict[str, Any]:
        state = "active"
        now = int(time.time())
        if not self.expires_at or self.expires_at <= now:
            state = "expired"
        elif self.expires_at - now <= REFRESH_WINDOW_SECS:
            state = "refresh_required"
        return {"ok": True, "state": state, "expires_at": self.expires_at}

    def _handle_connection(self, connection: socket.socket) -> None:
        try:
            if not _peer_is_current_user(connection):
                _send_message(connection, {"ok": False, "reason": "unauthorized", "message": "shell authentication rejected"})
                return
            request = _read_message(connection)
            supplied_secret = request.get("secret")
            if not isinstance(supplied_secret, str) or not hmac.compare_digest(supplied_secret, self.shell_secret):
                _send_message(connection, {"ok": False, "reason": "unauthorized", "message": "shell authentication rejected"})
                return
            if request.get("version") != PROTOCOL_VERSION:
                raise ShellAuthError("unsupported shell authentication protocol", reason="unsupported_protocol")
            operation = request.get("operation")
            if operation == "ensure-access":
                response = self.ensure_access()
            elif operation == "replace":
                response = self.replace(request.get("payload") if isinstance(request.get("payload"), dict) else {})
            elif operation == "status":
                response = self.status()
            elif operation == "stop":
                response = {"ok": True, "state": "stopping"}
                self.stop_event.set()
            else:
                raise ShellAuthError("unknown shell authentication operation", reason="invalid_operation")
            _send_message(connection, response)
        except ShellAuthError as exc:
            _send_message(
                connection,
                {
                    "ok": False,
                    "reason": exc.reason,
                    "message": str(exc),
                    "incident_id": exc.incident_id,
                    "requires_login": exc.requires_login,
                },
            )
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            connection.close()

    def serve(self) -> None:
        if self.socket_path.exists():
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(16)
            listener.settimeout(0.25)
            _write_status(self.status_path, self.status()["state"], self.expires_at)
            while not self.stop_event.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self._handle_connection, args=(connection,), daemon=True).start()
        finally:
            stopped_by_request = self.stop_event.is_set()
            self.stop_event.set()
            listener.close()
            if self.socket_path.exists():
                self.socket_path.unlink()
            if not stopped_by_request:
                _write_status(self.status_path, "error", self.expires_at)
            self.access_token = ""
            self.refresh_token = None
            self.shell_secret = ""


def broker_main(socket_path: str, status_path: str, config_path: str) -> int:
    try:
        initial = json.loads(sys.stdin.readline())
        if not isinstance(initial, dict):
            raise ValueError("invalid broker initialization")
        broker = CredentialBroker(
            socket_path=Path(socket_path),
            status_path=Path(status_path),
            config_path=Path(config_path),
            manager_url=str(initial["manager_url"]),
            skip_tls_verify=bool(initial.get("skip_tls_verify")),
            timeout_secs=int(initial.get("timeout_secs") or 30),
            shell_secret=str(initial["shell_secret"]),
            access_token=str(initial["access_token"]),
            refresh_token=str(initial["refresh_token"]) if initial.get("refresh_token") else None,
            expires_at=int(initial["expires_at"]) if initial.get("expires_at") else None,
            refresh_expires_at=int(initial["refresh_expires_at"]) if initial.get("refresh_expires_at") else None,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 2
    broker.serve()
    return 0


def shell_auth_request(
    operation: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    timeout_secs: Optional[int] = None,
    required: bool = False,
    socket_path_override: Optional[str] = None,
    secret_override: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    socket_path = (socket_path_override if socket_path_override is not None else os.getenv(SHELL_SOCKET_ENV, "")).strip()
    secret = (secret_override if secret_override is not None else os.getenv(SHELL_SECRET_ENV, "")).strip()
    if not socket_path or not secret:
        if required:
            raise ShellAuthError("docker-stack shell credential broker is unavailable", reason="broker_unavailable")
        return None
    request = {
        "version": PROTOCOL_VERSION,
        "operation": operation,
        "secret": secret,
        "payload": payload or {},
    }
    if timeout_secs is None:
        try:
            timeout_secs = int(os.getenv(SHELL_AUTH_TIMEOUT_ENV, "305"))
        except ValueError:
            timeout_secs = 305
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(max(1, timeout_secs))
    try:
        connection.connect(socket_path)
        _send_message(connection, request)
        response = _read_message(connection)
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
        raise ShellAuthError(f"docker-stack shell credential broker is unavailable: {exc}", reason="broker_unavailable") from exc
    finally:
        connection.close()
    if not response.get("ok"):
        raise ShellAuthError(
            str(response.get("message") or "shell authentication failed"),
            reason=str(response.get("reason") or "shell_auth_failed"),
            incident_id=str(response.get("incident_id") or "").strip() or None,
        )
    return response


def ensure_shell_access(*, required: bool = False) -> Optional[Dict[str, Any]]:
    return shell_auth_request("ensure-access", required=required)


def replace_shell_credentials(result: DockerManagerLoginResult) -> None:
    if not shell_session_active():
        return
    if not result.refresh_token:
        raise ShellAuthError(
            "Docker-Manager did not return a CLI refresh token; upgrade Docker-Manager",
            reason="missing_refresh_token",
        )
    shell_auth_request(
        "replace",
        {
            "access_token": result.access_token,
            "refresh_token": result.refresh_token,
            "expires_at": result.expires_at,
            "refresh_expires_at": result.refresh_expires_at,
        },
        required=True,
    )


def _wait_for_socket(socket_path: Path, process: subprocess.Popen, timeout_secs: int = 5) -> None:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        if process.poll() is not None:
            raise ShellAuthError("docker-stack shell credential broker failed to start", reason="broker_unavailable")
        time.sleep(0.02)
    raise ShellAuthError("timed out starting docker-stack shell credential broker", reason="broker_unavailable")


def _prompt_context(context_name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in context_name) or "manager"


def _write_shell_wrappers(
    session_dir: Path,
    original_shell: str,
    *,
    context_name: str,
    status_path: Path,
    node_state_path: Optional[Path] = None,
) -> Dict[str, str]:
    context = _prompt_context(context_name)
    status_path_value = str(status_path)
    node_state_path_value = str(node_state_path or status_path.with_name("node-selection"))
    no_color = bool(os.getenv("NO_COLOR"))
    bash_rc = session_dir / "bashrc"
    zsh_dir = session_dir / "zsh"
    zsh_dir.mkdir(mode=0o700)
    original_bashrc = Path.home() / ".bashrc"
    original_zshrc_root = Path(
        os.getenv(SHELL_ORIGINAL_ZDOTDIR_ENV) or os.getenv("ZDOTDIR", str(Path.home()))
    )
    original_zshrc = original_zshrc_root / ".zshrc"
    original_zsh_history = original_zshrc_root / ".zsh_history"
    bash_rc.write_text(
        f'''[ -f {json.dumps(str(original_bashrc))} ] && source {json.dumps(str(original_bashrc))}
__docker_stack_base_ps1="$PS1"
__docker_stack_prompt_update() {{
  local state expires now color reset node
  if [ -n "${{DOCKER_HOST:-}}" ]; then
    color='\\[\\e[31m\\]'; reset='\\[\\e[0m\\]'
    [ {str(no_color).lower()} = true ] && color= && reset=
    PS1="${{color}}(docker:!DOCKER_HOST)${{reset}} ${{__docker_stack_base_ps1}}"
    return
  fi
  read -r state expires < {json.dumps(status_path_value)} 2>/dev/null || state=error
  now=$(date +%s)
  if [ "$state" = active ] && [ "${{expires:-0}}" -gt $((now + {REFRESH_WINDOW_SECS})) ]; then color='\\[\\e[32m\\]';
  elif {{ [ "$state" = active ] && [ "${{expires:-0}}" -gt "$now" ]; }} || [ "$state" = refreshing ] || [ "$state" = refresh_required ]; then color='\\[\\e[33m\\]';
  else color='\\[\\e[31m\\]'; fi
  reset='\\[\\e[0m\\]'
  [ {str(no_color).lower()} = true ] && color= && reset=
  node=$(cat {json.dumps(node_state_path_value)} 2>/dev/null || printf cluster)
  PS1="${{color}}(docker:{context}@${{node}})${{reset}} ${{__docker_stack_base_ps1}}"
}}
case ";${{PROMPT_COMMAND:-}};" in *';__docker_stack_prompt_update;'*) ;; *) PROMPT_COMMAND="__docker_stack_prompt_update${{PROMPT_COMMAND:+;$PROMPT_COMMAND}}";; esac
docker() {{ docker-stack shell-auth ensure || return $?; docker-stack docker -- "$@"; }}
''',
        encoding="utf-8",
    )
    os.chmod(bash_rc, 0o600)
    zsh_rc = zsh_dir / ".zshrc"
    zsh_rc.write_text(
        f'''typeset -g __docker_stack_session_zdotdir="$ZDOTDIR"
typeset -g ZDOTDIR={json.dumps(str(original_zshrc_root))}
typeset -g HISTFILE={json.dumps(str(original_zsh_history))}
typeset -g SHELL_SESSION_HISTORY=0
[ -f {json.dumps(str(original_zshrc))} ] && source {json.dumps(str(original_zshrc))}
[[ -r "$HISTFILE" ]] && fc -R "$HISTFILE"
typeset -g ZDOTDIR="$__docker_stack_session_zdotdir"
unset __docker_stack_session_zdotdir
autoload -Uz add-zsh-hook
typeset -g __docker_stack_base_prompt="$PROMPT"
__docker_stack_prompt_update() {{
  local state expires now color reset node
  if [[ -n "${{DOCKER_HOST:-}}" ]]; then
    color='%F{{red}}'; reset='%f'
    [[ -n "${{NO_COLOR:-}}" ]] && color= && reset=
    PROMPT="${{color}}(docker:!DOCKER_HOST)${{reset}} ${{__docker_stack_base_prompt}}"
    return
  fi
  read -r state expires < {json.dumps(status_path_value)} 2>/dev/null || state=error
  now=$(date +%s)
  if [[ "$state" = active && "${{expires:-0}}" -gt $((now + {REFRESH_WINDOW_SECS})) ]]; then color='%F{{green}}';
  elif [[ ( "$state" = active && "${{expires:-0}}" -gt "$now" ) || "$state" = refreshing || "$state" = refresh_required ]]; then color='%F{{yellow}}';
  else color='%F{{red}}'; fi
  reset='%f'
  [[ -n "${{NO_COLOR:-}}" ]] && color= && reset=
  node=$(cat {json.dumps(node_state_path_value)} 2>/dev/null || printf cluster)
  PROMPT="${{color}}(docker:{context}@${{node}})${{reset}} ${{__docker_stack_base_prompt}}"
}}
add-zsh-hook precmd __docker_stack_prompt_update
docker() {{ docker-stack shell-auth ensure || return $?; docker-stack docker -- "$@"; }}
''',
        encoding="utf-8",
    )
    os.chmod(zsh_rc, 0o600)
    if Path(original_shell).name == "zsh":
        return {"ZDOTDIR": str(zsh_dir)}
    return {}


def _session_source_config_path() -> Optional[Path]:
    configured = os.getenv("DOCKER_CONFIG", "").strip()
    if configured:
        candidate = Path(configured) / "config.json"
        if candidate.exists():
            return candidate
    candidate = Path.home() / ".docker" / "config.json"
    return candidate if candidate.exists() else None


def _create_session_config(session_dir: Path) -> Path:
    source_path = _session_source_config_path()
    payload = read_docker_config(source_path) if source_path else {}
    payload = dict(payload)
    payload.pop("currentContext", None)
    headers = payload.get("HttpHeaders")
    if isinstance(headers, dict):
        headers = dict(headers)
        headers.pop("Authorization", None)
        if headers:
            payload["HttpHeaders"] = headers
        else:
            payload.pop("HttpHeaders", None)
    config_path = session_dir / "config.json"
    _atomic_json_write(config_path, payload)
    return config_path


def run_managed_shell(config: DockerManagerLoginConfig, result: DockerManagerLoginResult) -> int:
    if not result.refresh_token:
        raise ShellAuthError(
            "Docker-Manager did not return a CLI refresh token; upgrade Docker-Manager before using auto-renewing shells",
            reason="missing_refresh_token",
        )
    sessions_root = Path.home() / ".docker-stack" / "sessions"
    sessions_root.mkdir(parents=True, exist_ok=True)
    os.chmod(sessions_root, 0o700)
    session_dir = Path(tempfile.mkdtemp(prefix=f"{_prompt_context(config.context_name)}-", dir=str(sessions_root)))
    os.chmod(session_dir, 0o700)
    broker_process: Optional[subprocess.Popen] = None
    socket_path: Optional[Path] = None
    shell_secret: Optional[str] = None
    supervisor_stopping = threading.Event()
    try:
        config_path = _create_session_config(session_dir)
        configure_docker_context_in_store(config, session_dir)
        update_access_header(config_path, result.access_token)
        socket_path = session_dir / "auth.sock"
        status_path = session_dir / "status"
        node_state_path = session_dir / "node-selection"
        node_state_path.write_text("cluster\n", encoding="utf-8")
        os.chmod(node_state_path, 0o600)
        shell_secret = os.urandom(32).hex()
        broker_env = dict(os.environ)
        for key in (SHELL_SOCKET_ENV, SHELL_SECRET_ENV, SHELL_CONTEXT_ENV, SHELL_STATUS_ENV):
            broker_env.pop(key, None)
        broker_process = subprocess.Popen(
            [sys.executable, "-m", "docker_stack.shell_auth", "broker", str(socket_path), str(status_path), str(config_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            env=broker_env,
        )
        assert broker_process.stdin is not None
        json.dump(
            {
                "manager_url": config.manager_url,
                "skip_tls_verify": config.skip_tls_verify,
                "timeout_secs": config.timeout_secs,
                "shell_secret": shell_secret,
                "access_token": result.access_token,
                "refresh_token": result.refresh_token,
                "expires_at": result.expires_at,
                "refresh_expires_at": result.refresh_expires_at,
            },
            broker_process.stdin,
        )
        broker_process.stdin.write("\n")
        broker_process.stdin.close()
        # The supervisor no longer needs credential values after transferring
        # them through the anonymous pipe. The broker becomes their sole
        # in-memory owner; the access token also exists in the ephemeral config.
        result.access_token = ""
        result.refresh_token = None
        _wait_for_socket(socket_path, broker_process)

        def watch_broker() -> None:
            assert broker_process is not None
            broker_process.wait()
            if not supervisor_stopping.is_set():
                _write_status(status_path, "error", result.expires_at)

        threading.Thread(target=watch_broker, daemon=True).start()
        shell = os.getenv("SHELL", "").strip() or "/bin/bash"
        shell_name = Path(shell).name
        if shell_name not in {"bash", "zsh"}:
            raise ShellAuthError(f"docker-stack shell supports Bash and Zsh, not {shell_name}", reason="unsupported_shell")
        wrapper_env = _write_shell_wrappers(
            session_dir,
            shell,
            context_name=config.context_name,
            status_path=status_path,
            node_state_path=node_state_path,
        )
        env = dict(os.environ)
        env[SHELL_SOCKET_ENV] = str(socket_path)
        env[SHELL_SECRET_ENV] = shell_secret
        env[SHELL_CONTEXT_ENV] = config.context_name
        env[SHELL_STATUS_ENV] = str(status_path)
        env[SHELL_NODE_STATE_ENV] = str(node_state_path)
        env[SHELL_AUTH_TIMEOUT_ENV] = str(max(1, config.timeout_secs) + 5)
        env.update(wrapper_env)
        for key in ("DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_API_VERSION", "DOCKER_MACHINE_NAME"):
            env.pop(key, None)
        env["DOCKER_CONFIG"] = str(session_dir)
        env["DOCKER_CONTEXT"] = config.context_name
        env["DOCKER_MANAGER_URL"] = config.manager_url
        env["DOCKER_MANAGER_CONTEXT_NAME"] = config.context_name
        # The endpoint's TLS mode is already known, so in-shell commands never
        # need to re-probe it (a verified probe against a private CA only
        # produces aborted TLS connections on the manager).
        env[DOCKER_MANAGER_SKIP_TLS_VERIFY_ENV] = "1" if config.skip_tls_verify else "0"
        env[SHELL_ORIGINAL_ZDOTDIR_ENV] = str(
            Path(os.getenv(SHELL_ORIGINAL_ZDOTDIR_ENV) or os.getenv("ZDOTDIR", str(Path.home())))
        )
        command = [shell, "--rcfile", str(session_dir / "bashrc"), "-i"] if shell_name == "bash" else [shell, "-i"]
        return subprocess.run(command, check=False, env=env).returncode
    finally:
        supervisor_stopping.set()
        if broker_process is not None and broker_process.poll() is None:
            try:
                if socket_path is None or shell_secret is None:
                    raise ShellAuthError("broker startup was incomplete", reason="broker_unavailable")
                shell_auth_request("stop", timeout_secs=2, socket_path_override=str(socket_path), secret_override=shell_secret)
            except ShellAuthError:
                broker_process.terminate()
            try:
                broker_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                broker_process.kill()
                broker_process.wait(timeout=2)
        shutil.rmtree(session_dir, ignore_errors=True)


def login_for_active_shell(config: DockerManagerLoginConfig) -> DockerManagerLoginResult:
    result = browser_login(config)
    replace_shell_credentials(result)
    return result


def main(argv=None) -> int:
    argv = list(argv or sys.argv[1:])
    if len(argv) == 4 and argv[0] == "broker":
        return broker_main(argv[1], argv[2], argv[3])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
