# 个人结构库与跨模块导入

## 用户行为

六个共用画板页面提供“保存结构”“我的结构”。保存是一张完整画板的不可变快照，包含原始业务 SMILES、KET 坐标和标签；多片段不拆分。同一结构可以保存多次。记录可重命名、删除，编辑后通过另存生成新记录。删除需要确认，不影响已复制到模块的值。

各输入的“选择结构”默认展示账号下已保存记录，不预选。用户也可明确选择“当前画板”。只有确认导入才修改目标输入，不会开始计算。列表提供名称/SMILES 搜索、分页、静态缩略图、时间。确认时重新读取详情，避免导入其他窗口已删除的记录。

| 接入位置 | 导入影响 |
| --- | --- |
| 工作台、数据库查询、均聚物预测、相似性探索、条件生成、Tg 逆向设计 | 恢复共享画板的完整 KET 与 SMILES |
| 单体聚合 A/B | 仅修改选择入口对应的单体 |
| 聚合物 MD、单体 MD 快速演示 | 修改模块自身结构与预览 |
| 单体 MD 正式模拟 | 按稳定行 ID 修改指定组分的 SMILES，保留名称、配比，仍需应用配置 |
| 单体逆合成 | 修改独立目标；工作台快捷操作只导航 |
| DFT | 修改独立表单；手输、标准化不回写画板 |
| PolyTAO | 修改独立参考快照；预览、复制、描述符均使用该快照 |

DFT 表单与 PolyTAO 参考/描述符草稿在当前账号会话内跨页面保留；刷新不承诺保留草稿。结构库记录保存在 PostgreSQL，同账号重新登录或其他浏览器可访问。游客画板不会自动上传。独立输入显示导入时的来源名称；手动修改 SMILES 后移除来源标记。

PolyTAO 选择结构与提取描述符分开操作。更换参考会清除从旧结构提取的描述符；手填和示例向量保留。已过期的计算响应不会覆盖新参考或后续编辑。

## 快照与并发约束

- `workspace.captureDocument()` 是严格捕获接口。画板控制器先完成文本防抖同步，再从同一稳定修订导出 KET 与 SMILES。导出复用现有 Molfile 路径，清除瞬态 `selected`，保留布局、注释和化学信息。
- 无效草稿、空图、KET 导出失败、编辑器超时、持续编辑以及聚合连接点显示回退均不能静默保存旧值。命名窗口持有固定快照，保存期间的后续画图不改变上传内容。
- `restoreDocument()` 先备份原画板和文本错误状态；写入期间阻止其他编辑器写入。KET 写入、稳定和标准化比对成功后才提交工作区。异常或超时退休原 SDK 实例，以备份重新挂载，阻断迟到写入。
- 未记录变化由当前完整快照与最近保存/载入快照比较，包含布局变化与无效草稿。替换时可“先保存再载入”“直接载入”“取消”。KET 失败后仅在用户再次确认时按 SMILES 重建布局。
- 请求使用现有 `privateFetch`、CSRF 与会话上下文。退出/换号关闭窗口、取消请求、清除内存草稿。独立表单还检查目标修订、组件生命周期；正式 MD 的行/协议变化使原选择失效。
- 选择器使用静态 SVG，不创建额外 Ketcher；支持焦点约束、Escape、返回触发点和移动端全屏面板。

## 服务合同

前缀 `/api/v1/structure-library`，全部要求登录：

| 方法 | 路径 | 含义 |
| --- | --- | --- |
| GET | `/status` | 功能开关、数据库就绪与容量上限 |
| GET | `/records?q=&limit=&cursor=` | 创建时间和 ID 倒序游标分页，默认 20、最多 100，不传 KET |
| POST | `/records` | 保存名称、SMILES、KET 对象、格式版本、编辑器版本、`client_request_id` |
| GET | `/records/{id}` | 完整记录 |
| PATCH | `/records/{id}` | 仅重命名，要求 `metadata_revision` |
| DELETE | `/records/{id}` | 删除本人记录 |

名称去首尾空白后为 1–100 字符；SMILES 最多 8000 字符。保存重试复用请求 ID，同 ID 同内容返回原记录，内容不同返回 409。重命名版本冲突返回 409；无效结构 422；KET/请求过大 413；配额不足 429；不存在和其他账号的 ID 均返回 404。

后端 RDKit 校验 SMILES 并生成可选缩略图，原始 SMILES 独立保留。后端检查 KET JSON、根节点、引用、深度和大小，但不声称证明 KET/SMILES 的化学等价；正常捕获与画板恢复分别检查一致性。缩略图生成失败不阻止保存。

