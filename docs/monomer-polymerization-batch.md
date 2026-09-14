# 双表批量正向聚合：实现与运维

实现日期：2026-09-10。功能开关默认关闭；本次改动没有应用生产迁移或开启生产入口。

## 功能与使用

开启后，普通入口 `/monomer-polymerization` 默认进入批量模式。结构工作台携带单体跳转时使用 `?mode=single`，继续使用原单次聚合。任务链接为 `?mode=batch&job_id=<32 位任务 ID>`，浏览器另保存最近 20 个任务 ID。离开页面只停止轮询，后台计算继续。

服务状态刷新失败时保留上一次确认的批量模式，避免清空已选文件和面板状态。成功读取任务链接后自动记入本浏览器历史。历史导航和任务选择均立即清除旧任务、终止旧请求；界面数据、取消与下载操作须对应当前任务，迟到的旧响应不能覆盖新任务。

1. 选择目标类别，默认 `polyimide`；支持原有七类和 `all`。
2. 上传 A、B 两份 CSV/XLSX，可混用格式。默认使用 UTF-8 和自动识别的字段；通过一个「导入设置与前 20 行」入口，同时展开或收起两表的字段设置和输入记录。任一表需要修正时自动展开；收起不清空已选映射。
3. 预检显示原始记录、有效记录、唯一结构、重复与错误数量，以及原始/有效/独立化学组合数。
4. 提交后台任务，查看分类、聚合、导出阶段与进度。可取消、刷新或通过任务链接恢复。
5. 在设置区下方独立的「批量任务」面板查看任务状态、已处理组合、候选与错误数量、结束及到期时间，下载 CSV ZIP、XLSX 或单独的错误 CSV。面板默认展开，可收起为标题与状态摘要；折叠不影响后台计算和状态轮询，切换批量／单次后保留折叠状态，新任务提交成功后自动展开。批量任务区域不展示候选表格、结构图或分页，也不请求候选预览接口；后端分页接口保留兼容。

批量／单次切换位于「02 单体输入」，两种模式均使用模块页面整体滚动。主工作区随内容自然展开；单次结果抽屉保持在可视范围内。两种模式按元素角色共用字体、字号、字重和行高：正文与卡片标题为普通屏 12px／2K 17px，标签与说明为 11px／15px，主要操作按钮为 12px／18px，紧凑操作按钮为 11px／15px。统计数字使用正文同款字体，为 18px／24px；化学结构输入保留等宽字体。

按钮共用主题浅蓝悬停、按下反馈与键盘焦点样式；当前模式、展开入口及选中选项使用蓝青渐变和描边。刷新状态显示旋转图标与「刷新中…」，请求期间禁用重复点击，完成或失败后恢复。减少动态效果偏好生效时取消按下位移并缩短过渡。

任务取消和下载独立于上传预检管理，进度与错误均显示在任务面板中，折叠摘要也保留反馈。下载先检查 HTTP 响应并完整读取文件，再交给浏览器保存；失败时提供重试，`expired` 响应移除全部下载入口，单文件缺失仅禁用该文件，可刷新后重试。完成任务停止轮询后仍按文件到期时间更新过期状态；较早发出的状态请求不能恢复已由服务端确认过期的下载入口。

组合数按输入记录或去重后的单体结构对计数；候选结果数按输出条目计数。一组单体可生成多个候选，因此候选结果数可能超过组合数。页面在任务统计下提供说明，具体每组数量可查下载包中的 `pairs` 表。

同一表内业务编号不要求唯一；标准结构重复只减少计算量。任一侧完全无有效输入时不能提交。`polyolefin` 等类别的单体独立反应不能作为双表结果；无符合跨表规则的候选时记录 `no_match`，不代表化学上不能反应。

## 文件与来源契约

