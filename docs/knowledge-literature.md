# 已提取文献接入知识检索

首期在现有「本地知识库」页面提供两个独立来源，默认仍为现有知识库。已提取文献支持题名、关键词、摘要和 DOI 检索，按样品查看性能、工艺、原料和表征，再读取可核验的原文。结果明确标为提取结果，成功提取不等于科学内容已经审核。

## 数据和代码边界

- 候选入口为源 `public.dwd_paper` 及通过 `paper_id` 关联的提取记录。ODS 和未匹配 DWD 的 PDEE 文献不增加候选范围；PDEE 只提供已收录论文的文件线索。
- 独立 CLI 使用 `LITERATURE_SOURCE_DSN`，连接时即设置只读事务，全部 SELECT 共用 REPEATABLE READ 快照。在线 API 从不读取源 PostgreSQL，也不替换 `APP_POSTGRES_DSN`。
- 平台三表位于现有 `knowledge` schema：`literature_generations` 保存构建报告和原子发布指针，`literature_catalog` 保存按代的检索投影，`literature_snapshots` 保存不可变提取及资产清单。
- DOI 仅去首尾、前缀和转小写；合法 SICI DOI 中的分号、括号、尖括号保留。每 DOI 一张卡，源 paper ID 全部保留。不同 DOI 不按题名合并。
- 规范化器只使用选定的单份 raw JSON，不通过共享材料关联补齐其他论文的参数。中英文适配、空模板排除、DOI 冲突、零值、范围、误差、多条位置依据和未知字段保留均有测试。新样品和条目 UID 绑定快照与 JSON 路径。
- `normalized_json` 是阅读投影；`raw_json` 保留审计依据。两者分别限制 2 MiB，超限排除并报告。大整数来源 ID 在接口中是字符串。
- 当前导入解析器为 `literature-v2`，保留只有实际收率的工艺（包括 0%）。读取兼容 `literature-v1`、`literature-v1.1` 的历史快照；规则改变通过新版本产生新快照，不覆盖历史批次。
- 原知识导入器和旧静态重建器继续操作原表，不执行新文献表的清空或重建。

主实现：`backend/app/services/literature_normalizer.py`、`literature_source.py`、`literature_repository.py`、`literature_assets.py` 和 `backend/app/import_literature.py`。前端位于现有知识检索组件，文献详情为 `LiteratureDetail.tsx`。

## 配置与权限

在线后端配置见 `backend/.env.example` 和 `ops/config/app.env.example`：

| 环境变量 | 用途 |
| --- | --- |
| `KNOWLEDGE_LITERATURE_ENABLED` | 默认 false；仅启用文献来源 |
| `LITERATURE_MINIO_ENDPOINT` | MinIO API 地址，支持明确的 http/https 前缀 |
| `LITERATURE_MINIO_ACCESS_KEY` / `LITERATURE_MINIO_SECRET_KEY` | 对登记文件具有读取权限的专用身份 |
| `LITERATURE_MINIO_SECURE` | 不带协议的 endpoint 是否使用 TLS，默认 true |

同步进程单独配置 `LITERATURE_SOURCE_DSN` 和 `LITERATURE_IMPORT_DSN`。不要把源 DSN 放入 Web 后端的环境文件。源账号只授予所需 DWD/PDEE 目录的 SELECT；MinIO 只需要读取对象元数据和对象。凭证通过权限为 0600 的运维文件或凭证管理器注入，不写进命令参数、报告、仓库或浏览器。

离线同步进程可单独设置 `LITERATURE_SOURCE_STATEMENT_TIMEOUT_MS`：默认 45000 毫秒，仅接受 1000–300000 范围内的 ASCII 整数字符串，空值、单位、符号、空白和越界值在连接源库前拒绝。全量数据或受控代理链路超过默认预算时，在私有离线导入环境中显式提高，最高 300000 毫秒；实际值记入 `source_summary.source_statement_timeout_ms`。该变量只影响源库每条 SQL 的执行预算，不改变只读、REPEATABLE READ 或 2000 毫秒锁等待门槛，也不影响目标库及 MinIO；不要注入在线后端环境。

