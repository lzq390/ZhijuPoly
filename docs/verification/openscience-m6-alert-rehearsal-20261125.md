# M6 开发存储告警收发演练

## 范围

演练从锁定的 Ubuntu 基础镜像启动无网卡临时客机，在新建的 768 MiB ext4 池中为 dev-m1 的 A、B 测试身份创建两个 3 GiB 稀疏卷。每卷的用户硬配额为 2,000,000,000 字节。监控从真实文件系统取可用空间，并读回两个卷的内核组配额。演练使用仓库验收记录里的身份绑定，客机使用自己的文件系统 UUID。生产 9011、保留的 dev-m1、dev-m6 和原私有卷均未参与。

## 实测结果

| 操作 | 监控和磁盘读回 | 独立接收端 |
|---|---|---|
| 客机空闲，启用 96 / 160 MiB 双阈值 | 两卷可分配，空闲时无告警 | 接收器尚未启动 |
| 在共享测试盘分配空间至约 64 MiB 可用 | 序号 1 为 `firing`，A、B 的组配额均关闭分配 | 发送器记录连接失败，持久回执为空 |
| 在接收端离线期间释放占用 | 空间回到恢复线以上，序号 2 为 `resolved`，两卷均重新开放分配 | 两条事件等待按序投递 |
| 接收端启动，第一条事件入库后立即退出 | 第一次传输没有有效回执 | 独立磁盘中已有一条记录 |
| 重启发送器和接收端，完成补发后再次重启发送器 | 最终回执为序号 2 | 收到的请求序号为 1、1、2，两条唯一事件的完整内容分别与监控原件一致 |

机器可读结果包含池和两卷的 UUID、剩余字节、事件时间、接收请求序列及最终回执。[实测 JSON](openscience-m6-alert-rehearsal-20261125.json)

## 接入和回执

候选存储客机的运维进程需要在受管理员控制的目录监听 Unix socket。使用安装了本次模板的管理器注册接收地址。已在运行旧监控的候选需要在存储维护窗口重启更新后的监控，确认支持保留待发事件，再订阅。省略接收地址时沿用原本的本地告警行为。已有订阅在重复配置、回收工作区或重启进程后继续生效。

```sh
python3 scripts/openscience_per_user_dev.py --state-dir "$candidate_registry" set-disk-watermark \
  --filesystem-uuid "$candidate_pool_uuid" --low-mib 96 --recover-mib 160 \
  --alert-socket /run/m6-operator-alerts.sock
python3 scripts/openscience_per_user_dev.py --state-dir "$candidate_registry" disk-watermark --alerts
```

监控在受保护池以外的客机控制盘持久化事件。独立的 `m5-disk-alert-relay.service` 按序向 socket 发送 HTTP `POST /watermark`，网络等待在磁盘准入锁以外。请求体为原始事件的 JSON，`Idempotency-Key` 为“池 UUID:序号”。接收方需要先按此键持久化事件并去重，再返回 HTTP 2xx 及 JSON `{"id":"原事件 ID"}`。同键重投须核对内容并返回同一回执。持久回执仅证明下一跳已接收。正式接收端、升级路由和当班人员响应仍待指定和验收。

管理器可查最近 64 条事件、接收端最后一条匹配回执以及待发数 `pending`。首次订阅从下一次变化开始，如已有低水位或故障则先发当前事件。接收端离线时，未确认事件单独保留在同一序列中，恢复后按序补发。长时间离线需为控制盘上的积压预留空间。发现缺号或回执不匹配时，发送器保留原进度并在其 systemd 日志记录原因。

## 重跑

```sh
python3 scripts/ci/verify_openscience_m6_alert_rehearsal.py --output /tmp/m6-alert-result.json
python3 -m unittest discover -s scripts/tests -p test_openscience_m5_watermark.py
```

重跑依赖本地锁定的 Ubuntu 镜像、内核配额模块及 QEMU 工具。收件箱为另一进程中的 SQLite，位于独立临时磁盘。客机检查低水位和恢复两条通知，模拟接收端短时不可达、入库后丢回执、双方进程重启，并比对两端完整事件。结果文件须使用新路径。
