# 潮汐 Tide

一款可自行运行的中文聊天工作台，提供经济和极速两条模型线路。访客可以先免费试用演示，再决定是否注册并使用已配置的付费线路。

## 先运行

1. 在 Windows 上将完整项目解压到普通文件夹。安装 Python 3.10 或更高版本后，双击 **start.cmd**。使用 macOS 或 Linux 时，在项目目录运行 `python3 start.py`。
2. 浏览器会打开 [本机工作台](http://127.0.0.1:8765)。保持启动窗口打开即可使用。此项目没有需另外安装的 Python 或 npm 依赖。
3. 点「写点东西」，再点发送，试一条演示对话。点击输入框左下角的模式名可切换线路。页面会标明演示回复，费用为零。

## 接入自己的模型

将 `.env.example` 复制为 `.env`。在文件中填写服务端密钥，保存后重启。极速线路可使用 `DEEPSEEK_API_KEY`。经济线路填写 `ECONOMY_BASE_URL`、`ECONOMY_MODEL` 和 `ECONOMY_API_KEY`，支持带服务端 API 的 Chat Completions 兼容供应商。两条线路可以独立开通。

服务端按供应商返回的输入、输出 token 数量和你配置的单价扣款。模板里的金额是可修改的示例售价。点击「用量与余额」查看余额与流水。实际可用的上下文长度取决于所选模型、账户余额和请求大小。

首次试用真实模型时，先在工作台创建自己的账号，再在项目目录发放体验余额。例如，向用户名为 `demo_user` 的账号发放 1 元，可运行 `python admin.py credit --username demo_user --amount 1.00 --reference trial-demo-001`。同一笔发放重试时使用相同编号。选择已配置的线路发送消息，真实模型的调用费用由供应商账户承担。

服务端定价、账号、存储和接口的配置见 [后端说明](BACKEND.md)。Stripe Checkout 的商户配置、回调注册和测试步骤见 [支付接入](PAYMENTS.md)。本项目使用内置 SQLite 账本。

## 迁移对话

在「记录迁移」中选择 DeepSeek 导出的 v3 ZIP、兼容的会话 JSON，或另一台设备导出的潮汐 JSON。确认待导入数量后，系统合并去重并保留现有记录。DeepSeek 备份取当前分支的提问与回答正文，图片、工具记录及其他分支不在迁移范围中。

消息记录按浏览器、站点和账户分开保存。更换设备、清理浏览器数据或切换访问地址前，可先导出备份。模型密钥和支付密钥只在服务端读取。发送真实对话时，当前会话正文和附加的文本会提交给所选模型供应商。

## 自检

在项目目录运行 `python -m unittest discover -v`，验证注册登录、聊天计费、失败重试、并发结算、体验额度和模拟支付回调。备份格式回归测试还会调用 Node.js，已用 Node 24 验证。测试使用临时数据库及模拟供应商，不产生费用。

模型接口和迁移格式依据 [DeepSeek 聊天 API](https://api-docs.deepseek.com/api/create-chat-completion/) 与 [官方导出格式](https://github.com/deepseek-ai/chat-demo/blob/main/src/services/chat/history/README.md)。此版本采用独立前端，提供多轮文本聊天、TXT/Markdown/CSV 附件、历史搜索与导入导出。
