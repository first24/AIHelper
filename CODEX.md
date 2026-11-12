# 微信 / 飞书 → 本机 Codex

云端助理保存任务、执行状态和回报。本机 worker 经已校验主机身份的 SSH 主动领取任务，以本机 Codex CLI 执行，再将最终结果交给云端，原聊天接收开始和完成通知。无需给本机开放端口，Codex 登录凭证始终留在本机。

## 使用

```text
让 Codex 分析 AIHelper 的提醒实现，找出风险并补充测试
/codex 只读分析这个项目，列出三个可以优化的地方
/codex状态 cx_任务编号
/codex任务
/codex继续 cx_任务编号 根据刚才的分析实现第一项优化并验证
/codex取消 cx_任务编号
/codex连接
/codex项目
```

`让 Codex …` 和 `Codex：…` 直接进入任务队列，其余自然语言通过助理工具路由。普通提醒、邮箱与日历沿用原功能。当前默认项目代号为 `aihelper`。

每个新任务建立独立的 Git worktree，保留修改供审查。继续任务复用原工作目录和 Codex 会话，只有本人已结束、具有会话编号的任务可以继续。结果包括最终说明和本机工作目录；源码、生成文件保存在电脑，当前文字通道不会自动发送文件附件。不会自动推送、部署、合并或发邮件；需要草稿 PR 的任务可使用原 `/修复项目` 路径。

## 本机设置

安装官方 Codex CLI，在本机完成 `codex login`。本功能使用本机已有 ChatGPT 登录，消耗该账号适用的 Codex 使用额度；云端原自然语言路由仍使用已有 DeepSeek 配置。

创建私有 runner.json：

```json
{
  "worker": "hp-local",
  "codex_executable": "C:/path/to/native/codex.exe",
  "data_dir": "C:/AIHelper/runner-data",
  "tasks_dir": "C:/AIHelper/tasks",
  "task_timeout_seconds": 3600,
  "ssh": {
    "host": "ubuntu@server.example.com",
    "identity": "C:/path/to/private-key.pem",
    "known_hosts": "C:/AIHelper/known_hosts",
    "container": "aihelper-v2-assistant-1"
  },
  "projects": {
    "aihelper": {"source": "C:/projects/AIHelper", "ref": "feature/connected-assistant"}
  }
}
```

使用 Windows 原生 codex.exe 以避免批处理包装器重新解析参数。用户任务经 stdin 传入，不拼接进 shell 命令。runner 配置中的路径与项目是本机可信配置，聊天消息不能指定任意本机路径。

启动：`python -m assistant.local_codex --config runner.json`。`--once` 领取最多一个任务用于联调。必须在源码目录启动，或配置 PYTHONPATH 指向源码。可将该命令注册为当前用户登录时运行的 Windows 计划任务，普通用户权限、后台隐藏窗口运行。

云端 `.env` 配置 `CODEX_RUNNER_ID=hp-local` 和 `CODEX_PROJECTS_JSON={"aihelper":"AIHelper 本机项目"}`，本机项目代号与云端一致。只有已授权飞书本人和已绑定的微信本人可以创建、查询、取消任务。

## 生命周期与限制

- 空闲时约 15 秒检查一次队列；本机需要开机、联网，用户登录会话中的 worker 需要运行。锁屏可以继续，关机或休眠期间任务排队。
- 同时执行一个任务，最多保留五个待执行/执行中任务。任务默认最多运行一小时，每约五分钟回报一次简要执行状态。
- 每次领取有独立租约；云端只保存租约摘要。领取后持续心跳，超过三分钟失联标记中断，不会自动重跑。重启时本机优先回传待报告结果。Windows Job Object 会在 worker 意外退出时终止其 Codex 子进程。
- 取消执行中任务会在下次心跳时终止该任务的进程树；已产生文件保留。失败或部分完成的结果会明确标记。
- Codex 使用 workspace-write 沙箱，遇到超出权限的操作会拒绝并报告，不会自动关闭沙箱。当前不在手机端提供高权限审批交互。
- Codex 沿用本机已配置的模型、提供方、Windows 沙箱和登录。项目与用户 execpolicy 规则继续生效。本机 worker 配置显式禁用不需要的 MCP 服务与 notify 钩子；worker 不向子进程继承 GH_TOKEN、SMTP 授权码和 DeepSeek 密钥。受信任证书路径继续沿用，保持 TLS 证书校验。
- 这是独立的 Codex 会话；本聊天已有上下文不会自动复制过去。提交任务时提供必要需求，之后通过 `/codex继续` 延续。

官方依据：[Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)。
