# 用户管理、资产隔离与任务控制

本文说明账号接口、私人资产隔离、任务控制和开发运维配置。MD、DFT、batch 保留各自的 Worker 环境、进程与业务状态表；生产发布仍需满足独立的迁移和上线门禁。

领域接入细节见 [MD / DFT 执行合同](user-isolation-worker-contract.md)，全部私人资产限制和 batch 清理约束见 [私有资产配额配置](private-asset-quotas.md)。

## 身份和接口

运维管理普通账号；不开放注册和网页管理后台。用户名规范化为 3–64 位小写字母、数字、点、下划线或横线。密码为 12–256 字符（UTF-8 最多 1024 字节），使用 Argon2id 哈希。创建和重置密码要求首次改密；改密、重置、禁用撤销全部旧会话，退出撤销当前会话。会话绝对有效期默认 12 小时。

| 接口 | 请求/结果 |
|---|---|
| `GET /api/v1/auth/session` | `{authenticated,user,session_id,csrf_token,capabilities}`；用户字段为 `{id,username,must_change_password}` |
| `POST /api/v1/auth/login` | `{username,password}`；返回新会话，设置 HttpOnly、host-only、SameSite=Lax Cookie |
| `POST /api/v1/auth/logout` | `{}`；成功 204 |
| `POST /api/v1/auth/password` | `{current_password,new_password}`；成功 204，并要求重新登录 |
| `GET /api/v1/monomer-polymerization/batch/jobs?offset=0&limit=20` | `{items:BatchJob[],total,next_offset}`；只含本人，最大每页 100 条 |

私人请求使用 `X-Session-Context`，写请求额外使用 `X-CSRF-Token`。这些值来自当前会话，不能从持久存储中复用。后端在业务解析前验证 Cookie、可信 Origin、会话上下文及 CSRF。401 表示需要登录，403 表示权限/操作限制，409 `session_context_mismatch` 表示旧标签页使用了新的 Cookie。前端在身份错误时重新验证。其他人的资源与不存在资源统一返回 404。

退出和改密以数据库会话撤销为准，不发送可能迟到的 `Delete-Cookie`，避免清除另一个标签页刚建立的 Cookie。残留 HttpOnly 令牌已不可用于认证，随后过期或由下次登录覆盖；私人页面和浏览器数据照常清除。

普通业务路由默认要求登录；公开例外为健康检查、会话查询、登录和固定批量模板下载。运维、隐藏实验模块和内部服务接口不以普通账号开放。首次改密完成前不开放私人业务。

## 数据与任务公共合同

六张私人主表包含不可空的 `owner_user_id`：知识历史、知识任务、MD 任务、DFT 任务、batch 导入、batch 任务。owner 由认证上下文创建，客户端不能指定或改变。历史去重和任务幂等在用户内生效。产物/attempt/chunk 继承父任务归属，下载、预览及结果操作都先验证父资源。

用户仓储按 owner 过滤，数据库 RLS 再限制访问。事务使用 `app.user_id`；缺少该上下文不得读取私人数据。角色职责分离：

| 权限组 | 用途 |
|---|---|
| `nexpoly_api` | 用户业务连接；无超级用户、BYPASSRLS、表所有权及服务/认证组继承 |
| `nexpoly_auth` | 身份表和可撤销会话 |
| `nexpoly_service` | 全局调度、合法后台回写、开始授权；不改变用户状态和密码 |
| `nexpoly_mutable_audit` | 受控全局审计；身份表不授权读取密码哈希和会话摘要 |

角色是 NOLOGIN 权限组，实际登录角色由运维单独配置。API 的数据库身份及 `0018` 切换状态不符合要求时，私人入口保持关闭。普通账号删除不级联删除科研资产。

共享模块负责身份、准入、启动授权、结束/释放和错误语义，领域表和既有 journal 继续作为状态来源，不建立第二套中央任务状态。队列、运行、取消收尾计入每用户通道名额；同一用户幂等重试复用原任务。不同通道和既有全局容量继续分别约束实际执行。

`TaskExecutionContext` 统一日志字段 `owner_user_id`、`request_id`、`task_type`、`channel`、`task_id`、`attempt_id`；记录准入、拒绝、提交后的启动授权和实际释放。后台恢复没有浏览器 request 时保留空值，通过领域任务关联；attempt 使用序号、chunk 或 Worker instance，不记录执行凭证、Cookie、密码、模型输入和结果。

