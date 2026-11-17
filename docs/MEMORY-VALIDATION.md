# 记忆升级：本地验证记录

日期：2026-11-17，Asia/Shanghai。

工作分支 `feature/local-codex-bridge`，开始时 HEAD 为 `f83d619646a89b51fee824efd4d70f07e4d5dece`。本轮未提交、未部署。原有 `docs/WEIXIN.md` 改动与全部交接/学习资料保留。

## 本轮实际执行

| 检查 | 结果 | 口径 |
|---|---|---|
| 修改前基线 | 101 项通过 | 本轮重新运行，不沿用历史结果 |
| 最终工程测试 | **138 项通过，2.514 秒** | 原 101 项 + 新记忆模块 37 项；微信既有用例增加输入归档断言 |
| 离线 demo-assistant | 通过 | 提醒、待办、邮件草稿、显式模拟发送；real_model/real_feishu/real_email 均 false |
| 维护命令 smoke | 通过 | migrate、stats、reindex、maintain、SQLite backup；全部临时合成库 |
| 修改源码的 whitespace 检查 | 通过 | `.env.example`、README、assistant、tests；未更改用户已有文档的空行 |

运行环境为 Windows，项目 `.venv` Python 3.14.7。CI 的 Python 3.12、Docker 和真实 Linux 卷权限本轮未验证。

全部测试、demo 和维护命令 smoke 同一轮运行时，使用 mock 阻断 `socket.socket.connect/connect_ex` 与 `socket.getaddrinfo`。没有运行机器人、worker、登录、SSH RPC、GitHub 写入、SMTP、IMAP 或真实模型调用。演示报告写入临时目录并随目录清理，没有覆盖工作区已有报告；没有读取真实 `.env`、私有账号文件、真实数据库或 `.aihelper` 目录。

可复现工程测试：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_memory.py -v
```

上面的普通命令本身不添加网络阻断；本轮工具运行额外包裹了 socket mock。测试中的外部能力仍使用现有 fake/mock。

## 新增行为覆盖

- 增量迁移、外键、旧轮次未知时间、重复导入、持久库重新打开、派生任务恢复。
- 飞书/微信共同入队、归档失败回滚、重复消息不重复记录。
- 六轮外的原文仍可查；命令快照在后台排序变化后仍保持原展示顺序。
- 并行工具请求/结果配对；成功后模型失败仍保留结果；崩溃后 interrupted/unknown，不执行未知写操作；隐藏推理不归档。
- schema、输出预留及安全余量计入预算；每次工具追加后重算；必需输入过大停止动作；供应商超限只缩小上下文重试一次。
- 大结果 JSON 预览、归档分块、证据分页和 payload 到期标记；大量长记忆仅自动注入有句柄的有限预览。
- 跨 owner/chat/epoch 读取与猜测 ID 防护；昨天/上周的时区范围；模糊时间范围披露；当前问题不能成为自身的历史证据。
- 显式保存、版本冲突、跨聊天 owner 记忆、session 记忆隔离、有效期、偏好本次例外和权威值同步遗忘。
- 模糊遗忘先选目标；被删来源的事件、回执、索引和相关摘要失效；排除笔记后再次显式查看也不恢复自动记忆。
- 清空后已排队消息进入新周期；会话删除预览的版本检查和业务数据保留。
- 摘要读取区间、批量排队输入交错、来源 revision/generation 变化、伪造编号、错误动作状态与生成期间删除。
- 派生任务租约、embedding 失败重试和关键词降级、向量模型空间隔离、扫描 partial 标志。语义路径仅使用 fake provider。
- 独立删除清单应用到旧备份、清单重复应用、损坏时停止读取；删除不影响未涉及的可见记忆。
- Codex 背景显式选择、领取前再次过滤和不附无关历史。

## 不代表已完成的验收

上述数字是确定性工程测试数量，不是模型准确率。`docs/memory-eval-cases.json` 为未运行的人工标注场景；没有真实模型 Recall@K、引用正确率、对象选择正确率、误写率、成本或延迟基线。

预算目前是保守估算；官方模型容量与适配 tokenizer 尚未确认。摘要默认关闭，真实生成质量待测。没有配置真实 embedding provider，只能报告“关键词检索降级可用；混合检索实现经 fake 验证”。

保留/删除测试覆盖应用可见性和已知关联副本，不证明 SQLite/WAL/备份介质物理不可恢复。真实数据库迁移、副本规模、备份轮换、容器、平台联调和发布均未执行。完整设计不能因此标为已上线。