- CSV 支持 UTF-8、BOM 和显式 GB18030，支持引号内换行。XLSX 使用 openpyxl 只读模式，默认首个可见工作表。
- 首行必须为非空、唯一表头；完全空白行跳过，但仍占来源行号。有其他内容而无 SMILES 的行计入错误。
- CSV 的行号是包含表头的逻辑记录序号，带引号的多行字段仍是一条记录；XLSX 使用工作表行号。来源键为 `a:<行号>` / `b:<行号>`；配对键为 `a<行号>:b<行号>`。
- SMILES 禁止 `*` 连接点、超过 1000 字符或 Excel 公式。编号模板使用文本；已经被 Excel 改写的数字编号无法还原原始前导零。
- 表头自动匹配不唯一时必须选择。重新上传或新预检开始即废弃旧修订，失败后保持禁止提交，只有当前导入的有效预检可用于创建任务；并发旧请求不能覆盖较新预检。
- 原始额外列保存在 `inputs_a/inputs_b` 中，使用 `input:` 前缀避免覆盖来源键等内部字段。表头长度为该前缀预留空间，导出时再次校验，禁止静默截断；输入阶段拒绝 Excel 无法保存的控制字符，损坏的工作表内容返回可修正的文件错误。

ZIP 中包括 `results.csv`、`pairs.csv`、`input_errors.csv`、`inputs_a.csv`、`inputs_b.csv`、`summary.csv` 和 `summary.json`；XLSX 包含对应工作表。`summary.json` 保存数据 CSV 哈希、输入文件哈希、统计、限额快照、引擎版本与完整性。为避免自引用哈希，`csv_files` 不包含 summary 文件本身；最终下载清单对所有文件另提供 SHA256。

`results` 保留 A/B 原始来源、原始与标准 SMILES、候选序号、精确产物 SMILES、类别、规则号以及引擎实际 `mon1/mon2/reactset`。每个原始行对可对应多个候选，按 A 行号、B 行号及稳定候选顺序输出。CSV 带 UTF-8 BOM，Excel 使用只写模式，每张表最多 1,000,000 行，自动续表；单元格无法完整表示时明确失败。电子表格公式前缀被转义；JSON 预览仍保留原值。

| pairs 状态 | 含义 |
|---|---|
| `success` | 至少一个候选 |
| `no_match` | 正常计算，当前规则未生成候选 |
| `invalid_input` | A/B 任一原始输入未通过校验 |
| `error` | 分类或聚合异常已经隔离到该化学组合 |
| `not_processed` | 取消或失败导致尚未提交结果 |

完整结果中各状态数量之和等于原始非空行对总数。正常含输入/计算错误的任务为 `completed_with_errors`。取消或计算失败时尝试用已完成分片导出明确标为不完整的结果，已知分类失败的组合保持 `error` 及对应统计；如果最终导出本身失败或没有足够磁盘空间，则状态为 `failed`，不会把不完整文件发布为成功下载。

## 计算和持久化

后端实现位于 `backend/app/services/polymerization_batch/`。单次与批量共用标准化、SMiPoly 运行时及候选解析；批量不会调用单次 HTTP 接口，也不会继承其 20 条返回限制或批量生成 SVG。

唯一结构先按最多 128 个一组分类，移除分类器补入、没有上传来源键的辅助行；显式上传的相同辅助分子保留。A、B 分别按最多 32 种结构分块，每块合并池最多 64 种。子进程读取分类缓存的独立副本，使用一次 `biplym(targ=[target])`；`all` 仍是一次原生调用。候选按照两种反应物方向映射到当前块 A/B，排除同表来源，再以“来源对＋精确产物＋类别＋规则号”去重。最后展开到全部原始行对。

分类异常二分隔离输入；聚合异常沿较长维度二分，最终隔离到单对。缺引擎、版本不符、损坏分片等系统错误直接停止，不重复拆分。化学运算在可终止进程中，限制地址空间和单次耗时；取消/退出会杀死并回收子进程，Linux 父进程死亡信号防止 worker 被强杀后留下计算进程。

PostgreSQL 新增 `polymerization_batch` schema：

| 表 | 内容 |
|---|---|
| `imports` | 原始文件、预检修订及到期时间 |
| `jobs` | 幂等请求、参数/版本/统计、状态、当前执行令牌、累计耗时、下载清单 |
| `chunks` | 稳定分类/聚合/导出编号、attempt、租约、计数及完成分片引用 |
| `worker_status` | 单例 worker 心跳和能力 |

