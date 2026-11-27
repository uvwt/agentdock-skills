from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("nexusdock_rest_skill", ROOT / "run.py")
assert SPEC and SPEC.loader
skill = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = skill
SPEC.loader.exec_module(skill)


class State:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.capability_hash = "sha256:current"
        self.execution_status = "queued"
        self.oauth_authorized = False
        self.oauth_refresh_count = 0
        self.oauth_client_id = "mcpc_0123456789abcdef0123456789abcdef"
        self.oauth_device_code = "ndod_device-secret"
        self.oauth_user_code = "BCDF-GHJK"


class Handler(BaseHTTPRequestHandler):
    state: State

    def log_message(self, format: str, *args: object) -> None:
        return

    def _record(self, body: object | None = None) -> None:
        self.state.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "idempotency_key": self.headers.get("Idempotency-Key"),
                "body": body,
            }
        )

    def _json(self, status: int, payload: object, headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/redirect":
            self._record()
            self.send_response(302)
            self.send_header("Location", "https://example.invalid/should-not-receive-token")
            self.end_headers()
            return
        if self.path == "/health":
            self._record()
            self._json(200, {"ok": True})
            return
        if self.path == "/v1/integrations/devices":
            self._record()
            self._json(200, {"devices": [{"id": "node_1", "online": True}]})
            return
        if self.path == "/v1/integrations/capabilities/cap_1":
            self._record()
            self._json(
                200,
                {
                    "capability": {
                        "id": "cap_1",
                        "node_id": "node_1",
                        "type": "tool",
                        "name": "echo",
                        "online": True,
                        "stale": False,
                        "contract_hash": self.state.capability_hash,
                        "input_schema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                },
            )
            return
        if self.path == "/v1/integrations/executions/exec_123":
            self._record()
            payload: dict[str, object] = {
                "id": "exec_123",
                "status": self.state.execution_status,
                "node_id": "node_1",
                "capability_id": "cap_1",
                "tool_name": "echo",
                "contract_hash": "sha256:current",
                "timeout_ms": 120000,
                "version": 3,
                "created_at": "2026-10-31T00:00:00Z",
                "updated_at": "2026-10-31T00:00:01Z",
            }
            if self.state.execution_status == "unknown":
                payload["error"] = {"code": "EXECUTION_RESULT_UNKNOWN", "message": "unknown"}
            self._json(200, {"execution": payload})
            return
        if self.path == "/v1/integrations/executions/exec_123/events":
            self._record()
            event = json.dumps(
                {"id": "exec_123", "status": "succeeded", "version": 4},
                separators=(",", ":"),
            )
            raw = f"id: 4\nevent: execution\ndata: {event}\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            self.state.execution_status = "succeeded"
            return
        self._record()
        self._json(404, {"error": {"code": "NOT_FOUND", "message": "not found"}})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("application/x-www-form-urlencoded"):
            parsed = parse_qs(raw.decode(), keep_blank_values=True)
            body = {key: values[-1] for key, values in parsed.items()}
        else:
            body = json.loads(raw) if raw else None
        self._record(body)
        base_url = "http://" + self.headers["Host"]
        if self.path == "/oauth/register":
            self._json(
                201,
                {
                    "client_id": self.state.oauth_client_id,
                    "client_name": "NexusDock REST Skill",
                    "redirect_uris": [],
                    "grant_types": [
                        "refresh_token",
                        "urn:ietf:params:oauth:grant-type:device_code",
                    ],
                    "response_types": [],
                    "token_endpoint_auth_method": "none",
                    "client_id_issued_at": 1,
                },
            )
            return
        if self.path == "/oauth/device_authorization":
            self._json(
                200,
                {
                    "device_code": self.state.oauth_device_code,
                    "user_code": self.state.oauth_user_code,
                    "verification_uri": base_url + "/activate",
                    "verification_uri_complete": base_url + "/activate?user_code=" + self.state.oauth_user_code,
                    "expires_in": 600,
                    "interval": 5,
                },
            )
            return
        if self.path == "/oauth/token":
            assert isinstance(body, dict)
            if body.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code":
                if not self.state.oauth_authorized:
                    self._json(
                        400,
                        {
                            "error": "authorization_pending",
                            "error_description": "authorization is still pending",
                        },
                    )
                    return
                self._json(
                    200,
                    {
                        "access_token": "ndoa_access-secret-1",
                        "refresh_token": "ndor_refresh-secret-1",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": "integrations",
                        "sub": "user-1",
                    },
                )
                return
            if body.get("grant_type") == "refresh_token":
                self.state.oauth_refresh_count += 1
                suffix = self.state.oauth_refresh_count + 1
                self._json(
                    200,
                    {
                        "access_token": f"ndoa_access-secret-{suffix}",
                        "refresh_token": f"ndor_refresh-secret-{suffix}",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": "integrations",
                        "sub": "user-1",
                    },
                )
                return
        if self.path == "/oauth/revoke":
            self.send_response(200)
            self.end_headers()
            return
        if self.path == "/v1/integrations/executions":
            self._json(
                202,
                {
                    "execution": {
                        "id": "exec_123",
                        "status": "queued",
                        "node_id": "node_1",
                        "capability_id": "cap_1",
                        "tool_name": "echo",
                        "contract_hash": "sha256:current",
                        "timeout_ms": 120000,
                        "version": 1,
                        "created_at": "2026-10-31T00:00:00Z",
                        "updated_at": "2026-10-31T00:00:00Z",
                    }
                },
            )
            return
        if self.path == "/v1/integrations/executions/exec_123/cancel":
            self._json(202, {"execution": {"id": "exec_123", "status": "running"}})
            return
        self._json(404, {"error": {"code": "NOT_FOUND", "message": "not found"}})


@contextmanager
def server():
    state = State()
    handler = type("BoundHandler", (Handler,), {"state": state})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=2)
        httpd.server_close()


class SkillTests(unittest.TestCase):
    def test_non_loopback_http_is_rejected(self) -> None:
        with self.assertRaises(skill.SkillError) as caught:
            skill.normalize_base_url("http://example.com")
        self.assertEqual(caught.exception.code, "insecure_base_url")

    def test_bearer_request_never_follows_redirect(self) -> None:
        with server() as (state, base_url), patch.dict(
            os.environ,
            {"NEXUSDOCK_BASE_URL": base_url, "NEXUSDOCK_TOKEN": "top-secret-token"},
            clear=False,
        ):
            client = skill.NexusDockClient({})
            with self.assertRaises(skill.SkillError) as caught:
                client.request_json("GET", "/redirect")

        self.assertEqual(caught.exception.code, "unexpected_redirect")
        self.assertEqual(len(state.requests), 1)
        self.assertEqual(state.requests[0]["authorization"], "Bearer top-secret-token")

    def test_status_without_token_only_checks_health(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir, server() as (state, base_url), patch.dict(
            os.environ,
            {"NEXUSDOCK_BASE_URL": base_url, "SKILL_DATA_DIR": data_dir},
            clear=False,
        ):
            os.environ.pop("NEXUSDOCK_TOKEN", None)
            result = skill.operation_status({})
        self.assertTrue(result["configured"])
        self.assertFalse(result["ready"])
        self.assertEqual(result["auth_mode"], "oauth")
        self.assertEqual([item["path"] for item in state.requests], ["/health"])

    def test_oauth_device_flow_refresh_and_logout_never_return_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir, server() as (state, base_url), patch.dict(
            os.environ,
            {"NEXUSDOCK_BASE_URL": base_url, "SKILL_DATA_DIR": data_dir},
            clear=False,
        ):
            os.environ.pop("NEXUSDOCK_TOKEN", None)

            started = skill.operation_oauth_login({})
            self.assertFalse(started["authorized"])
            self.assertEqual(started["oauth"]["user_code"], state.oauth_user_code)
            self.assertEqual(started["oauth"]["verification_uri"], base_url + "/activate")
            self.assertNotIn(state.oauth_device_code, json.dumps(started))

            saved = json.loads((Path(data_dir) / "oauth.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["pending"]["device_code"], state.oauth_device_code)

            state.oauth_authorized = True
            with skill.oauth_state_lock():
                saved = skill.load_oauth_state()
                saved["pending"]["next_poll_at"] = 0
                skill.save_oauth_state(saved)

            authorized = skill.operation_oauth_login({})
            rendered = json.dumps(authorized)
            self.assertTrue(authorized["authorized"])
            self.assertNotIn("ndoa_access-secret-1", rendered)
            self.assertNotIn("ndor_refresh-secret-1", rendered)
            self.assertNotIn(state.oauth_device_code, rendered)

            devices = skill.operation_devices({})
            self.assertTrue(devices["ok"])
            device_request = next(
                item for item in reversed(state.requests)
                if item["path"] == "/v1/integrations/devices"
            )
            self.assertEqual(device_request["authorization"], "Bearer ndoa_access-secret-1")

            refreshed = skill.operation_oauth_refresh({})
            refreshed_rendered = json.dumps(refreshed)
            self.assertNotIn("ndoa_access-secret-2", refreshed_rendered)
            self.assertNotIn("ndor_refresh-secret-2", refreshed_rendered)
            saved_raw = (Path(data_dir) / "oauth.json").read_text(encoding="utf-8")
            self.assertNotIn("ndor_refresh-secret-1", saved_raw)
            self.assertIn("ndor_refresh-secret-2", saved_raw)

            logged_out = skill.operation_oauth_logout({})
            self.assertFalse(logged_out["oauth"]["authorized"])
            self.assertFalse(logged_out["oauth"]["refresh_available"])
            revoke = next(item for item in reversed(state.requests) if item["path"] == "/oauth/revoke")
            self.assertEqual(revoke["body"]["token"], "ndor_refresh-secret-2")
            self.assertNotIn("ndor_refresh-secret-2", json.dumps(logged_out))

    def test_expired_oauth_access_token_refreshes_before_api_request(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir, server() as (state, base_url), patch.dict(
            os.environ,
            {"NEXUSDOCK_BASE_URL": base_url, "SKILL_DATA_DIR": data_dir},
            clear=False,
        ):
            os.environ.pop("NEXUSDOCK_TOKEN", None)
            with skill.oauth_state_lock():
                skill.save_oauth_state(
                    {
                        "base_url": base_url,
                        "client_id": state.oauth_client_id,
                        "tokens": {
                            "access_token": "ndoa_expired-secret",
                            "refresh_token": "ndor_refresh-secret-1",
                            "expires_at": 1,
                            "scope": "integrations",
                            "sub": "user-1",
                        },
                    }
                )

            result = skill.operation_devices({})

            self.assertTrue(result["ok"])
            token_request = next(item for item in state.requests if item["path"] == "/oauth/token")
            self.assertEqual(token_request["body"]["grant_type"], "refresh_token")
            device_request = next(item for item in state.requests if item["path"] == "/v1/integrations/devices")
            self.assertEqual(device_request["authorization"], "Bearer ndoa_access-secret-2")

    def test_oauth_state_is_bound_to_exact_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir, server() as (state, base_url), patch.dict(
            os.environ,
            {"NEXUSDOCK_BASE_URL": base_url, "SKILL_DATA_DIR": data_dir},
            clear=False,
        ):
            os.environ.pop("NEXUSDOCK_TOKEN", None)
            with skill.oauth_state_lock():
                skill.save_oauth_state(
                    {
                        "base_url": "https://dev.example.invalid",
                        "client_id": state.oauth_client_id,
                        "tokens": {
                            "access_token": "ndoa_wrong-origin-secret",
                            "refresh_token": "ndor_wrong-origin-secret",
                            "expires_at": int(skill.time.time()) + 3600,
                            "scope": "integrations",
                        },
                    }
                )
                current = skill.oauth_state_for_base_url(base_url)

            self.assertEqual(current, {"base_url": base_url})
            saved = (Path(data_dir) / "oauth.json").read_text(encoding="utf-8")
            self.assertNotIn("wrong-origin-secret", saved)

    def test_execute_revalidates_capability_and_sends_idempotency_header(self) -> None:
        with server() as (state, base_url), patch.dict(
            os.environ,
            {"NEXUSDOCK_BASE_URL": base_url, "NEXUSDOCK_TOKEN": "top-secret-token"},
            clear=False,
        ):
            result = skill.operation_execute(
                {
                    "capability_id": "cap_1",
                    "contract_hash": "sha256:current",
                    "input": {"text": "hello"},
                    "idempotency_key": "attempt-123",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual([item["path"] for item in state.requests], [
            "/v1/integrations/capabilities/cap_1",
            "/v1/integrations/executions",
        ])
        created = state.requests[-1]
        self.assertEqual(created["authorization"], "Bearer top-secret-token")
        self.assertEqual(created["idempotency_key"], "attempt-123")
        self.assertEqual(
            created["body"],
            {
                "capability_id": "cap_1",
                "contract_hash": "sha256:current",
                "input": {"text": "hello"},
            },
        )
        self.assertNotIn("top-secret-token", json.dumps(result))

    def test_execute_stops_before_post_when_contract_changed(self) -> None:
        with server() as (state, base_url), patch.dict(
            os.environ,
            {"NEXUSDOCK_BASE_URL": base_url, "NEXUSDOCK_TOKEN": "token"},
            clear=False,
        ):
            state.capability_hash = "sha256:new"
            with self.assertRaises(skill.SkillError) as caught:
                skill.operation_execute(
                    {
                        "capability_id": "cap_1",
                        "contract_hash": "sha256:old",
                        "input": {"text": "hello"},
                        "idempotency_key": "attempt-124",
                    }
                )

        self.assertEqual(caught.exception.code, "CAPABILITY_SCHEMA_CHANGED")
        self.assertEqual(len(state.requests), 1)
        self.assertEqual(state.requests[0]["method"], "GET")

    def test_unknown_wait_never_retries_the_tool(self) -> None:
        with server() as (state, base_url), patch.dict(
            os.environ,
            {"NEXUSDOCK_BASE_URL": base_url, "NEXUSDOCK_TOKEN": "token"},
            clear=False,
        ):
            state.execution_status = "unknown"
            result = skill.operation_wait_execution(
                {"execution_id": "exec_123", "wait_seconds": 1, "poll_seconds": 0.2}
            )

        self.assertTrue(result["terminal"])
        self.assertFalse(result["automatic_retry_allowed"])
        self.assertEqual([item["method"] for item in state.requests], ["GET"])

    def test_sse_watch_reads_terminal_event_then_detail(self) -> None:
        with server() as (state, base_url), patch.dict(
            os.environ,
            {"NEXUSDOCK_BASE_URL": base_url, "NEXUSDOCK_TOKEN": "token"},
            clear=False,
        ):
            result = skill.operation_watch_execution(
                {"execution_id": "exec_123", "wait_seconds": 20}
            )

        self.assertTrue(result["terminal"])
        self.assertEqual(result["events"][0]["status"], "succeeded")
        self.assertEqual(
            [item["path"] for item in state.requests],
            [
                "/v1/integrations/executions/exec_123/events",
                "/v1/integrations/executions/exec_123",
            ],
        )


if __name__ == "__main__":
    unittest.main()
