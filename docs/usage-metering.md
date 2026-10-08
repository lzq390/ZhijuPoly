# 个人用量统计（第一阶段）

本模块为将来的个人免费版／会员版提供可核验的用量观测，不是账单系统。现有游客、已登录用户的权限保持不变；不增加订阅、价格、扣费、配额、用户降级或管理员跨用户面板。匿名游客没有可验证的用户标识，不生成假用户账本。

## 用户入口与接口

账户菜单“我的用量”，路由 `/account/usage`。默认最近 30 天，UTC 日期范围最多 366 天，结束日期在界面包含当日、请求中转换为次日零点的排他上界。可按模块筛选，查看状态、日趋势、指标、分页任务明细及子调用。

三个接口均使用现有会话身份，既不接受也不返回其他用户的标识：

* `GET /api/v1/usage/catalog`：模块、覆盖范围和指标单位；关闭时仍可说明未启用。
* `GET /api/v1/usage/summary`：时间范围、汇总、模块统计、日趋势及覆盖说明。
* `GET /api/v1/usage/records`：分页记录；支持 `status`、`include_children`。

汇总和明细由 SQL 显式限定当前用户，并同时受数据库 RLS 保护。重复可读快照保证同一次响应中的计数和指标一致。界面沿用既有离开编辑页保护，登录身份切换会清空旧结果、取消请求并拒绝迟到响应。

## 统计口径

`usage.events` 保存不可变事件，`usage.attempts` 是由同一事务内的 invoker trigger 生成的投影。任务维度是 owner/module/task/attempt/kind；每个 owner 的稳定事件键只能对应同一内容。重放相同内容不会重复计量，不同内容复用事件键会报错。指标为累计绝对观测值，不是可重复加算的增量。

* `tasks`：根任务数量；一个任务的多次尝试不增加任务数。
* `attempts`：根任务尝试数；分块执行和供应商调用单列 `child_calls`。
* 状态统计使用任务最新一次尝试的最后观测状态。支持排队、运行、完成、失败、取消、超时、中断和拒绝。
* 时间范围以根任务首次提交时间归属。跨日执行和重试仍归属于同一任务的提交日期；这不是按自然日资源消耗结算。
* 任务历时只汇总根任务，绝不与其子执行／供应商请求历时相加。模型执行观测优先取 execution 子记录；没有子记录时才使用根任务提供的真实执行观测。
* 供应商 tokens 只取真实响应 usage 字段，并仅汇总 provider 记录。缺失保持 NULL，不用字符数或本地分词估算。缓存输入、推理与总 tokens 分开显示，不能相互加算成额外消费。
* `artifact_bytes` 目前只有字段与展示契约，生产采集代码尚无写入点；不能将界面中的未知值理解为零产物。当前磁盘占用也未采集。
* 未记录与零值不同。每项汇总包含 `known_count` 和 `unknown_count`；已知部分求和不代表完整资源消耗。token 的 unknown 还包含不适用 token 的文献 HTTP 子调用，不等于遗漏了相同数量的模型账单。
* queue/execution/wall 都是各自边界内的时间观测；不采集进程 CPU 时间、GPU 利用率／显存占用／GPU 小时、当前存储占用或费用，不能据此收费。

## 采集边界

21 个受控模块全部标记为 `partial`（部分覆盖），包含 36 条同步任务路由、7 条原生异步提交路由及 48 条显式观察路由。路由分类来自 `app/usage_catalog.py`。机械覆盖测试验证路由分类，没有证明所有执行分支或所有资源指标均已采集；OpenScience 也不是唯一缺口。新增 API 必须明确归类，避免遗漏或重复创建根任务。

原生异步任务包括 MD、DFT、批量聚合、在线知识抽取、Tg 条件生成、PolyTAO 和逆向设计。持久任务在业务事务中写入观测；分块与重试保留独立 attempt。保留原有取消、租约、终态与启动授权机制，不新增可绕过保护的任务操作。

