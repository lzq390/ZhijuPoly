# OpenScience M5 首轮容量与工作资料恢复演练

本轮在 2026 年 11 月 23 日的本机独立开发环境执行，入口条件是 [M0–M4 复核通过](openscience-m0-m4-review-20261123.md)。容量演练使用全新注册表，恢复演练从已回收的 dev-m1 A、B 原私有卷只读导出。线上入口及生产 9011 未参与。

## 启动准入

每个工作区后端的内存硬上限为 2,048 MiB，配套进程为 96 MiB，两者合计承诺 2,144 MiB。开发注册表最多容纳两个活跃工作区，默认总承诺为 4,288 MiB。宿主机内存留出 16 GiB，实际卷盘留出 32 GiB。三项设置均属本轮开发试点参数。已存在的注册表缺少配置时也使用相同默认值。[实例管理器](../../scripts/openscience_per_user_dev.py)

- 新启动在注册表锁内计算拟驻留工作区数，核对实例数和内存承诺。宿主 `MemAvailable` 须达到 16 GiB 加全部拟驻留实例的内存承诺。已有容器的实际占用已反映在宿主余量中，另外保留全部承诺提供保守余量。
- 在 Docker 本地卷内测量普通用户可用空间。首次启动从同类匿名卷取数。要求卷盘可用空间达到 32 GiB 加每个拟驻留工作区 2,048 MiB。无法取得可靠探针值时拒绝新增实例。启动水位不限制已运行工作区后续写入。
- 准入由一个部署的注册表和本地管理器共同执行。其他服务和部署的负载反映在宿主当前余量中。等待同一注册表锁的时间计入下表的端到端冷启动时延。共享网关、模型进程和原生科学计算各有自己的预算。

通过临时调整注册表水位，分别触发四种拒绝。每次 A 的私有文件读取都返回 200，A 的代次与运行资源保持一致。预算恢复后 B 成功启动，回收一个实例后可以补位。路由和父页测试也验证了容量拒绝会呈现“工作区容量已满，请稍后重试。”[容量实测](openscience-m5-capacity-20261123.json)

| 实测项 | 双账号开发结果 |
|---|---|
| 容量拒绝 | 工作区内存需 4,288 MiB、试验上限 4,287 MiB；宿主可用 193,581 MiB、试验要求 261,810 MiB；卷盘可用 106,598 MiB、试验要求 1,804,612 MiB；请求第 3 个活跃实例超过上限 2。三项试验阈值仅对临时注册表生效。 |
| 首次建卷至就绪 | A 9.223 秒，B 9.028 秒。 |
| 保留卷冷启动 | 三轮同时提交 A/B，按最近秩计算 6 个样本，P50 为 7.040 秒，P95 为 13.871 秒，低于双账号档的 30 秒目标。 |
| 负载及内存 | 各实例执行一次合成对话后读取 cgroup v2 峰值，后端最高 254.66 MiB，配套进程最高 38.46 MiB。两个值分别取各自最大值。OOM 事件及 OOM kill 均为 0，演练完成后临时容器、卷各剩 0 个。 |

## 按账号恢复

资料包包含 `records/**`、`files/**`（排除 `files/.runtime/**`）、工作区标记和必要的目录元数据。恢复保留环境、账号、工作区 ID、UID/GID、模板、容量策略及代次，在另一个部署建立新的私有卷，启动时签发新运行凭据。归档在独立磁盘的私有目录，目录模式 0700、档案和清单模式 0600。当前模板支持目录和普通单链接文件，遇到其他类型会拒绝备份。仓库只保存合成账号的摘要与测量值。[备份与恢复命令](../../scripts/openscience_m5_backup.py)

| 原工作区 | 恢复的常规文件 / 目录 | 工作资料文件内容大小 | UID/GID | 原普通会话 / 项目会话 |
|---|---:|---:|---:|---|
| A | 1,338 / 374 | 545,523 B | 58161656 | 2 / 2 条消息，完整消息与附件引用摘要匹配 |
| B | 1,037 / 301 | 389,523 B | 43620582 | 2 / 2 条消息，完整消息与附件引用摘要匹配 |

备份流在写入时检查备份盘 16 GiB 底线。恢复先验证从备份时独立取得的清单摘要、账号绑定和全部档案，再按合计大小及卷盘水位准入。两份资料的文件树按路径、归属、模式和内容重新计算摘要，发布注册表前逐个同步目标文件及目录。错配 A/B 时，工具在创建注册表前拒绝。新实例能从各自的 `same-project` 打开同名附件，读对方原会话返回 404。生成下一代实例后，上代凭据返回 401，当前凭据能继续读取原项目。最终备份保留于 `/data/lzq/nexpoly-workspace-backups/openscience-m5-20261123-ooxy1c_i`，两个恢复用临时卷均已清理。[恢复实测及两个清单的校验值](openscience-m5-restore-20261123.json)

## 复测入口

容量驱动自行创建并清理临时注册表。恢复驱动需要先将两个离线资料包恢复到指定的全新注册表，运行时与原卷数据做只读比对，结束后回收测试实例。命令中的报告路径须尚未存在。[容量驱动](../../scripts/ci/verify_openscience_m5_capacity.py) · [双账号恢复驱动](../../scripts/ci/verify_openscience_m5_restore.py)

```bash
python3 scripts/ci/verify_openscience_m5_capacity.py --output "$new_capacity_report"
python3 scripts/openscience_m5_backup.py backup --environment dev-m1 \
  --registry "$HOME/.local/state/nexpoly-openscience-m1-dev" --user-id test-user-a --output "$package_a"
python3 scripts/openscience_m5_backup.py backup --environment dev-m1 \
  --registry "$HOME/.local/state/nexpoly-openscience-m1-dev" --user-id test-user-b --output "$package_b"
python3 scripts/openscience_m5_backup.py restore --environment dev-m1 --registry "$new_registry" \
  --owner test-user-a "$package_a" "$trusted_pin_a" --owner test-user-b "$package_b" "$trusted_pin_b"
python3 scripts/ci/verify_openscience_m5_restore.py \
  --source "$HOME/.local/state/nexpoly-openscience-m1-dev" --restored "$new_registry" --output "$new_restore_report"
```

## 后续验收

| 项目 | 当前边界和完成条件 |
|---|---|
| 单用户硬配额（AC-17） | 两个私有卷在 `/dev/nvme1n1p2`，备份目录在 `/dev/nvme0n1` 的 `/data`。两处 ext4 均报告 `noquota`。需对带硬配额的目标卷写满 A，实测拒写、原资料可读、删除后恢复写入，同时 B 仍可写。 |
| 完整业务恢复（AC-16、18） | 工作资料、中央 PostgreSQL、业务计算产物、适配器草稿/幂等日志和模型提供端回执各有存储位置。要对同一恢复点联动还原，特别核对待确认和已提交任务的引用与计费。此轮副本位于同机第二块盘，主机级灾难及密钥恢复须另行演练。 |
| 放量和观测（AC-15） | 首轮仅验证两个人工账号。持续采集容量拒绝、OOM、磁盘增长、冷启动与告警送达，补测共享模型/科研工具受压时平台其他作业仍可用，再评估更高并发。5 人和 20 个活跃实例的灰度沿用规划中的独立放行条件。 |
