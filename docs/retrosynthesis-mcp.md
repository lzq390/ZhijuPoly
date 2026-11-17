# 单体逆合成 MCP

逆合成通过 19111 的现有 Research 对话调用：自然语言准备草稿，参数板补充或修改目标结构，用户确认后创建持久任务，原会话查询并展示单步前体候选。

## 输入与参数

目标支持 SMILES、本地名称匹配、授权原生账号的个人结构库。名称未命中时提供 SMILES 或选择结构；多个命中必须选择。自然语言中的用途、性质不直接转换成猜测的分子。

异步目标必须是一个不含通配原子的完整分子，最多 8,000 字符、512 个模型输入 token；超限明确拒绝，不截断。结构库未启用时仍可使用 SMILES。

| 参数 | 默认 | 范围 |
|---|---|---|
| `target_role` | `auto` | auto / diamine / dianhydride / other |
| `num_return_sequences` | 5 | 1–10 |
| `num_beams` | 5 | 1–20，且不小于候选数量 |
| `max_new_tokens` | 128 | 16–256 |

角色是用途标签，不约束模型生成。新异步结果的 `target_role` 表示用户选择，`inferred_target_role` 表示实际结构检测结果。旧同步接口保持兼容。

## MCP 工具

| 工具 | 模型 | 页面 | 行为 |
|---|---|---|---|
| get_retrosynthesis_capabilities | 是 | 否 | 查询可用性、默认参数和功能边界 |
| resolve_retrosynthesis_target | 是 | 是 | 校验 SMILES、名称匹配、查询个人结构 |
| prepare_monomer_retrosynthesis | 是 | 否 | 创建草稿，可缺少目标 |
| get_retrosynthesis_draft | 是 | 是 | 读取草稿、版本、缺失项和回执 |
| update_retrosynthesis_draft | 否 | 是 | 校验并保存修改 |
| confirm_monomer_retrosynthesis | 否 | 是 | 冻结版本，提交一次 |
| cancel_retrosynthesis_draft | 否 | 是 | 取消尚未提交的草稿 |
| get_retrosynthesis_job | 是 | 否 | 查询会话绑定任务，最长等待 20 秒 |

准备、解析、修改、取消草稿不调用逆合成模型。确认只能通过页面凭据执行。会话 ID 由服务端注入，浏览器不持有上游令牌。启用全部能力后，网关模型侧 19 工具、页面侧 9 工具；关闭逆合成时保持 14/4。

## 原生接口与持久化

原有 `POST /api/v1/monomer-retrosynthesis` 保持同步语义。新增：

- `GET /api/v1/monomer-retrosynthesis/status`
- `POST /api/v1/monomer-retrosynthesis/targets/resolve`
- `POST /api/v1/monomer-retrosynthesis/jobs`，必需 `Idempotency-Key`
- `GET /api/v1/monomer-retrosynthesis/jobs/lookup?idempotency_key=…`
- `GET /api/v1/monomer-retrosynthesis/jobs/{job_id}?wait_seconds=0`

任务及结果存入业务 PostgreSQL 的 `retrosynthesis.jobs`，强制 RLS。相同账号与幂等键只能关联一个请求；同键不同内容返回 409，重复请求返回原任务。草稿 SQLite journal 保存交互与提交身份，不能替代原生任务结果。

任务状态为 `pending → queued → running → completed/failed`。排队超时为 `timed_out`；能证明未执行或已终止的异常为 `interrupted`；曾开始但缺少终止证据的任务为 `unknown`。

重启不重新执行旧任务。未开始的任务经数据库条件更新阻止旧执行器启动后中断；已开始任务保留额度预留并等待核验。`unknown` 存在时任务服务停止接收新任务，读取仍可用。原执行器提交可信完成证据后才能对账；时间经过、HTTP 超时或页面关闭均不能证明推理停止。

单后台进程通过数据库 advisory lock 持有执行权，继续使用现有 GPU registry。数据库保存失败时只重试保存结果，不重新推理。部署排空 v5 同时统计持久任务和内存 Future。

