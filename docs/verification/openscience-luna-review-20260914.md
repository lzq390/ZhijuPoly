# OpenScience Luna 生产修复复核

北京时间 2026-09-14 15:03 完成复核：未发现阻断本次配置修复提交的问题。生产 Luna 的普通对话、图片和辅助标题调用正常，Sol、DeepSeek 回归通过。

## 提交内容与生产状态

`ops/config/openscience-luna-responses.merge.json` 保存本次不含凭据的嵌套合并片段，唯一叶子字段为：

```text
provider.lab-openai-compatible.models.gpt-5.6-luna.provider.npm = "@ai-sdk/openai"
```

该文件不是完整的 OpenScience 配置，不能直接替换生产配置。应用时使用 JSONC 编辑器把上述字段合入既有模型对象，并保留原文件的注释和其他配置；不要对整个 `provider` 对象作浅层覆盖。

已用生产实际使用的 `jsonc-parser` 对原配置副本执行同样的修改，生成字节与已上线候选配置完全一致；实际生产配置也与候选字节一致。网关、凭据、能力声明、模型限额、默认 Sol 和辅助 Luna 的选择保持原值。普通调用 `medium`、辅助调用 `low` 的既有策略保留；部署前隔离实例的出站元数据已验证 Luna 使用 `/v1/responses` 及这两种参数。

生产配置位于私有运行目录，不由此 Git 仓库跟踪。本次提交保留可审查的配置片段与验证记录，完整配置、会话数据、原始环境和截图保留在私有操作目录。

| 对象 | 已核验身份 |
|---|---|
| 原配置 SHA-256 | `984ba82fe9fc944a1859e83191f9ccfdc6f92cf6f673149b5a1e79428b81e737` |
| 生产配置 SHA-256 | `8646c6938b30b442804b61501dd45e62c80fd0cc118d9f4aae31a2bb6b16dabe` |
| 提交的合并片段 SHA-256 | `278b1672ebd3048fe1d484e091e80993417002ea3095de14cad914c7e69f0989` |
| OpenScience Backend 镜像 ID | `sha256:72f31605e271d810cccfc4bd9b3a2b32b475eb22d4a20e47df1ffc4cb39180cf` |
| OpenScience UI 镜像 ID | `sha256:fd3102826a4fec93c18a91f41af6541c44c6752ce361b096bca4259b7d2f820a` |
| 生效 Luna 适配器 | `@ai-sdk/openai` |
| Backend 最后启动时间 | 2026-09-14 11:19:06，北京时间 |

此次复核没有修改生产配置或重启服务。Backend/UI 及 NexPoly 的容器、镜像、挂载与部署记录一致，原 UI 代理配置摘要一致；9000/9011 健康检查通过。生产配置 inode、属主、权限保持 `18225934`、`1001:1001`、`0640`。

## 本轮实际调用

所有模型探针设置 90 秒上限，检查响应内容、实际返回模型、流式增量与正常结束，不以 HTTP 200 单独判定成功。探针使用专用会话、禁止业务工具执行，返回内容仅记录长度与摘要。

| 验证 | 结果 |
|---|---|
| Luna 连续两轮 | 3.71 秒、3.40 秒；第二轮正确读取前一轮标记，均有 SSE 增量及 `finish=stop` |
| Luna 自动标题 | 新建会话未预设标题，自动生成非空标题并保存；辅助模型配置仍为 Luna |
| Sol 切换 Luna | Sol 回答 7.88 秒；同一会话切 Luna 后 3.34 秒正确续接上下文，均正常流式结束 |
| DeepSeek 回归 | 1.31 秒，非空回答、正确模型及正常流式结束 |
| 浏览器单图 | 生产 9011 页面实际发送 Luna 图片请求，返回模型为 Luna；图片和回复保存成功 |
| 刷新后历史 | 消息 ID、原始内容摘要与渲染文本核验通过，图片预览加载成功，无页面脚本错误 |

本轮共生成并归档 4 个专用测试会话。完整只读工具往返及工具后续对话已在本次部署的隔离验收中通过，此轮未重复调用生产业务工具。

刷新后的输入框沿用默认 Sol，历史回答的实际模型通过发送请求和保存消息中的 `modelID` 核验为 Luna。

## 日志结论与限制

截至北京时间 15:03，已读取上线以来的完整当前应用日志及 15 条监控采样。监控最后一次定时采样为 14:20，复核另做了 15:03 的即时运行与日志检查。未发现模型 400、流式失败、标题生成失败、镜像或配置漂移、异常重启、入口故障。

应用日志共 5 条 `Provider does not exist in model list synsci`：11:19 的两条及 12:20、13:20、14:20 各一条。已对照当前镜像的 `backend/cli/src/provider/provider.ts:1201–1207`：目录加载器发现未使用的内置提供方不在模型目录时记录错误并继续。这些记录属于非阻断的目录日志，真实 Luna/Sol/DeepSeek 请求复测通过；4 次自动监控 `attention` 原样保留，不改写为全绿。

12 小时本地监控仍在运行，计划于北京时间 **2026-09-14 23:20:40** 自行结束。首小时每 5 分钟、其后每小时采样，不发送外部通知。定时健康检查与本轮真实调用结果分别记录；监控不周期性发起收费模型请求。本结论覆盖已检查时段和上述功能探针。

## 切换历史与回退

部署首次窗口因浏览器脚本把原始 Markdown 与渲染正文直接比较而误报，控制器执行了配置回退。原会话及图片完整；修正验收断言、只读重放原会话通过后，第二次切换于北京时间 11:19:32 成功，用时 57.03 秒，随后完成至少 15 分钟观察。

生产配置为单文件挂载；回退必须在确认业务空闲并维护入口后，停止 Backend、原位恢复配置字节并 `fsync`、启动原容器，保留 inode、属主和权限。回退仅恢复本次配置和入口，不覆盖会话、业务文件或数据库。首次实际回退已经验证 Sol/DeepSeek 恢复路径。

私有操作目录：

```text
/data/lzq/gith/nexpoly-runtime/manual-operations/openscience-luna-fix-20260914t023410z
```

该目录中的 `OPERATIONS.md` 提供本次专用检查和回退命令，`IMPLEMENTATION-REPORT.md` 保留完整部署证据。此次复核证据位于：

```text
reviews/review-20260914t070015z/config-review.json
reviews/review-20260914t070015z/live-api-review.json
reviews/review-20260914t070015z/sse-metadata.json
reviews/review-20260914t070015z/runtime-and-log-review.json
evidence/review-20260914t070015z-browser/report.json
evidence/review-20260914t070015z-browser/reply.png
evidence/review-20260914t070015z-browser/reloaded.png
logs/monitor.jsonl
logs/anomalies.jsonl
```
