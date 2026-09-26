---
name: desktop
description: macOS desktop automation as an AgentDock Skill workflow.
---

# macOS Desktop Skill

AgentDock 的 macOS 桌面自动化能力已经从 core 工具拆分为此 Skill 工作流。

## 调用入口

- `skill_action=observe`：`action=preflight | list_apps | app_state | window_list | snapshot | snapshot_app`。
- `preflight` 会分别报告 Screen Recording、System Events Automation 与 Accessibility/AX；无法实际验证 AX 时返回 unknown/not_checked，不再用普通 AppleScript 成功代表桌面能力全部就绪。
- `skill_action=act`：`action=focus | move | click | double_click | scroll | drag | type | set_value | secondary_action | hotkey | wait`。
- `skill_action=clipboard-read` / `skill_action=clipboard-write`：读写剪贴板。

## 数据目录

截图原始文件写入 `SKILL_DATA_DIR/artifacts`；AgentDock managed 运行时由宿主注入私有 `SKILL_DATA_DIR`，其他宿主使用 XDG 状态目录作为 fallback。`snapshot` / `snapshot_app` 同时发布到 `AGENTDOCK_HOME/public-artifacts`，并返回包含 `artifact_id` 的轻量 `screenshot` 引用。配置 `AGENTDOCK_SERVER_URL` 时额外返回带过期时间的签名 `screenshot.url`。需要模型查看图片时，再调用 AgentDock `view_image(artifact_id=...)`；Desktop Skill 不直接返回 Base64。

- 动作参数不支持别名：`skill_action=act` 必须使用 `action` 枚举值；`secondary_action` 的辅助动作名必须放在 `ax_action`；`wait` 使用 `ms`。

## 辅助脚本执行

Skill 本体是本说明文档。确需调用包内辅助脚本时，在 Skill 包根目录使用相对路径执行；运行宿主负责切换到包根目录并把所需变量注入当前子进程。

```bash
printf '%s' '{"skill_action":"<动作>"}' | python3 run.py
```

输入必须是 JSON 对象。写操作仍按本文档中的确认规则执行。

| 动作 | 用途 |
|---|---|
| `status` | Run a desktop preflight check and separately report Screen Recording, System Events Automation, and Accessibility/AX readiness. |
| `observe` | Unified macOS desktop observation action: preflight, list_apps, app_state, window_list, snapshot, or snapshot_app. |
| `act` | Unified macOS desktop action: focus, move, click, double_click, scroll, drag, type, set_value, secondary_action, hotkey, or wait. |
| `clipboard-read` | Read macOS clipboard text. |
| `clipboard-write` | Write macOS clipboard text and optionally verify by reading it back. |