`structure_library.records` 使用账号外键、FORCE RLS 和显式 owner 条件。API 只拥有 SELECT/INSERT/DELETE 以及元数据列 UPDATE；审计角色只读；Worker 无访问权限。运行期检查精确 RLS、表/列/Schema 权限，拒绝漂移。每个账号通过事务 advisory lock 串行检查配额、幂等和写入。容量按持久化内容估算，预留最长 UTF-8 名称空间，防止改名绕过配额。

| 配置 | 默认值 |
| --- | --- |
| `STRUCTURE_LIBRARY_ENABLED` | `false` |
| `PRIVATE_STRUCTURE_LIBRARY_MAX_ROWS` | `1000` |
| `PRIVATE_STRUCTURE_LIBRARY_MAX_BYTES` | `67108864` |
| `PRIVATE_STRUCTURE_LIBRARY_KET_BYTES` | `1048576` |

开关关闭保留记录，仅拒绝访问功能；不会清理用户数据。请求体总上限 2 MiB。保存日志包含请求 ID、状态和耗时，不输出结构内容；浏览器失败在原窗口/画板显示。

## 迁移、发布和回退

新增 `0020_structure_library` 是 expand 迁移，manifest 依赖校验包含 0018/0019。没有修改既有 SQL。全新库 bootstrap 仍先停在身份初始化前，完成原有 0018/0019 流程后才应用 0020。

结构库独立发布的历史运行边界为 checksum 完全匹配的 0019 或 0020；审计版本 1/2/3 仍分别固定到 0018/0019/0020，旧版本不会查询结构库。合并用量与权益后，当前构建还支持完整的 0021 用量、0022 权益前缀，审计版本 4/5 分别对应它们，`user_isolation_audit.py` 默认版本为 5；审计历史 0019、0020 必须分别显式传 `--schema-version 2`、`--schema-version 3`。应用启动、开发容器健康检查和开发启动脚本使用 `user-isolation-current`，解析已安装的精确 0019/0020/0021/0022 前缀，再执行对应账本、角色、RLS 和 ACL 检查；后续前缀仍检查结构库，未知版本拒绝启动。显式预检目标 `user-isolation-0020` 以及历史 `user-isolation-0018`、`user-isolation-0019` 保持原用途。Worker 预检只检查结构库元数据，不读取个人记录。

采用维护窗口部署：

1. 准备支持 0020 合同、关闭功能开关的回退构建；暂停入口和写入，按现有 `app.auth.backup` 流程创建完整备份并在隔离空库验证恢复。
2. 确认管理员迁移连接指向目标环境，使用新版代码执行 `PYTHONPATH=backend python -m app.postgres_migrations --mode expand`。连接变量按现有部署环境注入，不把凭证放到命令行或文档。
3. 启动新版应用，先保持开关关闭。执行 `PYTHONPATH=backend python -m app.postgres_preflight --mode schema --strict --service-context --schema-target user-isolation-0020`。
4. 使用既有 API/Auth/Service/Audit 独立连接配置执行 `PYTHONPATH=backend:. python scripts/user_isolation_audit.py preflight --schema-version 3 --output <new-private-report>`，并执行 `capture` 生成新版只读审计证据。
5. 显式开启 `STRUCTURE_LIBRARY_ENABLED=true`，重启应用。验证 `/status` 可用，以及保存 A/B/C、画板 C 导入 A/B、刷新重登录、跨账号不可见、失败保留原图，然后恢复入口。

旧应用精确限定 0019，不能在其承接流量时直接追加 0020。回退使用已适配新账本的构建并关闭开关；不回滚 SQL、不删表。完整备份摘要 `backup_state` 已纳入结构库，pg_dump/restore 保留结构及身份关系。静态重建白名单及冻结 v7 证据协议保持历史范围；0020 发布额外要求 v3 私有数据审计和完整备份，不可用旧 v7 证据替代。

## 验证入口

- 前端：`cd frontend && npm test && npm run build`。
- 浏览器：构建后在 `127.0.0.1:5187` 启动 preview，运行 `npm run test:structure-browser`；真实 Ketcher，账号/结构库/计算 API 使用隔离 fixture，不提交真实计算任务。
- 后端：为隔离 PostgreSQL 16 配置 `ISOLATION_TEST_ADMIN_DSN`，运行 `PYTHONPATH=backend:.:scripts pytest backend/tests/test_structure_library.py`；配合原有 auth、service privilege、multiuser/restore 与 release-contract 测试。
- 新测试自动纳入后端 CI 分片，mandatory multiuser gate 禁止结构库测试被跳过。已有前端 CI 自动包含新的 Vitest 测试；`test:structure-engines` 调用扩展后的浏览器脚本，覆盖保存、A/B 请求、刷新后的布局/标签恢复、失败回滚、移动端焦点和 DFT 独立性。

不包含自动历史、团队共享、选区保存、分子拆分、文件夹、标签、批量文件导入改造、回收站或普通草稿的跨刷新持久化。
