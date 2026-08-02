---
name: trilium
description: Search, read, create, update and organize notes in a Trilium Notes instance through the official ETAPI, including revisions, branches, attributes, attachments, calendar notes and explicitly confirmed destructive operations.
version: 1.1.1
---

# Trilium Skill

通过 Trilium 官方 ETAPI 查询和管理个人知识库。Skill 只依赖 Python 标准库，不包含 Trilium 服务端，也不调用内部 `/api/*` 或任意自定义路径。

## 适用场景

使用本 Skill 处理：

- 检查 Trilium 实例版本、连通性和 ETAPI 鉴权；
- 使用 Trilium 搜索语法查询笔记；
- 读取、创建、修改和删除笔记；
- 读取、替换、追加或精确局部编辑笔记正文；
- 查询最近变更和笔记版本，创建版本或恢复已删除笔记；
- 查询子节点和子树，创建、克隆、移动和调整笔记树 Branch；
- 查询、创建和修改 Label / Relation 属性；
- 查询附件元数据并读取文本型附件内容；
- 获取 Inbox、日记、周记、月记和年记；
- 在明确确认后触发 Trilium 数据库备份。

不要使用本 Skill 处理用户密码登录、ETAPI Token 创建或吊销、内部 API、任意路径透传、批量删除、二进制上传、ZIP 导入导出或直接修改 Trilium 数据库。

## 环境变量

| 变量 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `TRILIUM_URL` | config | 是 | Trilium 实例根地址，例如 `https://notes.example.com`；也接受以 `/etapi` 结尾的地址 |
| `TRILIUM_ETAPI_TOKEN` | secret | 是 | 在 Trilium `Options → ETAPI` 中创建的 Token |
| `TRILIUM_INSECURE_TLS` | config | 否 | 仅自签名证书调试时设为 `1`；会跳过 TLS 证书校验 |

Token 只从当前进程环境读取，不支持通过输入 JSON 传递。缺少环境变量时只返回变量名，不读取浏览器 Cookie、配置文件或宿主私有目录。

## 重要内容格式

ETAPI 返回和接收的是 Trilium 实际存储格式：

- `text` 富文本笔记正文是 HTML，不是 Markdown；
- `code`、`search`、`mermaid` 等文本型笔记通常是原始文本；
- 图片、PDF 等二进制正文不会写入 JSON 输出，只返回 `binary`、`content_type` 和 `size_bytes`；
- 不做 Markdown 与 HTML 转换，避免无损性和格式语义不明确；
- `append-note-content` 对富文本笔记要求输入 HTML 片段，对其他文本笔记自动补一个换行；
- `edit-note-content` 只支持非富文本笔记，每个 `old_text` 必须在当前正文中恰好出现一次。

替换正文前先读取笔记类型和当前正文。只修改标题时使用 `update-note`，不要重写正文。

## 执行方式

在 Skill 包根目录运行：

```bash
printf '%s' '{"skill_action":"status"}' | python3 run.py
```

输入必须是 JSON 对象，动作字段为 `skill_action`。输出始终为 JSON。除 `status` 可报告未配置状态外，其余动作均要求 URL 和 Token。

## 动作

### 笔记与正文

| 动作 | 类型 | 主要输入 | 说明 |
|---|---|---|---|
| `status` | 只读 | 无 | 检查配置并调用 `/etapi/app-info` |
| `search-notes` | 只读 | `query`，以及可选搜索参数 | 使用 Trilium 搜索语法查询笔记 |
| `get-note` | 只读 | `note_id` | 获取笔记元数据、Branch ID、属性等 |
| `get-note-content` | 只读 | `note_id` | 获取笔记实际存储正文 |
| `create-note` | 写入 | `parent_note_id`, `title`, `type`, `content?` | 在指定父笔记下创建笔记 |
| `update-note` | 写入 | `note_id` 及需修改的字段 | 修改标题、类型、MIME 或创建时间 |
| `set-note-content` | 写入 | `note_id`, `content` | 替换完整正文；允许空字符串 |
| `append-note-content` | 写入 | `note_id`, `content` | 读取并重新校验版本后追加正文 |
| `edit-note-content` | 写入 | `note_id`, `edits` | 对非富文本正文执行唯一片段替换 |
| `delete-note` | 删除 | `note_id`, `confirm: true`, `confirm_title` | 读取当前标题并精确匹配后软删除 |

`edit-note-content` 的 `edits` 是 1 至 50 个对象，按顺序执行：

```json
{
  "old_text": "必须唯一出现的旧片段",
  "new_text": "替换后的片段"
}
```

