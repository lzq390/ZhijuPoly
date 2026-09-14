# 浏览总结：正式集成、部署与 POC 开发环境

## 当前交付状态

正式后端集成（2026-09-11）：`app.main` 已初始化 `BrowsingRecordingStore` 并注册观察/记录/总结路由。知识库搜索与属性筛选的正式路由返回 `search_id`，可接收可选 `recording_id`；原 SQL 查询和筛选响应头保留。轻量入口 `app.knowledge_poc:create_app` 复用同一套正式路由与记录服务，仅保留精简模块和开发观察日志。

共用代码位于 `backend/app/services/browsing_recording.py`、`backend/app/routers/browsing_recording.py` 和 `backend/app/recording_models.py`；总结生成继续复用 `knowledge_poc_summary.py`，历史文件名不代表只供 POC 调用。正式入口不启用 POC 原始观察日志。无新增依赖或数据库迁移，模型沿用已有 `ASSISTANT_*` 配置。

功能已完成正式后端集成和 9021 页面验收，当前进入 PR 收尾。采集范围为本地知识库检索/文献阅读/反应信息，以及数据库筛选/测量详情/SMILES；公共按钮支持跨模块记录、三段式总结和面板收起后重新查看。PR 收尾已移除早期 POC 的 `adad` 隐藏入口，当前通过“开始记录”或“开始新记录”按钮开启。

本阶段沿用内存记录：同一记录的请求必须到达同一进程，最多 32 条记录、每条 200 个事件；刷新不恢复前端状态，后端重启清空记录。持久化、多进程共享和容量升级留待后续；用户与行为隔离由另一开发线负责，本 PR 不新增其设计与实现。

## 正式部署要求

使用现有正式前后端 Dockerfile 和发布流程配套更新镜像，后端入口仍为 `app.main:app`。本 PR 不新增数据库 schema、迁移、数据回填、数据库权限、生产依赖、GPU 需求、Redis 或持久化卷，也不要求新增公网端口。

| 配置 | 用途与继承关系 |
|---|---|
| `APP_POSTGRES_DSN` | 复用现有知识库和筛选数据库连接；总结功能不单独连接数据库。 |
| `ASSISTANT_BASE_URL` | 复用已有模型服务地址，需支持在该地址后追加 `/chat/completions`；未显式配置时沿用已有 `ONLINE_KNOWLEDGE_BASE_URL` 回退规则。 |
| `ASSISTANT_API_KEY` | 复用已有模型密钥；未显式配置时沿用已有 `ONLINE_KNOWLEDGE_API_KEY` 回退规则。 |
| `ASSISTANT_MODEL` | 指定服务支持的模型。当前配置代码缺省为 `gpt-5.5`，不会自动继承 `ONLINE_KNOWLEDGE_MODEL`；部署时应核对模型可用性。 |
| `AI_PROXY_URL` | 可选的已有代理配置；当前仍兼容 `ONLINE_KNOWLEDGE_PROXY_URL` 回退，两者均为空时直接访问模型服务。 |

正式 Compose 从 `NEXPOLY_APP_ENV_FILE` 指定的文件注入模型等环境变量；数据库 DSN 继续使用正式 Compose 现有变量注入方式。轻量开发入口可读取 `backend/.env`。环境文件使用 `KEY=value` 格式，权限设为 `600`；密钥和完整连接串不进入 Git、文档或前端。容器中的数据库主机名必须在该实例所在网络内可解析。

现阶段使用单实例、单 worker；仓库现有 Docker 启动命令已指定 `--workers 1`，无需为本功能调整标准启动命令。模型请求默认超时为 60 秒，仓库现有 `/api/` 代理读取超时为 120 秒；存在其他入口代理时，需避免其提前截断总结请求。成功的总结按记录缓存，模型配置缺失或调用失败会显示错误并允许重试。

## 正式镜像验证与验收

公网镜像验证（2026-09-11）：使用原生 `ssh` / `scp` 连接 `4-4090`，工作区为用户指定的 `/home/devuser/gsx/Zhiju_poly`。同步 831 个源码文件并核对 SHA-256，不含 `.git`、密钥、运行数据、模型或依赖目录；未覆盖服务器现有开发工作区。

