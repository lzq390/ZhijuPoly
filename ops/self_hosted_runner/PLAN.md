# 本地 GitHub Actions Runner 实施与运维方案

版本：2026-11-13。目标仓库：`lzq390/ZhijuPoly`。

## 当前交付状态

已实现控制器、宿主安装工具、受限网络配置、VM 模板制作、guest 启动程序、版本锁和 CI 迁移代码。源码位于仓库 `ops/self_hosted_runner/`，部署副本位于 `/data/lzq/github-runner/control/`。本文件同时保存为 `/data/lzq/github-runner/PLAN.md`。

**专用 runner 已上线，完整 CI 迁移验收待进行。** 2026-11-13 16:50（北京时间）GitHub API 确认目标仓库恰有一台 runner：ID 21，`nexpoly-ci-a0398f54c8a643a4aa30f79b8809b10f`，status=online、busy=false，标签为 self-hosted/Linux/X64/nexpoly-ci-ephemeral。控制器 active、NRestarts=0，唯一执行 VM active，8 CPU 配额、MemoryMax 34 GiB、MemorySwapMax=0。

管理员已完成 guest 启动顺序修复后的模板重建，新镜像为 `base-d3bd857a91806acc549ed09a8ed404c06dc63a2a210f017d886276ae607b93da.qcow2`，上一版本 053aef 仍保留。bootstrap 使用 Type=exec、WantedBy=cloud-init.target、Requires=cloud-final.service，新执行 VM 已完成注册。首次完整 PR、每 job 身份变化/回收、BuildKit/cache/浏览器依赖与 CI 资源峰值仍待验证，暂不将 Online 视为完整 CI 已通过。

专用 GitHub App 已创建并安装：`ZhijuPolyrunner`（slug `zhijupolyrunner`），App ID `5740902`、Installation ID `181516914`，所有者 `lzq390`。用户提供的 PEM 已通过格式与实际 JWT 认证校验，App 权限精确为 Administration write、Actions read、Metadata read，events 为空。用户将安装范围从 all 改为 selected 后，GitHub 再次核实仅含 `lzq390/ZhijuPoly` 一个仓库、未暂停；短期 installation token 及目标仓库 runner API 验证通过，现有 runner 数为 0。验证用短期 token 已撤销。

系统已配置 App ID 5740902、Installation ID 181516914，私钥为 `/etc/nexpoly-runner/app.pem`、root:root 0600；安装和真实 App/runner API 认证已核验。部署专用 config.app.toml 已安装，源码 example 保留通用默认值。此次 guest 修复通过重装及重建模板应用，不需要重新配置 App 或私钥。

执行 VM 的 qcow2 backing image AppArmor 只读锁权限 `rk` 已加载，执行 VM 已成功启动；未赋予基础镜像写权限。新的 guest unit 改挂 cloud-init.target、明确依赖 cloud-final.service，并使用 Type=exec，避免循环及长任务阻塞 boot target。回归测试实际复现旧版 systemd 事务循环、验证新版没有循环且保留启动任务；构建期间仍因 build-mode 跳过注册。此修复必须重新构建基础镜像后才能影响执行 VM。

以下为部署期间的问题记录；最新状态以上述段落为准。

模板制作曾受上游 HTTPS 抖动阻塞：`27891` 通道的 HTTPS CONNECT 一度多次超时，随后恢复；最新完整 `27892 → 27891` 链路返回 GitHub HTTPS 200，程序实际健康检查也通过。`17891` 同样是现有隧道入口，不代表独立稳定的代理。本轮保持原方案 `27891`，模板可重试；本期仍依赖既有 SSH/客户端代理通道存活。

此前重装定位到实际预留降为约 196.875 GiB：ext4 后台初始化通过 loop 的 WRITE_ZEROES 释放了底层文件的零块，原安装器将容量不满误报成挂载身份错误。已应用的修复区分挂载身份与预留容量，只在来源、大小、权限、偏移均匹配后补足预留；专用 storage 服务在每次开机挂载后关闭该 loop 的 discard，避免再次变稀疏。

