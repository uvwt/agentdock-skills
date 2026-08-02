# AgentDock Skills

AgentDock 官方与社区 Skill 源码仓库。

AgentDock 主仓库只保留必须随运行时安装和升级的三个核心 Skill：`skill-authoring`、`skill-installation`、`skill-vetter-runtime`。其余独立集成都在本仓库维护、测试和发布，避免普通 Skill 与 AgentDock 核心版本强耦合。

本仓库保留了原 `agentdock/skill-sources` 的 Git 历史，迁移后的 `git log --follow` 和 `git blame` 仍可追踪各 Skill 的演进记录。

## 仓库结构

```text
skills/<skill-name>/       Skill 源码
scripts/skills.py          校验、目录生成和确定性打包
catalog.json               机器可读的 Skill 目录与制品摘要
.github/workflows/ci.yml   全量校验与测试
.github/workflows/release.yml  按 Skill 标签发布
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

## 打包与安装

生成单个确定性 ZIP 和 SHA-256 文件：

```bash
python3 scripts/skills.py package --skill trilium --output-dir dist
```

然后通过 AgentDock 的 `skill_package install` 能力安装生成的 ZIP。安装前仍应执行安全审查和包校验，不要把真实 Token、Cookie、`.env`、缓存或运行数据提交到仓库。

## 发布约定

每个 Skill 独立版本和发布。标签格式为：

```text
<skill-name>-v<version>
```

例如：

```text
trilium-v1.2.0
```

标签必须与对应 `SKILL.md` 的 `name` 和 `version` 完全一致。推送标签后，Release Workflow 会生成：

```text
trilium-v1.2.0.zip
trilium-v1.2.0.zip.sha256
```

`catalog.json` 中的下载地址和摘要由同一套确定性打包逻辑生成。

## 贡献边界

普通业务集成、个人效率工具和社区 Skill 放在本仓库。只有 AgentDock 安装、自举、安全审查本身不可缺少，并且必须与 AgentDock 运行时同步发布的 Skill，才进入 AgentDock 主仓库的 `core-skills/`。

提交前必须：

1. 递增发生行为变化的 Skill 版本。
2. 运行仓库校验、目录检查和相关测试。
3. 确认包内没有秘密、私有路径、符号链接和运行产物。
4. 保持同名同版本内容不可变。

## License

[MIT](LICENSE)