- 使用仓库原有 Dockerfile 构建正式后端及同源测试镜像 `zhiju-summary-backend:integration-20260911`、`zhiju-summary-backend-test:integration-20260911`。最终前端为 `zhiju-summary-web:http-start-fix-20260911`，包含 HTTP 兼容修复；以上镜像均构建成功，前端构建包含 TypeScript 检查。
- 最终测试容器使用新镜像内的应用代码，只读挂载测试文件，以 `--network none` 禁用网络；数据库和模型调用使用模拟实现，不运行应用 lifespan。共 39 项通过（6.06 秒，9 条现有依赖弃用警告）。前端镜像 `nginx -t` 通过。
- HTTP 首轮手测发现 `crypto.randomUUID` 不可用导致开始按钮无响应。已增加 `crypto.getRandomValues` 回退并将 ID 生成纳入异常处理；两个新增用例先复现错误，修复后与既有记录/按钮/跨模块测试共 13 项通过。无需新增前端依赖。
- 真实数据库连接和所需表的只读检查通过，模型合成输入测试正常返回（该次耗时 39.2 秒）。9021 的首页、健康、筛选选项、知识库搜索/阅读、属性筛选及开始/结束记录接口均返回 200，跨模块事件核对通过。
- 修复版前端更新后，用户完成手动页面测试并确认未再遇到问题。按用户要求未继续自动浏览器测试。本次验收覆盖上述两个采集模块和总结交互，不代表全平台科学计算、所有浏览器或负载场景均已回归；最终提交的 GitHub Actions 结果待推送后核对。
- 构建与测试日志位于该工作区 `.runtime/summary-integration/`，包括 `test-final-image.log`、`build-web-http-fix.log`、`check-config.log` 和 `check-database.log`。初始源码包及后续更新的文件清单位于同目录，均不进入 Git。

9021 的独立 Compose 文件为远端 `.runtime/summary-integration/compose.test.json`，项目名为 `zhiju_summary_integration`。后端接入既有开发数据库网络 `nexpoly_dev_default`，前后端另用独立网络；`backend` 别名仅配置在独立网络中，未改变 9001 服务。

经用户确认，测试实例通过 `PGOPTIONS=-c default_transaction_read_only=on` 使用只读数据库会话，关闭本次无关的科学计算及后台清理任务；启动前已验证 `transaction_read_only` 和 `default_transaction_read_only` 均为 `on`。正式后端原有启动逻辑会尝试更新过期 MD 任务，因此必须保留测试实例的只读保护，不能把服务启动当作纯只读探测。**这些只读、模块开关、网络及 9021 端口设置均为本次测试安排，不是整个平台的生产部署模板。**

## 轻量开发环境（POC 复现）

- SSH：`codex-lab`；目录：`/home/codexlab/DevTool/ZhijuPoly`。
- 虚拟机目录是本地 `feat/knowledge-summary-poc` 的源码快照，基线 `b2ea83b`；不包含 `.git`，不是独立 Git checkout。
- uv `0.12.10`，用户级安装在 `/home/codexlab/.local/bin/uv`；未修改系统 Python 或 Shell 启动文件。
- Python `3.11.16`；虚拟环境位于 `.runtime/knowledge-summary/.venv`，实测约 33 MB。
- `backend/requirements-knowledge-poc.in` 定义 7 个直接依赖，锁文件固定 21 个包及其哈希。

以下保留轻量环境、样本与观察工具的复现方法；历史阶段的用例数量和临时入口不代表当前部署状态。当前操作方式见下文“跨模块记录”和“页面模型总结”，最终验收以上文为准。

2026-09-09 的固定样本评测采用 v3 阅读回顾提示词；同一模型两次回放耗时 70.14/35.76 秒，属于当时的评测结果，不作为当前部署的模型配置或延迟承诺。来源限制、复现方法及后续模型对比见[提示词评测](summary-prompt-evaluation.md)。

## 使用与复现

在虚拟机项目根目录执行：

```bash
cd /home/codexlab/DevTool/ZhijuPoly
~/.local/bin/uv venv --python 3.11.16 .runtime/knowledge-summary/.venv
~/.local/bin/uv pip sync --python .runtime/knowledge-summary/.venv/bin/python \
  --require-hashes backend/requirements-knowledge-poc.lock
~/.local/bin/uv pip check --python .runtime/knowledge-summary/.venv/bin/python
source .runtime/knowledge-summary/.venv/bin/activate
```

