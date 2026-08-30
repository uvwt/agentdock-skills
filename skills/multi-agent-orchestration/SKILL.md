---
name: multi-agent-orchestration
description: 当用户要让多个 Agent 长期协作推进软件开发、研究、运维、内容、数据分析或受限工作区内的自由任务，并需要 1 号总管、2 号独立门禁、3/4/5 动态执行槽、可恢复状态和 Git 持久化时使用。
version: 5.1.0
---

# Multi-Agent Orchestration

把可中断的多个 Agent 组织成一条可恢复、可审计、可换模型继续执行的协作链。

核心不是让 Agent 永久在线，而是让任务事实独立于聊天窗口存在：

```text
用户目标
→ 1 号总管拆分、任职、分派
→ 3/4/5 按需执行并写回事实
→ 1 号整合 Worker evidence，形成唯一最终候选
→ 2 号只对最终候选做唯一正式 Gate
→ 1 号根据 Gate 收口、返工或 DONE
```

聊天、定时任务和临时 Agent 都只是可替换的“身体”。**Record 是协作事实，STATE / BOARD / RELEASE 是由 Record 重放生成的投影；Git 负责持久化与同步，不等于通信协议本身。**

## 适用场景

使用本 Skill 处理：

- 多个 Agent 需要跨会话、跨唤醒长期推进同一目标；
- 需要一个总管统一拆分和收口，同时保留独立门禁；
- 任务可能属于开发、研究、运维、内容、数据分析或自由任务；
- 希望 3/4/5 的职责按任务动态任职，而不是固定 Backend / Frontend / QA；
- 希望即使 Agent 被替换，也能从持久化状态恢复；
- 希望多个 Agent 共享观察信息和产物，但避免互相随意改分工。

不适合：

- 几分钟即可由单 Agent 完成的一次性任务；
- 强依赖秒级实时群聊或低延迟消息队列的任务；
- 用户明确要求完全无持久化痕迹的临时协作。

## 一、最小必要约束

只保留真正需要的硬边界：

1. **1 号是唯一总管。** 用户给目标与边界；正式 Assignment、改分工、暂停、恢复、取消和最终收口统一由 1 号写入控制面 Record。
2. **2 号是独立 Gatekeeper。** 2 号可以读代码、跑测试、分析日志、写验证脚本和提出建议，但只提交 Gate 结论，不直接替 Worker 改正式 Assignment。
3. **3/4/5 是动态 Worker 槽。** 最多启用 3 个，不要求开满；Role 只是本次 Assignment 的职责重点，不是 capability / tool 权限矩阵。
4. **Worker 可以互相读取信息和产物，但不能互相正式派工。** 需要改分工时向 1 号提出 Message，由 1 号决定是否发新 Assignment。
5. **不越用户明确的任务、目录、设备、生产环境和安全边界。** 不覆盖来源不明的修改，不伪造验证结果。

除这些必要边界外，Agent 应保留足够自主性。比如被任职为“后端调查”时，如果证据指向前端、Docker 或反代，可以继续合理调查，不因 Role 标签停止。

## 二、角色模型

```text
1：Orchestrator / 总管                 常驻
2：Independent Gatekeeper / 独立门禁  常驻
3：Worker A                           动态
4：Worker B                           动态
5：Worker C                           动态
```

### 1 号：Orchestrator

负责：

- 理解目标和验收条件；
- 拆 Work Item 与 Assignment；
- 决定是否启用 3/4/5，以及每个槽本轮 Role；
- 管依赖、优先级、冲突和范围；
- 汇总 Event / Artifact / Message；
- 把已接受的 Worker 产物集成成唯一最终候选；
- 向 2 号发起针对该最终候选的门禁请求；
- 根据 Gate 结果决定放行、返工、换槽、暂停或取消；
- 最终收口和交付。

1 号可以自己完成无冲突的 merge / cherry-pick / rebase 等机械集成，但不应因为“自己能做”就失去编排职责。跨 Work Item 出现语义、内容或实现冲突时，1 号负责决定顺序、范围和最终意图，再把需要实际改内容的冲突解决工作派给 Worker；不要让 1 号悄悄变成所有领域的实现者。

### 2 号：Independent Gatekeeper

负责独立验证。检查项随 profile 变化，不再写死为软件专用四门禁。

2 号可以：

- 阅读所有相关 Record、代码、文档、日志和 Artifact；
- 运行与风险匹配的真实验证；
- 写验证脚本、复现步骤和证据；
- 产生 `gate` Record。

2 号不直接修改 Worker 的正式任务。`FAIL` 后由 1 号决定返工 Assignment。