worker 使用 PostgreSQL 会话 advisory lock，默认只有一个执行者。领取任务和发布排空检查在同一事务中锁定 deployment-control 行。排空后不领取新分片；正在执行的单元完成或退出后释放令牌，排队和已保存进度不阻塞发布。排空期间心跳和清理也不改写业务审计数据；恢复仅回收仍阻塞排空的失效执行，不修改已空闲任务。

每次执行写入唯一 attempt 目录，文件关闭/校验后通过事务发布引用。数据库提交确认丢失时保留输入及分片文件，由幂等重试或恢复确认提交状态，未引用的旧文件随后清理。只有完成记录进入后续导出。重启清除旧执行令牌，重算未提交单元；已提交分片保持原样，旧 worker 无法提交新令牌的结果。分类缓存、分片和打包验证统一版本指纹，包含 SMiPoly、RDKit、pandas、规则和适配代码哈希；预检与 worker 版本也必须相同。版本改变时旧未完成任务明确失败，不混合不同版本结果。

保留清理删除到期输入、预检、分类、分片和输出，保留 `expired` 任务元数据。未引用的旧 attempt 也会回收。下载先打开文件，避免清理 unlink 中断正在传输的已打开文件。停用 worker 时物理清理暂停，接口仍按过期时间禁止新下载。

## API

统一前缀 `/api/v1/monomer-polymerization/batch`，独立类型位于后端 `models.py` 和前端 `types/polymerizationBatch.ts`。

| 方法与路径 | 请求/返回 |
|---|---|
| `POST /imports` | multipart：`file_a`、`file_b`；201，导入信息和初始预览 |
| `POST /imports/{id}/preview` | JSON：`a`、`b` 各自的列/表/编码映射；预检修订与统计 |
| `POST /jobs` | JSON：`import_id`、`preview_revision`、`target_class`；必需 `Idempotency-Key`；202 |
| `GET /jobs/{id}` | 状态、阶段、统计和已发布下载清单 |
| `GET /jobs/{id}/results?offset=0&limit=50` | 候选分页，单页最多 100 |
| `POST /jobs/{id}/cancel` | 幂等取消；`cancelling` 不是已停止 |
| `GET /jobs/{id}/artifacts/{name}` | 仅下载该任务清单内的已发布文件 |
| `GET /templates/{a\|b}.{csv\|xlsx}` | 两侧示例模板 |

现有 `/api/v1/monomer-polymerization/status` 增加可选 `batch` 字段，包括启用/worker 可用性、格式、限额和说明。相同幂等键与参数返回原任务，即使之后队列已满或 worker 暂不可用；参数冲突 409，旧预检 409，容量不足 429，过期 410，未启用/系统不可用 503。

首版沿用站点访问边界，以不可猜测的任务 ID 链接找回；没有引入账户所有权或跨浏览器个人任务列表。

## 配置与启动

API 与 worker 必须使用同一数据库、同一后端镜像和同一持久目录。所有设置由 `BatchSettings.from_env()` 读取；下表键名除特别注明外均加 `MONOMER_POLYMERIZATION_BATCH_` 前缀。

| 配置后缀 | 默认值 |
|---|---:|
| `ENABLED` | `false`；同时要求 `SMIPOLY_ENABLED` 未关闭 |
| `STORAGE_ROOT` | 本地 `.runtime/monomer-polymerization-batch`；Compose 内 `/app/.runtime/monomer-polymerization-batch` |
| `FILE_BYTES` / `REQUEST_BYTES` | 10485760 / 23068672 |
| `MAX_ROWS` / `MAX_PAIRS` | 5000 / 50000 |
| `MAX_COLUMNS` / `MAX_CELL_CHARS` | 100 / 32767 |
| `XLSX_UNCOMPRESSED_BYTES` | 67108864；另限制压缩包条目数 2048 |
| `CHUNK_SIZE` / `CLASSIFICATION_SIZE` | 32 / 128；块宽不能大于 32 |
| `QUEUE_CAPACITY` | 10，另保留 1 个执行名额 |
| `SUBPROCESS_SECONDS` / `MEMORY_BYTES` | 60 / 2147483648 |
| `JOB_SECONDS` | 3600，不含排队 |
| `RESULT_BYTES` | 536870912，包含已提交分片和最终输出 |
| `IMPORT_HOURS` / `RETENTION_DAYS` | 24 / 7 |
| `LEASE_SECONDS` | 90 |