已创建环境时跳过第一条 `uv venv`。其他机器先按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/) 安装；使用 `UV_NO_MODIFY_PATH=1` 可避免安装器修改 Shell 启动文件。

需要更新依赖锁时，沿用仓库现有版本约束：

```bash
~/.local/bin/uv pip compile backend/requirements-knowledge-poc.in \
  --constraint backend/requirements.lock --constraint backend/requirements-ci.lock \
  --python .runtime/knowledge-summary/.venv/bin/python --generate-hashes \
  --output-file backend/requirements-knowledge-poc.lock
```

## 轻量后端测试 CI

`ci.yml` 的 `lightweight-backend-tests` 任务使用 Python 3.11 和现有 `backend/requirements-knowledge-poc.lock`，按哈希安装依赖，设置 `PYTHONPATH=backend` 并使用 pytest。该任务纳入 `ci-gate`，失败会阻止总检查通过。

- 收集 `scripts/tests/` 顶层的 `test_*_poc_*.py` 与 `test_lightweight_*.py`；前者兼容现有 POC 测试，后续通用轻量测试使用后者命名，无需逐个修改 CI。
- 原 unittest 任务排除相同两类文件；没有找到轻量测试时 CI 显式失败，避免空跑。正式集成后收集 7 个文件、38 项测试。完整 `app.main` 的 `backend/tests/test_browsing_recording.py` 由既有完整后端分片任务执行。
- 适用范围：可仅靠该锁文件运行、模拟数据库和模型调用的后端测试。需要 RDKit、GPU、真实数据库或外部模型凭据的测试继续进入对应专用任务，不因文件名迁入本任务。当前不启动数据库服务，不配置模型密钥。
- 仍复用 POC 锁文件，未新增或升级依赖。真实模型的耗时/质量评测脚本不进入 CI。

已有轻量环境时，在仓库根目录用 Bash 复现：

```bash
mapfile -t tests < <(
  find scripts/tests -maxdepth 1 -type f \
    \( -name 'test_*_poc_*.py' -o -name 'test_lightweight_*.py' \) -print | sort
)
PYTHONPATH=backend .runtime/knowledge-summary/.venv/bin/python -m pytest "${tests[@]}" -q
python3 -m unittest scripts.tests.test_validate_workflows
python3 scripts/ci/validate_workflows.py
```

2026-09-11 验证：在 codex-lab 的临时源码副本中，不复制 `.env`，用全新 Python 3.11 环境按哈希安装锁定的 21 个包（本地验证通过 uv 离线缓存安装，CI 使用 pip），确认无 RDKit/Torch/NumPy；执行工作流中的测试命令，35 项通过。工作流策略回归 73 项通过，actionlint v1.7.7、工作流策略与依赖锁校验通过。GitHub Actions 云端运行仍待推送后的结果。

## 独立 POC 数据库

- Compose 文件：`docker-compose.knowledge-poc.yml`，项目名 `zhijupoly_knowledge_poc`；独立使用，不与全平台 Compose 文件叠加。
- 实测 PostgreSQL `16.14`，镜像 digest 沿用仓库已有 PostgreSQL 16 镜像。
- 虚拟机地址：`127.0.0.1:15432`；库名及用户：`knowledge_poc`。
- 数据卷：`zhijupoly_knowledge_poc_postgres_data`；容器名：`zhijupoly_knowledge_poc-postgres-1`。
- 密码保存在虚拟机 `.runtime/knowledge-summary/postgres-password`；完整连接配置保存在同目录 `database.env`，两者权限均为 `0600`，不进入 Git。
- 知识库阶段创建 `knowledge.documents`、`pg_trgm` 和对应索引；后续筛选阶段新增的表见下节。沿用现有迁移中的 SQL，不修改迁移源文件，也不标记全平台迁移已经应用。
- 共有 11 个索引（包含主键、唯一约束）；原迁移两次声明同一 formulation 索引，因此 10 条普通索引语句实际创建 9 个普通索引。

用户提供的两份 PDF 保留在虚拟机项目根目录。当前每篇一行，只录入英文标题、摘要与来源；摘要来自 PDF 第 1 页，仅整理 Unicode、空白及换行断词。`source_row_number=1` 在这两个样本中表示 PDF 页码；中文标题及实验字段未提取，保持空值。