0024 创建 `nexpoly_literature_import` NOLOGIN 权限组。运维单独创建 LOGIN 并加入该组：可构建新目录、插入快照及切换批次，不能修改快照或已发布目录。在线 `nexpoly_api`、`nexpoly_service` 对三表只有 SELECT；审计组有 schema USAGE 和 SELECT。不要让 API 账号继承同步组。备份仍使用既有受控 owner 身份。

开发 Compose 的 `.env.dev.ai`、生产的受控 `app.env` 可注入在线变量；它们不授予迁移或同步权限。执行任何维护前先检查 Compose 项目、解析后的配置以及目标数据库身份。

## 兼容迁移与发布顺序

`0024_literature_projection` 是 epoch 9 的 expand，checksum 为：

```text
2a5681e4b5000ed9cbf6ec88e1d8d7e6c6c5d0faf82270e802a3a9abb2060cf3
```

SQL、manifest、`app.literature_contract` 与审计工具中的登记必须一致。已有迁移不修改。首次 bootstrap 仍停在 identity prepare；0018/0019 必须先通过既有显式身份维护完成，并保留完整的 0001–0023 账本，之后才能执行 0024。0020–0023 分别属于结构库、用量、权益和聚合物 MD，不能省略。不要用 fresh bootstrap 绕过已有实例的维护要求。

分两次交付应用：

1. 先发布兼容包，文献保持关闭，且不执行 0024。兼容包至少包含 `app/literature_contract.py`、`postgres_preflight.py`、`auth/isolation_ledger.py`、`auth/cutover.py`、`auth/backup.py` 与对应审计兼容改动。兼容桥接仅允许完整的 0023 账本扩展到唯一登记的 0024；历史身份回执仍各自要求精确账本，不能从 0019 直接追加 0024。缺项、checksum 漂移和未知版本继续拒绝。确认所有应用、审计和恢复验证节点均已更新。
2. 再发布含 0024 SQL、manifest 和维护入口的版本，在明确的 dev 栈按下节执行 `--mode literature-dev-0024`。普通 `expand` 始终跳过 0023/0024，不能用于文献启用；该维护入口也不能用于生产。功能仍关闭。旧生产 v7 Pull 控制器会拒绝跨 0017 认证边界；不修改或绕过这一门禁。
3. 用独立同步身份 prepare，审核收录/异常报告，再 publish。启用在线开关，检查 `/api/v1/knowledge/sources`。
4. 验证旧知识检索、登录、浏览记录、总结以及新来源，再恢复常规流量。保留应用版本、数据库身份、迁移回执、批次和报告指纹。

兼容包是之后代码回滚的最低版本。功能回滚优先关闭开关，数据回滚切换历史 generation，不删表、不删除快照、不改旧 migration 账本。

### 显式开发库维护

先封闭提交入口、排空任务和租约，正常停止 Backend、batch、monomer MD/DFT、polymer MD 和文献同步进程；未部署的组件也要明确登记。使用受控 owner 身份运行 `app.auth.backup`，完成私有整库 dump、空库恢复、数据及资产校验。不要复用运行中取得、之后又发生写入的备份。数据库/MinIO 密钥只通过私有环境注入，不放入命令参数。

维护证据和备份回执均为当前操作者拥有、无链接、单硬链接的 0600 JSON 文件。维护证据格式为：

```json
{
  "version": 1,
  "purpose": "literature-dev-0024",
  "database_identity": {
    "database": "DEV_DATABASE", "database_oid": 0,
    "host": "SERVER_ADDRESS", "port": 5432
  },
  "backup_receipt_sha256": "SHA256_OF_RECEIPT_FILE",
  "writers_stopped": true,
  "submission_disabled": true,
  "active_jobs": 0,
  "active_leases": 0,
  "writer_states": {
    "backend": "stopped", "batch": "stopped",
    "monomer_md": "stopped", "monomer_dft": "stopped",
    "polymer_md": "not_deployed", "literature_import": "not_deployed"
  }
}
```

