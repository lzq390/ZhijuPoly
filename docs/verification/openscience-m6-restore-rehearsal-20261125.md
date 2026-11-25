# M6 开发环境逐户恢复演练

本次演练验证源客机关机后，仅凭分户备份、独立取得的清单摘要和目标用户绑定，在另一台新建客机上恢复工作区。两台 QEMU 客机依次运行于同一台物理开发宿主，分别启动 Linux 内核。正式跨物理宿主的存储、备份运输和恢复耗时仍须在指定目标环境实测。

演练从 AC-22 记录读取 `dev-m1` 测试用户绑定，A 的 UID 为 58161656，B 为 43620582。每次均新建两个 3 GiB 源盘、两个 3 GiB 目标盘和一份 96 MiB 备份盘。客机禁用网络，只挂载本轮虚拟盘及只读程序输入。保留的 `dev-m1`、`dev-m6`、原私有卷、已有账号备份及生产 9011 均未参与。[身份记录](openscience-m6-ac22-20261124.json)

## 恢复路径

1. 源客机分别格式化 A、B 的 ext4 盘，按绑定启用每人 2,000,000,000 字节硬配额。以各自的无特权 UID 在同名项目中写入不同内容的测试记录、附件和笔记，并给零长度文件从偏移 65,536 字节起预留 262,144 字节。挂载独立备份盘，调用现有 M5 分户备份程序。通过结果串口分别取出两份清单的 SHA-256。
2. 源客机正常关机后，删除源系统盘、两份源数据盘和只读输入盘。按字节复制备份盘，比对 SHA-256，移除原备份盘。目标客机只读挂载副本，并验证两份新目标数据盘是空盘。
3. 先将 B 的完整备份连同 B 的正确摘要交给 A 的空目标，再将 A 的备份交给 B 的空目标。两次均要求目标仍为空且硬限为零。按 A、B 的顺序恢复，完成 A 时单独检查 B 仍为空。逐户比较整树摘要、文件数、逻辑字节数、权限、UID/GID、文件 SHA-256 和预留区间。
4. 以每户自己的无特权 UID 尝试突破配额，检查内核返回 `EDQUOT`，再写入并读回一个新文件。用对方 UID 打开附件，检查内核访问错误。最后比对两个内核启动标识、四个 ext4 UUID 和只读备份盘结束时的 SHA-256。

基础轮两户分别恢复 5 个文件、12 个目录、65,775 个逻辑字节，整树摘要逐户匹配。A、B 同名附件的摘要不同。两户均得到超限错误 `EDQUOT` 和跨户读取错误 `EACCES`，且分别成功写入、读回 16 字节。源、目标阶段含客机系统启动与关机分别用时 208.15 秒和 204.78 秒。这些耗时仅适用于本轮小数据集和本机软件模拟。[基础轮原始结果](openscience-m6-restore-rehearsal-20261125.json)

11 月镜像原生数据轮中，A、B 各恢复 23 个文件和 24 个目录，整树摘要均匹配。A 有 69,707 个逻辑字节，B 有 69,706 个。恢复后，两户以对方 UID 打开附件进行读或写都返回 `EACCES`，自己的硬限、预留区间和可写性均通过。目标内实际执行分户恢复分别用时 1.385 秒和 1.213 秒，包含引导、校验和关机的目标阶段共 230.22 秒。两份从关机目标导出的归档分别通过摘要核验。[原生轮存储结果](openscience-m6-restore-rehearsal-native-20261125.json)

将两份导出分别载入全新 11 月镜像工作区，通过原生 API 重读同名项目。两人的会话和附件 SHA-256 各自与源端匹配。A、B 双向按对方会话 ID 查询均返回 HTTP 404，各自的附件列表只出现自己的文件，按对方附件文件名读取均返回原生空内容。API 回执记录了本轮存储结果文件的 SHA-256。[源端与恢复端 API 对照](openscience-m6-restore-native-api-20261125.json)

## 复演

开发宿主需有锁定的 Ubuntu 镜像和配套配额内核模块。命令使用全新的输出文件，虚机、系统盘、测试盘和备份副本随进程正常结束而清理。[镜像与内核核验程序](../../scripts/ci/verify_openscience_m5_quota.py)

```bash
rehearsal_dir=$(mktemp -d)
python3 -B scripts/ci/openscience_m6_restore_rehearsal.py \
  --output "$rehearsal_dir/storage.json"
```

可传入 `--native-fixtures DIR --restored-packages NEW_DIR`，把完整的 `a.tar` 和 `b.tar` 测试工作区仅放在源客机。目标端在恢复完成、进行额外测试写入之前，分别从恢复出的目录重新归档。目标关机后，宿主用只读 `debugfs` 从目标数据盘导出这两份归档，逐个对照目标客机计算的 SHA-256，再发布到新目录。这样可以用应用 API 另行验证恢复后的原生数据。[双客机程序](../../scripts/ci/openscience_m6_restore_rehearsal.py) · [客机内逐户核对](../../scripts/ci/openscience_m6_restore_rehearsal_guest.py)

完整原生轮还需要固定的 11 月工作区镜像和本地 Docker 开发环境。下列命令会新建临时应用部署，分别在存储演练前后通过原生 API 核对内容。[原生应用核对程序](../../scripts/ci/openscience_m6_restore_native.py)

```bash
native_dir=$(mktemp -d)
python3 -B scripts/ci/openscience_m6_restore_native.py seed "$native_dir/seed"
python3 -B scripts/ci/openscience_m6_restore_rehearsal.py \
  --native-fixtures "$native_dir/seed" --restored-packages "$native_dir/restored" \
  --output "$native_dir/storage.json"
python3 -B scripts/ci/openscience_m6_restore_native.py verify \
  "$native_dir/seed/report.json" "$native_dir/storage.json" "$native_dir/restored" "$native_dir/api"
```