默认每账号最多 1,000 条、128 MiB，单任务请求及结果预算 1 MiB；在途任务预留空间。一期不自动删除幂等记录，满额后拒绝新任务。

用量沿用 `retrosynthesis / chemistry_small` 策略。空结果是否计费取决于当前政策的 `chemistry_no_match_counts`；不要把空结果固定解释为免费。额度关闭时仍持久保存任务和结果。

## 独立开发服务

原生后端需配置 `RETRO_MODEL_ENABLED=true`、`RETRO_JOBS_ENABLED=true`，并具备本地模型、兼容数据库迁移和原有 GPU 授权。`RETRO_JOBS_ENABLED` 默认关闭。

```bash
python3 scripts/retrosynthesis_mcp_dev.py setup --account-uuid ACCOUNT_UUID
python3 scripts/retrosynthesis_mcp_dev.py login --username USERNAME
python3 scripts/retrosynthesis_mcp_dev.py up
python3 scripts/retrosynthesis_mcp_dev.py smoke
```

密码使用隐藏交互输入，不保存在脚本参数或配置中。已有同账号会话可通过 `import-session --session-source PATH` 导入；脚本检查文件权限、固定身份、到期时间并向原生后端重新验证。

MCP 只在容器内 18211 端口监听，不发布主机端口，容器通过独立内部网络连接网关。只读 smoke 使用容器内 RPC，不提交任务。

在现有 19111 部署 checkout 中，先停止独立开发入口，再准备逆合成配置：

```bash
python3 scripts/openscience_mcp_dev.py down
python3 scripts/openscience_mcp_dev.py setup --retrosynthesis-runtime /ABSOLUTE/PRIVATE/RETROSYNTHESIS_RUNTIME
python3 scripts/openscience_mcp_dev.py up
python3 scripts/openscience_mcp_dev.py smoke
python3 scripts/openscience_mcp_dev.py verify-protected
```

运行目录及 Compose 项目归属必须匹配。保留原会话、文件和日志；不能从另一个工作树直接接管现有项目。

## 迁移与回退

0026 为受控 expand 迁移，不随应用启动执行。部署兼容后端、排空任务并备份后，使用管理员环境执行：

```bash
python -m app.retrosynthesis_migrations
python -m app.retrosynthesis_migrations --apply
```

默认预览；只接受完整、校验和一致的 0025 前缀或已经应用的 0026，不代办其他迁移。API、service、备份、RLS 和 preflight 都识别新增账本；显式检查目标为 `retrosynthesis-0026`。

回退先停止 19111 开发入口，再执行 `openscience_mcp_dev.py setup --disable-retrosynthesis` 并重新启动，使工具清单恢复 14/4；关闭原生新任务提交，保留兼容后端、新表、已接收任务和草稿 journal。不要删除迁移或运行旧版本不认识的账本，不自动退款或重新提交状态待核验任务。

## 验证与科学边界

```bash
python3 scripts/test_retrosynthesis_mcp.py --python /PATH/TO/LOCKED/VENV/bin/python
python3 -m unittest scripts.tests.test_retrosynthesis_mcp_dev
```

真实 MCP 与浏览器验收分别使用 `scripts/verify_retrosynthesis_mcp.py`、`scripts/verify_openscience_retrosynthesis_gateway_browser.mjs`；查看各脚本 `--help`。真实提交需要明确运行验证命令，普通 smoke 不提交。

测试覆盖并发幂等、账号撤权、RLS、额度开关、提交异常、结果保存重试、重启、页面刷新、可信回执及窄屏布局。GitHub CI 的模拟推理与开发环境真实模型验收分别记录。

结果是单步前体候选，不提供完整多步路线、实验条件、产率、采购可得性或经校准的置信度。候选排名保留模型顺序；无效候选明确标记。耗时报告将排队及加载与实际推理分开，无法独立观测的阶段不编造数值。