`database_identity` 必须来自目标连接上的 `app.auth.isolation_ledger.database_identity`，不能手填示例值或仅凭数据库名匹配。`writer_states` 逐项记录实际正常停服/未部署状态；这些是运维声明，命令不会代为停服。命令另行锁定业务、用量和权益表，重新检查真实活动任务/租约为零，以及备份完整数据、账号、用量、权益和资产 seals；任何漂移都要求重新备份恢复验证。

`usage.attempts` 是用量观察投影，不是执行状态权威。历史缺少终态的 queued/running 观察保持原值，完整锁定和 seal 后仅将数量记入迁移审计，不为维护补造终态。停止 Backend 时还必须确认 conditional generation、PolyTao 等内存任务没有活动请求或执行进程；原生任务、租约和权益 operation 的零活动门槛不变。

在锁定依赖环境、backend 目录运行；`AUTH_ADMIN_POSTGRES_DSN` 指向明确的开发库 owner，且具备受控角色创建权限：

```bash
POLYMER_MD_ENVIRONMENT=dev python -m app.postgres_migrations \
  --mode literature-dev-0024 --dsn-env AUTH_ADMIN_POSTGRES_DSN \
  --backup-receipt /private/backup/receipt.json \
  --maintenance-evidence /private/maintenance.json --writers-stopped \
  --audit-output /private/migration-result.json
```

该命令仅接受完整、精确校验和的 0022 或 0023 前缀：从 0022 时在同一事务内追加原始 0023 和 0024，从 0023 时仅追加 0024。持有既有 0023 advisory lock 与迁移账本锁，最终验证隔离、服务权限、PMD schema 和文献列/键/索引/不可变触发器/ACL；任一步失败则两项 DDL、角色及账本一起回滚。已在 0024 时仍要求该状态下的新备份/维护证据，验证后返回 no-op。失败保留关闭的入口，审计文件只保存结果或错误类型，不输出 DSN。

已有 `nexpoly_literature_import` 集群角色或未登记的文献对象必须拒绝并单独审查来源；不自动接管、不改历史 SQL/checksum、不跳过校验。迁移创建的是 NOLOGIN 组，之后单独配置同步 LOGIN，在线身份不得继承该组。最终完整 0024 库的健康检查使用 `literature-0024`，同时验证已存在的 PMD schema；固定要求精确 0023 的健康检查不能用于该组合。

迁移成功不代表文献已发布或 PMD 已允许提交。继续按下面的导入步骤生成/审核/发布批次，再开启在线功能。旧开发应用如果只识别 0022，不能直接切回；要么保留兼容 0024 的应用并关闭功能，要么在受控停机状态恢复升级前整库及同一切点的资产。

## 手动同步

在 backend 目录、项目锁定依赖环境中运行。下面的 DSN 均从受控环境注入：

```bash
python -m app.import_literature dry-run --report /private/literature-dry-run.json
python -m app.import_literature prepare --verify-assets --report /private/literature-prepared.json
python -m app.import_literature publish GENERATION_UUID
python -m app.import_literature rollback PREVIOUS_PUBLISHED_GENERATION_UUID
```

`dry-run` 不写平台业务表。`prepare` 先登记 building，完成后 ready；失败记为 failed，当前发布批次不变。内容、解析器、身份或资产版本变化产生新快照，相同内容复用既有不可变记录。publish/rollback 使用短事务及同一 advisory lock，最多一个 active。部分扫描 `--limit N` 只用于开发；其发布必须额外提供 `--allow-partial`，避免误替换全量库。

报告核对：`ready_documents + quarantined_documents = candidate_doi_groups`。每个入选只使用最新合格提取；有回退时记录跳过原因。无 paper_id 的源提取单独报告，不自动收录。发布数以当次报告为准，不把源候选行数作为文献数。

