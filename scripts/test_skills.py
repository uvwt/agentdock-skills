from __future__ import annotations

import hashlib
import io
import unittest
import zipfile

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

    def test_catalog_digest_matches_package(self) -> None:
        catalog = skills.build_catalog(self.all_skills)
        entry = next(item for item in catalog["skills"] if item["name"] == "desktop")
        digest = hashlib.sha256(skills.package_bytes(self.desktop)).hexdigest()

        self.assertEqual(entry["digest"], f"sha256:{digest}")
        self.assertEqual(entry["release_tag"], f"desktop-v{self.desktop.version}")

    def test_release_tag_must_match_skill_version_exactly(self) -> None:
        selected = skills.select_skill(self.all_skills, None, self.desktop.release_tag)
        self.assertEqual(selected, self.desktop)

        with self.assertRaisesRegex(ValueError, "找不到匹配的 Skill"):
            skills.select_skill(self.all_skills, None, "desktop-v0.0.0")


if __name__ == "__main__":
    unittest.main()
