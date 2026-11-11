# 微信 ClawBot 接入

通过腾讯公开的 iLink 协议，为 AIHelper 增加个人微信文字入口。协议参考 [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md)。本项目直接实现 Python 文字通道，无需额外运行 OpenClaw。

## 绑定

在已有助理的 `.env` 加入 `WEIXIN_ENABLED=true`，重建镜像并重新创建容器。

```sh
docker compose up -d --build
docker compose exec assistant python -m assistant weixin-login
```

绑定命令会输出 `QR_READY` 和二维码 PNG 路径。默认路径为 `/data/weixin/login.png`。保持命令运行，在另一终端复制图片并用本人的手机微信扫码确认。

```sh
docker compose cp assistant:/data/weixin/login.png ./weixin-login.png
```

首次绑定仅需要本人微信扫码。凭证保存到数据卷的 `weixin/account.json`，目录权限 700，文件权限 600。微信线程自动加载新凭证，绑定后无需再次重启。

如果手机要求输入数字，绑定进程会输出 `PIN_REQUIRED`。将手机显示的数字写入数据卷的 `/data/weixin/verify-code` 文件，程序读取后立即删除。不要把数字、绑定凭证或 API Key 发到聊天、提交到代码仓库或打包发布。

二维码过期时，程序会刷新，重新复制图片后扫码。连续三次失败后退出，可重新运行绑定命令。微信登录失效时日志会提示重新扫码。

## 使用

在微信 ClawBot 聊天中发送“一分钟后提醒我喝水”“记一条待办”“帮我写一封邮件”，或使用 `/提醒`、`/待办`、`/草稿`、`/修复 演示`。邮件仍先返回草稿，用户输入 `/发送邮件 草稿编号` 后发送。

仅接收扫码本人发来的私聊文字。图片、语音和文件会提示使用文字。微信和飞书共用同一个助理所有者，因此共享提醒、待办、联系人、偏好和草稿。对话历史按聊天保留，任务通知发送到创建它的聊天。

只有一个已授权飞书用户时会自动关联该用户。多个用户时，须将 `WEIXIN_OWNER_OPEN_ID` 设置为其中本人的 open_id，避免身份归属不明确。`/whoami` 返回助理使用的飞书所有者标识。

## 持久化与发送

收到消息后，先将轮询游标、上下文令牌和待处理消息放在同一 SQLite 事务中保存，再进入现有助理队列。重启后恢复待处理消息，消息编号按账号、发送者及原消息编号计算，防止重复执行工具。

回复会复用微信返回的 `context_token`。长文本分段发送，已确认成功的分段单独保存，失败重试只处理余下分段。微信侧令牌有效期及去重行为以平台实际响应为准，不能承诺任意故障下严格恰好一次投递。微信拒绝的通知保留在现有重试队列中，实际投递需要服务和账号持续可用。

二维码绑定、微信真实收发和定时通知需要实际账号验收，自动测试中的微信 API 和模型使用模拟响应。

健康文件包含 `weixin_status` 与 `weixin_worker_alive`。`login_required` 表示需要扫码，`connected` 表示轮询成功，`retrying` 表示网络或接口异常后正在重试。通用 `health` 同时检查微信工作线程存活情况，首次扫码状态可以通过健康文件单独查看。