不配置定时任务。变更收录规则时先在 dry-run 对照异常，再构建新批次。不要逐行修补已发布目录。

大批构建或历史批次数量变化后，由平台库 owner 执行三表的 `ANALYZE` 并复核查询计划及实测性能。同步账号和在线账号不为此增加写权限；不修改全局 planner 开关。备份和维护期间同时停止手动同步，避免构建/发布与备份边界交错。

可选 `--verify-assets --verify-extraction-input` 会读取已登记的 PDEE 提取结果 JSON，计算与所选 DWD raw JSON 相同的 canonical SHA-256，仅唯一匹配且完整的 convert/preprocess 对象键链成立时确认精确输入。额外 GET 分别限制 2 MiB，失败或多候选保持未验证，不影响其余文献收录。首次发布可先保留论文级原文，之后通过新 generation 增加已验证的输入证据。

## API 和版本约束

本地知识检索保留“本地知识库 / 在线文献 / PDF 相似度”三个入口。检索词右侧的“条件设置”弹窗可勾选现有专利知识库、提取文献库，默认仅选择专利库。可同时检索两库，结果按来源分别显示数量、排序和页码，切换结果来源不会重新查询或丢失另一来源的页码。

弹窗提供每库每页 20、50、100 条，以及仅对提取文献生效的起止年份、有性质信息、有工艺信息、有可用原文条件。三个下拉统一使用平台 `WorkbenchSelect`，年份菜单最多约 208px（约六行），在菜单内滚动并自动避让弹窗边界；保留“不限”及 1900 年至当前 UTC 年份的完整范围，按年倒序，上界逐年更新。弹窗 portal 显式接入共享工作台主题，沿用平台颜色、边框、圆角及焦点样式。起止选项相互限制，起始年不得晚于结束年，同年允许；旧设置若越界或倒置会明确提示并阻止应用。起止均为“不限”时包含未标年份文献，设置任一端点后只匹配有年份且满足范围的文献。API 仍保留 1000–3000 的兼容约束及起止顺序校验。未勾选的内容条件不作限制；“可用原文”指已入库验证的全文或 PDF，不等于实时对象存储健康检查。专利库继续使用关键词、IUPAC、配方、标题、权利要求和摘要检索，不接收文献筛选参数。

进入页面时自动获取两个库的可用状态和条目数，条件弹窗只展示状态，不提供手动“重新检查来源”选项。

“应用条件”保存设置并清空旧结果，随后点击“运行检索”；取消、关闭或 Escape 丢弃弹窗草稿。分页使用已提交的检索词和条件，文献同时冻结 `generation_id`；未应用的草稿不影响当前结果。来源离线时明确显示不可用，不将故障展示为零条结果；双库请求失败互不覆盖。

`POST /api/v1/knowledge/search` 新增 `data_source=extracted_literature`、`generation_id` 和年份/内容能力筛选，原请求默认 legacy。仍需检索词；不混合来源排序。文字只匹配题名、关键词、摘要，按 4/2/1 权重；组间 AND、组内 OR，每组每字段至多计一次分。完整 DOI 先精确匹配，不扩词、不拆 SICI 分号。排序为相关性、年份、文献 UID。

| 路径 | 说明 |
| --- | --- |
| `GET /api/v1/knowledge/sources` | 开关、独立 readiness、当前 generation、发布时间和数量 |
| `GET /api/v1/knowledge/literature/{document_uid}` | 整篇元数据、选定样品、来源和安全资产清单 |
| 同路径 `/fulltext` | 游标读取正文，默认 100 块，每次最多 200 块 |
| 同路径 `/files/{asset_uid}/content` | GET、HEAD、单段 Range；`download=true` 强制下载 |