| ID | 样本主题 | 可复现检索词 |
|---|---|---|
| 1 | 无色透明聚酰亚胺薄膜、柔性显示与热稳定性 | `polyimide`、`thermal` |
| 2 | 两步连续反应的 pH–速率曲线与吸光度极值法 | `absorbance`、`catalyst` |

样本 JSON、来源 SHA-256、提取的 SQL 及本次初始化/验证脚本保存在虚拟机 `.runtime/knowledge-summary/`。PDF 提取使用 uv 临时环境中的 pypdf 与 fonttools，未加入后端依赖。运行材料依赖用户样本，不随源码快照自动提供。

在虚拟机项目根目录管理和验证现有数据库：

```bash
docker compose -p zhijupoly_knowledge_poc -f docker-compose.knowledge-poc.yml config --quiet
docker compose -p zhijupoly_knowledge_poc -f docker-compose.knowledge-poc.yml up -d --wait postgres
docker compose -p zhijupoly_knowledge_poc -f docker-compose.knowledge-poc.yml ps
.runtime/knowledge-summary/.venv/bin/python -B .runtime/knowledge-summary/load-verify-knowledge-poc.py
```

最后一条命令默认只读，通过现有 FastAPI 知识库路由连接真实数据库进行测试，不启动网络 API 服务。`--init` 用于本次独立样本初始化，不是通用导入器。

## 数据库筛选演示样本

本节记录独立开发库的样本准备与早期 Hook 验证过程；正式筛选事件已接入统一记录和总结，并已由用户完成页面验收。

在同一独立开发库中新增 `core.polymer_property_filter_records`、`governance.source_files`、`governance.import_batches` 和 `governance.property_filter_options_snapshots`，复用现有迁移 0001 的必要建表语句及 0006、0015。未执行全平台迁移。

样本在 `scripts/fixtures/property_filter_poc.csv`，共 4 个材料、13 条属性记录。全部为合成演示数据，名称含“演示材料”，记录来源/质量标记明确说明，不与已有两篇文献关联。SMILES 是演示结构字符串，未运行 RDKit 标准化；可靠度和单位转换结果保持未提供。

| 材料 | Tg（C） | 拉伸强度（MPa） | 断裂伸长率（%，原始属性） |
|---|---|---|---|
| POC-A | 180、190（两条记录） | 60 | 15 |
| POC-B | 240 | 90 | 8 |
| POC-C | 300 | 110 | 3 |
| POC-D | 120 | 40 | 25 |

在虚拟机根目录复现：

```bash
.runtime/knowledge-summary/.venv/bin/python -B scripts/seed_property_filter_poc.py
# 首次初始化；重复执行不重复插入样本，不覆盖不同内容的同 ID 记录
.runtime/knowledge-summary/.venv/bin/python -B scripts/seed_property_filter_poc.py --init
```

脚本只从 `.runtime/knowledge-summary/database.env` 读取 APP_POSTGRES_DSN，并校验目标为既定的 `127.0.0.1:15432/knowledge_poc` 开发库。默认只读；初始化在单个事务内完成，冲突回滚。

轻量入口复用 `/api/v1/database-browser/property-filter/{options,histogram,search}`；RDKit 仅在其它结构计算函数被调用时导入，未增加环境依赖。仍只需转发 5173，通过侧栏“数据库筛选”进入。

手动验收：Tg 最小值 200 → B/C；增加拉伸强度最小值 100 → C；Tg 最小值 400 → 无结果；仅使用 Tg 最小值 100、关键词 POC-A → 一个材料，展开记录详情可见 180/190 两条测量。原始属性断裂伸长率最小值 10 → A/D。

样本准备阶段验证：新增入口测试先失败后通过，POC 后端 25 项测试通过；经 5173 代理的 11 组真实检查通过，涵盖目录/缓存、全部属性分布、上述筛选场景、分页/越界、非法范围及已有知识库检索。重复初始化仍为 4 个材料、13 条记录。该阶段未改前端，筛选操作当时尚未接入记录与总结，后续接线及验收已完成。