“已开始”统一指**启动授权提交成功**，不能以各领域的 `running` 字符串判断。首次启动授权与账号禁用按一致顺序锁定用户。MD / DFT 创建的锁顺序为用户、领域准入、任务；batch 为领域准入、用户、任务，各自不嵌套其他领域锁。实际启动授权均先用户再父任务，账号禁用只先锁用户再撤销会话。禁用先提交则不允许新计算，授权先提交则允许原 owner 的任务安全收尾。等待 GPU 时不保持数据库事务；取得资源后授权失败须释放资源。

| 执行部分 | 授权存储/边界 |
|---|---|
| Backend 内存任务 | 本进程执行许可；原重启失效语义不变 |
| 在线知识、MD、batch | 原任务记录的 `start_authorized_at`；batch 后续分片和导出沿用首次授权 |
| DFT | 原 attempt 的 `start_authorized_at`；Worker 不接数据库 |

DFT Worker 使用服务令牌调用 `POST /internal/monomer-dft/jobs/{job_id}/authorize-start`，发送原 attempt token、请求摘要和入队序号。接口核验当前有效 attempt；授权响应丢失时重试同一 attempt，不能创建新的执行尝试绕过禁用。Backend/Worker 能力版本为 `start_authorization_version=1`；不配置或确认授权时不得计算。内部路径不得由公共反向代理转发。

在线检索、AI 和预检采用有界许可；Backend GPU 通道沿用 Broker/租约/fencing。普通退出不取消后台任务。SSE 会话撤销后停止发送，实际执行尚未结束时不提前释放额度。单 Backend 是本版配置前提，跨进程总额度和通用任务中心未纳入本版。

MD / DFT 清理、租约撤销或重启证据不明确时，保持原任务占额并关闭新执行；HTTP 错误和线程 Future 被取消均不能直接生成终态。batch 子进程继承同一本地存储目录的执行文件锁；替代 Worker 只有取得锁并清理 scratch 后才能清除旧执行凭证。无法证明清理时需要先恢复 Broker / 文件系统，再重启对应 Worker 重新核验。

## 浏览器行为

游客和普通用户使用同一个 `App`、导航、模块页面和工作区布局。`AuthProvider` 位于 App 外；身份加载、认证异常和首次改密状态不挂载工作区。游客确认后挂载共用界面，但私人数据读取、自动恢复、浏览录制和 Agent 桥接按身份关闭，OpenScience iframe 仍不挂载。登录后的 OpenScience 继续保持已确认的共享服务例外。

游客可浏览各模块和使用本地 Ketcher 画板、示例、复制及公开批量模板。点击真实查询、上传、预测、计算、在线知识、AI 或其他服务任务时，界面提示“请登录账号”并打开登录窗口，任务不得发出；统一私人请求层和后端认证另行拒绝匿名调用。界面一致不代表向游客开放底库或平台服务。游客编辑只使用本地结构引擎；登录前捕获本次游客画板，经明确选择导入后才交给新工作空间，不自动查询或计算。左下角账号菜单在游客状态提供登录，登录后在同处提供改密和退出。

侧栏 GPU 检测/恢复属于运维功能。按钮展示和状态轮询共同要求开发开关及 `operations.use` 权限；游客和普通用户均不展示、不请求该接口，避免停用轮询后仍显示“正在检测”或反复收到403。GPU 服务状态由受控运维入口查看，普通用户正常提交计算任务的权限不受此处按钮隐藏影响。

切号/退出时先关闭请求入口、递增身份代次并中止请求，再卸载私人页面、清除浏览器数据。清理覆盖 NexPoly/PolyProp 草稿和旧无主键、消息、最近记录、属性缓存、IndexedDB 缩略图及私人 URL 参数。正常单次会话可使用标签页存储；首次认领或身份变化不自动继承旧内容。

JSON、FormData、Blob、SSE 使用同一请求封装：请求开始、响应到达、解析完成、每次读取流数据都检查原身份。旧 401 不影响新账号。MD/聚合页面卸载保存、助手消息和缩略图异步保存另有身份代次检查，避免清理后重新写入旧数据。BroadcastChannel、storage 事件和焦点/可见性恢复用于同步身份；服务端上下文校验防止旧标签页操作套用新 Cookie。

