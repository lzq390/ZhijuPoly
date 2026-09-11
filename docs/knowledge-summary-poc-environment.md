# 知识库总结 POC：轻量开发环境

## 已准备的环境

- SSH：`codex-lab`；目录：`/home/codexlab/DevTool/ZhijuPoly`。
- 虚拟机目录是本地 `feat/knowledge-summary-poc` 的源码快照，基线 `b2ea83b`；不包含 `.git`，不是独立 Git checkout。
- uv `0.12.10`，用户级安装在 `/home/codexlab/.local/bin/uv`；未修改系统 Python 或 Shell 启动文件。
- Python `3.11.16`；虚拟环境位于 `.runtime/knowledge-summary/.venv`，实测约 33 MB。
- `backend/requirements-knowledge-poc.in` 定义 7 个直接依赖，锁文件固定 21 个包及其哈希。

本环境用于知识库检索与数据库筛选的总结 POC。两个模块已接入统一记录与三段式总结，按钮提升到公共布局，用户已完成跨模块 9 事件测试。下文早期独立观察阶段的限制以“跨模块记录”小节为准。

2026-09-09 提示词评测：用户更换为 gpt-5.6-sol，已冻结重建的 9 事件样本并采用 v3 阅读回顾提示词。真实回放两次耗时 70.14/35.76 秒；评测脚本单独允许等待 180 秒，应用默认仍为 60 秒，新模型页面可能超时。前端及模型配置未在本轮改动，缓存总结不重写。来源限制、复现方法和原始输出见[提示词评测](summary-prompt-evaluation.md)。

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

验证：新增入口测试先失败后通过，POC 后端 25 项测试通过；经 5173 代理的 11 组真实检查通过，涵盖目录/缓存、全部属性分布、上述筛选场景、分页/越界、非法范围及已有知识库检索。重复初始化仍为 4 个材料、13 条记录。前端未改，浏览器实际展示由用户手动验收；筛选操作尚不进入记录与总结。

用户随后确认页面可正常筛选。现已补充筛选日志：`KNOWLEDGE_TRACE` 对 search 接口记录条件、关键词、分页、返回材料和测量字段白名单，复用下方监听脚本，无需 adad。请求/响应分别最多捕获 64 KiB；超限标记 `truncated`，正文可能无法解析，不能将缺失内容当作零结果。原请求/响应保持不变。27 项 POC 回归及真实代理日志检查通过；自动测试请求 ID 和用户观察起点保存在虚拟机 `.runtime/knowledge-summary/last-observation-check.json`。

仅靠筛选请求日志只能证明后端返回，不能推断用户主动查看；自动目录/分布加载也不视为主动阅读。旧日志未保存的条件无法追溯恢复，需重新提交筛选观察。

用户随后授权的两个查看 Hook 已接线：

- POC search 响应新增 `search_id`；`POST /api/v1/database-browser/property-filter/observations` 接收 `search_id`、从零起的页内 `result_index` 和 `source`。普通后端未返回 ID 时前端不上报，原有筛选保持兼容。
- `source=measurement_details` 携带 `filter_index`，后端返回该材料在该条件下的全部测量，事件为 `property_filter.measurements_viewed`。
- `source=smiles` 携带 `smiles_field=smiles|canonical_smiles`，后端返回实际展开的字符串，事件为 `property_filter.smiles_viewed`。样本未做标准化，目前页面展示原始 SMILES。
- 内容由后端原筛选快照关联；前端不传测量值或结构正文。仅保留最近 32 次筛选，淘汰/服务重载后返回 410；不属于该结果或未提供的内容返回 404，非法通知返回 422。
- 原生 details 展开触发通知，支持浏览器原生鼠标/键盘展开；自动渲染、收起、重复 toggle 不通知，关闭再展开新增一次。上报失败仅控制台警告，不自动重试或阻止详情展示，不连接知识库记录会话。
- 验证：后端 32 项、前端相关 49 项与筛选页面 18 项测试通过，前端构建通过；真实 Vite 代理核对旧筛选关联、两条 Tg 测量、SMILES、错误关联/缺失内容拒绝及两种通知日志。浏览器实际操作仍待用户验收。

