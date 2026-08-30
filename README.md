# AgentDock Skills

AgentDock 官方与社区 Skill 源码仓库。

AgentDock 主仓库只保留必须随运行时安装和升级的三个核心 Skill：`skill-authoring`、`skill-installation`、`skill-vetter-runtime`。其余独立集成都在本仓库维护和测试，避免普通 Skill 与 AgentDock 核心版本强耦合。

本仓库保留了原 `agentdock/skill-sources` 的 Git 历史，迁移后的 `git log --follow` 和 `git blame` 仍可追踪各 Skill 的演进记录。

## 安装入口

把目标 Skill 的 `SKILL.md` GitHub 链接交给支持 AgentDock Skill 的 AI。`SKILL.md` 只是定位入口：AI 应获取完整 Skill 目录，执行来源与权限审查，再 validate、install、activate。不要把单个 Markdown 文件当成完整安装包，也不要通过 GitHub Release 或 ZIP 安装。

示例：

<https://github.com/uvwt/agentdock-skills/blob/main/skills/trilium/SKILL.md>

可以直接对 AI 说：

> 帮我添加这个 Skill：https://github.com/uvwt/agentdock-skills/blob/main/skills/trilium/SKILL.md

安装前建议要求 AI 先检查来源、完整目录、所需权限和敏感配置。如果当前环境不支持安装 Skill，它应该明确说明，而不是假装已经安装成功。

## 仓库结构

```text
skills/<skill-name>/       Skill 源码
scripts/skills.py          校验、目录生成和可选本地打包
catalog.json               机器可读的 Skill 源码目录
.github/workflows/ci.yml   全量校验与测试
```

每个 Skill 至少包含一份 `SKILL.md`，并在 frontmatter 中声明与目录名一致的 `name`、语义化 `version` 和非空 `description`。脚本、引用和测试只在确有需要时加入。

## 本地开发

校验所有 Skill：

```bash
python3 scripts/skills.py validate
```

修改 Skill 版本或内容后重新生成目录：

```bash
python3 scripts/skills.py catalog
python3 scripts/skills.py catalog --check
```

运行仓库内 Python 测试：

```bash
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/agentdock-skills-pycache"
find skills -name 'test*.py' -type f -print0 | while IFS= read -r -d '' test_file; do
  (cd "$(dirname "$test_file")" && python3 "$(basename "$test_file")")
done
```

## 可选本地打包

维护者如需生成确定性 ZIP（例如本地调试 `skill_package`），可以：

```bash
python3 scripts/skills.py package --skill trilium --output-dir dist
```

这不是安装路径。不要把生成的 ZIP 提交进仓库，也不要把它当作 GitHub Release 制品。

## 贡献边界

普通业务集成、个人效率工具和社区 Skill 放在本仓库。只有 AgentDock 安装、自举、安全审查本身不可缺少，并且必须与 AgentDock 运行时同步发布的 Skill，才进入 AgentDock 主仓库的 `core-skills/`。

提交前必须：

1. 递增发生行为变化的 Skill 版本。
2. 运行仓库校验、目录检查和相关测试。
3. 确认没有秘密、私有路径、符号链接和运行产物。
4. 保持同名同版本源码内容不可变。

## License

[MIT](LICENSE)
