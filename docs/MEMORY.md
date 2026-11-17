# 记忆机制：使用、配置与恢复

2026-11-17。对应 `MEMORY-IMPROVEMENT-PLAN.md` 的本地实现；未部署、未检查真实账号在线状态。设计文档保留原样，本文区分实际能力与待验证项。

## 现在的行为

- 微信 drain 与飞书 receive 共用 `State.enqueue`：消息入队和输入事件归档同事务提交，重复输入不重复归档。
- 命令、模型公开响应、工具开始/结果、最终回复都有来源编号。工具成功后模型失败，成功记录仍保留；中断不伪造成功，不自动重跑业务。
- 归档与近期窗口分开：归档默认保留 90 天；模型近期目标为六个完整轮次，超预算时整组移除。旧记录可检索，不因第七轮到来就删除。
- `/提醒`、`/草稿`、`/收件箱` 等命令结果可作为后续上下文资料。列表保留当时顺序；这解决信息传递断点，尚未用真实模型证明所有指代表达都正确。对象状态仍须查询，歧义须澄清。
- 显式长期事实/决定有 owner/session 范围、来源、版本及替代关系。首版只接受用户明确要求保存的原话片段，宁可澄清，不自动把模型总结当成确认事实。
- 笔记和偏好仍由原表管理；版本表是只读历史，不是第二套可独立编辑的值。偏好更新须带当前版本；“这次简单说”不覆盖长期偏好。
- SQL 支持中文子串、字面 `%`/`_`/反斜杠、时间半开区间，以及本人允许范围内的跨会话历史。结果带来源、时间与覆盖范围；先找候选，再按真实编号读取。
- 历史只能证明“当时记录了什么”，不能证明提醒、邮件或代码任务的当前状态，也不能授予执行权限。

没有配置真实 embedding provider。默认 `semantic_available=false`，仅关键词检索可用。SQLite 向量存储、权限前置、向量校验、模型空间隔离与排名融合已用 fake provider 测试；不能据此宣称真实混合检索已验收。

## 用户命令和自然语言

```text
/查历史 咖啡
/查已清空历史 项目路径
/记忆
/记忆状态
/遗忘记忆 mi_真实编号
/遗忘记忆 pref:email_signature
/会话记录
/删除会话记录 ms_真实编号 预览版本
```

自然语言可说“记住我喜欢咖啡”“查上周讨论的项目路径”“以前的邮件签名是什么”。模型工具有 `search_history`、`read_history_evidence`、`list_memories`、`get_memory`、`save_memory`、`update_memory`、`forget_memory`、`get_preference_history`、`exclude_note_from_memory`。

跨会话读取必须符合本轮用户的回溯请求；参数中不能传 owner。检索旧周期还须明确要求“已清空历史/清空前”。“前几天”先搜索最近七天并说明实际范围，不把模糊表达伪造成精确日期。时间使用用户时区转 UTC；旧轮次原始时间未知时保留未知。

`/会话记录` 返回的是指定周期的删除预览。其后若又有其他消息或状态改变，旧预览失效，需重新预览，不能用过期范围删除新增记录。

## 清空、遗忘与副本范围

| 操作 | 归档与上下文 | 业务数据 |
|---|---|---|
| `/清空对话` | 新 epoch；旧周期不参与任何聊天的自动召回，明确历史查询仍可访问 | 偏好、笔记、提醒、邮件、任务保留 |
| 遗忘长期事实/决定 | 清理同一逻辑键的全部历史版本，来源交互组停止使用，派生记录失效 | 提醒、邮件等不因此取消 |
| 遗忘 `pref:键` | 同步删除 preferences 权威值及其历史正文 | 其他偏好保留 |
| 停止笔记用于记忆 | 持久排除索引和自动读取；后续重建不能收回 | 原笔记保留，可用笔记命令直接查看 |
| `/删除笔记 编号` | 失效相关事件、摘要、分块、向量及已关联回执副本 | 原笔记删除 |
| 删除指定会话周期 | 清理其事件、payload、对应 messages_v2 正文；保留去重身份和投递状态 | 长期记忆、笔记、邮件、任务保留 |