### 3/4/5：Dynamic Worker Slots

由 1 号按当前任务动态任职，例如：

```text
开发：backend developer / frontend developer / integration investigator
研究：evidence researcher / counterexample analyst / synthesizer
运维：network investigator / service operator / rollback verifier
内容：researcher / writer / editor
```

Role 是工作重点，不是权限表。1 号给 3/4/5 发正式 Assignment 时必须写一个简短动态 Role，让 Worker 知道本轮优先负责什么；1 号自身的协议 Role 固定为 `orchestrator`，2 号固定为 `independent-gatekeeper`，不能通过 Assignment 改任；它不限制 Worker 使用当前宿主允许的其他必要 Skill / Tool，也不禁止跨领域调查。

## 三、统一 Record 协议

所有协作事实使用一套不可变 Record：

```text
assignment
message
event
gate
```

它们是 **Record Type**，不是四套传输系统。

公共字段：

```json
{
  "id": "R-20260830T010203Z-a1b2c3d4",
  "type": "event",
  "work_item": "WI-0042",
  "from": "3",
  "to": "board",
  "created_at": "2026-08-29T17:02:03Z"
}
```

Record 文件一旦创建就不修改；实现必须使用独占创建，ID 碰撞时失败而不是覆盖历史；修正通过后续 Record 表达。文件名使用 UTC 微秒时间 + 随机短 ID，`created_at` 从同一 Record ID 时间解析，保证文件排序与事实时间一致；避免多个 Agent 追加同一个文件产生 Git 冲突。

### Assignment

只有 1 号可以写正式 Assignment；用户目标由 1 号接管后再形成 Assignment。

关键字段：

```text
assignee: 1 | 2 | 3 | 4 | 5
role: 本轮职责重点
标题 / goal / acceptance / depends_on
```

正式 Assignment 统一由 1 号发出；用户目标先进入 1 号，3/4/5 也不允许给其他 Worker 发 Assignment。

### Message

用于问题、建议、协调请求和观察信息。任意 Agent 都可以写；`to` 只能是 `user`、`board` 或 1/2/3/4/5，且正文不能为空。

Message 不改变正式任务归属；如果需要改分工，最终由 1 号发新的 Assignment。

### Event

用于 Agent 报告已经发生的事实；协议中 `user` 只作为 Message 来源，不直接写 Assignment / Event / Gate，用户意图先进入 1 号再转成控制面事实。例如：

```text
started
blocked
done
cancelled
artifact_ready
wi_paused
wi_resumed
wi_cancelled
wi_completed
gate_waiver
wi_status（显式生命周期校正）
```

`blocked` 必须带 `detail/body`，`artifact_ready` 必须带 `artifact`。`wi_paused / wi_resumed / wi_cancelled / wi_completed / gate_waiver / wi_status` 只能由 1 号总管发起；Assignment 的 `cancelled` 由该 Assignment owner 或 1 号发起。用户意图先由 1 号记录为正式控制面 Event；`gate_waiver` 必须明确指出被豁免的 `check`，`wi_status` 只用于显式生命周期校正。

### Gate

只能由 2 号写入。

统一结构：

```json
{
  "type": "gate",
  "from": "2",
  "profile": "software",
  "verdict": "PASS",
  "business_head_sha": "0123456789abcdef0123456789abcdef01234567",
  "checks": [
    {"id": "qa", "result": "PASS", "evidence": "..."},
    {"id": "security", "result": "PASS", "evidence": "..."}
  ],
  "evidence": "最终门禁证据"
}
```

Gate 的核心是非空 `profile`、`checks[]` 和 `verdict`。不同领域使用不同 profile，但不改变 Record 格式。

当 Project 声明 `needs_business_git=true` 时，`business_head_sha` 必填，必须指向**业务目标 worktree 当前 clean HEAD**，不能填 Worker 隔离 worktree 的 HEAD。调用方可以输入唯一可解析的短 SHA，但 Record 落盘前必须正规化为 40 位 lowercase full SHA。HEAD 或最终候选一旦变化，旧 Gate 立即失效，必须重新 Gate；观察投影会把原始 PASS Gate 显示为 `STALE`，同时保留 `record_verdict=PASS` 作为历史事实。仅追加同一候选的 `artifact_ready` 证据不会让 Gate 自动失效。

正式 Gate 只有这一处。Worker 自测、互审、验证脚本和中间检查都是 evidence，不再冒充第二套正式 Gate。

## 四、控制面与观察面

不要把“不能互相派工”误解成“Agent 之间不能看信息”。

