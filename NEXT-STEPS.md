# 继续真实联调

当前进度：2026-11-10 已完成飞书指令 → GitHub → DeepSeek 修复 → 独立测试 → 草稿 PR → 飞书完成通知的真实闭环。[PR #1](https://github.com/first24/AIHelper/pull/1) 及其 CI 均已验证，等待你审查。下方步骤保留用于在其他主机重新部署；当前主机已配置，无需重复填写凭证。

## 1. 飞书

打开本目录 `.env`，填入已有应用的 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`。本机 GitHub 已有可用登录，`GH_TOKEN` 可留空，保留 `USE_GIT_LOGIN=true`。

在飞书后台确认：启用机器人、可用范围包含你、申请私聊消息读取和机器人发送权限、订阅 `im.message.receive_v1`，使用长连接，并发布应用版本。详细路径见 README。

在本目录 PowerShell 运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Mode discover
```

给机器人发送 `/whoami`，把回复的 open_id 填入 `.env` 的 `ALLOWED_OPEN_IDS`，结束发现模式。

## 2. 模型

在 [仓库 Secrets 页面](https://github.com/first24/AIHelper/settings/secrets/actions) 新建 `DEEPSEEK_API_KEY`，填入 [DeepSeek 开放平台](https://platform.deepseek.com/) 的 API Key。原先的 `OPENAI_API_KEY` 不再被修复工作流使用。不用把 Key 发到聊天里。

当前调用 `deepseek-flash`，费用计入 DeepSeek API 账户。仅保存 Secret 不产生模型调用；发出修复任务后才会调用模型。

## 3. 发出第一条修复指令

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Mode run
```

在飞书私聊发送 `/修复 演示`。验收标准：飞书收到**真实 PR 链接**，该 PR 修正 `19.99 元打九折` 的金额错误，原始测试通过，等待你审查合并。

配置完成后告诉聊天里的助理“已配置”，可以继续检查连接并跟进首个真实任务。
