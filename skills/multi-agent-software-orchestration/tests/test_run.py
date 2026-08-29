from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "run.py"
SPEC = importlib.util.spec_from_file_location("multi_agent_orchestration_run", MODULE_PATH)
assert SPEC and SPEC.loader
run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run)


class ConsoleStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "demo"
        self.root.mkdir()
        self.projects = run.normalize_projects([{"id": "demo", "name": "Demo", "path": str(self.root)}])
        self.project = self.projects[0]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_create_work_item_initializes_project_and_owner_zero(self) -> None:
        item = run.create_work_item(self.project, {"title": "文件同步", "goal": "完成局域网文件同步"})

        self.assertEqual("WI-0001", item["id"])
        self.assertEqual("ACTIVE", item["status"])
        self.assertEqual("0", item["owner"])
        self.assertTrue((self.root / ".multi-agent/PROJECT.md").is_file())
        self.assertTrue((self.root / ".multi-agent/work-items/WI-0001/roles/0/TASK.md").is_file())
        self.assertIn("WI-0001", (self.root / ".multi-agent/BOARD.md").read_text(encoding="utf-8"))

    def test_ids_are_monotonic_and_snapshot_maps_agents(self) -> None:
        first = run.create_work_item(self.project, {"title": "后端 API", "goal": "实现 API", "owner": "2", "priority": "HIGH"})
        second = run.create_work_item(self.project, {"title": "前端控制台", "goal": "实现 UI", "owner": "3"})
        snapshot = run.build_snapshot(self.projects)

        self.assertEqual("WI-0001", first["id"])
        self.assertEqual("WI-0002", second["id"])
        project = snapshot["projects"][0]
        self.assertEqual(2, project["counts"]["active"])
        agent_two = next(agent for agent in project["agents"] if agent["id"] == "2")
        self.assertEqual("READY", agent_two["status"])
        self.assertEqual("WI-0001", agent_two["assignments"][0]["work_item"])

    def test_duplicate_open_title_is_rejected(self) -> None:
        run.create_work_item(self.project, {"title": "重复任务", "goal": "first"})
        with self.assertRaises(run.SkillError) as context:
            run.create_work_item(self.project, {"title": "重复任务", "goal": "second"})
        self.assertEqual("duplicate_work_item", context.exception.code)

    def test_frontmatter_handles_special_title_and_multiline_goal(self) -> None:
        item = run.create_work_item(self.project, {"title": "任务: A", "goal": "第一行\n第二行"})
        fields, body = run.parse_frontmatter(self.root / f".multi-agent/work-items/{item['id']}/WORK.md")

        self.assertEqual("任务: A", fields["title"])
        self.assertIn("第一行\n第二行", body)

    @unittest.skipUnless(shutil.which("git"), "requires git")
    def test_git_sync_commits_and_pushes_only_orchestration_state(self) -> None:
        remote = Path(self.tempdir.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Skill Test"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "skill-test@example.invalid"], check=True)
        (self.root / "README.md").write_text("demo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "remote", "add", "origin", str(remote)], check=True)
        subprocess.run(["git", "-C", str(self.root), "push", "-u", "origin", "main"], check=True, capture_output=True)

        item = run.create_work_item(self.project, {"title": "共享任务", "goal": "验证 Git 同步"})
        result = run.sync_orchestration_git(self.project, item)

        self.assertEqual("synced", result["status"])
        remote_board = subprocess.run(
            ["git", "--git-dir", str(remote), "show", "main:.multi-agent/BOARD.md"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        self.assertIn("WI-0001", remote_board)

    def test_unknown_owner_is_rejected(self) -> None:
        with self.assertRaises(run.SkillError) as context:
            run.create_work_item(self.project, {"title": "非法角色", "goal": "test", "owner": "9"})
        self.assertEqual("invalid_owner", context.exception.code)


if __name__ == "__main__":
    unittest.main()
