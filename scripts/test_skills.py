from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import skills


class SkillsRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.all_skills = skills.validate_repository()
        cls.desktop = next(skill for skill in cls.all_skills if skill.name == "desktop")

    def test_package_is_deterministic_and_rooted_at_skill_contents(self) -> None:
        first = skills.package_bytes(self.desktop)
        second = skills.package_bytes(self.desktop)

        self.assertEqual(first, second)
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            names = sorted(archive.namelist())
        self.assertIn("SKILL.md", names)
        self.assertIn("run.py", names)
        self.assertFalse(any(name.startswith("desktop/") for name in names))

    def test_catalog_is_source_index_without_release_artifacts(self) -> None:
        catalog = skills.build_catalog(self.all_skills)
        forbidden = {"release_tag", "download_url", "digest"}

        self.assertEqual(catalog["schema_version"], skills.CATALOG_SCHEMA_VERSION)
        self.assertEqual(catalog["repository"], skills.REPOSITORY_URL)
        self.assertEqual(len(catalog["skills"]), len(self.all_skills))
        for skill, entry in zip(self.all_skills, catalog["skills"]):
            self.assertEqual(entry["name"], skill.name)
            self.assertEqual(entry["version"], skill.version)
            self.assertEqual(entry["description"], skill.description)
            self.assertEqual(entry["path"], f"skills/{skill.name}")
            self.assertEqual(entry["source_url"], f"{skills.REPOSITORY_URL}/tree/main/skills/{skill.name}")
            self.assertTrue(forbidden.isdisjoint(entry))

    def test_select_skill_matches_by_name_only(self) -> None:
        selected = skills.select_skill(self.all_skills, self.desktop.name)
        self.assertEqual(selected, self.desktop)

        with self.assertRaisesRegex(ValueError, "找不到匹配的 Skill"):
            skills.select_skill(self.all_skills, "desktop-v0.0.0")

    def test_package_skill_writes_local_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            skills.package_skill(self.all_skills, self.desktop.name, output)
            archive = output / self.desktop.archive_name
            checksum = output / f"{self.desktop.archive_name}.sha256"
            self.assertTrue(archive.is_file())
            self.assertTrue(checksum.is_file())
            self.assertEqual(archive.read_bytes(), skills.package_bytes(self.desktop))
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(checksum.read_text(encoding="utf-8"), f"{digest}  {self.desktop.archive_name}\n")


if __name__ == "__main__":
    unittest.main()
