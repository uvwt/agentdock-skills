#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SKILL_NAME = "multi-agent-orchestration"
SCHEMA_VERSION = "mao.v1"
SKILL_ROOT = Path(__file__).resolve().parent
WEB_ROOT = SKILL_ROOT / "web"
WORKSPACE_ENV = "MULTI_AGENT_WORKSPACE_ROOT"
WORKSPACES_DIR = "workspaces"
ORCHESTRATIONS_DIR = "orchestrations"
STATE_GITIGNORE = ".DS_Store\n__pycache__/\n*.pyc\n"
OWNERSHIP_MARKER = ".agentdock-project.json"
WORK_ITEM_PATTERN = re.compile(r"^WI-(\d{4,})$")
RECORD_PATTERN = re.compile(r"^R-(\d{8}T\d{12}Z)-([0-9a-f]{8})\.json$")
BUSINESS_HEAD_PATTERN = re.compile(r"^[0-9a-fA-F]{4,40}$")
SEATS = {
    "1": {"name": "Orchestrator", "kind": "orchestrator", "permanent": True},
    "2": {"name": "Independent Gatekeeper", "kind": "gatekeeper", "permanent": True},
    "3": {"name": "Worker A", "kind": "worker", "permanent": False},
    "4": {"name": "Worker B", "kind": "worker", "permanent": False},
    "5": {"name": "Worker C", "kind": "worker", "permanent": False},
}
RECORD_TYPES = {"assignment", "message", "event", "gate"}
EVENT_KINDS = {
    "started",
    "blocked",
    "done",
    "cancelled",
    "artifact_ready",
    "wi_paused",
    "wi_resumed",
    "wi_cancelled",
    "wi_completed",
    "gate_waiver",
    "wi_status",
}
WORK_STATUSES = {"BACKLOG", "ACTIVE", "BLOCKED", "REVIEW", "DONE", "PAUSED", "CANCELLED"}
PRIORITIES = {"LOW", "NORMAL", "HIGH", "URGENT"}
MAX_BODY_BYTES = 64 * 1024
WRITE_LOCK = threading.RLock()
LOCK_STATE = threading.local()
LOCK_TIMEOUT_SECONDS = 30.0
LOCK_STALE_SECONDS = 300.0


