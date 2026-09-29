# 私有资产配额配置

以下配置由服务端环境变量设置，均为正整数。省略时采用表中默认值；更新环境变量后重启对应服务。客户端不能覆盖这些配额。

| 环境变量 | 默认值 | 范围 |
| --- | ---: | --- |
| `PRIVATE_MEMORY_JOBS_PER_USER` | 100 | 每用户内存任务保留条数（GPU共用池，reverse独立池） |
| `PRIVATE_MEMORY_BYTES_PER_USER` | 67108864 | 每用户内存任务结果字节预算 |
| `PRIVATE_MEMORY_RETENTION_SECONDS` | 86400 | 内存任务完成后保留秒数；reverse按接收时间清理终态 |
| `PRIVATE_BROWSING_RECORDINGS_PER_USER` | 8 | 每用户浏览记录数 |
| `PRIVATE_BROWSING_SNAPSHOTS_PER_USER` | 16 | 每用户每类检索快照数 |
| `PRIVATE_BROWSING_BYTES_PER_USER` | 16777216 | 每用户浏览记录与检索快照总字节预算 |
| `PRIVATE_BROWSING_RECORD_BYTES` | 4194304 | 单条浏览记录事件字节预算 |
| `PRIVATE_BROWSING_IDLE_SECONDS` | 3600 | 浏览记录与快照空闲保留秒数 |
| `PRIVATE_SUMMARY_OUTPUT_BYTES` | 1048576 | 单次浏览总结文本字节预算；HTTP/SSE传输另限2倍 |
| `PRIVATE_SUMMARY_EVIDENCE_CHARACTERS` | 80000 | 浏览总结证据字符数 |
| `PRIVATE_SUMMARY_MAX_TOKENS` | 4096 | 浏览总结模型最大输出token数 |
| `PRIVATE_ONLINE_HISTORY_MAX_ROWS` | 1000 | 每用户在线检索历史持久条数 |
| `PRIVATE_ONLINE_HISTORY_MAX_BYTES` | 67108864 | 每用户在线检索历史JSONB结果总字节数 |
| `PRIVATE_ONLINE_JOB_MAX_ROWS` | 1000 | 每用户在线任务持久条数，包含失败任务 |
| `PRIVATE_ONLINE_JOB_MAX_BYTES` | 134217728 | 每用户在线任务JSONB结果总字节数 |
| `PRIVATE_ONLINE_RESULT_BYTES` | 8388608 | 单次在线检索结果与导出JSON字节上限 |
| `PRIVATE_ONLINE_EXECUTION_SECONDS` | 600 | 在线检索阶段及逐篇提取检查的总时间预算 |
| `PRIVATE_REVERSE_EXECUTION_SECONDS` | 600 | reverse任务总时间预算秒数 |
| `PRIVATE_PREDICTION_SMILES_CHARACTERS` | 8000 | CPU预测SMILES字符数上限，直接skill同样生效 |
| `PRIVATE_MODEL_INPUT_CHARACTERS` | 80000 | 单篇在线模型提取的标题与摘要字符总数 |
| `PRIVATE_MODEL_OUTPUT_BYTES` | 1048576 | 单次模型输出字节预算；助手流按事件深层内存大小累计 |
| `PRIVATE_MODEL_EXECUTION_SECONDS` | 600 | 助手流执行总秒数，在每次provider拉取前检查 |

在线历史与任务限额在 PostgreSQL 同一事务中按用户加锁、计算、写入。历史覆盖同一 material/mode 时先扣除旧结果大小，禁止并发保存突破配额。超过限额返回 `429 online_retention_quota`；后台任务保存超额则转为失败，保留此前已保存资料。系统不会为满足配额自动删除持久历史或任务。清空/删除本人历史仅释放历史配额；任务条数上限需管理员评估后调整配置。

字节合计使用 PostgreSQL `octet_length(result_data::text)`，保留条数同时约束元数据增长。内存数据仍受既有全局容量限制，只有已完成且执行线程退出的记录可被回收。执行时间预算不会强杀正在运行的线程或GPU运算；底层网络超时、阶段检查和资源许可共同约束执行，额度在实际执行结束后释放。

批量上传的配额保持在 `BatchSettings`：`MONOMER_POLYMERIZATION_BATCH_MAX_USER_IMPORTS`（默认10）和 `MONOMER_POLYMERIZATION_BATCH_USER_IMPORT_BYTES`（默认104857600）。每用户暂存文件条数和实际文件字节数在创建导入记录时同事务检查；失败或取消上传清理暂存目录。

## 批量执行与故障恢复的资源占用

本版批量 Worker 只支持单机、同一个本地 `storage_root`。`.worker-execution.lock` 使用 Linux `flock`；计算子进程通过 `pass_fds` 继承相同的文件描述。父 Worker 数据库连接丢失或进程退出后，只要计算子进程仍在运行，该锁就保持占用。新的 Worker 必须取得此锁、成功删除旧活动尝试的 scratch 后才能清除执行 token 并恢复领取；不以固定等待时间推断旧计算已经停止。

子进程取消或超时后等待实际退出；进程或 scratch 清理不能确认时保留原任务状态、执行 token 和用户额度，记录 `cleanup_pending`，Worker 后续重试清理与恢复。不能通过删除锁文件、替换 `storage_root` 或手动清空 execution token 来绕过占用。同主机的多个 Worker 必须挂载同一个目录，不能用各自独立目录冒充同一个资源池。本版不承诺 NFS 或跨主机文件锁语义。

备份/恢复须先排空并停止批量 Worker。锁文件中的字节不承载任务状态；恢复到新主机时不把锁文件是否存在当作“已有/没有活动任务”的证据。应保留 jobs/imports、文件引用及父任务状态，并从正常 Worker 恢复入口核对资源和 scratch。数据库执行 token 与文件锁共同控制恢复，单独恢复其一不证明另一个已释放。
