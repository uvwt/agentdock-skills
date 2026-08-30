from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "run.py"
WEB_ROOT = MODULE_PATH.parent / "web"

SPEC = importlib.util.spec_from_file_location("multi_agent_orchestration_overview", MODULE_PATH)
assert SPEC and SPEC.loader
run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run)


class ConsoleOverviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "demo"
        self.root.mkdir()
        self.workspace = Path(self.tempdir.name) / "multi-agent-orchestration"
        self.previous_workspace = os.environ.get(run.WORKSPACE_ENV)
        os.environ[run.WORKSPACE_ENV] = str(self.workspace)
        self.projects = run.normalize_projects([{"id": "demo", "name": "Demo", "path": str(self.root), "profile": "software"}])
        self.project = self.projects[0]

    def tearDown(self) -> None:
        if self.previous_workspace is None:
            os.environ.pop(run.WORKSPACE_ENV, None)
        else:
            os.environ[run.WORKSPACE_ENV] = self.previous_workspace
        self.tempdir.cleanup()

    def test_overview_exposes_intake_search_filter_and_refresh_controls(self) -> None:
        html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        for marker in (
            'id="openComposer"',
            'id="overviewTaskForm"',
            'id="composerProject"',
            'id="workSearch"',
            'id="statusFilter"',
            'id="refreshButton"',
            'id="updatedAt"',
            'href="/add"',
        ):
            self.assertIn(marker, html)

        dashboard = (WEB_ROOT / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn('fetch("/api/work-items"', dashboard)
        self.assertIn('fetch("/api/snapshot"', dashboard)
        self.assertNotIn("/api/records", dashboard)
        self.assertIn("itemMatchesFilters", dashboard)

        add_js = (WEB_ROOT / "add.js").read_text(encoding="utf-8")
        self.assertIn('URLSearchParams(location.search).get("project")', add_js)
        self.assertIn('fetch("/api/work-items"', add_js)
        self.assertNotIn("/api/records", add_js)

    def test_filter_refresh_and_mobile_contracts_remain_wired(self) -> None:
        html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('<option value="open">进行中</option>', html)
        for status in ("all", "BACKLOG", "ACTIVE", "REVIEW", "BLOCKED", "PAUSED", "DONE", "CANCELLED"):
            self.assertIn(f'<option value="{status}"', html)

        dashboard = (WEB_ROOT / "dashboard.js").read_text(encoding="utf-8")
        for marker in (
            'const OPEN_STATUSES = new Set(["BACKLOG", "ACTIVE", "REVIEW", "BLOCKED", "PAUSED"])',
            'params.set("q", query)',
            'params.set("status", status)',
            'workSearch").addEventListener("input"',
            'statusFilter").addEventListener("change"',
            'refreshButton").addEventListener("click"',
            'setInterval(() => refresh("auto"), 5000)',
            'reason === "manual" ? `已刷新',
            'data-compose-project=',
            '/add?project=${encodeURIComponent(projectId)}',
        ):
            self.assertIn(marker, dashboard)

        styles = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")
        self.assertIn("@media (max-width: 760px)", styles)
        self.assertIn(".desktop-table { display: none; }", styles)
        self.assertIn(".work-cards { display: grid; }", styles)
        self.assertIn(".board-toolbar { grid-template-columns: 1fr; position: sticky;", styles)

    def test_web_console_creates_work_item_and_keeps_record_api_disabled(self) -> None:
        server = run.ThreadingHTTPServer(("127.0.0.1", 0), run.ConsoleHandler)
        server.projects = self.projects
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = "127.0.0.1", server.server_address[1]
            home = HTTPConnection(host, port, timeout=2)
            home.request("GET", "/")
            home_response = home.getresponse()
            home_body = home_response.read().decode("utf-8")
            home.close()
            self.assertEqual(200, home_response.status)
            self.assertIn("workSearch", home_body)
            self.assertIn("overviewTaskForm", home_body)

            add_page = HTTPConnection(host, port, timeout=2)
            add_page.request("GET", "/add?project=demo")
            add_response = add_page.getresponse()
            add_body = add_response.read().decode("utf-8")
            add_page.close()
            self.assertEqual(200, add_response.status)
            self.assertIn("taskForm", add_body)

            create = HTTPConnection(host, port, timeout=2)
            create.request(
                "POST",
                "/api/work-items",
                body=json.dumps({"project_id": "demo", "title": "总览创建入口", "goal": "验证既有契约", "sync_git": False}),
                headers={"Content-Type": "application/json"},
            )
            create_response = create.getresponse()
            payload = json.loads(create_response.read().decode("utf-8"))
            create.close()
            self.assertEqual(201, create_response.status)
            self.assertTrue(payload["ok"])
            self.assertEqual("WI-0001", payload["data"]["id"])
            self.assertEqual("1", payload["data"]["owner"])

            forbidden = HTTPConnection(host, port, timeout=2)
            forbidden.request(
                "POST",
                "/api/records",
                body=json.dumps({"project_id": "demo", "work_item": "WI-0001", "type": "gate", "from": "2", "payload": {"verdict": "PASS", "checks": []}}),
                headers={"Content-Type": "application/json"},
            )
            forbidden_response = forbidden.getresponse()
            forbidden_payload = json.loads(forbidden_response.read().decode("utf-8"))
            forbidden.close()
            self.assertEqual(403, forbidden_response.status)
            self.assertEqual("record_api_disabled", forbidden_payload["error"]["code"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