### 控制面

```text
正式派工
改分工
Work Item 生命周期
最终放行/返工
```

由 1 号收口。

### 观察面

```text
Event
Message
Artifact
证据
日志摘要
```

相关 Agent 可以直接读取和引用，不需要让 1 号充当消息转发机器人。

## 五、Project → Work Item → Assignment

核心层级：

```text
Project
└── Work Item
    ├── Records
    └── Artifacts
```

Assignment 是 Work Item 内的正式 Record，不再把每个角色目录当任务事实源。

每个 Project 对应独立 orchestration Git：

```text
$MULTI_AGENT_WORKSPACE_ROOT/orchestrations/<project-id>/
├── .git/
├── PROJECT.md
├── BOARD.md                 # 投影
├── DECISIONS.md
└── work-items/
    └── WI-0001/
        ├── WORK.md
        ├── records/         # 唯一协作事实
        │   └── R-*.json
        ├── artifacts/
        ├── BOARD.md         # Record 重放投影
        ├── RELEASE.md       # Gate 投影
        └── slots/
            └── <1-5>/
                ├── TASK.md  # 投影
                └── STATE.md # 投影
```

不要手工修改投影来“通知”其他 Agent。要改变事实就只追加 Record。控制台直接从 Record 重放当前状态；需要持久化人类可读投影时，由 1 号或维护动作显式执行 `rebuild_projections`，避免 Worker 同时改共享 BOARD / STATE / RELEASE。

## 六、Git 的职责

Git 继续保留，但职责分开。

### Orchestration Git

适用于所有任务类型，保存：

- Project / Work Item；
- 不可变 Record；
- DECISIONS；
- 投影；
- 轻量 Artifact 索引。

它承担可恢复、同步、审计和历史版本，不承担实时消息总线职责。默认只在本机 orchestration 仓库提交；只有 Agent/CLI 调用在当前任务已获得用户明确远端同步授权后传入 `push_remote=true` 时才推送。Web 控制台永不接受远端推送授权，不能把“有 upstream”视为自动推送授权。

### Business Git / worktree

只在任务本身需要版本控制时使用，典型是软件开发。

开发任务：

```text
1 分派
→ 3/4/5 各自独立 branch / worktree
→ Event 引用 commit / Artifact
→ 1 检查跨 Work Item 冲突并形成业务目标 worktree 的唯一最终候选
→ 2 对该候选的 exact business_head_sha 做独立 Gate
→ Gate PASS 后 1 使用同一 business_head_sha 收口 DONE
```

业务目标 worktree 在正式 Gate 和 DONE 时都必须 clean。这样 Gate 绑定的是可复现 commit，而不是“HEAD + 未提交文件”的临时状态。是否 push 远端仍由用户授权决定，push 不是 DONE 的必要条件。

研究、运维、内容等任务不应为了符合 Skill 形式强行创建业务 Git / worktree。

## 七、Profile

Profile 只描述常用门禁检查包和领域提示，不是权限系统。

推荐：

```text
generic
software
research
operations
content
custom
```

`software` 可以继续使用原来的检查语义：

```text
qa
confirmatory
security
benchmark
```

但它们进入统一 `checks[]`，不再成为 RELEASE.md 固定字段。

其他 profile 可以按实际任务使用更合适的检查，例如研究的来源质量、证据充分性、反证和结论一致性；运维的真实环境验证、服务健康、回滚和副作用。

测试失败不能直接忽略，也不能为了“变绿”默认修改产品去迎合可疑断言。2 号应把失败归类为 `product_failure`、`test_failure`、`environment_failure` 或经明确批准的 `approved_skip`；无法完成分类时 Gate 不得 PASS。若确认是坏测试/脆弱测试，1 号派 Worker 修测试与 fixture，并保留最小复现和证据。

## 八、入口模式与工作根目录

环境变量：

| 变量 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `MULTI_AGENT_WORKSPACE_ROOT` | config | 是 | Multi-Agent 专用根目录；`orchestrations/` 保存编排状态，`workspaces/` 只在需要独立业务工作区时使用。 |

项目可声明：

```text
needs_business_git: true | false
```

- `true`：需要真实业务路径，控制台展示业务 Git；
- `false`：只建立 orchestration Project，不强迫创建业务仓库。

`true` 还意味着正式 Gate 与 DONE 都必须绑定 `business_head_sha`。`wi_completed` 与 `wi_status=DONE` 使用完全相同的收口校验，不能用生命周期校正绕过最终 Gate；完成 Event 会把同一 40 位 `business_head_sha` 写回 Record。