测试方式：刷新并重新筛选 POC-A（Tg 最小值 100），展开“记录详情”和 SMILES，关闭再展开。无需 adad，通知自动写入同一个 backend.log，可继续用 `scripts/watch_knowledge_poc.sh` 观察。

## 直接开发运行与请求观察

- 前后端直接作为虚拟机进程运行：Vite `127.0.0.1:5173`（热更新），Uvicorn `127.0.0.1:8000`（`app.knowledge_poc:create_app --factory`，自动重载）。数据库保留独立容器。
- 用户自行转发 `5173` 后访问 `/knowledge` 即可；Vite 将 `/api` 代理到后端，不需要转发后端或数据库端口。
- 日志和进程号保存在 `.runtime/knowledge-summary/{backend.log,frontend.log,processes.json}`。修改虚拟机源码会触发重载；本地源码修改仍需同步到虚拟机。
- `KnowledgeTraceMiddleware` 仅接入 POC 入口，观察实际到达后端的 `/api/` 请求。搜索额外记录白名单查询字段、总数、文章 ID/标题、摘要长度及前 160 字符；不记录请求头、凭据或推断阅读行为。
- POC 搜索响应新增 `search_id`，与当前这批结果一同保存在前端。`useKnowledgeObservation` 在点击文章卡片及重新打开详情时调用 `POST /api/v1/knowledge/observations`，发送 `search_id`、`knowledge_id` 和 `source`（`result_card` / `drawer_reopen`）。
- 后端从该次结果快照核对文章，返回并记录 `article.opened`、原查询、标题和摘要长度。前端不发送正文，自动展开第一条不产生通知；后端未返回 `search_id` 时也不发送通知，兼容普通平台后端。
- 主动切入“反应信息”页签时复用同一 Hook 与接口，`source=reaction_tab`，事件为 `article.reaction_viewed`。鼠标与键盘切换均支持；重复点击当前页签、自动重置页签不通知，切走再切回会再通知。关闭后重开且保留该页签时，仍只记录原有 `drawer_reopen`。
- 反应事件的 `reaction_info` 来自后端原检索快照，包含聚合物、配方、催化剂、溶剂、温度、时间、分析与判断理由；缺失字段保持 `null`。两篇当前样本均未提取这些字段，因此只能证明查看了该区域，不能据此总结出具体配方。
- 仅在 POC 单进程内保留最近 32 次检索快照；重载或淘汰后旧 ID 返回 410，需要重新搜索，文章不属于该次结果返回 404。通知失败只在浏览器控制台告警，不阻止详情展示，也不自动重试或持久化查看记录。
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

- Provider 位于 App 根部，公共总结按钮位于 AppShell 内容区上方，页面切换不会卸载记录状态。输入框外 adad 可从本地知识库或数据库筛选页面开始；其它模块保留按钮和已有记录，但不增加采集范围。
- 搜索知识库、打开文章/反应信息、数据库筛选、展开测量详情/SMILES 共用 recording_id。后台日志仍可独立观察；只有开启记录之后携带 ID 的操作才写入正式记录。
- 筛选记录按 search_id 保存原始条件与结果，即使普通最近 32 次缓存被淘汰仍可关联；不同记录的快照禁止混用。结束等待在途请求，冻结后缓存总结；失败保留清单并可重试。页面切换期间已记录的查询继续完成，不因组件卸载取消；操作失败或超时仍提示可能不完整。
- 三段式内容为：使用的功能/检索词/筛选条件 → 主动查看的文献主题/材料及信息区域 → 有依据的内容关系。未查看材料不会被当作重点阅读，重复测量按快照合并；SMILES 仅从对应展开事件提供，合成样本不当作真实文献证据。
- 内存记录仍限单进程、最多 32 条记录、每条 200 个事件；浏览器刷新不会恢复前端状态，后端重载会清空记录。跨模块导航可持续记录，浏览器刷新不属于该保证。
- 验证：后端 35 项、前端 132 项相关测试及构建通过；真实模型在约 28.54 秒完成跨模块 7 事件的三段式总结，缓存与冻结结果一致。测试已标记在虚拟机 last-observation-check.json，浏览器实际效果由用户验收。

