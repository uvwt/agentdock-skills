"""Producer tests use newly created fixtures only, never existing browser data."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import run as cleanup


class ProducerTests(unittest.TestCase):
    def setUp(self):
        import snapshot_producer
        self.p = snapshot_producer
        self.tmp = tempfile.TemporaryDirectory(prefix="cleanup-producer-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        (self.workspace / ".playwright-mcp").mkdir()
        self.registry = self.base / "receipts.json"
        self.env = {"CLEANUP_SCOPE_JSON": json.dumps({"workspace": str(self.workspace)}),
                    "CLEANUP_SIGNING_KEY": "31" * 32,
                    "CLEANUP_RECEIPTS_FILE": str(self.registry)}

    def fake_peer(self, source):
        package = self.base / "fake-peer"
        package.mkdir()
        (package / "package.json").write_text('{"name":"@playwright/mcp","version":"0.0.82"}')
        cli = package / "cli.js"
        cli.write_text(source, encoding="utf-8")
        self.env.update(CLEANUP_PRODUCER_NODE=sys.executable, CLEANUP_PRODUCER_CLI=str(cli), CLEANUP_PRODUCER_BROWSER=sys.executable)

    @unittest.skipUnless(os.name == "nt", "Windows producer subprocess")
    def test_child_does_not_inherit_secrets_or_node_options(self):
        self.fake_peer('''import json, os, sys
for line in sys.stdin:
    q = json.loads(line)
    if "id" not in q: continue
    text = "```yaml\\n" + str(any(k.startswith("CLEANUP_") or k == "NODE_OPTIONS" for k in os.environ)) + "\\n```"
    print(json.dumps({"id":q["id"],"result":{"content":[{"type":"text","text":text}]}}), flush=True)
''')
        with patch.dict(os.environ, {"CLEANUP_SIGNING_KEY": "secret-fixture", "NODE_OPTIONS": "untrusted-fixture"}):
            self.assertEqual(self.p.capture(self.env, self.workspace, "about:blank"), "False\n")

    @unittest.skipUnless(os.name == "nt", "Windows producer subprocess")
    def test_blocked_stdin_is_terminated_by_watchdog(self):
        self.fake_peer('''import json, sys, time
q = json.loads(sys.stdin.readline())
print(json.dumps({"id":q["id"],"result":{}}), flush=True)
time.sleep(30)
''')
        timer = threading.Timer
        started = time.monotonic()
        with patch.object(self.p.threading, "Timer", side_effect=lambda delay, fn: timer(0.75, fn)):
            result = self.p.handle({"action": "capture_snapshot", "url": "http://example.invalid/" + "x" * 8000}, self.env)
        self.assertFalse(result["ok"])
        self.assertLess(time.monotonic() - started, 8)
        self.assertFalse(self.registry.exists())

    def test_default_does_not_launch_or_write(self):
        with patch.object(self.p, "capture") as capture:
            self.assertTrue(self.p.handle({}, self.env)["read_only"])
            capture.assert_not_called()
        self.assertFalse(self.registry.exists())

    def test_no_arbitrary_file_registration_or_launch_arguments(self):
        for request in ({"action": "register", "path": "existing.yml"},
                        {"action": "capture_snapshot", "path": "existing.yml"},
                        {"action": "capture_snapshot", "command": ["other"]}):
            self.assertEqual(self.p.handle(request, self.env)["code"], "INVALID_REQUEST")

    def test_unsafe_urls_rejected(self):
        for url in ("file:///C:/secret", "javascript:alert(1)", "data:text/html,test", 123):
            self.assertEqual(self.p.handle({"action": "capture_snapshot", "url": url}, self.env)["code"], "INVALID_REQUEST")

    @unittest.skipUnless(os.name == "nt", "Windows producer handles")
    def test_missing_executables_fail_without_receipt(self):
        self.assertEqual(self.p.handle({"action": "capture_snapshot"}, self.env)["code"], "PRODUCER_CONFIGURATION_REQUIRED")
        self.assertFalse(self.registry.exists())

    @unittest.skipUnless(os.name == "nt", "Windows producer handles")
    def test_registry_inside_scan_root_rejected(self):
        self.env["CLEANUP_RECEIPTS_FILE"] = str(self.workspace / "receipts.json")
        with patch.object(self.p, "capture") as capture:
            self.assertEqual(self.p.handle({"action": "capture_snapshot"}, self.env)["code"], "INVALID_CONFIGURATION")
            capture.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows producer handles")
    def test_hardlinked_registry_not_modified(self):
        self.registry.write_text('{"version":1,"receipts":[]}')
        original = self.registry.read_bytes()
        os.link(self.registry, self.base / "registry-alias.json")
        with patch.object(self.p, "capture", return_value="- heading Fixture"):
            result = self.p.handle({"action": "capture_snapshot"}, self.env)
        self.assertEqual(result["code"], "LINK_NOT_ALLOWED")
        self.assertEqual(self.registry.read_bytes(), original)

    @unittest.skipUnless(os.name == "nt", "Windows producer handles")
    def test_existing_empty_registry_not_overwritten(self):
        self.registry.write_bytes(b"")
        with patch.object(self.p, "capture", return_value="- heading Fixture"):
            self.assertFalse(self.p.handle({"action": "capture_snapshot"}, self.env)["ok"])
        self.assertEqual(self.registry.read_bytes(), b"")

    @unittest.skipUnless(os.name == "nt", "Windows producer handles")
    def test_failed_registry_commit_preserves_previous_receipts(self):
        with patch.object(self.p, "capture", return_value="- heading Fixture"):
            self.assertTrue(self.p.handle({"action": "capture_snapshot"}, self.env)["ok"])
            original = self.registry.read_bytes()
            with patch.object(self.p.os, "replace", side_effect=OSError("simulated disk failure")):
                self.assertFalse(self.p.handle({"action": "capture_snapshot"}, self.env)["ok"])
        self.assertEqual(self.registry.read_bytes(), original)
        self.assertEqual(len(cleanup.Collector(self.env, None).receipts()), 1)

    @unittest.skipUnless(os.name == "nt", "Windows producer handles")
    def test_output_collision_preserves_original(self):
        from datetime import datetime, timezone
        fixed = datetime(2026, 9, 22, tzinfo=timezone.utc)
        with patch.object(self.p, "capture", return_value="- heading Fixture"), patch.object(self.p, "datetime") as clock, patch.object(self.p.time, "time_ns", return_value=123):
            clock.now.return_value = fixed
            first = self.p.handle({"action": "capture_snapshot"}, self.env)
            self.assertTrue(first["ok"], first)
            original = Path(first["path"]).read_bytes()
            self.assertFalse(self.p.handle({"action": "capture_snapshot"}, self.env)["ok"])
        self.assertEqual(Path(first["path"]).read_bytes(), original)
        self.assertEqual(len(json.loads(self.registry.read_text())["receipts"]), 1)

    @unittest.skipUnless(os.name == "nt", "Windows producer handles")
    def test_modification_before_reopen_cannot_get_receipt(self):
        original = self.p.locked_file
        @contextmanager
        def changed(path, **kwargs):
            with original(path, **kwargs) as opened:
                yield opened
            if kwargs.get("create") and str(path).endswith(".yml"):
                Path(path).write_text("replaced content")
        with patch.object(self.p, "capture", return_value="- heading Fixture"), patch.object(self.p, "locked_file", changed):
            self.assertEqual(self.p.handle({"action": "capture_snapshot"}, self.env)["code"], "PLAN_STALE")
        self.assertFalse(self.registry.exists())

    @unittest.skipUnless(os.name == "nt", "Windows producer handles")
    def test_created_snapshot_receipt_and_retention(self):
        with patch.object(self.p, "capture", return_value="- heading \"Fixture\" [level=1]"):
            result = self.p.handle({"action": "capture_snapshot"}, self.env)
        self.assertTrue(result["ok"], result)
        path = Path(result["path"])
        self.assertIn("Fixture", path.read_text())
        collector = cleanup.Collector(self.env, lambda: {"complete": True, "processes": []})
        self.assertIn(cleanup.path_key(path), collector.receipts())
        row = cleanup.handle({"skill_action": "scan", "paths": [str(path)]}, self.env,
                             lambda: {"complete": True, "processes": []})["items"][0]
        self.assertFalse(row["eligible"])
        # Advance the test clock; do not mutate the production file or receipt.
        with patch.object(cleanup.time, "time", return_value=time.time() + 8 * 86400):
            row = cleanup.handle({"skill_action": "scan", "paths": [str(path)]}, self.env,
                                 lambda: {"complete": True, "processes": []})["items"][0]
        self.assertTrue(row["eligible"], row)

    @unittest.skipUnless(os.name == "nt", "Windows producer handles")
    def test_failure_does_not_issue_receipt(self):
        with patch.object(self.p, "capture", side_effect=cleanup.CleanupError("MCP_FAILED", "failed")):
            self.assertEqual(self.p.handle({"action": "capture_snapshot"}, self.env)["code"], "MCP_FAILED")
        self.assertFalse(self.registry.exists())

    @unittest.skipUnless(os.name == "nt", "Windows producer handles")
    def test_existing_registry_preserved_and_appended(self):
        with patch.object(self.p, "capture", return_value="- heading \"Fixture\""):
            a = self.p.handle({"action": "capture_snapshot"}, self.env)
            b = self.p.handle({"action": "capture_snapshot"}, self.env)
        self.assertTrue(a["ok"] and b["ok"], (a, b))
        self.assertNotEqual(a["path"], b["path"])
        self.assertEqual(len(json.loads(self.registry.read_text())["receipts"]), 2)

    @unittest.skipUnless(os.name == "nt", "Windows producer handles")
    def test_corrupt_registry_is_not_overwritten(self):
        self.registry.write_text("not JSON")
        with patch.object(self.p, "capture", return_value="- heading \"Fixture\""):
            result = self.p.handle({"action": "capture_snapshot"}, self.env)
        self.assertFalse(result["ok"])
        self.assertEqual(self.registry.read_text(), "not JSON")


if __name__ == "__main__":
    unittest.main(verbosity=2)