用户随后确认页面可正常筛选。现已补充筛选日志：`KNOWLEDGE_TRACE` 对 search 接口记录条件、关键词、分页、返回材料和测量字段白名单，复用下方监听脚本，无需 adad。请求/响应分别最多捕获 64 KiB；超限标记 `truncated`，正文可能无法解析，不能将缺失内容当作零结果。原请求/响应保持不变。27 项 POC 回归及真实代理日志检查通过；自动测试请求 ID 和用户观察起点保存在虚拟机 `.runtime/knowledge-summary/last-observation-check.json`。

仅靠筛选请求日志只能证明后端返回，不能推断用户主动查看；自动目录/分布加载也不视为主动阅读。旧日志未保存的条件无法追溯恢复，需重新提交筛选观察。

用户随后授权的两个查看 Hook 已接线：

- search 响应包含 `search_id`；`POST /api/v1/database-browser/property-filter/observations` 接收 `search_id`、从零起的页内 `result_index` 和 `source`。旧版本后端未返回 ID 时前端不上报，原有筛选保持兼容。
- `source=measurement_details` 携带 `filter_index`，后端返回该材料在该条件下的全部测量，事件为 `property_filter.measurements_viewed`。
- `source=smiles` 携带 `smiles_field=smiles|canonical_smiles`，后端返回实际展开的字符串，事件为 `property_filter.smiles_viewed`。样本未做标准化，目前页面展示原始 SMILES。
- 内容由后端原筛选快照关联；前端不传测量值或结构正文。普通筛选缓存保留最近 32 次，活动记录另行保留自己的关联快照；淘汰/服务重载后失效的 ID 返回 410，不属于该结果或未提供的内容返回 404，非法通知返回 422。
- 原生 details 展开触发通知，支持浏览器原生鼠标/键盘展开；自动渲染、收起、重复 toggle 不通知，关闭再展开新增一次。上报失败不自动重试或阻止详情展示；当前开启记录后会使用统一 recording_id，失败时提示记录可能不完整。
- Hook 阶段验证：后端 32 项、前端相关 49 项与筛选页面 18 项测试通过，前端构建通过；真实 Vite 代理核对旧筛选关联、两条 Tg 测量、SMILES、错误关联/缺失内容拒绝及两种通知日志。用户随后完成页面操作验收。

测试方式：刷新并重新筛选 POC-A（Tg 最小值 100），展开“记录详情”和 SMILES，关闭再展开。无需 adad，通知自动写入同一个 backend.log，可继续用 `scripts/watch_knowledge_poc.sh` 观察。

## 直接开发运行与请求观察

- 前后端直接作为虚拟机进程运行：Vite `127.0.0.1:5173`（热更新），Uvicorn `127.0.0.1:8000`（`app.knowledge_poc:create_app --factory`，自动重载）。数据库保留独立容器。
- 用户自行转发 `5173` 后访问 `/knowledge` 即可；Vite 将 `/api` 代理到后端，不需要转发后端或数据库端口。
- 日志和进程号保存在 `.runtime/knowledge-summary/{backend.log,frontend.log,processes.json}`。修改虚拟机源码会触发重载；本地源码修改仍需同步到虚拟机。
- `KnowledgeTraceMiddleware` 仅接入 POC 入口，观察实际到达后端的 `/api/` 请求。搜索额外记录白名单查询字段、总数、文章 ID/标题、摘要长度及前 160 字符；不记录请求头、凭据或推断阅读行为。
- 正式和轻量入口的搜索响应均包含 `search_id`，与当前这批结果一同保存在前端。`useKnowledgeObservation` 在点击文章卡片及重新打开详情时调用 `POST /api/v1/knowledge/observations`，发送 `search_id`、`knowledge_id` 和 `source`（`result_card` / `drawer_reopen`）。
- 后端从该次结果快照核对文章，返回 `article.opened`、原查询、标题和摘要长度；开启记录后保存该次查看事件。前端不发送正文，自动展开第一条不产生通知；旧版本后端未返回 `search_id` 时也不发送通知。
- 主动切入“反应信息”页签时复用同一 Hook 与接口，`source=reaction_tab`，事件为 `article.reaction_viewed`。鼠标与键盘切换均支持；重复点击当前页签、自动重置页签不通知，切走再切回会再通知。关闭后重开且保留该页签时，仍只记录原有 `drawer_reopen`。
- 反应事件的 `reaction_info` 来自后端原检索快照，包含聚合物、配方、催化剂、溶剂、温度、时间、分析与判断理由；缺失字段保持 `null`。两篇当前样本均未提取这些字段，因此只能证明查看了该区域，不能据此总结出具体配方。
- 共用记录服务在进程内保留最近 32 次普通检索快照，活动记录另行保留自己的关联快照；重载或快照淘汰后旧 ID 返回 410，需要重新搜索，文章不属于该次结果返回 404。通知失败不阻止详情展示，也不自动重试；开启记录时额外提示记录可能不完整。
- 请求和响应分别最多额外保留 64 KiB 用于解析，超过后日志标记 `truncated`；客户端请求与响应照常传递。时间为 UTC，`request_id` 仅用于区分请求，不是后续正式记录会话 ID。

