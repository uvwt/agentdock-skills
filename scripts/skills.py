#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import stat
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = ROOT / "skills"
CATALOG_PATH = ROOT / "catalog.json"
REPOSITORY_URL = "https://github.com/uvwt/agentdock-skills"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
FORBIDDEN_DIRS = {"__pycache__", "node_modules", ".pytest_cache", ".mypy_cache"}
FORBIDDEN_FILES = {".DS_Store", ".env"}


@dataclass(frozen=True)
class Skill:
    name: str
    version: str
    description: str
    root: Path

    @property
    def release_tag(self) -> str:
        return f"{self.name}-v{self.version}"

    @property
    def archive_name(self) -> str:
        return f"{self.release_tag}.zip"


def parse_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md 必须以 YAML frontmatter 开始")

    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("SKILL.md frontmatter 未正确结束")
    if not text[end + 5 :].strip():
        raise ValueError("SKILL.md 正文不能为空")

    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


def discover_skills() -> list[Skill]:
    if not SKILLS_ROOT.is_dir():
        raise ValueError(f"Skill 目录不存在: {SKILLS_ROOT}")

    skills: list[Skill] = []
    for root in sorted(path for path in SKILLS_ROOT.iterdir() if path.is_dir()):
        document = root / "SKILL.md"
        if not document.is_file():
            raise ValueError(f"{root.name}: 缺少 SKILL.md")

        fields = parse_frontmatter(document)
        name = fields.get("name", "")
        version = fields.get("version", "")
        description = fields.get("description", "")
        if name != root.name:
            raise ValueError(f"{root.name}: frontmatter name 必须与目录名一致，实际为 {name!r}")
        if not SEMVER_PATTERN.fullmatch(version):
            raise ValueError(f"{root.name}: version 不是合法语义化版本: {version!r}")
        if not description:
            raise ValueError(f"{root.name}: description 不能为空")
        skills.append(Skill(name=name, version=version, description=description, root=root))

    if not skills:
        raise ValueError("仓库中没有 Skill")
    return skills


def validate_repository() -> list[Skill]:
    errors: list[str] = []

    for path in SKILLS_ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if path.is_symlink():
            errors.append(f"{relative}: 不允许符号链接")
        if any(part in FORBIDDEN_DIRS for part in relative.parts):
            errors.append(f"{relative}: 不允许缓存或依赖目录")
        if path.name in FORBIDDEN_FILES or path.suffix == ".pyc":
            errors.append(f"{relative}: 不允许运行时或私密文件")

    try:
        skills = discover_skills()
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(str(exc))
        skills = []

    names = [skill.name for skill in skills]
    if len(names) != len(set(names)):
        errors.append("Skill 名称不能重复")

    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
    return skills


def package_bytes(skill: Skill) -> bytes:
    output = io.BytesIO()
    files = sorted(path for path in skill.root.rglob("*") if path.is_file() or path.is_symlink())
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            if path.is_symlink():
                raise ValueError(f"不允许打包符号链接: {path}")
            relative = path.relative_to(skill.root).as_posix()
            mode = stat.S_IMODE(path.stat().st_mode)
            info = zipfile.ZipInfo(relative, FIXED_ZIP_TIME)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output.getvalue()


def build_catalog(skills: list[Skill]) -> dict[str, object]:
    entries: list[dict[str, str]] = []
    for skill in skills:
        digest = hashlib.sha256(package_bytes(skill)).hexdigest()
        entries.append(
            {
                "name": skill.name,
                "version": skill.version,
                "description": skill.description,
                "path": f"skills/{skill.name}",
                "source_url": f"{REPOSITORY_URL}/tree/main/skills/{skill.name}",
                "release_tag": skill.release_tag,
                "download_url": f"{REPOSITORY_URL}/releases/download/{skill.release_tag}/{skill.archive_name}",
                "digest": f"sha256:{digest}",
            }
        )
    return {"schema_version": 1, "repository": REPOSITORY_URL, "skills": entries}


def render_catalog(skills: list[Skill]) -> str:
    return json.dumps(build_catalog(skills), ensure_ascii=False, indent=2) + "\n"


def write_or_check_catalog(skills: list[Skill], check: bool) -> None:
    expected = render_catalog(skills)
    if check:
        actual = CATALOG_PATH.read_text(encoding="utf-8") if CATALOG_PATH.exists() else ""
        if actual != expected:
            print("catalog.json 不是当前 Skill 源码的最新结果，请运行 scripts/skills.py catalog", file=sys.stderr)
            raise SystemExit(1)
        print(f"catalog ok: {len(skills)} skills")
        return

    CATALOG_PATH.write_text(expected, encoding="utf-8")
    print(f"catalog written: {CATALOG_PATH} ({len(skills)} skills)")


def select_skill(skills: list[Skill], name: str | None, tag: str | None) -> Skill:
    for skill in skills:
        if name and skill.name == name:
            return skill
        if tag and skill.release_tag == tag:
            return skill
    selector = name or tag or ""
    raise ValueError(f"找不到匹配的 Skill: {selector}")


def package_skill(skills: list[Skill], name: str | None, tag: str | None, output_dir: Path) -> None:
    skill = select_skill(skills, name, tag)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / skill.archive_name
    archive_data = package_bytes(skill)
    archive_path.write_bytes(archive_data)
    digest = hashlib.sha256(archive_data).hexdigest()
    checksum_path = output_dir / f"{skill.archive_name}.sha256"
    checksum_path.write_text(f"{digest}  {skill.archive_name}\n", encoding="utf-8")
    print(json.dumps({"skill": skill.name, "version": skill.version, "archive": str(archive_path), "digest": f"sha256:{digest}"}, ensure_ascii=False))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and package AgentDock Skills.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("validate", help="校验所有 Skill")

    catalog = subcommands.add_parser("catalog", help="生成或检查 catalog.json")
    catalog.add_argument("--check", action="store_true")

    package = subcommands.add_parser("package", help="打包单个 Skill")
    selector = package.add_mutually_exclusive_group(required=True)
    selector.add_argument("--skill")
    selector.add_argument("--tag")
    package.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    skills = validate_repository()
    if args.command == "validate":
        print(f"validated: {len(skills)} skills")
    elif args.command == "catalog":
        write_or_check_catalog(skills, args.check)
    elif args.command == "package":
        package_skill(skills, args.skill, args.tag, args.output_dir)


if __name__ == "__main__":
    main()
