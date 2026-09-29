# 用户隔离：MD / DFT 执行合同与运维

MD、DFT 保留独立 Worker、原任务表和恢复协议。Backend 以可信用户 UUID 创建任务，Worker 使用服务身份；浏览器身份不传入科学计算子进程。

## 数据和准入

- 六张主表中的 MD / DFT 表有非空 `owner_user_id`。公开仓储查询、列表、取消、产物查询显式要求 owner；永久删除先授权，再联系 Worker 清理文件。
- 创建任务先锁定用户，再持有领域准入锁；本人最多一个未完成任务，同时保留全局容量。DFT 幂等键按用户唯一，重复请求在容量检查前复用本人任务。
- 调度、对账、保留期清理使用独立服务连接；API 数据库角色不继承服务、认证、审计或表所有者权限。
- 文件响应禁止缓存；DFT 产物通过 `(job_id, artifact_id)` 与父任务归属验证后才代理读取。

## 启动和禁用

统一开始点是持久启动授权成功提交；领域 `running` 不是跨模块授权依据。

MD 获取计算许可后，按用户、任务的固定顺序锁定数据库记录，校验 Worker instance 与活动状态，记录 `start_authorized_at`。拒绝时先释放计算资源，再结束任务。已经授权的任务可在账号禁用后完成；旧 Worker instance 不能获得新授权。

DFT 保持无数据库权限。Worker 在引擎的 GPU admission 回调中调用：

`POST /internal/monomer-dft/jobs/{job_id}/authorize-start`

请求携带服务 Bearer token 和原任务的 `attempt_token`、`request_sha256`、`enqueue_sequence`。Backend 锁定用户、任务和当前 attempt，验证非终态与执行身份，在 attempt 上保存授权时间。相同有效 attempt 幂等返回，失效 attempt、终态或未授权的禁用账号拒绝。响应丢失最多按同一 attempt 重试三次；服务不可用、协议错误、未配置或重定向均拒绝开始。

MD 与 DFT 的启动授权日志仅在数据库事务成功提交后记录；公共字段包含 owner、任务和公开执行序号/Worker instance，不包含会话或 attempt token。拒绝原因采用固定代码。

Backend 每次向 DFT Worker 提交前检查 `start_authorization_version == 1`，阻止旧 Worker 绕过授权；MD 健康协议同样声明版本。DFT journal 继续防重复执行，资源释放以原执行器清理完成为准。

## 清理、取消和恢复

计算失败、客户端断流、超时响应、取消请求和 Worker HTTP 错误都不是资源已释放的证据。现有任务/attempt 和 Worker journal 继续作为唯一业务状态；不增加另一套可写任务账本。

- MD 无法确认租约或子进程清理时，保留 `cancel_requested`，以 `cleanup_pending` 和 `resource_cleanup_unconfirmed` 记录原因，用户与全局额度继续占用。当前任务等待 Broker 确认后才能写失败/取消终态；状态回写也校验原 Worker instance。
- MD 启动恢复和退出批量收尾同样先确认 Broker 权威：响应版本及实例合法、无隔离 GPU、无待决 waiter、本环境 MD 租约全部消失，且本进程没有仍未确定的 acquire。Broker 的 suspect/terminating 租约仍算资源未释放。真实计算未配置 Broker 时无法证明旧资源安全，恢复保持关闭。
- DFT 在 GPU 等待中取消，或 Backend 启动授权拒绝但 Worker 已取得租约时，保存 `cancel_requested`；`started_at` 保持未设置，直到真实启动。曾经发出而响应丢失的 dispatch 也必须等待原 attempt 清理，不能直接结束或创建新 attempt。
- DFT 引擎不能证明清理安全时，Worker journal 保留活动取消状态，健康检查报告清理未确认；后续恢复仍核对原 journal、执行尝试与 Broker 证据。只有验证通过的 Worker 终态快照能够释放已派发任务名额。Backend 收到404、409、协议错误或其他供应商/Worker错误时仅记录错误并继续对账。
- 普通用户永久删除和自动保留期清理都要求任务已安全进入终态；未确认清理的任务不会被清理入口删除。

## Worker 配置

- Backend 与 DFT Worker 使用相同 `MONOMER_DFT_START_AUTHORIZATION_TOKEN`；凭证仅存于私有环境。
- DFT Worker 设置 `MONOMER_DFT_START_AUTHORIZATION_URL` 为 Backend 根地址。HTTP 仅允许 loopback，其他连接要求 HTTPS。内部授权路径不能配置公开反向代理。
- MD Worker 使用 `APP_SERVICE_POSTGRES_DSN`。Backend 另有独立 API、认证及服务连接。
- 缺少任何启动授权配置时，系统保持拒绝计算。

