# OpenScience M5 开发环境全局磁盘低水位

**双账号开发验收通过。** 在共享盘低水位时，既有工作区继续提供读取和删除，新增写入及另一账号冷启动被拒。A 从网页删除实际占用的文件越过恢复线后，两个账号均恢复写入。操作员可从独立告警记录核对触发与解除。[逐项测量与浏览器证据](openscience-m5-global-watermark-20261123.json)

## 测量对象与策略

本轮复用 dev-m1 的 A、B、11 月工作区镜像和已有的用户配额卷身份。源卷的两个 3 GiB ext4 镜像以稀疏文件形式放在同一块新建的 768 MiB ext4 测试盘上。管理器将这块共享文件系统的 UUID 与两个工作区的 UUID 一起校验。存储端沿 loop 设备核对各自的后端文件，再从共同的挂载点读取普通用户可用字节数（`f_bavail × f_frsize`）。两个 UID 原有的十进制 2 GB 硬限继续生效。

| 共享盘可用空间 | 管理器和存储端的行为 |
| --- | --- |
| 降至 96 MiB 或以下 | 拒绝新启动和可能增长的网页请求。在两份卷上同时限制工作区 GID 分配新块、新 inode。既有实例可以读取和删除文件。 |
| 告警后回升至 96–160 MiB 之间 | 保持拒写和原告警，避免少量空间释放导致开关反复切换。 |
| 达到 160 MiB 或以上 | 解除临时组配额，恢复启动和写入，记录解除事件。 |

这组阈值用于触发小盘试验。宿主机原有的启动容量预留另行执行。监控服务在存储虚机内定期检查，管理器也在启动和可能增长的网页操作前同步复核。文件读取、文件删除和中止操作可继续执行。内核组配额限制需要新增块或 inode 的后台写入。浏览器从编辑器发起的修改即使能覆盖原块，也须通过网关准入。

## 实测结果

共享盘的文件系统 UUID 为 `2b007549-c9a2-4045-85f9-9f8e28c443d8`。在原登录页已打开 A 的笔记、B 的实例已回收时，测试程序在共享盘上实际分配空间。通过私有开发登录重新验证了两个原账号。

| 检查 | 观察结果 |
| --- | --- |
| 共享盘低水位 | 后台监控记录可用 83,881,984 字节，发布第 5 号触发事件。A 保存笔记、新建文件均返回 HTTP 507，错误为 `workspace_disk_low_watermark`。刷新 GET 返回 200，原笔记摘要一致。 |
| 冷启动 | B 网页登录成功，工作区显示容量已满并可重试。管理器给出的原因为 `workspace_capacity: global_disk (workspace_disk_low_watermark)`。B 未创建容器，工作区代次未增加。A 原容器继续运行。 |
| 两个 UID 的实际存储操作 | A、B 各自创建新文件或给已有文件分配 4 KiB 数据块，均收到内核 `EDQUOT`（errno 122），原文件可读。两人的用户硬限仍为 2,000,000,000 字节。 |
| 告警保持 | 共享盘回到 128 MiB、再回到 159 MiB 时，仍保持同一触发事件。A 在网页删除前再试一次保存，仍收到 507。 |
| 用户删除触发恢复 | A 在网页确认删除本人已实际占用 3 MiB 的测试文件，DELETE 返回 200，刷新列表确认文件消失，原笔记可读。共享盘从 159 MiB 升至 162 MiB，后台发布第 6 号解除事件。前后独立占位文件的 inode、逻辑大小与实际分配字节均相同。 |

两次试验中，A 重试此前被拒绝的相同内容，均保存并读回成功。B 均从原登录页重试后成功冷启动，并写入、读回自己的同名项目笔记。验收程序在网页恢复了两份笔记的原始字节，A 的 SHA-256 为 `0dd69cdf35944805…`，B 为 `703ebd319d006315…`。

另一次双账号试验由操作员直接移除共享盘上的测试占用，同样记录了一次触发和一次解除。两轮使用独立的浏览器登录上下文。

