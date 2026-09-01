---
name: multi-agent-orchestration
description: 当 1~5 号 Agent 通过定时任务长期协作，并由 AgentDock Orchestrator 统一分配 Assignment、记录 Execution、独立 Gate 和成果状态时使用。每次唤醒只需声明 slot，Skill 负责领取服务端正式任务并回报执行事实。
version: 6.0.0
---

# Multi-Agent Orchestration

本 Skill 是 **AgentDock Orchestrator 的轻量客户端**。它不再在本地维护第二套 Git/Record/HTML 控制面；Project、Work Item、Assignment、Execution、Gate、Record 与 Outcome 的唯一事实源都在 Orchestrator 服务端。

适合的运行方式是：**1~5 号各自有固定定时任务 → 定时唤醒 AgentDock → 调用本 Skill → 按 slot 原子领取一个服务端 Assignment → 执行 → 回报。**

## 1. 固定 Slot

| Slot | 协议身份 | 主要职责 |
|---|---|---|
| 1 | Orchestrator | plan / rework / integrate / close |
| 2 | Independent Gatekeeper | 对最终候选做独立 Gate |
| 3 | Worker | 动态角色，由 Assignment `focus_role` 决定 |
| 4 | Worker | 动态角色，由 Assignment `focus_role` 决定 |
| 5 | Worker | 动态角色，由 Assignment `focus_role` 决定 |

**Slot 只是定时任务的固定入口，不是权限声明。** 真正的协议角色、任务归属、Work Item 状态和可执行动作都由服务端当前 Assignment/Execution 决定。不要在 prompt、payload 或 Record 中伪造 `from` / `role`。

## 2. 每次定时唤醒

定时任务只需要告诉模型自己的编号，例如：

```text
使用 multi-agent-orchestration Skill。你是 3 号，领取并推进当前正式任务；如果 idle 或 busy 就结束本轮。
```

模型调用：

```bash
printf '%s' '{"skill_action":"claim","slot":3}' | python3 run.py
```

脚本从 Skill 根目录以相对路径执行；URL、Token、节点标识等环境由运行宿主注入，不读取 AgentDock 私有目录或环境文件。

`claim` 是推荐入口，它已经把“选下一项 + 创建 Execution”做成服务端原子操作，不需要先 `next` 再 `claim`。

返回状态：

- `claimed`：本轮拿到一个新的正式 Assignment，继续执行。
- `idle`：当前没有属于该 Slot 的待办，正常结束。
- `busy`：该 Slot 已有 queued/running Execution，通常说明另一个定时触发仍在工作；不要重复执行，正常结束。

同一 Slot 一次只运行一个 Execution。服务端会按 FIFO 从所有活动 Project/Work Item 中选择当前有效的 READY Assignment。

## 3. claimed 后的统一流程

收到 `claimed` 后先阅读返回的：

- `project`
- `work_item`
- `assignment`
- `execution`
- `recent_records`

只执行 `assignment` 明确要求的工作，不自行改变正式任务归属。

开始实际工作前：

```json
{"skill_action":"start","execution_id":"exe_..."}
```

有真实成果时及时上报 Artifact：

```json
{
  "skill_action": "artifact",
  "execution_id": "exe_...",
  "artifact": {
    "title": "实现结果",
    "type": "text",
    "value": "真实路径、URL、commit、报告或可验证结果",
    "description": "这份成果如何核验"
  }
}
```

普通 Worker 完成后：

```json
{"skill_action":"finish","execution_id":"exe_...","status":"succeeded","summary":"完成了什么，以及关键证据"}
```

如果本轮明确失败，应使用 `failed` 或 `interrupted`，让 Assignment 回到 READY 供后续定时任务重试；不要把失败包装成成功。

## 4. 1 号的 Assignment kind

1 号必须先看 `assignment.kind`，不同 kind 的后续动作不同。

### `plan`

1. 理解 Work Item 目标、验收条件和已有 Record。
2. 拆成 1~3 个真正需要的 Worker Assignment；不用为了凑满 3/4/5 全开。
3. 先把当前 plan Execution `finish: succeeded`。
4. 再调用 `dispatch` 形成正式 Worker Assignment。

```json
{
  "skill_action": "dispatch",
  "execution_id": "exe_...",
  "assignments": [
    {
      "seat": 3,
      "role": "backend",
      "title": "实现后端",
      "goal": "完成可验证后端增量",
      "acceptance": ["测试通过"],
      "required": true
    }
  ]
}
```

至少一个 Worker 必须 `required=true`。

### `rework`

读取最新 Gate/Record 中的失败或 STALE 证据，重新形成必要 Worker Assignment。顺序同 plan：先成功结束 rework Execution，再 `dispatch`。

### `integrate`

1. 汇总 Worker Artifact 和真实业务结果。
2. 对代码交付项目，确保服务端配置的 Business Git 工作区已经形成 clean 最终候选。
3. 先 `finish: succeeded`。
4. 再 `request_gate`：

```json
{"skill_action":"request_gate","execution_id":"exe_..."}
```

