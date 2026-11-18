# 潮汐后端

需要 Python 3.10 或更新版本。依赖均来自 Python 标准库。在项目目录运行以下命令。

```powershell
python server.py
```

打开 http://127.0.0.1:8765 。可用 `--port 8787` 切换端口。默认仅监听本机。浏览器和接口共用一个来源。托管在其他域名时，将 `PUBLIC_BASE_URL` 配成该站点的 HTTPS 根地址，并在反向代理中保留原始 Host。服务端静态文件仅从 dist 读取。

## 模型与余额

复制 `.env.example` 为 `.env`，填写需要开通的线路，按实际业务售价设置每百万输入和输出 Token 的人民币单价。重启后生效。系统环境变量优先。默认售价为示例值。极速线路默认使用 DeepSeek 的 deepseek-flash 模型。经济线路可连接具有服务端 API 密钥及商用授权的兼容渠道，也可单独配置 DeepSeek 官方接口。填写供应商给出的基础 URL，服务端追加 /chat/completions。

游客及未配置的线路会返回 demo=true 的免费示例。已配置的线路对已登录用户按用量计费。新账号余额为零。充值配置见 [支付接入](PAYMENTS.md)。商户订单经签名回调确认后写入同一余额和流水数据库。

真实请求连同当前对话历史发给所选线路。请求体上限 8 MiB，最多 4096 条消息，单次输出上限 4096 Token。可输入的长度也取决于供应商的模型窗口和账户余额。系统以输入字节数保守预估余额，余额接近耗尽时可能需要缩短历史；结算只看供应商实际返回的 prompt_tokens 与 completion_tokens，不单独计算缓存折扣。模型调用失败或用量缺失时不扣用户余额。

每个账号同时最多生成一条回复。服务端以事务写入余额、用量流水和幂等响应，相同请求编号重试只扣一次。失败后可用相同编号和内容重试。每次尝试单独标识，挂起超过五分钟的请求在用户下次尝试时释放，过期尝试返回的结果不参与结算。支付到账和模型扣费共用 SQLite 事务。数据库存储密码的加盐 PBKDF2 摘要、会话令牌的摘要、余额、流水以及付费请求的回复副本（用于幂等重试）。请求摘要不含原始用户消息。浏览器端聊天记录的存储和迁移由前端处理。备份 SQLite 时使用 SQLite 备份工具，或先正常停止服务再复制数据库及同目录存在的日志文件。

## 本机发放体验余额

配置好一条真实模型线路，启动服务并在页面注册账号后，可在同一项目目录用管理员命令发放余额。命令读取和服务端相同的 .env 与数据库。将示例用户名替换成实际账号。

```powershell
python admin.py users
python admin.py credit --username demo_user --amount 1.00 --reference welcome-demo-user-001
```

每次发放提供一个唯一业务编号。重跑同一笔会返回已有流水及当前余额；如要追加额度，应更换编号。复用编号但变更用户或金额会报错。额度可用于真实模型调用。每笔发放按 credit 类型写入账户明细。users 默认列出最早的 100 个账号，用 --after 和 --limit 翻页。

## HTTP 接口

- GET /api/session 返回当前身份、人民币余额、线路、单价和支付状态。
- POST /api/register 或 /api/login 接受 username 和 password。用户名长度 2–40，密码长度 8–128。成功后通过 HttpOnly 会话 Cookie 标识用户，有效期 14 天。POST /api/logout 使当前会话失效。
- POST /api/chat 接受 mode（economy / fast）、requestId（UUID）和 messages（role、content 对象数组）。响应包含 reply、demo、usage.input、usage.output、cost 和 balance。cost、balance 单位为元。演示调用的 usage 为零。
- GET /api/ledger 返回最近 500 笔流水，amount 为有符号人民币金额。充值为 topup，管理员发放额度为 credit，用量扣费为 chat。

普通写入接口需使用 Content-Type: application/json。聊天和创建支付订单时可传 expectedUserId，登录用户传整数 ID，游客传 null。服务端按同一请求的 Cookie 校验身份，账号发生变化时返回 HTTP 409。错误响应包含 error 字段。余额不足为 HTTP 402，重复或冲突请求为 409。支付回调直接校验原始请求体和 Stripe 签名。

官方参考：[聊天接口](https://api-docs.deepseek.com/api/create-chat-completion/) · [多轮对话](https://api-docs.deepseek.com/guides/multi_round_chat/) · [模型与供应商定价](https://api-docs.deepseek.com/quick_start/pricing/)。
