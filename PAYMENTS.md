# 账户充值

潮汐通过 Stripe 托管收银台接受人民币计价的银行卡付款。登录账户可选 10 元、30 元或 100 元。银行卡资料由 Stripe 收集。应用通过已签名的支付成功事件更新 SQLite 中的余额。浏览器从收银台返回时，可以刷新余额查看入账情况。[Stripe 支付确认](https://docs.stripe.com/checkout/fulfillment)

## 本地测试

1. 使用单独的测试数据库和 Stripe 测试账户。在 PowerShell 中给运行后端的终端设置下列环境变量，端口应与后端监听端口一致。将占位符替换为你自己的测试密钥。

   ```powershell
   $env:PUBLIC_BASE_URL = 'http://127.0.0.1:8765'
   $env:STRIPE_SECRET_KEY = '<Stripe 测试密钥>'
   $env:STRIPE_WEBHOOK_SECRET = '<本次 Stripe CLI 监听器给出的 whsec_ 签名密钥>'
   ```

2. 在已登录 Stripe CLI 的另一个终端运行以下命令，复制签名密钥到上一步的环境变量，然后启动后端。

   ```powershell
   stripe listen --events checkout.session.completed,checkout.session.async_payment_succeeded --forward-to http://127.0.0.1:8765/api/payments/webhook
   ```

3. 按 BACKEND.md 配置至少一条可用模型线路，重启服务后在本地登录潮汐并发起 10 元充值。使用 Stripe 测试卡完成付款。收银台返回后刷新账户，核对余额增加 10 元，且仅有一条充值流水。测试回调必须对应本地发起的真实测试订单。[Stripe 测试卡](https://docs.stripe.com/testing)

4. 用下列命令运行离线回归测试，覆盖重复请求、金额与币种校验、未支付通知、签名时效以及入账中断后的重试。

   ```powershell
   python -B -m unittest -v test_payment
   ```

## 开通收款

使用支持人民币银行卡交易的商户账户。在服务器环境中将 `PUBLIC_BASE_URL` 设置为用户实际访问的 HTTPS 源站地址，例如 `https://chat.example.com`。将 `STRIPE_SECRET_KEY` 设置为该商户的正式密钥。在 Stripe 后台注册 `https://chat.example.com/api/payments/webhook`，勾选前述两个事件，将此 **正式端点** 的签名密钥放入 `STRIPE_WEBHOOK_SECRET`，再启动服务。反向代理须保留回调的原始请求正文和 `Stripe-Signature` 请求头。密钥保存在服务端，不得写入网页或分发包。[Stripe 签名与投递](https://docs.stripe.com/events/manage-webhook-endpoints)

生产数据库需单独创建并定期备份。测试密钥仅支持回环地址，测试库内的余额同样可消费，请避免混用测试与生产数据。若使用 Stripe 自定义收银台域名，需在 `payment.py` 中显式配置受信域名，当前实现使用 `checkout.stripe.com`。

## 到账与对账

发起充值时客户端为同一次请求传入相同的 UUID。后端对每个订单使用固定的 Stripe 幂等键。到账时核对订单、收银台会话、币种、金额、环境及已支付状态，将余额更新、充值流水与订单状态写入同一个事务。签名有效期为五分钟，保持服务器时钟同步。Stripe 重试投递时会重新签名。[Stripe 幂等请求](https://docs.stripe.com/api/idempotent_requests)

遇到支付成功但余额未增加，可在 Stripe 后台检查该会话的事件投递结果。用事件的 `data.object.client_reference_id` 匹配本地 `payment_orders.id`，用 `data.object.id` 匹配 `session_id`。处理失败原因后重新投递事件。退款与拒付需由运营人员逐笔核对并处理相应账户余额。