9001 开发启动器须显式设置 `NEXPOLY_DEV_USER_ISOLATION_ENABLED=true`，才会加载当前工作区 `.env.dev.auth` 并叠加 `docker-compose.user-isolation-dev.yml`。该文件必须属于当前用户、权限为0600且不是符号链接。Compose 内 API、auth、service、迁移连接使用开发数据库的容器地址；主机 MD Worker 单独使用 `MONOMER_MD_DEV_SERVICE_POSTGRES_DSN`（通常为开发数据库的 loopback 映射端口），同时注入 `APP_POSTGRES_DSN` 和 `APP_SERVICE_POSTGRES_DSN`。DFT Worker 的 URL 使用 `http://127.0.0.1:18000`，令牌与 Backend 相同；旧 Worker 配置不能覆盖这两个启动器值。允许来源须包含实际使用的 loopback 前端9001地址。

CPU 和 GPU 健康检查均使用 `user-isolation-0018` 目标和显式 service 连接执行全局结构检查；Backend 启动仍独立核查实际受限 API 角色。GPU operator 的私有身份记录绑定隔离开关，systemd 启动时只传递该开关，凭证由子进程重新读取私有文件。

已有会话可通过以下入口停在维护状态。它使用原会话身份停止 Backend、MD 和 DFT，再回收该会话 Broker/MPS，不创建 CPU Backend；批量 Worker 和前端由维护操作者另行停写。省略 `NEXPOLY_DEV_GPU_SESSION_EXECUTE=1` 时仅输出说明。

```bash
NEXPOLY_DEV_GPU_SESSION_EXECUTE=1 scripts/dev_server_gpu.sh gpu-session-maintenance-stop
```

完成显式0018切换、真实备份恢复及预构建开发镜像后，可用以下入口启动。`up-prebuilt` 限定9001隔离开发模式，核对镜像的代码、锁文件及七个构建配置文件标签，执行只读服务角色预检，再启动 CPU Backend、批量 Worker 和前端；它不创建账号、不运行迁移。之后沿用原有 GPU direct-start，重新建立受控租约和激活证据。普通 `up` 的构建和迁移流程保持原规则。

```bash
NEXPOLY_DEV_USER_ISOLATION_ENABLED=true NEXPOLY_DEV_GPU_DIRECT_START=1 \
  scripts/dev_server_gpu.sh up-prebuilt
NEXPOLY_DEV_USER_ISOLATION_ENABLED=true NEXPOLY_DEV_GPU_DIRECT_START=1 \
  NEXPOLY_DEV_GPU_SESSION_EXECUTE=1 scripts/dev_server_gpu.sh gpu-session-up
```

## 启动预检、审计与发布边界

[PostgreSQL 隔离测试](../backend/tests/test_monomer_user_isolation_postgres.py)在独立 PostgreSQL 16 数据库创建真实的非所有者 API、service、audit 登录角色。覆盖用户内幂等、A/B 查询/取消/产物、漏 owner 的 RLS、防并发超额、禁用/启动锁顺序、attempt/Worker fencing、准确策略/权限漂移和无凭证审计。

`backend/app/auth/schema.py` 在启动预检检查实际数据库角色、六个归属外键、九张私人表的 FORCE RLS、准确的27条策略和 DFT 完整目录指纹。PG16 dump/restore 会将三个合法 CHECK 中的 BETWEEN 展平；仅接受已验证的完整恢复目录指纹，其他约束、ACL 或策略变化仍拒绝。

开发运维工具：

```bash
python scripts/user_isolation_audit.py preflight --output /private/new-preflight.json
python scripts/user_isolation_audit.py capture --output /private/new-isolation-audit.json
```

预检读取 `APP_POSTGRES_DSN`、`AUTH_POSTGRES_DSN`、`APP_SERVICE_POSTGRES_DSN`；审计读取 `AUTH_AUDIT_POSTGRES_DSN`。输出路径必须尚不存在，文件权限为0600。审计采用只读一致快照，只输出行数、SHA-256摘要和安全元数据，显式不读取账号密码哈希或会话令牌摘要。

备份必须包含 `auth.users`、`auth.sessions`、私人业务记录及关联文件。完整要求见[备份与身份切换说明](user-isolation.md#开发配置与运维命令)，由 [app.auth.backup](../backend/app/auth/backup.py)和 [app.auth.cutover](../backend/app/auth/cutover.py)核验数据与文件摘要。凭证原数据仅存在私有全库备份内；恢复在隔离数据库进行，需核对完整内容及安全审计。

历史生产 mutable-audit v7、旧 bridge 和 rollback 证据格式保持原边界。通用生产控制器在预检阶段拒绝携带0017/0018的部署和回滚，要求专用 `app.auth.cutover`。正式生产 wrapper、发布描述符和新审计协议的接线属于上线前置工作；生产发布前仍须完成这些集成与验证。