`search-notes` 可选参数：

```text
fast_search
include_archived_notes
ancestor_note_id
ancestor_depth
order_by
order_direction
limit
debug
```

创建笔记的可选字段：

```text
mime
note_position
prefix
is_expanded
note_id
date_created
utc_date_created
```

### 版本与恢复

| 动作 | 类型 | 主要输入 | 说明 |
|---|---|---|---|
| `note-history` | 只读 | `ancestor_note_id?` | 查询最近变更，可限定子树 |
| `list-note-revisions` | 只读 | `note_id` | 查询一个笔记的版本列表 |
| `get-revision` | 只读 | `revision_id` | 获取版本元数据 |
| `get-revision-content` | 只读 | `revision_id` | 获取版本正文 |
| `create-revision` | 写入 | `note_id`, `description?` | 手动保存当前版本 |
| `undelete-note` | 恢复写入 | `note_id`, `confirm: true`, `confirm_note_id` | ID 精确匹配后恢复软删除笔记 |

### 树结构与属性

| 动作 | 类型 | 主要输入 | 说明 |
|---|---|---|---|
| `get-child-notes` | 只读 | `note_id`, `limit?` | 返回直接子笔记、类型和各自子节点数量 |
| `get-subtree` | 只读 | `note_id`, `depth?`, `node_limit?` | 递归读取子树，深度最多 5，每层最多 10 个节点 |
| `get-branch` | 只读 | `branch_id` | 获取 Branch |
| `create-branch` | 写入 | `note_id`, `parent_note_id` | 把笔记放入另一个父节点；已有 Branch 时更新它 |
| `clone-note` | 写入 | `note_id`, `parent_note_id`, `prefix?` | 为同一笔记新增一个父节点，内容保持同步 |
| `move-note` | 高影响写入 | `note_id`, `new_parent_note_id`, `confirm: true`, `confirm_title`, `branch_id?` | 新建目标 Branch 后删除原 Branch并验证结果 |
| `update-branch` | 写入 | `branch_id` 及修改字段 | 修改位置、前缀或展开状态 |
| `delete-branch` | 删除 | `branch_id`, `confirm: true`, `confirm_branch_id` | 删除 Branch；若是最后一个 Branch，还需 `confirm_delete_note: true` 和 `confirm_title` |
| `refresh-note-ordering` | 写入 | `parent_note_id` | 通知 Trilium 刷新该父节点排序 |
| `get-attributes` | 只读 | `note_id` | 返回当前笔记自己拥有的非自动链接属性 |
| `get-attribute` | 只读 | `attribute_id` | 获取 Label 或 Relation |
| `create-attribute` | 写入 | `note_id`, `type`, `name`, `value?` | 创建属性；未指定 `attribute_id` 时生成 12 位 ID |
| `set-attribute` | 写入 | `note_id`, `type`, `name`, `value?` | 同名属性存在时更新，否则创建；Relation 改目标时替换属性实体 |
| `update-attribute` | 写入 | `attribute_id`, `value?`, `position?` | 修改属性值或位置；Relation 改目标时替换属性实体 |
| `delete-attribute` | 删除 | `attribute_id`, `confirm: true`, `confirm_attribute_id` | 精确匹配 ID 后删除属性 |

`create-branch` 和 `update-branch` 的可选字段是 `note_position`、`prefix`、`is_expanded`。`type` 只接受 `label` 或 `relation`；Relation 的 `value` 必须是非空且真实存在的目标笔记 ID。

`get-child-notes` 默认最多返回 100 个直接子节点，可将 `limit` 调整到 500。`get-subtree` 默认深度为 2、总节点上限为 100，总节点上限最多设为 500，避免远程 ETAPI 产生失控请求量。

公开 ETAPI 的 Branch 创建接口不会调用 Trilium 内部的树环路校验。因此 `create-branch`、`clone-note` 和 `move-note` 会在写入前遍历源笔记子树，拒绝把笔记放到自身或后代下面；安全校验最多读取 5000 个节点，超限时拒绝写入。

`move-note` 在笔记只有一个父 Branch 时自动选择它；若笔记已经被克隆到多个父节点，必须显式提供 `branch_id`。ETAPI 没有原子移动端点，因此 Skill 始终先创建新 Branch，再删除旧 Branch；若删除结果不确定，会返回 `move_incomplete` 并保留新位置，不执行可能导致孤儿笔记的自动回滚。

ETAPI 不允许直接修改 Relation 的目标值。`set-attribute` 和 `update-attribute` 会先创建新 Relation，再删除旧 Relation并重新读取验证；若删除结果不确定，会返回 `attribute_replace_incomplete` 并保留两个实体，避免误删。