额外空行的物理记录上限为 `max(MAX_ROWS × 4, 20000)`。API 同时最多运行两个文件解析/预检进程。正常导出同样受累计 3600 秒限制。已取消或失败任务的打包收尾最多另外给予 600 秒，不能因此宣告超时任务成功。Compose worker 限 1 CPU、3 GiB 容器内存，其中计算子进程地址空间默认 2 GiB。

生产配置写入既有 `NEXPOLY_APP_ENV_FILE`，由 API/worker 一起读取；开发覆盖文件中可通过 Compose 环境变量启用。代码默认关闭，因此只更新镜像不会自动开放功能。

常规开发入口 `scripts/dev_server_gpu.sh up` 在迁移后启动 API 和 CPU worker 并等待健康；`stop/down/contract-migrate` 在停止数据库前停止 worker。GPU 会话切换保持 CPU worker 运行。

开发机也可在已经完成迁移的专用数据库上单独启动 worker：

```bash
export MONOMER_POLYMERIZATION_BATCH_ENABLED=true
export MONOMER_POLYMERIZATION_BATCH_STORAGE_ROOT=/absolute/persistent/batch
PYTHONPATH=backend python -m app.monomer_polymerization_batch_worker
```

2026-09-10 已在 9001 开发环境启用：`.env.dev` 中批量开关为 `true`，开发库已应用 0016，API 与独立 CPU worker 使用相同镜像和共享持久目录。开发环境的 GPU 会话已恢复，发布排空已解除。

实机启动修复了两项遗漏：worker 的 `GPU_PRELOAD_MODE` 使用配置解析器接受的 `lazy`，通过关闭 broker 和 GPU 可见性保持 CPU 执行；Postgres 预检实际查询四张批量表，避免已迁移后仍报告缺表。Compose 检查 6 项、Postgres 预检回归 10 项通过。

手工上传样本位于 `frontend/public/samples/monomer-polymerization-batch/`，分别为 `smipoly-diamines-120.csv` 和 `smipoly-dianhydrides-120.csv`。来源为本机 SMiPoly 的 `classified_monomers.csv`，保留来源行号和标签；每表选择 120 个有效、不同的结构。9001 实际上传预检确认原始、有效、独立组合均为 14,400，错误为 0。另以 2×2 小样验证后台计算和 CSV ZIP／XLSX 下载，4 对全部成功。浏览器验证默认批量入口和上传按钮可用；随后用户完成 120×120 手工验证：14,400 对全部成功，输出 15,875 条候选；下载 ZIP 的大小和 SHA256 与发布清单一致。

依赖增加 `openpyxl==3.1.5`、`defusedxml==0.7.1`，CI 同时安装真实 SMiPoly；已更新哈希锁文件。使用项目 Python 3.11 环境运行 `scripts/compile_requirements.sh`，锁校验为 `python scripts/ci/validate_dependency_locks.py`。

## 发布与回滚

