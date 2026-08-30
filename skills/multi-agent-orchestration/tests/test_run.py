from __future__ import annotations

import importlib.util
import inspect
import json
import os
import multiprocessing
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "run.py"


def lock_process_worker(state_root: str, output: str) -> None:
    module_spec = importlib.util.spec_from_file_location("multi_agent_orchestration_child", MODULE_PATH)
    assert module_spec and module_spec.loader
    child_run = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(child_run)
    with child_run.orchestration_write_lock(Path(state_root)):
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"start-{os.getpid()}\n")
            handle.flush()
        import time as child_time
        child_time.sleep(0.15)
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"end-{os.getpid()}\n")


SPEC = importlib.util.spec_from_file_location("multi_agent_orchestration_run", MODULE_PATH)
assert SPEC and SPEC.loader
run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run)


class OrchestrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "demo"
        self.root.mkdir()
        self.workspace = Path(self.tempdir.name) / "multi-agent-orchestration"
        self.previous_workspace = os.environ.get(run.WORKSPACE_ENV)
        os.environ[run.WORKSPACE_ENV] = str(self.workspace)
        self.projects = run.normalize_projects([{"id": "demo", "name": "Demo", "path": str(self.root), "profile": "software"}])
        self.project = self.projects[0]
        self.state_root = self.workspace / run.ORCHESTRATIONS_DIR / "demo"

    def tearDown(self) -> None:
        if self.previous_workspace is None:
            os.environ.pop(run.WORKSPACE_ENV, None)
        else:
            os.environ[run.WORKSPACE_ENV] = self.previous_workspace
        self.tempdir.cleanup()

    def create(self, **overrides):
        payload = {"title": "示例任务", "goal": "完成一个可验证目标"}
        payload.update(overrides)
        item, _ = run.create_work_item(self.project, payload)
        return item

    def test_new_work_item_defaults_to_orchestrator_and_record_truth(self) -> None:
        item = self.create()

        self.assertEqual("WI-0001", item["id"])
        self.assertEqual("1", item["owner"])
        self.assertEqual("ACTIVE", item["status"])
        self.assertFalse((self.state_root / "work-items/WI-0001/roles").exists())
        self.assertTrue((self.state_root / "work-items/WI-0001/records").is_dir())
        self.assertEqual("assignment", item["records"][0]["type"])
        self.assertEqual("1", item["records"][0]["from"])
        self.assertEqual("1", item["records"][0]["assignee"])
        self.assertEqual(["1", "2", "3", "4", "5"], [seat["id"] for seat in item["seats"]])
        self.assertEqual("IDLE", next(seat for seat in item["seats"] if seat["id"] == "2")["status"])
        self.assertEqual("CLOSED", next(seat for seat in item["seats"] if seat["id"] == "3")["status"])

    def test_worker_assignment_is_dynamic_and_role_is_soft_label(self) -> None:
        item = self.create()
        record = run.append_record(
            self.state_root,
            item["id"],
            "assignment",
            "1",
            {"assignee": "3", "role": "backend-investigator", "title": "调查服务端", "goal": "找到根因"},
        )
        refreshed = run.read_work_item(self.state_root / "work-items" / item["id"])
        seat = next(seat for seat in refreshed["seats"] if seat["id"] == "3")

        self.assertEqual("assignment", record["type"])
        self.assertEqual("READY", seat["status"])
        self.assertEqual("backend-investigator", seat["role"])
        self.assertEqual("调查服务端", seat["task"])

    def test_permanent_seats_cannot_be_repurposed(self) -> None:
        item = self.create()
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "assignment", "1", {"assignee": "1", "role": "worker", "title": "错误改任"})
        self.assertEqual("orchestrator_role_fixed", context.exception.code)
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "assignment", "1", {"assignee": "2", "role": "backend-worker", "title": "错误改任"})
        self.assertEqual("gatekeeper_role_fixed", context.exception.code)

        gate_assignment = run.append_record(
            self.state_root,
            item["id"],
            "assignment",
            "1",
            {"assignee": "2", "role": "independent-gatekeeper", "title": "独立检查"},
        )
        self.assertEqual("2", gate_assignment["assignee"])
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "assignment", "1", {"assignee": "1", "role": "manager", "title": "错误别名"})
        self.assertEqual("orchestrator_role_fixed", context.exception.code)
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "assignment", "user", {"assignee": "3", "role": "researcher", "title": "用户绕过总管"})
        self.assertEqual("user_message_only", context.exception.code)

    def test_worker_cannot_dispatch_but_can_message(self) -> None:
        item = self.create()
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "assignment", "3", {"assignee": "4", "title": "越权派工"})
        self.assertEqual("forbidden_dispatch", context.exception.code)
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "assignment", "1", {"assignee": "4", "title": "缺少任职"})
        self.assertEqual("worker_role_required", context.exception.code)
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "assignment", "1", {"assignee": "1", "role": "orchestrator"})
        self.assertEqual("invalid_assignment", context.exception.code)

        message = run.append_record(self.state_root, item["id"], "message", "3", {"to": "4", "body": "观察到一个相关线索"})
        self.assertEqual("message", message["type"])
        self.assertEqual("4", message["to"])
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "message", "3", {"to": "999", "body": "错误目标"})
        self.assertEqual("invalid_recipient", context.exception.code)
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "message", "3", {"to": "4", "body": ""})
        self.assertEqual("invalid_message", context.exception.code)
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "message", "3", {"to": "4", "body": "x" * 8001})
        self.assertEqual("invalid_message", context.exception.code)
        user_message = run.append_record(self.state_root, item["id"], "message", "user", {"to": "1", "body": "用户补充边界"})
        self.assertEqual("user", user_message["from"])

    def test_only_gatekeeper_can_write_gate(self) -> None:
        item = self.create()
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "gate", "1", {"profile": "generic", "verdict": "PASS", "checks": []})
        self.assertEqual("gatekeeper_only", context.exception.code)

        gate = run.append_record(
            self.state_root,
            item["id"],
            "gate",
            "2",
            {"profile": "software", "verdict": "PASS", "checks": [{"id": "qa", "result": "PASS"}]},
        )
        refreshed = run.read_work_item(self.state_root / "work-items" / item["id"])
        self.assertEqual(gate["id"], refreshed["gate"]["record_id"])
        self.assertEqual("PASS", refreshed["gate"]["verdict"])
        self.assertEqual([{"id": "qa", "result": "PASS"}], refreshed["gate"]["checks"])

    def test_gatekeeper_assignment_status_is_not_changed_by_gate_record(self) -> None:
        item = self.create()
        gate_assignment = run.append_record(
            self.state_root,
            item["id"],
            "assignment",
            "1",
            {"assignee": "2", "role": "independent-gatekeeper", "title": "独立验收"},
        )
        run.append_record(self.state_root, item["id"], "event", "2", {"kind": "started", "assignment": gate_assignment["id"]})
        run.append_record(
            self.state_root,
            item["id"],
            "gate",
            "2",
            {"profile": "generic", "verdict": "PASS", "checks": [{"id": "evidence", "result": "PASS"}]},
        )
        refreshed = run.read_work_item(self.state_root / "work-items" / item["id"])
        gatekeeper = next(seat for seat in refreshed["seats"] if seat["id"] == "2")
        self.assertEqual("WORKING", gatekeeper["status"])

    def test_events_drive_projection_without_mutating_records(self) -> None:
        item = self.create()
        assignment = run.append_record(self.state_root, item["id"], "assignment", "1", {"assignee": "3", "role": "researcher", "title": "查证"})
        original = (self.state_root / "work-items" / item["id"] / "records" / f"{assignment['id']}.json").read_text(encoding="utf-8")
        run.append_record(self.state_root, item["id"], "event", "3", {"kind": "blocked", "assignment": assignment["id"], "detail": "缺少证据"})
        refreshed = run.read_work_item(self.state_root / "work-items" / item["id"])

        self.assertEqual("BLOCKED", refreshed["status"])
        seat = next(seat for seat in refreshed["seats"] if seat["id"] == "3")
        self.assertEqual("BLOCKED", seat["status"])
        self.assertEqual("缺少证据", seat["blockers"])
        self.assertEqual(original, (self.state_root / "work-items" / item["id"] / "records" / f"{assignment['id']}.json").read_text(encoding="utf-8"))

    def test_worker_record_does_not_rewrite_shared_projections(self) -> None:
        item = self.create()
        work_root = self.state_root / "work-items" / item["id"]
        projection_paths = [work_root / "WORK.md", work_root / "BOARD.md", work_root / "RELEASE.md", self.state_root / "BOARD.md"]
        before = {path: path.read_bytes() for path in projection_paths}

        assignment = run.append_record(self.state_root, item["id"], "assignment", "1", {"assignee": "3", "role": "researcher", "title": "调查"})
        run.append_record(self.state_root, item["id"], "event", "3", {"kind": "started", "assignment": assignment["id"]})

        self.assertEqual(before, {path: path.read_bytes() for path in projection_paths})
        live = run.read_work_item(work_root)
        self.assertEqual("WORKING", next(seat for seat in live["seats"] if seat["id"] == "3")["status"])
        run.rebuild_projections(self.state_root)
        self.assertNotEqual(before[work_root / "BOARD.md"], (work_root / "BOARD.md").read_bytes())

    def test_record_git_sync_path_contains_only_new_record(self) -> None:
        item = self.create()
        record = run.append_record(self.state_root, item["id"], "message", "3", {"to": "1", "body": "进度"})
        self.assertEqual(
            [f"work-items/{item['id']}/records/{record['id']}.json"],
            run.record_sync_paths(item["id"], record),
        )

    def test_work_item_lifecycle_is_orchestrator_control_plane(self) -> None:
        item = self.create()
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "event", "3", {"kind": "wi_paused"})
        self.assertEqual("orchestrator_only", context.exception.code)
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "event", "user", {"kind": "wi_paused"})
        self.assertEqual("user_message_only", context.exception.code)

        run.append_record(self.state_root, item["id"], "event", "1", {"kind": "wi_paused"})
        self.assertEqual("PAUSED", run.read_work_item(self.state_root / "work-items" / item["id"])["status"])
        run.append_record(self.state_root, item["id"], "event", "1", {"kind": "wi_resumed"})
        self.assertEqual("ACTIVE", run.read_work_item(self.state_root / "work-items" / item["id"])["status"])
        run.append_record(self.state_root, item["id"], "event", "1", {"kind": "wi_status", "status": "REVIEW"})
        self.assertEqual("REVIEW", run.read_work_item(self.state_root / "work-items" / item["id"])["status"])
        run.append_record(self.state_root, item["id"], "event", "1", {"kind": "wi_status", "status": "BLOCKED"})
        self.assertEqual("BLOCKED", run.read_work_item(self.state_root / "work-items" / item["id"])["status"])
        run.append_record(self.state_root, item["id"], "event", "1", {"kind": "wi_status", "status": "DONE"})
        self.assertEqual("DONE", run.read_work_item(self.state_root / "work-items" / item["id"])["status"])
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "event", "1", {"kind": "wi_status", "status": "UNKNOWN"})
        self.assertEqual("invalid_work_status", context.exception.code)

    def test_serve_action_does_not_require_project_id(self) -> None:
        source = inspect.getsource(run.main)
        self.assertLess(source.index('if action == "serve"'), source.index('project_id = clean_scalar'))

    def test_greenfield_init_creates_only_local_orchestration_repo(self) -> None:
        request = {"name": "Greenfield", "slug": "greenfield", "goal": "demo", "profile": "research", "needs_business_git": False}
        result = run.init_greenfield_project(request)
        self.assertEqual("greenfield", result["id"])
        orchestration = self.workspace / run.ORCHESTRATIONS_DIR / "greenfield"
        self.assertTrue((orchestration / ".git").is_dir())
        remotes = subprocess.run(["git", "-C", str(orchestration), "remote"], text=True, capture_output=True, check=True).stdout.strip()
        self.assertEqual("", remotes)

    def test_non_software_project_does_not_require_business_git(self) -> None:
        projects = run.normalize_projects([{"id": "research", "name": "Research", "needs_business_git": False, "profile": "research"}])
        project = projects[0]
        item, _ = run.create_work_item(project, {"title": "研究主题", "goal": "形成证据化结论", "profile": "research"})
        snapshot = run.build_project_snapshot(project)

        self.assertEqual("research", item["profile"])
        self.assertFalse(snapshot["needs_business_git"])
        self.assertFalse(snapshot["git"]["available"])
        self.assertEqual("not_required", snapshot["git"]["reason"])

    def test_duplicate_open_title_is_rejected(self) -> None:
        self.create()
        with self.assertRaises(run.SkillError) as context:
            self.create()
        self.assertEqual("duplicate_work_item", context.exception.code)

    def test_record_filenames_are_unique_immutable_ids(self) -> None:
        item = self.create()
        first = run.append_record(self.state_root, item["id"], "message", "1", {"body": "a"})
        second = run.append_record(self.state_root, item["id"], "message", "1", {"body": "b"})
        self.assertNotEqual(first["id"], second["id"])
        self.assertRegex(first["id"], r"^R-\d{8}T\d{12}Z-[0-9a-f]{8}$")
        self.assertEqual(first["created_at"], run.record_timestamp_from_id(first["id"]))

    def test_record_collision_never_overwrites_existing_fact(self) -> None:
        item = self.create()
        work_root = self.state_root / "work-items" / item["id"]
        existing = run.load_records(work_root)[0]
        existing_path = work_root / "records" / f"{existing['id']}.json"
        before = existing_path.read_bytes()
        original_new_record_id = run.new_record_id
        run.new_record_id = lambda: existing["id"]
        try:
            with self.assertRaises(run.SkillError) as context:
                run.append_record(self.state_root, item["id"], "message", "1", {"body": "must not overwrite"})
            self.assertEqual("record_id_collision", context.exception.code)
        finally:
            run.new_record_id = original_new_record_id
        self.assertEqual(before, existing_path.read_bytes())

    def test_rebuild_projections_is_idempotent(self) -> None:
        item = self.create()
        work_root = self.state_root / "work-items" / item["id"]
        before = {path.relative_to(work_root): path.read_bytes() for path in work_root.rglob("*") if path.is_file()}
        project_board_before = (self.state_root / "BOARD.md").read_bytes()
        result = run.rebuild_projections(self.state_root)
        after = {path.relative_to(work_root): path.read_bytes() for path in work_root.rglob("*") if path.is_file()}
        self.assertEqual(1, result["rebuilt"])
        self.assertEqual(before, after)
        self.assertEqual(project_board_before, (self.state_root / "BOARD.md").read_bytes())

    def test_worker_cannot_update_another_workers_assignment(self) -> None:
        item = self.create()
        assignment = run.append_record(self.state_root, item["id"], "assignment", "1", {"assignee": "4", "role": "operator", "title": "执行操作"})
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "event", "3", {"kind": "done", "assignment": assignment["id"]})
        self.assertEqual("assignment_owner_only", context.exception.code)
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "event", "user", {"kind": "cancelled", "assignment": assignment["id"]})
        self.assertEqual("user_message_only", context.exception.code)
        cancelled = run.append_record(self.state_root, item["id"], "event", "1", {"kind": "cancelled", "assignment": assignment["id"]})
        self.assertEqual("cancelled", cancelled["kind"])

    def test_event_payload_requires_minimum_evidence_fields(self) -> None:
        item = self.create()
        assignment = run.append_record(self.state_root, item["id"], "assignment", "1", {"assignee": "3", "role": "builder", "title": "执行"})
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "event", "3", {"kind": "blocked", "assignment": assignment["id"]})
        self.assertEqual("invalid_blocker", context.exception.code)
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "event", "3", {"kind": "artifact_ready", "assignment": assignment["id"]})
        self.assertEqual("invalid_artifact", context.exception.code)

    def test_gate_waiver_requires_explicit_check(self) -> None:
        item = self.create()
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "event", "1", {"kind": "gate_waiver"})
        self.assertEqual("invalid_gate_waiver", context.exception.code)
        waiver = run.append_record(self.state_root, item["id"], "event", "1", {"kind": "gate_waiver", "check": "benchmark"})
        self.assertEqual("benchmark", waiver["check"])

    def test_gate_contract_is_validated(self) -> None:
        item = self.create()
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "gate", "2", {"verdict": "PASS", "checks": []})
        self.assertEqual("invalid_gate_profile", context.exception.code)
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "gate", "2", {"profile": "generic", "verdict": "MAYBE", "checks": []})
        self.assertEqual("invalid_gate_verdict", context.exception.code)
        with self.assertRaises(run.SkillError) as context:
            run.append_record(self.state_root, item["id"], "gate", "2", {"profile": "generic", "verdict": "PASS", "checks": [{}]})
        self.assertEqual("invalid_gate_checks", context.exception.code)

    def test_finished_worker_is_not_reported_as_active_agent(self) -> None:
        item = self.create()
        assignment = run.append_record(self.state_root, item["id"], "assignment", "1", {"assignee": "3", "role": "researcher", "title": "完成调查"})
        run.append_record(self.state_root, item["id"], "event", "3", {"kind": "done", "assignment": assignment["id"]})
        snapshot = run.build_project_snapshot(self.project)
        agent = next(agent for agent in snapshot["agents"] if agent["id"] == "3")
        self.assertEqual("IDLE", agent["status"])
        self.assertEqual([], agent["assignments"])

    def test_git_sync_does_not_stage_unrelated_tracked_decision_change(self) -> None:
        item = self.create()
        subprocess.run(["git", "-C", str(self.state_root), "config", "user.name", "Skill Test"], check=True)
        subprocess.run(["git", "-C", str(self.state_root), "config", "user.email", "skill-test@example.invalid"], check=True)
        first_paths = run.work_item_sync_paths(self.state_root, item["id"])
        first = run.sync_orchestration_git(self.project, first_paths, "chore(orchestration): initial")
        self.assertEqual("committed_not_pushed", first["status"])
        self.assertEqual("push_not_requested", first["reason"])
        (self.state_root / "DECISIONS.md").write_text("# unrelated local decision\n", encoding="utf-8")
        second, result = run.create_work_item(self.project, {"title": "第二任务", "goal": "验证同步边界"}, sync_git=True)
        second_paths = run.work_item_sync_paths(self.state_root, second["id"])
        self.assertNotIn("DECISIONS.md", second_paths)
        self.assertEqual("committed_not_pushed", result["status"])
        self.assertEqual("push_not_requested", result["reason"])
        staged = subprocess.run(["git", "-C", str(self.state_root), "diff", "--cached", "--name-only"], text=True, capture_output=True, check=True).stdout
        self.assertEqual("", staged)
        self.assertIn("DECISIONS.md", subprocess.run(["git", "-C", str(self.state_root), "status", "--porcelain"], text=True, capture_output=True, check=True).stdout)

    def test_concurrent_record_git_sync_keeps_commits_isolated(self) -> None:
        item = self.create()
        subprocess.run(["git", "-C", str(self.state_root), "config", "user.name", "Skill Test"], check=True)
        subprocess.run(["git", "-C", str(self.state_root), "config", "user.email", "skill-test@example.invalid"], check=True)
        initial = run.sync_orchestration_git(self.project, run.work_item_sync_paths(self.state_root, item["id"]), "chore(orchestration): initial")
        self.assertEqual("committed_not_pushed", initial["status"])
        self.assertEqual("push_not_requested", initial["reason"])

        barrier = threading.Barrier(2)
        results: list[tuple[str, dict[str, str]]] = []
        errors: list[BaseException] = []

        def write_and_sync(sender: str) -> None:
            try:
                barrier.wait()
                record, sync = run.append_record_and_sync(
                    self.project,
                    item["id"],
                    "message",
                    sender,
                    {"to": "1", "body": f"from-{sender}"},
                )
                results.append((record["id"], sync))
            except BaseException as exc:  # pragma: no cover - only used to surface thread failures
                errors.append(exc)

        threads = [threading.Thread(target=write_and_sync, args=(sender,)) for sender in ("3", "4")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertTrue(all(sync["status"] == "committed_not_pushed" and sync["reason"] == "push_not_requested" for _, sync in results))
        commits = subprocess.run(
            ["git", "-C", str(self.state_root), "log", "-2", "--name-only", "--pretty=format:COMMIT"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.split("COMMIT")[1:]
        changed_sets = [{line.strip() for line in block.splitlines() if line.strip()} for block in commits]
        expected = [{f"work-items/{item['id']}/records/{record_id}.json"} for record_id, _ in results]
        self.assertCountEqual(expected, changed_sets)

    def test_separate_processes_serialize_record_git_sync(self) -> None:
        item = self.create()
        subprocess.run(["git", "-C", str(self.state_root), "config", "user.name", "Skill Test"], check=True)
        subprocess.run(["git", "-C", str(self.state_root), "config", "user.email", "skill-test@example.invalid"], check=True)
        initial = run.sync_orchestration_git(self.project, run.work_item_sync_paths(self.state_root, item["id"]), "chore(orchestration): initial")
        self.assertEqual("committed_not_pushed", initial["status"])

        env = os.environ.copy()
        env[run.WORKSPACE_ENV] = str(self.workspace)
        processes = []
        for sender in ("3", "4", "5"):
            request = {
                "skill_action": "post_message",
                "projects": [{"id": "demo", "name": "Demo", "path": str(self.root), "profile": "software"}],
                "project_id": "demo",
                "work_item": item["id"],
                "from": sender,
                "payload": {"to": "1", "body": f"process-{sender}"},
                "sync_git": True,
            }
            process = subprocess.Popen(
                [os.environ.get("PYTHON", "python3"), str(MODULE_PATH)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            assert process.stdin is not None
            process.stdin.write(json.dumps(request))
            process.stdin.close()
            processes.append(process)

        outputs = []
        for process in processes:
            assert process.stdout is not None and process.stderr is not None
            stdout = process.stdout.read()
            stderr = process.stderr.read()
            process.stdout.close()
            process.stderr.close()
            returncode = process.wait(timeout=20)
            self.assertEqual(0, returncode, stderr)
            outputs.append(json.loads(stdout))

        record_ids = [output["data"]["id"] for output in outputs]
        self.assertTrue(all(output["git_sync"]["status"] == "committed_not_pushed" for output in outputs))
        self.assertFalse((self.state_root / ".git/index.lock").exists())
        commits = subprocess.run(
            ["git", "-C", str(self.state_root), "log", "-3", "--name-only", "--pretty=format:COMMIT"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.split("COMMIT")[1:]
        changed_sets = [{line.strip() for line in block.splitlines() if line.strip()} for block in commits]
        expected = [{f"work-items/{item['id']}/records/{record_id}.json"} for record_id in record_ids]
        self.assertCountEqual(expected, changed_sets)

    def test_concurrent_work_item_create_and_sync_is_atomic(self) -> None:
        run.initialize_orchestration_repo(self.project, mode="existing")
        subprocess.run(["git", "-C", str(self.state_root), "config", "user.name", "Skill Test"], check=True)
        subprocess.run(["git", "-C", str(self.state_root), "config", "user.email", "skill-test@example.invalid"], check=True)
        bootstrap = run.sync_orchestration_git(self.project, ["PROJECT.md", "BOARD.md", ".gitignore", "DECISIONS.md"], "chore(orchestration): bootstrap")
        self.assertEqual("committed_not_pushed", bootstrap["status"])

        barrier = threading.Barrier(2)
        results: list[tuple[dict[str, object], dict[str, str]]] = []
        errors: list[BaseException] = []

        def create_and_sync(title: str) -> None:
            try:
                barrier.wait()
                results.append(run.create_work_item(self.project, {"title": title, "goal": title}, sync_git=True))
            except BaseException as exc:  # pragma: no cover - only used to surface thread failures
                errors.append(exc)

        threads = [threading.Thread(target=create_and_sync, args=(title,)) for title in ("任务 A", "任务 B")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertTrue(all(sync["status"] == "committed_not_pushed" for _, sync in results))
        commits = subprocess.run(["git", "-C", str(self.state_root), "log", "-2", "--name-only", "--pretty=format:COMMIT"], text=True, capture_output=True, check=True).stdout.split("COMMIT")[1:]
        changed_sets = [{line.strip() for line in block.splitlines() if line.strip()} for block in commits]
        for item, _ in results:
            self.assertTrue(any(f"work-items/{item['id']}/WORK.md" in paths for paths in changed_sets))

    def test_orchestration_lock_serializes_independent_processes(self) -> None:
        run.initialize_orchestration_repo(self.project, mode="existing")
        marker = Path(self.tempdir.name) / "critical.log"

        processes = [multiprocessing.Process(target=lock_process_worker, args=(str(self.state_root), str(marker))) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=5)
        self.assertTrue(all(process.exitcode == 0 for process in processes))
        lines = marker.read_text(encoding="utf-8").splitlines()
        self.assertEqual(4, len(lines))
        self.assertTrue(lines[0].startswith("start-") and lines[1].startswith("end-"))
        self.assertTrue(lines[2].startswith("start-") and lines[3].startswith("end-"))

    def test_stale_lock_is_not_removed_while_owner_process_is_alive(self) -> None:
        run.initialize_orchestration_repo(self.project, mode="existing")
        lock_dir = self.state_root.parent / f".{self.state_root.name}.write-lock"
        lock_dir.mkdir()
        (lock_dir / "owner.json").write_text(json.dumps({"pid": os.getpid(), "created_at": 0}), encoding="utf-8")
        os.utime(lock_dir, (0, 0))
        original_timeout = run.LOCK_TIMEOUT_SECONDS
        run.LOCK_TIMEOUT_SECONDS = 0.05
        try:
            with self.assertRaises(run.SkillError) as context:
                with run.orchestration_write_lock(self.state_root):
                    pass
            self.assertEqual("orchestration_busy", context.exception.code)
            self.assertTrue(lock_dir.is_dir())
        finally:
            run.LOCK_TIMEOUT_SECONDS = original_timeout
            shutil.rmtree(lock_dir, ignore_errors=True)

    def test_git_sync_surfaces_orchestration_busy(self) -> None:
        item = self.create()
        lock_dir = self.state_root.parent / f".{self.state_root.name}.write-lock"
        lock_dir.mkdir()
        (lock_dir / "owner.json").write_text(json.dumps({"pid": os.getpid(), "created_at": 0}), encoding="utf-8")
        os.utime(lock_dir, None)
        original_timeout = run.LOCK_TIMEOUT_SECONDS
        run.LOCK_TIMEOUT_SECONDS = 0.05
        try:
            with self.assertRaises(run.SkillError) as context:
                run.sync_orchestration_git(self.project, run.work_item_sync_paths(self.state_root, item["id"]), "busy")
            self.assertEqual("orchestration_busy", context.exception.code)
        finally:
            run.LOCK_TIMEOUT_SECONDS = original_timeout
            shutil.rmtree(lock_dir, ignore_errors=True)

    def test_web_console_record_endpoint_is_disabled(self) -> None:
        server = run.ThreadingHTTPServer(("127.0.0.1", 0), run.ConsoleHandler)
        server.projects = self.projects
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"project_id": "demo", "work_item": "WI-0001", "type": "gate", "from": "2", "payload": {"verdict": "PASS", "checks": []}})
            connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
            connection.request("POST", "/api/records", body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(403, response.status)
            self.assertEqual("record_api_disabled", payload["error"]["code"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_historical_blocked_assignment_does_not_block_reassigned_seat(self) -> None:
        item = self.create()
        first = run.append_record(self.state_root, item["id"], "assignment", "1", {"assignee": "3", "role": "researcher", "title": "旧调查"})
        run.append_record(self.state_root, item["id"], "event", "3", {"kind": "blocked", "assignment": first["id"], "detail": "旧阻塞"})
        run.append_record(self.state_root, item["id"], "assignment", "1", {"assignee": "3", "role": "researcher", "title": "重新分派"})
        refreshed = run.read_work_item(self.state_root / "work-items" / item["id"])
        self.assertEqual("ACTIVE", refreshed["status"])
        self.assertEqual("READY", next(seat for seat in refreshed["seats"] if seat["id"] == "3")["status"])

    def test_artifact_event_does_not_replace_assignment_state_cursor(self) -> None:
        item = self.create()
        assignment = run.append_record(self.state_root, item["id"], "assignment", "1", {"assignee": "3", "role": "builder", "title": "构建"})
        started = run.append_record(self.state_root, item["id"], "event", "3", {"kind": "started", "assignment": assignment["id"]})
        run.append_record(self.state_root, item["id"], "event", "3", {"kind": "artifact_ready", "assignment": assignment["id"], "artifact": "result.md"})
        refreshed = run.read_work_item(self.state_root / "work-items" / item["id"])
        seat = next(seat for seat in refreshed["seats"] if seat["id"] == "3")
        self.assertEqual("WORKING", seat["status"])
        self.assertEqual(started["id"], seat["last_event"])

    @unittest.skipUnless(shutil.which("git"), "requires git")
    def test_git_sync_only_touches_orchestration_repo(self) -> None:
        subprocess.run(["git", "-C", str(self.root), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Skill Test"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "skill-test@example.invalid"], check=True)
        (self.root / "README.md").write_text("demo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "init"], check=True, capture_output=True)
        business_head = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()

        item = self.create()
        subprocess.run(["git", "-C", str(self.state_root), "config", "user.name", "Skill Test"], check=True)
        subprocess.run(["git", "-C", str(self.state_root), "config", "user.email", "skill-test@example.invalid"], check=True)
        result = run.sync_orchestration_git(self.project, ["PROJECT.md", "BOARD.md", f"work-items/{item['id']}", ".gitignore", "DECISIONS.md"], "chore(orchestration): test")

        self.assertEqual("committed_not_pushed", result["status"])
        self.assertEqual("push_not_requested", result["reason"])
        self.assertEqual(business_head, subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip())
        self.assertEqual("", subprocess.run(["git", "-C", str(self.root), "status", "--porcelain"], text=True, capture_output=True, check=True).stdout)

    def test_create_work_item_rejects_push_without_sync(self) -> None:
        with self.assertRaises(run.SkillError) as context:
            run.create_work_item(self.project, {"title": "非法 push", "goal": "不应创建"}, push_remote=True)
        self.assertEqual("push_requires_sync", context.exception.code)
        self.assertFalse(self.state_root.exists())

    def test_sync_git_defaults_to_local_commit_even_with_upstream(self) -> None:
        item = self.create()
        subprocess.run(["git", "-C", str(self.state_root), "config", "user.name", "Skill Test"], check=True)
        subprocess.run(["git", "-C", str(self.state_root), "config", "user.email", "skill-test@example.invalid"], check=True)
        remote = Path(self.tempdir.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.state_root), "remote", "add", "origin", str(remote)], check=True)
        first = run.sync_orchestration_git(self.project, run.work_item_sync_paths(self.state_root, item["id"]), "chore(orchestration): initial", push_remote=True)
        self.assertEqual("synced", first["status"])
        self.assertEqual("committed_and_pushed", first["reason"])

        record = run.append_record(self.state_root, item["id"], "message", "3", {"to": "1", "body": "local only"})
        result = run.sync_orchestration_git(self.project, run.record_sync_paths(item["id"], record), "chore(orchestration): local only")
        self.assertEqual("committed_not_pushed", result["status"])
        self.assertEqual("push_not_requested", result["reason"])
        local_head = subprocess.run(["git", "-C", str(self.state_root), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
        remote_head = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"], text=True, capture_output=True, check=True).stdout.strip()
        self.assertNotEqual(local_head, remote_head)

    def test_record_sync_refuses_to_write_fact_when_orchestration_git_is_missing(self) -> None:
        item = self.create()
        shutil.rmtree(self.state_root / ".git")
        before = len(run.load_records(self.state_root / "work-items" / item["id"]))
        with self.assertRaises(run.SkillError) as context:
            run.append_record_and_sync(self.project, item["id"], "message", "3", {"to": "1", "body": "must not be left unsynced"})
        self.assertEqual("orchestration_not_git_repository", context.exception.code)
        after = len(run.load_records(self.state_root / "work-items" / item["id"]))
        self.assertEqual(before, after)

    def test_record_round_trip_json_is_human_readable(self) -> None:
        item = self.create()
        record = run.append_record(self.state_root, item["id"], "message", "4", {"to": "board", "body": "中文消息"})
        path = self.state_root / "work-items" / item["id"] / "records" / f"{record['id']}.json"
        parsed = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("中文消息", parsed["body"])
        self.assertIn("\n  \"body\"", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
