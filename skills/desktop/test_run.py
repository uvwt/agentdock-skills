#!/usr/bin/env python3
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("run.py")
SPEC = importlib.util.spec_from_file_location("desktop_run", MODULE_PATH)
desktop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(desktop)


class DesktopPreflightTests(unittest.TestCase):
    def test_permission_errors_are_classified_separately(self):
        accessibility = {"ok": False}
        desktop.apply_command_warnings(
            accessibility,
            'execution error: “System Events”遇到一个错误：“osascript”不允许辅助访问。 (-25211)',
        )
        self.assertEqual(accessibility["error_code"], "ACCESSIBILITY_NOT_TRUSTED")

        automation = {"ok": False}
        desktop.apply_command_warnings(
            automation,
            '未获得授权将 Apple 事件发送给 System Events (-1743)',
        )
        self.assertEqual(automation["error_code"], "AUTOMATION_NOT_PERMITTED")

    def test_success_output_cannot_be_reclassified_as_permission_failure(self):
        result = {
            "ok": True,
            "command_ok": True,
            "stdout": "Code\ttrue\tFix issue -25211 and -1743\n",
        }
        desktop.apply_command_warnings(result, result["stdout"])

        self.assertTrue(result["ok"])
        self.assertTrue(result["permission_ok"])
        self.assertNotIn("error_code", result)

    def test_preflight_does_not_treat_applescript_as_accessibility(self):
        calls = []

        def fake_applescript(script, **_kwargs):
            calls.append(script)
            if len(calls) == 1:
                return {"ok": True, "stdout": "111\n"}
            return {
                "ok": False,
                "stdout": '不允许辅助访问。 (-25211)',
                "error_code": "ACCESSIBILITY_NOT_TRUSTED",
            }

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(desktop, "is_darwin", return_value=True),
            patch.object(desktop, "command_exists", return_value=True),
            patch.object(desktop, "artifact_root", return_value=Path(tmp)),
            patch.object(desktop, "run_applescript", side_effect=fake_applescript),
            patch.object(desktop, "run_process", return_value={"ok": False, "stdout": "denied"}),
        ):
            result = desktop.preflight({"check_screenshot": False})

        self.assertFalse(result["ok"])
        self.assertTrue(result["checks"]["applescript_ok"])
        self.assertTrue(result["checks"]["system_events_automation_ok"])
        self.assertFalse(result["checks"]["accessibility_ok"])
        self.assertEqual(result["checks"]["accessibility_status"], "not_granted")
        self.assertEqual(len(calls), 2)

    def test_preflight_marks_unverifiable_accessibility_unknown(self):
        responses = [
            {"ok": True, "stdout": "111\n"},
            {"ok": False, "stdout": "frontmost application unavailable", "error_code": "COMMAND_FAILED"},
        ]

        with (
            patch.object(desktop, "is_darwin", return_value=True),
            patch.object(desktop, "command_exists", return_value=True),
            patch.object(desktop, "run_applescript", side_effect=responses),
        ):
            result = desktop.preflight({"check_screenshot": False})

        self.assertFalse(result["ok"])
        self.assertIsNone(result["checks"]["accessibility_ok"])
        self.assertEqual(result["checks"]["accessibility_status"], "unknown")

    def test_ready_preflight_cleans_probe_screenshot(self):
        def fake_process(argv, **_kwargs):
            Path(argv[-1]).write_bytes(b"probe")
            return {"ok": True, "stdout": ""}

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(desktop, "is_darwin", return_value=True),
            patch.object(desktop, "command_exists", return_value=True),
            patch.object(desktop, "artifact_root", return_value=Path(tmp)),
            patch.object(desktop, "run_process", side_effect=fake_process),
            patch.object(
                desktop,
                "run_applescript",
                side_effect=[
                    {"ok": True, "stdout": "111\n"},
                    {"ok": True, "stdout": "2\n"},
                ],
            ),
        ):
            result = desktop.preflight({})
            remaining = list(Path(tmp).rglob("*.png"))

        self.assertTrue(result["ok"])
        self.assertTrue(result["checks"]["screenshot_ok"])
        self.assertTrue(result["checks"]["system_events_automation_ok"])
        self.assertTrue(result["checks"]["accessibility_ok"])
        self.assertEqual(result["checks"]["accessibility_status"], "ready")
        self.assertEqual(remaining, [])


if __name__ == "__main__":
    unittest.main()