同步任务覆盖性质预测、结构查询和处理、OCSR、逆合成、单次聚合、批量导入校验、数据库检索、历史 DFT、知识检索、MD 演示，以及助手、Tg 助手和浏览总结。局部模型调用保留独立 execution 观测。轮询、事件订阅、下载、登录、健康检查、清理操作和用量查询自身不产生新的计算任务。演示 MD 与历史 DFT 查询明确区别于真实科学计算。

供应商包装器已接入现有 Chat/Responses 同步与流调用、在线抽取、文献 HTTP 和原始 HTTP 浏览总结；fallback 是独立子尝试。这里只说明插桩入口，不保证供应商一定返回 usage。助手 Chat 流当前未附加 `stream_options.include_usage`，因而常缺 token；包装器保持原请求参数，不擅自给兼容端点增加此选项。浏览总结流原有请求已带 `include_usage`，仍只记录真实回传值。Responses 仅按收到的终态事件读取 usage。计量不增加付费调用、不改变认证和重试策略。生产者实际结束才结束对应观测，客户端断开不等于后台计算结束。

### 21 个模块当前实际指标

下表列代码已有写入点，所有行的覆盖等级均为 **partial**；未到达对应调用或未可靠写入终态时，指标仍可未知。

* **W**：根任务 `observed_wall_ms`。同步请求为中间件／生产者观测历时；原生任务为提交／attempt 创建到终态观测，含等待。batch／online 重启恢复时还可能含停机后直到恢复发现的间隔。
* **E**：局部模型调用的 `execution_ms` 子记录，仍是调用边界的墙钟历时，不是 CPU 时间或 GPU kernel 时间。
* **P**：外部调用子记录的 `observed_wall_ms`；**T**：仅响应实际提供时才记录的 token 字段。普通文献 HTTP 只有 P，token 不适用而保持 NULL。
* **Q**：MD 数据库创建到首次 `running` 的 `queue_ms`，不是单独测得的 Broker GPU 等待。当前只有 MD 写入该指标。

| 模块 ID | 路径与当前指标 | 明确边界 |
| --- | --- | --- |
| `assistant` | 同步／流根 W；调用供应商时 P、可选 T；实际执行性质预测工具时可有 E 子记录 | Chat 流未请求 usage；工具未执行时没有 E |
| `tg_assistant` | 同步／流根 W；供应商 P、可选 T | Chat 流未请求 usage；不能从输出文本推算 token |
| `recording_summary` | 根 W；真正生成总结时 P、可选 T | 空记录等跳过生成的路径没有供应商调用 |
| `knowledge` | 检索请求 W | 未采集数据库 CPU、IO 或独立执行指标 |
| `online_knowledge` | 同步搜索 W；原生任务 W；两条路径真正调用文献源／抽取模型时 P、模型可选 T | HTTP 子调用无 token；恢复中断只证明本地观测结束 |
| `conditional_generation` | 原生根 W；每次生成／评估 E | 根 running 可仍在等 GPU，未单独采集 Q |
| `polytao` | descriptors 同步请求仅 W；原生生成 W + 每个生成／解码 microbatch 的 E | 同模块不同路径不能视作相同计算量 |
| `predict` | 同步根 W；每个实际预测属性的模型调用 E | 特征准备、加载等不都在 E 边界内 |
| `ocsr` | 同步根 W；实际模型 `predict` 的 E | 排队、加载、前后处理不都在 E 内 |
| `retrosynthesis` | 同步根 W；模型生成和解码 E | 没有硬件利用率／费用观测 |
| `reverse_design` | 同步查询／原生任务 W | 当前没有独立模型 E 插桩 |
| `query` | 请求 W；性质匹配实际调用预测器时有 E 子记录 | 普通 lookup／PG／SVG 路径仅 W |
| `structure` | 2D／3D／标准化请求 W | 结构计算没有独立 E 插桩 |
| `database_browser` | 已列入目录的浏览／筛选请求 W | 未采集数据库资源；观察接口不另建根任务 |
| `dft_history` | 历史查询 W | 不代表本次运行了 DFT |
| `monomer_md` | 原生根 W；已观察到开始时 Q | 没有科学执行 E；计划步数不是已测执行量 |
| `monomer_dft` | 持久 attempt 创建到终态观测 W | 无 Q 或科学 E；仅收到终态不能补造开始时间 |
| `polymerization` | 单次聚合请求 W | 没有独立 CPU／模型执行指标 |
| `polymerization_batch` | 原生根 W；chunk 另有 observed wall 子记录 | chunk 的 `execution_ms` 仍未知；重启中断 chunk 不估算时长 |
| `batch_preflight` | 上传／预检请求 W | 不是 batch 计算任务，未采集产物字节 |
| `md_demo` | 演示／原子距离请求 W | 不代表真实 MD 科学计算量 |

