from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "run.py"
spec = importlib.util.spec_from_file_location("multi_agent_orchestration_run", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
assert spec.loader is not None
spec.loader.exec_module(module)


class FakeHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def log_message(self, *_args: object) -> None:
        return

    def _reply(self, status: int, payload: object) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        payload = json.loads(body) if body else None
        item = {
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "payload": payload,
        }
        self.__class__.requests.append(item)
        return item

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        if self.path == "/healthz":
            self._reply(200, {"status": "ok"})
            return
        if self.path == "/api/v1/agent/slots/3/next":
            self._reply(200, {"status": "idle", "slot": 3})
            return
        if self.path.startswith("/api/v1/work-items/wi_1/records"):
            self._reply(200, [{"id": "rec_1"}])
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        item = self._record()
        if self.path == "/api/v1/agent/slots/3/claim":
            self._reply(201, {
                "status": "claimed",
                "slot": 3,
                "assignment": {"id": "asg_1", "seat": 3, "focus_role": "backend", "kind": "work"},
                "execution": {"id": "exe_1", "status": "queued"},
                "work_item": {"id": "wi_1", "title": "实现后端"},
            })
            return
        if self.path == "/api/v1/agent/executions/exe_1/status":
            payload = item["payload"]
            self._reply(200, {"id": "exe_1", "status": payload["status"], "summary": payload.get("summary", "")})
            return
        if self.path == "/api/v1/agent/executions/exe_1/artifacts":
            self._reply(201, {"id": "art_1", **item["payload"]})
            return
        if self.path == "/api/v1/agent/executions/exe_1/assignments":
            self._reply(201, item["payload"]["assignments"])
            return
        if self.path == "/api/v1/agent/executions/exe_1/gate-request":
            self._reply(201, {"id": "asg_gate", "seat": 2, "kind": "gate"})
            return
        if self.path == "/api/v1/agent/executions/exe_1/gate":
            self._reply(201, {"id": "gate_1", **item["payload"]})
            return
        if self.path == "/api/v1/agent/executions/exe_1/complete":
            self._reply(200, {"id": "wi_1", "status": "completed"})
            return
        self._reply(404, {"error": "not found"})


@contextmanager
def fake_server():
    FakeHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class MultiAgentOrchestratorClientTests(unittest.TestCase):
    def test_status只暴露配置状态不暴露Token(self) -> None:
        with fake_server() as base_url, patch.dict(os.environ, {
            module.ORCHESTRATOR_URL_ENV: base_url,
            module.ORCHESTRATOR_TOKEN_ENV: "super-secret-token",
        }, clear=True):
            result = module.handle({"skill_action": "status"})
        self.assertEqual(result["health"], {"status": "ok"})
        self.assertTrue(result["config"]["token_configured"])
        self.assertNotIn("super-secret-token", json.dumps(result, ensure_ascii=False))
        self.assertEqual(FakeHandler.requests[-1]["authorization"], "")

    def test_claim只需slot并使用BearerToken(self) -> None:
        with fake_server() as base_url, patch.dict(os.environ, {
            module.ORCHESTRATOR_URL_ENV: base_url,
            module.ORCHESTRATOR_TOKEN_ENV: "agent-token",
            module.NODE_ID_ENV: "dockmini",
            module.AGENT_ENV: "timer-agent",
        }, clear=True):
            result = module.handle({"skill_action": "claim", "slot": 3})
        self.assertEqual(result["status"], "claimed")
        request = FakeHandler.requests[-1]
        self.assertEqual(request["path"], "/api/v1/agent/slots/3/claim")
        self.assertEqual(request["authorization"], "Bearer agent-token")
        self.assertEqual(request["payload"]["node_id"], "dockmini")
        self.assertEqual(request["payload"]["agent"], "timer-agent")
        self.assertNotIn("role", request["payload"])
        self.assertNotIn("from", request["payload"])

    def test_next支持slot环境变量且idle正常返回(self) -> None:
        with fake_server() as base_url, patch.dict(os.environ, {
            module.ORCHESTRATOR_URL_ENV: base_url,
            module.ORCHESTRATOR_TOKEN_ENV: "agent-token",
            module.SLOT_ENV: "3",
        }, clear=True):
            result = module.handle({"skill_action": "next"})
        self.assertEqual(result, {"status": "idle", "slot": 3})

    def test执行回报与角色动作映射到AgentAPI(self) -> None:
        with fake_server() as base_url, patch.dict(os.environ, {
            module.ORCHESTRATOR_URL_ENV: base_url,
            module.ORCHESTRATOR_TOKEN_ENV: "agent-token",
        }, clear=True):
            started = module.handle({"skill_action": "start", "execution_id": "exe_1"})
            finished = module.handle({"skill_action": "finish", "execution_id": "exe_1", "summary": "done"})
            artifact = module.handle({"skill_action": "artifact", "execution_id": "exe_1", "title": "result", "type": "text", "value": "ok"})
            dispatched = module.handle({"skill_action": "dispatch", "execution_id": "exe_1", "assignments": [{"seat": 3, "role": "backend", "title": "x", "goal": "y", "required": True}]})
            gate_assignment = module.handle({"skill_action": "request_gate", "execution_id": "exe_1"})
            gate = module.handle({"skill_action": "submit_gate", "execution_id": "exe_1", "gate": {"profile": "generic", "verdict": "PASS", "checks": [{"id": "x", "result": "PASS", "evidence": "ok"}], "evidence": "ok"}})
            completed = module.handle({"skill_action": "complete", "execution_id": "exe_1"})
        self.assertEqual(started["status"], "running")
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(artifact["id"], "art_1")
        self.assertEqual(dispatched[0]["seat"], 3)
        self.assertEqual(gate_assignment["seat"], 2)
        self.assertEqual(gate["verdict"], "PASS")
        self.assertEqual(completed["status"], "completed")

    def test缺少Token和非法URL快速失败(self) -> None:
        with patch.dict(os.environ, {module.ORCHESTRATOR_URL_ENV: "https://example.com"}, clear=True):
            with self.assertRaises(module.SkillError) as captured:
                module.handle({"skill_action": "claim", "slot": 3})
            self.assertEqual(captured.exception.code, "orchestrator_token_not_configured")
        with patch.dict(os.environ, {module.ORCHESTRATOR_URL_ENV: "https://user:pass@example.com/path"}, clear=True):
            with self.assertRaises(module.SkillError) as captured:
                module.handle({"skill_action": "status"})
            self.assertEqual(captured.exception.code, "invalid_orchestrator_url")

    def test_records是匿名只读接口且限制limit(self) -> None:
        with fake_server() as base_url, patch.dict(os.environ, {module.ORCHESTRATOR_URL_ENV: base_url}, clear=True):
            result = module.handle({"skill_action": "records", "work_item_id": "wi_1", "limit": 999})
        self.assertEqual(result, [{"id": "rec_1"}])
        request = FakeHandler.requests[-1]
        self.assertEqual(request["path"], "/api/v1/work-items/wi_1/records?limit=200")
        self.assertEqual(request["authorization"], "")


if __name__ == "__main__":
    unittest.main()
