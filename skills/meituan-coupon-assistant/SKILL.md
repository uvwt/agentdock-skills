---
name: meituan-coupon-assistant
description: 当用户明确要求领取美团优惠券、红包或神券，并希望通过美团登录完成当前一次领券时使用；只执行当前请求，不创建后台或定时任务。
---

# 美团领券助手（AgentDock 适配版）

本 Skill 根据用户提供的 `meituan-coupon-assistant` 包适配。保留上游 Passport 登录、二维码生成和领券业务流程，移除上游宿主专属运行时绑定、包外状态符号链接、运行时依赖安装和用户 Home 目录中的动态更新加载。

## 安全边界

- 仅在用户明确发起当前一次美团领券请求时执行；不做消费推荐，也不创建定时、后台或自动任务。
- 登录 Token、device token 和 Passport 会话只保存在本 Skill 独立数据目录；不得展示、复制或迁移其他应用的凭据。
- 对外统一通过根目录 `run.py`，stdin 传 JSON，顶层字段为 `skill_action`。不要直接调用包内 `pt-passport` 或让 Token 出现在命令行和回复中。
- 网络用于美团 Passport 授权、`https://click.meituan.com` 二维码接口和 `https://media.meituan.com` 领券接口。不得覆盖 client id、aiScene 或接口地址。
- 包内 Passport 与 CLIGuard 代码来自用户提供的上游包；AgentDock 适配版只加载包内固定版本，不读取用户 Home 目录中的更新代码，也不启动 CLIGuard wrapper 的后台更新或上报逻辑。
- 请求失败、超时或结果未知时不使用同一 Token 自动重试。401 仅按上游流程清除旧登录并进入一次新授权。
- `clear_device_token` 会清除设备标识并影响登录状态，必须由用户明确确认后执行。

## 数据目录

在 AgentDock managed Skill 中，不需要用户配置数据目录。宿主执行 `exec_command(skill_ref=...)` 时会自动注入保留变量 `SKILL_DATA_DIR`；根目录 `run.py` 优先使用它保存设备标识、Passport 登录状态、临时授权状态、当日领券缓存和诊断日志。

- 不要通过 `skill_manage env_set` 或请求级 `env` 配置、覆盖 `SKILL_DATA_DIR`。
- shared/workspace 同名候选不会获得 managed Skill 的 `SKILL_DATA_DIR`。
- 在不提供 `SKILL_DATA_DIR` 的其他宿主中，可以显式设置 `MEITUAN_COUPON_DATA_DIR` 作为兼容 fallback；两者同时存在时始终以 `SKILL_DATA_DIR` 为准。
- 持久状态只写数据目录，不写回 Skill 包。

运行需要 Node.js >= 18 和 Python 3；其他 JavaScript 依赖均已固化在包内，不在运行时安装依赖。

## 执行接口

### 只读状态

```json
{"skill_action":"status"}
```

只检查独立数据目录中是否存在设备状态和登录状态，不访问网络，也不返回凭据内容。

### 当前一次领券

```json
{"skill_action":"execute"}
```

返回两类状态：

- `AUTH_REQUIRED`：读取 `references/auth-flow.md`，向用户展示本次返回的 `url` 和可选 `imageUrl`。用户完成登录后再执行 `auth_complete`。
- `TERMINAL`：直接把 `response_text` 作为本次结果；不要改写成功/失败语义，也不要自动重试。

### 完成登录并领券

用户明确表示已完成刚才的美团登录后执行：

```json
{"skill_action":"auth_complete"}
```

该动作会等待本次 Passport 授权并立即执行一次领券。执行完成后使用真实 `response_text` 返回结果，不创建新的授权会话。

### 退出登录

```json
{"skill_action":"logout"}
```

只在用户明确要求退出时执行。保留设备标识。

### 清除设备标识

只有用户明确二次确认后执行：

```json
{"skill_action":"clear_device_token","confirmed":true}
```

## 通用运行方式

从 Skill 根目录执行入口脚本，业务请求通过 stdin 传入：

```bash
printf '%s' '{"skill_action":"status"}' | python3 run.py
```

非 AgentDock 宿主在需要持久状态的动作前设置 `MEITUAN_COUPON_DATA_DIR`；入口脚本不会把 Token、Cookie 或授权状态写回 Skill 包。

## AgentDock 运行方式

从 `agentdock_context` 选择 managed 候选，使用它返回的精确 `skill_ref` 执行根目录 `run.py`，并把 JSON 放入 stdin。不要按名称重建安装路径，不要 `source` 环境文件，也不要把秘密写到参数中。运行时数据目录由宿主通过 `SKILL_DATA_DIR` 注入。