登录、退出和改密必须在同一来源的所有标签页间串行执行。优先使用 Web Locks；普通 HTTP 不提供该 API 时，使用独立 IndexedDB 库 `nexpoly-auth-coordination` 的 readwrite 事务原子取得并持久保存占用标记。标记只含随机 ID，不包含账号、密码或会话凭证。没有 Web Locks 且浏览器存储不可用时，认证写操作拒绝执行并提示允许本站存储。随机 ID 使用普通 HTTP 可用的 `crypto.getRandomValues`，不依赖 `crypto.randomUUID`。

排队的认证操作可取消，取得锁后仍校验发起时的身份代次；请求发出后必须等待其实际响应结束再释放锁，防止迟到的 `Set-Cookie` 覆盖下一次登录。界面被卸载或身份改变时丢弃旧结果，不能据此提前释放已发送请求的锁。IndexedDB 占用标记没有到期抢占机制：页面冻结不会将锁转给其他页面；若页面在操作中途崩溃或关闭而留下标记，后续认证操作保持拒绝，并提示先关闭本站所有页面、清除此站点的浏览器数据，再重新打开。不能在旧页面仍可能继续请求时手动移除标记。

“我的批量任务”来自服务端分页，可在退出、清理本地数据后重新找到任务；文件过期只禁止下载，任务元数据仍显示。在线知识请求不再发送 `api_key`、`base_url`、`model` 或 `use_server_default`；平台配置由服务端决定。

## 开发配置与运维命令

后端以既有虚拟环境从 `backend` 目录运行。配置实际 DSN 时使用既有受控环境文件或运行环境，不把密码写入命令行、Git 或验收报告。至少分开以下连接：

```dotenv
APP_POSTGRES_DSN=<仅属于 nexpoly_api 的登录角色连接>
AUTH_POSTGRES_DSN=<仅属于 nexpoly_auth 的登录角色连接>
APP_SERVICE_POSTGRES_DSN=<属于 nexpoly_service 的登录角色连接>
AUTH_ADMIN_POSTGRES_DSN=<仅维护命令使用的管理连接>
AUTH_AUDIT_POSTGRES_DSN=<仅属于 nexpoly_mutable_audit 的登录角色连接>
AUTH_RESTORE_TEST_DSN=<明确指定的空隔离恢复数据库管理连接>
AUTH_COOKIE_NAME=nexpoly_dev_session
AUTH_COOKIE_SECURE=false
ALLOWED_ORIGINS=http://127.0.0.1:5173,http://localhost:5173
AUTH_DEV_HTTP_ORIGINS=
MONOMER_DFT_START_AUTHORIZATION_TOKEN=<由 Backend 与 DFT Worker 共享的内部服务令牌>
```

默认配置中，`AUTH_COOKIE_SECURE=false` 只允许 loopback 来源，可通过 SSH 隧道访问。需要通过非 loopback 地址访问开发环境时，显式配置完整 HTTP Origin；以下 `dev.example.test` 应替换为实际开发主机名：

```dotenv
GPU_BROKER_ENVIRONMENT=dev
AUTH_COOKIE_NAME=nexpoly_dev_session
AUTH_COOKIE_SECURE=false
ALLOWED_ORIGINS=http://localhost:9001,http://127.0.0.1:9001,http://dev.example.test:9001
AUTH_DEV_HTTP_ORIGINS=http://dev.example.test:9001
```

`AUTH_DEV_HTTP_ORIGINS` 中每一项都必须是端口 `9001` 的完整 HTTP Origin，并已在 `ALLOWED_ORIGINS` 中明确列出；不接受通配、路径、查询、片段或 URL 凭证。例外还要求 `GPU_BROKER_ENVIRONMENT=dev`、非 Secure Cookie 和以 `nexpoly_dev_` 开头的 Cookie 名称，配置不匹配时拒绝启动认证服务。Origin、CSRF、会话上下文、用户隔离和后端任务权限不因该例外放宽。HTTP 不提供传输加密，此配置仅适用于开发环境；生产继续使用 Secure Cookie、HTTPS 和独立 Cookie 名称，生产 Compose 的 `prod` 环境不能启用该例外。

前端通过同源 `/api/v1` 代理访问，代理保留浏览器实际 Host。DFT Worker 还需 `MONOMER_DFT_START_AUTHORIZATION_URL` 指向可信 Backend 根地址；DFT 内部明文 HTTP 回调仍仅允许 loopback，该要求不受开发网页 HTTP 例外影响。

