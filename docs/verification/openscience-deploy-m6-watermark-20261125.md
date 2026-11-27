# deploy-m6 目标盘低水位与值班收件验收

2026 年 11 月 25 日，部署候选的双账号目标盘保护通过。候选产生并保存了两条水位事件。实际值班渠道尚未登记，两条通知的收件回执均待验，正式运维放行条件仍未满足。[逐项原始记录](openscience-deploy-m6-watermark-20261125.json)

11 月 27 日已在部署宿主绑定值班收件端，重新触发和解除并按新事件 ID 核对两份回执。[后续值班收件验收](openscience-deploy-m6-oncall-20261127.md)

## 目标盘和阈值

两份候选源卷映像实际由存储客机 QEMU 打开。其设备号和 inode 与工作区登记一致，所在宿主 ext4 文件系统为 `/data`，UUID 为 `17741123-97c1-49e5-af93-b53272bf051a`。监控在已固定的映像文件描述符上读取普通用户可分配字节，控制文件和事件记录放在另一块盘。管理器同时核对两户的 UID、源卷 UUID 和每户 2,000,000,000 字节硬限。

常态低水位沿用候选的 32 GiB 磁盘保留量，解除线取 36 GiB，留出 4 GiB 回差。演练仅临时调整这个候选的两条阈值，同时设置独立的 8 分钟自动复位计时器。两份网页项目的测试文件内容合计写入 206 字节。期间没有在共享盘创建压力文件，也没有调整宿主挂载方式。

| 北京时间 | 普通用户可用空间 | 低水位 / 解除线 | 两户的内核分配状态 | 本地事件 |
| --- | ---: | ---: | --- | --- |
| 准备工作区前 | 245.608 GiB | 32 / 36 GiB | 开放 | 序号 0 |
| 11:35:29 | 245.607 GiB | 246.606 / 247.606 GiB | 限制新增块和 inode | 触发，序号 1 |
| 11:38:16 | 245.607 GiB | 32 / 36 GiB | 开放 | 解除，序号 2 |

## A、B 实测

| 场景 | A，事先回收实例 | B，保留原标签和在线实例 |
| --- | --- | --- |
| 低水位准入 | 新启动收到全局磁盘容量拒绝，实例仍为已回收。网页登录限于本人文件清理页 | 原工作台新增文件返回 HTTP 507，目录中没有残留文件 |
| 低水位读删 | 网页预览两份自有文件，核对内容后确认删除其中一份，返回 HTTP 200 | 原工作台读回两份文件并删除其中一份，返回 HTTP 200 |
| 存储强制 | 以本人 UID/GID 新建空文件收到内核 `EDQUOT`，目录中没有新增 inode | 同样收到 `EDQUOT`。两卷组硬限均为 1 KiB、1 inode，用户硬限保持 2 GB |
| 恢复后 | 从清理标签重试，原卷以相同 UID 重启，实例代次从 4 变为 5，新增文件写入和读回成功 | 同一实例保持代次 4，原标签新增文件写入和读回成功 |

两户都读回了演练前各自的会话编号和保留文件摘要，已删除文件持续缺失。恢复后另外用两个新网页登录，核对了上轮候选的项目会话、项目文件、原模型答复和私有知识文档。两份保留环境注册表的摘要前后一致，九个开发私有卷、数据库卷和配额测试卷均保留且没有新增容器占用。生产 9011 未参与本次操作。[网页、内核与保留资源核对](openscience-deploy-m6-watermark-20261125.json)

## 值班回执

目标盘事件带有候选部署 ID、文件系统 UUID 和序号。独立查询确认触发与解除各持久化一份。查询时 `subscribed=false`，实际通道回执列表为空。正式收件验收需要指定值班接收器及值班侧查询位置，在绑定后重新走一次阈值触发和解除，分别对照本轮两个新事件 ID、接收时间和渠道侧记录。[本地事件和收件状态](openscience-deploy-m6-watermark-20261125.json)

## 在同一候选复验

使用启用了目标盘监控的 `deploy-m6` 源卷站点和 Node 22。下列命令先读取当前运行阈值，演练结束后恢复原值。输出目录需是控制盘上尚不存在的新目录。

```bash
export PATH="$HOME/.nvm/versions/node/v22.23.2/bin:$PATH"
site="$HOME/.local/state/nexpoly-m6-deploy-01/site"
python3 -B scripts/ci/verify_openscience_deploy_watermark.py \
  --state "$site" --output "/tmp/nexpoly-deploy-m6-water-$(date +%s)"
```

首次安装时，先将仓库中的 `candidate_gate.py` 和 `water_pool.py` 安装到已登记候选存储客机的 `/opt/nexpoly/ops/openscience-per-user-dev/`。启动更新后的候选管理器，并确认两户工作区均已回收、卷无占用，再从宿主注册常态阈值。已有正式接收器时，在同一命令追加 `--alert-socket` 和运维指定的宿主 Unix socket 路径。接收器须以相同事件 ID 幂等确认，管理器可查询最近 64 条接收确认。

```bash
ws="$HOME/.local/state/nexpoly-m6-deploy-01/ws"
python3 -B scripts/openscience_per_user_dev.py --state-dir "$ws" set-disk-watermark \
  --filesystem-uuid 17741123-97c1-49e5-af93-b53272bf051a --low-mib 32768 --recover-mib 36864
python3 -B scripts/openscience_per_user_dev.py --state-dir "$ws" disk-watermark --alerts
```