在虚拟机另开终端，从新请求开始监听：

```bash
cd /home/codexlab/DevTool/ZhijuPoly
bash scripts/watch_knowledge_poc.sh
```

手动验收顺序：刷新页面并重新搜索 `polyimide|absorbance` → 观察自动展开 → 主动切换两篇文章、关闭再打开详情 → 切入“反应信息”、重复点击、切走再切回 → 搜索 `polyimide` 后再点击文章及“反应信息”。每步停留约 2 秒，将动作与新日志对照。

预期搜索日志包含新的 `response.search_id`；主动打开/重开时新增 `/knowledge/observations` 日志，成功响应含 `event=article.opened`，且与当前结果的 `search_id` 对应。主动切入“反应信息”产生 `article.reaction_viewed` 和 `reaction_info`；自动展开、关闭详情、滚动及切入其他页签不发送通知。`Ctrl+C` 只停止终端查看；API 仍运行，日志仍写入 `backend.log`。

## 验证与限制

### 跨模块记录（当前入口）

- Provider 位于 App 根部，公共总结按钮位于 AppShell 内容区上方，页面切换不会卸载记录状态。在本地知识库或数据库筛选点击“开始记录”即可开启；`adad` 隐藏入口已移除。其它模块保留按钮和已有记录，但不增加采集范围。
- 搜索知识库、打开文章/反应信息、数据库筛选、展开测量详情/SMILES 共用 recording_id。后台日志仍可独立观察；只有开启记录之后携带 ID 的操作才写入正式记录。
- 筛选记录按 search_id 保存原始条件与结果，即使普通最近 32 次缓存被淘汰仍可关联；不同记录的快照禁止混用。结束等待在途请求，冻结后缓存总结；失败保留清单并可重试。页面切换期间已记录的查询继续完成，不因组件卸载取消；操作失败或超时仍提示可能不完整。
- 三段式内容为：使用的功能/检索词/筛选条件 → 主动查看的文献主题/材料及信息区域 → 有依据的内容关系。未查看材料不会被当作重点阅读，重复测量按快照合并；SMILES 仅从对应展开事件提供，合成样本不当作真实文献证据。
- 内存记录仍限单进程、最多 32 条记录、每条 200 个事件；浏览器刷新不会恢复前端状态，后端重载会清空记录。跨模块导航可持续记录，浏览器刷新不属于该保证。
- 验证：后端 35 项、前端 132 项相关测试及构建通过；真实模型在约 28.54 秒完成跨模块 7 事件的三段式总结，缓存与冻结结果一致。测试已标记在虚拟机 last-observation-check.json，浏览器实际效果由用户验收。

页面复测：在本地知识库点击“开始记录” → 搜索 polyimide 并打开文献 → 切换数据库筛选，Tg 150–260，展开材料记录/SMILES → 可返回知识库继续搜索 → 点击公共“正在记录 · 总结”。整段流程只开启一次记录，过程中不刷新浏览器；样本数据对应的预期结果仅适用于上述独立 POC 库。

### 页面模型总结（当前入口）