副本清单：`memory_events/payloads`、`memory_source_revisions/items`、`memory_summaries/segments/embeddings`、`turns` 缓存、关联的 `messages_v2.text/reply` 与 `effects.result`。effects 保留最小“已处理”回执，避免删除记录引发重复写操作。来源被读取后的转述也沿引用关系失效。summary 按**整个读取区间**失效，不只按模型主动引用失效。

旧资料若无法可靠对应原消息和工具，标记 partial/legacy_final_only。发生来源删除时，这些无法判定依赖的旧归档会保守退出记忆读取；不会编造关联，也不会任意删除其他业务表。无法确定归属的旧原文副本、业务邮件/任务产物、本机 Codex 会话和工作目录不在“遗忘一条记忆”的自动擦除承诺内。

删除在应用读取边界立即生效，SQLite/WAL/磁盘或备份的物理不可恢复不作保证。已向模型或本机发送的数据也无法撤回。当前没有自动备份轮换和备份介质擦除；运维方必须为备份另设明确期限，不能宣称已即时销毁备份。

## 配置与预算

开关见 `.env.example`：archive_write/context_v2/history_search/long_term_memory 默认 true；summary/semantic_search 默认 false。生产配置仍由现有启动流程加载，本轮没有读取或修改真实 `.env`。

每次请求计算 messages、工具 schema、输出预留和安全余量；工具返回后再次计算。`MEMORY_APP_TOKEN_LIMIT=64000` 是应用成本上限，**不是官方模型容量**。`MEMORY_CONTEXT_LIMIT=0` 表示官方容量尚未确认。若配置了已确认容量，取二者较小值。输出默认 2500，安全余量至少应用上限的 5%。

配置非零供应商容量时，`MEMORY_MODEL_ID` 必须与当前 DeepSeek 模型完全匹配，避免切换模型后继续沿用旧容量。

当前以 UTF-8 字节数和协议余量保守估算，`count_quality=estimated`。可注入经过适配验证的计数器；不能把其他模型 tokenizer 冒充准确计数。预算不足先移除旧轮次/摘要；当前问题、规则和明确偏好不能静默丢弃。长结果按字段预览，保留编号、结果状态、数量和全文句柄，JSON 不截断。仍放不下时停止新动作，提示拆分。

供应商明确返回 context_length_exceeded/context_window_exceeded 时，只缩小历史重试一次。保留本轮已经执行的工具及真实结果，绝不重跑整个请求。`memory_request_stats` 仅存来源清单、预算、耗时及 usage，不重复保存完整 prompt 或鉴权头。

精确 token 容量保证与真实模型成本基线尚未验收。当前不承诺特定成本下降或准确率。

## 摘要与派生任务

`MEMORY_SUMMARY=true` 后，已有 connected-worker 会消费 SQLite 派生任务，不增加常驻框架。摘要调用禁用工具，只读取退出近期窗口的完整轮次，保存范围、来源 revision/generation、模型与 schema 版本。数字/编号和动作状态须有对应证据；结构错误、模型失败、来源变更或并发修改不激活新摘要。原文不因摘要成功而立刻删除。

默认保留策略：普通归档 90 天，大 payload 7 天，显式记忆和笔记/偏好保留至修改、删除或有效期结束。到期来源同步失效派生内容；payload 到期会明确标记全文不可用。小时级维护由 connected-worker 驱动，也可在数据库副本上手工运行。删除日志只存标识，需保留到相关备份全部到期。

任务具有唯一键、租约、重试时间和错误类型，不保存异常中的私人正文。调用模型/embedding 在事务外；提交短事务再次检查 generation、来源版本、会话 revision 与租约。派生任务不执行任何业务工具。

EmbeddingProvider 接口为 `provider/model/version/dimension` 属性及 `embed(list[str]) -> list[list[float]]`。通过应用组合层显式传给 `MemoryJobs(..., embedding_provider=provider)`，并启用 semantic_search。没有默认远程服务，没有自动下载本地模型，也没有读取新凭证。替换 provider 后需排队 reindex；查询不混用不同空间分数。扫描受 scan_limit 限制，索引积压或超限时标记 coverage=partial。

