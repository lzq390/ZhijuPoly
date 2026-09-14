# OpenScience Luna 配置修复验收（2026-09-14）

本记录保留截至北京时间 **2026-09-14 15:03** 的配置修复验收，对应提交 `75f9e7d`。当时 Luna 普通对话、图片和辅助标题调用正常，Sol、DeepSeek 回归通过。配置合并及回退要求见[Backend 适配器维护说明](../deployment.md#openscience-backend-model-adapter-configuration)；本记录不是实时健康检查。

## 配置与版本身份

[合并片段](../../ops/config/openscience-luna-responses.merge.json)仅指定 Luna 的适配器为 `@ai-sdk/openai`，不含凭据，不能替换完整生产配置。使用生产实际依赖的 `jsonc-parser` 修改原配置副本所得字节，与已上线候选及复核时生产配置一致；网关、凭据、能力、模型限额、默认 Sol、辅助 Luna 及 medium/low 策略保持原值。部署前隔离实例的出站元数据确认 Luna 使用 `/v1/responses`。

| 对象 | 当时核验身份 |
|---|---|
| 原配置 SHA-256 | `984ba82fe9fc944a1859e83191f9ccfdc6f92cf6f673149b5a1e79428b81e737` |
| 生效配置 SHA-256 | `8646c6938b30b442804b61501dd45e62c80fd0cc118d9f4aae31a2bb6b16dabe` |
| 合并片段 SHA-256 | `278b1672ebd3048fe1d484e091e80993417002ea3095de14cad914c7e69f0989` |
| Backend 镜像 ID | `sha256:72f31605e271d810cccfc4bd9b3a2b32b475eb22d4a20e47df1ffc4cb39180cf` |
| UI 镜像 ID | `sha256:fd3102826a4fec93c18a91f41af6541c44c6752ce361b096bca4259b7d2f820a` |
| Backend 最后启动 | 2026-09-14 11:19:06，北京时间 |

本轮复核没有修改生产配置或重启服务。容器、镜像、挂载、部署记录及原 UI 代理配置摘要一致；9000/9011 健康检查通过，配置 inode、属主及权限保持不变。

## 功能验收

所有模型探针设置90秒上限，检查非空内容、实际模型、SSE增量及正常结束，不以 HTTP 200 单独判断成功。使用专用会话并禁止业务工具执行；返回内容仅保留长度和摘要。

| 验证 | 当轮结果 |
|---|---|
| Luna 连续两轮 | 正确读取前一轮标记，两轮均有 SSE 增量及 `finish=stop` |
| 自动标题 | 未预设标题的新会话自动生成并保存非空标题，辅助模型仍为 Luna |
| Sol 切换 Luna | 同一会话切换后正确续接上下文，两次均正常流式结束 |
| DeepSeek | 非空回答、正确模型及正常流式结束 |
| 浏览器单图 | 生产9011页面实际发送 Luna 图片请求，返回模型正确，图片与回复保存成功 |
| 刷新后历史 | 消息ID、内容摘要与渲染文本一致，图片预览加载成功，无页面脚本错误 |

本轮生成并归档4个专用测试会话。完整只读工具往返及后续对话在部署的隔离验收中通过，本轮未重复调用生产业务工具。刷新后输入框仍默认 Sol；历史回答的模型由发送请求与保存消息中的 `modelID` 核验为 Luna。

## 已知日志与覆盖边界

截至15:03，复核读取上线以来的完整当前应用日志及15条监控采样；最后一次定时采样为14:20，另做15:03即时运行与日志检查。未发现模型400、流式或标题失败、配置／镜像漂移、异常重启与入口故障。

日志保留5条 `Provider does not exist in model list synsci`：11:19两条，12:20、13:20、14:20各一条。对照当时镜像的 `backend/cli/src/provider/provider.ts:1201–1207`，目录加载器遇到未使用的内置提供方缺少模型目录时记录错误后继续。当时真实 Luna/Sol/DeepSeek 请求通过，因此判断这些目录日志非阻断；4次监控 `attention` 原样保留，不改成全绿。

截至本记录截止时，12小时本地监控尚未完成，预计北京时间23:20:40结束；本记录不覆盖最终结束结果。其首小时每5分钟、之后每小时采样，不发外部通知，也不周期性发起收费模型请求。定时健康检查与真实调用验证分别记录。

首次部署窗口因浏览器断言直接比较原始 Markdown 与渲染正文而误报并触发配置回退，原会话和图片完整。修正断言并只读重放原会话后，第二次切换于11:19:32成功，用时57.03秒，随后观察至少15分钟；首次实际回退验证了 Sol/DeepSeek 恢复路径。上述失败和回退不因文档精简而移除。

## 私有证据定位

操作标识为 `openscience-luna-fix-20260914t023410z`，私有目录位置记为 `<runtime-root>/manual-operations/<operation-id>/`。其中 `OPERATIONS.md` 保存专用检查与回退命令，`IMPLEMENTATION-REPORT.md` 保存完整部署证据；这些文件及完整配置、会话、原始环境、截图不随仓库交付。

该目录内保留 `reviews/review-20260914t070015z/` 下的 `config-review.json`、`live-api-review.json`、`sse-metadata.json`、`runtime-and-log-review.json`，以及 `evidence/review-20260914t070015z-browser/` 下的浏览器报告／截图、`logs/monitor.jsonl` 和 `logs/anomalies.jsonl`。原逐轮复核全文仍可从 `75f9e7d` 的 Git 历史追溯。