汇总将子记录归属原根模块，例如 query／助手下的 predict 子记录仍属于该次 query／助手任务。根 W 不与 E、P 或 chunk wall 相加；provider／chunk wall 可在子明细查看，当前根任务 wall 汇总不累加它们。全表所有模块都没有生产 `artifact_bytes` writer。

### 计数、状态与恢复限制

1. 同步路由在通过身份中间件后、进入业务前建立请求根；后续参数校验／准入返回 4xx 可计作 `rejected`。原生提交不另建 HTTP 根，只有到达 repository／manager 采集点才入账；此前配置、参数、容量等拒绝通常没有账本。两类计数不能直接当作相同口径的“所有提交次数”，认证前拒绝也不在账本中。
2. 同步请求的客户端重试会产生新 UUID 和新根任务，不是同一尝试的自动去重。没有原生幂等键的再次提交同样可能成为新任务；DFT／batch 仍遵循各自既有幂等提交契约。事件键重放幂等不等于业务请求 exactly-once。
3. 状态表示已观测的生命周期，不等于所有 HTTP 成功都通过了业务结果验收。3D 仅在原 deadline 停止并等待子进程退出后抛出的 `Structure3DTimeoutError` 标为 `timed_out`，HTTP 仍为 422；OCSR／逆合成仅 `GpuQueueTimeoutError` 标为 `timed_out`，HTTP 仍为 503。一般 422／503、queue full／stopped 不据此改判；已恢复的子调用失败不自动使根失败，interrupted／cancelled 不被超时覆盖。逆向设计 deadline／row limit 等其余业务状态映射仍有审计边界。原生取消请求本身也不是资源已释放或终态的证明。
4. 进程硬崩溃、写入提交后 ACK 丢失、供应商未回传 usage 等场景尚无全面自动对账；可能留下最后观测状态或未知指标。内存补记队列不能跨进程恢复，数据库锁消失不等价进程死亡。不能为消除未知而生成时长或 token。
5. online 仅在既有单后端启动恢复实际返回的业务行上，同事务将该 owner／root 下仍未终态的 provider 子记录标记 `interrupted`。这证明本地观察者中断，不证明远端推理停止；没有补造 token 或 provider 历时。它不是跨模块全局恢复扫描。
6. batch／online 根终态 W 来自业务 created 到终态／恢复观测时间。旧 NULL 终态再次被原生路径读取时，以稳定 `root-wall-v1` 事件键补充该观测，不新增根或 attempt，不改旧事件内容；没有后台历史回填。batch 的 heartbeat `consumed_seconds` 估算不作为实测用量。
7. 共享 OpenScience 是独立服务，本仓库不能验证其 owner 或内部消耗，另标 `external`。隐藏的 lab-data 运维接口不因此向普通用户开放。
8. 启用前历史任务不自动回填。业务删除／产物过期不删除账本；本阶段没有自动清理账本的作业。接口中 365 天是最低保留策略目标，不是已启用的定时删除承诺。

## 开启与故障行为