- 在本地知识库或数据库筛选点击灰色“开始记录”；记录中点击“正在记录 · 总结”结束并生成。其他模块不开放开始入口，但已开启的记录和结果可跨模块保留；当前不再监听 `adad`。
- 总结在独立的阅读回顾面板展示，三段文本分别使用“浏览线索 / 阅读收获 / 内容联系”标题，空记录显示说明。操作时间线、计数、详情和原始字段不再展示；后端证据仍保留。提示词及模型配置本轮未改。
- 关闭按钮、Esc、点击面板外部或“继续浏览”均可收起。生成中收起不会取消请求，完成后不会自动弹回；顶部“查看生成进度 / 查看总结”可重新打开，避免重复请求。成功后可点击“开始新记录”。总结失败在面板内重试同一记录，开始失败可直接再点开始，结束失败从顶部重试。
- UI 阶段验证：新增交互测试先失败再通过；codex-lab 的 7 个文件共 91 项相关前端测试及 TypeScript/Vite 构建通过。HTTP 修复后另有 13 项针对记录逻辑的回归通过，用户已完成 9021 手动页面验收；没有据此宣称所有浏览器或屏幕尺寸均已覆盖。
- 手动验收：刷新 5173 → 开始记录 → 搜索/阅读并跨到筛选 → 点击总结 → 生成中关闭面板 → 继续浏览 → 查看总结 → 确认只显示回顾内容 → Esc 收起 → 开始新记录。页面刷新不恢复状态，后端重载清空内存。
- 轻量开发的模型配置位于 `backend/.env`；正式环境沿用上文的环境变量注入方式。使用已有 httpx，无需新增 OpenAI SDK；模型请求仅使用应用选定的代理配置，不自动继承系统代理。
- `POST /api/v1/knowledge/recordings/{id}/summary` 只接受已结束记录。成功缓存到该记录，并发请求通过同一记录的锁合并；失败允许重试。最多发送 80,000 字符的证据，超限返回 413，不静默截断。模型请求超时配置为 60 秒；认证、网络、超时、空/无效/截断响应会给脱敏错误。
- 输入包含检索/筛选条件与操作事实、主动查看文章的标题/摘要、主动查看的反应字段，以及展开的材料测量或 SMILES。同一快照内容去重，保留操作事实；不发送未查看文章正文或未展开材料的详情。
- 提示词在 `backend/app/services/knowledge_poc_summary.py`；轻量开发可热重载，正式镜像需按发布流程更新。要求不执行文献中的指令，不推断读完/偏好，不编造配方；专业化学原词与实验数字归属明确。页面将模型文本按普通文本展示，不执行模型返回的 HTML。
- 后端 24 项、前端 49 项目标测试及构建通过。真实 Vite 代理完成 6 个操作并返回总结约 12.7 秒，缓存重试约 2 毫秒，冻结清单不变。自动记录 ID 继续写入 `last-observation-check.json` 以便排除。
- 真实调用发现过模型将文献按出现顺序重新编号；已在输入中加入基于数据库 ID 的显式引用，使用唯一快照标识并按文献 ID 排列材料，9 项总结测试复测通过。该阶段最终模型调用约 10.7 秒，文献引用、操作计数及已核对的实验数据归属正确，缓存重试相同。提示词约束不是事实正确性的保证，科学内容仍需对照原文和来源记录；输出可能中英混用。

下面保留此前接线阶段的验证历史，不代表当前模型未接通。

### 2026-09-09：记录与操作清单

以下快捷键和操作清单描述仅为历史记录；`adad` 已移除，当前使用按钮入口与阅读回顾面板。

- 用户已完成文章打开与反应信息的浏览器验收，当前进入记录与操作清单阶段，尚未接 AI。
- `POST /api/v1/knowledge/recordings` 接收 `{recording_id}` 开始记录；同一 ID 重试不会清空。搜索和查看请求可带 `recording_id`，仅这些请求进入相应清单。原始 HTTP 观察日志仍独立记录所有请求。
- `POST /api/v1/knowledge/recordings/{recording_id}/stop` 冻结并返回 `started_at`、`ended_at`、`events`。重复结束返回同一清单；结束后带该 ID 的新操作返回 409。未知或重载丢失的 ID 返回 410。
- 清单包含 `search.completed` / `search.failed` / `article.opened` / `article.reaction_viewed`，有序号与时间；查看事件包含原搜索对应的完整文献快照。完成的搜索表示后端完成，不能推断前端展示或用户阅读。
- 后端至多保留 32 条记录，优先淘汰已结束记录；全部活动时拒绝新增。每条最多 200 个操作，含执行中的搜索预留名额；超限明确拒绝后续采集。活动记录保留自己的搜索快照，不受最近 32 次普通搜索缓存淘汰影响。刷新不恢复记录，后端重载清空。
- 前端状态、常驻按钮与可折叠操作清单已接线。记录中的搜索/上报未完成时禁用结束；失败或取消显示清单可能不完整。结束失败暂停本地收集并提供重试，后续操作不混入旧记录。
- 用户确认改用 **adad**，仅在本地知识库页面、输入框外触发。监听已接入；输入框/可编辑区域、组合键、输入法组合输入与长按不触发。切换模式、移动焦点/点击、窗口失焦会清理未完成序列，卸载页面移除监听。这是临时 POC 演示入口，后续替换。
- 后端 15 项测试通过；真实 Vite 代理验证两次搜索及各自打开/反应查看共 6 个事件、原摘要保存、结束幂等及边界外搜索排除。自动 ID 追加在 `last-observation-check.json` 的 `automated_recording_ids` / `automated_search_ids`。
- 前端 4 个目标文件共 48 项测试全部通过，无跳过；包括触发边界、模式切换、重复开始、上报等待、清单内容、结束失败冻结及重试。构建通过。先确认 4 个页面测试因未接入触发而失败；接线后修正一处测试在详情/清单中查找同名配方的定位范围。

