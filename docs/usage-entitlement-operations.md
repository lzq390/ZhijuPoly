# 成功次数配置操作

本页只说明配置入口。新增代码默认关闭计量限制；本轮没有迁移运行数据库、安装线上策略、调整真实用户等级或启用环境。CLI 不创建账号、登录令牌或数据库凭据，不启停服务。

## 操作边界

- `validate-policy` 只读取明确指定的 JSON 文件，不连接数据库。
- `install-policy` 和 `set-tier` 默认预览：连接已有服务或管理员身份，使用只读事务，返回计划和安全元数据。只有显式 `--apply` 才写入。
- 默认读取已经配置的 `APP_SERVICE_POSTGRES_DSN`。可用 `--dsn-env` 指定另一个现有的环境变量**名称**；不支持在命令行传 DSN，不输出 DSN、密码、环境变量内容、原始异常或完整策略文本。
- 预览和应用都校验当前发布的精确 `0019_service_auth_least_privilege` → `0020_structure_library` → `0021_usage_metering` → `0022_usage_entitlements` 迁移台账，以及隔离、usage 和 entitlement 完整结构、RLS、ACL、触发器定义。结构缺失、台账不匹配或身份权限错误时不写入；CLI 不自动执行迁移或修复。
- 配置使用明确的服务／管理员函数，因此 `USAGE_ENTITLEMENTS_ENABLED=false` 时也可预装策略和配置等级。它不会改动关闭状态下的业务 hooks，也不会开启计量。
- `set-tier` 只接受 `free` 或 `member`，目标必须是已有非系统账号。核心函数在 `auth.users` 行锁内再次核对账号，然后更新等级；它与任务真正起跑的授权检查按同一账号锁排序。已起跑任务保留原授权，尚未起跑任务会重新检查。

任何运行库迁移和启用都应另行获得明确批准，先完成备份、无任务维护及对应发布流程；下列示例不是对运行库的执行授权。

## 验证和安装策略

在包含 `backend/` 和 `task_usage/` 的发布目录执行。以下 Linux 示例依赖已有 Python 环境；不安装依赖、不设置 DSN。

```bash
PYTHONPATH=backend:. python -m app.entitlement_cli validate-policy --file /approved/config/entitlements-v1.json
PYTHONPATH=backend:. python -m app.entitlement_cli install-policy --file /approved/config/entitlements-v1.json --dsn-env APP_SERVICE_POSTGRES_DSN
```

第二条只预览，输出 `mode=preview`、策略版本和 SHA-256、`would_install`、`written=false`。核对明确文件、版本、hash 和目标环境后，应用使用：

```bash
PYTHONPATH=backend:. python -m app.entitlement_cli install-policy --file /approved/config/entitlements-v1.json --dsn-env APP_SERVICE_POSTGRES_DSN --apply
```

同版本同内容重复应用是无改动操作；同版本不同内容拒绝。要改变额度或规格，应提供另一个经过审核的策略版本。CLI 不推测商业额度、不把示例额度改造成正式额度。

仓库 `config/examples/entitlements-example-v1.json` 仅为示例。验证或安装示例都要求显式 `--allow-example`，例如：

```bash
PYTHONPATH=backend:. python -m app.entitlement_cli validate-policy --file config/examples/entitlements-example-v1.json --allow-example
```

安装并不等于业务启用。运行进程还必须使用与数据库已安装版本/hash一致的明确策略文件。

## 预览和设置账号等级

`OWNER_UUID` 应由操作者明确设置为目标已有非系统账号的 UUID，不通过用户名猜测。先预览：

```bash
PYTHONPATH=backend:. python -m app.entitlement_cli set-tier --owner "$OWNER_UUID" --tier free --dsn-env APP_SERVICE_POSTGRES_DSN
```

输出只包含目标 UUID、观察到的等级、请求等级、是否会改变／写入，以及 `written=false`。预览不会占用额度或创建等级行。批准这个具体变更后才加 `--apply`：

```bash
PYTHONPATH=backend:. python -m app.entitlement_cli set-tier --owner "$OWNER_UUID" --tier free --dsn-env APP_SERVICE_POSTGRES_DSN --apply
```

恢复会员等级时使用相同入口和 `--tier member`。无论计量采集开关是否启用，实际起跑的访问权限都独立检查；不要把关闭采集开关当成绕过免费层访问限制的方法。

## 运行配置与结果重取

开发 compose 仅转发以下配置，默认保持关闭／空值：

| 变量 | 默认 | 要求 |
| --- | --- | --- |
| `USAGE_ENTITLEMENTS_ENABLED` | `false` | API、MD 主机 launcher、batch 等执行进程的开关必须配套 |
| `USAGE_ENTITLEMENTS_POLICY_FILE` | 空 | 必须指向 backend 容器内实际可读的明确文件路径 |
| `USAGE_ENTITLEMENTS_ALLOW_EXAMPLE` | `false` | 仅显式开发验证允许示例策略 |

当前没有自动挂载任何新的策略目录；宿主机路径存在不代表容器内可读。CLI 不建立挂载、不更新 compose 环境、不重启容器。MD 主机 launcher 只转发明确的 enabled 开关，其余进程须按各自运行契约配置；不得仅开启 API 而让执行者未接收配置。

助手、Tg 助手、录制摘要使用稳定 `Idempotency-Key`，响应 `X-Usage-Operation-ID`。有效终稿必须先持久化结果并原子结算，再发送 SSE `done`；HTTP 200、流 EOF、usage completed 都不单独构成成功证明。相同键重放不调用供应商、不重复扣次，未知状态继续保留预占。

本人可通过 `/api/v1/usage/operations/{operation_id}` 和 `/api/v1/usage/operations/{operation_id}/result` 重取已持久结果；关闭采集后仍可读取旧结果。新摘要固定 `frozen_version`／snapshot hash；同进程缓存复用原 operation ID。启用前旧内存摘要缓存只读返回 `X-Usage-Legacy-Cache: true`，不补造历史消费或持久化承诺。

旧资源统计不是收费权威，HTTP 请求次数、执行尝试数和成功消费次数分别统计。

## 错误与验证证据

CLI 成功返回 JSON 和退出码 0；输入／版本冲突为退出码 2；权限、就绪检查或数据库操作失败为退出码 3；中断为 130。错误仅输出固定 `code`，不打印原始连接错误。数据库配置变更在一个事务内完成，异常时回滚。

`backend/tests/test_entitlement_cli.py` 覆盖无需数据库的校验、默认只读预览、示例显式确认、秘密不回显、错误身份／未就绪不写、不可变版本冲突、采集关闭时的显式配置和账号限制。真实 PostgreSQL 测试仅使用独有临时容器、合成账号／DSN；不访问运行数据库或供应商。