`USAGE_METERING_ENABLED` 默认 false：采集不连接数据库，现有业务行为不变。启用后，任务开始前计量不可写会拒绝该次工作；持久业务与事件在同一事务内失败回滚。流已经开始或真实执行已结束时，终态写失败不能破坏原生清理；保留稳定元数据等待下一次真实写入重试，并暴露 degraded 状态。内存补记队列不能承诺跨进程恢复。

开发 Compose 将开关传给后端和 batch worker；MD 启动脚本传递同一开关。DFT 由后端持久 repository 采集。共享 `task_usage` 包同时复制进后端和 MD 镜像，主机 worker 从仓库 PYTHONPATH 导入。不要只启用 API 而遗忘参与写入的 worker。

## 0020 数据库变更与发布门禁

本阶段只写代码并运行独立、合成数据库测试，**不对当前开发库或生产库执行迁移，不部署、不重启、不推送**。

`0020_usage_metering.sql` 是 epoch 5 expand，精确依赖已批准的 0012、0018、0019。新增 `usage` schema、events/attempts 两表、索引、三个 SECURITY INVOKER 函数和相关 triggers；不修改任何既有业务表、auth.users 权限、用户或角色成员关系，没有 SECURITY DEFINER。

新对象权限范围：

* `nexpoly_api` / `nexpoly_service`：schema USAGE；events SELECT/INSERT；attempts SELECT/INSERT/UPDATE，但直接改投影被 trigger 拒绝，只允许事件派生写入；三函数 EXECUTE。
* `nexpoly_mutable_audit`：schema USAGE 及两表 SELECT。
* PUBLIC：撤销新 schema／表／函数权限。两表强制 RLS；API 限本人，service 保持可信后台跨 owner 写入，audit 只读。

启用前必须批准上述新权限范围，并核实环境隔离、无运行任务、可恢复备份和版本配套。运行时只接受精确 0019 或本版本 manifest 中的精确 0020；启用计量时必须有 0020，并校验真实 schema/RLS/权限，未知迁移或 checksum 漂移仍拒绝。历史身份迁移命令保持原严格版本边界。备份工具对 0020 单列已知版本分支并核验账本内容；原 0017/0018/0019 的验收标准不变。

未启用计量的现有 0019 运行检查应显式使用 `--schema-target user-isolation-0019`；CLI 默认 strict target 跟随最新 manifest，会要求 0020，不应为此放宽检查。

普通新库 bootstrap 仍停在单独审批的身份迁移之前，不绕过 0018/0019；只有精确 0019 已存在时，标准 expand 流程才可应用 0020。

回滚优先关闭计量开关并保留新账本，用兼容 0020 的代码恢复业务。旧 main 严格要求 0019，不能在已含 0020 的数据库上直接换回旧镜像宣称回滚成功。不得为回滚删除账本或改写迁移记录；若需恢复旧数据库，须另行核准完整恢复方案及恢复点之后的数据处理。

## 验证

本轮验证由不同边界的测试组成，不能表述为 21 模块全部真实端到端运行成功：

* 真实 PostgreSQL 使用临时实例、合成账号与任务，验证 0017→0018→0019→0020 链、账本并发／重放／权限防篡改、RLS、查询、事务回滚及 MD／DFT／batch／online 原生 repository。repository 生命周期测试未派发科学 worker 计算。
* Python 单元／回归测试验证事件上下文、取消／终态回调、故障补记、开关和状态映射；供应商响应及流为 mock，没有据此验证付费供应商实际返回 token 的完整性。
* 个人面板验证使用 fixture／模拟接口数据，覆盖展示、筛选和身份隔离行为；它不等于真实用户会话经生产后端和数据库的完整链路验证。
* 没有在本轮运行库启用 0020 或计量；未做全部科学模型／真实 GPU 任务／付费接口的计量端到端验收，也未证明硬崩溃、ACK 丢失后的自动对账完整性。

隔离测试采用 network none、合成数据和资源上限，并核验临时资源清理。本轮源码哈希、原始测试结果保留在工作树 `.runtime/usage-*` 等证据目录；交付应按各套件最终结果列示，不能用路由分类数量代替执行覆盖率。
