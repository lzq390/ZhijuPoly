# 已提取文献接入知识检索

首期在现有「本地知识库」页面提供两个独立来源，默认仍为现有知识库。已提取文献支持题名、关键词、摘要和 DOI 检索，按样品查看性能、工艺、原料和表征，再读取可核验的原文。结果明确标为提取结果，成功提取不等于科学内容已经审核。

## 数据和代码边界

- 候选入口为源 `public.dwd_paper` 及通过 `paper_id` 关联的提取记录。ODS 和未匹配 DWD 的 PDEE 文献不增加候选范围；PDEE 只提供已收录论文的文件线索。
- 独立 CLI 使用 `LITERATURE_SOURCE_DSN`，连接时即设置只读事务，全部 SELECT 共用 REPEATABLE READ 快照。在线 API 从不读取源 PostgreSQL，也不替换 `APP_POSTGRES_DSN`。
- 平台三表位于现有 `knowledge` schema：`literature_generations` 保存构建报告和原子发布指针，`literature_catalog` 保存按代的检索投影，`literature_snapshots` 保存不可变提取及资产清单。
- DOI 仅去首尾、前缀和转小写；合法 SICI DOI 中的分号、括号、尖括号保留。每 DOI 一张卡，源 paper ID 全部保留。不同 DOI 不按题名合并。
- 规范化器只使用选定的单份 raw JSON，不通过共享材料关联补齐其他论文的参数。中英文适配、空模板排除、DOI 冲突、零值、范围、误差、多条位置依据和未知字段保留均有测试。新样品和条目 UID 绑定快照与 JSON 路径。
- `normalized_json` 是阅读投影；`raw_json` 保留审计依据。两者分别限制 2 MiB，超限排除并报告。大整数来源 ID 在接口中是字符串。
- 当前导入解析器为 `literature-v1.1`，保留只有实际收率的工艺（包括 0%）。读取兼容 `literature-v1` 的历史快照；规则改变通过新版本产生新快照，不覆盖历史批次。
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
2. 再发布含 0024 SQL、manifest 和功能代码的版本，在明确的 dev 栈先验证，然后沿用身份隔离后的生产维护流程执行 `python -m app.postgres_migrations --mode expand`。功能仍关闭。旧生产 v7 Pull 控制器会拒绝跨 0017 认证边界；不修改或绕过这一门禁。
3. 用独立同步身份 prepare，审核收录/异常报告，再 publish。启用在线开关，检查 `/api/v1/knowledge/sources`。
4. 验证旧知识检索、登录、浏览记录、总结以及新来源，再恢复常规流量。保留应用版本、数据库身份、迁移回执、批次和报告指纹。

兼容包是之后代码回滚的最低版本。功能回滚优先关闭开关，数据回滚切换历史 generation，不删表、不删除快照、不改旧 migration 账本。

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

使用既有 `python -m app.auth.backup` 的私有全库 dump 和空库恢复验证；0024 在有限兼容账本范围内。`backup_state_seal` 包括三张新表的内容摘要。MinIO 远端文件不复制进数据库备份，需对象存储独立备份/版本保留；数据库清单冻结的对象丢失会明确报错。`scripts/user_isolation_audit.py` 在登记 0024 后附加 `literature_tables` 指纹和 knowledge schema 备份要求，历史证据保持原形状。

测试均使用隔离 PostgreSQL 和受控对象存储替身，源库没有写入。回归覆盖身份、解析、发布/回滚、归属、故障/Range/取消、录制白名单，以及旧知识 API。前端覆盖来源切换、提交条件分页、样品和工艺分离、安全表格、离线及过期响应。执行 TypeScript/Vite 构建；页面检查 1440×900、1024×768、390×844。

真实数据验收使用 DOI `10.1039/c5ra14526k`：PI-1～PI-6 分开，PI-1 Tg 264 °C，DSC、氮气、20 °C/min 和 ODPA/M1/NMP 用量保持原始样品归属。性能验收另记录数据量、并发、请求数和 p95；目标搜索 ≤1 秒、提取详情 ≤2 秒，正文/文件单独计时。

本次开发验收记录见 `knowledge-literature-acceptance.md`。实际生产发布需要使用经审核的不可变应用制品与上述维护流程；工作区实现不等于生产已启用。
