---
name: xhs-publisher
description: 当用户要管理小红书图文自动发布器，包括扫描采集素材、AI 准备草稿、账号/Profile 登录状态、多账号调度、正式发帖、发布恢复或后台服务状态时使用。
version: 0.3.0
---

# XHS Publisher

用于操作已经安装的 `xhs-publisher` CLI。Skill 只描述稳定工作流和安全边界；账号 Profile、SQLite、AI 密钥、通知密钥和 launchd 状态都由发布器本身管理，不进入 Skill 包。

## 适用场景

- 查看还有哪些采集来源未发布；
- 扫描新增采集内容；
- 生成但不发布一篇 AI 润色草稿；
- 检查或说明发布前图片引流过滤行为与失败原因；
- 查看或验证账号独立 Chrome Profile；
- 用户明确要求“发一篇”“发布下一篇”时执行真实发布；
- 查看多账号自动调度顺序；
- 处理 `needs_manual_review`，只读核对是否已经发布；
- 查看、安装或卸载 macOS launchd 后台 runner。

不用于采集器本身、验证码破解、滑块绕过、指纹伪装、代理轮换或其他规避平台安全机制的操作。

## 前置检查

先确认 CLI 可用：

```bash
command -v xhs-publisher
xhs-publisher --version
```

如果 CLI 不在 PATH，返回明确失败证据，不要猜测项目绝对路径。

## 常用只读操作

```bash
xhs-publisher account list
xhs-publisher scheduler status
xhs-publisher source list --status pending
xhs-publisher publication list --status needs_manual_review
xhs-publisher service status
```

只想检查自动调度但绝不触碰浏览器和发布时：

```bash
xhs-publisher scheduler run --dry-run
```

## 草稿准备

用户只要求润色、预备或看下一篇，不要求公开发布时：

```bash
xhs-publisher draft prepare --account <account-id>
```

这一步可以调用 AI、验证素材并创建 `ready` reservation，但不会点击小红书发布。

正文生成使用结构化 JSON 作为模型内部传输格式，但最终发到小红书的仍是普通标题和正文。当前正文规则是：标题不超过 20 个字符；正文口语化、按 3—6 个要点组织；不自动追加来源/出处；正文最后一行必须有 6—10 个 `#话题`。如果模型输出不符合这些硬约束，草稿准备失败，不能带着错误格式继续发布。

正文模型地址既可以是 API 根地址，也可以直接是完整 `/chat/completions` endpoint。Skill 排查配置时只确认 `XHS_AI_BASE_URL`、`XHS_AI_API_KEY`、`XHS_AI_MODEL` 是否存在，以及模型请求是否成功；不得回显 API Key。

## 图片引流过滤

当发布器配置启用 `vision.enabled=true` 时，`draft prepare` 和正式发布都会在浏览器上传前自动执行图片引流过滤，不需要 Skill 额外调用模型。

过滤目标包括二维码、手机号、微信/QQ/Telegram/WhatsApp、邮箱、网址/域名、公众号/小程序、加群/加V/扫码咨询/私信关键词领取等明显站外或私域导流。普通作者昵称、平台水印、平台标识和没有联系方式或跳转目标的普通“点赞收藏关注”不能仅凭这些本身判为引流。

命中图片只从本次上传列表剔除，不删除或改写原文件。模型调用失败、结果结构矛盾或无法解析时必须 fail-closed，停止本次准备；不能为了继续发帖而绕过过滤。若全部图片被过滤，来源会进入 `blocked`，不会创建 ready publication。

多模态模型与正文模型使用独立配置。视觉地址既可以是 API 根地址/完整 `/chat/completions`，也可以直接使用完整 `/responses` endpoint；后者由发布器自动切换为 Responses `input_image` 格式。Skill 不读取或输出密钥；需要排查配置时只确认 `XHS_VISION_BASE_URL`、`XHS_VISION_API_KEY`、`XHS_VISION_MODEL` 是否已由发布器环境提供，不回显值。

## 通知

发布器支持 Bark 与钉钉自定义机器人同时启用。钉钉配置只保存环境变量名，真实 Webhook/Secret 不进入 YAML、SQLite、plist、日志或 Skill 包：

- `DINGTALK_WEBHOOK_URL`：机器人完整 Webhook，包含 `access_token`，属于 secret；
- `DINGTALK_SECRET`：机器人启用“加签”时提供，可选；
- `notification.dingtalk.keyword`：会放进每条 Markdown 消息，使用钉钉“自定义关键词”安全模式时应与后台关键词一致。

钉钉和 Bark 分别记录通知去重状态。某个通道成功后，另一个通道失败不会让已成功通道重复发送；通知失败始终只是旁路错误，不能回滚 `published`，也不能触发重复发帖。

## 正式发布

用户明确要求自动选择账号并发一篇时：

```bash
xhs-publisher scheduler run
```

调度器一次最多产生一篇真实发布；会先应用每日上限、最小间隔、未决发布检查和账号登录检查。

用户明确指定账号时：

```bash
xhs-publisher publish next --account <account-id>
```

发布成功必须以 CLI 最终返回 `status: published` 为准。程序内部还会经过成功页和笔记管理二次验证，不能把“点击了发布”当作完成。

## 登录与人工接管

查看登录：

```bash
xhs-publisher account status <account-id>
```

需要重新登录时：

```bash
xhs-publisher account login <account-id>
```

验证码、扫码、滑块和安全验证必须由用户处理。不要实现或调用绕过验证的流程。每个账号只能使用自己的 Persistent Profile。

## 未决发布恢复

CLI 退出码 `21` 或数据库状态为 `needs_manual_review` 时，禁止再次执行 `publish next` / `scheduler run` 来碰同一来源。

先查看：

```bash
xhs-publisher publication list --status needs_manual_review
```

然后只读核对：

```bash
xhs-publisher publication reconcile <publication-id>
```

只有笔记管理实际找到对应笔记后，发布器才会收敛为 `published`。找不到时保持未决，继续报告证据，不要自动二次提交。

CLI 退出码 `20` 表示账号需要人工登录。

## 后台服务

```bash
xhs-publisher service status
xhs-publisher service install
xhs-publisher service uninstall
```

`service install` 只安装并加载 launchd，不应通过 Skill 额外 kickstart 一次发布。后台周期由发布器配置控制，并有全局 PID 锁避免重入。

## AgentDock 适配

在 AgentDock 中使用普通命令工具执行上面的 CLI；需要真实发布时，用户当前请求必须明确包含发布意图。只读状态、dry-run 和 reconcile 可以直接执行。

执行后至少返回：账号、publication ID、标题、最终状态，以及是否需要登录/人工核对。不要返回 Cookie、验证码、AI Key、Bark device key、钉钉 Webhook/Secret 或浏览器 session 数据。
