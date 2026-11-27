# deploy-m6 宿主值班收件与回执核对

2026 年 11 月 27 日，当前部署宿主为 `deploy-m6` 启用了持久值班收件箱。管理员可用部署账号列出最近 32 条收件，或按完整事件 ID 查询首次收件时间、接收端独立生成的回执 ID 和原始事件。收件端在 SQLite 提交成功后向中继确认。值班人员可通过宿主 user journal 跟踪新告警。[部署与实测记录](openscience-deploy-m6-oncall-20261127.json)

## 本轮收件

候选在正常水位绑定接收端，此时原事件序号为 2，接收端查询结果为空。下表使用北京时间。两个事件的共同 ID 前缀为 `18eefdc362ce467ba5f71180cbc9e2ea:17741123-97c1-49e5-af93-b53272bf051a:`。

| 事件 | 候选发出 | 接收端落库 | 耗时 | 接收端回执 ID |
| --- | --- | --- | ---: | --- |
| 3 · 低水位触发 | 09:19:23.411 | 09:19:24.361 | 0.950 秒 | `c837f5637af5429ba6bfb64a2d524424` |
| 4 · 水位解除 | 09:19:33.259 | 09:19:34.370 | 1.111 秒 | `6a8c0f0dd88d414385cfdc9c41bd7a5d` |

当时目标盘 `/data` 约有 242.14 GiB 普通可用空间。只把候选的两条阈值临时提高到 243.136 / 244.136 GiB，另设 5 分钟独立复位计时器。触发时两户源卷均限制分配。收到触发回执后立即恢复原有的 32 / 36 GiB 阈值，读回两户均开放后撤下计时器。全程没有向共享盘写入压力文件。

对账从接收端按两条完整事件 ID 查询，再对照候选事件和中继确认。接收端数据库只读查询各命中一行，journal 各有一条首次收件记录，中继待投递数为 0。单独重启收件服务后，两条回执 ID、首次收件时间和事件内容均与重启前相同。A、B 的 UID、卷名、源映像 inode 和实例代次前后一致。另核对了两个保留环境的注册表摘要及九个保留卷的存在和占用状态。生产 9011 未参与本次操作。[源事件、回执与最终状态](openscience-deploy-m6-oncall-20261127.json)

## 值班查询

在部署宿主使用部署账号查询。数据库在宿主控制盘的独立私有目录，Unix socket 和数据库权限均为 0600。用户级 socket 单元已启用，宿主为部署账号开启了 linger，退出终端后仍能接收。

```bash
inbox="$HOME/.local/state/nexpoly-m6-deploy-01/oncall/oncall_receiver.py"
prefix='18eefdc362ce467ba5f71180cbc9e2ea:17741123-97c1-49e5-af93-b53272bf051a'
python3 -IB "$inbox" latest
python3 -IB "$inbox" query "${prefix}:3"
python3 -IB "$inbox" query "${prefix}:4"
journalctl --user -u nexpoly-deploy-m6-oncall.service -f
```

从仓库复核本轮两份回执，命令会比较候选保存的事件、接收端查询结果和中继确认。

```bash
ws="$HOME/.local/state/nexpoly-m6-deploy-01/ws"
python3 -B scripts/ci/verify_openscience_deploy_oncall.py --registry "$ws" --first-sequence 3
```

## 安装与绑定

在同一候选重新部署接收端时保留 `receipts.sqlite3`。从仓库根目录安装。初始化会核对部署和目标盘，重复初始化会校验原绑定。服务使用安装后的程序副本。

```bash
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
candidate="$HOME/.local/state/nexpoly-m6-deploy-01"
install -d -m 0700 "$candidate/oncall"
install -m 0600 ops/openscience-per-user-dev/oncall_receiver.py "$candidate/oncall/oncall_receiver.py"
python3 -IB "$candidate/oncall/oncall_receiver.py" init
mkdir -p "$HOME/.config/systemd/user"
install -m 0644 ops/systemd/nexpoly-deploy-m6-oncall.{socket,service} "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now nexpoly-deploy-m6-oncall.socket
systemctl --user restart nexpoly-deploy-m6-oncall.service
python3 -IB "$candidate/oncall/oncall_receiver.py" latest
python3 -B scripts/openscience_per_user_dev.py --state-dir "$candidate/ws" set-disk-watermark \
  --filesystem-uuid 17741123-97c1-49e5-af93-b53272bf051a --low-mib 32768 --recover-mib 36864 \
  --alert-socket "$XDG_RUNTIME_DIR/nexpoly-deploy-m6-oncall.sock"
```
