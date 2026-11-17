# 记忆升级发布记录

日期：2026-11-17，Asia/Shanghai。此记录补充本地开发验证报告，不代表真实模型语义评测已经完成。

## 发布结果

- 应用提交：`bdd5da1b2d519a40af78d765cfd322169117e3b1`，已推送到 `origin/feature/local-codex-bridge`；未合并 main。
- 部署目标：既有 Ubuntu 服务器 `120.53.24.130`，项目目录 `/home/ubuntu/aihelper-v2`。
- 容器：`aihelper-v2-assistant-1`，镜像 `aihelper-memory:bdd5da1-runtime3.1.1`。
- 镜像 ID：`sha256:64d27db942f542b45350db1d294b84680323ab7d004282ebc5bd94c5e27bc518`。
- 启动时间：2026-11-17 12:44:49（Asia/Shanghai）。复查为 healthy，健康心跳新鲜，飞书 connected、微信 connected，三个工作线程存活。
- 已部署容器中全部 32 个应用 Python 文件的哈希与应用提交完全一致。本文和后续说明文档的提交不会改变应用镜像内容。

## 构建与验证

发布前生产应用源码哈希与旧基线 `f83d619` 一致，旧镜像为 `aihelper-codex:3.1.1`。标准 Dockerfile 的全新依赖构建尝试遇到 PyPI 下载超时，且会引入新的飞书 SDK；该候选未用于生产。

实际发布沿用已健康运行的基础镜像，只替换应用源码。服务器发布目录中保留 `Dockerfile.release`：

```dockerfile
FROM aihelper-codex:3.1.1
COPY --chown=assistant:assistant assistant/ /app/assistant/
LABEL org.opencontainers.image.revision="bdd5da1b2d519a40af78d765cfd322169117e3b1"
LABEL org.opencontainers.image.base.name="aihelper-codex:3.1.1"
```

基础镜像 ID 为 `sha256:bac13938475a854c5b26a68f3e6cb46b05e1048f6c6eb13a00518c3a255522eb`。实际运行时为 Python 3.12.16、lark-oapi 1.7.5，未顺带升级 SDK。重建该镜像需要保留此基础镜像；不能声称标准 Dockerfile 从空缓存重建已通过。

| 检查 | 本次结果 |
|---|---|
| 本地提交前回归 | 138 项通过，Python 3.14.7；真实网络连接/DNS 被 mock 阻断 |
| 最终发布镜像回归 | 138 项通过，Python 3.12.16，0.971 秒 |
| 镜像离线演示 | 通过，real_model/real_feishu/real_email 均 false |
| 容器限制 | 非 root、只读根目录、cap_drop=ALL、no-new-privileges；测试容器 network=none |
| 生产备份副本迁移 | 原业务表行数保持一致、quick_check=ok、foreign_key_check 无错误、重复打开不重复导入 |
| 正式迁移 | 原业务表行数保持一致、quick_check=ok、外键错误 0，导入 75 个有完整度标记的历史事件 |
| 部署后后台派生任务 | 32 项 done，无错误；形成 92 个可用检索分块 |
| 实例状态 | 单个原名容器 healthy；飞书/微信连接正常 |

生产副本迁移和部署后的检查只输出状态、计数和源码哈希。未把数据库、凭证、聊天正文或账号文件下载到本机，未发送测试聊天、邮件、提醒或 Codex 任务。

## 生效开关

正式 `compose.override.yaml` 指定版本镜像，并明确设置：

```text
archive_write=true
context_v2=true
history_search=true
long_term_memory=true
summary=false
semantic_search=false
```

应用预算为 64000，输出预留 2500，初始安全余量配置 1600（计算时至少为应用容量的 5%），官方容量配置为 0，表示未确认。计数质量仍为 estimated。

本次上线的是事件归档、有限近期上下文、显式长期记忆和关键词历史检索。摘要和真实语义检索没有启用；不能把健康检查、连接状态或工程测试当作真实模型准确率。

## 备份、配置和回退

- 生产命名卷仍为 `aihelper-v2_assistant-data`，挂载 `/data`；未删除或替换该卷。
- 在线预演备份：`/data/backups/memory-bdd5da1/preflight/before.sqlite3`。
- 停机后的最终备份：`/data/backups/memory-bdd5da1/final/before.sqlite3`。
- 最终备份目录另存迁移前行数和迁移结果。发布前没有删除清单；未来产生的 `/data/memory-deletions.jsonl` 必须独立保留，不随数据库回滚。
- 发布目录：`/home/ubuntu/aihelper-v2/releases/memory-bdd5da1`，包含提交归档、最终构建文件、迁移脚本和新旧 Compose 镜像覆盖文件。
- 旧覆盖文件：上述发布目录下 `compose.override.before.yaml`。实际 `.env`、微信/OAuth 文件和本机执行器配置未修改。

切换前已确认处理中的消息、发送中的邮件和运行中的 Codex 任务均为零。随后停止旧实例，用 SQLite backup API 做最终备份，在锁保护下迁移并校验，再通过 Compose 创建同名新实例。

上线后可能产生遗忘操作，**不能直接恢复旧数据库或启动不识别删除过滤的旧版本**。优先关闭新上下文/摘要/语义读取并保留新公共读取边界；恢复备份时先应用当前删除清单，再允许索引重建。具体步骤见 `MEMORY.md`。当前备份位于同一持久卷，尚不构成独立灾备，也未建立自动轮换或介质擦除策略。

## 尚未验收

真实聊天收发的完整用户故事、真实模型指代/回忆效果、精确 tokenizer 与官方容量、真实摘要质量、真实 embedding 与混合检索质量、长期成本和延迟基线仍待后续明确联调。`memory-eval-cases.json` 保持 not_run。
