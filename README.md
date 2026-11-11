# AIHelper 第二版：飞书个人助理

通过飞书自然语言管理提醒、重复任务、待办、联系人、邮件草稿和每日简报，保留第一版已验证的 GitHub 代码修复。模型使用 DeepSeek，服务可用 Docker 在 Linux 云服务器常驻。

第二版提供完整源码、自动测试和部署配置。当前尚未部署到用户的云服务器，QQ 真实发信需要配置邮箱授权码后验收。第一版真实修复记录见 [E2E-RESULT.md](E2E-RESULT.md)。

## 已实现

| 能力 | 飞书示例 |
|---|---|
| 单次提醒 | 明天下午三点提醒我交报告 |
| 多轮修改 | 把刚才的提醒改成四点 |
| 重复提醒 | 每个工作日九点提醒我查看待办 |
| 待办 | 记一下：周五前提交报价 |
| 完成待办 | 把编号 t_xxx 的待办标记为完成 |
| 待办简报 | 每天早上九点把我的待办汇总发给我 |
| 联系人 | 记住小王的邮箱是 wang@example.com |
| 邮件草稿 | 帮我给小王写邮件，说明周五交付 |
| 明确发送 | `/发送邮件 e_草稿编号` |
| 偏好 | 记住我的邮件签名是“张三” |
| GitHub | `/修复 演示`、`/状态 任务编号` |

管理命令：`/帮助`、`/提醒`、`/待办`、`/草稿`、`/取消提醒 编号`、`/清空对话`、`/whoami`。

邮件先生成包含收件人、主题和正文的草稿，用户按编号发送。模型没有发信工具。SMTP 服务端接受、明确失败、结果未知分别记录；发送结果未知时不自动重发。当前只支持单收件人纯文本邮件，不包含附件、抄送、收件箱读取和定时发信。

## 云服务器部署

完整步骤见 [DEPLOY.md](DEPLOY.md)。准备一台能访问飞书、DeepSeek、GitHub 和 QQ SMTP 的 Linux 主机并安装 Docker Engine 与 Compose 插件。无需 GPU；飞书长连接不需要开放公网业务端口。

```sh
cp .env.example .env
chmod 600 .env
# 在服务器编辑 .env，填写下面的配置
sh deploy.sh
docker compose logs --tail=100 -f assistant
```

必要配置：

```dotenv
ASSISTANT_MODE=general
FEISHU_APP_ID=你的应用ID
FEISHU_APP_SECRET=你的应用Secret
ALLOWED_OPEN_IDS=你的飞书open_id
DEEPSEEK_API_KEY=助理服务使用的DeepSeek密钥
DEEPSEEK_MODEL=deepseek-flash
ASSISTANT_TIMEZONE=Asia/Shanghai
```

QQ 发信另填 `SMTP_USER=完整QQ邮箱地址`、`SMTP_AUTH_CODE=客户端授权码`。GitHub 修复另填受限的 `GH_TOKEN`。详细权限和密钥用途见 DEPLOY.md。

云端上线前停止本机旧实例。只运行一个实例；同一数据目录通过文件锁阻止重复启动。服务以非 root 用户运行，容器文件系统只读，任务数据保存在 Docker 持久化卷中。

## 本机开发与验证

建议 Python 3.12 或 3.13：

```sh
python -m venv .venv
# 使用 .venv 的 Python；Windows 为 .venv\Scripts\python.exe
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m assistant demo-assistant
```

`demo-assistant` 是不依赖账号的离线验证：模拟模型与通道，真实执行任务存储、提醒调度、待办和邮件状态管理，写出 `assistant-demo-report.json`。不会发送真实邮件、飞书消息或调用模型。

本机运行可使用 `start.ps1` 或 `python -m assistant run`。`configure.ps1` 可交互生成本机 `.env`。`discover` 模式仅处理 `/whoami`，用于获取发送者自己的 open_id；正式模式同样支持该命令。

`doctor` 检查配置和可选的 GitHub 连接，不调用模型或发送邮件。`health` 检查连接状态、后台工作线程及调度器心跳；Docker 会显示 unhealthy 状态，自动重启适用于进程退出，健康检查本身不会强制重启。

保留第一版单功能运行方式：`ASSISTANT_MODE=legacy`。代码修复细节见 [docs/CODE-REPAIR.md](docs/CODE-REPAIR.md)。

## 运行机制与边界

- DeepSeek 使用原生工具调用，后端仅执行登记并验证参数的工具，没有任意服务器命令工具。
- 用户通过飞书 open_id 授权。提醒、待办、草稿、联系人和会话历史均按用户隔离。
- 日期解析使用当前真实时间与用户时区；工作日表示周一至周五，尚不计算法定节假日。
- 单轮最多 4 次模型往返、8 次工具调用，日常对话使用助理服务自己的 DeepSeek Key，计费进入 DeepSeek API 账户。
- 单次和 daily/weekdays/weekly 重复任务持久化。服务恢复后补发一次错过的提醒，周期任务跳到下一个未来时间，避免历史积压连续发送。
- 固定提醒、待办简报与管理命令不再调用模型。调度器与会话处理线程分离，模型响应慢不会堵住正常的提醒触发。
- 通知使用持久化 outbox、重试退避与稳定消息 UUID；飞书 UUID 的去重受平台有效期约束，不承诺跨任意长故障的严格恰好一次投递。
- 会话只保留最近 6 轮；`/清空对话` 清除会话历史，业务记录单独保留。
- 邮箱授权码、飞书 Secret、GitHub Token 和 DeepSeek Key 由服务器环境读取，不传入模型工具参数。
- 暂无网页浏览、收件箱检索、附件处理、任意项目修改、节假日日历、自动扩展插件等能力。

## 参考

- [DeepSeek 工具调用](https://api-docs.deepseek.com/guides/tool_calls/)
- [飞书 Python SDK 长连接](https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-events)
- [第一版真实修复 PR](https://github.com/first24/AIHelper/pull/1)