1. 先发布兼容基线：批量开关关闭，保留当前数据库终点 0015，基线包含识别精确 0016 校验和、统计 schema 3、worker 生命周期和新版审计 helper 的能力。基线发布产物的迁移 SQL 文件集和清单仍止于 0015，strict preflight 根据该清单不要求批量表；正式 0016 包则强制检查全部四张批量表；新控制器写入基线状态时预先登记精确的 post-0016 兼容状态，历史状态读取不会被自动改写；不要直接把未具备兼容能力的旧镜像作为回滚目标。
2. 更新受管部署审计 helper 和只读角色配置。示例位于 `ops/config/deployment-mutable-data-audit.example`、`mutable-data-audit-role.sql.example`；旧证据仍可验证，0016 后必须覆盖批量表。迁移会在审计角色已存在时授予新 schema 的只读审计权限。
3. 通过现有发布控制器应用下一编号 expand migration `0016_monomer_polymerization_batch.sql`，执行严格 preflight。`final-0013` 仍是兼容命令名，但校验当前完整 manifest（含 0016）。不得改写已有 0012 contract 或跳过 checksum 校验。
4. API 和 `polymerization-batch-worker` 使用同一受管镜像 digest；共享 `${NEXPOLY_RUNTIME_ROOT}/state/monomer-polymerization-batch`。控制器已纳入 worker 停止、启动、停止证明和镜像一致性验证，不依赖手工单独 `compose up`。
5. 在目标环境完成万级任务、重启、下载核对及真实发布排空/回滚演练后再启用。schema 1/2/3 统计均可读；只有实际执行中的批量单元计入 active work。控制器持久保留 0015→0016 的有序迁移审计链，使后续代码升级和回滚继续验证完整来源。
6. 升级改变引擎/适配指纹时，先让旧版本任务完成或取消，或接受旧未完成任务明确失败后重新提交。保留 7 天的已完成下载文件不需要重新计算。回滚保留 expand 表及持久目录，恢复兼容基线；不要删除目录或降级数据库。

本次仅进行了隔离数据库和本机测试。真实生产镜像构建、兼容基线发布、现场 helper 安装、生产排空和回滚演练仍属于发布步骤。

## 验证与复现

后端专项测试 `backend/tests/test_polymerization_batch.py` 覆盖真实七类/`all` 与逐对比较、重复来源、反向来源、辅助分子、CSV/XLSX、取消、恢复令牌、发布失败重试、版本冲突、资源失败、子进程回收、保留清理及流式 Excel 拆表。前端覆盖上传/映射失效、旧响应隔离、幂等重试、刷新恢复、取消及部分结果提示；原单次聚合测试保留。

可复现的真实万级脚本：

```bash
# APP_POSTGRES_DSN 指向专用测试 PostgreSQL，角色须能创建/删除测试数据库。
# 脚本自行创建、迁移、删除临时数据库，不迁移该 DSN 指向的原数据库。
python scripts/benchmark_polymerization_batch.py --rows 100 --output /tmp/batch-normal
python scripts/benchmark_polymerization_batch.py --rows 100 --restart --output /tmp/batch-restart
```

每侧 100 个不同有效结构，10,000 个独立组合。输出保留 `report.json`、CSV ZIP、XLSX；重启模式额外保留 `worker.log`，在实际独立 worker 计算时强杀并重启，同时测量状态接口及单次聚合的响应。

本机连续计算基准：19,100 条候选，10,000 对成功，端到端约 217.4 秒，其中输入准备约 2.7 秒、导出约 10.1 秒；计算子进程峰值 RSS 约 285 MiB。CSV ZIP 413,714 字节，XLSX 1,892,832 字节。该基准使用特定二胺/二酐样本，不是各类别、各硬件的生产 SLA。

独立 worker 在聚合中被强杀并重启一次后，仍输出 19,100 条候选；五份数据 CSV 的 SHA256 与不中断运行完全一致。重启任务约 224.8 秒，批量计算期间状态查询 P95 约 32 ms，单次聚合请求约 126 ms。原始测量与版本指纹见 [验收报告](monomer-polymerization-batch-acceptance.json)。验收后进一步收紧了正常导出的累计时限，并用独立回归测试覆盖；此调整不改变化学计算。

整体 review 回归：批量后端 47 项（原有 28、worker 边界 9、文件边界 10）、原单次聚合 31 项、部署与迁移 515 项、前端 58 项（功能 41、导航 17）均通过。前端在仅包含本次提交内容的独立目录中重新安装锁定依赖并完成生产构建；依赖锁和迁移 checksum 校验通过。浏览器覆盖 2560×1440、2048×1152、1440×1000 和 390×844，检查展开/收起、模式切换、无横向溢出及实际下载。
