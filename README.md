# AgentDock Skills

这里收集了可以交给 AI 使用的 AgentDock Skills。你可以把 Skill 理解成一份“能力说明 + 必要脚本/资料”：它告诉 AI 在特定任务里应该怎么做、用什么工具、遵守什么边界。

如果你只是想**找一个 Skill 并让 AI 帮你添加**，不需要先学 Git、ZIP、digest 或发布流程。

## 最简单的用法

1. 在下面的 [Skill 列表](#skill-列表) 里找到你需要的 Skill。
2. 打开它的 `SKILL.md`。
3. 复制浏览器里的 GitHub 链接。
4. 把链接发给支持 AgentDock Skill 的 AI，让它检查并添加这个 Skill。

例如你想添加 `trilium`：

<https://github.com/uvwt/agentdock-skills/blob/main/skills/trilium/SKILL.md>

可以直接对 AI 说：

> 帮我添加这个 Skill：https://github.com/uvwt/agentdock-skills/blob/main/skills/trilium/SKILL.md

如果希望 AI 在安装前先说明风险，可以说：

> 先检查这个 Skill 的来源、需要的权限和完整目录内容；确认安全后帮我安装并验证：https://github.com/uvwt/agentdock-skills/blob/main/skills/trilium/SKILL.md

`SKILL.md` 链接是给 AI 的**定位入口**。一个 Skill 可能还包含 `scripts/`、`references/`、`tests/` 等文件，所以 AI 应获取并检查完整 Skill 目录，而不是只把单个 Markdown 文件当成完整安装包。普通用户不要通过 GitHub Release 或 ZIP 安装 Skill。

> [!IMPORTANT]
> Skill 可能调用本机命令、第三方服务或账号数据。安装陌生 Skill 时，建议让 AI 先检查来源、完整目录、所需权限和敏感配置；涉及账号、Token、Cookie、文件修改或外部写操作时尤其如此。

## Skill 列表

### AI、开发与自动化

| Skill | 能做什么 |
| --- | --- |
| [`code-debrief`](skills/code-debrief/SKILL.md) | 从真实代码讲解 AI / vibecoding 产出的核心实现、调用链和设计思路 |
| [`find-skills-skill`](skills/find-skills-skill/SKILL.md) | 搜索和发现可用的 OpenClaw Skills |
| [`grill-me`](skills/grill-me/SKILL.md) | 通过连续追问把计划或设计中的关键决策澄清完整 |
| [`multi-agent-orchestration`](skills/multi-agent-orchestration/SKILL.md) | 编排多个 Agent 长期协作、分工、独立门禁和可恢复状态 |
| [`personal-dev-guard`](skills/personal-dev-guard/SKILL.md) | 在开发和 Review 时约束代码可读性、可维护性与补丁味 |
| [`desktop`](skills/desktop/SKILL.md) | 在 macOS 上执行桌面自动化工作流 |

### 内容、阅读与媒体

| Skill | 能做什么 |
| --- | --- |
| [`douban-marks`](skills/douban-marks/SKILL.md) | 只读查询豆瓣电影、图书、音乐标记和电影联想 |
| [`douyin-hot`](skills/douyin-hot/SKILL.md) | 获取抖音实时热榜、热度和跳转链接 |
| [`rsshub`](skills/rsshub/SKILL.md) | 使用本地 RSSHub 构造路由、读取和解析 Feed |
| [`spotify-web-api`](skills/spotify-web-api/SKILL.md) | 通过 Spotify 官方 API 授权、搜索、读取播放状态和添加歌曲 |
| [`weread-skills`](skills/weread-skills/SKILL.md) | 搜索微信读书书籍并查看书架、笔记、书评、统计与推荐 |

### 文件、知识与自托管服务

| Skill | 能做什么 |
| --- | --- |
| [`baidu-netdisk`](skills/baidu-netdisk/SKILL.md) | 通过 bdpan CLI 管理百度网盘文件 |
| [`cloudsaver`](skills/cloudsaver/SKILL.md) | 调用本地 CloudSaver 搜索资源、解析分享和执行转存 |
| [`linkwarden`](skills/linkwarden/SKILL.md) | 通过官方 API 管理 Linkwarden 书签、集合、标签和高亮 |
| [`openlist`](skills/openlist/SKILL.md) | 通过 OpenList v4 API 浏览、搜索和管理文件与存储 |
| [`trilium`](skills/trilium/SKILL.md) | 通过 ETAPI 搜索、读取、创建和整理 Trilium Notes |
| [`wallos`](skills/wallos/SKILL.md) | 通过官方 API 查询和管理 Wallos 订阅 |

### 通知与消息

| Skill | 能做什么 |
| --- | --- |
| [`bark`](skills/bark/SKILL.md) | 发送 Bark 兼容通知 |
| [`ntfy`](skills/ntfy/SKILL.md) | 通过 ntfy.sh 或自托管 ntfy 发送通知 |
| [`poke-api`](skills/poke-api/SKILL.md) | 通过 V2 inbound API 向 Poke 发送带上下文的指令 |
| [`telegram-official`](skills/telegram-official/SKILL.md) | 通过 Telegram 官方 Bot API 发送通知 |

### 账号、配额与运维

| Skill | 能做什么 |
| --- | --- |
| [`codex-usage`](skills/codex-usage/SKILL.md) | 查询本机 Codex CLI 账号状态和使用配额 |
| [`grok-quota`](skills/grok-quota/SKILL.md) | 查询 Grok 周配额、计划、积分和使用明细 |
| [`qinglong`](skills/qinglong/SKILL.md) | 通过官方 API 管理青龙面板状态、环境变量、定时任务和日志 |
| [`vaultwarden-cli`](skills/vaultwarden-cli/SKILL.md) | 通过 Bitwarden 官方 CLI 安全访问自托管 Vaultwarden |
| [`vitapulse`](skills/vitapulse/SKILL.md) | 读取 VitaPulse HealthKit API 网关中的健康数据与趋势 |
| [`volcengine-ark-quota`](skills/volcengine-ark-quota/SKILL.md) | 查询火山引擎 Ark Coding Plan 配额使用情况 |

## 我需要自己下载安装包吗？

通常不需要。

对于普通用户，更推荐把目标 `SKILL.md` 的 GitHub 链接交给 AI，让 AI 负责：

1. 确认来源和 Skill 身份；
2. 获取并审查完整 Skill 目录；
3. 检查权限、依赖和敏感配置；
4. 调用当前环境提供的 Skill 安装能力；
5. 安装后验证是否可用。

如果当前 AI 或 Agent 环境不支持安装 Skill，它应该明确告诉你缺少什么能力，而不是假装已经安装成功。

## 给开发者和维护者

AgentDock 主仓库只保留必须随运行时安装和升级的核心 Skill。普通业务集成、个人效率工具和社区 Skill 放在本仓库独立维护，避免与 AgentDock 核心版本强耦合。

每个 Skill 位于：

```text
skills/<skill-name>/
└── SKILL.md
```

根据需要还可以包含 `scripts/`、`references/`、`tests/` 等内容。`SKILL.md` frontmatter 中的 `name` 应与目录名一致，并声明非空 `description`。本仓库不为 Skill 建立独立版本身份；内容演进由 Git 历史追踪。

### 本地校验

校验所有 Skill：

```bash
python3 scripts/skills.py validate
```

检查机器可读目录：

```bash
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

### 可选本地打包

维护者如需本地验证 ZIP 输入，仍可以生成确定性 ZIP 和 SHA-256：

```bash
python3 scripts/skills.py package --skill trilium --output-dir dist
```

这只是本地维护工具，不是用户安装路径。不要把生成的 ZIP 提交进仓库，也不要把它作为 GitHub Release 制品发布。仓库的机器可读 `catalog.json` 只描述源码目录，不依赖 Release 下载地址、标签或制品 digest。

### 提交前

- 行为发生变化时直接更新当前 Skill 内容，并让 Git 历史记录演进；
- 运行仓库校验、目录检查和相关测试；
- 不提交真实 Token、Cookie、`.env`、缓存或运行数据；
- 检查包内没有秘密、私有路径、符号链接和意外产物；
- 不在仓库工具或 Skill frontmatter 中恢复独立 version、activate 或 rollback 生命周期。

本仓库保留了原 `agentdock/skill-sources` 的 Git 历史，迁移后的 `git log --follow` 和 `git blame` 仍可追踪各 Skill 的演进记录。

## License

[MIT](LICENSE)
