#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SKILL_ROOT = Path(__file__).resolve().parent
WEB_ROOT = SKILL_ROOT / "web"
STATE_DIR = ".multi-agent"
WORK_ITEM_PATTERN = re.compile(r"^WI-(\d{4,})$")
ROLE_NAMES = {
    "0": "总控与集成",
    "1": "核心系统",
    "2": "Web Backend",
    "3": "Web Frontend",
    "4": "QA 独立审核",
    "5": "DevX / 文档 / 工具链",
    "6": "Confirmatory Prior",
    "7": "Security Red Team",
    "8": "Evaluation Benchmark",
}
ACTIVE_WORK_STATUSES = {"ACTIVE", "BLOCKED", "REVIEW"}
ACTIVE_ROLE_STATUSES = {"ACTIVE", "IN_PROGRESS", "BLOCKED", "REVIEW", "READY"}
PRIORITIES = {"LOW", "NORMAL", "HIGH", "URGENT"}
MAX_BODY_BYTES = 64 * 1024
WRITE_LOCK = threading.Lock()


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


def clean_text(value: Any, *, field: str, max_length: int = 4000) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()
    if not text:
        raise SkillError("invalid_input", f"{field} 不能为空")
    if len(text) > max_length:
        raise SkillError("invalid_input", f"{field} 过长，最多 {max_length} 个字符")
    return text


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "project"


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
        if separator:
            raw_value = value.strip()
            if raw_value.startswith('"'):
                try:
                    decoded = json.loads(raw_value)
                    fields[key.strip()] = str(decoded)
                    continue
                except json.JSONDecodeError:
                    pass
            fields[key.strip()] = raw_value
    return fields, text[end + 5 :]


