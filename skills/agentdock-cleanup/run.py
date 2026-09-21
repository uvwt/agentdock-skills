#!/usr/bin/env python3
"""Conservative AgentDock artifact GC. No writes during status/scan/plan."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time

VERSION = "0.1.0"
POLICY_VERSION = 1
RETENTION_SECONDS = 7 * 86400
PLAN_SECONDS = 900
MAX_INPUT = 2 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_WALK = 20000
MAX_ITEMS = 1000
ROLES = {"workspace", "runtime_tmp", "temp", "profile_cache", "npm_cache", "logs"}
PROTECTED_PARTS = {
    ".agentdock", ".codex", ".git", ".ssh", "skill-store", "skills", "core-skills",
    "oauth", "secrets", "credentials", "dpapi", "tasks", "task-state", "config",
    "configuration", "mcp", "bin", "node_modules", "documents", "downloads",
    "desktop", "user data", "src", "source", "projects",
}
PROTECTED_SUFFIXES = {".py", ".pyc", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs",
                      ".c", ".h", ".cpp", ".cs", ".java", ".exe", ".dll", ".ps1",
                      ".bat", ".cmd", ".pem", ".key", ".db", ".sqlite", ".json",
                      ".toml", ".ini", ".env", ".yaml", ".yml"}
PROTECTED_NAME = re.compile(r"(?:^|[._-])(auth|oauth|secret|credential|dpapi|token|config|settings|task|mcp|runtime)(?:[._-]|$)", re.I)
PLAYWRIGHT_NAMES = {
    "playwright_snapshot": re.compile(r"(?:page|snapshot)-\d{4}-\d{2}-\d{2}T[0-9TZ._-]+\.ya?ml$", re.I),
    "playwright_console": re.compile(r"console-\d{4}-\d{2}-\d{2}T[0-9TZ._-]+\.log$", re.I),
    "playwright_screenshot": re.compile(r"(?:page|screenshot)-\d{4}-\d{2}-\d{2}T[0-9TZ._-]+\.png$", re.I),
}
RUNTIME_NAME = re.compile(r"agentdock-[a-f0-9]{32}\.tmp$", re.I)


class CleanupError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def mac(key, domain, value):
    return hmac.new(key, domain.encode("ascii") + b"\0" + canonical_json(value), hashlib.sha256).hexdigest()


def path_key(path):
    return os.path.normcase(str(path))


def inside(path, root):
    try:
        return os.path.commonpath([path_key(path), path_key(root)]) == path_key(root)
    except ValueError:
        return False


def canonical_path(value):
    """Reject ambiguous syntax and every reparse component, then resolve casing."""
    if not isinstance(value, str) or not value or "\0" in value:
        raise CleanupError("OUTSIDE_CLEANUP_BOUNDARY", "An absolute unambiguous path is required")
    if ".." in value.replace("\\", "/").split("/"):
        raise CleanupError("OUTSIDE_CLEANUP_BOUNDARY", "Parent traversal is prohibited")
    p = Path(value)
    if not p.is_absolute():
        raise CleanupError("OUTSIDE_CLEANUP_BOUNDARY", "Relative paths are prohibited")
    if os.name == "nt":
        if value.startswith(("\\\\", "//")) or ":" in value[2:]:
            raise CleanupError("OUTSIDE_CLEANUP_BOUNDARY", "UNC, device paths and alternate streams are prohibited")
        for component in p.parts[1:]:
            if component.endswith((".", " ")) or re.search(r'[<>"|?*]', component):
                raise CleanupError("OUTSIDE_CLEANUP_BOUNDARY", "Ambiguous Windows path")
            if re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", component, re.I):
                raise CleanupError("OUTSIDE_CLEANUP_BOUNDARY", "Device names are prohibited")
    for component in reversed((p, *p.parents)):
        try:
            s = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(s.st_mode) or getattr(s, "st_file_attributes", 0) & 0x400:
            raise CleanupError("OUTSIDE_CLEANUP_BOUNDARY", "Links and reparse points are prohibited")
    return p.resolve(strict=False)


def stat_fields(s):
    return {"device": s.st_dev, "inode": s.st_ino, "size": s.st_size,
            "mtime_ns": s.st_mtime_ns}


def fingerprint(path=None, stream=None):
    if stream is None:
        path = canonical_path(str(path))
        s = path.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
            raise CleanupError("LINK_NOT_ALLOWED", "Only ordinary single-link files are eligible")
        if s.st_size > MAX_FILE_BYTES:
            raise CleanupError("FILE_TOO_LARGE", "File exceeds the first-version verification limit")
        with path.open("rb") as f:
            result = fingerprint(stream=f)
        if stat_fields(path.lstat()) != {k: result[k] for k in stat_fields(s)}:
            raise CleanupError("PLAN_STALE", "File changed while being measured")
        return result
    before = os.fstat(stream.fileno())
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise CleanupError("LINK_NOT_ALLOWED", "Only ordinary single-link files are eligible")
    if before.st_size > MAX_FILE_BYTES:
        raise CleanupError("FILE_TOO_LARGE", "File exceeds verification limit")
    stream.seek(0)
    sha = hashlib.sha256()
    total = 0
    while chunk := stream.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_FILE_BYTES:
            raise CleanupError("PLAN_STALE", "File grew during verification")
        sha.update(chunk)
    after = os.fstat(stream.fileno())
    if stat_fields(before) != stat_fields(after) or total != after.st_size:
        raise CleanupError("PLAN_STALE", "File changed during verification")
    if os.name == "nt":
        from windows_guard import change_time
        changed = change_time(stream)
    else:
        changed = after.st_ctime_ns
    return dict(stat_fields(after), change_time_ns=changed, sha256=sha.hexdigest())


def process_snapshot():
    if os.name != "nt":
        return {"complete": False, "processes": [], "code": "PLATFORM_UNSUPPORTED"}
    command = (
        "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
        "$p=@(Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine); "
        "ConvertTo-Json -InputObject $p -Compress"
    )
    try:
        result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, timeout=20, creationflags=0x08000000)
        if result.returncode or len(result.stdout) > 8 * MAX_INPUT:
            raise ValueError("process query failed")
        rows = json.loads(result.stdout.decode("utf-8-sig"))
        if not isinstance(rows, list) or not rows:
            raise ValueError("invalid process snapshot")
        processes = []
        complete = True
        for row in rows:
            name = str(row.get("Name") or "")
            line = row.get("CommandLine")
            if re.search(r"node|chrome|msedge|agentdock|playwright|python", name, re.I) and not line:
                complete = False
            processes.append({"pid": row["ProcessId"], "name": name, "command_line": line or ""})
        return {"complete": complete, "processes": processes}
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return {"complete": False, "processes": [], "code": "PROCESS_STATE_UNKNOWN"}


def usage(path, snapshot):
    # Conservative substring matching: false positives retain a file. Never emit commands.
    needles = {str(path).replace("/", "\\").casefold()}
    pids = []
    for p in snapshot.get("processes", []):
        line = str(p.get("command_line", "")).replace("/", "\\").casefold()
        if any(n in line for n in needles):
            pids.append(p.get("pid"))
    return (True if pids else False if snapshot.get("complete") is True else None), pids


def verify_absent(raw):
    try:
        canonical_path(raw).lstat()
    except FileNotFoundError:
        return True
    return False


class Collector:
    def __init__(self, env, processes):
        self.processes = processes
        try:
            config = json.loads(env.get("CLEANUP_SCOPE_JSON", "{}"))
            if not isinstance(config, dict) or set(config) - ROLES - {"protected"}:
                raise ValueError("scope")
            protected = config.get("protected", [])
            if not isinstance(protected, list):
                raise ValueError("protected")
            self.roots = {role: canonical_path(value) for role, value in config.items() if role in ROLES}
            if any(p == Path(p.anchor) for p in self.roots.values()):
                raise ValueError("volume root")
            self.protected = [canonical_path(p) for p in protected]
            key = env.get("CLEANUP_SIGNING_KEY", "")
            if key and not re.fullmatch(r"[0-9a-fA-F]{64}", key):
                raise ValueError("signing key")
            self.key = bytes.fromhex(key) if key else None
            self.receipt_path = canonical_path(env["CLEANUP_RECEIPTS_FILE"]) if env.get("CLEANUP_RECEIPTS_FILE") else None
        except (ValueError, TypeError, OSError, CleanupError):
            raise CleanupError("INVALID_CONFIGURATION", "Check declared cleanup environment variable names and formats")
        self.scope_id = digest({"policy": POLICY_VERSION, "version": VERSION, "roots": {k: path_key(v) for k, v in self.roots.items()},
                                "protected": [path_key(p) for p in self.protected], "receipt_path": path_key(self.receipt_path)})

    def receipts(self):
        if not self.key or not self.receipt_path:
            return {}
        try:
            p = canonical_path(str(self.receipt_path))
            if p.stat().st_size > MAX_INPUT:
                return {}
            with p.open("rb") as f:
                data = json.loads(f.read(MAX_INPUT + 1))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("receipts"), list):
                return {}
            result = {}
            for item in data["receipts"]:
                if not isinstance(item, dict) or not isinstance(item.get("body"), dict) or not isinstance(item.get("signature"), str):
                    continue
                body = item["body"]
                if not hmac.compare_digest(mac(self.key, "ownership", body), item["signature"]):
                    continue
                if body.get("owner") != "agentdock" or body.get("closed") is not True:
                    continue
                p = canonical_path(body["path"])
                result[path_key(p)] = body
            return result
        except (OSError, ValueError, TypeError, KeyError, CleanupError):
            return {}

    def kind(self, p):
        workspace = self.roots.get("workspace")
        if workspace and path_key(p.parent) == path_key(workspace / ".playwright-mcp"):
            for kind, pattern in PLAYWRIGHT_NAMES.items():
                if pattern.fullmatch(p.name):
                    return kind
        runtime = self.roots.get("runtime_tmp")
        if runtime and path_key(p.parent) == path_key(runtime) and RUNTIME_NAME.fullmatch(p.name):
            return "runtime_temp"
        return None

    def is_protected(self, p, kind=None):
        if p == Path(p.anchor) or any(inside(p, root) for root in self.protected):
            return True
        if self.receipt_path and path_key(p) == path_key(self.receipt_path):
            return True
        if any(part.casefold() in PROTECTED_PARTS for part in p.parts):
            return True
        if p.name.casefold() == ".env" or (p.name.casefold() != "ms-playwright-mcp" and PROTECTED_NAME.search(p.name)):
            return True
        return p.suffix.casefold() in PROTECTED_SUFFIXES and kind != "playwright_snapshot"

    def is_shared(self, p):
        npm = self.roots.get("npm_cache")
        return (npm and inside(p, npm)) or any(part.casefold() in {"npm-cache", "_npx", "_cacache"} for part in p.parts)

    def classify(self, raw, snapshot, receipts):
        row = {"path": str(raw), "size": 0, "size_complete": True, "category": "USER_ARTIFACT", "risk": "high",
               "age": None, "age_seconds": None, "in_use": None, "process_ids": [], "eligible": False,
               "reason": "Ownership is unknown", "code": "OWNERSHIP_UNPROVEN", "recommended_action": "report_only", "reclaimable_bytes": 0}
        def deny(code, reason, category=None, action="report_only"):
            row.update(code=code, reason=reason, recommended_action=action)
            if category:
                row["category"] = category
            return row
        try:
            p = canonical_path(str(raw))
            row["path"] = str(p)
            row["in_use"], row["process_ids"] = usage(p, snapshot)
            kind = self.kind(p)
            if self.is_protected(p, kind):
                return deny("PROTECTED_RESOURCE", "Configuration, credentials, state, binaries or source are protected", "PROTECTED")
            if not any(inside(p, root) for root in self.roots.values()):
                return deny("OUTSIDE_CLEANUP_BOUNDARY", "Path is outside configured report scopes")
            s = p.lstat()
            row["age_seconds"] = max(0, int(time.time() - s.st_mtime))
            row["age"] = {"seconds": row["age_seconds"], "basis": "mtime", "retention_seconds": RETENTION_SECONDS}
            row["size"] = s.st_size if stat.S_ISREG(s.st_mode) else 0
            if self.is_shared(p):
                row["risk"] = "high"
                return deny("SHARED_RESOURCE", "Shared npm cache is report-only, including active Dynamic MCP installations", "SAFE_CACHE")
            profile_cache = self.roots.get("profile_cache")
            if (profile_cache and inside(p, profile_cache)) or any(part.casefold().startswith("playwright_chromiumdev_profile-") or part.casefold() == "ms-playwright-mcp" for part in p.parts):
                return deny("DIRECTORY_REPORT_ONLY", "Browser profiles and all their contents are report-only", "CONDITIONAL")
            if p.is_dir():
                category = "CONDITIONAL" if p.name.startswith("playwright_chromiumdev_profile-") or "ms-playwright-mcp" in p.parts else "USER_ARTIFACT"
                return deny("DIRECTORY_REPORT_ONLY", "Directory and browser-profile deletion requires producer lifecycle integration", category)
            if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
                return deny("LINK_NOT_ALLOWED", "Only ordinary single-link files can be candidates")
            logs = self.roots.get("logs")
            if logs and inside(p, logs):
                return deny("LOG_REPORT_ONLY", "Use producer-supported retention/rotation for logs", "CONDITIONAL", "recommend_retention_rotation")
            if not kind:
                return deny("OWNERSHIP_UNPROVEN", "Name and location are outside the finite artifact allowlist")
            row["category"] = "CONDITIONAL"
            receipt = receipts.get(path_key(p))
            producer = "agentdock" if kind == "runtime_temp" else "playwright-mcp"
            if not receipt or receipt.get("kind") != kind or receipt.get("producer") != producer:
                return deny("OWNERSHIP_UNPROVEN", "No authenticated, closed producer receipt; names alone do not prove ownership")
            if row["in_use"] is True:
                return deny("RESOURCE_NOW_IN_USE", "A process command line references the target")
            if row["in_use"] is None:
                return deny("PROCESS_STATE_UNKNOWN", "Process visibility is incomplete")
            if row["age_seconds"] < RETENTION_SECONDS:
                return deny("RETENTION_NOT_MET", "Artifact has not passed the fixed retention period")
            current = fingerprint(p)
            if current != receipt.get("fingerprint"):
                return deny("OWNERSHIP_UNPROVEN", "Artifact identity/content differs from the producer receipt")
            row.update(category="SAFE_TEMP", risk="low", eligible=True, code="ELIGIBLE", fingerprint=current,
                       reason="Expired allowlisted artifact with authenticated closed producer receipt", recommended_action="plan_then_confirm",
                       reclaimable_bytes=s.st_size)
            return row
        except CleanupError as exc:
            return deny(exc.code, str(exc))
        except FileNotFoundError:
            return deny("RESOURCE_MISSING", "Target no longer exists")
        except OSError:
            return deny("RESOURCE_UNREADABLE", "Target metadata could not be inspected")

    def measure_directory(self, raw):
        """Bounded size estimate; never follow links or enter protected trees."""
        total, count, complete = 0, 0, True
        stack = [Path(raw)]
        try:
            while stack and count < MAX_WALK:
                parent = canonical_path(str(stack.pop()))
                if self.is_protected(parent):
                    complete = False
                    continue
                with os.scandir(parent) as entries:
                    for entry in entries:
                        count += 1
                        if count >= MAX_WALK:
                            complete = False
                            break
                        s = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(s.st_mode) or getattr(s, "st_file_attributes", 0) & 0x400:
                            complete = False
                        elif stat.S_ISDIR(s.st_mode):
                            stack.append(Path(entry.path))
                        elif stat.S_ISREG(s.st_mode):
                            total += s.st_size
            return total, complete and not stack
        except (OSError, CleanupError):
            return total, False

    def discover(self):
        paths = []
        for role, root in self.roots.items():
            try:
                canonical_path(str(root))
                if self.is_protected(root):
                    paths.append(str(root))
                    continue
                with os.scandir(root) as entries:
                    for entry in entries:
                        if role == "temp" and not (entry.name.startswith("playwright_chromiumdev_profile-") or entry.name == "ms-playwright-mcp"):
                            continue
                        if role == "npm_cache" and entry.name not in {"_npx", "_cacache"}:
                            continue
                        paths.append(entry.path)
                        if len(paths) >= MAX_ITEMS:
                            return paths, True
                if role == "workspace":
                    output = root / ".playwright-mcp"
                    if output.exists():
                        canonical_path(str(output))
                        with os.scandir(output) as entries:
                            for entry in entries:
                                paths.append(entry.path)
                                if len(paths) >= MAX_ITEMS:
                                    return paths, True
            except (OSError, CleanupError):
                paths.append(str(root))
        return list(dict.fromkeys(paths)), False

    def scan(self, request):
        if not self.roots:
            raise CleanupError("CONFIGURATION_REQUIRED", "Host must supply CLEANUP_SCOPE_JSON report scopes")
        paths = request.get("paths")
        if paths is not None and (not isinstance(paths, list) or len(paths) > MAX_ITEMS or any(not isinstance(p, str) for p in paths)):
            raise CleanupError("INVALID_INPUT", "paths must be a bounded list of absolute paths")
        truncated = False
        if paths is None:
            paths, truncated = self.discover()
        snapshot = self.processes()
        receipts = self.receipts()
        items = [self.classify(p, snapshot, receipts) for p in paths]
        # Directory sizes are estimates and overlap with child rows. Never sum them as reclaimable bytes.
        for row in items:
            if row["code"] in {"SHARED_RESOURCE", "DIRECTORY_REPORT_ONLY"} and Path(row["path"]).is_dir():
                row["size"], row["size_complete"] = self.measure_directory(row["path"])
        unique = {}
        for row in items:
            unique.setdefault(path_key(row["path"]), row)
        items = list(unique.values())
        return {"ok": True, "action": "scan", "read_only": True, "items": items, "truncated": truncated,
                "process_visibility_complete": snapshot.get("complete") is True,
                "summary": {"count": len(items), "eligible_count": sum(r["eligible"] for r in items),
                            "reclaimable_bytes": sum(r["reclaimable_bytes"] for r in items)},
                "limitations": ["Directories/profiles, logs and shared caches are report-only",
                                "Producer receipts are required; legacy artifacts are not auto-enrolled"]}

    def plan(self, request):
        result = self.scan(request)
        selected = [{"path": r["path"], "fingerprint": r["fingerprint"], "size": r["size"]} for r in result["items"] if r["eligible"]]
        now = int(time.time())
        body = {"version": VERSION, "policy": POLICY_VERSION, "scope_id": self.scope_id,
                "created_at": now, "expires_at": now + PLAN_SECONDS, "nonce": secrets.token_hex(16), "items": selected}
        plan = {"plan_id": digest(body), "body": body, "signature": mac(self.key, "plan", body) if self.key else None,
                "items": selected}
        result.update(action="plan", plan=plan, confirmation_required=True)
        return result

    def validate_plan(self, request):
        plan = request.get("plan")
        confirmation = request.get("confirmation")
        if not isinstance(plan, dict) or not isinstance(confirmation, dict) or confirmation.get("phrase") != "DELETE_AGENTDOCK_RUNTIME_ARTIFACTS" or confirmation.get("plan_id") != plan.get("plan_id") or request.get("plan_id") != plan.get("plan_id"):
            raise CleanupError("CONFIRMATION_REQUIRED", "Confirm the exact plan_id and deletion phrase after reviewing the plan")
        try:
            body = plan["body"]
            if not self.key or plan["plan_id"] != digest(body) or not hmac.compare_digest(plan["signature"], mac(self.key, "plan", body)):
                raise ValueError("signature")
            if plan.get("items") != body["items"] or not isinstance(body["items"], list) or len(body["items"]) > MAX_ITEMS:
                raise ValueError("items")
            if body["version"] != VERSION or body["policy"] != POLICY_VERSION or body["scope_id"] != self.scope_id:
                raise CleanupError("PLAN_STALE", "Policy or host scope changed; generate a new plan")
            now = time.time()
            if not body["created_at"] <= now <= body["expires_at"] or body["expires_at"] - body["created_at"] != PLAN_SECONDS:
                raise CleanupError("PLAN_STALE", "Plan expired or clock moved; generate a new plan")
            return body["items"]
        except (KeyError, ValueError, TypeError):
            raise CleanupError("INVALID_PLAN", "Plan integrity check failed")

    def revalidate(self, item):
        # Never trust eligibility or paths in a signed plan without evaluating current policy.
        snapshot = self.processes()
        row = self.classify(item["path"], snapshot, self.receipts())
        if row["in_use"] is True:
            raise CleanupError("RESOURCE_NOW_IN_USE", "Resource became referenced by a process")
        if row["code"] in {"OUTSIDE_CLEANUP_BOUNDARY", "PROTECTED_RESOURCE", "PROCESS_STATE_UNKNOWN", "SHARED_RESOURCE"}:
            raise CleanupError(row["code"], row["reason"])
        if not row["eligible"] or row.get("fingerprint") != item["fingerprint"]:
            raise CleanupError("PLAN_STALE", "Target or producer ownership changed after planning")
        return row

    def clean(self, request):
        items = self.validate_plan(request)
        if request.get("dry_run", False) not in (True, False) or type(request.get("dry_run", False)) is not bool:
            raise CleanupError("INVALID_INPUT", "dry_run must be a boolean")
        dry_run = request.get("dry_run", False)
        results = []
        for item in items:
            result = {"path": item["path"], "verified": False, "reclaimed_bytes": 0}
            try:
                self.revalidate(item)
                if dry_run:
                    result["code"] = "WOULD_DELETE"
                elif os.name != "nt":
                    raise CleanupError("PLATFORM_UNSUPPORTED", "First-version clean requires Windows handle protection")
                else:
                    from windows_guard import locked_file
                    with locked_file(item["path"]) as locked:
                        if fingerprint(stream=locked.stream) != item["fingerprint"]:
                            raise CleanupError("PLAN_STALE", "Locked file differs from the planned file")
                        snapshot = self.processes()
                        in_use, _ = usage(Path(item["path"]), snapshot)
                        if in_use is not False:
                            raise CleanupError("RESOURCE_NOW_IN_USE" if in_use else "PROCESS_STATE_UNKNOWN", "Final process check failed")
                        # Receipt revocation is checked again while the artifact is locked.
                        receipt = self.receipts().get(path_key(canonical_path(item["path"])))
                        if not receipt or receipt.get("fingerprint") != item["fingerprint"]:
                            raise CleanupError("PLAN_STALE", "Producer receipt was revoked or changed")
                        locked.delete()
                    if verify_absent(item["path"]):
                        result.update(code="DELETED", verified=True, reclaimed_bytes=item["size"])
                    else:
                        raise CleanupError("VERIFY_FAILED", "A resource is still present at the target path")
            except CleanupError as exc:
                result.update(code=exc.code, message=str(exc))
            except OSError as exc:
                code = "RESOURCE_NOW_IN_USE" if getattr(exc, "winerror", 0) in {32, 33} else "DELETE_DENIED"
                result.update(code=code, message="OS refused the guarded operation")
            results.append(result)
        return {"ok": all(r["code"] in {"DELETED", "WOULD_DELETE"} for r in results), "action": "clean",
                "read_only": dry_run, "plan_id": request["plan_id"], "results": results,
                "reclaimed_bytes": sum(r["reclaimed_bytes"] for r in results)}


def handle(request, env=None, processes=None):
    try:
        if not isinstance(request, dict):
            raise CleanupError("INVALID_INPUT", "stdin must contain a JSON object")
        action = request.get("skill_action", "status")
        if action not in {"status", "scan", "plan", "clean"}:
            raise CleanupError("UNKNOWN_ACTION", "Use status, scan, plan or clean")
        allowed = {"skill_action", "paths"} if action in {"scan", "plan"} else {"skill_action"} if action == "status" else {"skill_action", "plan", "plan_id", "confirmation", "confirmed", "dry_run"}
        if set(request) - allowed:
            raise CleanupError("INVALID_INPUT", "Request includes unsupported fields; host configuration is environment-only")
        collector = Collector(os.environ if env is None else env, processes or process_snapshot)
        if action == "status":
            return {"ok": True, "action": action, "version": VERSION, "read_only": True,
                    "delete_ready": bool(collector.key and collector.receipt_path and collector.roots and os.name == "nt"),
                    "configured_roles": sorted(collector.roots), "default_retention_days": 7,
                    "directory_cleanup_supported": False, "platform": sys.platform}
        return getattr(collector, action)(request)
    except CleanupError as exc:
        return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return {"ok": False, "error": {"code": "INVALID_INPUT", "message": "Input or host state could not be safely interpreted"}}


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("too large")
        request = json.loads(raw) if raw.strip() else {}
        result = handle(request)
    except (ValueError, UnicodeError, RecursionError):
        result = {"ok": False, "error": {"code": "INVALID_INPUT", "message": "Expected a bounded JSON object"}}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True, allow_nan=False).encode("utf-8") + b"\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
