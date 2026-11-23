# OpenScience M5 双账号磁盘硬配额

**AC-17 的双账号硬配额与恢复在独立 ext4 测试卷通过。** A 首次被拒绝时，内核已计费 1,999,998,976 B，距硬限剩 1,024 B，底层卷仍有 1,077,297,152 B 可用。新文件的普通 4 KiB 写入在 `write` 阶段返回 `EDQUOT`（errno 122）。[逐项实测 JSON](openscience-m5-quota-20261123.json)

| 验收项 | 实测结果 |
|---|---|
| A 写满时的可用性 | A 能读取原项目的 `notebook.txt`，其摘要与封存版本一致。此时 B 以自己的 UID 写入并读回 4 KiB 文件，随后 A 再次写入仍得到 `EDQUOT`。 |
| A 删除后重试 | A 删除占用配额的文件，对原先失败的同一路径重试相同的 4 KiB 写入并同步成功，内容读回一致。清理和写入完成后，A 的内核计费回落至 7,131,136 B。 |
| 备份及恢复 | 初始资料 A 有 1,338 个文件，B 有 1,037 个。各加一份不同内容的验证文件后备份，再恢复到全新卷。A 的 1,339 个、B 的 1,038 个文件连同目录、权限和归属，逐用户整树摘要一致。 |
| 恢复后的配额和归属 | 两个目标卷的初始限额均为零。交换账号资料包时，两个目标均拒绝且未产生工作区或设置限额。用本人资料包恢复并重新挂载后，原 UID/GID 和 1,953,125 KiB 硬限保留。两人各自再次写满，均在普通写入阶段收到 `EDQUOT`，删除后均能重新写入。互读对方私有文件均返回 `EACCES`（errno 13）。 |

后续已将独立硬配额卷接入开发工作区管理器，使用同一组登录账号从实际编辑器复核满额操作与换卷恢复。[开发工作区网页验收](openscience-m5-workspace-quota-20261123.md)

## 验收环境

本轮验证 AC-17 中的每用户写满、读取、删除、恢复及归属。输入取自上一轮 dev-m1 A、B 的异盘资料包。启动前按当前注册表、工作区 ID、UID 和已经留存的清单与归档摘要逐项核对。A 的 UID/GID 为 58161656，B 为 43620582。两个原 Docker 私有卷只查元数据。[上一轮异盘恢复证据](openscience-m5-restore-20261123.json)

验收驱动以普通宿主用户运行固定摘要的 Ubuntu 虚拟机。客体没有网卡、宿主块设备或共享目录。A、B 各有一个全新 3 GiB ext4 源卷和一个全新 3 GiB 恢复卷。系统盘和传递资料包的备份盘单独分配。每个工作区的数据目录均位于自己的测试卷，实际读写进程使用原 UID/GID，附加组为空，Linux 有效能力集为零。[验收驱动](../../scripts/ci/verify_openscience_m5_quota.py) · [客体读写验证](../../ops/openscience-per-user-dev/quota-guest.py)

采用规划中的十进制 **2 GB（2,000,000,000 字节，即 1,953,125 KiB）**，文件系统块为 4 KiB。先通过 Linux 内核接口确认用户配额计费和强制执行均已开启，再设置并读回硬限。写满实验以实际分配的文件区段将用量推至边界，以普通文件写入和同步的 errno 判定是否被拒绝，同时记录卷盘的剩余空间。[内核配额工具](../../ops/openscience-per-user-dev/quota.py)

## 备份与恢复边界

每个配额资料包将完整的工作资料清单与源卷内核读回的限额、源文件系统 UUID、环境、工作区、账号和 UID 绑定。恢复以备份时保存的清单 SHA-256 为校验依据，先检查身份和档案，在独立挂载的新目标卷上设置并读回原限额。文件归属、模式及 SHA-256 全树核对成功后，才发布恢复目录。源工作区在备份期间须无写入。目标处于恢复中断状态时，同一有效限额允许重试。[配额资料包工具](../../scripts/openscience_m5_quota_bundle.py)

此次备份盘与工作区盘是同一虚拟机的不同虚拟设备，用于检验“限额随资料恢复”。不同物理 NVMe 上的备份证明见[首轮 M5 报告](openscience-m5-20261123.md)。宿主现有 Docker 默认卷和 `/data` 的 ext4 仍为 `noquota`。若要对实际应用卷放量，需要给工作区管理器接入已配置好配额的目标存储，并经真实登录入口复测。此次验证范围是独立测试卷上的文件系统行为。

## 复测

下列命令复用本机封存的 A、B 资料包和原注册表，输出路径须尚未存在。虚拟机使用镜像自带的 `6.8.0-147-generic` 内核。精简镜像缺少 ext4 的配额格式模块，首轮挂载因而返回 `ESRCH`。复测使用 Ubuntu `linux-modules-extra-6.8.0-147-generic=6.8.0-147.147` 中的 `quota_tree.ko.zst` 和 `quota_v2.ko.zst`。驱动校验这两个文件的固定摘要，在客体中装载。默认模块目录为 `~/.cache/nexpoly/openscience-m5/6.8.0-147-generic`，也可用 `--quota-modules` 指定已解包的目录。驱动自动清理本轮的虚拟机、测试卷及临时配额资料包。

```bash
python3 scripts/ci/verify_openscience_m5_quota.py \
  --registry "$HOME/.local/state/nexpoly-openscience-m1-dev" \
  --packages /data/lzq/nexpoly-workspace-backups/openscience-m5-20261123-ooxy1c_i \
  --evidence docs/verification/openscience-m5-restore-20261123.json \
  --output "$new_quota_report"
```
