# OpenScience M5 开发工作区硬配额

**开发环境验收通过。** dev-m1 的 A、B 使用原账号登录真实网页，在工作区硬配额、实例回收重启、备份恢复三轮均通过。原私有卷保持回收状态、本轮未挂载，前后注册表摘要相同。生产 9011 无操作。[逐项实测 JSON](openscience-m5-workspace-quota-20261123.json)

## 开发环境

NexPoly 复用开发登录库中的 `test-user-a` 和 `test-user-b`。浏览器从 NexPoly 登录，通过一次性票据和现有用户路由进入 11 月 OpenScience 编辑器。网页、路由器、工作区管理器和 OpenScience 均在开发宿主原生运行。工作区使用 Docker 本地卷驱动挂载专用 NFSv4.1 导出，每份导出背后都是独立存储虚机中一块 3 GiB 的 ext4 文件系统。每个 UID 的硬限均为十进制 **2,000,000,000 字节**。

| dev-m1 账号 | UID 与 GID | 工作区 ID | 封存原卷备份 |
| --- | ---: | --- | --- |
| A · `test-user-a` | 58161656 | `463fc2f89367468bb7fc6e048d522c9e` | 1,338 个文件，374 个目录 |
| B · `test-user-b` | 43620582 | `3c137e9c236d4998bed842786c0f4548` | 1,037 个文件，301 个目录 |

管理器把环境、用户、UID、工作区和文件系统 UUID 固定到开发注册表。存储虚机用持久所有者记录将每组源卷或恢复卷绑定到一个管理器部署及注册表路径，防止同一份卷被两个开发注册表同时接管。注册和启动时核对存储虚机中实际挂载的 ext4、内核启用的计费与硬限、NFS 导出及 Docker 卷配置。启动前以用户 UID 挂载只读检查卷，上线后在后端进程所在挂载命名空间核对 NFS 来源、0700 工作区和只读所有者标记。NFS 仅监听虚机回环，宿主回环转发器只接受内核保留源端口，并通过私有 SSH 通道转发。工作区目录、对话及编辑器文件均落在该用户的限额内。[管理器](../../scripts/openscience_per_user_dev.py) · [卷与内核核验](../../ops/openscience-per-user-dev/quota_nfs.py)

编辑器先将新内容写入同目录的临时文件，完成同步后再替换原文件。配额拒写时网页显示空间已用完，原文件仍可打开。删除走当前用户实例的文件接口，用户须在项目文件面板确认文件名。[文件操作](../../ops/openscience-per-user-dev/quota-file-io.mjs) · [网页验收程序](../../scripts/ci/verify_openscience_m5_quota_browser.mjs)

## 实测结果

Chromium 153.0.8010.12 的两个独立浏览器上下文分别完成了 NexPoly 表单登录，进入各自的同名项目 `same-project`，打开普通对话、项目对话和原笔记。两个账号都已就绪后，以 A 的 UID 预分配空间。ext4 报告已计费 **1,999,998,976 字节**，硬限仅剩 1,024 字节，小于一个 4 KiB 块。当时 A 的文件系统仍有 **1,077,297,152 字节**可用。

| 网页操作 | 实测结果 |
| --- | --- |
| A 在满额时修改并保存笔记 | 两次 PUT 均返回 507 和 `workspace_quota_exceeded`。每次刷新 GET 都返回 200，编辑器读回原始 37 字节。 |
| B 在 A 两次拒写之间保存自己的笔记 | PUT、刷新 GET 均为 200，笔记摘要为 `703ebd319d006315…`。 |
| A 在项目文件面板确认删除 `m5-release.txt` | DELETE 返回 200。刷新文件列表返回 200，列表中已无该文件。删除释放 2 MiB。 |
| A 再次提交刚才被拒绝的相同内容 | PUT、刷新 GET 均为 200，笔记摘要为 `0dd69cdf35944805…`。此时配额仍强制执行，计费为 1,997,901,824 字节。 |

两个后端均从第 27 代回收，在相同源卷上重建为第 28 代，容器 ID 随之改变。随后原地重启第 28 代，容器 ID 保持一致，Docker 启动时间发生变化。逐次从后端读取进程 UID/GID、笔记所有者及 SHA-256，两个账号均与上表相符。两人再从新浏览器上下文登录，原对话与更新的笔记均可打开。

## 从源卷备份恢复到新卷

源实例回收后，验收程序持有源注册表的独占锁并检查两个 Docker 卷均无容器引用，再导出工作资料及硬限。恢复前两块目标盘的 UID 硬限均为 0，且无工作区目录。恢复后重新从内核读回两个 2,000,000,000 字节硬限。A 的 1,338 个文件、374 个目录和 B 的 1,037 个文件、301 个目录逐项通过内容和权限校验。两份笔记各比输入备份增加 26 字节。

| 账号 | 源文件系统 UUID | 恢复文件系统 UUID | 备份与恢复共用的整树 SHA-256 |
| --- | --- | --- | --- |
| A | `d6880177-5059-41b4-a39e-4bf4be62ce86` | `200eb121-16ad-4d52-8602-61de18774580` | `5128a634f6497a7a1b59e2ebb32d25d8b74f6df26040495960837ddbb85fe5b6` |
| B | `ba7a6d37-6612-4eea-a8ef-d380032dc190` | `b3816521-e2f8-441b-88b5-60d76cee954a` | `322b3324207cfc8a0b12410a78a4adaa5d966f17385be1ca1282ff0e6739e242` |