服务端会锁定最终候选，并创建 2 号正式 Gate Assignment。

### `close`

只有正式 Gate PASS 后才会出现。先 `finish: succeeded`，再：

```json
{"skill_action":"complete","execution_id":"exe_..."}
```

如果历史 PASS 已因 Business Git HEAD/clean 状态变化而 STALE，服务端会拒绝完成并自动进入 rework；不要绕过。

## 5. 2 号 Gate

2 号只处理服务端给它的 `kind=gate` Assignment。

流程：

1. `claim slot=2`。
2. `start`。
3. 独立检查最终候选，不接受 1/3/4/5 号“代验收”。
4. 先 `finish: succeeded`，表示 Gate 执行过程本身完成。
5. 再 `submit_gate` 写正式结论。

```json
{
  "skill_action": "submit_gate",
  "execution_id": "exe_...",
  "gate": {
    "profile": "software",
    "verdict": "PASS",
    "business_head_sha": "服务端要求时填写当前候选 commit",
    "checks": [
      {"id": "tests", "result": "PASS", "evidence": "真实测试证据"}
    ],
    "evidence": "独立 Gate 总结"
  }
}
```

Gate `verdict` 与 check 只能基于真实证据。Business Git 项目由服务端校验候选 SHA、clean 状态和 STALE 语义。

## 6. 可用 action

| action | 用途 |
|---|---|
| `status` | 检查 Skill 配置与 Orchestrator `/healthz` |
| `next` | 只读查看某 Slot 下一状态，不认领 |
| `claim` | 推荐的定时任务入口；原子领取下一 Assignment |
| `start` | Execution → running |
| `finish` | Execution → succeeded / failed / interrupted |
| `artifact` | 上报真实 Artifact |
| `dispatch` | 1 号 plan/rework 后生成 3/4/5 Assignment |
| `request_gate` | 1 号 integrate 后请求 2 号 Gate |
| `submit_gate` | 2 号提交正式 Gate |
| `complete` | 1 号 close 后完成 Work Item |
| `work_item` | 只读获取 Work Item 当前快照 |
| `outcome` | 只读获取成果视图数据 |
| `records` | 只读获取最近 Record |

脚本从 stdin 接收单个 JSON 对象，并只输出结构化 JSON。失败返回：

```json
{"ok":false,"error":{"code":"...","message":"..."}}
```

## 7. 环境配置

| 环境变量 | 类型 | 说明 |
|---|---|---|
| `MULTI_AGENT_ORCHESTRATOR_URL` | 配置 | Orchestrator origin，例如 `https://example.com`；不要包含 path 或凭据 |
| `MULTI_AGENT_ORCHESTRATOR_TOKEN` | **secret** | Agent API Bearer Token；不要写进 SKILL.md、代码、Git 或定时任务 prompt |
| `MULTI_AGENT_SLOT` | 配置，可选 | 专用安装实例可固定 1~5；同一 Skill 跑多个定时任务时建议在每次调用里传 `slot` |
| `MULTI_AGENT_NODE_ID` | 配置，可选 | 运行节点标识；默认本机 hostname |
| `MULTI_AGENT_AGENT` | 配置，可选 | Execution 里的 Agent 标识；默认 `scheduled-agent` |
| `MULTI_AGENT_SESSION_ID` | 配置，可选 | 运行时会话标识 |

`MULTI_AGENT_ORCHESTRATOR_TOKEN` 应通过 AgentDock Skill 隔离环境配置。脚本不会输出 Token。

## 8. 失败与恢复

- `idle`：不是错误，结束本轮。
- `busy`：不是错误；不要抢占或重复执行。
- HTTP 401：Token 未配置或不匹配。
- HTTP 409：服务端状态已经变化，重新 `next/claim`，不要根据旧上下文强推。
- `finish: failed/interrupted`：服务端会把当前 Assignment 重新变为 READY，后续定时任务可重试。
- 控制面重启时会把遗留 queued/running Execution 标记 interrupted 并释放 Assignment。
- 当前 V1.1 不做时间型 lease 自动抢占：如果 Agent 进程被硬杀而 Orchestrator 本身未重启，Slot 会保持 `busy`，避免误判另一个仍在工作的 Agent。出现持续 busy 时应先确认原执行是否真的死亡，再处理恢复，不要让定时任务自动并发接管。

## 9. 核心约束

1. Orchestrator 数据库与 Record 是唯一事实源；不要再创建本地编排 Git/Markdown 状态仓库。
2. 角色与权限来自服务端 Assignment/Execution；Skill 不提供 `from` / `role` 冒充入口。
3. 2 号 Gate 独立于 1/3/4/5 的执行结果声明。
4. Worker 只做当前 Assignment；不自己派工、不自己改变 Work Item 生命周期。
5. Artifact 必须是真实可访问、可核验的成果，不制造占位卡片。
6. 对代码交付，业务 Git 的 clean HEAD / Gate / STALE 约束由服务端统一执行，不在 Skill 复制第二套状态机。
