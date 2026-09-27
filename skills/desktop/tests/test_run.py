import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("desktop_run", Path(__file__).resolve().parents[1] / "run.py")
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, replacement in [
            ("is_darwin", lambda: True),
            ("command_exists", lambda name: True),
            ("artifact_root", lambda: self.root),
        ]:
            mock = patch.object(desktop, name, replacement)
            mock.start()
            self.addCleanup(mock.stop)

    def screenshot(self, argv, **kwargs):
        Path(argv[-1]).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 100, 80))
        return {"ok": True}

    def test_real_ax_failure_cannot_be_hidden_by_process_enumeration(self):
        def applescript(script, **kwargs):
            if kwargs["operation"] == "accessibility_preflight":
                self.assertIn("name of every window", script)
                return {"ok": False, "stdout": "not allowed assistive access (-25211)"}
            return {"ok": True, "stdout": "123"}

        with patch.object(desktop, "run_process", self.screenshot), patch.object(desktop, "run_applescript", applescript):
            result = desktop.preflight({})
        self.assertFalse(result["ok"])
        self.assertTrue(result["checks"]["applescript_ok"])
        self.assertFalse(result["checks"]["accessibility_ok"])
        self.assertEqual(result["readiness"]["accessibility"], "failed")
        self.assertIn("older executable identity", " ".join(result["warnings"]))
        self.assertFalse(list(self.root.rglob("*.png")))

    def test_success_checks_capabilities_and_removes_capture(self):
        with patch.object(desktop, "run_process", self.screenshot), patch.object(desktop, "run_applescript", return_value={"ok": True, "stdout": "private window title"}):
            result = desktop.preflight({})
        self.assertTrue(result["ok"])
        self.assertTrue(all(value == "passed" for value in result["readiness"].values()))
        self.assertNotIn("private window title", str(result))
        self.assertFalse(list(self.root.rglob("*.png")))

    def test_skipped_checks_are_unverified(self):
        with patch.object(desktop, "run_process") as process, patch.object(desktop, "run_applescript") as script:
            result = desktop.preflight({"check_screenshot": False, "check_applescript": False, "check_accessibility": False})
        self.assertFalse(result["ok"])
        self.assertTrue(all(value == "not_checked" for value in result["readiness"].values()))
        for key in ["screenshot_ok", "applescript_ok", "accessibility_ok"]:
            self.assertIsNone(result["checks"][key])
        process.assert_not_called()
        script.assert_not_called()

    def test_missing_or_invalid_image_cannot_pass(self):
        for contents in [None, b"not a PNG", b"\x89PNG\r\n\x1a\n" + b"\0" * 16]:
            def capture(argv, **kwargs):
                if contents is not None:
                    Path(argv[-1]).write_bytes(contents)
                return {"ok": True}

            with self.subTest(contents=contents), patch.object(desktop, "run_process", capture), patch.object(desktop, "run_applescript", return_value={"ok": True}):
                result = desktop.preflight({})
                self.assertFalse(result["ok"])
                self.assertFalse(result["checks"]["screenshot_ok"])
                self.assertFalse(list(self.root.rglob("*.png")))

    def test_missing_dependency_returns_structured_failure(self):
        with patch.object(desktop.subprocess, "run", side_effect=FileNotFoundError("missing command")):
            result = desktop.run_process(["missing-command"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "COMMAND_START_FAILED")

    def test_unsupported_os_keeps_capabilities_unchecked(self):
        with patch.object(desktop, "is_darwin", return_value=False), patch.object(desktop, "run_process") as process:
            result = desktop.preflight({})
        self.assertFalse(result["ok"])
        self.assertTrue(all(value == "not_checked" for value in result["readiness"].values()))
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