`greenfield_init` 在 `profile=software` 时默认创建业务 Git；其他 profile 默认可以只创建 orchestration Project，也可显式要求业务 Git。初始化动作本身不接受 `sync_git` / `push_remote`，避免把“创建本地项目”隐式升级成远端副作用；需要同步时从后续 `add_work_item` / Record 动作开始。

## 九、每次唤醒的恢复协议

每个 Agent 开始前：

1. 确认当前 Project / Work Item / 座位编号；
2. 读取 `PROJECT.md`、当前 `WORK.md` 和 Record；
3. 根据 Record 重放确认自己的最新 Assignment、依赖和阻塞；
4. 读取相关 Agent 已公开的 Event / Message / Artifact；
5. 只围绕当前目标执行一个可验证增量；
6. 结束前追加 Event / Message / Gate，并保留真实验证证据。

不要依赖上一次聊天记忆作为长期真相。

## 十、辅助动作

`run.py` 支持：

```text
status
snapshot
greenfield_init
add_work_item
append_record
assign
post_message
report_event
submit_gate
rebuild_projections
serve
```

示例：

```bash
printf '%s' '{"skill_action":"status"}' | python3 run.py
```

创建 Work Item 默认只向 1 号发首条 Assignment，不预创建 3/4/5 空 Worker，也不预造 Gate PASS/PENDING 事实。

`append_record` 是统一底层写口；`assign / post_message / report_event / submit_gate` 只是更易读的动作名，最终都写相同 Record 协议。Record 写入属于 Agent 执行通道，不开放给本地 Web 控制台；控制台只负责观察和创建 Work Item，避免浏览器请求伪装成 1/2/3/4/5。需要 `sync_git` 时，`run.py` 内部使用 `append_record_and_sync` 把 Record 写入和该 Record 的 Git 同步串行化，并在写 Record 前确认 orchestration Git 仍有效，避免“事实已写但同步根本不可能”的半完成状态；底层 orchestration 写入使用按项目目录的跨进程锁，Git 的 add / commit / push 也在同一锁域内串行；超时锁只有在持有进程已不存在时才清理。Record 本身仍是独立不可变文件，避免并发 Agent 交叉污染共享 Git index。

## 十一、本地 HTML 控制台

`serve` 只监听 loopback，展示：

- Project / Work Item；
- 1/2 常驻座位和 3/4/5 动态槽；
- 当前 Assignment / Role / blocker；
- 通用 Gate profile、verdict 和 checks；
- 最近不可变 Record 时间线；
- 需要业务 Git 时才展示业务仓库状态；

控制台直接从持久化 Record 重放当前状态；BOARD / STATE / RELEASE 只是可再生成的人类可读投影，不冒充 Agent 在线心跳。

## 十二、第一版明确不做

不要为了“架构完整”提前加入：

- 实时消息总线、WebSocket 群聊；
- capability / tool 权限矩阵；
- CRDT、分布式锁、复杂 lease / ack 协议；
- 自动把 Gate FAIL 变成返工任务；
- Agent 在线心跳；
- Profile 市场或动态插件体系；
- 自动为所有任务创建 business Git/worktree；
- 让 3/4/5 互相正式派工。

只有当 Git 轮询延迟、跨机器 ACK、频繁并发冲突、多编排实例或多租户成为真实瓶颈时，再替换 transport；Record、角色边界和门禁协议不需要重做。

## 十三、完成标准

一个 Work Item 真正完成至少要求：

- 1 号已收口目标、分工与最终状态；
- 需要的 3/4/5 Assignment 已有真实 Event / Artifact 证据；
- 1 号已经把接受的产物整合成唯一最终候选；
- 2 号针对该最终候选完成与风险匹配的**唯一正式 Gate**，且最新正式 Gate 的当前投影仍有效为 `PASS`（不是历史 `record_verdict=PASS` 但已 `STALE`）；`gate_waiver` 只能解释具体 check 的例外，不能替代整个正式 Gate；
- `needs_business_git=true` 时，Gate、DONE Event 与业务目标 worktree 当前 clean HEAD 的 40 位 `business_head_sha` 三者完全一致；HEAD 改变后旧 Gate 不可用于 DONE；
- 业务产物按任务类型真实验证；
- Record 已持久化，投影可由 Record 重建；
- 下一次唤醒不依赖旧聊天窗口也能继续。

一句话：**Worker 交 evidence，1 号形成最终候选，2 号只 Gate 最终候选，1 号只在同一候选仍成立时 DONE；Record 保存事实，Git 保存历史，投影负责展示。**