## 冷启动与数据保留

回收 A、B 实例后，将开发存储虚机关机再开机。共享盘和两份卷的 UUID、两个 UID、2 GB 用户硬限、已用量及组配额状态均与关机前一致。6 条告警记录逐条相同，序号仍为 6。其中第 1、2 条与第 5、6 条分别对应两次磁盘水位试验，第 3、4 条记录两轮之间重启监控服务时的异常与恢复。另一次重新安装存储服务的检查保留了 A 已修改文件的摘要和 UID。

对两块原配额盘、两块恢复盘及一块备份盘重新计算 SHA-256，五个摘要均与试验前相同。原 M1 登记记录的摘要也一致。最终已回收两个实例并关闭测试虚机与转发，四个开发配额卷均无容器占用，共享测试盘已保留。相关回归通过 149 项 Python 测试和 8 项 Node 测试。

## 告警与复测

系统状态无法核验时，管理器拒绝新增写入和启动。存储端在能够核验目标卷时收紧组配额，并记录监控异常。后台服务由 systemd 启动，每完成一次采样后等待 2 秒。它在共享盘之外保存最近的状态，按状态转换生成告警。开发告警写到存储虚机系统盘的私有目录 `/var/lib/nexpoly-m5-watermark/alerts/`，与受压盘分开。告警包含触发或解除状态、实际剩余字节、两条阈值、时间和序号。文件系统 UUID 与序号共同标识一个事件。最近 64 条送达记录可独立于当前状态读取。管理器可读取后台采样和已发布的告警。

```bash
python3 scripts/openscience_per_user_dev.py --state-dir "$registries/src" disk-watermark --observed
python3 scripts/openscience_per_user_dev.py --state-dir "$registries/src" disk-watermark --alerts
```

在已验收的私有开发环境中，`vm` 为工作树内的 `.runtime/openscience-m5-web-vm`，`registries` 为 `~/.local/state/nexpoly-m5-native-01`。首次接入时，先回收两个账号的实例并停妥原存储虚机，然后准备小盘。启动器校验保留镜像并使用写时复制的系统盘及辅助盘。

```bash
python3 scripts/ci/prepare_openscience_m5_water_pool.py prepare --vm "$vm"
python3 scripts/ci/start_openscience_m5_web_vm.py --resume --work "$vm"
```

新版启动器从虚机目录的 `vm.json` 读取已登记的存储布局。已有共享池升级时，先从留存注册表的 `disk_watermark.filesystem_uuid` 取得 UUID，在停机状态执行一次 `prepare --vm "$vm" --legacy-pool-uuid "$uuid"`。程序核对原卷和验收清单后写入登记。池丢失、UUID 不符或清单改变时，启动会被拒绝。没有登记且缺少共享池的旧目录无法证明迁移状态，应在新目录从已核验备份恢复。

等待存储虚机的 `m5-storage.service` 就绪后安装共享盘及监控脚本，然后启动私有转发。验收程序负责设置阈值、打开真实网页登录、施加和释放测试负载，并在结束时回收实例。使用带 Node 22、Chromium 和原 M2 登录状态的开发宿主，指定全新的验收目录和结果文件。

```bash
python3 scripts/ci/prepare_openscience_m5_water_pool.py install --vm "$vm"
python3 scripts/openscience_m5_native_storage.py --vm "$vm" start
python3 scripts/ci/verify_openscience_m5_watermark.py --vm "$vm" \
  --registry-root "$registries" --work "$new_private_work" --output "$new_report"
```

已配置的环境再次启动时复用保留的测试盘，系统服务自动挂载并恢复监控。原工作区保存在各自的硬配额卷里。[存储端保护](../../ops/openscience-per-user-dev/water_pool.py) · [告警实现](../../ops/openscience-per-user-dev/water_monitor.py) · [验收程序](../../scripts/ci/verify_openscience_m5_watermark.py)
