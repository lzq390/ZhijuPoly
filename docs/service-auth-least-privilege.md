# Service 身份表最小权限：0019

`0019_service_auth_least_privilege` 收回 `nexpoly_service` 对 `auth.users`
的整表读取。service 仅能读取 `user_id`、`status`、`is_system`，并保留
`UPDATE(updated_at)`，供现有 `SELECT … FOR UPDATE` 启动授权使用。
业务 API、任务模型、九张私人表的 RLS 和 service 后台回写合同不变。

| 身份 | auth.users | auth.sessions | 私人任务和产物 |
| --- | --- | --- | --- |
| api | 不授予身份表读取 | 不授予 | 用户 RLS |
| service | SELECT 三列；UPDATE updated_at | 无读写 | 原有全局调度、回写、清理 |
| auth | 原有登录、改密、禁用权限 | 原有会话管理 | 不新增业务执行权 |
| mutable audit | 原有非秘密列读取 | 原有非秘密列读取 | 原有只读审计 |

迁移是 epoch 4 的 contract，精确依赖 `0012` 和 `0018` 的原 checksum。
旧迁移 SQL 不改写，普通 bootstrap/expand 即使在空库上也跳过 0018、0019。

## 维护入口

当前应用要求完整精确的 0019 ledger，以及实际 service 登录角色的有效权限。
先用已验证的部署维护流程准备目标库与维护身份；本次补丁自身不部署或修改开发库。
仅拥有 0018 的库不能启动新版应用。

在安装该版本的后端环境中执行：

```bash
python -m app.auth.service_privileges \
  --dsn-env AUTH_ADMIN_POSTGRES_DSN \
  --expected-database '<已核对的数据库名>' \
  --expected-system-identifier '<已核对的数据库集群标识>' \
  --service-role '<实际service登录角色>' \
  --audit-output '<不存在的审计文件路径>'
```

多个实际 service 登录角色使用重复的 `--service-role` 参数。
DSN 通过指定环境变量提供，不放入命令行参数或审计输出。
数据库名与集群标识由维护人员从目标环境核对，不能由一次连接失败的日志推断。

入口先以独占创建方式预留 0600 审计文件，再取得身份切换共用的事务 advisory lock。
锁等待上限 10 秒，单条语句上限 60 秒。它核对目标身份、完整 ledger、权限组与
所指定登录角色的权限前提，在同一事务内执行三条授权语句、验证结果并写入 0019。
缺失列、额外授权、错误 checksum、未知迁移等都会使事务失败。

权限检查只查询系统目录和权限布尔值，覆盖 PUBLIC、直接授权、继承与 PG16
`SET ROLE` 路径，包括不可自动继承但可切换的角色。运行时身份不得以
`SET ROLE` 隐藏实际登录身份。检查不读取密码哈希、会话摘要、角色密码或业务行。
发现漂移后停止，不自动清除未知角色或 PUBLIC 的权限。

权限检查还拒绝 `pg_catalog` 系统文件函数的所有重载（`pg_read_file`、
`pg_read_binary_file`、`pg_stat_file`、`pg_ls_dir`、服务器端 `lo_import` / `lo_export`）
的所有权、有效 EXECUTE 和 grant option。这些权限可以通过函数直接授权获得，
不能仅靠拒绝文件访问角色来检查。依据：[PG16 文件函数](https://www.postgresql.org/docs/16/functions-admin.html#FUNCTIONS-ADMIN-GENFILE)、
[服务器端大对象文件访问](https://www.postgresql.org/docs/16/lo-funcs.html)。

service 不承担服务器管理/监控职责：检查拒绝文件访问、全库读写、后端发信号、
CHECKPOINT、监控、预留连接、订阅管理等 PG16 预定义权限角色，数据库所有权另行
检查。集群级 `pg_parameter_acl` 中向 PUBLIC 或可用身份授予的显式 SET / ALTER SYSTEM
及其 grant option 同样拒绝，包括尚未加载扩展的参数。检查只读取授权目录，不读取
参数值或执行管理操作；默认允许的普通会话 SET、业务行锁和后台回写继续保留。
依据：[PG16 角色](https://www.postgresql.org/docs/16/predefined-roles.html)、
[PG16 参数授权目录](https://www.postgresql.org/docs/16/catalog-pg-parameter-acl.html)。

同样的命令可以在精确 0019 库上重试，但仍重新检查有效权限。`already_applied`
只表示迁移已经存在，不能绕过 ACL 校验。审计的 `commit_confirmed=false`
表示未确认成功；遇到连接或提交响应丢失，应核对 ledger 和权限后重试。

## 版本与恢复

| 入口/证据 | 精确数据库版本 | 含义 |
| --- | --- | --- |
| `user-isolation-0019` preflight | 0019 | 当前服务检查，含有效权限 |
| `user-isolation-0018` preflight | 0018 | 显式历史检查，`current_readiness=false` |
| isolation audit v2（默认） | 0019 | 版本化 ledger、RLS、service 权限 |
| isolation audit v1（显式选择） | 0018 | 固定的历史合同，不随 manifest 扩张 |
| 首次切换前备份恢复 | 0017 | 保留后续 0018 → 0019 维护链 |
| 已切换库备份恢复 | 精确 0018 或 0019 | 分别核对对应 ledger，0019 另校验权限 |

当前预检命令：

```bash
python -m app.postgres_preflight --mode schema --strict \
  --schema-target user-isolation-0019 --service-context
```

`app.auth.cutover` 的库函数遇到精确 0019 时立即返回切换已完成，不再读取旧
切换所需的数据封印或执行 0018 SQL。这个返回值不替代 0019 权限就绪检查。
历史 B/F 桥接白名单和旧生产发布控制器不获得跨越身份合同的权限。

事务提交前的失败自动回滚。提交后的回退只使用认识 0019、已验证兼容的版本；
不得删除 ledger、重跑 0018 扩权，或默认使用旧 `402415b` 镜像。

## 验证边界

新增测试以一次性 PostgreSQL 16 和合成身份验证实际非 owner 角色：三列读取、
行锁、禁止密码/session 读取、未知授权拒绝、迁移回滚、幂等重验、版本就绪、
真实 dump/restore，以及 DFT 目录指纹不变。既有业务回归使用被阻断或替换的
执行入口，覆盖禁用与启动的先后顺序、合法收尾、登录改密和私人资源隔离。

镜像 smoke 在独立容器网络验证 bootstrap 停在 0017、显式切换到 0019、
预检和后端启动。测试不启动科学 Worker、GPU Broker、MPS 或科学任务。
真实 GPU 故障验收与这批代码/权限验证分开报告，参见
[版本化验收合同](multiuser-acceptance-scope-v2.md)。
