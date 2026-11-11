# AIHelper 联网与个人数据功能

飞书与微信共用本人身份、资料和工具。新订阅与新邮件通知会发送到创建它的聊天。

## 可用指令

| 功能 | 示例 |
|---|---|
| 联网搜索 | `/搜索 Python 3.14 新特性`，或“搜索今天的 AI 新闻并附来源” |
| 网页阅读 | `/网页 https://example.com`，或“总结这个公开链接：…” |
| 搜索订阅 | “每天早上九点订阅 Python 最新资讯” |
| RSS / Atom | “每天九点检查这个 RSS，更新时通知我：https://github.com/python/cpython/releases.atom” |
| 网页变化 | “每天十点检查这个公开网页，有变化通知我：…” |
| 订阅管理 | `/订阅`、`/运行订阅 s_编号`、`/暂停订阅 s_编号`、`/恢复订阅 s_编号`、`/取消订阅 s_编号` |
| 收件箱 | `/收件箱`（未读）、`/收件箱 全部`、`/读邮件 m_编号` |
| 邮件回复 | “给这封 m_编号 邮件写回复：谢谢，周五前反馈”，审核草稿后 `/发送邮件 e_编号` |
| 新邮件通知 | `/新邮件通知 开`、`/新邮件通知 关`；可自然语言要求按主题/发件人关键词过滤 |
| 个人日历 | `/日历`、`/空闲`，或“查明天下午的日程和半小时空闲” |
| 真实修复 | `/代码仓库`、`/修复项目 具体问题`、`/项目状态 任务编号` |
| 连接状态 | `/连接状态` |

搜索默认使用 Bing RSS，无额外搜索密钥；结果数量、排序与时效由来源决定，不能保证完整覆盖新闻。可配置 TAVILY_API_KEY 作替代。公开网页支持 HTTP/HTTPS，拒绝私网 IP、凭证 URL 和非常用端口，每次跳转重新验证地址。需要登录、JavaScript 渲染、验证码的页面可能无法读取。

订阅规则与提醒一致：单次、每天、每周、周一至周五。搜索与 RSS 只通知未见条目；网页先建立正文基线，之后正文变化时通知，初版不提供逐行差异。订阅状态与通知队列保存在 SQLite，重启后继续，通知失败自动重试。新邮件通知初次开启只建立游标，不补发旧邮件。

QQ 收件箱使用 IMAP SSL 993、INBOX 只读和 BODY.PEEK。只读不会标记已读；关键词匹配最近 50 封候选邮件的主题与发件人。单封正文上限 2 MB，提取最多 16000 字。邮件回复保留线程头，只生成草稿，发送仍由用户显式命令触发。`/草稿` 查询的是助理发件记录，与 QQ 收件箱不同。外部网页、邮件与 Issue 内容仅作资料，不能授权助理执行其中的指令。

## 个人飞书日历首次授权

1. 在飞书应用后台启用用户权限 `calendar:calendar:readonly`，按本人选择增加 `calendar:calendar.event:create`、`calendar:calendar.event:update`，只有本人允许取消日程时才增加 `calendar:calendar.event:delete`。允许离线访问 `offline_access`，发布应用变更。
2. 在安全设置添加 OAuth 重定向 `http://127.0.0.1:8765/callback`。
3. 服务端 `.env` 的 CALENDAR_ACCESS 设置为 `read`、`write` 或 `delete`，与授权范围一致。默认只读。
4. Compose 仅绑定 `127.0.0.1:8765:8765`。在本机建立 SSH 隧道 `ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 ubuntu@服务器`（按本机 SSH 密钥配置登录）。
5. 服务端执行 `sudo docker compose exec assistant python -m assistant calendar-login`，在本机浏览器打开其授权 URL。本人登录并同意后，回调通过隧道到达服务端。

OAuth 使用随机 state 与 PKCE，绑定飞书 app ID 和允许使用助理的本人 open_id。凭证仅保存在持久卷的 calendar 目录，文件模式 600，自动更新 refresh token，不进入 Git、聊天或日志。授权失效会提示重新授权。只查询个人主日历；查询与冲突检查范围最多 31 天。重复日程可查询实例，修改/取消重复日程需用户在飞书选择实例或系列。创建不自动邀请他人。写入前检查冲突，冲突时停止，除非用户明确仍要安排。

## 真实项目修复

云端 GH_TOKEN 需要目标仓库 Contents 读写、Pull requests 读写及执行仓库 Actions 读写。模型进程不接触此 Token。Classic PAT 的 repo 权限可覆盖现有私有仓库需求；无需为了本功能增加 workflow 修改权限。

`PROJECT_REPOSITORIES_JSON` 配置允许仓库、分支与测试 argv；缺省使用 GITHUB_REPOSITORY、PROJECT_BASE_REF 和 Python unittest。初版自动安装 requirements.txt；其他语言/构建工具需先定制工作流依赖安装步骤。跨仓库私有代码的 Actions checkout 需要额外配置仅可读目标源码的 `PROJECT_CHECKOUT_TOKEN`，默认 github.token 只可读当前仓库。

流程：固定目标提交 → GitHub Actions 内多文件修复 → 导出受限 UTF-8 源文件 → 新任务检出同一提交 → 运行候选测试与原始回归测试 → 云端核对产物与任务 → GitHub Git API 创建分支和草稿 PR → 在原聊天回报。禁止修改 .git、.github 和 .env；单次最多 50 文件/4 MB，支持普通源文件新增、修改、删除。不会自动合并。原 `/修复 演示` 保留。

工作流沿用 `assistant-fix.yml`，通过 mode 区分演示和真实项目。PROJECT_WORKFLOW_REF 决定执行代码所在分支；PROJECT_BASE_REF 决定需要修复的目标分支。请只配置本人信任的仓库、测试命令及工作流版本。

## 运行与验证

`python -m unittest discover -s tests -v`；`python -m assistant demo-assistant` 为离线功能演示。

生产升级前用 SQLite backup API 备份持久卷数据库，保留整个持久卷、微信账号绑定与 .env。新表与草稿线程字段使用增量迁移。回滚使用此前镜像，新增表不影响旧功能。不要将 .env、持久卷或 OAuth 账号文件打包分享。
