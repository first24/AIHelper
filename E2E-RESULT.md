# 飞书个人助理真实验收记录

日期：2026-11-10（Asia/Shanghai）

结果：完成飞书指令 → GitHub Actions → DeepSeek 代码修复 → 独立测试 → 草稿 PR → 飞书完成通知。

## 真实任务与证据

- 场景：订单金额 `19.99` 元打九折，原实现得到 `18.99`，应得到 `17.99`。
- 飞书入口：用户实际发送 `/修复 演示`，本机接收到私聊事件，授权匹配成功并回报任务编号。
- 首轮任务：`1bd95066cf3c68be`。执行器权限初始化失败，在模型修复开始前结束；[运行记录](https://github.com/first24/AIHelper/actions/runs/42840040745)。
- 修复运行器配置：使用独立 `aihelper` 低权限用户和工作区沙箱；保持独立验证及分支推送作业。
- 成功重试任务：`536581d7033e02c9`，继续处理原用户的同一修复请求。
- [成功的修复工作流](https://github.com/first24/AIHelper/actions/runs/42840686176)：DeepSeek 修复、业务回归、助理测试、分支推送均成功。
- [草稿 PR #1](https://github.com/first24/AIHelper/pull/1)：`fix: 修复订单折扣金额计算`。
- 修复提交：`0bd59bf52d0cfee552eb3701ba849afc1405a551`。
- [PR 独立 CI](https://github.com/first24/AIHelper/actions/runs/42840906382)：成功。
- 本机任务状态：`done`，完成通知标记 `notified=1`；仅在飞书发送接口返回成功后设置该标记。

## 修改及验证

PR 仅修改 `demo_shop/pricing.py`，将折扣金额计算替换为：

```python
return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
```

保留负数价格和折扣范围验证。5 项业务回归测试及 15 项助理服务测试全部通过，PR 自身的 CI 也通过。

模型：`deepseek-flash`。执行器通过 DeepSeek 官方 Responses 接口调用模型，读取仓库 Secret `DEEPSEEK_API_KEY`；本记录不包含任何密钥。

## 使用状态

PR 保持草稿，尚未合并，等待用户审查。main 中刻意保留演示 bug，便于在合并前演示；合并后同一 bug 已修复，再发相同请求可能不产生变更。

助理当前运行于本机，需要电脑及进程保持在线。可使用项目中的 `start.ps1 -Mode run` 重新启动；不要同时运行多个实例。独立部署时保留本机 `.env` 和 `data/jobs.sqlite3`，两者都不提交到 Git。
