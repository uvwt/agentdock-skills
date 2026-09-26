from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / "run.py").exists():
    spec = importlib.util.spec_from_file_location("cleanup_run", ROOT / "run.py")
    c = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = c
    spec.loader.exec_module(c)
else:
    c = None


class ImplementationExists(unittest.TestCase):
    def test_implementation_exists(self):
        self.assertIsNotNone(c, "cleanup helper has not been implemented")


@unittest.skipIf(c is None, "implementation not yet present")
class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cleanup-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.workspace = self.base / "workspace"
        self.runtime = self.base / "runtime-tmp"
        self.npm = self.base / "npm-cache"
        self.temp = self.base / "shared-temp"
        self.logs = self.base / "logs"
        for p in (self.workspace, self.runtime, self.npm, self.temp, self.logs):
            p.mkdir()
        (self.workspace / ".playwright-mcp").mkdir()
        self.registry = self.base / "producer-receipts.json"
        self.key = bytes.fromhex("19" * 32)
        self.scope = {"workspace": str(self.workspace), "runtime_tmp": str(self.runtime),
                      "npm_cache": str(self.npm), "temp": str(self.temp), "logs": str(self.logs),
                      "protected": [str(self.base / "project-source")]}
        self.env = {"CLEANUP_SCOPE_JSON": json.dumps(self.scope),
                    "CLEANUP_SIGNING_KEY": self.key.hex(),
                    "CLEANUP_RECEIPTS_FILE": str(self.registry)}
        self.receipts = []
        self.registry.write_text('{"version":1,"receipts":[]}', encoding="utf-8")
        self.proc = {"complete": True, "processes": []}

    def handle(self, request):
        return c.handle(request, env=self.env, processes=lambda: copy.deepcopy(self.proc))

    def create(self, path=None, owned=True, kind="runtime_temp"):
        path = path or self.runtime / ("agentdock-" + "a" * 32 + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic disposable fixture\n")
        old = time.time() - 9 * 86400
        os.utime(path, (old, old))
        if owned:
            body = {"owner": "agentdock", "producer": "agentdock" if kind == "runtime_temp" else "playwright-mcp",
                    "kind": kind, "path": str(path), "closed": True,
                    "fingerprint": c.fingerprint(path)}
            signature = hmac.new(self.key, b"ownership\0" + c.canonical_json(body), hashlib.sha256).hexdigest()
            self.receipts.append({"body": body, "signature": signature})
            self.registry.write_text(json.dumps({"version": 1, "receipts": self.receipts}), encoding="utf-8")
        return path

    def row(self, path):
        return self.handle({"skill_action": "scan", "paths": [str(path)]})["items"][0]

    def plan(self, path):
        return self.handle({"skill_action": "plan", "paths": [str(path)]})["plan"]

    def clean_request(self, plan):
        return {"skill_action": "clean", "plan": plan, "plan_id": plan["plan_id"],
                "confirmation": {"plan_id": plan["plan_id"], "phrase": "DELETE_AGENTDOCK_RUNTIME_ARTIFACTS"}}

    def test_readonly_defaults_and_unconfigured_status(self):
        self.assertTrue(c.handle({}, env={})["read_only"])
        self.assertFalse(c.handle({}, env={})["delete_ready"])

    def test_allowlisted_owned_file(self):
        row = self.row(self.create())
        self.assertEqual(row["category"], "SAFE_TEMP")
        self.assertTrue(row["eligible"])
        self.assertGreater(row["reclaimable_bytes"], 0)

    def test_unknown_file_even_inside_runtime_root_is_user_artifact(self):
        path = self.create(self.runtime / "work.txt", owned=False)
        self.assertEqual(self.row(path)["category"], "USER_ARTIFACT")
        self.assertEqual(self.plan(path)["items"], [])

    def test_name_alone_never_proves_ownership(self):
        path = self.create(owned=False)
        self.assertFalse(self.row(path)["eligible"])
        self.assertEqual(self.row(path)["code"], "OWNERSHIP_UNPROVEN")

    def test_outside_root_even_with_receipt(self):
        path = self.create(self.base / "outside" / ("agentdock-" + "a" * 32 + ".tmp"))
        self.assertEqual(self.row(path)["code"], "OUTSIDE_CLEANUP_BOUNDARY")
        self.assertEqual(self.plan(path)["items"], [])

    def test_protected_files_override_receipt(self):
        for name in ("config.json", "secret.tmp", "auth.json", "main.py", "mcp.json", "runtime.json"):
            with self.subTest(name=name):
                row = self.row(self.create(self.runtime / name))
                self.assertEqual(row["category"], "PROTECTED")
                self.assertFalse(row["eligible"])

    def test_protected_ancestor_overrides_allowlist(self):
        path = self.create(self.workspace / ".agentdock" / "tmp" / ("agentdock-" + "a" * 32 + ".tmp"))
        self.assertEqual(self.row(path)["category"], "PROTECTED")

    def test_protected_configured_root_overrides(self):
        self.scope["protected"].append(str(self.runtime))
        self.env["CLEANUP_SCOPE_JSON"] = json.dumps(self.scope)
        self.assertEqual(self.row(self.create())["category"], "PROTECTED")

    def test_shared_cache_never_eligible_even_with_receipt_and_overlapping_root(self):
        self.scope["runtime_tmp"] = str(self.npm / "_npx")
        self.env["CLEANUP_SCOPE_JSON"] = json.dumps(self.scope)
        path = self.create(self.npm / "_npx" / ("agentdock-" + "a" * 32 + ".tmp"))
        self.assertFalse(self.row(path)["eligible"])
        self.assertEqual(self.row(path)["code"], "SHARED_RESOURCE")

    def test_npx_running_mcp_is_in_use(self):
        path = self.npm / "_npx" / "abc123"
        (path / "node_modules").mkdir(parents=True)
        self.proc["processes"] = [{"pid": 123, "name": "node.exe", "command_line": f'node "{path}/node_modules/@playwright/mcp/cli.js"'}]
        row = self.row(path)
        self.assertTrue(row["in_use"])
        self.assertFalse(row["eligible"])

    def test_running_profile_is_in_use(self):
        for name in ("playwright_chromiumdev_profile-test", "ms-playwright-mcp"):
            path = self.temp / name
            path.mkdir()
            self.proc["processes"] = [{"pid": 321, "name": "chrome.exe", "command_line": f'chrome --user-data-dir="{path}"'}]
            row = self.row(path)
            self.assertTrue(row["in_use"])
            self.assertEqual(row["category"], "CONDITIONAL")
            self.assertFalse(row["eligible"])

    def test_inactive_profile_remains_report_only(self):
        path = self.temp / "playwright_chromiumdev_profile-test"
        path.mkdir()
        row = self.row(path)
        self.assertEqual(row["code"], "DIRECTORY_REPORT_ONLY")
        self.assertFalse(row["eligible"])

    def test_unknown_process_visibility_fails_closed(self):
        self.proc["complete"] = False
        row = self.row(self.create())
        self.assertIsNone(row["in_use"])
        self.assertFalse(row["eligible"])
        self.assertEqual(row["code"], "PROCESS_STATE_UNKNOWN")

    def test_process_command_lines_are_not_in_output(self):
        self.proc["processes"] = [{"pid": 555, "name": "node.exe", "command_line": "node --token=NEVER_PRINT_THIS"}]
        self.assertNotIn("NEVER_PRINT_THIS", json.dumps(self.row(self.create())))

    def test_plan_and_scan_do_not_write_or_delete(self):
        path = self.create()
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.rglob("*") if p.is_file()}
        self.row(path)
        plan = self.plan(path)
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(len(plan["items"]), 1)

    def test_confirmation_requires_exact_plan_and_phrase(self):
        plan = self.plan(self.create())
        for confirmation in (None, True, {"plan_id": "wrong", "phrase": "DELETE_AGENTDOCK_RUNTIME_ARTIFACTS"}, {"plan_id": plan["plan_id"], "phrase": "yes"}):
            req = self.clean_request(plan)
            req["confirmed"] = True
            req["confirmation"] = confirmation
            self.assertEqual(self.handle(req)["error"]["code"], "CONFIRMATION_REQUIRED")

    def test_forged_plan_cannot_delete(self):
        path = self.create()
        plan = self.plan(path)
        plan["body"]["expires_at"] += 3600
        self.assertEqual(self.handle(self.clean_request(plan))["error"]["code"], "INVALID_PLAN")
        self.assertTrue(path.exists())

    def test_stale_plan_changed_file(self):
        path = self.create()
        plan = self.plan(path)
        path.write_bytes(b"user edited this")
        result = self.handle(self.clean_request(plan))
        self.assertEqual(result["results"][0]["code"], "PLAN_STALE")
        self.assertTrue(path.exists())

    def test_stale_plan_same_size_and_restored_mtime(self):
        path = self.create()
        plan = self.plan(path)
        st = path.stat()
        path.write_bytes(b"x" * st.st_size)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(self.handle(self.clean_request(plan))["results"][0]["code"], "PLAN_STALE")

    def test_now_in_use_after_plan(self):
        path = self.create()
        plan = self.plan(path)
        self.proc["processes"] = [{"pid": 5, "name": "node.exe", "command_line": f'node "{path}"'}]
        self.assertEqual(self.handle(self.clean_request(plan))["results"][0]["code"], "RESOURCE_NOW_IN_USE")
        self.assertTrue(path.exists())

    def test_path_traversal_rejected(self):
        path = self.create()
        traversal = str(path.parent / ".." / path.parent.name / path.name)
        self.assertEqual(self.row(traversal)["code"], "OUTSIDE_CLEANUP_BOUNDARY")

    def test_prefix_sibling_is_outside(self):
        path = self.create(self.base / "runtime-tmp-other" / ("agentdock-" + "a" * 32 + ".tmp"))
        self.assertEqual(self.row(path)["code"], "OUTSIDE_CLEANUP_BOUNDARY")

    def test_hardlink_rejected(self):
        path = self.create()
        os.link(path, self.base / "user-hardlink")
        self.assertEqual(self.row(path)["code"], "LINK_NOT_ALLOWED")

    def test_invalid_receipt_signature_fails_closed(self):
        path = self.create()
        data = json.loads(self.registry.read_text())
        data["receipts"][0]["signature"] = "0" * 64
        self.registry.write_text(json.dumps(data))
        self.assertEqual(self.row(path)["code"], "OWNERSHIP_UNPROVEN")

    def test_malformed_receipt_shapes_fail_closed(self):
        path = self.create()
        for value in ([], None, 1, "bad", {"version": 1, "receipts": [None]},
                      {"version": 1, "receipts": [{"body": []}]}):
            with self.subTest(value=value):
                self.registry.write_text(json.dumps(value), encoding="utf-8")
                row = self.row(path)
                self.assertFalse(row["eligible"])
                self.assertEqual(row["code"], "OWNERSHIP_UNPROVEN")

    def test_profile_subtree_cannot_be_relabelled_runtime_tmp(self):
        for name in ("playwright_chromiumdev_profile-unit", "ms-playwright-mcp"):
            profile = self.temp / name
            self.scope["runtime_tmp"] = str(profile / "runtime")
            self.env["CLEANUP_SCOPE_JSON"] = json.dumps(self.scope)
            path = self.create(profile / "runtime" / ("agentdock-" + "b" * 32 + ".tmp"))
            row = self.row(path)
            self.assertEqual(row["code"], "DIRECTORY_REPORT_ONLY")
            self.assertFalse(row["eligible"])
            self.assertEqual(self.plan(path)["items"], [])

    def test_scan_requires_explicit_host_scope(self):
        result = c.handle({"skill_action": "scan"}, env={}, processes=lambda: self.proc)
        self.assertEqual(result["error"]["code"], "CONFIGURATION_REQUIRED")

    def test_logs_statistics_only(self):
        path = self.create(self.logs / "agentdock.log", owned=False)
        row = self.row(path)
        self.assertEqual(row["recommended_action"], "recommend_retention_rotation")
        self.assertFalse(row["eligible"])

    def test_expired_plan_rejected(self):
        plan = self.plan(self.create())
        with patch.object(c.time, "time", return_value=plan["body"]["expires_at"] + 1):
            self.assertEqual(self.handle(self.clean_request(plan))["error"]["code"], "PLAN_STALE")

    def test_changed_scope_invalidates_plan(self):
        plan = self.plan(self.create())
        self.scope["protected"].append(str(self.runtime))
        self.env["CLEANUP_SCOPE_JSON"] = json.dumps(self.scope)
        self.assertEqual(self.handle(self.clean_request(plan))["error"]["code"], "PLAN_STALE")

    def test_dry_run_never_deletes(self):
        path = self.create()
        req = self.clean_request(self.plan(path))
        req["dry_run"] = True
        result = self.handle(req)
        self.assertTrue(result["read_only"])
        self.assertTrue(path.exists())

    def test_missing_file_is_stale(self):
        path = self.create()
        plan = self.plan(path)
        path.rename(path.with_suffix(".retained"))
        self.assertEqual(self.handle(self.clean_request(plan))["results"][0]["code"], "PLAN_STALE")

    def test_playwright_snapshot_with_receipt(self):
        path = self.create(self.workspace / ".playwright-mcp" / "page-2026-01-01T12-00-00-000Z.yml", kind="playwright_snapshot")
        self.assertTrue(self.row(path)["eligible"])

    def test_new_file_is_not_expired(self):
        path = self.create()
        os.utime(path, None)
        self.assertFalse(self.row(path)["eligible"])

    def test_invalid_input_and_unknown_action(self):
        for req in ([], None, "scan"):
            self.assertEqual(self.handle(req)["error"]["code"], "INVALID_INPUT")
        self.assertEqual(self.handle({"skill_action": "delete_all"})["error"]["code"], "UNKNOWN_ACTION")

    @unittest.skipUnless(os.name == "nt" and os.environ.get("CLEANUP_TEST_ALLOW_DELETE") == "1", "Requires separate approval for disposable fixture deletion")
    def test_delete_and_verify_and_replay(self):
        path = self.create()
        req = self.clean_request(self.plan(path))
        result = self.handle(req)
        self.assertEqual(result["results"][0]["code"], "DELETED", result)
        self.assertTrue(result["results"][0]["verified"])
        self.assertFalse(path.exists())
        self.assertEqual(self.handle(req)["results"][0]["code"], "PLAN_STALE")

    @unittest.skipUnless(os.name == "nt", "Windows handle integration")
    def test_open_file_handle_blocks_delete(self):
        path = self.create()
        req = self.clean_request(self.plan(path))
        # Assert safety even if a regression unexpectedly obtains an exclusive handle.
        with patch("windows_guard.LockedFile.delete", side_effect=AssertionError("Must not reach deletion")):
            with path.open("rb"):
                result = self.handle(req)
        self.assertIn(result["results"][0]["code"], {"RESOURCE_NOW_IN_USE", "DELETE_DENIED"})
        self.assertTrue(path.exists())

    @unittest.skipUnless(os.name == "nt", "Windows dispatch simulation")
    def test_clean_orchestration_and_verify_failure_without_deletion(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        path = self.create()
        req = self.clean_request(self.plan(path))
        delete = Mock()
        @contextmanager
        def simulated_lock(raw):
            with open(raw, "rb") as stream:
                yield SimpleNamespace(stream=stream, delete=delete)
        with patch("windows_guard.locked_file", simulated_lock):
            result = self.handle(req)
            self.assertEqual(result["results"][0]["code"], "VERIFY_FAILED")
            self.assertEqual(result["reclaimed_bytes"], 0)
            self.assertTrue(path.exists())
            with patch.object(c, "verify_absent", return_value=True):
                result = self.handle(req)
                self.assertEqual(result["results"][0]["code"], "DELETED")
                self.assertTrue(result["results"][0]["verified"])
        self.assertEqual(delete.call_count, 2)
        self.assertTrue(path.exists())

    def test_replacement_after_plan_retains_new_file(self):
        path = self.create()
        req = self.clean_request(self.plan(path))
        path.rename(path.with_suffix(".old"))
        path.write_bytes(b"replacement")
        self.assertEqual(self.handle(req)["results"][0]["code"], "PLAN_STALE")
        self.assertEqual(path.read_bytes(), b"replacement")

    def test_receipt_revocation_invalidates_plan(self):
        path = self.create()
        req = self.clean_request(self.plan(path))
        self.registry.write_text('{"version":1,"receipts":[]}', encoding="utf-8")
        self.assertEqual(self.handle(req)["results"][0]["code"], "PLAN_STALE")

    def test_config_cannot_be_injected_in_request(self):
        result = self.handle({"skill_action": "plan", "scope": self.scope, "receipts": self.receipts})
        self.assertEqual(result["error"]["code"], "INVALID_INPUT")

    def test_process_failure_after_plan(self):
        path = self.create()
        req = self.clean_request(self.plan(path))
        self.proc["complete"] = False
        self.assertEqual(self.handle(req)["results"][0]["code"], "PROCESS_STATE_UNKNOWN")

    def test_real_json_interface_from_package_root(self):
        for payload in (b'[]', b'{bad json', b'{"skill_action":"unsupported"}', b'{"skill_action":[]}'):
            result = subprocess.run([sys.executable, "-B", "run.py"], cwd=ROOT, input=payload, capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(json.loads(result.stdout)["ok"])
            self.assertEqual(result.stderr, b"")

    @unittest.skipUnless(os.name == "nt", "Windows junction integration")
    def test_junction_cannot_escape(self):
        outside = self.base / "elsewhere"
        outside.mkdir()
        target = self.create(outside / ("agentdock-" + "a" * 32 + ".tmp"))
        link = self.runtime / "redirect"
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                   "New-Item -ItemType Junction -Path $env:CLEANUP_TEST_LINK -Target $env:CLEANUP_TEST_TARGET | Out-Null"]
        env = dict(os.environ, CLEANUP_TEST_LINK=str(link), CLEANUP_TEST_TARGET=str(outside))
        result = subprocess.run(command, env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            self.assertEqual(self.row(link / target.name)["code"], "OUTSIDE_CLEANUP_BOUNDARY")
            self.assertTrue(target.exists())
        finally:
            os.rmdir(link)  # Remove only this test junction, never its target.

    @unittest.skipUnless(os.name == "nt", "Windows path syntax")
    def test_windows_aliases_and_streams_rejected(self):
        path = self.create()
        for value in (str(path) + ":hidden", "\\\\?\\" + str(path), str(path) + ".", str(path) + " "):
            self.assertEqual(self.row(value)["code"], "OUTSIDE_CLEANUP_BOUNDARY")
        self.assertTrue(self.row(str(path).upper())["eligible"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
