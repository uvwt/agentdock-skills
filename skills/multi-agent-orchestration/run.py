#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

SKILL_NAME = "multi-agent-orchestration"
ORCHESTRATOR_URL_ENV = "MULTI_AGENT_ORCHESTRATOR_URL"
ORCHESTRATOR_TOKEN_ENV = "MULTI_AGENT_ORCHESTRATOR_TOKEN"
SLOT_ENV = "MULTI_AGENT_SLOT"
NODE_ID_ENV = "MULTI_AGENT_NODE_ID"
AGENT_ENV = "MULTI_AGENT_AGENT"
SESSION_ID_ENV = "MULTI_AGENT_SESSION_ID"
REQUEST_TIMEOUT_SECONDS = 20


class SkillError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ClientConfig:
    base_url: str
    token: str


def read_request() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {"skill_action": "status"}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SkillError("invalid_input", f"stdin 不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SkillError("invalid_input", "stdin 必须是 JSON 对象")
    return payload


def configured_url() -> str:
    raw = os.environ.get(ORCHESTRATOR_URL_ENV, "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise SkillError("invalid_orchestrator_url", f"{ORCHESTRATOR_URL_ENV} 必须是 http/https 基础地址，且不能内嵌凭据")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise SkillError("invalid_orchestrator_url", f"{ORCHESTRATOR_URL_ENV} 只能配置 origin，不要附带 path/query/fragment")
    return raw


def require_client(*, require_token: bool = True) -> ClientConfig:
    base_url = configured_url()
    if not base_url:
        raise SkillError("orchestrator_not_configured", f"未配置 {ORCHESTRATOR_URL_ENV}")
    token = os.environ.get(ORCHESTRATOR_TOKEN_ENV, "").strip()
    if require_token and not token:
        raise SkillError("orchestrator_token_not_configured", f"未配置 {ORCHESTRATOR_TOKEN_ENV}")
    return ClientConfig(base_url=base_url, token=token)


def parse_slot(request: dict[str, Any]) -> int:
    raw = request.get("slot")
    if raw in {None, ""}:
        raw = os.environ.get(SLOT_ENV, "")
    try:
        slot = int(raw)
    except (TypeError, ValueError) as exc:
        raise SkillError("invalid_slot", "slot 必须是 1/2/3/4/5") from exc
    if slot not in {1, 2, 3, 4, 5}:
        raise SkillError("invalid_slot", "slot 必须是 1/2/3/4/5")
    return slot


def clean_id(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 160:
        raise SkillError("invalid_input", f"{field} 不能为空且不能超过 160 字符")
    return text


def request_json(
    client: ClientConfig,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    authenticated: bool = True,
) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if authenticated:
        if not client.token:
            raise SkillError("orchestrator_token_not_configured", f"未配置 {ORCHESTRATOR_TOKEN_ENV}")
        headers["Authorization"] = f"Bearer {client.token}"

    request = Request(client.base_url + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except HTTPError as exc:
        raw = exc.read()
        message = f"Orchestrator HTTP {exc.code}"
        try:
            error_payload = json.loads(raw.decode("utf-8"))
            if isinstance(error_payload, dict) and str(error_payload.get("error") or "").strip():
                message = str(error_payload["error"]).strip()
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        raise SkillError(f"orchestrator_http_{exc.code}", message) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SkillError("orchestrator_unreachable", f"无法连接 Orchestrator: {exc}") from exc

    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillError("invalid_orchestrator_response", "Orchestrator 返回了非 JSON 响应") from exc


def execution_identity(request: dict[str, Any]) -> dict[str, str]:
    node_id = str(request.get("node_id") or os.environ.get(NODE_ID_ENV) or platform.node() or "unknown-node").strip()
    agent = str(request.get("agent") or os.environ.get(AGENT_ENV) or "scheduled-agent").strip()
    session_id = str(request.get("session_id") or os.environ.get(SESSION_ID_ENV) or "").strip()
    if not node_id or not agent:
        raise SkillError("invalid_execution_identity", "node_id 和 agent 不能为空")
    return {"node_id": node_id, "agent": agent, "session_id": session_id}


def action_status() -> dict[str, Any]:
    base_url = configured_url()
    token_configured = bool(os.environ.get(ORCHESTRATOR_TOKEN_ENV, "").strip())
    slot_raw = os.environ.get(SLOT_ENV, "").strip()
    data: dict[str, Any] = {
        "skill": SKILL_NAME,
        "mode": "orchestrator-client",
        "actions": [
            "status",
            "next",
            "claim",
            "start",
            "finish",
            "artifact",
            "dispatch",
            "request_gate",
            "submit_gate",
            "complete",
            "work_item",
            "outcome",
            "records",
        ],
        "config": {
            "url_configured": bool(base_url),
            "url": base_url,
            "token_configured": token_configured,
            "slot": slot_raw,
        },
    }
    if base_url:
        client = require_client(require_token=False)
        try:
            data["health"] = request_json(client, "GET", "/healthz", authenticated=False)
        except SkillError as exc:
            data["health"] = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
    return data


def action_next(request: dict[str, Any]) -> Any:
    slot = parse_slot(request)
    client = require_client()
    return request_json(client, "GET", f"/api/v1/agent/slots/{slot}/next")


def action_claim(request: dict[str, Any]) -> Any:
    slot = parse_slot(request)
    client = require_client()
    return request_json(client, "POST", f"/api/v1/agent/slots/{slot}/claim", payload=execution_identity(request))


def action_start(request: dict[str, Any]) -> Any:
    return update_execution(request, "running")


def action_finish(request: dict[str, Any]) -> Any:
    status = str(request.get("status") or "succeeded").strip().lower()
    if status not in {"succeeded", "failed", "interrupted"}:
        raise SkillError("invalid_execution_status", "finish.status 只能是 succeeded / failed / interrupted")
    return update_execution(request, status)


def update_execution(request: dict[str, Any], status: str) -> Any:
    execution_id = clean_id(request.get("execution_id"), field="execution_id")
    client = require_client()
    payload = {"status": status, "summary": str(request.get("summary") or "").strip()}
    return request_json(client, "POST", f"/api/v1/agent/executions/{quote(execution_id, safe='')}/status", payload=payload)


def action_artifact(request: dict[str, Any]) -> Any:
    execution_id = clean_id(request.get("execution_id"), field="execution_id")
    payload = request.get("artifact") if isinstance(request.get("artifact"), dict) else {
        "title": request.get("title"),
        "type": request.get("type"),
        "value": request.get("value"),
        "description": request.get("description"),
    }
    client = require_client()
    return request_json(client, "POST", f"/api/v1/agent/executions/{quote(execution_id, safe='')}/artifacts", payload=payload)


def action_dispatch(request: dict[str, Any]) -> Any:
    execution_id = clean_id(request.get("execution_id"), field="execution_id")
    assignments = request.get("assignments")
    if not isinstance(assignments, list) or not assignments:
        raise SkillError("invalid_assignments", "dispatch.assignments 必须是非空数组")
    client = require_client()
    return request_json(
        client,
        "POST",
        f"/api/v1/agent/executions/{quote(execution_id, safe='')}/assignments",
        payload={"assignments": assignments},
    )


def action_request_gate(request: dict[str, Any]) -> Any:
    execution_id = clean_id(request.get("execution_id"), field="execution_id")
    client = require_client()
    return request_json(client, "POST", f"/api/v1/agent/executions/{quote(execution_id, safe='')}/gate-request")


def action_submit_gate(request: dict[str, Any]) -> Any:
    execution_id = clean_id(request.get("execution_id"), field="execution_id")
    gate = request.get("gate") if isinstance(request.get("gate"), dict) else {
        "profile": request.get("profile"),
        "verdict": request.get("verdict"),
        "business_head_sha": request.get("business_head_sha"),
        "checks": request.get("checks"),
        "evidence": request.get("evidence"),
    }
    client = require_client()
    return request_json(client, "POST", f"/api/v1/agent/executions/{quote(execution_id, safe='')}/gate", payload=gate)


def action_complete(request: dict[str, Any]) -> Any:
    execution_id = clean_id(request.get("execution_id"), field="execution_id")
    client = require_client()
    return request_json(client, "POST", f"/api/v1/agent/executions/{quote(execution_id, safe='')}/complete")


def public_read(request: dict[str, Any], resource: str) -> Any:
    work_item_id = clean_id(request.get("work_item_id") or request.get("work_item"), field="work_item_id")
    client = require_client(require_token=False)
    suffix = ""
    if resource:
        suffix = f"/{resource}"
    if resource == "records":
        limit = request.get("limit", 50)
        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError) as exc:
            raise SkillError("invalid_limit", "records.limit 必须是整数") from exc
        suffix += f"?limit={limit}"
    return request_json(client, "GET", f"/api/v1/work-items/{quote(work_item_id, safe='')}{suffix}", authenticated=False)


def handle(request: dict[str, Any]) -> Any:
    action = str(request.get("skill_action") or "status").strip().lower()
    if action == "status":
        return action_status()
    if action == "next":
        return action_next(request)
    if action == "claim":
        return action_claim(request)
    if action == "start":
        return action_start(request)
    if action == "finish":
        return action_finish(request)
    if action == "artifact":
        return action_artifact(request)
    if action == "dispatch":
        return action_dispatch(request)
    if action == "request_gate":
        return action_request_gate(request)
    if action == "submit_gate":
        return action_submit_gate(request)
    if action == "complete":
        return action_complete(request)
    if action == "work_item":
        return public_read(request, "")
    if action == "outcome":
        return public_read(request, "outcome")
    if action == "records":
        return public_read(request, "records")
    raise SkillError("unknown_action", f"不支持的 skill_action: {action}")


def main() -> None:
    try:
        result = handle(read_request())
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))
    except SkillError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