删除 Branch 可能改变笔记在树中的可见位置。若目标是笔记最后一个 Branch，Trilium 会连同笔记及其子树一起软删除，因此必须额外提供 `confirm_delete_note: true` 和当前 `confirm_title`。

### 附件、日历与备份

| 动作 | 类型 | 主要输入 | 说明 |
|---|---|---|---|
| `list-note-attachments` | 只读 | `note_id` | 列出笔记附件 |
| `get-attachment` | 只读 | `attachment_id` | 获取附件元数据 |
| `get-attachment-content` | 只读 | `attachment_id` | 只直接返回文本内容；二进制仅返回元数据 |
| `get-inbox-note` | 只读/按需创建 | `date` | 获取指定 `YYYY-MM-DD` 的 Inbox 笔记 |
| `get-day-note` | 只读/按需创建 | `date` | 获取日记 |
| `get-week-note` | 只读 | `week` | 获取 `YYYY-Www` 周记 |
| `get-month-note` | 只读/按需创建 | `month` | 获取 `YYYY-MM` 月记 |
| `get-year-note` | 只读/按需创建 | `year` | 获取 `YYYY` 年记 |
| `create-backup` | 高影响写入 | `backup_name`, `confirm: true`, `confirm_backup_name` | 触发数据库备份 |

Trilium 的特殊笔记接口可能在笔记不存在时自动创建对应节点，因此日历类动作虽按查询使用，也可能造成服务端数据变化。

## 安全约束

- 服务地址只允许 `http` 或 `https`，禁止 URL 内嵌凭据、查询参数和片段。
- 所有请求路径均由固定动作生成，不开放任意 endpoint。
- Token 不通过输入参数、命令行或输出传递；服务端错误中出现 Token 时会被替换为 `<redacted>`。
- 单次响应限制为 8 MiB，避免把大型附件直接装入模型上下文。
- 追加和局部编辑在写回前重新读取 `blobId` 与 `utcDateModified`，检测到并发修改时拒绝覆盖。
- 创建子笔记、创建或修改 Branch、属性写入和删除操作都会先读取目标，拒绝 protected 笔记；系统笔记不能移动、克隆或删除。
- `delete-note` 必须先读取目标并要求当前标题精确匹配。
- `move-note` 必须确认当前标题；多父节点笔记必须明确指定要移动的 Branch，并在写入前检查树环路。
- `delete-branch`、`delete-attribute` 和 `undelete-note` 必须额外匹配目标 ID；删除最后一个 Branch 还需确认会删除整棵子树。
- `create-attribute`、`set-attribute` 和 `update-attribute` 拒绝 Trilium 官方标记为危险的执行、Widget、共享脚本等属性。
- `create-backup` 必须匹配备份名，备份名仅允许字母、数字、点、下划线和连字符。
- 写操作后根据返回值或重新读取目标验证最终状态；失败时保留 HTTP 状态、固定 endpoint 和脱敏响应。
- Protected 内容仍以 Trilium ETAPI 为最终权限边界；Skill 对高影响写入额外提前拒绝，不尝试会话解锁或其他绕过。

## 推荐操作流程

1. 首次使用先执行 `status`，确认实例、Token 和版本。
2. 搜索后使用 `get-note` 确认真实 `note_id`、标题、类型和父子关系。
3. 改正文前先执行 `get-note-content`；富文本必须按 HTML 处理，小改动优先使用 `append-note-content` 或 `edit-note-content`。
4. 移动被克隆的笔记前先读取 `parentBranchIds`，明确要移动的是哪个树位置。
5. 删除、移动、恢复和备份前向用户复述目标，并只在确认信息完全匹配时执行。
6. 写入完成后读取目标或相关父节点验证，不用成功提示代替真实状态。

## 与官方内置 MCP 的关系

本 Skill 已覆盖官方 MCP 中可通过公开 ETAPI 稳定复刻的核心模型操作，包括正文追加与局部编辑、属性 upsert、子节点、子树、移动和克隆。仍不实现 `search_icons` 和 Trilium 内部 `load_skill`：这两项没有公开 ETAPI，不能通过内部 API 或硬编码数据伪造。

接口依据和字段映射见 `references/etapi.md`。

## AgentDock 适配

在 AgentDock 中，把 URL 和 Token 配置到该 Skill 的独立环境，再通过绑定当前激活 Skill 的命令执行能力运行 `python3 run.py`。不要手工拼接已安装版本目录，也不要把真实 Token 写入 Skill 包、源码仓库或 Recall。
