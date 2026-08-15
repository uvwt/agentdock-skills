---
name: crawler-api
description: 使用 ShilongLee/Crawler 兼容 HTTP 服务查询公开媒体内容时使用，重点支持抖音关键词搜索、图文/视频筛选与详情查询；不负责绕过登录、验证码、风控或采集非公开信息。
version: 1.0.1
---

# Crawler API

用于调用一个已部署的 ShilongLee/Crawler 兼容 HTTP API。优先处理公开内容查询，当前辅助脚本重点覆盖抖音关键词搜索和详情读取。

## 适用场景

- 按关键词搜索抖音公开内容。
- 只筛选抖音图文作品，或只筛选视频作品。
- 根据抖音作品 ID 读取详情。
- 检查 Crawler 服务是否在线以及是否暴露所需 API。

不用于：

- 绕过登录、验证码、反爬或平台风控。
- 自动获取浏览器 Cookie、账号密码或其他认证信息。
- 采集非公开、受限或隐私数据。
- 未经用户明确授权写入、删除或修改 Crawler 账号状态。

## 环境变量

| 变量 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `CRAWLER_BASE_URL` | config | 是 | Crawler HTTP 服务根地址，例如 `http://127.0.0.1:18081` |

本 Skill 不保存 Cookie、账号密码或其他秘密。

## 调用方式

辅助脚本只使用 Python 标准库，从 stdin 读取 JSON 对象并向 stdout 输出 JSON。

```bash
printf '%s' '{"skill_action":"status"}' | python3 run.py
```

### 服务状态

```json
{"skill_action":"status"}
```

成功时返回服务地址、HTTP 状态和已识别的抖音 API。

### 抖音关键词搜索

```json
{
  "skill_action": "douyin_search",
  "keyword": "露营",
  "offset": 0,
  "limit": 10,
  "kind": "all"
}
```

`kind` 支持：

- `all`：全部作品；
- `image`：只保留图文作品；
- `video`：只保留视频作品。

脚本会把 Crawler 的原始搜索结果归一化为较稳定的小结构，包括作品 ID、描述、作者、作品类型、图片数量和可用的分享链接。图文判断优先依据 `images` 字段，其次参考常见的抖音图文 `aweme_type`。

如果 Crawler 返回 `code=3` 和“请先添加账号”，说明服务本身正常，但当前没有可用的抖音账号 Cookie。此时应明确告知用户搜索尚不能执行；不要自行读取浏览器 Cookie 或伪造账号。

### 抖音详情

```json
{
  "skill_action": "douyin_detail",
  "id": "7375004964311010598"
}
```

详情接口返回 Crawler 原始 `data`，便于后续读取图片、视频、作者或统计字段。

## 数据边界

- 默认动作均为只读 HTTP GET。
- `status` 只读取 `/openapi.json`。
- `douyin_search` 只读取 `/douyin/search`。
- `douyin_detail` 只读取 `/douyin/detail`。
- 不提供添加账号、修改账号、代理配置或下载动作，避免把凭据和写操作混入默认搜索能力。

## 失败处理

- 缺少 `CRAWLER_BASE_URL`：返回 `missing_config`。
- 服务不可达或 HTTP 失败：返回 `request_failed`，保留可诊断错误，不回显秘密。
- 输入字段非法：返回 `invalid_input`。
- Crawler 返回业务错误：返回 `crawler_error`，保留其 `code` 和 `msg`。
- 搜索成功但筛选后为空：正常返回空 `items`，不要把它误判成请求失败。

## 结果使用建议

搜索结果来自平台公开接口，字段可能随上游变化。需要精确处理某个作品时，优先使用搜索返回的作品 ID 再调用 `douyin_detail`，不要依赖搜索结果里所有嵌套字段长期稳定。
