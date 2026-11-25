# 当前宿主的 deploy-m6 配额卷与备份

2026 年 11 月 25 日在当前宿主新建了两户专用的 ext4 工作盘。工作盘镜像位于 `/data/lzq/nexpoly-m6-current-host-01`，物理设备为 `nvme0n1`。备份盘和两份恢复盘位于操作者的 `~/.local/state/nexpoly-m6-current-host-backup-01`，物理设备为 `nvme1n1`。宿主分区继续使用原来的挂载选项。配额由专用 Linux 存储虚机的 ext4 内核执行。[实测清单](openscience-deploy-m6-storage-20261125.json)

| 项目 | 结果 |
|---|---|
| 身份和介质 | 从当前候选的受控登记文件读取两名用户的平台注册 UUID、工作区 ID 和 UID/GID。源盘与恢复盘各自有独立文件系统 UUID。按环境、用户、工作区、UID、源或恢复角色生成卷标。管理器另外核对实际文件系统 UUID。 |
| 物理磁盘 | 源 A、B 盘各 3 GiB。另一块物理盘承载一个 5 GiB 备份盘和两份 3 GiB 恢复盘。均为专门创建的稀疏虚拟磁盘。启动程序核对物理设备、固定介质大小、属主及 inode。初次建盘为宿主工作盘预留 32 GiB、备份盘预留 16 GiB。 |
| 硬限与账户隔离 | 源盘和恢复盘的每名用户硬限均为 2,000,000,000 字节，内核计量和强制执行同时启用。A 实际耗尽可用配额，普通文件写入返回 `EDQUOT`，原内容仍可读。该期间 B 能写入和读回新文件。A 删除占用后在先前被拒绝的路径成功写入。恢复到新卷后相同场景再次通过。 |
| 首次逐户恢复 | 先向两个空恢复卷各提供对方的有效备份，均在写入前被拒绝。随后从备份盘按用户恢复。两户 UID、硬限和工作区清单均与源端一致，跨 UID 读取返回 `EACCES`。 |
| 接入 | 专用虚机分配 2 GiB 内存，经固定 SSH 主机密钥、回环 NFSv4.1 和按文件系统 UUID 标记的 Docker 卷工作。服务监听当前宿主的回环地址。现有开发 VM、原卷、原备份和生产 9011 均未参加。 |

两块磁盘仍位于同一台物理宿主。独立物理宿主恢复、宿主物理磁盘低水位下对已运行用户的完整写入保护和实际值班渠道的送达，需要另行实测。本次分户备份保存 OpenScience 工作区中的文件、附件、对话记录、身份标记与 UID 配额。NexPoly 的认证数据库、登录和授权状态、工具任务及模型用量账本、知识服务的数据需要分别备份并核对，整站恢复尚待验收。初次恢复清单覆盖启动前写入的存储测试标记。浏览器测试后，可按下列维护流程单独备份和核对最终工作区。

## 网页测试后的恢复

同日 11:06 停止候选站点后，核对四个 NFS 卷均无容器占用，在管理器离线锁和候选转发暂停期间创建备份代次 `7df5f33551d94377a77ae0772fdacd38`。A、B 源实例代次均为 2。在恢复盘上分别新建临时 ext4 文件系统并恢复，两户的整树 SHA-256、逻辑字节数和内核硬限均与各自源端一致。核对结束后已移除临时恢复镜像，保留了持久备份和四个已登记卷，并确认候选中继恢复服务。[网页数据备份与恢复回执](openscience-deploy-m6-web-checkpoint-20261125.json)

| 账号 | UID | 文件 | 目录 | 项目目录 | 项目文件 | 记录文件 |
|---|---:|---:|---:|---:|---:|---:|
| A | 21920475 | 139 | 55 | 2 | 1 | 136 |
| B | 11718917 | 126 | 51 | 1 | 1 | 123 |

记录文件按 `records/` 下的普通文件计数，每个应用会话可能对应多份记录。项目文件按 `files/projects/` 下的普通文件计数。

## 操作入口

`owners.json` 由候选登记流程生成，只含 `a`、`b` 两个键，每条登记含 `environment`、`user_id`、`workspace`、`uid` 和 `gid`。文件属于操作者且权限为 0600。新建时给出两个从未使用的绝对目录，程序要求它们位于不同的物理磁盘。宿主需要已有锁定的 Ubuntu 镜像、配额内核模块、QEMU、Docker 和 SSH。NFS 服务使用仓库固定 SHA-256 的 Ubuntu 软件包。

```bash
python3 scripts/ci/start_openscience_m5_web_vm.py \
  --work "$new_vm" --owners "$candidate_dir/owners.json" --backup-dir "$new_backup"
```

待新客机出现 `/run/m5-storage-ready`，可用以下命令创建四个 NFS 存储卷和注册材料。`--packages-dir` 可指向预先下载的固定版本软件包。操作者读取 `$new_vm/native/manifest.json` 以及同目录的 `src-bindings.json` 作为工作区管理器注册输入。恢复盘也有独立的 `dst-bindings.json`。再次启动同一台已正常关机的候选 VM 使用 `--resume --work "$new_vm"`，沿用原有卷与身份绑定。

```bash
python3 scripts/ci/openscience_m6_candidate_storage.py \
  --vm "$new_vm" --output "$new_receipt" connect
```

网站完成写入后，先回收两户的后端并停止该候选的站点和工作区监督进程。下列命令获取注册表的离线锁，检查四个卷及其别名没有容器占用，暂时关闭该候选的本机 NFS 转发。两份新备份在备份盘上生成，各自恢复到一个新建 ext4 文件系统，通过内核配额、所有者、文件数量与整树摘要核对后才发布本次备份并清理临时恢复盘。最后恢复原来已启动的转发。每次运行应使用新的输出文件名。备份代次及清单摘要保存在回执中。

```bash
python3 scripts/ci/openscience_m6_candidate_storage.py \
  --vm "$new_vm" --output "$new_checkpoint_receipt" checkpoint \
  --registry "$candidate_dir/ws"
```