class SkillError(Exception):
    def __init__(self, code: str, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = int(status)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_scalar(value: Any, *, field: str, max_length: int = 500) -> str:
    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    if not text:
        raise SkillError("invalid_input", f"{field} 不能为空")
    if len(text) > max_length:
        raise SkillError("invalid_input", f"{field} 过长，最多 {max_length} 个字符")
    return text


def clean_text(value: Any, *, field: str, max_length: int = 8000) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()
    if not text:
        raise SkillError("invalid_input", f"{field} 不能为空")
    if len(text) > max_length:
        raise SkillError("invalid_input", f"{field} 过长，最多 {max_length} 个字符")
    return text


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "project"


def configured_workspace_root() -> Path | None:
    raw = os.environ.get(WORKSPACE_ENV, "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def require_workspace_root(*, create: bool = False) -> Path:
    root = configured_workspace_root()
    if root is None:
        raise SkillError("workspace_not_configured", f"未配置 {WORKSPACE_ENV}")
    if root.exists() and not root.is_dir():
        raise SkillError("invalid_workspace", f"Multi-Agent 工作根目录不是目录: {root}")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def workspace_layout(*, create: bool = False) -> tuple[Path, Path, Path]:
    root = require_workspace_root(create=create)
    workspaces_root = root / WORKSPACES_DIR
    orchestrations_root = root / ORCHESTRATIONS_DIR
    if create:
        workspaces_root.mkdir(parents=True, exist_ok=True)
        orchestrations_root.mkdir(parents=True, exist_ok=True)
    return root, workspaces_root, orchestrations_root


@contextmanager
def orchestration_write_lock(state_root: Path):
    """Serialize one orchestration repository across threads and independent Agent processes."""
    state_root = state_root.resolve()
    lock_dir = state_root.parent / f".{state_root.name}.write-lock"
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    key = str(lock_dir)
    with WRITE_LOCK:
        depths = getattr(LOCK_STATE, "depths", None)
        if depths is None:
            depths = {}
            LOCK_STATE.depths = depths
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return

        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                lock_dir.mkdir(parents=False)
                (lock_dir / "owner.json").write_text(json.dumps({"pid": os.getpid(), "created_at": time.time()}), encoding="utf-8")
                break
            except FileExistsError:
                try:
                    age = time.time() - lock_dir.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > LOCK_STALE_SECONDS:
                    owner_pid = 0
                    try:
                        owner = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
                        owner_pid = int(owner.get("pid") or 0)
                    except (OSError, ValueError, json.JSONDecodeError):
                        owner_pid = 0
                    owner_alive = False
                    if owner_pid > 0:
                        try:
                            os.kill(owner_pid, 0)
                            owner_alive = True
                        except ProcessLookupError:
                            owner_alive = False
                        except PermissionError:
                            owner_alive = True
                    if not owner_alive:
                        shutil.rmtree(lock_dir, ignore_errors=True)
                        continue
                if time.monotonic() >= deadline:
                    raise SkillError("orchestration_busy", f"编排仓库正被另一个 Agent 写入: {state_root}", HTTPStatus.CONFLICT)
                time.sleep(0.05)

        depths[key] = 1
        try:
            yield
        finally:
            depths.pop(key, None)
            owner_pid = 0
            try:
                owner = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
                owner_pid = int(owner.get("pid") or 0)
            except (OSError, ValueError, json.JSONDecodeError):
                owner_pid = 0
            if owner_pid == os.getpid():
                shutil.rmtree(lock_dir, ignore_errors=True)


def run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, timeout=20, check=check)


def required_business_git_root(project_fields: dict[str, str]) -> Path | None:
    if project_fields.get("needs_business_git") != "true":
        return None
    raw_path = str(project_fields.get("repository_path") or "").strip()
    if not raw_path:
        raise SkillError("business_git_unavailable", "项目要求 business Git，但 PROJECT.md 未记录 repository_path", HTTPStatus.CONFLICT)
    root = Path(raw_path).expanduser().resolve()
    if not root.is_dir() or run_git(root, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        raise SkillError("business_git_unavailable", f"业务路径不是可用 Git 仓库: {root}", HTTPStatus.CONFLICT)
    return root


def normalize_business_head_sha(root: Path, value: Any) -> str:
    raw = str(value or "").strip()
    if not BUSINESS_HEAD_PATTERN.fullmatch(raw):
        raise SkillError("business_head_required", "business_head_sha 必须是 4-40 位十六进制 Git commit SHA")
    resolved = run_git(root, "rev-parse", "--verify", f"{raw}^{{commit}}", check=False)
    sha = resolved.stdout.strip().lower() if resolved.returncode == 0 else ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SkillError("business_head_unresolvable", "business_head_sha 必须能唯一解析为当前业务仓库中的 40 位 commit SHA", HTTPStatus.CONFLICT)
    return sha


def current_business_head_sha(root: Path) -> str:
    result = run_git(root, "rev-parse", "HEAD", check=False)
    sha = result.stdout.strip().lower() if result.returncode == 0 else ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SkillError("business_head_unavailable", "业务 Git 尚无可验证 HEAD commit", HTTPStatus.CONFLICT)
    return sha


def require_clean_business_git(root: Path) -> None:
    result = run_git(root, "status", "--porcelain", check=False)
    if result.returncode != 0:
        raise SkillError("business_git_status_failed", "无法确认业务 Git 工作区是否 clean；正式 Gate / DONE 必须 fail closed", HTTPStatus.CONFLICT)
    if any(line.strip() for line in result.stdout.splitlines()):
        raise SkillError("business_git_dirty", "业务 Git 工作区存在未提交改动；正式 Gate / DONE 必须绑定可复现的 clean HEAD", HTTPStatus.CONFLICT)


def init_git_repository(root: Path) -> None:
    result = run_git(root, "init", "-b", "main", check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-500:] or "git init 失败"
        raise SkillError("git_init_failed", detail)


def current_branch(root: Path) -> str:
    result = run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "main"


def repository_identity(project_root: Path) -> str:
    project_root = project_root.resolve()
    result = run_git(project_root, "rev-parse", "--git-common-dir", check=False)
    if result.returncode == 0 and result.stdout.strip():
        common_dir = Path(result.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = project_root / common_dir
        return f"git:{common_dir.resolve()}"
    return f"path:{project_root}"


def git_lines(root: Path, *args: str) -> list[str]:
    result = run_git(root, *args, check=False)
    return [line for line in result.stdout.splitlines() if line.strip()] if result.returncode == 0 else []


def business_git_snapshot(project: dict[str, Any], project_fields: dict[str, str]) -> dict[str, Any]:
    if not project.get("needs_business_git") or not project.get("path"):
        return {"available": False, "required": False, "reason": "not_required"}
    root = Path(str(project["path"])).resolve()
    inside = run_git(root, "rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode != 0:
        return {"available": False, "required": True, "error": "业务路径不是 Git 仓库"}
    status = git_lines(root, "status", "--porcelain")
    head = run_git(root, "rev-parse", "--short", "HEAD", check=False).stdout.strip()
    upstream = run_git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    tracking: dict[str, Any] = {"upstream": upstream.stdout.strip() if upstream.returncode == 0 else ""}
    if tracking["upstream"]:
        counts = run_git(root, "rev-list", "--left-right", "--count", f"{tracking['upstream']}...HEAD", check=False).stdout.split()
        if len(counts) == 2:
            tracking.update({"behind": int(counts[0]), "ahead": int(counts[1])})
    return {
        "available": True,
        "required": True,
        "branch": current_branch(root),
        "head": head,
        "dirty": bool(status),
        "tracking": tracking,
        "repository_identity": project_fields.get("repository_identity") or repository_identity(root),
        "recent_commits": [
            {"short": line.split("\x1f", 1)[0], "subject": line.split("\x1f", 1)[1] if "\x1f" in line else ""}
            for line in git_lines(root, "log", "-5", "--pretty=format:%h%x1f%s")
        ],
        "worktrees": [
            {"path": block.get("worktree", ""), "head": block.get("HEAD", ""), "branch": block.get("branch", "").removeprefix("refs/heads/")}
            for block in _parse_worktree_porcelain(git_lines(root, "worktree", "list", "--porcelain"))
        ],
    }


def _parse_worktree_porcelain(lines: list[str]) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in lines + [""]:
        if not line:
            if current:
                blocks.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return blocks


def parse_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    if not path.is_file():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        raw = value.strip()
        if raw.startswith('"'):
            try:
                fields[key.strip()] = str(json.loads(raw))
                continue
            except json.JSONDecodeError:
                pass
        fields[key.strip()] = raw
    return fields, text[end + 5 :]


def write_document(path: Path, fields: dict[str, Any], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, raw_value in fields.items():
        value = str(raw_value).replace("\r", " ").replace("\n", " ").strip()
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    # 投影文件会被反复重建；只规范首尾换行，不改变正文内部空白，确保同一事实重放结果字节稳定。
    lines.extend(["---", "", body.strip("\n"), ""])
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(path)


def read_title_from_body(body: str, fallback: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def markdown_section(body: str, heading: str) -> str:
    marker = f"## {heading}"
    if marker not in body:
        return ""
    tail = body.split(marker, 1)[1].lstrip("\n")
    return tail.split("\n## ", 1)[0].strip()


def normalize_projects(raw_projects: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_projects, list) or not raw_projects:
        raise SkillError("invalid_projects", "projects 必须是至少包含一个项目的数组")
    _, _, orchestrations_root = workspace_layout(create=False)
    projects: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, raw in enumerate(raw_projects, start=1):
        if isinstance(raw, str):
            root = Path(raw).expanduser().resolve()
            if not root.is_dir():
                raise SkillError("project_not_found", f"项目目录不存在: {root}")
            name = root.name
            project_id = safe_slug(name)
            needs_business_git = True
            path = str(root)
        elif isinstance(raw, dict):
            name = clean_scalar(raw.get("name") or raw.get("id") or f"project-{index}", field=f"projects[{index}].name", max_length=120)
            project_id = safe_slug(str(raw.get("id") or name))
            raw_path = str(raw.get("path") or "").strip()
            needs_business_git = bool(raw.get("needs_business_git", bool(raw_path)))
            if raw_path:
                root = Path(raw_path).expanduser().resolve()
                if not root.is_dir():
                    raise SkillError("project_not_found", f"项目目录不存在: {root}")
                path = str(root)
            else:
                path = ""
            if needs_business_git and not path:
                raise SkillError("business_path_required", f"projects[{index}] 声明需要业务 Git，但缺少 path")
        else:
            raise SkillError("invalid_projects", f"projects[{index}] 必须是路径字符串或对象")
        if project_id in used_ids:
            raise SkillError("duplicate_project_id", f"重复 project id: {project_id}")
        used_ids.add(project_id)
        projects.append({
            "id": project_id,
            "name": name,
            "path": path,
            "needs_business_git": needs_business_git,
            "profile": clean_scalar(raw.get("profile") or ("software" if needs_business_git else "generic"), field=f"projects[{index}].profile", max_length=80) if isinstance(raw, dict) else "software",
            "state_path": str((orchestrations_root / project_id).resolve()),
        })
    return projects


def orchestration_root(project: dict[str, Any]) -> Path:
    return Path(str(project["state_path"])).expanduser().resolve()


def project_state_fields(project: dict[str, Any], *, mode: str, timestamp: str) -> dict[str, str]:
    root = Path(str(project["path"])).resolve() if project.get("path") else None
    return {
        "schema": SCHEMA_VERSION,
        "skill": SKILL_NAME,
        "project_id": str(project["id"]),
        "name": str(project["name"]),
        "status": "ACTIVE",
        "mode": mode,
        "profile": str(project.get("profile") or "generic"),
        "needs_business_git": "true" if project.get("needs_business_git") else "false",
        "repository_path": str(root) if root else "",
        "repository_identity": repository_identity(root) if root else "",
        "default_branch": current_branch(root) if root else "",
        "created_at": timestamp,
    }


def initialize_orchestration_repo(project: dict[str, Any], *, mode: str, goal: str | None = None) -> Path:
    state_root = orchestration_root(project)
    state_root.parent.mkdir(parents=True, exist_ok=True)
    if state_root.exists() and not state_root.is_dir():
        raise SkillError("orchestration_path_invalid", f"编排状态路径不是目录: {state_root}")
    if state_root.exists() and not (state_root / ".git").exists() and any(state_root.iterdir()):
        raise SkillError("orchestration_path_occupied", f"编排状态目录已被其他内容占用: {state_root}", HTTPStatus.CONFLICT)
    created_root = False
    if not state_root.exists():
        state_root.mkdir(parents=False)
        created_root = True
    try:
        if not (state_root / ".git").exists():
            init_git_repository(state_root)
        top_level = Path(run_git(state_root, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        if top_level != state_root:
            raise SkillError("invalid_orchestration_repo", f"编排状态目录不是独立 Git 根目录: {state_root}")
        project_file = state_root / "PROJECT.md"
        if project_file.is_file():
            fields, _ = parse_frontmatter(project_file)
            existing_id = fields.get("project_id")
            if existing_id and existing_id != project["id"]:
                raise SkillError("project_binding_conflict", f"编排仓库已绑定其他项目 ID: {existing_id}", HTTPStatus.CONFLICT)
            if project.get("path"):
                identity = repository_identity(Path(str(project["path"])))
                if fields.get("repository_identity") and fields["repository_identity"] != identity:
                    raise SkillError("project_binding_conflict", "编排仓库已绑定其他业务仓库", HTTPStatus.CONFLICT)
                existing_path = fields.get("repository_path")
                if not fields.get("repository_identity") and existing_path and repository_identity(Path(existing_path)) != identity:
                    raise SkillError("project_binding_conflict", "编排仓库已绑定其他业务仓库", HTTPStatus.CONFLICT)
            return state_root
        timestamp = now_iso()
        body = f"# {project['name']}\n\n由 {SKILL_NAME} 管理的独立编排状态仓库。"
        if goal:
            body += f"\n\n## 目标\n\n{goal}"
        write_document(project_file, project_state_fields(project, mode=mode, timestamp=timestamp), body)
        (state_root / "BOARD.md").write_text("# Work Items\n", encoding="utf-8")
        (state_root / "DECISIONS.md").write_text("# Decisions\n", encoding="utf-8")
        (state_root / ".gitignore").write_text(STATE_GITIGNORE, encoding="utf-8")
        return state_root
    except Exception:
        if created_root:
            shutil.rmtree(state_root, ignore_errors=True)
        raise


def init_greenfield_project(payload: dict[str, Any]) -> dict[str, str]:
    name = clean_scalar(payload.get("name"), field="name", max_length=120)
    slug = safe_slug(clean_scalar(payload.get("slug") or name, field="slug", max_length=120))
    goal = clean_text(payload.get("goal") or name, field="goal", max_length=4000)
    profile = clean_scalar(payload.get("profile") or "software", field="profile", max_length=80)
    needs_business_git = bool(payload.get("needs_business_git", profile == "software"))
    workspace_root, workspaces_root, orchestrations_root = workspace_layout(create=True)
    state_root = (orchestrations_root / slug).resolve()
    if state_root.exists():
        raise SkillError("orchestration_exists", f"目标编排状态目录已存在，拒绝覆盖: {state_root}", HTTPStatus.CONFLICT)
    project_root = (workspaces_root / slug).resolve() if needs_business_git else None
    if project_root and project_root.exists():
        raise SkillError("project_exists", f"目标项目目录已存在，拒绝接管: {project_root}", HTTPStatus.CONFLICT)
    if project_root:
        project_root.mkdir(parents=False)
    try:
        if project_root:
            init_git_repository(project_root)
            marker = {"schema": SCHEMA_VERSION, "created_by": SKILL_NAME, "name": name, "slug": slug, "created_at": now_iso()}
            (project_root / OWNERSHIP_MARKER).write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        project = {"id": slug, "name": name, "path": str(project_root) if project_root else "", "state_path": str(state_root), "needs_business_git": needs_business_git, "profile": profile}
        initialize_orchestration_repo(project, mode="greenfield", goal=goal)
    except Exception:
        if project_root:
            shutil.rmtree(project_root, ignore_errors=True)
        shutil.rmtree(state_root, ignore_errors=True)
        raise
    return {"id": slug, "name": name, "path": str(project_root) if project_root else "", "orchestration_path": str(state_root), "workspace_root": str(workspace_root), "profile": profile, "needs_business_git": str(needs_business_git).lower()}


def work_item_directories(state_root: Path) -> list[Path]:
    root = state_root / "work-items"
    if not root.is_dir():
        return []
    return sorted([p for p in root.iterdir() if p.is_dir() and WORK_ITEM_PATTERN.fullmatch(p.name)], key=lambda p: int(WORK_ITEM_PATTERN.fullmatch(p.name).group(1)))


def next_work_item_id(state_root: Path) -> str:
    highest = 0
    for path in work_item_directories(state_root):
        match = WORK_ITEM_PATTERN.fullmatch(path.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"WI-{highest + 1:04d}"


def record_files(work_root: Path) -> list[Path]:
    root = work_root / "records"
    if not root.is_dir():
        return []
    return sorted([p for p in root.iterdir() if p.is_file() and RECORD_PATTERN.fullmatch(p.name)])


def load_records(work_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in record_files(work_root):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SkillError("invalid_record", f"Record JSON 损坏: {path.name}: {exc}") from exc
        if isinstance(record, dict):
            if not str(record.get("created_at") or ""):
                raise SkillError("invalid_record", f"Record 缺少 created_at: {path.name}")
            records.append(record)
    records.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
    return records


def new_record_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"R-{stamp}-{uuid.uuid4().hex[:8]}"


def record_timestamp_from_id(record_id: str) -> str:
    match = re.fullmatch(r"R-(\d{8}T\d{12}Z)-[0-9a-f]{8}", record_id)
    if match is None:
        raise SkillError("invalid_record_id", f"Record ID 格式无效: {record_id}")
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_record(record_type: str, sender: str, payload: dict[str, Any]) -> None:
    if record_type not in RECORD_TYPES:
        raise SkillError("invalid_record_type", f"type 必须是 {', '.join(sorted(RECORD_TYPES))}")
    if sender not in {*SEATS, "user"}:
        raise SkillError("invalid_sender", "from 必须是 user 或 1/2/3/4/5")
    if sender == "user" and record_type != "message":
        raise SkillError("user_message_only", "user 只作为 Message 来源；Assignment / Event / Gate 必须由 Agent 写入", HTTPStatus.FORBIDDEN)
    if record_type == "assignment":
        if sender != "1":
            raise SkillError("forbidden_dispatch", "正式 Assignment 只能由 1 号总管发出", HTTPStatus.FORBIDDEN)
        assignee = str(payload.get("assignee") or payload.get("to") or "").strip()
        if assignee not in SEATS:
            raise SkillError("invalid_assignee", "assignee 必须是 1/2/3/4/5")
        role = str(payload.get("role") or "").strip().lower()
        if len(role) > 120:
            raise SkillError("invalid_role", "Assignment role 过长，最多 120 个字符")
        if assignee == "1" and role != "orchestrator":
            raise SkillError("orchestrator_role_fixed", "1 号是固定 Orchestrator，不能被 Assignment 改任其他角色", HTTPStatus.CONFLICT)
        if assignee == "2" and role != "independent-gatekeeper":
            raise SkillError("gatekeeper_role_fixed", "2 号是固定 Independent Gatekeeper，不能被 Assignment 改任其他角色", HTTPStatus.CONFLICT)
        if assignee in {"3", "4", "5"} and not role:
            raise SkillError("worker_role_required", "3/4/5 的 Assignment 必须包含动态 role")
        title = str(payload.get("title") or "").strip()
        goal = str(payload.get("goal") or "").strip()
        if not title and not goal:
            raise SkillError("invalid_assignment", "Assignment 至少需要 title 或 goal")
        if len(title) > 500 or len(goal) > 8000:
            raise SkillError("invalid_assignment", "Assignment title 最多 500 字符，goal 最多 8000 字符")
    elif record_type == "message":
        recipient = str(payload.get("to") or "board").strip()
        if recipient not in {*SEATS, "user", "board"}:
            raise SkillError("invalid_recipient", "message.to 必须是 user / board / 1/2/3/4/5")
        body = str(payload.get("body") or "").strip()
        if not body:
            raise SkillError("invalid_message", "message.body 不能为空")
        if len(body) > 8000:
            raise SkillError("invalid_message", "message.body 过长，最多 8000 个字符")
    elif record_type == "gate":
        if sender != "2":
            raise SkillError("gatekeeper_only", "Gate Record 只能由 2 号独立门禁写入", HTTPStatus.FORBIDDEN)
        profile = str(payload.get("profile") or "").strip()
        if not profile:
            raise SkillError("invalid_gate_profile", "Gate profile 不能为空")
        if len(profile) > 80:
            raise SkillError("invalid_gate_profile", "Gate profile 过长，最多 80 个字符")
        evidence = str(payload.get("evidence") or "")
        if len(evidence) > 16000:
            raise SkillError("invalid_gate_evidence", "Gate evidence 过长，最多 16000 个字符")
        verdict = str(payload.get("verdict") or "").strip().upper()
        if verdict not in {"PASS", "FAIL", "BLOCKED", "PENDING"}:
            raise SkillError("invalid_gate_verdict", "Gate verdict 必须是 PASS / FAIL / BLOCKED / PENDING")
        checks = payload.get("checks")
        if not isinstance(checks, list):
            raise SkillError("invalid_gate_checks", "Gate checks 必须是数组")
        if len(checks) > 100:
            raise SkillError("invalid_gate_checks", "Gate checks 最多 100 项")
        for check in checks:
            if not isinstance(check, dict) or not str(check.get("id") or "").strip() or not str(check.get("result") or "").strip():
                raise SkillError("invalid_gate_checks", "每个 Gate check 都必须包含 id 和 result")
            if len(str(check.get("id"))) > 120 or len(str(check.get("result"))) > 120 or len(str(check.get("evidence") or "")) > 8000:
                raise SkillError("invalid_gate_checks", "Gate check 的 id/result 最多 120 字符，evidence 最多 8000 字符")
    elif record_type == "event":
        kind = str(payload.get("kind") or "").strip()
        if kind not in EVENT_KINDS:
            raise SkillError("invalid_event_kind", f"不支持的 event kind: {kind}")
        if kind in {"wi_paused", "wi_resumed", "wi_cancelled", "wi_completed", "gate_waiver", "wi_status"} and sender != "1":
            raise SkillError("orchestrator_only", f"{kind} 只能由 1 号总管发出", HTTPStatus.FORBIDDEN)
        if kind == "wi_status" and str(payload.get("status") or "").strip().upper() not in WORK_STATUSES:
            raise SkillError("invalid_work_status", f"wi_status.status 必须是 {', '.join(sorted(WORK_STATUSES))}")
        if kind == "gate_waiver":
            check = str(payload.get("check") or "").strip()
            if not check:
                raise SkillError("invalid_gate_waiver", "gate_waiver.check 不能为空")
            if len(check) > 120:
                raise SkillError("invalid_gate_waiver", "gate_waiver.check 过长，最多 120 个字符")
        if kind == "artifact_ready":
            artifact = str(payload.get("artifact") or "").strip()
            if not artifact:
                raise SkillError("invalid_artifact", "artifact_ready.artifact 不能为空")
            if len(artifact) > 1000:
                raise SkillError("invalid_artifact", "artifact_ready.artifact 过长，最多 1000 个字符")
        if kind == "blocked":
            blocker = str(payload.get("detail") or payload.get("body") or "").strip()
            if not blocker:
                raise SkillError("invalid_blocker", "blocked event 必须说明 detail/body")
            if len(blocker) > 8000:
                raise SkillError("invalid_blocker", "blocked detail/body 过长，最多 8000 个字符")



def _validate_record_context(work_root: Path, record_type: str, sender: str, payload: dict[str, Any]) -> None:
    if record_type != "event":
        return
    assignment_id = str(payload.get("assignment") or "").strip()
    kind = str(payload.get("kind") or "").strip()
    if not assignment_id:
        if kind in {"started", "blocked", "done", "cancelled", "artifact_ready"}:
            raise SkillError("assignment_required", f"event {kind} 必须引用 assignment")
        return
    assignment = next((record for record in load_records(work_root) if record.get("id") == assignment_id and record.get("type") == "assignment"), None)
    if assignment is None:
        raise SkillError("assignment_not_found", f"未找到 Assignment: {assignment_id}")
    assignee = str(assignment.get("assignee") or assignment.get("to") or "")
    if sender == assignee:
        return
    if kind == "cancelled" and sender == "1":
        return
    raise SkillError("assignment_owner_only", f"{sender} 号不能替 {assignee} 号更新 Assignment {assignment_id}", HTTPStatus.FORBIDDEN)


def _latest_gate_record(work_root: Path) -> dict[str, Any] | None:
    gates = [record for record in load_records(work_root) if record.get("type") == "gate"]
    return gates[-1] if gates else None


def _projected_gate_validity(work_root: Path, gate: dict[str, Any] | None) -> tuple[str, bool, str]:
    record_verdict = str((gate or {}).get("verdict") or "PENDING").upper()
    if gate is None or record_verdict != "PASS":
        return record_verdict, False, ""

    project_fields, _ = parse_frontmatter(work_root.parent.parent / "PROJECT.md")
    if project_fields.get("needs_business_git") != "true":
        return record_verdict, False, ""

    gate_sha = str(gate.get("business_head_sha") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", gate_sha):
        return "STALE", True, "final_gate_unbound"

    try:
        business_root = required_business_git_root(project_fields)
        if current_business_head_sha(business_root) != gate_sha:
            return "STALE", True, "business_head_changed"
        require_clean_business_git(business_root)
    except SkillError as exc:
        return "STALE", True, exc.code
    return record_verdict, False, ""


def _validate_final_candidate(
    project_fields: dict[str, str],
    work_root: Path,
    record_type: str,
    payload: dict[str, Any],
) -> None:
    business_root = required_business_git_root(project_fields)
    if record_type == "gate":
        if business_root is None:
            return
        candidate_sha = normalize_business_head_sha(business_root, payload.get("business_head_sha"))
        current_sha = current_business_head_sha(business_root)
        if candidate_sha != current_sha:
            raise SkillError("business_head_mismatch", "正式 Gate 只能绑定业务目标工作区当前 HEAD", HTTPStatus.CONFLICT)
        require_clean_business_git(business_root)
        payload["business_head_sha"] = candidate_sha
        return

    if record_type != "event":
        return
    kind = str(payload.get("kind") or "").strip()
    is_completion = kind == "wi_completed" or (kind == "wi_status" and str(payload.get("status") or "").strip().upper() == "DONE")
    if not is_completion:
        return

    gate = _latest_gate_record(work_root)
    if gate is None or str(gate.get("verdict") or "").strip().upper() != "PASS":
        raise SkillError("final_gate_required", "DONE 前必须存在 2 号对最终候选写入的最新 PASS Gate", HTTPStatus.CONFLICT)
    if business_root is None:
        return

    gate_sha = str(gate.get("business_head_sha") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", gate_sha):
        raise SkillError("final_gate_unbound", "最新 PASS Gate 未绑定 40 位 business_head_sha，不能 DONE", HTTPStatus.CONFLICT)
    completion_sha = normalize_business_head_sha(business_root, payload.get("business_head_sha"))
    current_sha = current_business_head_sha(business_root)
    if completion_sha != gate_sha or completion_sha != current_sha:
        raise SkillError("stale_final_gate", "业务 HEAD 已与正式 Gate 候选不一致；需要重新 Gate 后才能 DONE", HTTPStatus.CONFLICT)
    require_clean_business_git(business_root)
    payload["business_head_sha"] = completion_sha


def _append_record_unlocked(work_root: Path, record_type: str, sender: str, payload: dict[str, Any]) -> dict[str, Any]:
    _validate_record(record_type, sender, payload)
    record_id = new_record_id()
    record = {
        "id": record_id,
        "type": record_type,
        "work_item": work_root.name,
        "from": sender,
        "to": str(payload.get("to") or payload.get("assignee") or "board"),
        "created_at": record_timestamp_from_id(record_id),
    }
    for key, value in payload.items():
        if key not in record:
            record[key] = value
    records_root = work_root / "records"
    records_root.mkdir(parents=True, exist_ok=True)
    path = records_root / f"{record_id}.json"
    # Record 是 append-only 事实；使用独占创建而不是覆盖写，哪怕极端 ID 碰撞也只能失败，不能改写历史。
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    except FileExistsError as exc:
        raise SkillError("record_id_collision", f"Record ID 已存在，拒绝覆盖: {record_id}", HTTPStatus.CONFLICT) from exc
    return record


def append_record(state_root: Path, work_item_id: str, record_type: str, sender: str, payload: dict[str, Any]) -> dict[str, Any]:
    work_root = state_root / "work-items" / work_item_id
    if not work_root.is_dir():
        raise SkillError("work_item_not_found", f"未找到 Work Item: {work_item_id}", HTTPStatus.NOT_FOUND)
    project_fields, _ = parse_frontmatter(state_root / "PROJECT.md")
    if project_fields.get("schema") != SCHEMA_VERSION:
        raise SkillError("unsupported_schema", f"只支持 schema={SCHEMA_VERSION}", HTTPStatus.CONFLICT)
    with orchestration_write_lock(state_root):
        _validate_record(record_type, sender, payload)
        _validate_record_context(work_root, record_type, sender, payload)
        _validate_final_candidate(project_fields, work_root, record_type, payload)
        # Record 是多 Agent 共享的唯一可写事实。这里不顺手重写 BOARD/STATE/RELEASE，
        # 否则多个 Worker 虽然写不同 Record 文件，仍会在共享投影上制造 Git 冲突。
        # 控制台直接从 Record 重放；需要持久化人类可读投影时由 rebuild_projections 显式生成。
        return _append_record_unlocked(work_root, record_type, sender, payload)


def append_record_and_sync(
    project: dict[str, Any],
    work_item_id: str,
    record_type: str,
    sender: str,
    payload: dict[str, Any],
    *,
    push_remote: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    state_root = orchestration_root(project)
    with orchestration_write_lock(state_root):
        top = run_git(state_root, "rev-parse", "--show-toplevel", check=False)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != state_root:
            raise SkillError("orchestration_not_git_repository", "sync_git 需要独立 orchestration Git 仓库", HTTPStatus.CONFLICT)
        record = append_record(state_root, work_item_id, record_type, sender, payload)
        git_sync = sync_orchestration_git(
            project,
            record_sync_paths(work_item_id, record),
            f"chore(orchestration): 记录 {record['id']} {record_type}",
            push_remote=push_remote,
        )
    return record, git_sync


def project_records(work_root: Path) -> dict[str, Any]:
    records = load_records(work_root)
    assignments: dict[str, dict[str, Any]] = {}
    assignment_state: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    latest_gate: dict[str, Any] | None = None
    work_status = "ACTIVE"
    waived_checks: set[str] = set()

    # 第一遍只建立稳定事实索引。Worker 的时钟可能与总管略有偏差，因此不能要求
    # Event 在排序上一定落在它引用的 Assignment 后面；因果关系以 assignment id 为准。
    for record in records:
        record_type = record.get("type")
        if record_type == "assignment":
            assignee = str(record.get("assignee") or record.get("to") or "")
            if assignee in SEATS:
                previous = assignments.get(assignee)
                if previous is None or (str(record.get("created_at") or ""), str(record.get("id") or "")) > (str(previous.get("created_at") or ""), str(previous.get("id") or "")):
                    assignments[assignee] = record
                assignment_state[record["id"]] = {"status": "READY", "blockers": "", "last_event": ""}
        elif record_type == "event":
            events.append(record)
        elif record_type == "message":
            messages.append(record)
        elif record_type == "gate":
            if latest_gate is None or (str(record.get("created_at") or ""), str(record.get("id") or "")) > (str(latest_gate.get("created_at") or ""), str(latest_gate.get("id") or "")):
                latest_gate = record

    # 第二遍根据显式引用更新 Assignment 状态，并处理 Work Item 控制面事件。
    for record in events:
        kind = str(record.get("kind") or "")
        if kind == "wi_paused":
            work_status = "PAUSED"
        elif kind == "wi_resumed":
            work_status = "ACTIVE"
        elif kind == "wi_cancelled":
            work_status = "CANCELLED"
        elif kind == "wi_completed":
            work_status = "DONE"
        elif kind == "gate_waiver":
            waived_checks.add(str(record.get("check") or ""))
        elif kind == "wi_status":
            work_status = str(record.get("status") or "ACTIVE").upper()
        assignment_id = str(record.get("assignment") or "")
        state = assignment_state.get(assignment_id)
        if state is None:
            continue
        if kind not in {"started", "blocked", "done", "cancelled"}:
            continue
        previous_event_id = state.get("last_event") or ""
        if previous_event_id:
            previous_event = next((item for item in events if item.get("id") == previous_event_id), None)
            if previous_event and (str(record.get("created_at") or ""), str(record.get("id") or "")) <= (str(previous_event.get("created_at") or ""), str(previous_event.get("id") or "")):
                continue
        if kind == "started":
            state["status"] = "WORKING"
            state["blockers"] = ""
        elif kind == "blocked":
            state["status"] = "BLOCKED"
            state["blockers"] = str(record.get("detail") or record.get("body") or "")
        elif kind in {"done", "cancelled"}:
            state["status"] = "DONE" if kind == "done" else "CANCELLED"
            state["blockers"] = ""
        state["last_event"] = record["id"]

    if latest_gate is not None and work_status == "ACTIVE":
        work_status = "REVIEW"
    current_assignment_states = [assignment_state[assignment["id"]] for assignment in assignments.values() if assignment.get("id") in assignment_state]
    if work_status in {"ACTIVE", "REVIEW"} and any(state.get("status") == "BLOCKED" for state in current_assignment_states):
        work_status = "BLOCKED"

    seats: list[dict[str, Any]] = []
    for seat_id, meta in SEATS.items():
        assignment = assignments.get(seat_id)
        if assignment:
            state = assignment_state.get(assignment["id"], {"status": "READY", "blockers": "", "last_event": ""})
            seats.append({
                "id": seat_id,
                "name": meta["name"],
                "kind": meta["kind"],
                "status": state["status"],
                "assignment_id": assignment["id"],
                "role": str(assignment.get("role") or ""),
                "task": str(assignment.get("title") or assignment.get("goal") or ""),
                "blockers": state["blockers"],
                "last_event": state["last_event"],
                "updated_at": str(assignment.get("created_at") or ""),
            })
        else:
            seats.append({"id": seat_id, "name": meta["name"], "kind": meta["kind"], "status": "IDLE" if meta["permanent"] else "CLOSED", "assignment_id": "", "role": "", "task": "", "blockers": "", "last_event": "", "updated_at": ""})
    record_verdict = str((latest_gate or {}).get("verdict") or "PENDING").upper()
    projected_verdict, gate_stale, stale_reason = _projected_gate_validity(work_root, latest_gate)

    gate = {
        "profile": str((latest_gate or {}).get("profile") or "generic"),
        "verdict": projected_verdict,
        "record_verdict": record_verdict,
        "business_head_sha": str((latest_gate or {}).get("business_head_sha") or ""),
        "checks": (latest_gate or {}).get("checks") if isinstance((latest_gate or {}).get("checks"), list) else [],
        "evidence": str((latest_gate or {}).get("evidence") or ""),
        "record_id": str((latest_gate or {}).get("id") or ""),
        "stale": gate_stale,
        "stale_reason": stale_reason,
        "waived": sorted(filter(None, waived_checks)),
    }
    return {"records": records, "seats": seats, "gate": gate, "status": work_status, "messages": messages}

def rebuild_work_item_projection(work_root: Path) -> None:
    projection = project_records(work_root)
    work_fields, work_body = parse_frontmatter(work_root / "WORK.md")
    work_fields["status"] = projection["status"]
    work_fields["owner"] = "1"
    projection_time = str(projection["records"][-1].get("created_at") or "") if projection["records"] else work_fields.get("created_at", "")
    work_fields["updated_at"] = projection_time
    write_document(work_root / "WORK.md", work_fields, work_body)
    rows = ["| 座位 | 类型 | 当前角色 | 任务 | 状态 |", "|---|---|---|---|---|"]
    for seat in projection["seats"]:
        rows.append(f"| {seat['id']} | {seat['kind']} | {seat['role'] or '-'} | {seat['task'] or '-'} | {seat['status']} |")
        slot_root = work_root / "slots" / seat["id"]
        if seat["status"] == "CLOSED" and not slot_root.exists():
            continue
        write_document(slot_root / "TASK.md", {"work_item": work_root.name, "seat": seat["id"], "assignment": seat["assignment_id"], "role": seat["role"], "status": seat["status"], "updated_at": projection_time}, f"# {seat['task'] or seat['name']}\n\n这是 Record 重放生成的投影，不是任务事实源。")
        write_document(slot_root / "STATE.md", {"work_item": work_root.name, "seat": seat["id"], "status": seat["status"], "assignment": seat["assignment_id"], "blockers": seat["blockers"], "last_event": seat["last_event"], "updated_at": projection_time}, "# State\n\n这是 Record 重放生成的投影，不要直接编辑。")
    write_document(work_root / "BOARD.md", {"work_item": work_root.name, "records_head": projection["records"][-1]["id"] if projection["records"] else "", "updated_at": projection_time}, f"# {work_root.name} Seats\n\n" + "\n".join(rows))
    gate = projection["gate"]
    check_lines = [f"- {item.get('id', 'check')}: {item.get('result', 'UNKNOWN')}" for item in gate["checks"] if isinstance(item, dict)]
    body = "# Gate Projection\n\n由最新 Gate Record 投影生成。\n\n" + ("\n".join(check_lines) if check_lines else "暂无 Gate Record。")
    write_document(work_root / "RELEASE.md", {"work_item": work_root.name, "profile": gate["profile"], "verdict": gate["verdict"], "record_verdict": gate["record_verdict"], "business_head_sha": gate["business_head_sha"], "stale": gate["stale"], "stale_reason": gate["stale_reason"], "record_id": gate["record_id"], "updated_at": projection_time}, body)


def rebuild_projections(state_root: Path) -> dict[str, int]:
    count = 0
    with orchestration_write_lock(state_root):
        for work_root in work_item_directories(state_root):
            if (work_root / "roles").is_dir():
                continue
            rebuild_work_item_projection(work_root)
            count += 1
        render_project_board(state_root)
    return {"rebuilt": count}


def read_work_item(work_root: Path) -> dict[str, Any]:
    fields, body = parse_frontmatter(work_root / "WORK.md")
    projection = project_records(work_root)
    return {
        "id": work_root.name,
        "title": fields.get("title") or read_title_from_body(body, work_root.name),
        "status": projection["status"],
        "priority": fields.get("priority", "NORMAL").upper(),
        "owner": "1",
        "profile": fields.get("profile") or "generic",
        "goal": markdown_section(body, "目标"),
        "acceptance": markdown_section(body, "验收"),
        "seats": projection["seats"],
        "gate": projection["gate"],
        "records": projection["records"][-20:],
        "records_head": projection["records"][-1]["id"] if projection["records"] else "",
        "created_at": fields.get("created_at", ""),
        "updated_at": fields.get("updated_at", ""),
    }


def aggregate_agents(work_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    agents: list[dict[str, Any]] = []
    for seat_id, meta in SEATS.items():
        assignments: list[dict[str, Any]] = []
        ever_opened = meta["permanent"]
        for item in work_items:
            for seat in item.get("seats", []):
                if seat["id"] != seat_id:
                    continue
                ever_opened = ever_opened or seat["status"] != "CLOSED"
                if seat["assignment_id"] and seat["status"] in {"READY", "WORKING", "BLOCKED"} and item["status"] not in {"DONE", "CANCELLED"}:
                    assignments.append({"work_item": item["id"], "work_title": item["title"], **seat})
        statuses = {a["status"] for a in assignments}
        if "BLOCKED" in statuses:
            status = "BLOCKED"
        elif "WORKING" in statuses:
            status = "WORKING"
        elif assignments:
            status = "READY"
        else:
            status = "IDLE" if ever_opened else "CLOSED"
        agents.append({"id": seat_id, "name": meta["name"], "kind": meta["kind"], "status": status, "assignments": assignments})
    return agents


def build_project_snapshot(project: dict[str, Any]) -> dict[str, Any]:
    state_root = orchestration_root(project)
    project_fields, _ = parse_frontmatter(state_root / "PROJECT.md")
    work_items = [read_work_item(path) for path in work_item_directories(state_root)] if state_root.is_dir() else []
    work_items.sort(key=lambda item: item["id"], reverse=True)
    agents = aggregate_agents(work_items)
    return {
        "id": project["id"],
        "name": project_fields.get("name") or project["name"],
        "path": project.get("path", ""),
        "orchestration_path": str(state_root),
        "initialized": (state_root / "PROJECT.md").is_file(),
        "schema": project_fields.get("schema", ""),
        "profile": project_fields.get("profile") or project.get("profile") or "generic",
        "needs_business_git": (project_fields.get("needs_business_git") == "true") if project_fields else bool(project.get("needs_business_git")),
        "default_branch": project_fields.get("default_branch", ""),
        "repository_identity": project_fields.get("repository_identity", ""),
        "work_items": work_items,
        "agents": agents,
        "git": business_git_snapshot(project, project_fields),
        "counts": {
            "total": len(work_items),
            "active": sum(item["status"] == "ACTIVE" for item in work_items),
            "blocked": sum(item["status"] == "BLOCKED" for item in work_items),
            "review": sum(item["status"] == "REVIEW" for item in work_items),
            "done": sum(item["status"] == "DONE" for item in work_items),
            "working_agents": sum(agent["status"] in {"WORKING", "READY", "BLOCKED"} for agent in agents),
        },
    }


def build_snapshot(projects: list[dict[str, Any]]) -> dict[str, Any]:
    snapshots = [build_project_snapshot(project) for project in projects]
    return {
        "generated_at": now_iso(),
        "schema": SCHEMA_VERSION,
        "projects": snapshots,
        "totals": {
            "projects": len(snapshots),
            "work_items": sum(p["counts"]["total"] for p in snapshots),
            "active_work_items": sum(p["counts"]["active"] + p["counts"]["review"] for p in snapshots),
            "blocked_work_items": sum(p["counts"]["blocked"] for p in snapshots),
            "working_agents": sum(p["counts"]["working_agents"] for p in snapshots),
        },
    }


def render_project_board(state_root: Path) -> None:
    rows = ["| ID | 标题 | 状态 | 优先级 | Owner | Gate |", "|---|---|---|---|---|---|"]
    updated_at = ""
    for work_root in reversed(work_item_directories(state_root)):
        item = read_work_item(work_root)
        title = str(item["title"]).replace("|", "\\|")
        rows.append(f"| {item['id']} | {title} | {item['status']} | {item['priority']} | {item['owner']} | {item['gate']['verdict']} |")
        updated_at = max(updated_at, str(item.get("updated_at") or item.get("created_at") or ""))
    write_document(state_root / "BOARD.md", {"schema": SCHEMA_VERSION, "updated_at": updated_at}, "# Work Items\n\n" + "\n".join(rows))


def create_work_item(project: dict[str, Any], payload: dict[str, Any], *, sync_git: bool = False, push_remote: bool = False) -> tuple[dict[str, Any], dict[str, str]]:
    if push_remote and not sync_git:
        raise SkillError("push_requires_sync", "push_remote=true 必须同时启用 sync_git")
    title = clean_scalar(payload.get("title"), field="title", max_length=180)
    goal = clean_text(payload.get("goal") or title, field="goal", max_length=4000)
    acceptance = clean_text(payload.get("acceptance") or "由 1 号总管补充与任务风险匹配的可验证完成条件。", field="acceptance", max_length=4000)
    priority = str(payload.get("priority") or "NORMAL").strip().upper()
    if priority not in PRIORITIES:
        raise SkillError("invalid_priority", f"priority 必须是 {', '.join(sorted(PRIORITIES))}")
    profile = clean_scalar(payload.get("profile") or project.get("profile") or "generic", field="profile", max_length=80)
    state_root = orchestration_root(project)
    with orchestration_write_lock(state_root):
        state_root = initialize_orchestration_repo(project, mode="existing")
        project_fields, _ = parse_frontmatter(state_root / "PROJECT.md")
        if project_fields.get("schema") != SCHEMA_VERSION:
            raise SkillError("unsupported_schema", f"只支持 schema={SCHEMA_VERSION}", HTTPStatus.CONFLICT)
        for work_root in work_item_directories(state_root):
            existing = read_work_item(work_root)
            if existing["title"].casefold() == title.casefold() and existing["status"] not in {"DONE", "CANCELLED"}:
                raise SkillError("duplicate_work_item", f"已有未完成的同名任务: {existing['id']}", HTTPStatus.CONFLICT)
        work_item_id = next_work_item_id(state_root)
        timestamp = now_iso()
        work_root = state_root / "work-items" / work_item_id
        write_document(work_root / "WORK.md", {"id": work_item_id, "title": title, "status": "ACTIVE", "priority": priority, "owner": "1", "profile": profile, "created_at": timestamp, "updated_at": timestamp, "source": clean_scalar(payload.get("source") or "user", field="source", max_length=40)}, f"# {title}\n\n## 目标\n\n{goal}\n\n## 验收\n\n{acceptance}")
        _append_record_unlocked(work_root, "assignment", "1", {"assignee": "1", "role": "orchestrator", "title": f"接管、拆分并推进：{title}", "goal": goal, "acceptance": acceptance})
        rebuild_work_item_projection(work_root)
        render_project_board(state_root)
        item = read_work_item(work_root)
        git_sync = (
            sync_orchestration_git(
                project,
                work_item_sync_paths(state_root, item["id"]),
                f"chore(orchestration): 添加 {item['id']} {item['title']}",
                push_remote=push_remote,
            )
            if sync_git
            else {"status": "skipped", "reason": "not_requested"}
        )
    return item, git_sync


def work_item_sync_paths(state_root: Path, work_item_id: str) -> list[str]:
    paths = ["PROJECT.md", "BOARD.md", f"work-items/{work_item_id}"]
    for bootstrap_path in (".gitignore", "DECISIONS.md"):
        tracked = run_git(state_root, "ls-files", "--error-unmatch", bootstrap_path, check=False)
        if tracked.returncode != 0 and (state_root / bootstrap_path).is_file():
            paths.append(bootstrap_path)
    return paths


def record_sync_paths(work_item_id: str, record: dict[str, Any]) -> list[str]:
    record_id = clean_scalar(record.get("id"), field="record.id", max_length=80)
    return [f"work-items/{work_item_id}/records/{record_id}.json"]


def sync_orchestration_git(project: dict[str, Any], paths: list[str], message: str, *, push_remote: bool = False) -> dict[str, str]:
    state_root = orchestration_root(project)
    branch = "main"
    try:
        # Record 文件彼此独立，但 Git index 是整个仓库共享状态；使用跨进程 orchestration 锁串行化 Git 操作，
        # 否则独立 Agent 进程的 `git add/commit` 会互相带入 staged 内容或竞争 index.lock。
        with orchestration_write_lock(state_root):
            top = run_git(state_root, "rev-parse", "--show-toplevel", check=False)
            if top.returncode != 0 or Path(top.stdout.strip()).resolve() != state_root:
                return {"status": "skipped", "reason": "orchestration_not_git_repository"}
            branch = current_branch(state_root)
            run_git(state_root, "add", "--", *paths)
            diff = run_git(state_root, "diff", "--cached", "--quiet", "--", *paths, check=False)
            if diff.returncode == 0:
                return {"status": "synced", "reason": "no_changes", "branch": branch}
            if diff.returncode != 1:
                return {"status": "failed", "reason": "git_diff_failed", "branch": branch}
            commit = run_git(state_root, "commit", "-m", message, "--", *paths, check=False)
            if commit.returncode != 0:
                return {"status": "failed", "reason": "git_commit_failed", "detail": (commit.stderr or commit.stdout).strip()[-500:], "branch": branch}
            sha = run_git(state_root, "rev-parse", "--short", "HEAD").stdout.strip()
            if not push_remote:
                return {"status": "committed_not_pushed", "reason": "push_not_requested", "branch": branch, "commit": sha}
            upstream = run_git(state_root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
            if upstream.returncode == 0:
                push = run_git(state_root, "push", check=False)
            else:
                remote = run_git(state_root, "remote", "get-url", "origin", check=False)
                if remote.returncode != 0:
                    return {"status": "committed_not_pushed", "reason": "no_upstream_or_origin", "branch": branch, "commit": sha}
                push = run_git(state_root, "push", "-u", "origin", branch, check=False)
            if push.returncode != 0:
                return {"status": "committed_not_pushed", "reason": "git_push_failed", "branch": branch, "commit": sha, "detail": (push.stderr or push.stdout).strip()[-500:]}
            return {"status": "synced", "reason": "committed_and_pushed", "branch": branch, "commit": sha}
    except SkillError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "reason": "git_command_failed", "branch": branch, "detail": str(exc)[-500:]}


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "MultiAgentConsole/4.2"

    @property
    def projects(self) -> list[dict[str, Any]]:
        return self.server.projects  # type: ignore[attr-defined]

    def log_message(self, format_string: str, *args: Any) -> None:
        sys.stderr.write("console: " + (format_string % args) + "\n")

    def send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_asset(self, path: Path, content_type: str) -> None:
        if not path.is_file() or WEB_ROOT not in path.resolve().parents:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _payload(self) -> dict[str, Any]:
        origin = self.headers.get("Origin", "")
        if origin and (urlparse(origin).hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
            raise SkillError("origin_rejected", "只接受本机控制台请求", HTTPStatus.FORBIDDEN)
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY_BYTES:
            raise SkillError("invalid_body", "请求体为空或过大")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise SkillError("invalid_body", "请求体必须是 JSON 对象")
        return payload

    def _project(self, project_id: str) -> dict[str, Any]:
        project = next((item for item in self.projects if item["id"] == project_id), None)
        if project is None:
            raise SkillError("project_not_allowed", "项目不在本次控制台允许列表中", HTTPStatus.FORBIDDEN)
        return project

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json({"ok": True, "skill": SKILL_NAME, "schema": SCHEMA_VERSION})
        elif path == "/api/snapshot":
            self.send_json({"ok": True, "data": build_snapshot(self.projects)})
        elif path in {"/", "/index.html"}:
            self.send_asset(WEB_ROOT / "index.html", "text/html; charset=utf-8")
        elif path in {"/add", "/add.html"}:
            self.send_asset(WEB_ROOT / "add.html", "text/html; charset=utf-8")
        elif path == "/assets/styles.css":
            self.send_asset(WEB_ROOT / "styles.css", "text/css; charset=utf-8")
        elif path == "/assets/dashboard.js":
            self.send_asset(WEB_ROOT / "dashboard.js", "text/javascript; charset=utf-8")
        elif path == "/assets/add.js":
            self.send_asset(WEB_ROOT / "add.js", "text/javascript; charset=utf-8")
        elif path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            payload = self._payload()
            project_id = clean_scalar(payload.get("project_id"), field="project_id", max_length=120)
            project = self._project(project_id)
            if path == "/api/work-items":
                item, git_sync = create_work_item(project, payload, sync_git=bool(payload.get("sync_git")))
                self.send_json({"ok": True, "data": item, "git_sync": git_sync}, HTTPStatus.CREATED)
            elif path == "/api/records":
                # 浏览器控制台只负责观察与创建 Work Item；Agent Record 必须通过 Skill/CLI 通道写入，
                # 避免任意本机网页脚本伪装成 1/2/3/4/5 写 Assignment 或 Gate。
                raise SkillError("record_api_disabled", "Record 写入只允许通过 Skill/CLI 通道", HTTPStatus.FORBIDDEN)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except SkillError as exc:
            self.send_json({"ok": False, "error": {"code": exc.code, "message": exc.message}}, exc.status)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self.send_json({"ok": False, "error": {"code": "invalid_json", "message": "请求体不是合法 JSON"}}, HTTPStatus.BAD_REQUEST)


def serve(projects: list[dict[str, Any]], port: int) -> None:
    if not 1 <= port <= 65535:
        raise SkillError("invalid_port", "port 必须在 1–65535 之间")
    server = ThreadingHTTPServer(("127.0.0.1", port), ConsoleHandler)
    server.projects = projects  # type: ignore[attr-defined]
    actual_port = server.server_address[1]
    print(json.dumps({"ok": True, "url": f"http://127.0.0.1:{actual_port}", "projects": len(projects), "skill": SKILL_NAME}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def read_request() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {"skill_action": "status"}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise SkillError("invalid_input", "stdin 必须是 JSON 对象")
    return payload


def _record_action_payload(action: str, request: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    mapping = {"assign": "assignment", "post_message": "message", "report_event": "event", "submit_gate": "gate"}
    record_type = mapping[action]
    sender = str(request.get("from") or ("1" if action == "assign" else "2" if action == "submit_gate" else "")).strip()
    payload = request.get("payload") if isinstance(request.get("payload"), dict) else {k: v for k, v in request.items() if k not in {"skill_action", "projects", "project_id", "work_item", "from", "sync_git"}}
    return record_type, sender, payload


def main() -> None:
    try:
        request = read_request()
        action = str(request.get("skill_action") or "status").strip()
        if action == "status":
            workspace_root = configured_workspace_root()
            print(json.dumps({"ok": True, "skill": SKILL_NAME, "schema": SCHEMA_VERSION, "console": True, "actions": ["status", "greenfield_init", "snapshot", "add_work_item", "append_record", "assign", "post_message", "report_event", "submit_gate", "rebuild_projections", "serve"], "workspace": {"configured": workspace_root is not None, "root": str(workspace_root) if workspace_root else ""}}, ensure_ascii=False))
            return
        if action == "greenfield_init":
            if request.get("push_remote") or request.get("sync_git"):
                raise SkillError("sync_not_supported", "greenfield_init 不执行 orchestration Git 同步；初始化后再通过 add_work_item / Record 动作同步")
            print(json.dumps({"ok": True, "data": init_greenfield_project(request)}, ensure_ascii=False))
            return
        projects = normalize_projects(request.get("projects"))
        if action == "snapshot":
            print(json.dumps({"ok": True, "data": build_snapshot(projects)}, ensure_ascii=False))
            return
        if action == "serve":
            serve(projects, int(request.get("port") or 8765))
            return
        project_id = clean_scalar(request.get("project_id"), field="project_id", max_length=120)
        project = next((item for item in projects if item["id"] == project_id), None)
        if project is None:
            raise SkillError("project_not_allowed", "project_id 不在 projects 中")
        if action == "add_work_item":
            if request.get("push_remote") and not request.get("sync_git"):
                raise SkillError("push_requires_sync", "push_remote=true 必须同时启用 sync_git")
            item, git_sync = create_work_item(
                project,
                request,
                sync_git=bool(request.get("sync_git")),
                push_remote=bool(request.get("push_remote")),
            )
            print(json.dumps({"ok": True, "data": item, "git_sync": git_sync}, ensure_ascii=False))
            return
        elif action in {"append_record", "assign", "post_message", "report_event", "submit_gate"}:
            if request.get("push_remote") and not request.get("sync_git"):
                raise SkillError("push_requires_sync", "push_remote=true 必须同时启用 sync_git")
            work_item = clean_scalar(request.get("work_item"), field="work_item", max_length=40)
            if action == "append_record":
                record_type = clean_scalar(request.get("type"), field="type", max_length=40)
                sender = clean_scalar(request.get("from"), field="from", max_length=20)
                payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
            else:
                record_type, sender, payload = _record_action_payload(action, request)
            if request.get("sync_git"):
                data, git_sync = append_record_and_sync(
                    project,
                    work_item,
                    record_type,
                    sender,
                    payload,
                    push_remote=bool(request.get("push_remote")),
                )
            else:
                data = append_record(orchestration_root(project), work_item, record_type, sender, payload)
                git_sync = {"status": "skipped", "reason": "not_requested"}
            print(json.dumps({"ok": True, "data": data, "git_sync": git_sync}, ensure_ascii=False))
            return
        elif action == "rebuild_projections":
            data = rebuild_projections(orchestration_root(project))
        else:
            raise SkillError("unknown_action", f"不支持的 skill_action: {action}")
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
    except SkillError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False))
        raise SystemExit(2)
    except (ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "invalid_input", "message": str(exc)}}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
