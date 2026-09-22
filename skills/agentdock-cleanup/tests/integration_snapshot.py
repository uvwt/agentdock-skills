"""Explicit opt-in real Playwright -> receipt -> confirmed cleanup fixture test."""
import http.server
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run as cleanup
import snapshot_producer


def main():
    if os.environ.get("CLEANUP_TEST_ALLOW_DELETE") != "1" or os.name != "nt":
        raise SystemExit("Requires explicit isolated deletion-test authorization and Windows")
    marker = "AgentDock snapshot fixture " + secrets.token_hex(8)
    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = ("<!doctype html><title>Fixture</title><h1>" + marker + "</h1>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="cleanup-e2e-test-") as folder:
            root = Path(folder).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / ".playwright-mcp").mkdir()
            env = {k: v for k, v in os.environ.items() if k.startswith("CLEANUP_PRODUCER_")}
            env.update(CLEANUP_SCOPE_JSON=json.dumps({"workspace": str(workspace)}),
                       CLEANUP_SIGNING_KEY=secrets.token_hex(32), CLEANUP_RECEIPTS_FILE=str(root / "receipts.json"))
            generated = snapshot_producer.handle({"action": "capture_snapshot", "url": f"http://127.0.0.1:{server.server_port}/"}, env)
            assert generated.get("ok"), generated
            target = Path(generated["path"])
            assert target.parent == workspace / ".playwright-mcp"
            assert marker in target.read_text(encoding="utf-8")
            request = {"skill_action": "scan", "paths": [str(target)]}
            fresh = cleanup.handle(request, env)
            assert not fresh["items"][0]["eligible"], fresh
            # Only move the test clock. Production mtime, receipt and seven-day
            # retention are untouched; actual process detection remains active.
            future = time.time() + 8 * 86400
            with patch.object(cleanup.time, "time", return_value=future):
                scan = cleanup.handle(request, env)
                assert scan["items"][0]["eligible"], scan
                plan = cleanup.handle({"skill_action": "plan", "paths": [str(target)]}, env)["plan"]
                assert target.exists()
                clean_request = {"skill_action": "clean", "plan": plan, "plan_id": plan["plan_id"],
                                 "confirmation": {"plan_id": plan["plan_id"], "phrase": "DELETE_AGENTDOCK_RUNTIME_ARTIFACTS"}}
                dry = cleanup.handle(dict(clean_request, dry_run=True), env)
                assert dry["ok"] and target.exists(), dry
                result = cleanup.handle(clean_request, env)
                assert result["ok"] and result["results"][0]["verified"] and not target.exists(), result
            print(json.dumps({"ok": True, "real_browser_snapshot": True, "receipt_verified": True,
                              "fresh_retained": True, "clock_only_expiry": True,
                              "real_process_checks": True, "dry_run_preserved": True,
                              "deleted_fixture_verified": True, "reclaimed_bytes": result["reclaimed_bytes"]}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