演示步骤：刷新 `/knowledge` → 确认处于本地知识库 → 点击输入框外并切到英文输入状态，连续按 `adad` → 等按钮显示“正在记录 · 总结” → 搜索 `polyimide`、打开文章、查看反应信息 → 搜索 `absorbance` 并重复查看 → 点击“正在记录 · 总结” → 核对操作清单及“查看对应内容”。点击清单标题可折叠；结束后的操作不会加入旧清单，再按 adad 可开新记录。页面刷新不恢复记录；仅后端重载才会清空服务器内存。浏览器手动验收待用户执行，当前展示清单而非 AI 总结。

以下保留此前阶段验证历史。

- 已核对首次源码传输的 824 个文件 SHA-256；传输清单保存在 `.runtime/knowledge-summary/source-manifest.json`，用于追溯初始快照。
- `uv pip check` 通过；知识库路由导入、请求模型校验、内存测试应用的 OpenAPI 响应及 psycopg 驱动导入通过。
- 环境不包含 Torch、RDKit、NumPy、Pandas 等科学计算依赖。
- 数据库健康检查、独立数据卷与 loopback 绑定已确认；两行标题、摘要和来源与样本 JSON 一致。
- 真实 PostgreSQL API 验证通过 7 个场景：两个独立主题命中、OR、AND、无命中、分页、空查询 422。首次初始化因验证脚本将重复索引计数为两个而事务回滚；修正预期后初始化及检索验证通过。
- `app.main` 导入全平台服务，`backend/tests/conftest.py` 也依赖 RDKit；本环境不能直接启动该入口或运行共享后端测试集，本次未宣称全平台回归通过。
- 轻量入口 3 个测试、前端知识库 10 个目标测试与 TypeScript/Vite 构建通过；用户已确认两篇可在浏览器搜索。
- 观察日志遵循先失败再实现：新增 3 个观察测试及原入口 3 个测试共 6 个通过；实际经 Vite 代理的两次检索返回正确，并生成包含文章 ID 和摘要信息的日志。尚待用户对照点击/阅读动作进行手动验收。
- 阅读通知接线后：后端 9 个相关测试、前端 39 个知识库/API 测试和 TypeScript/Vite 构建通过；已用真实数据库经 Vite 代理验证两种通知、较早检索的关联及错误关联拒绝。自动测试使用的检索 ID 保存在虚拟机 `.runtime/knowledge-summary/last-observation-check.json`，便于与后续用户操作区分。
- 用户于 2026-09-08 17:44（北京时间）实测 1 次搜索、3 次卡片点击（文章 2 → 1 → 2），均成功关联同一检索；该轮没有重新展开事件。
- 反应页签接线遵循先失败再实现：后端 11 个相关测试、前端 41 个知识库/API 测试及构建通过。测试覆盖鼠标/键盘切入、重复点击不重复通知、换页关联、通知失败不阻断、后端原快照字段及空值。真实 Vite 代理接口模拟两篇配方查看，已核对日志，自动检索 ID 同样保存到 `last-observation-check.json`。
- 该阶段之后已完成记录会话、总结接线及页面验收。当前仍不采集停留时间，打开文章不代表已读完。
