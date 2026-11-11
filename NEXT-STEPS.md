# 第二版接下来怎么用

你选择了“先准备云部署包”和“QQ 邮箱”。新增代码已放在独立分支，不需要改动当前运行的第一版配置。

1. 无账号验证：运行 `python -m assistant demo-assistant`，检查提醒、待办、草稿和发送状态流程；外部通道均为模拟。
2. 有 Linux 云服务器后，按 [DEPLOY.md](DEPLOY.md) 安装 Docker、填写 `.env`，运行 `sh deploy.sh`。
3. 助理服务须单独配置 DeepSeek Key；GitHub Secrets 中的 Key 不能自动取回。邮箱使用 QQ 客户端授权码，配置时不需要发到聊天里。
4. 云端启动前先停止本机旧机器人实例，然后在飞书按 DEPLOY.md 的真实验收步骤测试。

部署包不包含任何现有凭证、私人会话、邮箱地址或本机任务数据库。详细功能与边界见 [README.md](README.md)。