首次模板预检曾触发 nft JSON 格式兼容问题：本机 nftables 1.0.9 的 numeric 输出将 IPv6 表示为 10、连接状态表示为数组 `[2,4]`。管理员导出的现场 JSON 已确认实际规则符合批准策略；修复已部署，模板能够进入基础镜像下载阶段。修复保留所有出口限制，并对后续不匹配输出具体链和规则位置。

此前一次模板构建在基础镜像下载/校验阶段失败退出，未启动 VM。官方镜像响应大小为 621284352 bytes（约 592.5 MiB），现场观察到下载从 19 MiB 增至约 45 MiB，伴随长时间无增长；旧版随后清理了不完整文件，且 CLI 只显示 `TemplateError`。现已安装下载修复：阶段与进度输出、明确错误、45 秒 socket 空闲期限、单次下载 30 分钟期限，以及仅针对 SHA256 锁定文件的安全续传。后续构建已越过这一阶段。

11 月 13 日健康检查曾返回 403。现场确认是共享出口的 GitHub 匿名 REST API 配额耗尽（remaining=0），普通 GitHub HTTPS 正常。现已安装修复，宿主与 guest 的连通性探针改为 `https://github.com/robots.txt`；真实 runner API 操作仍使用专用 App 的认证。

此前构建进入 guest 后收到 `template_build_failed`。旧版未连接 QEMU 串口，journal 不包含 guest 输出，实例回收后无法恢复那次具体原因。固定阶段、错误分类/退出码及独立串口日志的诊断修复现已安装。最新一轮 `65541069937e46be9e4da9c1c9bae222` 已完成依赖、Docker、Buildx 和 runner 安装，失败于 smoke 的 Docker 镜像拉取：Docker Hub 将 blob 下载重定向到 `production.cloudfront.docker.com`，该域名未列入白名单，返回 Forbidden。前面的 DNS、HTTPS、私网拒绝和直接出口拒绝检查均已通过。