恢复后，两个账号在新建的后端上重新登录，均打开了自己的原对话和修改后的笔记。恢复容器内的进程和笔记所有者分别为 58161656:58161656、43620582:43620582，笔记 SHA-256 与各自在源卷上保存时的值相同。两份原 M1 私有卷全程保持第 24 代回收状态。开发注册表前后 SHA-256 均为 `83274cac893edfc08a000af90e1410bb2e4190b4413f5c4a5774d36cc44f4fff`。

用另一个空注册表尝试接管同一对源卷，实例运行时因已有容器挂载而被拒。随后在 A、B 的容器引用均为 0 时重试，存储端仍因卷已有登记所有者而拒绝。两次尝试均未新建工作区或容器，原所有者记录一致。

最后将存储虚机正常关机，并使用同一系统盘、四块工作区盘和备份盘重新启动。两次内核 boot ID 不同。四份工作区各自的重启前后整树清单、有效 UID 配额和所有者标记均一致，两份注册表归属记录摘要也一致。宿主按本人 UID 只读重挂四个卷，源和恢复管理器分别重新核验了 A、B 的绑定。此次 201.33 秒覆盖关机、开机和整套复核。回归测试通过 138 项 OpenScience Python 测试和 7 项 Node 路由及活动测试。

## 联调期间修正

第一次满额网页试验中，A 两次保存收到 507，两次均读回旧笔记，B 在其间保存成功。随后 A 的删除和健康请求超时。现场采样记录到后端连续写入同一个 `files/.runtime/data/openscience/log/dev.log` 文件描述符并收到 `EDQUOT`，一个采样窗口内 8,209 次写调用共写入 0 字节。在同版镜像独立的 2 MiB 满盘对照中，写日志文件的版本完成第一个健康请求，第二个失败。改写标准错误后，连续 25 个请求均成功。

配额实例使用镜像支持的 `--print-logs --log-level ERROR`，每个实例的 Docker 日志上限为 2 MB。容器健康检查读取当前实例的只读票据。此前在软件 CPU 仿真中，11 月镜像的 Bun 1.3.5 还曾于空闲容量充足时发生段错误。最终开发拓扑仅用虚机运行存储服务，OpenScience 使用宿主原生 CPU 运行同一镜像。

## 复测和继续开发

需要当前开发环境的 M2 登录状态、Node 22、Chromium、Docker 和封存的原卷备份。下列变量指向新的私有 VM 目录、网页验收记录目录、简短的私有注册表目录和输出 JSON。启动程序按已登记的开发用户、归档摘要和整树清单校验输入，只从原 Docker 卷读取注册元数据。

```bash
packages=/data/lzq/nexpoly-workspace-backups/openscience-m5-20261123-ooxy1c_i
python3 scripts/ci/start_openscience_m5_web_vm.py \
  --work "$vm" --registry "$HOME/.local/state/nexpoly-openscience-m1-dev" \
  --packages "$packages" --evidence docs/verification/openscience-m5-restore-20261123.json
until ssh -o BatchMode=yes -o ConnectTimeout=3 -F "$vm/ssh.conf" m5-quota-guest \
  'test -f /run/m5-storage-ready'; do sleep 3; done
python3 scripts/openscience_m5_native_storage.py --vm "$vm" prepare
python3 scripts/openscience_m5_native_storage.py --vm "$vm" volumes
python3 scripts/ci/verify_openscience_m5_workspace_web.py \
  --ssh "$vm/ssh.conf" --storage-manifest "$vm/native/manifest.json" \
  --registry-root "$registries" --work "$checkpoints" --output "$report" --packages "$packages"
```

验收程序先用独立 Chromium 登录两个账号并打开各自的原笔记，随后在 A 的测试副本以本人 UID 预分配空间，等待内核报告配额已满。编辑器中的保存、重新打开、删除、重试均走实际网页。备份只在源实例回收且确认无人挂载时执行，归档写入独立虚拟备份盘。清单覆盖项目文件、对话、所有者标记和权限，运行时缓存按既有备份口径排除。以上虚拟盘的镜像存于同一台开发宿主，跨宿主容灾仍需另行验收。恢复到另一文件系统后重新注册并登录核验。[验收编排](../../scripts/ci/verify_openscience_m5_workspace_web.py) · [备份与恢复](../../scripts/openscience_m5_quota_bundle.py)

验收结束时，四个 Docker 测试卷的容器引用均为零，存储虚机已正常关机，SSH 和 NFS 转发端口均已关闭。

本轮保留的存储虚机目录为工作树内的 `.runtime/openscience-m5-web-vm`，注册表目录为 `~/.local/state/nexpoly-m5-native-01`，网页操作后的独立备份在虚机的 `/backup/web/{a,b}`。如需继续使用保留的开发副本，先启动存储虚机与私有转发，再指定源注册表 `src` 或恢复注册表 `dst` 运行 M2 开发站点。源和恢复注册表使用各自的 Docker 卷。

```bash
python3 scripts/ci/start_openscience_m5_web_vm.py --resume --work "$vm"
until ssh -o BatchMode=yes -o ConnectTimeout=3 -F "$vm/ssh.conf" m5-quota-guest \
  'systemctl is-active --quiet m5-native-nfs.service'; do sleep 3; done
python3 scripts/openscience_m5_native_storage.py --vm "$vm" start
python3 scripts/openscience_m2_dev.py --registry "$registries/dst" --m5-quota-files
```