页面验收：刷新 → 在本地知识库空白区域输入 adad → 搜索 polyimide 并打开文献 → 切换数据库筛选，Tg 150–260，展开材料记录/SMILES → 可返回知识库继续搜索 → 点击公共“正在记录 · 总结”。整段流程只开启一次记录，过程中不刷新浏览器。

### 页面模型总结（当前入口）

- 2026-09-10 UI 更新：在本地知识库或数据库筛选点击灰色“开始记录”，无需隐藏快捷键；记录中点击“正在记录 · 总结”结束并生成。其他模块不开放开始入口，但已开启的记录和结果可跨模块保留；adad 暂时兼容。
- 总结在独立的阅读回顾面板展示，三段文本分别使用“浏览线索 / 阅读收获 / 内容联系”标题，空记录显示说明。操作时间线、计数、详情和原始字段不再展示；后端证据仍保留。提示词及模型配置本轮未改。
- 关闭按钮、Esc、点击面板外部或“继续浏览”均可收起。生成中收起不会取消请求，完成后不会自动弹回；顶部“查看生成进度 / 查看总结”可重新打开，避免重复请求。成功后可点击“开始新记录”。总结失败在面板内重试同一记录，开始失败可直接再点开始，结束失败从顶部重试。
- 验证：新增交互测试先失败再通过；codex-lab 的 7 个文件共 91 项相关前端测试通过，`npm run build`（含 TypeScript 检查）通过。保留原有 Browserslist 数据过期和大包提示。当前无浏览器自动化能力，实际视觉、窄屏滚动及页面手动验收待用户完成。
- 手动验收：刷新 5173 → 开始记录 → 搜索/阅读并跨到筛选 → 点击总结 → 生成中关闭面板 → 继续浏览 → 查看总结 → 确认只显示回顾内容 → Esc 收起 → 开始新记录。页面刷新不恢复状态，后端重载清空内存。
- 模型配置位于虚拟机 `backend/.env`：`ASSISTANT_BASE_URL`、`ASSISTANT_API_KEY`、`ASSISTANT_MODEL`；可选 `AI_PROXY_URL`。使用已安装的 httpx，无需 openai SDK；未改写用户配置，也不继承系统代理。
- `POST /api/v1/knowledge/recordings/{id}/summary` 只接受已结束记录。成功缓存到该记录，并发请求通过同一记录的锁合并；失败允许重试。最多发送 80,000 字符的证据，超限返回 413，不静默截断。模型请求超时配置为 60 秒；认证、网络、超时、空/无效/截断响应会给脱敏错误。
- 输入只含检索与操作事实、主动查看文章的标题/摘要、主动查看反应信息时的字段。同一快照内容去重，保留完整操作次数，不发送未查看文章正文或原始记录中的其他字段。
- 提示词在 `backend/app/services/knowledge_poc_summary.py`，可直接调整并热重载。要求不执行文献中的指令，不推断读完/偏好，不编造配方；专业化学原词与实验数字归属明确。页面将模型文本按普通文本展示，不执行模型返回的 HTML。
- 后端 24 项、前端 49 项目标测试及构建通过。真实 Vite 代理完成 6 个操作并返回总结约 12.7 秒，缓存重试约 2 毫秒，冻结清单不变。自动记录 ID 继续写入 `last-observation-check.json` 以便排除。
- 真实调用发现过模型将文献按出现顺序重新编号；已在输入中加入基于数据库 ID 的显式引用，使用唯一快照标识并按文献 ID 排列材料，9 项总结测试复测通过。最终模型调用约 10.7 秒，文献引用、操作计数及已核对的实验数据归属正确，缓存重试相同。提示词约束不是事实正确性的保证，科学内容仍需对照清单；输出可能中英混用。

下面保留此前接线阶段的验证历史，不代表当前模型未接通。

### 2026-09-09：记录与操作清单

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
- 下一步由用户在浏览器验收“反应信息”通知，再接入记录会话和总结按钮；当前不采集停留时间，不代表用户已经读完文章。未新增依赖或修改数据库。