开发 Compose 在 `docker-compose.yml`、`docker-compose.dev.yml` 后叠加 `docker-compose.user-isolation-dev.yml`，分别配置初始化迁移、API、认证和 batch 服务连接。角色示例见 [数据库角色模板](../ops/config/user-isolation-roles.sql.example)，环境示例见 [用户隔离环境模板](../ops/config/user-isolation.env.example)。MD 的开发 GPU 启动器要求显式的 service 连接，不回退到管理连接。

开发启动器通过 `NEXPOLY_DEV_USER_ISOLATION_ENABLED=true` 加载当前工作区的 `.env.dev.auth`；该私有文件必须属于当前用户、权限为0600且不是符号链接。维护停服及预构建镜像启动入口见 [Worker 配置与维护](user-isolation-worker-contract.md#worker-配置)。

账号命令不会通过参数接收密码，创建/重置时从终端隐藏输入：

```bash
python -m app.auth.cli create researcher
python -m app.auth.cli list
python -m app.auth.cli disable <user-uuid>
python -m app.auth.cli enable <user-uuid>
python -m app.auth.cli reset <user-uuid>
```

可以用全局 `--dsn-env` 参数指定保存管理 DSN 的环境变量名称。启用账号不恢复已撤销会话；用户应重新登录。

迁移追加 `0017_user_isolation_prepare`（epoch 2）和 `0018_user_isolation_cutover`（epoch 3），不修改旧迁移校验和。常规 expand 只做准备，身份切换使用专用维护入口：

1. 完成 `0017` 并创建普通账号，记录指定历史资产所有者 UUID。
2. 排空未完成任务，停止 API/Worker 写入，保存数据库/文件备份并在隔离环境实际恢复。
3. 使用备份工具实际恢复到空的隔离数据库，生成并验证备份收据；核对备份 SHA-256、来源数据库名、集群标识及内容摘要。
4. 执行切换；SQL 会拒绝缺失、无效、系统或被禁用的目标账号，并拒绝未排空任务。
5. 校验内容摘要、六表非空 owner、文件关联、RLS、schema 指纹和发布审计，再开放入口。

```bash
python -m app.auth.backup --output <new-private-backup-directory> \
  --asset-root md=<md-data-directory> \
  --asset-root dft=<dft-data-directory> \
  --asset-root batch_imports=<batch-data-directory>/imports \
  --asset-root batch_jobs=<batch-data-directory>/jobs

python -m app.auth.cutover \
  --legacy-owner <existing-active-user-uuid> \
  --backup-receipt <verified-receipt.json> \
  --writers-stopped \
  --audit-output <new-cutover-audit.json>
```

备份工具使用 `AUTH_ADMIN_POSTGRES_DSN` 和 `AUTH_RESTORE_TEST_DSN`，拒绝相同源/目标和非空恢复目标。备份目录及切换审计文件必须尚不存在；备份目录须位于源文件目录之外。收据包含 `backup_path`、`backup_sha256`、`restore_verified:true`、`source:{name,system_id}` 及完整受控数据和文件摘要，必须以实际备份恢复证据生成；无对应资源记录时可省略该项 `--asset-root`。切换自动采用收据中的文件根目录，在表锁内核对当前内容；备份后有变更时必须重新备份验证。`--writers-stopped` 是运维对停止写入的确认；事务表锁不能替代停服。切换记录只输出数量/摘要/关联信息，不输出凭证和业务内容。已有完整 `0018` 账本再次调用不重复回填。

备份与恢复使用和服务器相同主版本的 PostgreSQL 客户端，可通过 `POSTGRES_BIN` 指定。完整恢复收据中的 `backup_state` 包含六张主表、三张子表、Worker 状态、身份与会话的聚合摘要；`assets` 保存文件证据。存在 MD、DFT 或 batch 记录时，必须提供对应 `--asset-root`，实际解包验证文件，并拒绝未明确处理的符号链接。数据库备份必须包含 `auth.users` 和 `auth.sessions`；凭证原数据仅保存在权限受控的完整备份内，公开日志和审计记录仅含聚合摘要。

从仓库根目录检查实际 API/认证/服务角色，并使用审计角色生成完整切换证据：

```bash
python scripts/user_isolation_audit.py preflight --output <new-preflight.json>
python scripts/user_isolation_audit.py capture --output <new-audit.json>
```

切换失败保持私人入口关闭。切换后禁止回滚到无鉴权版本；使用经验证的恢复流程或向前修复。遗留内存任务和旧浏览器数据不通过数据库迁移认领。
