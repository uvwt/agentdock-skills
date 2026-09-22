#!/usr/bin/env python3
"""Local device identifier state for meituan-coupon-assistant."""

import argparse
import hashlib
import json
import os
import random
import stat
import time
from pathlib import Path

AUTH_KEY = "meituan-c-user-auth"


def auth_file() -> Path:
    value = os.environ.get("MEITUAN_COUPON_AUTH_FILE", "").strip()
    if not value:
        raise RuntimeError("MEITUAN_COUPON_AUTH_FILE is required")
    return Path(value)


def load_auth() -> dict:
    path = auth_file()
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_auth(data: dict) -> None:
    path = auth_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def ensure_device_token() -> str:
    auth = load_auth()
    token_data = auth.get(AUTH_KEY, {})
    device_token = token_data.get("device_token", "")
    if not device_token:
        raw = f"huisheng{int(time.time() * 1000)}{random.randint(0, 1000)}"
        device_token = hashlib.md5(raw.encode("utf-8")).hexdigest()
        token_data["device_token"] = device_token
        auth[AUTH_KEY] = token_data
        save_auth(auth)
    return device_token


def clear_device_token() -> bool:
    auth = load_auth()
    token_data = auth.get(AUTH_KEY, {})
    had_device_token = bool(token_data.get("device_token"))
    token_data["device_token"] = ""
    auth[AUTH_KEY] = token_data
    save_auth(auth)
    return had_device_token


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["get-device-token", "clear-device-token"])
    args = parser.parse_args()
    if args.command == "get-device-token":
        print(json.dumps({"success": True, "device_token": ensure_device_token()}))
        return
    print(json.dumps({"success": True, "device_token_cleared": clear_device_token()}))


if __name__ == "__main__":
    main()
