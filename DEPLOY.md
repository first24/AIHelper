# Linux 云端部署与验收

此交付准备了部署包，尚未在你的云主机上运行。没有服务器时可先运行本地离线演示，不需要先购买主机或配置邮箱。

## 1. 主机和 Docker

使用受支持的 Linux 主机，安装 [Docker Engine](https://docs.docker.com/engine/install/) 及 Compose 插件。模型在 DeepSeek API 运行，无需 GPU。确保主机能够访问飞书 HTTPS/WebSocket、DeepSeek HTTPS、QQ 邮箱 `smtp.qq.com:465`（真实发信时）和可选的 GitHub API。

应用不监听公网业务端口，Compose 没有 ports 映射。服务器管理使用你自己的 SSH 登录方式。

## 2. 配置文件

解压源码包，进入目录：

```sh
cp .env.example .env
chmod 600 .env
```

在服务器编辑 `.env`：

| 配置 | 用途 |
|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 当前已发布的企业自建机器人 |
| `ALLOWED_OPEN_IDS` | 允许使用助理的本人 open_id；多用户用英文逗号分隔 |
| `DEEPSEEK_API_KEY` | 云端助理理解消息、调用工具；须在服务端单独填写 |
| `DEEPSEEK_MODEL` | 默认 `deepseek-flash`，也支持 `deepseek-v4-pro` |
| `GH_TOKEN` | 可选，代码修复派发和 PR 创建 |
| `SMTP_USER` / `SMTP_AUTH_CODE` | 可选，QQ 完整邮箱地址和客户端授权码 |
| `ASSISTANT_TIMEZONE` | 默认 `Asia/Shanghai` |

GitHub Actions Secret 的值不能由云端助理直接取回。可以在 DeepSeek 开放平台为助理服务创建独立 Key，便于分别管理日常对话与代码修复的用途。GitHub 仓库仍保留工作流的 `DEEPSEEK_API_KEY`。

云服务器使用 `USE_GIT_LOGIN=false`，不继承 Windows 本机 Git Credential Manager。若启用代码修复，PAT 只选择 `first24/AIHelper`，需要 Actions 读写、Contents 读取、Pull requests 读写。无需改变仓库的 PR 审批设置。

QQ 邮箱：在网页版邮箱的账户设置中开启 SMTP 服务并生成客户端授权码；登录密码不能替代授权码。保持 `SMTP_HOST=smtp.qq.com`、`SMTP_PORT=465`。没有填写邮箱配置时可以创建草稿，真实发送会明确提示缺少配置。

飞书权限继续使用 `im:message.p2p_msg:readonly` 与 `im:message:send_as_bot`，订阅 `im.message.receive_v1` 并使用长连接；版本已发布、可用范围包含本人。无需为第二版额外申请全量消息、通讯录或管理员权限。

## 3. 启动

迁移同一个飞书机器人时，先停止本机旧助理，再在云端启动，避免长连接消息分配到不同实例。

```sh
sh deploy.sh
docker compose ps
docker compose logs --tail=100 -f assistant
```

部署脚本会构建镜像、检查配置，再启动服务。不会安装系统软件、购买服务器或修改主机防火墙。

正常日志应显示“通用个人助理模式”和“飞书长连接已建立”。容器健康检查有 120 秒启动宽限期。健康状态仅用于观察；`restart: unless-stopped` 在进程退出后自动重启。

```sh
docker compose exec assistant python -m assistant doctor
docker compose exec assistant python -m assistant health
```

## 4. 飞书真实验收

按顺序执行：

1. `/whoami`，确认云端能收发。
2. “一分钟后提醒我验证第二版”，应回报真实编号与具体时间；到期收到提醒。
3. “两分钟后提醒我喝水”，随后“取消刚才的提醒”，通过 `/提醒` 确认已取消。
4. “每个工作日早上九点提醒我看待办”，通过 `/提醒` 检查规则。发送“改成十点”，应修改同一编号。
5. “记一条待办：本周五提交报价”，然后 `/待办`，按编号标记完成。
6. “每天早上九点把待办汇总给我”，应建立周期简报。
7. 使用自己的邮箱作为测试收件人，请助理撰写一封测试邮件，检查草稿后输入 `/发送邮件 e_编号`。
8. 检查邮箱实际收件。助理的“SMTP 服务端已接受”只证明提交成功，不代表对方阅读。
9. 重启 `docker compose restart assistant`，确认 `/提醒` 和 `/待办` 保留。

自然语言会调用 DeepSeek API；固定提醒到期、简报发送和 `/提醒` 等管理命令不调用模型。

## 5. 日常维护

```sh
# 查看状态和日志
docker compose ps
docker compose logs --tail=100 assistant
# 停止并保留数据
docker compose down
# 更新镜像并启动
docker compose up -d --build
```

数据库保存在 `assistant-data` 命名卷，包含原代码任务及第二版数据。`docker compose down` 默认保留该卷。不要使用带 `-v` 的删除命令处理仍需保留的数据。

备份时先 `docker compose stop assistant`，再把 `/data` 的数据库目录（含 WAL 等文件）备份到你的存储，完成后启动。迁移数据目录时一并保留数据库，不提交 `.env` 或 data 目录到 GitHub。

一个数据目录仅运行一个实例，文件锁会拒绝重复启动。不同机器上的独立数据目录无法互相加锁，因此迁移前仍需主动停止旧主机上的同一机器人实例。

## 6. 常见情况

| 现象 | 检查 |
|---|---|
| DeepSeek HTTP 401 / 402 | 服务端 Key 是否有效 / API 余额是否充足 |
| 飞书连接正常但私聊无事件 | 接收消息事件、私聊读取权限及发布版本是否生效 |
| 提醒延迟 | 主机是否持续在线、网络是否正常、日志中是否有调度重试 |
| QQ 邮箱鉴权失败 | SMTP 是否开启、是否使用当前有效的客户端授权码 |
| 邮件结果未知 | 先核查邮箱；系统不会自动重复发送，可重新创建草稿后再明确发送 |
| GitHub 暂不可用 | 检查 PAT、网络与仓库范围；日常助理可独立运行，修复配置后重启以恢复代码模块 |
| 不能读取收件箱 | 第二版只实现发信，尚未接入 IMAP |
