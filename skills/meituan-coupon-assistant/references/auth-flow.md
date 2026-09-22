# 登录与协议流程

当 `execute` 返回 `state=AUTH_REQUIRED` 时，向用户展示返回的 `url`；如果同时返回 `imageUrl`，也可以展示二维码图片地址。不得自行修改、拼接或缓存授权链接。

登录提示应包含：

- 链接有效期约 10 分钟；
- 登录完成后再执行 `auth_complete`；
- 上游包自述为美团官方开发并提供；
- 协议链接：
  - Skills 服务使用规则：https://page.meituan.net/html/1781510159285_2567a9/index.html
  - 美团用户服务协议：https://rules-center.meituan.com/rule-detail/4/1
  - 隐私政策：https://rules-center.meituan.com/m/detail/guize/2

`auth_complete` 会同步等待授权结果，成功后立即执行本次领券。授权失败或超时时不要自动创建第二个授权会话。
