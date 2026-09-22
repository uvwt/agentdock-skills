from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "run.py"


def load_runner_module():
    spec = importlib.util.spec_from_file_location("meituan_coupon_runner", RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load Skill runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunContractTests(unittest.TestCase):
    def invoke(
        self,
        request: dict,
        *,
        skill_data_dir: Path | None = None,
        fallback_data_dir: Path | None = None,
    ) -> dict:
        env = os.environ.copy()
        env.pop("SKILL_DATA_DIR", None)
        env.pop("MEITUAN_COUPON_DATA_DIR", None)
        if skill_data_dir is not None:
            env["SKILL_DATA_DIR"] = str(skill_data_dir)
        if fallback_data_dir is not None:
            env["MEITUAN_COUPON_DATA_DIR"] = str(fallback_data_dir)
        completed = subprocess.run(
            ["python3", str(RUNNER)],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            env=env,
            cwd=ROOT,
            check=True,
        )
        return json.loads(completed.stdout)

    def test_status_uses_fallback_without_creating_directory(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "state-not-created"
            result = self.invoke({"skill_action": "status"}, fallback_data_dir=data_dir)
            self.assertTrue(result["ok"])
            self.assertFalse(result["network_accessed"])
            self.assertFalse(result["device_state_present"])
            self.assertFalse(result["login_state_present"])
            self.assertFalse(data_dir.exists())

    def test_skill_data_dir_takes_precedence_over_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            skill_data_dir = Path(parent) / "agentdock-data"
            fallback_data_dir = Path(parent) / "fallback-data"
            credentials = skill_data_dir / "credentials"
            credentials.mkdir(parents=True)
            (credentials / "token.json").write_text("{}", encoding="utf-8")
            fallback_credentials = fallback_data_dir / "credentials"
            fallback_credentials.mkdir(parents=True)
            (fallback_credentials / "pt_passport_auth.json").write_text("{}", encoding="utf-8")

            result = self.invoke(
                {"skill_action": "status"},
                skill_data_dir=skill_data_dir,
                fallback_data_dir=fallback_data_dir,
            )
            self.assertTrue(result["device_state_present"])
            self.assertFalse(result["login_state_present"])

    def test_clear_device_token_requires_confirmation_with_skill_data_dir_only(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "state"
            result = self.invoke({"skill_action": "clear_device_token"}, skill_data_dir=data_dir)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "CONFIRMATION_REQUIRED")
            self.assertTrue(data_dir.is_dir())

    def test_upstream_receives_selected_directory_through_existing_variable(self) -> None:
        runner = load_runner_module()
        selected = Path(tempfile.gettempdir()) / "meituan-selected-data"
        completed = mock.Mock(stdout='{"ok":true}\n', stderr="", returncode=0)

        with mock.patch.object(runner.subprocess, "run", return_value=completed) as run:
            result = runner.run_upstream(["init"], selected, timeout=12)

        self.assertTrue(result["ok"])
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["env"]["MEITUAN_COUPON_DATA_DIR"], str(selected))
        self.assertEqual(kwargs["timeout"], 12)


if __name__ == "__main__":
    unittest.main()
