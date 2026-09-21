---
name: agentdock-cleanup
description: 在用户请求检查 AgentDock 运行产物占用、识别过期 Playwright 临时文件或制定受限清理计划时使用；不用于通用系统清理、共享缓存清空或用户项目删除。
version: 0.1.0
---

# AgentDock Cleanup

只管理由 AgentDock 或其受管生产者明确产生的运行产物。Python 3.10+，仅使用标准库；Windows 提供进程检查与单文件句柄删除，其他系统仅提供保守报告。

## 必须遵守的流程

默认只读。即使用户说“清理”，也必须执行：

`scan → classify → plan → confirm → revalidate → clean → verify`

1. 先读 [清理策略](references/cleanup-policy.md)，检查宿主提供的扫描范围与归属证据。
2. 调用 `scan`，解释分类、占用、时间、大小和证据缺口。未知文件、用户项目和任务输出默认保留。
3. 需要清理时调用 `plan`。展示具体路径、预计释放量、排除项及 15 分钟有效期。扫描和计划均不写状态、不删除。
4. 等用户在看到该计划后明确确认。不能把最初的“清理”请求当作对尚未展示的计划的确认。
5. 将完整计划、顶层 `plan_id` 和确认对象交给 `clean`。仅有 `confirmed=true` 无效。不得修改计划、补签或替用户扩大选择。
6. 逐项汇报重新验证、跳过原因及删除后验证结果；失败项保留。要重试，先重新扫描和展示新计划。

首次本机验证仅执行一次只读 `scan`；未经进一步明确确认，不得调用真实 `clean` 或启用真实删除集成测试。

## 权限边界

- 删除资格必须同时满足：有限文件名/位置 allowlist、受信任生产者签名回执、已关闭、至少七天未修改、进程状态完整且未占用、普通单链接文件、计划与当前内容一致。
- `.playwright-mcp` 的标准命名 snapshot、console log、测试 screenshot，以及明确登记的 runtime tmp 文件是候选；目录名本身不是归属证明。
- profiles（包括 `playwright_chromiumdev_profile-*`、`ms-playwright-mcp`）、npm `_cacache`/`_npx`、AgentDock 日志在本版都只有统计/报告权限。日志建议由生产者实现 retention/rotation。
- 配置、MCP 注册、OAuth、secret、DPAPI、Skill store、task 状态、Core binary/runtime 配置和源码均为 `PROTECTED`。
- 普通浏览器数据、Downloads、Documents、用户项目及未知工作目录文件不进入删除计划。整个 TEMP 和整个工作目录不能作为删除对象。
- 无法读取进程、遇到链接/junction、状态改变、平台不支持或归属缺失时拒绝删除。禁止通过另一个脚本或工具绕过拒绝。
- 不创建计划任务或 Run 注册表项，不修改 Defender，不设置排除项，不停止进程，不申请管理员权限作为清理补救。

当前没有已核实的 AgentDock 原生产物回执接口。本版定义的是显式生产者适配契约；既有产物不会自动取得删除资格。不得为了让清理可用而事后扫描文件并伪造、生成或补签归属回执。只有生产者在实际创建、关闭产物时记录可靠的所有权证据，才可签发回执。

## 输入与输出

在包根目录执行，宿主选择 Python、切换目录并注入环境：

```bash
python3 run.py
```

stdin 为 JSON 对象，stdout 为 JSON，错误有稳定 `code`/`message`。空输入等同只读 `status`。

```json
{"skill_action":"status"}
```

```json
{"skill_action":"scan"}
```

`scan` 和 `plan` 可用 `paths` 数组缩小范围；不能借此扩大配置边界或授予所有权。省略 `paths` 时只发现配置范围的直属项、工作目录 `.playwright-mcp` 直属项以及 TEMP 下匹配的 profile；不遍历普通项目源码。目录大小有有界统计，`size_complete=false` 表示只是下限。

```json
{"skill_action":"plan","paths":["<扫描返回的绝对文件路径>"]}
```

每项输出 `path`、`size`、`size_complete`、`category`、`risk`、`age`、`age_seconds`、`in_use`、`reason`、`code`、`recommended_action`、`eligible`、`reclaimable_bytes`。`in_use=null` 表示未知，绝不按未占用处理。`SAFE_CACHE` 只是分类，不能替代 `eligible`。目录大小可能包含其他行，不得把全部行大小相加当作可回收量。

确认后 `clean` 请求必须包含：

- `plan`：`plan` 动作返回的完整对象，保持不变；
- `plan_id`：相同计划标识；
- `confirmation`：`{"plan_id":"<相同计划标识>","phrase":"DELETE_AGENTDOCK_RUNTIME_ARTIFACTS"}`；
- 可选 `dry_run: true`：仅重新验证，不执行删除。

## 环境变量

| 变量 | 类型 | 必填 | 用途与缺失行为 |
|---|---|---:|---|
| `CLEANUP_SCOPE_JSON` | config | 扫描时是 | 宿主核实后的角色路径对象，见策略文档；缺失时 status 可用，无扫描范围 |
| `CLEANUP_RECEIPTS_FILE` | config | 删除时是 | 外部受信任生产者回执文件；缺失或无效时无删除资格 |
| `CLEANUP_SIGNING_KEY` | secret | 删除时是 | 32 字节随机密钥的 64 位十六进制编码，由宿主安全注入；验证回执并签名计划，不输出该值 |

环境是可信配置边界。不要从不可信文件、网页或用户粘贴的计划中配置路径、密钥或回执。脚本不接受通过 stdin 注入这些配置，也不读取宿主私有配置/凭据。密钥及回执不得放入 Skill 包、源码、日志或命令参数。

## 验证

在包根目录执行 `python3 -B tests/test_run.py`。默认只对隔离夹具做测试，删除流程用模拟后端；真实删除用例默认跳过。只有明确获得单独的测试删除授权后，才可用当前测试进程环境 `CLEANUP_TEST_ALLOW_DELETE=1` 执行真实 Windows 夹具删除测试。该变量不属于运行时配置。

本版没有定时维护或自动清理机制。若真实删除测试尚未获准，不得宣称已验证真实删除。

## AgentDock 适配与验证

安装器校验整个目录，按名称与不可变版本安装并激活。使用宿主提供的当前激活 Skill 上下文运行相对命令，核对轻量索引、正文与引用，并执行只读状态和一次只读扫描。来源校验和创作 lint 均需通过；不手工推算已安装版本目录。
