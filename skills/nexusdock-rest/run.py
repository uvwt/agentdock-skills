#!/usr/bin/env python3
"""Portable NexusDock REST helper. Uses only the Python standard library."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_BASE_URL = "https://mcp.nexusdock.co"
DEFAULT_TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
OAUTH_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
OAUTH_SCOPE = "integrations"
OAUTH_CLIENT_NAME = "NexusDock REST Skill"
OAUTH_STATE_FILE = "oauth.json"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "unknown"}
EXECUTION_STATUSES = {"queued", "running", *TERMINAL_STATUSES}
CAPABILITY_TYPES = {"tool", "skill", "plugin", "mcp"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class _NoRedirect(HTTPRedirectHandler):
    # API calls must never carry a Bearer credential across a redirect boundary.
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class SkillError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        http_status: int | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.details = details


@dataclass(frozen=True)
class APIResponse:
    status: int
    headers: dict[str, str]
    payload: Any


def emit(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return exit_code


def load_input() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SkillError(f"stdin must contain valid JSON: {exc.msg}", code="invalid_input") from exc
    if not isinstance(payload, dict):
        raise SkillError("input must be a JSON object", code="invalid_input")
    return payload


def normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SkillError("NEXUSDOCK_BASE_URL must be an absolute HTTP(S) URL", code="invalid_base_url")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SkillError(
            "NEXUSDOCK_BASE_URL must not contain credentials, query, or fragment",
            code="invalid_base_url",
        )
    if parsed.scheme != "https" and parsed.hostname.lower() not in LOOPBACK_HOSTS:
        raise SkillError("NEXUSDOCK_BASE_URL must use HTTPS except on loopback", code="insecure_base_url")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def resolve_base_url(data: dict[str, Any]) -> str:
    value = data.get("base_url") or os.environ.get("NEXUSDOCK_BASE_URL") or DEFAULT_BASE_URL
    if not isinstance(value, str) or not value.strip():
        raise SkillError("NEXUSDOCK_BASE_URL is not configured", code="missing_base_url")
    return normalize_base_url(value)


def static_token() -> str | None:
    token = os.environ.get("NEXUSDOCK_TOKEN", "").strip()
    if not token:
        return None
    if any(ch.isspace() for ch in token):
        raise SkillError("NEXUSDOCK_TOKEN is malformed", code="invalid_token_configuration")
    return token


def timeout_seconds(data: dict[str, Any], default: int = DEFAULT_TIMEOUT_SECONDS) -> int:
    value = data.get("timeout_seconds", default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 120:
        raise SkillError("timeout_seconds must be an integer between 1 and 120", code="invalid_input")
    return value


def require_text(data: dict[str, Any], name: str, maximum: int = 512) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SkillError(
            f"{name} is required and must be a non-empty string no longer than {maximum} characters",
            code="invalid_input",
            details={"field": name},
        )
    return value.strip()


def require_object(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise SkillError(f"{name} must be a JSON object", code="invalid_input", details={"field": name})
    return value


def integer(
    data: dict[str, Any],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SkillError(
            f"{name} must be an integer between {minimum} and {maximum}",
            code="invalid_input",
            details={"field": name},
        )
    return value


def number(
    data: dict[str, Any],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= float(value) <= maximum:
        raise SkillError(
            f"{name} must be between {minimum} and {maximum}",
            code="invalid_input",
            details={"field": name},
        )
    return float(value)


def clean_error_message(message: str, token: str = "") -> str:
    text = message.replace(token, "<redacted>") if token else message
    if len(text) > 2000:
        text = text[:2000] + "…"
    return text


def oauth_data_dir() -> Path:
    raw = os.environ.get("SKILL_DATA_DIR") or os.environ.get("NEXUSDOCK_STATE_DIR")
    path = Path(raw) if raw else Path.home() / ".nexusdock-rest"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


@contextmanager
def oauth_state_lock():
    """Serialize OAuth token rotation across concurrent Skill invocations."""
    path = oauth_data_dir() / ".oauth.lock"
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def load_oauth_state() -> dict[str, Any]:
    path = oauth_data_dir() / OAUTH_STATE_FILE
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillError("OAuth state is unreadable", code="oauth_state_corrupt") from exc
    if not isinstance(payload, dict):
        raise SkillError("OAuth state is malformed", code="oauth_state_corrupt")
    return payload


def oauth_state_for_base_url(base_url: str) -> dict[str, Any]:
    """Load OAuth state only for this exact NexusDock origin."""
    state = load_oauth_state()
    stored = state.get("base_url")
    if stored == base_url:
        return state
    # OAuth client ids and every token are authorization-server specific. Never carry them
    # across DEV/production or another NexusDock origin.
    state = {"base_url": base_url}
    save_oauth_state(state)
    return state


def save_oauth_state(state: dict[str, Any]) -> None:
    directory = oauth_data_dir()
    path = directory / OAUTH_STATE_FILE
    temporary = directory / f".{OAUTH_STATE_FILE}.{os.getpid()}.tmp"
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SkillError("cannot persist OAuth state", code="oauth_state_write_failed") from exc


def oauth_tokens(state: dict[str, Any]) -> dict[str, Any] | None:
    tokens = state.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    expires_at = tokens.get("expires_at")
    if not isinstance(access, str) or not access or not isinstance(expires_at, (int, float)):
        return None
    return tokens


def oauth_pending(state: dict[str, Any]) -> dict[str, Any] | None:
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return None
    if not isinstance(pending.get("device_code"), str) or not isinstance(pending.get("expires_at"), (int, float)):
        return None
    return pending


def public_oauth_status(state: dict[str, Any]) -> dict[str, Any]:
    tokens = oauth_tokens(state)
    pending = oauth_pending(state)
    now = time.time()
    result: dict[str, Any] = {
        "client_registered": isinstance(state.get("client_id"), str) and bool(state.get("client_id")),
        "authorized": bool(tokens and float(tokens["expires_at"]) > now),
        "refresh_available": bool(tokens and isinstance(tokens.get("refresh_token"), str) and tokens.get("refresh_token")),
    }
    if tokens:
        result["access_expires_at"] = int(float(tokens["expires_at"]))
        if isinstance(tokens.get("sub"), str) and tokens.get("sub"):
            result["subject"] = tokens["sub"]
        if isinstance(tokens.get("scope"), str):
            result["scope"] = tokens["scope"]
    if pending and float(pending["expires_at"]) > now:
        result["pending"] = True
        result["user_code"] = pending.get("user_code")
        result["verification_uri"] = pending.get("verification_uri")
        result["verification_uri_complete"] = pending.get("verification_uri_complete")
        result["expires_at"] = int(float(pending["expires_at"]))
        next_poll = float(pending.get("next_poll_at", 0))
        result["poll_after_seconds"] = max(0, int(next_poll - now + 0.999))
    else:
        result["pending"] = False
    return result


class NexusDockClient:
    def __init__(self, data: dict[str, Any]) -> None:
        self.base_url = resolve_base_url(data)
        self.timeout = timeout_seconds(data)
        self._token: str | None = None
        self._opener = build_opener(_NoRedirect())

    @property
    def token(self) -> str:
        if self._token is None:
            configured = static_token()
            self._token = configured if configured is not None else self._oauth_access_token()
        return self._token

    def _oauth_access_token(self) -> str:
        with oauth_state_lock():
            state = oauth_state_for_base_url(self.base_url)
            tokens = oauth_tokens(state)
            if tokens and float(tokens["expires_at"]) > time.time() + 60:
                return str(tokens["access_token"])
            return self._refresh_oauth_locked(state)

    def _refresh_oauth_locked(self, state: dict[str, Any]) -> str:
        client_id = state.get("client_id")
        tokens = oauth_tokens(state)
        refresh = tokens.get("refresh_token") if tokens else None
        if not isinstance(client_id, str) or not client_id or not isinstance(refresh, str) or not refresh:
            raise SkillError(
                "NexusDock OAuth authorization is required",
                code="oauth_authorization_required",
            )
        try:
            response = self.request_form(
                "/oauth/token",
                {
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh,
                    "resource": self.base_url + "/v1/integrations",
                },
            )
        except SkillError as exc:
            if exc.code in {"invalid_grant", "invalid_client"}:
                state.pop("tokens", None)
                save_oauth_state(state)
                raise SkillError(
                    "NexusDock OAuth authorization expired or was revoked",
                    code="oauth_authorization_required",
                ) from None
            raise
        stored = self._store_token_response(state, response.payload)
        return str(stored["access_token"])

    def _store_token_response(self, state: dict[str, Any], payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise SkillError("OAuth token response is malformed", code="invalid_response")
        access = payload.get("access_token")
        refresh = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        scope = payload.get("scope")
        subject = payload.get("sub")
        if (
            not isinstance(access, str)
            or not access
            or not isinstance(expires_in, int)
            or expires_in <= 0
            or scope != OAUTH_SCOPE
        ):
            raise SkillError("OAuth token response is malformed", code="invalid_response")
        previous = oauth_tokens(state) or {}
        if not isinstance(refresh, str) or not refresh:
            refresh = previous.get("refresh_token")
        tokens: dict[str, Any] = {
            "access_token": access,
            "expires_at": int(time.time()) + expires_in,
            "scope": scope,
        }
        if isinstance(refresh, str) and refresh:
            tokens["refresh_token"] = refresh
        if isinstance(subject, str) and subject:
            tokens["sub"] = subject
        state["tokens"] = tokens
        state.pop("pending", None)
        save_oauth_state(state)
        return tokens

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = self.base_url + path
        if query:
            cleaned = {key: value for key, value in query.items() if value not in (None, "")}
            if cleaned:
                url += "?" + urlencode(cleaned)
        return url

    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> APIResponse:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "NexusDock-REST-Skill",
        }
        token = ""
        if authenticated:
            token = self.token
            request_headers["Authorization"] = "Bearer " + token
        if headers:
            request_headers.update(headers)

        encoded = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        request = Request(
            self._url(path, query),
            data=encoded,
            headers=request_headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.status
                response_headers = dict(response.headers.items())
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise SkillError(
                    "NexusDock API returned an unexpected redirect",
                    code="unexpected_redirect",
                    http_status=exc.code,
                ) from None
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            status = exc.code
            response_headers = dict(exc.headers.items())
            payload = self._decode_json(raw, token)
            self._raise_http(status, payload, response_headers, token)
            raise AssertionError("unreachable")
        except (URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            raise SkillError(
                clean_error_message(f"cannot reach NexusDock API: {reason}", token),
                code="network_error",
            ) from None

        if len(raw) > MAX_RESPONSE_BYTES:
            raise SkillError("NexusDock API response exceeded 4 MiB", code="response_too_large")
        payload = self._decode_json(raw, token)
        return APIResponse(status=status, headers=response_headers, payload=payload)

    def request_form(self, path: str, form: dict[str, str]) -> APIResponse:
        encoded = urlencode(form).encode("ascii")
        request = Request(
            self._url(path),
            data=encoded,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "NexusDock-REST-Skill",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.status
                response_headers = dict(response.headers.items())
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise SkillError(
                    "NexusDock OAuth endpoint returned an unexpected redirect",
                    code="unexpected_redirect",
                    http_status=exc.code,
                ) from None
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            payload = self._decode_json(raw, "")
            self._raise_http(exc.code, payload, dict(exc.headers.items()), "")
            raise AssertionError("unreachable")
        except (URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            raise SkillError(
                clean_error_message(f"cannot reach NexusDock OAuth endpoint: {reason}"),
                code="network_error",
            ) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SkillError("NexusDock OAuth response exceeded 4 MiB", code="response_too_large")
        return APIResponse(status=status, headers=response_headers, payload=self._decode_json(raw, ""))

    def _decode_json(self, raw: bytes, token: str) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            text = clean_error_message(raw[:1000].decode("utf-8", errors="replace"), token)
            raise SkillError(
                "NexusDock API returned non-JSON content",
                code="invalid_response",
                details={"preview": text},
            ) from None

    def _raise_http(
        self,
        status: int,
        payload: Any,
        headers: dict[str, str],
        token: str,
    ) -> None:
        code = f"http_{status}"
        message = f"NexusDock API returned HTTP {status}"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                upstream_code = error.get("code")
                upstream_message = error.get("message")
                if isinstance(upstream_code, str) and upstream_code:
                    code = upstream_code
                if isinstance(upstream_message, str) and upstream_message:
                    message = upstream_message
            elif isinstance(error, str) and error:
                code = error
                description = payload.get("error_description")
                if isinstance(description, str) and description:
                    message = description
        details: dict[str, Any] = {}
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
        if retry_after:
            details["retry_after"] = retry_after
        raise SkillError(
            clean_error_message(message, token),
            code=code,
            http_status=status,
            details=details or None,
        )

    def watch_execution(
        self,
        execution_id: str,
        *,
        last_event_id: str | None,
        wait_seconds: int,
    ) -> list[dict[str, Any]]:
        headers = {
            "Accept": "text/event-stream",
            "Authorization": "Bearer " + self.token,
            "User-Agent": "NexusDock-REST-Skill",
        }
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        request = Request(
            self._url(f"/v1/integrations/executions/{quote(execution_id, safe='')}/events"),
            headers=headers,
            method="GET",
        )
        events: list[dict[str, Any]] = []
        try:
            with self._opener.open(request, timeout=wait_seconds + 5) as response:
                if response.status != 200:
                    raise SkillError(
                        f"unexpected SSE status {response.status}",
                        code="invalid_response",
                        http_status=response.status,
                    )
                data_lines: list[str] = []
                stream_bytes = 0
                deadline = time.monotonic() + wait_seconds
                for raw_line in response:
                    stream_bytes += len(raw_line)
                    if stream_bytes > MAX_RESPONSE_BYTES or len(events) >= 1000:
                        raise SkillError("SSE stream exceeded safety limits", code="response_too_large")
                    if time.monotonic() >= deadline:
                        return events
                    if len(raw_line) > 256 * 1024:
                        raise SkillError("SSE line exceeded 256 KiB", code="response_too_large")
                    line = raw_line.decode("utf-8", errors="strict").rstrip("\r\n")
                    if not line:
                        if data_lines:
                            payload = json.loads("\n".join(data_lines))
                            if isinstance(payload, dict):
                                events.append(payload)
                                if payload.get("status") in TERMINAL_STATUSES:
                                    return events
                            data_lines.clear()
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise SkillError(
                    "NexusDock event stream returned an unexpected redirect",
                    code="unexpected_redirect",
                    http_status=exc.code,
                ) from None
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            payload = self._decode_json(raw, self.token)
            self._raise_http(exc.code, payload, dict(exc.headers.items()), self.token)
        except (URLError, TimeoutError, socket.timeout) as exc:
            if events:
                return events
            reason = getattr(exc, "reason", exc)
            raise SkillError(
                clean_error_message(f"execution event stream ended before an event: {reason}", self.token),
                code="network_error",
            ) from None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SkillError("NexusDock returned an invalid SSE event", code="invalid_response") from exc
        return events


def execution_id(data: dict[str, Any]) -> str:
    value = require_text(data, "execution_id", 128)
    if not value.startswith("exec_"):
        raise SkillError("execution_id is not a NexusDock Execution id", code="invalid_input")
    return value


def capability_id(data: dict[str, Any]) -> str:
    return require_text(data, "capability_id", 256)


def _register_device_client(client: NexusDockClient, state: dict[str, Any]) -> str:
    response = client.request_json(
        "POST",
        "/oauth/register",
        body={
            "client_name": OAUTH_CLIENT_NAME,
            "grant_types": [OAUTH_DEVICE_GRANT, "refresh_token"],
            "token_endpoint_auth_method": "none",
        },
        authenticated=False,
    )
    payload = response.payload
    client_id = payload.get("client_id") if isinstance(payload, dict) else None
    if not isinstance(client_id, str) or not client_id:
        raise SkillError("OAuth client registration response is malformed", code="invalid_response")
    state["client_id"] = client_id
    save_oauth_state(state)
    return client_id


def _start_device_flow(client: NexusDockClient, state: dict[str, Any], client_id: str) -> dict[str, Any]:
    response = client.request_form(
        "/oauth/device_authorization",
        {
            "client_id": client_id,
            "resource": client.base_url + "/v1/integrations",
            "scope": OAUTH_SCOPE,
        },
    )
    payload = response.payload
    if not isinstance(payload, dict):
        raise SkillError("OAuth device authorization response is malformed", code="invalid_response")
    device_code = payload.get("device_code")
    user_code = payload.get("user_code")
    verification_uri = payload.get("verification_uri")
    verification_uri_complete = payload.get("verification_uri_complete")
    expires_in = payload.get("expires_in")
    interval = payload.get("interval")
    if (
        not isinstance(device_code, str)
        or not device_code
        or not isinstance(user_code, str)
        or not user_code
        or verification_uri != client.base_url + "/activate"
        or not isinstance(verification_uri_complete, str)
        or not verification_uri_complete.startswith(verification_uri + "?")
        or not isinstance(expires_in, int)
        or not 1 <= expires_in <= 3600
        or not isinstance(interval, int)
        or not 1 <= interval <= 60
    ):
        raise SkillError("OAuth device authorization response is malformed", code="invalid_response")
    now = time.time()
    pending = {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": verification_uri_complete,
        "expires_at": int(now) + expires_in,
        "interval": interval,
        "next_poll_at": now + interval,
    }
    state["pending"] = pending
    save_oauth_state(state)
    return pending


def operation_oauth_login(data: dict[str, Any]) -> dict[str, Any]:
    if static_token() is not None:
        raise SkillError(
            "NEXUSDOCK_TOKEN is configured; unset it before using managed OAuth",
            code="static_token_configured",
        )
    client = NexusDockClient(data)
    restart = data.get("restart", False)
    if not isinstance(restart, bool):
        raise SkillError("restart must be a boolean", code="invalid_input")
    with oauth_state_lock():
        state = oauth_state_for_base_url(client.base_url)
        if restart:
            state.pop("pending", None)
            state.pop("tokens", None)
            save_oauth_state(state)

        tokens = oauth_tokens(state)
        if tokens and float(tokens["expires_at"]) > time.time() + 60:
            return {
                "ok": True,
                "action": "oauth_login",
                "authorized": True,
                "oauth": public_oauth_status(state),
            }
        if tokens and isinstance(tokens.get("refresh_token"), str) and tokens.get("refresh_token"):
            client._refresh_oauth_locked(state)
            return {
                "ok": True,
                "action": "oauth_login",
                "authorized": True,
                "oauth": public_oauth_status(state),
            }

        pending = oauth_pending(state)
        if pending and float(pending["expires_at"]) <= time.time():
            state.pop("pending", None)
            save_oauth_state(state)
            pending = None

        if pending:
            now = time.time()
            next_poll = float(pending.get("next_poll_at", 0))
            if now < next_poll:
                return {
                    "ok": True,
                    "action": "oauth_login",
                    "authorized": False,
                    "oauth": public_oauth_status(state),
                }
            client_id = state.get("client_id")
            if not isinstance(client_id, str) or not client_id:
                state.pop("pending", None)
                save_oauth_state(state)
                pending = None
            else:
                try:
                    response = client.request_form(
                        "/oauth/token",
                        {
                            "grant_type": OAUTH_DEVICE_GRANT,
                            "client_id": client_id,
                            "device_code": str(pending["device_code"]),
                            "resource": client.base_url + "/v1/integrations",
                        },
                    )
                except SkillError as exc:
                    interval = int(pending.get("interval", 5))
                    if exc.code in {"authorization_pending", "slow_down"}:
                        if exc.code == "slow_down":
                            interval = min(interval + 5, 60)
                            pending["interval"] = interval
                        pending["next_poll_at"] = time.time() + interval
                        state["pending"] = pending
                        save_oauth_state(state)
                        return {
                            "ok": True,
                            "action": "oauth_login",
                            "authorized": False,
                            "oauth": public_oauth_status(state),
                        }
                    if exc.code in {"access_denied", "expired_token", "invalid_grant", "invalid_client"}:
                        state.pop("pending", None)
                        if exc.code == "invalid_client":
                            state.pop("client_id", None)
                        save_oauth_state(state)
                        raise SkillError(
                            "NexusDock OAuth authorization must be started again",
                            code="oauth_authorization_required",
                        ) from None
                    raise
                client._store_token_response(state, response.payload)
                return {
                    "ok": True,
                    "action": "oauth_login",
                    "authorized": True,
                    "oauth": public_oauth_status(state),
                }

        client_id = state.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            client_id = _register_device_client(client, state)
        try:
            pending = _start_device_flow(client, state, client_id)
        except SkillError as exc:
            if exc.code not in {"invalid_client", "unauthorized_client"}:
                raise
            state.pop("client_id", None)
            state.pop("pending", None)
            save_oauth_state(state)
            client_id = _register_device_client(client, state)
            pending = _start_device_flow(client, state, client_id)

        return {
            "ok": True,
            "action": "oauth_login",
            "authorized": False,
            "oauth": public_oauth_status(state),
        }


def operation_oauth_status(data: dict[str, Any]) -> dict[str, Any]:
    client = NexusDockClient(data)
    with oauth_state_lock():
        state = oauth_state_for_base_url(client.base_url)
        pending = oauth_pending(state)
        if pending and float(pending["expires_at"]) <= time.time():
            state.pop("pending", None)
            save_oauth_state(state)
        status = public_oauth_status(state)
    return {
        "ok": True,
        "action": "oauth_status",
        "auth_mode": "static_token" if static_token() is not None else "oauth",
        "oauth": status,
    }


def operation_oauth_refresh(data: dict[str, Any]) -> dict[str, Any]:
    if static_token() is not None:
        raise SkillError(
            "NEXUSDOCK_TOKEN is configured; managed OAuth refresh is not active",
            code="static_token_configured",
        )
    client = NexusDockClient(data)
    with oauth_state_lock():
        state = oauth_state_for_base_url(client.base_url)
        client._refresh_oauth_locked(state)
        status = public_oauth_status(state)
    client._token = None
    return {"ok": True, "action": "oauth_refresh", "oauth": status}


def operation_oauth_logout(data: dict[str, Any]) -> dict[str, Any]:
    if static_token() is not None:
        raise SkillError(
            "NEXUSDOCK_TOKEN is configured; remove that static credential separately",
            code="static_token_configured",
        )
    client = NexusDockClient(data)
    with oauth_state_lock():
        state = oauth_state_for_base_url(client.base_url)
        client_id = state.get("client_id")
        tokens = oauth_tokens(state)
        if isinstance(client_id, str) and client_id and tokens:
            token = tokens.get("refresh_token") or tokens.get("access_token")
            if isinstance(token, str) and token:
                client.request_form(
                    "/oauth/revoke",
                    {"client_id": client_id, "token": token},
                )
        state.pop("tokens", None)
        state.pop("pending", None)
        save_oauth_state(state)
        status = public_oauth_status(state)
    return {"ok": True, "action": "oauth_logout", "oauth": status}


def operation_status(data: dict[str, Any]) -> dict[str, Any]:
    client = NexusDockClient(data)
    health = client.request_json("GET", "/health", authenticated=False)
    with oauth_state_lock():
        state = oauth_state_for_base_url(client.base_url)
        oauth = public_oauth_status(state)
    static = static_token()
    result: dict[str, Any] = {
        "ok": True,
        "action": "status",
        "configured": True,
        "ready": False,
        "base_url": client.base_url,
        "health": health.payload,
        "auth_mode": "static_token" if static is not None else "oauth",
        "oauth": oauth,
    }
    if static is None and not oauth["authorized"] and not oauth["refresh_available"]:
        return result
    try:
        devices = client.request_json("GET", "/v1/integrations/devices")
    except SkillError as exc:
        if exc.code == "oauth_authorization_required":
            return result
        raise
    result["authenticated"] = True
    result["ready"] = True
    result["device_count"] = len(devices.payload.get("devices", [])) if isinstance(devices.payload, dict) else None
    return result


def operation_devices(data: dict[str, Any]) -> dict[str, Any]:
    client = NexusDockClient(data)
    response = client.request_json("GET", "/v1/integrations/devices")
    return {"ok": True, "action": "devices", "response": response.payload}


def operation_capabilities(data: dict[str, Any]) -> dict[str, Any]:
    query: dict[str, Any] = {
        "node_id": data.get("node_id"),
        "type": data.get("type"),
        "q": data.get("q"),
    }
    if query["type"] is not None and query["type"] not in CAPABILITY_TYPES:
        raise SkillError(
            "type must be one of tool, skill, plugin, mcp",
            code="invalid_input",
            details={"field": "type"},
        )
    client = NexusDockClient(data)
    response = client.request_json("GET", "/v1/integrations/capabilities", query=query)
    return {"ok": True, "action": "capabilities", "response": response.payload}


def operation_capability(data: dict[str, Any]) -> dict[str, Any]:
    cap_id = capability_id(data)
    client = NexusDockClient(data)
    response = client.request_json(
        "GET",
        "/v1/integrations/capabilities/" + quote(cap_id, safe=""),
    )
    return {"ok": True, "action": "capability", "response": response.payload}


def read_current_tool_capability(
    client: NexusDockClient,
    cap_id: str,
    expected_hash: str,
) -> dict[str, Any]:
    response = client.request_json(
        "GET",
        "/v1/integrations/capabilities/" + quote(cap_id, safe=""),
    )
    payload = response.payload
    capability = payload.get("capability") if isinstance(payload, dict) else None
    if not isinstance(capability, dict):
        raise SkillError("capability response is malformed", code="invalid_response")
    if capability.get("type") != "tool":
        raise SkillError("capability is not an executable Tool", code="not_a_tool")
    if capability.get("online") is not True or capability.get("stale") is True:
        raise SkillError("target Tool capability is offline or stale", code="capability_not_ready")
    current_hash = capability.get("contract_hash")
    if not isinstance(current_hash, str) or not current_hash:
        raise SkillError("target Tool capability has no contract_hash", code="capability_not_ready")
    if current_hash != expected_hash:
        raise SkillError(
            "target Tool contract changed; refresh the schema before executing",
            code="CAPABILITY_SCHEMA_CHANGED",
            details={"current_contract_hash": current_hash},
        )
    schema = capability.get("input_schema")
    if not isinstance(schema, dict):
        raise SkillError("target Tool capability has no input_schema", code="capability_not_ready")
    return capability


def operation_execute(data: dict[str, Any]) -> dict[str, Any]:
    cap_id = capability_id(data)
    contract_hash = require_text(data, "contract_hash", 256)
    input_value = require_object(data, "input")
    idempotency_key = require_text(data, "idempotency_key", 200)
    try:
        idempotency_key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SkillError("idempotency_key must contain ASCII characters only", code="invalid_input") from exc
    if any(ch.isspace() for ch in idempotency_key):
        raise SkillError("idempotency_key must not contain whitespace", code="invalid_input")

    body: dict[str, Any] = {
        "capability_id": cap_id,
        "contract_hash": contract_hash,
        "input": input_value,
    }
    if "timeout_ms" in data:
        body["timeout_ms"] = integer(data, "timeout_ms", 120000, 1000, 1800000)

    client = NexusDockClient(data)
    capability = read_current_tool_capability(client, cap_id, contract_hash)
    response = client.request_json(
        "POST",
        "/v1/integrations/executions",
        body=body,
        headers={"Idempotency-Key": idempotency_key},
    )
    replayed = (response.headers.get("Idempotency-Replayed") or "").lower() == "true"
    return {
        "ok": True,
        "action": "execute",
        "http_status": response.status,
        "idempotency_replayed": replayed,
        "tool": {
            "id": capability.get("id"),
            "node_id": capability.get("node_id"),
            "name": capability.get("name"),
            "contract_hash": capability.get("contract_hash"),
        },
        "response": response.payload,
    }


def operation_execution(data: dict[str, Any]) -> dict[str, Any]:
    item_id = execution_id(data)
    client = NexusDockClient(data)
    response = client.request_json(
        "GET",
        "/v1/integrations/executions/" + quote(item_id, safe=""),
    )
    return {"ok": True, "action": "execution", "response": response.payload}


def operation_list_executions(data: dict[str, Any]) -> dict[str, Any]:
    status = data.get("status")
    if status is not None and status not in EXECUTION_STATUSES:
        raise SkillError("status is not a valid Execution status", code="invalid_input")
    limit = integer(data, "limit", 50, 1, 100)
    query = {"status": status, "before": data.get("before"), "limit": limit}
    client = NexusDockClient(data)
    response = client.request_json("GET", "/v1/integrations/executions", query=query)
    return {"ok": True, "action": "list_executions", "response": response.payload}


def operation_cancel(data: dict[str, Any]) -> dict[str, Any]:
    item_id = execution_id(data)
    client = NexusDockClient(data)
    response = client.request_json(
        "POST",
        f"/v1/integrations/executions/{quote(item_id, safe='')}/cancel",
    )
    return {
        "ok": True,
        "action": "cancel",
        "http_status": response.status,
        "response": response.payload,
    }


def final_execution(payload: Any) -> dict[str, Any]:
    execution = payload.get("execution") if isinstance(payload, dict) else None
    if not isinstance(execution, dict):
        raise SkillError("execution response is malformed", code="invalid_response")
    status = execution.get("status")
    if status not in EXECUTION_STATUSES:
        raise SkillError("execution response has an invalid status", code="invalid_response")
    return execution


def operation_wait_execution(data: dict[str, Any]) -> dict[str, Any]:
    item_id = execution_id(data)
    wait_seconds = integer(data, "wait_seconds", 120, 1, 1800)
    poll_seconds = number(data, "poll_seconds", 1.0, 0.2, 10.0)
    client = NexusDockClient(data)
    deadline = time.monotonic() + wait_seconds
    last: Any = None
    while True:
        response = client.request_json(
            "GET",
            "/v1/integrations/executions/" + quote(item_id, safe=""),
        )
        last = response.payload
        execution = final_execution(last)
        if execution["status"] in TERMINAL_STATUSES:
            result: dict[str, Any] = {
                "ok": True,
                "action": "wait_execution",
                "terminal": True,
                "response": last,
            }
            if execution["status"] == "unknown":
                result["automatic_retry_allowed"] = False
                result["warning"] = (
                    "Execution result is unknown; the Tool may already have produced side effects. "
                    "Do not automatically execute again."
                )
            return result
        if time.monotonic() >= deadline:
            return {
                "ok": True,
                "action": "wait_execution",
                "terminal": False,
                "timed_out": True,
                "response": last,
            }
        time.sleep(poll_seconds)


def operation_watch_execution(data: dict[str, Any]) -> dict[str, Any]:
    item_id = execution_id(data)
    wait_seconds = integer(data, "wait_seconds", 120, 20, 1800)
    last_event_id = data.get("last_event_id")
    if last_event_id is not None:
        if not isinstance(last_event_id, (str, int)) or not str(last_event_id).isdigit():
            raise SkillError("last_event_id must be a non-negative integer", code="invalid_input")
        last_event_id = str(last_event_id)

    client = NexusDockClient(data)
    events = client.watch_execution(
        item_id,
        last_event_id=last_event_id,
        wait_seconds=wait_seconds,
    )
    detail = client.request_json(
        "GET",
        "/v1/integrations/executions/" + quote(item_id, safe=""),
    )
    execution = final_execution(detail.payload)
    result: dict[str, Any] = {
        "ok": True,
        "action": "watch_execution",
        "events": events,
        "terminal": execution["status"] in TERMINAL_STATUSES,
        "response": detail.payload,
    }
    if execution["status"] == "unknown":
        result["automatic_retry_allowed"] = False
        result["warning"] = (
            "Execution result is unknown; the Tool may already have produced side effects. "
            "Do not automatically execute again."
        )
    return result


ACTIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "status": operation_status,
    "oauth_login": operation_oauth_login,
    "oauth_status": operation_oauth_status,
    "oauth_refresh": operation_oauth_refresh,
    "oauth_logout": operation_oauth_logout,
    "devices": operation_devices,
    "capabilities": operation_capabilities,
    "capability": operation_capability,
    "execute": operation_execute,
    "execution": operation_execution,
    "list_executions": operation_list_executions,
    "cancel": operation_cancel,
    "wait_execution": operation_wait_execution,
    "watch_execution": operation_watch_execution,
}


def main() -> int:
    action = "status"
    try:
        data = load_input()
        raw_action = data.get("skill_action", "status")
        if not isinstance(raw_action, str):
            raise SkillError("skill_action must be a string", code="invalid_input")
        action = raw_action.strip()
        handler = ACTIONS.get(action)
        if handler is None:
            raise SkillError(
                f"unsupported skill_action: {action}",
                code="unsupported_action",
                details={"supported_actions": sorted(ACTIONS)},
            )
        return emit(handler(data))
    except SkillError as exc:
        error: dict[str, Any] = {
            "code": exc.code,
            "message": clean_error_message(str(exc), os.environ.get("NEXUSDOCK_TOKEN", "")),
        }
        if exc.http_status is not None:
            error["http_status"] = exc.http_status
        if exc.details is not None:
            error["details"] = exc.details
        return emit({"ok": False, "action": action, "error": error}, 1)
    except Exception:
        # Avoid printing arbitrary exception strings: urllib and parser failures can contain request data.
        return emit(
            {
                "ok": False,
                "action": action,
                "error": {"code": "internal_error", "message": "unexpected NexusDock REST Skill error"},
            },
            1,
        )


if __name__ == "__main__":
    raise SystemExit(main())
