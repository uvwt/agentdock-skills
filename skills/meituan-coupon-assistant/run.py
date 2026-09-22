#!/usr/bin/env python3
"""AgentDock-safe entrypoint for the Meituan coupon assistant."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_JS = ROOT / "scripts" / "run.js"
SENSITIVE_KEYS = {"token", "device_token", "user_token", "authorization", "cookie"}


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def scrub(value):
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if key.lower() in SENSITIVE_KEYS else scrub(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def data_dir(*, create: bool = True) -> Path:
    value = os.environ.get("SKILL_DATA_DIR", "").strip()
    if not value:
        value = os.environ.get("MEITUAN_COUPON_DATA_DIR", "").strip()
    if not value:
        raise ValueError("SKILL_DATA_DIR or MEITUAN_COUPON_DATA_DIR is required")
    path = Path(value).expanduser().resolve()
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return path


def status() -> dict:
    root = data_dir(create=False)
    credentials = root / "credentials"
    return {
        "ok": True,
        "action": "status",
        "data_dir_configured": True,
        "device_state_present": (credentials / "token.json").exists(),
        "login_state_present": (credentials / "pt_passport_auth.json").exists(),
        "network_accessed": False,
    }


def run_upstream(args: list[str], data_root: Path, timeout: int = 630) -> dict:
    env = os.environ.copy()
    # 上游脚本继续使用它原有的变量；AgentDock 专属适配只留在根入口。
    env["MEITUAN_COUPON_DATA_DIR"] = str(data_root)
    completed = subprocess.run(
        ["node", str(RUN_JS), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    stdout = completed.stdout.strip()
    if not stdout:
        return {"ok": False, "error": "NO_OUTPUT", "message": "Meituan helper returned no JSON output"}
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError:
        return {"ok": False, "error": "INVALID_OUTPUT", "message": "Meituan helper returned invalid JSON"}
    if completed.returncode != 0 and payload.get("ok") is not False:
        return {"ok": False, "error": "COMMAND_FAILED", "message": "Meituan helper exited with an error"}
    return scrub(payload)


def main() -> None:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("stdin must be a JSON object")
        action = request.get("skill_action")
        data_root = None if action == "status" else data_dir()

        if action == "status":
            emit(status())
        elif action == "execute":
            emit(run_upstream(["execute", "--output", "response"], data_root))
        elif action == "auth_complete":
            emit(run_upstream(["auth-complete", "--output", "response"], data_root))
        elif action == "logout":
            emit(run_upstream(["logout"], data_root, timeout=30))
        elif action == "clear_device_token":
            if request.get("confirmed") is not True:
                emit({"ok": False, "error": "CONFIRMATION_REQUIRED", "message": "clear_device_token requires confirmed=true"})
            else:
                emit(run_upstream(["clear-device-token"], data_root, timeout=30))
        else:
            emit({"ok": False, "error": "UNSUPPORTED_ACTION", "message": "Unsupported skill_action"})
    except subprocess.TimeoutExpired:
        emit({"ok": False, "error": "TIMEOUT", "message": "Meituan helper timed out"})
    except Exception as exc:
        emit({"ok": False, "error": "CONFIG_ERROR", "message": str(exc)})


if __name__ == "__main__":
    main()