依据 [Docker 官方白名单](https://docs.docker.com/desktop/enterprise/allow-list/) 与 [2026-05-20 发布说明](https://docs.docker.com/docker-hub/release-notes/#2026-05-20)，交付包精确新增 `production.cloudfront.docker.com`。管理员重装后重跑模板，保持固定镜像 digest 和全部隔离检查。此次不使用泛域名或自动根据拒绝日志放行；后续若官方依赖出现其他确证缺失，仍按同样流程审查。

诊断修复重装后，紧接模板更新曾收到本地代理 connection refused。复查时 Squid 持续 active、两个代理端口正常监听，实际同一 HTTPS 检查通过约 1.15 秒；当前可直接重试模板，无须为这次临时拒绝连接再重装。安装器确实存在 `Type=simple` 服务 active 早于监听就绪的时序缺口，交付包进一步增加专用端口有界等待，供下一次安装应用；无法从事后状态独立证明本次短暂错误的全部过程。

## 部署契约与资源预算

| 项目 | 固定配置 |
| --- | --- |
| 根目录 | `/data/lzq/github-runner` |
| 虚拟化 | QEMU/KVM，Ubuntu 24.04 amd64；无 GPU |
| 并发 | 最多一个活跃 VM；每个 runner 只执行一个 job |
| VM | 8 vCPU、32 GiB 内存；宿主 QEMU cgroup 上限 34 GiB、swap 0 |
| 磁盘 | 实际预分配 200 GiB 文件、独立 ext4；guest 虚拟盘 160 GiB |
| I/O | QEMU 虚拟盘读、写分别最多 100 MiB/s |
| admission 门槛 | 专用存储空闲至少 40 GiB，`/data` 空闲至少 100 GiB；启动前宿主 MemAvailable 至少 64 GiB |
| 模板与日志 | 单模板实际分配 ≤25 GiB；保留当前及上一个模板；本地日志合计上限低于 2 GiB、保留不超过 7 天 |
| 网络 | guest `10.0.2.2:27892` → 宿主 Squid `127.0.0.1:27892` → 既有代理 `127.0.0.1:27891` |
| DNS | `202.38.64.7`，TCP/UDP 53 |
| GitHub 权限 | 专用 App，仅目标仓库；Administration write、Actions read、Metadata read |

实施前只读盘点：宿主 Ubuntu 24.04 x64，128 逻辑 CPU、约 251 GiB RAM；当时约 192 GiB RAM 可用、`/data` 约 329 GiB 可用。分配 200 GiB 后预计剩余约 129 GiB；安装时重新检查，不以旧测量跳过门槛。34 GiB 上限约占总内存 13.5%，8 vCPU 相当于 128 逻辑 CPU 的 6.25% 调度配额；不是性能保证。模板更新与任务执行不并行。20 个 PR 本地 job 串行执行，冷启动及依赖下载会增加延迟；实际峰值和总耗时以首次完整验收记录为准。

```text
/data/lzq/github-runner/
├── PLAN.md
├── control/                       # 可审查交付副本；不是 root 服务直接执行路径
├── storage.ext4                   # 实际预分配 200 GiB；仅格式化首次新建文件
└── state/                         # loop ext4 挂载点：nodev,nosuid,noexec
    ├── templates/                 # root 所有、只读、按 SHA256 命名
    ├── instances/<32位hex UUID>/   # 本轮 qcow2 overlay、无凭据 seed
    ├── downloads/                 # 校验过的模板下载缓存
    ├── controller/                # 原子生命周期状态和 drain/resume 请求
    └── logs/                      # journald namespace、资源记录、template-builds/ 构建串口

/usr/local/libexec/nexpoly-runner/  # 安装器复制并保护的执行源码
/usr/local/bin/nexpoly-runnerctl
/etc/nexpoly-runner/config.toml
/etc/nexpoly-runner/app.pem         # root:root 0600，不属于 control 包
/etc/systemd/system/nexpoly-runner-*.service
```

## 隔离、网络与凭据

宿主控制器以 root 运行，QEMU 以无登录账号 `nexpoly-vm` 运行，仅加入 kvm；Squid 使用独立 `nexpoly-proxy`。QEMU 启用 AppArmor enforce、seccomp sandbox、NoNewPrivileges、限定写目录、设备与 cgroup 资源上限。无 libvirt 网络、桥接、hostfwd、宿主目录共享、Docker socket、GPU 或 App 凭据透传。guest 内 runner 可使用 sudo 和 guest Docker；这些权限不映射到宿主。

`/data/lzq` 的现有管理员账号属于受信宿主管理员；其父目录写权限和现有 Docker 管理权限不在 guest 隔离边界内。安装器把部署根目录、挂载点和实际执行源码改为 root 控制，拒绝软链接/特殊文件。业务代码、私钥不写进模板、cloud-init seed 或 Docker 构建上下文。

nftables 只管理独立的 `inet nexpoly_runner` 表，按两个专用 UID 限制 OUTPUT；不 flush 全局规则，不操作宿主 Docker NAT/FORWARD。VM UID 只可访问 Squid 和指定 DNS，禁止 IPv6。Squid UID 只可访问上游代理和 DNS，并允许已有连接的返回流量；`never_direct` 禁止失败后直连。

Squid 使用 `proxy/domains.txt` 官方依赖白名单，非 CONNECT 仅 80，CONNECT 仅 443；拒绝 IP 字面量、非白名单、宿主和直连网段、`114.214.255.0/24`、私网、链路本地与元数据地址。白名单变更需审查官方地址或依赖锁，不能自动学习放行。上游端口仍依赖当前 SSH/Codex 代理存活；离线不自动切到其他出口。

runner 进程、Docker daemon、runner 用户 Docker CLI、两个本地 Buildx builder 分别配置代理；`NO_PROXY` 为 `localhost,127.0.0.1,::1,postgres,backend,lab-postgres`。guest DNS 明确配置，关闭 DHCP 自动 DNS。Docker CLI 的代理配置为构建步骤提供默认代理参数。

App 私钥仅由宿主控制器读取，用于签发短期 JWT、换取限定仓库的 installation token，再申请注册 token。注册 token 经 virtio-serial 握手进入 guest 内存；不出现在命令行、磁盘配置、基础镜像、seed 或日志。guest 用完注册材料后清除注册诊断，唯一名称为 `nexpoly-ci-<uuid>`，使用 `--unattended --ephemeral --disableupdate` 和固定自定义标签。

## 控制器行为

`serve` 和模板更新共享 `/run/nexpoly-runner-controller/controller.lock`；实例身份在任何 VM 副作用前写入原子日志。创建前验证存储来源与容量、实际 nft 规则、AppArmor、代理 HTTPS 连通性、模板哈希，确认无其他活跃 VM。保持一台空闲 VM，无额外队列调度器。

启动/注册超过 600 秒回收；连续三次启动失败持久暂停，需显式 `resume`。每 30 秒查询 GitHub；API 失败退避，不将 `busy=false`、404 或 guest 的完成事件作为完成依据。guest 事件只用于握手、定位 job 和收紧期限；实际 job 身份/状态通过 App API 核验。

job 启动后设置 6300 秒宿主兜底，并创建独立的 systemd deadline timer；控制器重启或 API 卡顿不会重置已存在的计时器。GitHub 确认完成后最多给 120 秒 ACPI 关机宽限，之后停止整个 VM cgroup。systemd 停止阶段另有短暂进程终止宽限，验收需测量实际截止误差。

确认 MainPID 为 0、unit 不活跃且 cgroup `populated=0` 后，删除本实例 GitHub runner、增量盘和对应临时 watchdog。删除 API 失败时保留清理状态，禁止创建下一台 VM。控制器重启保留已登记的运行中 job；绝不重新发送已尝试的 bootstrap，不重用曾启动的 overlay。未知目录/VM 身份停止补机，要求管理员核对，不批量删除前缀相似的对象。模板构建也有独立持久日志，失败镜像不会晋升。

完整 job 日志保留在 GitHub。宿主保留生命周期、安全错误码和资源信息；journald 使用独立 namespace，1 GiB/7 天；资源记录限 64 MiB/7 天。另仅对无注册凭据的可信模板构建保存串口输出：root 0600、单份最多 8 MiB（超过后保留近期内容）、最多 32 份，总量不超过 256 MiB；现有 systemd-tmpfiles 清理定时器按修改时间执行 7 天过期清理，构建前也清理。该规则只清理已存在的专用子目录，不在存储未挂载时创建目录。执行 job 的 VM 不接入串口日志采集。禁止挂载不可信 guest 文件系统读日志。

## 安装与上线步骤

以下命令由拥有 sudo 权限的管理员在服务器执行。安装会分配真实 200 GiB、创建专用用户、安装软件并启用专用代理/防火墙；不会启用控制器。不要借用宿主 Docker 绕过 sudo。

### 1. 检查交付并安装宿主组件

```bash
python3 -I -B /data/lzq/github-runner/control/host.py doctor --config /data/lzq/github-runner/control/config.example.toml
python3 -I -B /data/lzq/github-runner/control/host.py render-config --config /data/lzq/github-runner/control/config.example.toml
sudo bash /data/lzq/github-runner/control/install.sh --config /data/lzq/github-runner/control/config.example.toml --install-packages
```

doctor 在前置条件不全时退出码为 1，仍输出完整诊断。安装器仅对新创建的存储临时文件执行 mkfs；已存在文件必须满足所有权、大小、实际分配和 ext4 检查，否则拒绝，不重格式化。系统 mount unit 管理 loop 挂载，不修改磁盘分区或 `/data` 的现有挂载配置。默认发行版 Squid 尚未安装时先 mask `squid.service`，防止包安装过程中额外开放 3128。

新建文件系统同步初始化 inode/journal，并在 mkfs 后再次确认实际预留。既有文件若身份正确、仅物理预留不足，安装器使用 `posix_fallocate` 补足空洞，保留原有内容，并维持 `/data` 至少 100 GiB 空闲。`nexpoly-runner-storage.service` 每次挂载启动后验证来源，仅将该 loop 的 `discard_max_bytes` 设为 0；模板、runner 和代理依赖此服务。mount unit 另加 `X-fstrim.notrim`，不修改全局 fstrim 或宿主 NVMe 参数。默认运行时校验仍要求完整 200 GiB 预留及有效保护，不降低容量标准。

重装保留现有 `/etc/nexpoly-runner/config.toml` 及其中 App IDs；配置修改通过 `sudoedit` 完成。首次安装的 APT 下载显式使用既有 `127.0.0.1:27891` 代理，不依赖尚未安装的新 Squid。

若首次安装在 `nexpoly-runner-proxy.service` 的 `ExecReload` 报错，使用已更新交付包重跑 `sudo bash /data/lzq/github-runner/control/install.sh` 即可。已安装依赖不需要再传 `--install-packages`；安装器保留现有存储，不会再次分配或格式化 200 GiB。

安装器重启专用代理后，先确认服务 active，再等待 `127.0.0.1:27892` 实际接受连接（最长 30 秒），就绪后才报告安装成功。此等待只处理本地启动；后续模板预检仍必须通过完整 GitHub HTTPS 检查，上游超时、TLS 和 HTTP 错误不会被该等待视为成功。若重装后立即出现 connection refused，而服务随后 active 且 HTTPS 已恢复，可直接重试 `update-template`；现有安装不需要反复重启代理。

### 2. 创建专用 GitHub App

安装系统软件和制作模板不要求 App；**启动自动接单控制器要求 App**。仓库所有者在 [New GitHub App](https://github.com/settings/apps/new) 创建私有 App：

1. 名称全局唯一，例如 `nexpoly-local-ci-runner-<所有者后缀>`；Homepage 填目标仓库地址。
2. 取消 Webhook Active；不请求用户 OAuth、不添加 webhook events。
3. Repository permissions：Administration **Read and write**、Actions **Read-only**；Metadata 为默认读取。其余权限不启用。
4. Only on this account；Install App 时选 **Only select repositories → ZhijuPoly**。
5. 记录 App 设置页的 App ID，以及安装设置 URL `/settings/installations/<数字>` 的 Installation ID。
6. Generate a private key，将 PEM 安全传到服务器，再安装到固定位置；不要在聊天、仓库或 CI secrets 中粘贴。

`github-app-manifest.json` 是字段参考，不是注册凭据；未实现自动账户注册。

```bash
sudo install -o root -g root -m 0600 /安全位置/下载的-app-key.pem /etc/nexpoly-runner/app.pem
sudoedit /etc/nexpoly-runner/config.toml
# 将 github.app_id 和 github.installation_id 的 0 改为真实整数。
```

本实例已核验的值为 app_id=5740902、installation_id=181516914；也可在控制器停止时用 `sudo install -o root -g root -m 0644 /data/lzq/github-runner/control/config.app.toml /etc/nexpoly-runner/config.toml` 安装已经审查过的配置副本，替代手工编辑。私钥仍需单独以 root:root 0600 安装，不进入 control 包。

### 3. 构建并验证模板（可以在创建 App 前完成）

```bash
sudo nexpoly-runnerctl drain
sudo nexpoly-runnerctl update-template
```

若控制器已经运行，先 drain 并等 `status` 中 `state.instance` 为 null，再停止控制器以释放全局锁，然后安装最新交付包和执行 update-template。2026-11-13 的 guest 启动顺序修复需要此流程重建；仅重装宿主代码不会修改已有封存镜像。构建成功后 resume，再启动控制器。

首次构建先下载官方 Ubuntu 镜像，再在唯一构建 VM 中安装软件；VM 内构建最多一小时，下载阶段的期限另计。`guest/software-lock.json` 锁定 Ubuntu dated image、GitHub runner、Buildx 和 Docker deb 的版本与 SHA256；Ubuntu 镜像校验发行签名，Docker deb 校验签名的 APT 元数据。Ubuntu APT 的系统依赖按签名发行库安装，最终模板以完整内容哈希封存；这部分不声称按历史仓库逐字节可重现。

下载发生在 VM 启动之前，会显示阶段、已下载/总大小、百分比和平均速度；单文件每次尝试最多 30 分钟，后续 VM 内构建另有一小时期限。网络中断或 Ctrl+C 时，已锁定 SHA256 的文件保留 `.partial`；再次执行同一命令会尝试 HTTP Range 续传。服务器若忽略 Range 则从头下载，错误范围或最终 SHA 不匹配会清除残留；未校验文件不会作为正式镜像使用。旧版已清理的数据无法恢复。下载速度仍取决于上游隧道。

VM 启动前显示串口日志完整路径；guest 会通过事件通道显示 `apt_update`、`apt_dependencies`、`docker_packages`、工具下载、`docker_start`、`smoke`、`seal` 等固定阶段。失败时终端显示阶段、错误分类及适用的命令退出码/HTTP 状态，详细输出保留在 `/data/lzq/github-runner/state/logs/template-builds/template-build-<UUID>.log`，可用 `sudo tail -n 120 <终端显示的完整路径>` 查看。模板构建不会注册 runner，因此日志不包含 App 私钥或注册 token。失败磁盘仍按原规则回收，串口日志留存供定位。

安装器与控制器/模板更新共享全局锁，下载中也拒绝重装，避免安装重启代理而中断传输。需要更新程序时，先等当前命令结束或在其终端正常 Ctrl+C，再重新安装；控制器的 runtime directory 保留到重启，防止退出时移除其他操作仍在使用的锁。

模板预装系统 Python、Git、ShellCheck、PostgreSQL 16 客户端、Docker/Buildx 和必要系统依赖。Python 3.11、Node 22、Go 1.24 仍由 workflow setup actions 安装。guest 无应用依赖、注册身份或跨 job 可写缓存。模板构建先执行不含注册材料的 smoke 检查，失败不封存。源码更新后要重新执行安装器快照再更新模板；修改 `control/` 不会悄悄改变 root 服务。

### 4. 配置 App 后启用控制器

首次上线前重跑安装器，加载封存 backing image 的 AppArmor 锁权限修复；此项只涉及宿主配置，不需要再次执行 `update-template`。按顺序执行，doctor 返回成功后继续启动。

```bash
sudo bash /data/lzq/github-runner/control/install.sh
sudo nexpoly-runnerctl doctor
sudo nexpoly-runnerctl resume
sudo systemctl enable --now nexpoly-runner-controller.service
sudo nexpoly-runnerctl status
```

doctor 不申请/打印 token，App 实际授权与注册是否正确由启动后的状态和 GitHub 页面核验。必须看到唯一 runner Online、名称含唯一 UUID、标签匹配，才能提交 CI 迁移 PR。首次执行完整 PR，确认 20 个本地实例和 `ci-gate` 全部成功后再合并。main 发布继续由 GitHub 托管 runner 独立从固定提交构建。

## CI 变更范围

保留唯一 `.github/workflows/ci.yml`。`resolve-sha`、`ci-gate`、`release`、`openscience-release` 保持 `ubuntu-24.04`；其余 14 个 job 定义改为 `[self-hosted, linux, x64, nexpoly-ci-ephemeral]`，矩阵展开后 PR 为 20 个、main push 为 18 个本地 job。

磁盘准备脚本在 self-hosted 分支验证 Ubuntu 24.04、root 所有且不可非特权写的 `/etc/nexpoly-ci-runner/profile` 和至少 32 GiB 空闲，仅允许现有科学镜像构建任务调用；不删除 SDK、不 prune。hosted 分支保留原逻辑。CI policy 精确验证路由、标签、权限、脚本 hash、两个 Buildx 代理选项及前端 `npm test -- --maxWorkers=4`，附 actionlint 自定义标签配置和负测。

固定 SHA、checkout 断言、Action pin、依赖锁、镜像身份、main-only 发布、digest 验证、PR 权限和外部 PR 审批规则保持现有策略。发布不接收本地构建产物，App 不用于依赖私有镜像鉴权。

## 验收记录

上线前逐项记录真实运行结果；单元测试通过不能填写为 VM 验收通过。

| 验收项 | 当前状态 / 记录内容 |
| --- | --- |
| actionlint、workflow policy、磁盘脚本、控制器/host/template 测试 | 见交付 `VALIDATION.md` |
| 唯一 VM / runner 与 cgroup 资源限制 | 唯一 Online/idle runner、唯一 VM，8 CPU/34 GiB/swap 0 已实测；任务运行峰值待验收 |
| 无注册 VM smoke（DNS、代理 HTTPS、Docker、部分隔离检查） | 模板构建已完成并封存；更完整目标矩阵及 CI 容量检查待验收 |
| 完整 PR 20 个不同身份、main 18 个、本地 ci-gate | runner 已上线，待迁移 PR 和完整 CI |
| Docker pull、BuildKit remote Git、GHA cache、apt/pip/npm | 模板构建完成 apt 与固定 hello-world pull/run；其余待真实 CI |
| 禁止宿主业务端口、27891、Docker 网段、元数据、IPv6、非白名单 | 已执行 27891、公共直接出口与私网代理拒绝 smoke；其余目标矩阵待完整验收 |
| 成功、失败、取消、超时、重启均回收；无前任务残留 | 状态机负测已覆盖；真实 VM 仍待实测 |
| 上游断开后无直连、恢复后补机 | 待维护窗口针对专用路径测试；不要随意停共享代理 |
| CPU/RAM/磁盘/流量/总耗时 | 读取每实例 `state/logs/instance-<uuid>.json` 并汇总 |
| 现有业务容器、网络与生产目录 | 上线前后对照只读基线；安装不调用宿主 Docker |

资源记录包括 cgroup MemoryPeak/CPUUsageNSec/I/O、overlay 实际分配峰值采样和 elapsed_seconds。`proxy_output_bytes` 减 `proxy_output_bytes_initial` 为专用代理出口字节量估算，包含少量控制请求、请求/响应开销，不能当成纯镜像下载字节。磁盘峰值为 30 秒采样，可能低于瞬时峰值。记录 GitHub PR URL、job ID、VM UUID、起止时间，验收至少覆盖一次完整冷 CI。

本地复验：

```bash
python3 -m unittest scripts.tests.test_local_runner_controller scripts.tests.test_local_runner_host scripts.tests.test_local_runner_template scripts.tests.test_local_runner_guest_build scripts.tests.test_local_runner_guest_ordering scripts.tests.test_ci_image_resources scripts.tests.test_validate_workflows
python3 scripts/ci/validate_workflows.py
actionlint -config-file .github/actionlint.yaml .github/workflows/ci.yml
```

## 日常运维与回滚

```bash
sudo nexpoly-runnerctl status
sudo nexpoly-runnerctl doctor
sudo journalctl --directory="/data/lzq/github-runner/state/logs/journal/$(cat /etc/machine-id).nexpoly-runner" -u nexpoly-runner-controller.service --since '-1 hour'
sudo nexpoly-runnerctl drain
# 轮询 status，确认 state.instance 为 null；当前 job 会完成后回收。
# 已注册但空闲的 runner 可能还会接一个 job，完成后才退出。
sudo systemctl stop nexpoly-runner-controller.service
sudo nexpoly-runnerctl update-template
sudo nexpoly-runnerctl resume
sudo systemctl start nexpoly-runner-controller.service
```

模板更新需要全局锁空闲、drain 已设置、无活动实例。`drain` 保证不补充 VM；已注册的空闲 runner 为避免删除/分配竞态，会保留到接收一个 job 并完成，可能需要等待后续正常 CI。没有新 job 时不会承诺自动立即 drain 到空。先更新锁文件并按官方发行签名/SHA 审查，重新安装代码快照；不要在未 drain 时修改模板。runner 禁止自动更新，需要定期检查官方 release 和 runner version end-of-life API，按其注册/运行截止日期提前更新，不永久固定当前 runner 版本。

回滚先 drain，待实例为空后停止控制器；同一提交回退 CI 路由、对应校验器/测试、磁盘脚本和 actionlint 配置，恢复 hosted 执行。若 GitHub API 故障导致 cleanup 挂起，保留 journal/overlay，恢复 API 后由控制器完成删除；不要手工清掉 journal 来强行补机。停止控制器不会自动杀死正在执行的 VM，也不会删除 200 GiB 存储；紧急终止需管理员明确停止登记的 `nexpoly-runner-vm@<uuid>.service`，之后让控制器按 journal 收尾。

## 官方参考

- [Self-hosted runners requirements and ephemeral behavior](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)
- [Runner REST API、App permissions、version end-of-life](https://docs.github.com/en/rest/actions/self-hosted-runners)
- [GitHub App registration](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
- [Runner releases](https://github.com/actions/runner/releases)
- [Docker daemon proxy configuration](https://docs.docker.com/engine/daemon/proxy/)
- [Docker CLI and build proxy configuration](https://docs.docker.com/engine/cli/proxy/)
- [Buildx docker-container driver options](https://docs.docker.com/build/builders/drivers/docker-container/)
- [Ubuntu cloud images](https://cloud-images.ubuntu.com/releases/noble/)