def write_document(path: Path, fields: dict[str, Any], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, raw_value in fields.items():
        value = str(raw_value).replace("\r", " ").replace("\n", " ").strip()
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.extend(["---", "", body.rstrip(), ""])
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(path)


def read_title_from_body(body: str, fallback: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def normalize_projects(raw_projects: Any) -> list[dict[str, str]]:
    if not isinstance(raw_projects, list) or not raw_projects:
        raise SkillError("invalid_projects", "projects 必须是至少包含一个项目的数组")

    projects: list[dict[str, str]] = []
    used_ids: set[str] = set()
    for index, raw in enumerate(raw_projects, start=1):
        if isinstance(raw, str):
            root = Path(raw).expanduser().resolve()
            name = root.name
            requested_id = ""
        elif isinstance(raw, dict):
            path_value = clean_scalar(raw.get("path"), field=f"projects[{index}].path", max_length=4096)
            root = Path(path_value).expanduser().resolve()
            name = clean_scalar(raw.get("name") or root.name, field=f"projects[{index}].name", max_length=120)
            requested_id = str(raw.get("id") or "").strip()
        else:
            raise SkillError("invalid_projects", f"projects[{index}] 必须是路径字符串或对象")

        if not root.is_dir():
            raise SkillError("project_not_found", f"项目目录不存在: {root}")

        project_id = safe_slug(requested_id or name)
        if project_id in used_ids:
            suffix = 2
            while f"{project_id}-{suffix}" in used_ids:
                suffix += 1
            project_id = f"{project_id}-{suffix}"
        used_ids.add(project_id)
        projects.append({"id": project_id, "name": name, "path": str(root)})
    return projects


def work_item_directories(project_root: Path) -> list[Path]:
    root = project_root / STATE_DIR / "work-items"
    if not root.is_dir():
        return []
    return sorted(
        [path for path in root.iterdir() if path.is_dir() and WORK_ITEM_PATTERN.fullmatch(path.name)],
        key=lambda path: int(WORK_ITEM_PATTERN.fullmatch(path.name).group(1)),
    )


def read_role_task(role_dir: Path, work_item_id: str, role_id: str) -> dict[str, str] | None:
    task_fields, task_body = parse_frontmatter(role_dir / "TASK.md")
    state_fields, _ = parse_frontmatter(role_dir / "STATE.md")
    if not task_fields and not state_fields and not task_body.strip():
        return None

    task_title = task_fields.get("title") or state_fields.get("task") or read_title_from_body(task_body, "未命名角色任务")
    status = (state_fields.get("status") or task_fields.get("status") or "READY").upper()
    return {
        "work_item": work_item_id,
        "role": role_id,
        "task": task_title,
        "status": status,
        "branch": state_fields.get("branch", ""),
        "last_commit": state_fields.get("last_commit", ""),
        "blockers": state_fields.get("blockers", ""),
        "updated_at": state_fields.get("updated_at") or task_fields.get("updated_at") or "",
    }


def read_work_item(project_root: Path, work_dir: Path) -> dict[str, Any]:
    fields, body = parse_frontmatter(work_dir / "WORK.md")
    work_item_id = fields.get("id") or work_dir.name
    title = fields.get("title") or read_title_from_body(body, work_item_id)
    roles: list[dict[str, str]] = []
    roles_root = work_dir / "roles"
    for role_id in ROLE_NAMES:
        role_task = read_role_task(roles_root / role_id, work_item_id, role_id)
        if role_task:
            roles.append(role_task)
    return {
        "id": work_item_id,
        "title": title,
        "status": fields.get("status", "BACKLOG").upper(),
        "priority": fields.get("priority", "NORMAL").upper(),
        "owner": fields.get("owner", "0"),
        "created_at": fields.get("created_at", ""),
        "updated_at": fields.get("updated_at", ""),
        "roles": roles,
    }


def aggregate_agents(work_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    agents: list[dict[str, Any]] = []
    for role_id, role_name in ROLE_NAMES.items():
        assignments: list[dict[str, str]] = []
        for work_item in work_items:
            if work_item["status"] not in ACTIVE_WORK_STATUSES:
                continue
            for role_task in work_item["roles"]:
                if role_task["role"] == role_id and role_task["status"] in ACTIVE_ROLE_STATUSES:
                    assignments.append(
                        {
                            "work_item": work_item["id"],
                            "work_title": work_item["title"],
                            "task": role_task["task"],
                            "status": role_task["status"],
                            "branch": role_task["branch"],
                            "blockers": role_task["blockers"],
                        }
                    )
        statuses = {item["status"] for item in assignments}
        if "BLOCKED" in statuses:
            status = "BLOCKED"
        elif statuses & {"ACTIVE", "IN_PROGRESS"}:
            status = "WORKING"
        elif "REVIEW" in statuses:
            status = "REVIEW"
        elif assignments:
            status = "READY"
        else:
            status = "IDLE"
        agents.append({"id": role_id, "name": role_name, "status": status, "assignments": assignments})
    return agents


def build_project_snapshot(project: dict[str, str]) -> dict[str, Any]:
    root = Path(project["path"])
    project_fields, _ = parse_frontmatter(root / STATE_DIR / "PROJECT.md")
    work_items = [read_work_item(root, path) for path in work_item_directories(root)]
    work_items.sort(key=lambda item: item["id"], reverse=True)
    agents = aggregate_agents(work_items)
    return {
        "id": project["id"],
        "name": project_fields.get("name") or project["name"],
        "initialized": (root / STATE_DIR / "PROJECT.md").is_file(),
        "work_items": work_items,
        "agents": agents,
        "counts": {
            "total": len(work_items),
            "active": sum(item["status"] == "ACTIVE" for item in work_items),
            "blocked": sum(item["status"] == "BLOCKED" for item in work_items),
            "review": sum(item["status"] == "REVIEW" for item in work_items),
            "done": sum(item["status"] == "DONE" for item in work_items),
            "working_agents": sum(agent["status"] != "IDLE" for agent in agents),
        },
    }


def build_snapshot(projects: list[dict[str, str]]) -> dict[str, Any]:
    project_snapshots = [build_project_snapshot(project) for project in projects]
    return {
        "generated_at": now_iso(),
        "projects": project_snapshots,
        "totals": {
            "projects": len(project_snapshots),
            "work_items": sum(project["counts"]["total"] for project in project_snapshots),
            "active_work_items": sum(project["counts"]["active"] + project["counts"]["review"] for project in project_snapshots),
            "blocked_work_items": sum(project["counts"]["blocked"] for project in project_snapshots),
            "working_agents": sum(project["counts"]["working_agents"] for project in project_snapshots),
        },
    }


def next_work_item_id(project_root: Path) -> str:
    highest = 0
    for path in work_item_directories(project_root):
        match = WORK_ITEM_PATTERN.fullmatch(path.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"WI-{highest + 1:04d}"


def render_project_board(project_root: Path) -> None:
    board_path = project_root / STATE_DIR / "BOARD.md"
    existing = board_path.read_text(encoding="utf-8") if board_path.is_file() else "# Work Items\n\n"
    start_marker = "<!-- multi-agent-console:start -->"
    end_marker = "<!-- multi-agent-console:end -->"
    rows = ["| ID | 标题 | 状态 | 优先级 | Owner |", "|---|---|---|---|---|"]
    for work_dir in reversed(work_item_directories(project_root)):
        item = read_work_item(project_root, work_dir)
        safe_title = str(item["title"]).replace("|", "\\|")
        rows.append(f"| {item['id']} | {safe_title} | {item['status']} | {item['priority']} | {item['owner']} |")
    managed = f"{start_marker}\n" + "\n".join(rows) + f"\n{end_marker}"
    if start_marker in existing and end_marker in existing:
        prefix, remainder = existing.split(start_marker, 1)
        _, suffix = remainder.split(end_marker, 1)
        updated = prefix.rstrip() + "\n\n" + managed + suffix
    else:
        updated = existing.rstrip() + "\n\n" + managed + "\n"
    board_path.parent.mkdir(parents=True, exist_ok=True)
    board_path.write_text(updated, encoding="utf-8")


def create_work_item(project: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    title = clean_scalar(payload.get("title"), field="title", max_length=180)
    goal = clean_text(payload.get("goal") or title, field="goal", max_length=4000)
    priority = str(payload.get("priority") or "NORMAL").strip().upper()
    if priority not in PRIORITIES:
        raise SkillError("invalid_priority", f"priority 必须是 {', '.join(sorted(PRIORITIES))}")
    owner = str(payload.get("owner") if payload.get("owner") is not None else "0").strip()
    if owner not in ROLE_NAMES:
        raise SkillError("invalid_owner", "owner 必须是 0–8")

    project_root = Path(project["path"])
    state_root = project_root / STATE_DIR
    with WRITE_LOCK:
        state_root.mkdir(parents=True, exist_ok=True)
        project_file = state_root / "PROJECT.md"
        if not project_file.exists():
            write_document(
                project_file,
                {"name": project["name"], "status": "ACTIVE", "created_at": now_iso()},
                f"# {project['name']}\n\n由 multi-agent-software-orchestration 管理的项目状态。",
            )

        for work_dir in work_item_directories(project_root):
            existing = read_work_item(project_root, work_dir)
            if existing["title"].casefold() == title.casefold() and existing["status"] not in {"DONE", "CANCELLED"}:
                raise SkillError("duplicate_work_item", f"已有未完成的同名任务: {existing['id']}", HTTPStatus.CONFLICT)

        work_item_id = next_work_item_id(project_root)
        timestamp = now_iso()
        work_root = state_root / "work-items" / work_item_id
        role_root = work_root / "roles" / owner
        write_document(
            work_root / "WORK.md",
            {
                "id": work_item_id,
                "title": title,
                "status": "ACTIVE",
                "priority": priority,
                "owner": owner,
                "created_at": timestamp,
                "updated_at": timestamp,
                "source": "console",
            },
            f"# {title}\n\n## 目标\n\n{goal}\n\n## 验收\n\n由 {owner} 号角色根据 Skill 拆分并补充可验证完成条件。",
        )
        write_document(
            work_root / "BOARD.md",
            {"work_item": work_item_id, "updated_at": timestamp},
            f"# {work_item_id} Role Tasks\n\n| 角色 | 任务 | 状态 |\n|---|---|---|\n| {owner} | 接管、拆分并推进：{title} | ACTIVE |",
        )
        write_document(
            work_root / "RELEASE.md",
            {"work_item": work_item_id, "status": "PENDING", "updated_at": timestamp},
            "# Release Gates\n\n等待 0 号根据任务范围确定 QA、确认、安全和 Benchmark 门禁。",
        )
        write_document(
            role_root / "TASK.md",
            {
                "work_item": work_item_id,
                "role": owner,
                "title": f"接管、拆分并推进：{title}" if owner == "0" else title,
                "status": "ACTIVE",
                "updated_at": timestamp,
            },
            f"# {title}\n\n{goal}\n\n只处理 {owner} 号角色职责范围；需要跨角色协作时更新 Work Item BOARD 并交给 0 号协调。",
        )
        write_document(
            role_root / "STATE.md",
            {
                "work_item": work_item_id,
                "role": owner,
                "status": "READY",
                "task": f"接管、拆分并推进：{title}" if owner == "0" else title,
                "branch": "",
                "last_commit": "",
                "blockers": "none",
                "updated_at": timestamp,
            },
            "# State\n\n等待该角色下一次唤醒后恢复并执行。",
        )
        render_project_board(project_root)
    return read_work_item(project_root, work_root)


def sync_orchestration_git(project: dict[str, str], work_item: dict[str, Any]) -> dict[str, str]:
    project_root = Path(project["path"])

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(project_root), *args],
            text=True,
            capture_output=True,
            timeout=20,
            check=check,
        )

    try:
        top_level = Path(git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        return {"status": "skipped", "reason": "not_git_repository"}
    if top_level != project_root.resolve():
        return {"status": "skipped", "reason": "project_root_not_git_toplevel"}

    try:
        branch = git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    except subprocess.SubprocessError:
        return {"status": "failed", "reason": "detached_head"}
    if not branch:
        return {"status": "failed", "reason": "detached_head"}

    try:
        git("add", "--", STATE_DIR)
        diff = git("diff", "--cached", "--quiet", "--", STATE_DIR, check=False)
        if diff.returncode == 0:
            return {"status": "synced", "reason": "no_changes", "branch": branch}
        if diff.returncode != 1:
            return {"status": "failed", "reason": "git_diff_failed", "branch": branch}

        title = clean_scalar(work_item.get("title"), field="title", max_length=120)
        message = f"chore(multi-agent): 添加 {work_item['id']} {title}"
        commit = git("commit", "-m", message, "--", STATE_DIR, check=False)
        if commit.returncode != 0:
            return {
                "status": "failed",
                "reason": "git_commit_failed",
                "branch": branch,
                "detail": (commit.stderr or commit.stdout).strip()[-500:],
            }
        sha = git("rev-parse", "--short", "HEAD").stdout.strip()

        upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
        if upstream.returncode == 0:
            push = git("push", check=False)
        else:
            remote = git("remote", "get-url", "origin", check=False)
            if remote.returncode != 0:
                return {"status": "committed_not_pushed", "reason": "no_upstream_or_origin", "branch": branch, "commit": sha}
            push = git("push", "-u", "origin", branch, check=False)
        if push.returncode != 0:
            return {
                "status": "committed_not_pushed",
                "reason": "git_push_failed",
                "branch": branch,
                "commit": sha,
                "detail": (push.stderr or push.stdout).strip()[-500:],
            }
        return {"status": "synced", "reason": "committed_and_pushed", "branch": branch, "commit": sha}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "reason": "git_command_failed", "branch": branch, "detail": str(exc)[-500:]}


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "MultiAgentConsole/1.0"

    @property
    def projects(self) -> list[dict[str, str]]:
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

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json({"ok": True})
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
        if urlparse(self.path).path != "/api/work-items":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        origin = self.headers.get("Origin", "")
        if origin:
            host = (urlparse(origin).hostname or "").lower()
            if host not in {"127.0.0.1", "localhost", "::1"}:
                self.send_json({"ok": False, "error": {"code": "origin_rejected", "message": "只接受本机控制台请求"}}, HTTPStatus.FORBIDDEN)
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise SkillError("invalid_body", "请求体为空或过大")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise SkillError("invalid_body", "请求体必须是 JSON 对象")
            project_id = clean_scalar(payload.get("project_id"), field="project_id", max_length=120)
            project = next((item for item in self.projects if item["id"] == project_id), None)
            if project is None:
                raise SkillError("project_not_allowed", "项目不在本次控制台允许列表中", HTTPStatus.FORBIDDEN)
            item = create_work_item(project, payload)
            git_sync = sync_orchestration_git(project, item) if payload.get("sync_git") else {"status": "skipped", "reason": "not_requested"}
            self.send_json({"ok": True, "data": item, "git_sync": git_sync}, HTTPStatus.CREATED)
        except SkillError as exc:
            self.send_json({"ok": False, "error": {"code": exc.code, "message": exc.message}}, exc.status)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json({"ok": False, "error": {"code": "invalid_json", "message": "请求体不是合法 JSON"}}, HTTPStatus.BAD_REQUEST)


def serve(projects: list[dict[str, str]], port: int) -> None:
    if not 1 <= port <= 65535:
        raise SkillError("invalid_port", "port 必须在 1–65535 之间")
    server = ThreadingHTTPServer(("127.0.0.1", port), ConsoleHandler)
    server.projects = projects  # type: ignore[attr-defined]
    actual_port = server.server_address[1]
    print(json.dumps({"ok": True, "url": f"http://127.0.0.1:{actual_port}", "projects": len(projects)}, ensure_ascii=False), flush=True)
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


def main() -> None:
    try:
        request = read_request()
        action = str(request.get("skill_action") or "status").strip()
        if action == "status":
            print(json.dumps({"ok": True, "skill": "multi-agent-software-orchestration", "console": True, "actions": ["status", "snapshot", "add_work_item", "serve"]}, ensure_ascii=False))
            return

        projects = normalize_projects(request.get("projects"))
        if action == "snapshot":
            print(json.dumps({"ok": True, "data": build_snapshot(projects)}, ensure_ascii=False))
        elif action == "add_work_item":
            project_id = clean_scalar(request.get("project_id"), field="project_id", max_length=120)
            project = next((item for item in projects if item["id"] == project_id), None)
            if project is None:
                raise SkillError("project_not_allowed", "project_id 不在 projects 中")
            item = create_work_item(project, request)
            git_sync = sync_orchestration_git(project, item) if request.get("sync_git") else {"status": "skipped", "reason": "not_requested"}
            print(json.dumps({"ok": True, "data": item, "git_sync": git_sync}, ensure_ascii=False))
        elif action == "serve":
            serve(projects, int(request.get("port") or 8765))
        else:
            raise SkillError("unknown_action", f"不支持的 skill_action: {action}")
    except SkillError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False))
        raise SystemExit(2)
    except (ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "invalid_input", "message": str(exc)}}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
