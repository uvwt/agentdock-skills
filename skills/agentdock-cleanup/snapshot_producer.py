#!/usr/bin/env python3
"""Opt-in managed snapshot producer; never registers existing files or deletes.

Trusted host config selects installed executables. MCP only supplies snapshot
text, never a path to sign. This adapter creates the artifact itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import run as c
from windows_guard import locked_file


def capture(env, workspace, url):
    """One fresh isolated Playwright MCP session, using bounded JSON-line RPC."""
    try:
        node = c.canonical_path(env["CLEANUP_PRODUCER_NODE"])
        cli = c.canonical_path(env["CLEANUP_PRODUCER_CLI"])
        browser = c.canonical_path(env["CLEANUP_PRODUCER_BROWSER"])
        package = json.loads((cli.parent / "package.json").read_text(encoding="utf-8"))
        if package.get("name") != "@playwright/mcp" or package.get("version") != "0.0.82":
            raise ValueError("unsupported producer")
        if not all(p.is_file() for p in (node, cli, browser)):
            raise ValueError("missing executable")
    except (KeyError, OSError, ValueError):
        raise c.CleanupError("PRODUCER_CONFIGURATION_REQUIRED", "Host must configure Node, Playwright MCP 0.0.82 CLI and browser paths")
    # Never share the cleanup signing key, receipt location, arbitrary NODE_OPTIONS
    # or host credentials with the browser/MCP subprocess.
    child_env = {k: v for k, v in os.environ.items()
                 if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "PATHEXT", "LOCALAPPDATA", "USERPROFILE"}}
    session = workspace / ("capture-" + c.secrets.token_hex(16))
    session.mkdir()
    proc = subprocess.Popen([str(node), str(cli), "--headless", "--isolated", "--no-webmcp",
                             "--executable-path", str(browser), "--output-dir", str(session),
                             "--timeout-navigation", "20000", "--timeout-action", "10000"],
                            cwd=session, env=child_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, creationflags=0x08000000)
    messages = queue.Queue(maxsize=32)
    stop = threading.Event()
    def reader():
        try:
            while not stop.is_set():
                line = proc.stdout.readline(c.MAX_INPUT + 1)
                if not line or len(line) > c.MAX_INPUT:
                    break
                messages.put(json.loads(line), timeout=1)
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            try:
                messages.put(None, timeout=1)
            except queue.Full:
                pass
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    # Enforce the deadline even if a failed child stops draining stdin and a
    # pipe write blocks. Killing this owned child releases both pipe operations.
    def expire():
        try:
            proc.kill()
        except OSError:
            pass
    watchdog = threading.Timer(90, expire)
    watchdog.daemon = True
    watchdog.start()
    serial = 0
    deadline = time.monotonic() + 90
    def send(message):
        proc.stdin.write(c.canonical_json(message) + b"\n")
        proc.stdin.flush()
    def rpc(method, params):
        nonlocal serial
        serial += 1
        send({"jsonrpc": "2.0", "id": serial, "method": method, "params": params})
        while True:
            try:
                message = messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise c.CleanupError("MCP_TIMEOUT", "Managed producer timed out")
            if not isinstance(message, dict) or time.monotonic() >= deadline:
                raise c.CleanupError("MCP_FAILED", "Managed producer ended or exceeded its deadline")
            if message.get("id") == serial:
                result = message.get("result")
                if "error" in message or not isinstance(result, dict) or result.get("isError"):
                    raise c.CleanupError("MCP_FAILED", "Managed producer rejected the request")
                return result
    try:
        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "agentdock-snapshot-producer", "version": c.VERSION}})
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        rpc("tools/call", {"name": "browser_navigate", "arguments": {"url": url}})
        result = rpc("tools/call", {"name": "browser_snapshot", "arguments": {}})
        rpc("tools/call", {"name": "browser_close", "arguments": {}})
        content = result.get("content")
        if not isinstance(content, list) or any(not isinstance(item, dict) for item in content):
            raise c.CleanupError("SNAPSHOT_FORMAT_UNSUPPORTED", "Invalid MCP content")
        parts = [item.get("text") for item in content if item.get("type") == "text"]
        if any(not isinstance(part, str) for part in parts):
            raise c.CleanupError("SNAPSHOT_FORMAT_UNSUPPORTED", "Invalid MCP text")
        texts = "\n".join(parts)
        match = re.search(r"```yaml\r?\n(.*?)\r?\n```", texts, re.S)
        if not match:
            raise c.CleanupError("SNAPSHOT_FORMAT_UNSUPPORTED", "Producer did not return an inline YAML snapshot")
        snapshot = match.group(1) + "\n"
    finally:
        stop.set()
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()  # Only the subprocess this invocation created.
            proc.wait(timeout=5)
        thread.join(timeout=2)
        proc.stdout.close()
        watchdog.cancel()
    if proc.returncode != 0:
        raise c.CleanupError("MCP_FAILED", "Producer did not exit cleanly; no receipt issued")
    return snapshot


def produce(env, url):
    if os.name != "nt":
        raise c.CleanupError("UNSUPPORTED_PLATFORM", "Receipt production currently requires Windows handles")
    collector = c.Collector(env, None)
    workspace = collector.roots.get("workspace")
    if not workspace or not collector.key or not collector.receipt_path:
        raise c.CleanupError("PRODUCER_CONFIGURATION_REQUIRED", "Explicit workspace, receipt path and key are required")
    folder = c.canonical_path(str(workspace / ".playwright-mcp"))
    if not folder.is_dir() or not collector.receipt_path.parent.is_dir():
        raise c.CleanupError("PRODUCER_CONFIGURATION_REQUIRED", "Host must pre-create the output and receipt directories")
    name = "snapshot-" + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S") + "-" + str(time.time_ns()) + ".yml"
    path = folder / name
    if collector.is_protected(path, "playwright_snapshot") or collector.is_shared(path):
        raise c.CleanupError("PROTECTED_RESOURCE", "Output overlaps a protected or shared resource")
    if any(c.inside(collector.receipt_path, root) for root in collector.roots.values()):
        raise c.CleanupError("INVALID_CONFIGURATION", "Receipt registry must be outside every scan root")
    snapshot = capture(env, workspace, url)
    if not isinstance(snapshot, str) or len(snapshot.encode("utf-8")) > c.MAX_INPUT:
        raise c.CleanupError("SNAPSHOT_FORMAT_UNSUPPORTED", "Invalid or oversized snapshot")
    payload = snapshot.encode("utf-8")
    # CREATE_NEW refuses existing paths; the same pinned handle owns every write.
    with locked_file(path, create=True, write=True) as opened:
        opened.stream.write(payload)
        opened.stream.flush()
        os.fsync(opened.stream.fileno())
        identity = c.stat_fields(os.fstat(opened.stream.fileno()))
    # Windows finalizes ChangeTime on close. Reopen exclusively and match the
    # newly created identity/content before signing; keep it pinned while recording.
    with locked_file(path) as artifact:
        fp = c.fingerprint(stream=artifact.stream)
        if any(fp[k] != v for k, v in identity.items()) or fp["sha256"] != hashlib.sha256(payload).hexdigest():
            raise c.CleanupError("PLAN_STALE", "New artifact changed before receipt issuance")
        body = {"owner": "agentdock", "producer": "playwright-mcp", "kind": "playwright_snapshot",
                "path": str(path), "closed": True, "fingerprint": fp}
        receipt = {"body": body, "signature": c.mac(collector.key, "ownership", body)}
        registry = c.canonical_path(str(collector.receipt_path))
        # A persistent independent lock serializes read/replace transactions and
        # pins the registry ancestors. It is never deleted or populated with keys.
        lock = c.canonical_path(str(registry) + ".lock")
        with locked_file(lock, create=not lock.exists(), write=True):
            if registry.exists():
                with locked_file(registry) as current:
                    if os.fstat(current.stream.fileno()).st_nlink != 1:
                        raise c.CleanupError("LINK_NOT_ALLOWED", "Receipt registry must have one link")
                    raw = current.stream.read(c.MAX_INPUT + 1)
                    if len(raw) > c.MAX_INPUT:
                        raise c.CleanupError("REGISTRY_FULL", "Registry exceeds the supported bound")
                    data = json.loads(raw)
            else:
                data = {"version": 1, "receipts": []}
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("receipts"), list):
                raise c.CleanupError("INVALID_REGISTRY", "Existing registry is invalid")
            data["receipts"].append(receipt)
            encoded = c.canonical_json(data)
            if len(encoded) > c.MAX_INPUT:
                raise c.CleanupError("REGISTRY_FULL", "Registry exceeds the supported bound")
            pending = registry.parent / (registry.name + ".pending-" + c.secrets.token_hex(16))
            with locked_file(pending, create=True, write=True) as opened:
                opened.stream.write(encoded)
                opened.stream.flush()
                os.fsync(opened.stream.fileno())
            # Same-directory atomic replacement: interruption before commit keeps
            # the previous registry. A failed pending file is retained, not deleted.
            os.replace(pending, registry)
    return {"ok": True, "path": str(path), "receipt_issued": True, "size": len(payload),
            "retention_seconds": c.RETENTION_SECONDS, "deleted": False}


def handle(request, env=None):
    try:
        if not isinstance(request, dict) or set(request) - {"action", "url"}:
            raise c.CleanupError("INVALID_REQUEST", "Only action and optional URL are accepted")
        action = request.get("action", "status")
        if action == "status" and "url" not in request:
            return {"ok": True, "read_only": True, "version": c.VERSION, "producer": "playwright_snapshot"}
        if action != "capture_snapshot":
            raise c.CleanupError("INVALID_REQUEST", "Use status or explicit capture_snapshot")
        url = request.get("url", "about:blank")
        if not isinstance(url, str) or len(url) > 8192 or (url != "about:blank" and urlsplit(url).scheme not in {"http", "https"}):
            raise c.CleanupError("INVALID_REQUEST", "Only about:blank and HTTP(S) page URLs are supported")
        return produce(os.environ if env is None else env, url)
    except c.CleanupError as exc:
        return {"ok": False, "code": exc.code, "message": str(exc)}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return {"ok": False, "code": "PRODUCER_FAILED", "message": "Snapshot or receipt creation failed; existing files were not registered"}


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read(c.MAX_INPUT + 1)
        request = json.loads(raw) if raw.strip() and len(raw) <= c.MAX_INPUT else ({} if not raw.strip() else None)
    except ValueError:
        request = None
    print(json.dumps(handle(request), ensure_ascii=True))