后三者必须同时传 `generation_id` 和 `snapshot_uid`，服务端验证目录归属。旧 generation 保留后，发布切换不会改变已提交搜索、分页、详情或录制。文件 ID 必须存在于该快照清单，客户端不能提交 bucket/key 或 MinIO URL。接口错误有稳定 `LITERATURE_*` 机器码；外部故障不伪装成零条搜索结果。

可选 readiness 检查新表、在线组的 SELECT 和禁止写入、当前发布及支持的解析器版本。开关关闭时不读取新表；文献未就绪不关闭旧知识库。

原生文件新窗口带当前公开会话上下文 ID，仍须通过 Cookie 认证和一致性校验；该兼容只限精确文件路径的 GET/HEAD。切换账号后旧链接不能沿用新会话读取。其他私有 API 继续要求会话请求头。

## 原文和提取依据

清单区分已确认论文资产与已验证提取输入。仅 DOI 相同、HEAD 存在、ETag 相同，都不足以证明是某次提取输入。多候选正文不任取第一份；未验证或模糊对象禁用读取。HEAD 冻结 ETag、size 和可用 version ID，访问时再次核对；ETag 始终作为不透明版本标识，不解释为 MD5。变化返回 409，缺失 404，超时 504，不可用 503。

文件流经平台，不向浏览器暴露存储凭证或内部对象键。支持单段 Range、HEAD、私有缓存头，并在完成/取消时释放上游连接。PDF 原生打开，其他附件下载。

正文 JSON 限制 32 MiB。保留块顺序，表格经受控解析转换为纯文本单元格；普通正文和公式按文本呈现。源 HTML、脚本和远程资源不会执行。精确块跳转必须额外证明唯一提取内容指纹与完整输入对象键链；未证明时仍可查看摘录，但不显示精确跳转。

## 浏览记录与总结

新结果 `knowledge_id=null`，使用字符串 `document_uid + generation_id + snapshot_uid`；旧整数 ID 和深链保留。观察要求当前用户、search_id、搜索结果成员及版本一致。主动打开、重开、切换提取/原文页签才记事件，自动展开或预加载不计。

新文献总结证据由服务端白名单生成，仅含题名、摘要、作者、期刊、年份、DOI 和固定引用及查看事件。实验值、全文、完整提取 JSON 和 reaction_info 不进入新文献总结。内存配额、证据容量、失效和用户隔离沿用现有实现。

## 备份、恢复和验收

使用既有 `python -m app.auth.backup` 的私有全库 dump 和空库恢复验证；0024 在有限兼容账本范围内。`backup_state_seal` 包括三张新表的内容摘要。MinIO 远端文件不复制进数据库备份，需对象存储独立备份/版本保留；数据库清单冻结的对象丢失会明确报错。

`scripts/user_isolation_audit.py --schema-version 5` 的维护回执仍绑定完整 0022 账本，不能作为 0024 的切换入口，也不能改写历史回执以绕过版本检查。0024 使用上述全库备份恢复回执、显式 `literature-dev-0024` 维护命令，以及 `python -m app.postgres_preflight --strict --schema-target literature-0024 --service-context` 的运行权限检查；历史证据保持原形状。

测试均使用隔离 PostgreSQL 和受控对象存储替身，源库没有写入。回归覆盖身份、解析、发布/回滚、归属、故障/Range/取消、录制白名单，以及旧知识 API。前端覆盖来源切换、提交条件分页、样品和工艺分离、安全表格、离线及过期响应。执行 TypeScript/Vite 构建；页面检查 1440×900、1024×768、390×844。

真实数据验收使用 DOI `10.1039/c5ra14526k`：PI-1～PI-6 分开，PI-1 Tg 264 °C，DSC、氮气、20 °C/min 和 ODPA/M1/NMP 用量保持原始样品归属。性能验收另记录数据量、并发、请求数和 p95；目标搜索 ≤1 秒、提取详情 ≤2 秒，正文/文件单独计时。

本次开发验收记录见 `knowledge-literature-acceptance.md`。实际生产发布需要使用经审核的不可变应用制品与上述维护流程；工作区实现不等于生产已启用。