## 迁移、备份和回滚

首次 `State` 初始化应用 `assistant/migrations` 的增量迁移，启用外键。旧表保留，旧 turns 与 messages_v2 分别导入并保留映射；不按相似文本猜测属于同一轮。旧偏好只有当前值，不能补造修改历史。导入可重跑，不生成虚假的旧工具记录。

以下命令仅示意，**本轮只在临时合成数据库验证过，没有对真实数据库执行**。维护入口不加载 `.env`，必须显式指定数据库；除 backup 外的入口会迁移指定库，应先对副本演练并在生产操作前停单实例。

```powershell
# SQLite backup API，支持 WAL；目标必须不存在。
.\.venv\Scripts\python.exe -m assistant.memory_admin --database COPY.sqlite3 backup --output BEFORE.sqlite3
.\.venv\Scripts\python.exe -m assistant.memory_admin --database COPY.sqlite3 migrate
.\.venv\Scripts\python.exe -m assistant.memory_admin --database COPY.sqlite3 stats
.\.venv\Scripts\python.exe -m assistant.memory_admin --database COPY.sqlite3 reindex
.\.venv\Scripts\python.exe -m assistant.memory_admin --database COPY.sqlite3 maintain
.\.venv\Scripts\python.exe -m assistant.memory_admin --database RESTORED.sqlite3 apply-deletions --ledger CURRENT-memory-deletions.jsonl
```

数据库目录的 `memory-deletions.jsonl` 是独立、仅追加的删除清单，在删除事务返回成功前 flush/fsync。不得与数据库一起回滚；须独立备份并限制文件权限。若删除事务随后失败，恢复时会保守应用排除。清单损坏时 State 初始化失败，修复前不允许读取。

恢复时：停止服务 → 在隔离目录恢复数据库 → 放入当前删除清单（或显式 apply-deletions）→ 验证 → 重建索引 → 启动单实例。State 返回前自动应用同目录清单。切勿在遗漏当前清单的情况下把旧备份直接当业务库启动。`backup` 不将当前清单冻结成可回滚副本；运维方须另行保存最新清单。

回退可关闭 context_v2/summary/semantic_search，仍使用公共可见性过滤。不能直接运行升级前的旧代码读原 turns，或恢复没有应用删除清单的旧库。archive_write=false 只停止新增归档；当前请求仍可处理，但不保证该期间的历史可追溯。保留已经写入的事件与墓碑。

## 本机 Codex 背景

`start_codex_task` 可选 evidence_id/evidence_version/evidence_scope。只有用户显式选择引用或明确要求使用相关背景时接收。队列只保存原始任务和证据引用；领取时再次检查来源可见性、版本、有效期及周期，构造有限背景包。失效资料不附带，任务标明缺项。原始任务约束不截断。

默认不附全部历史、其他聊天、邮箱正文或全部长期记忆。已领取后的本机产物独立保留，云端遗忘不代表远程擦除。本机执行、独立目录、parent_id 继续、取消、草稿 PR 审查及邮件显式发送边界保持原有语义。

## 验证与尚未完成的验收

工程用例位于 `tests/test_memory.py`，微信入口的归档断言位于 `tests/test_weixin.py`。均使用合成数据、临时数据库、fake 模型和 fake embedding。覆盖迁移重跑、六轮外查询、命令快照、并行工具协议、中断、预算、来源/身份隔离、冲突、删除/恢复、摘要并发和本机交接。

完整测试与离线演示的本轮结果见 `MEMORY-VALIDATION.md`。真实模型语义评测尚未运行；建议用 `memory-eval-cases.json` 的人工标注场景建立 Recall@K、引用正确率、澄清率、误写率和删除重现率基线，分别报告样本量、模型、配置、延迟和成本。不得用 fake 测试数替代模型准确率。

未完成：真实 tokenizer/供应商容量确认、真实摘要模型评测、真实 embedding 配置和混合检索质量基线、容器/生产数据库演练、真实渠道联调、备份轮换运维及部署。当前是本地实现，不是完整方案已上线。
