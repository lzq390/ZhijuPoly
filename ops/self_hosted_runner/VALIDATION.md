# 验证记录

日期：2026-11-12 起持续记录。初始实现分支：`codex/local-ci-runner`；迁移 PR 分支：`codex/local-ci-runner-migration`（基于最新 main，仅包含本任务改动）。

## 首次交付验证

| 检查 | 结果 |
| --- | --- |
| Controller/GitHub API 生命周期、安全及恢复测试 | 49 项通过 |
| 宿主安装、配置、文件及资源边界测试 | 13 项通过 |
| 模板、签名锁、cloud-init、guest bootstrap/smoke 测试 | 19 项通过 |
| CI 磁盘脚本和 workflow policy 相关测试 | 106 项通过 |
| 合计 | **187 项通过** |
| 完整 `scripts/ci/validate_workflows.py` | 通过 |
| actionlint v1.7.7，含自定义标签及 ShellCheck 集成 | 通过 |
| ShellCheck v0.10.0：磁盘脚本、installer、guest hooks | 通过 |
| Python 语法、shell 语法、`git diff --check` | 通过 |
| AppArmor profile 离线编译 | 通过；未加载内核 profile |
| Ubuntu 镜像 SHA256SUMS 的 GPG 签名与锁值 | 官方元数据实查通过，未下载镜像 |
| Docker APT InRelease → Packages.gz → 精确 deb 版本/SHA256 | 官方签名元数据实查通过，未安装软件 |

四个 hosted job 的块与修改前保持一致。新增控制器测试验证：401/429/503、404、busy=false 不作为完成依据；清理失败不得创建下一 VM；三次启动失败持久暂停；控制器重启不重发注册材料、不延长 job deadline；不复用 overlay；guest 提示必须通过 GitHub job 身份核对；只清理登记 UUID；cgroup 非空不得删除磁盘；已注册 idle runner 在 drain 下保留以规避分配竞态。

root 服务运行路径需要安装器的 root 所有代码快照，不能直接把用户可写的 `control/` 作为 systemd ExecStart。`SHA256SUMS` 为交付副本的逐文件清单，可在 `control/` 下执行 `sha256sum -c SHA256SUMS` 校验；它不代替管理员审查或官方发行签名。

## 首次交付时尚未进行

未执行宿主包安装、分配 200 GiB、创建账号、修改系统防火墙、启用服务、启动 KVM、注册 GitHub runner、跑真实构建或推送 CI 迁移 PR。没有调用宿主 Docker 执行安装，也未改变现有业务容器。

只读 doctor 的预期结果是 `ready=false`：当前交互账号无 KVM 权限，缺少 QEMU、cloud-localds、Squid、专用账号/文件系统/规则、App IDs 和私钥；现有上游代理 TCP 可达，`/data` 约 328 GiB 可用。sudo 要求管理员密码，App 尚未创建。

尚需管理员及仓库所有者按 `PLAN.md` 完成安装、App 创建、模板构建和真实网络/资源/生命周期验收。当前测试不证明实际 nft JSON 表达、Squid 配置加载、KVM/AppArmor 运行、BuildKit GHA cache、跨所有依赖的白名单或完整 CI 已成功；这些项目明确保留在上线验收表。

## 2026-11-12 安装现场补充

管理员已安装宿主组件和专用存储。现场只读核验：`state` 挂载为 `/dev/loop6` 的 ext4，含 `nosuid,nodev,noexec`；firewall/proxy service 均 active，controller inactive。专用代理访问 GitHub API 返回 200，私网 IP 和非白名单域名均返回 403。

首次安装最后一步 `reload-or-restart` 中的 `ExecReload=/bin/kill -HUP $MAINPID` 退出 1，但 Squid 主进程仍健康。无证据确定信号命令的具体失败原因，不将其归因于 AppArmor。已移除多余 reload，安装器改为 enable → restart → is-active。新增 3 项启动顺序/失败传播回归测试，宿主测试 **16 项通过**。源码及 control 副本已更新；部署修复到 root 所有的系统目录仍需管理员重跑安装器。

随后重装触发存储校验失败。只读检查证明 mount identity 正确，文件逻辑大小 214748364800 bytes，但物理分配仅 211393048576 bytes，缺少 3355316224 bytes。严格校验正确拒绝启动，原错误提示未区分原因。Linux 6.8 loop/blk-lib 源码确认 WRITE_ZEROES 可 punch hole，失败后可回退普通零写；现场内核为 6.8.0-31-generic。修复保留严格预留要求，增加经完整身份验证的无内容覆盖补分配，以及重启后自动应用的专用 loop discard 保护。使用独立 64 MiB 临时文件验证同步 mkfs 后补分配不改变文件 SHA256；未对现有存储执行写操作。

新增 storage 服务与其他四个服务经过 `systemd-analyze verify`，退出码 0；只出现与本次服务无关的既有 netplan unit 读取权限提示。运行时保护及 200 GiB 补分配仍需管理员应用修复后验收。

此次补充后宿主测试 **25 项通过**：包括真实 2 MiB 稀疏文件补分配前后 SHA256 一致；错误挂载、权限、硬链接、容量及剩余空间拒绝；只修改核验过的 loop 队列；默认诊断仍拒绝稀疏或未保护存储。

管理员随后重跑安装器成功。只读复核：实际分配为 200.0001068 GiB、逻辑大小为 200 GiB，专用 loop 的 `discard_max_bytes=0`；storage/firewall/proxy 均 active，controller inactive，GitHub 代理 HTTPS 返回 200。宿主安装与运行时预留保护已验证；App IDs 仍为 0，模板及真实 job 验收待完成。全部相关测试合计 **199 项通过**。

首次模板预检报告防火墙不匹配。nftables 1.0.9 官方源码与 JSON 测试确认 `--numeric` 将 nfproto IPv6 输出为 10，将逗号分隔的 established/related 输出为数组 `[2,4]`；旧校验器预期字符串及 set 对象，因而拒绝等价输出。修复仅规范这两个已知表示，不接受额外状态、目标、端口或操作符，也不忽略规则顺序。另拒绝 dormant 表、额外对象、错误归属、重复链及包含 verdict 的伪 counter，并新增明确失败原因。宿主测试现为 **36 项通过**，包含管理员导出的真实规则 JSON 回归测试。

管理员已导出现场 JSON 至 `/home/devuser/nexpoly-runner-firewall.json`。对同一份 4209-byte 现场数据，更新交付包返回 `ok=true`，已安装的旧版校验器返回 false；现场 nfproto 为 10、ct state 为 `[2,4]`，与兼容修复完全吻合。确认实际规则合规，无需改动或放宽防火墙，只需重跑安装器部署校验器修复。

该表示依据 [nftables 1.0.9 官方源码](https://www.netfilter.org/projects/nftables/files/nftables-1.0.9.tar.xz) 中的 `src/json.c`、`include/nftables/libnftables.h` 与 `tests/py/any/ct.t.json`，不是通过放宽实际防火墙实现。

随后模板预检报告代理不可达。专用 Squid 和 `27891` 端口都在监听，但直接经上游 `27891` 的 HTTPS CONNECT 同样多次超时；这次错误来自实际上游连通性，而不是规则校验。`17891` 现有代理经独立只读探测返回 GitHub HTTPS 200，尚未改动既定上游配置。控制器已增强错误分类，区分超时、连接拒绝、TLS、HTTP/CONNECT 状态，并提示固定代理链；不记录原始远程响应或异常中的凭据，也不回退到直连或其他代理。新增 10 项测试，控制器模块 **59 项通过**。该诊断增强不能替代上游通道修复。

继续复测时原 `27891` 恢复：上游 HTTPS 200 耗时约 12.12 秒，完整专用代理链 HTTPS 200 耗时约 7.78 秒，程序同一 `Host.proxy_check()` 实际通过耗时约 3.24 秒。用户指出 `17891` 也是代理隧道入口，因此不将该端口视为独立可用性方案；本轮不切换上游、不更改规则或共享隧道服务。当前可重试模板构建，后续仍需观察隧道的持续可用性。

随后构建进入 Ubuntu 下载。HEAD 实查长度 621284352 bytes；进程约运行数分钟，文件从 19 MiB 增长到约 45 MiB，期间有超过一分钟无磁盘增长。未创建 VM。进程随后退出，临时文件被旧版清理；用户终端为 `Operation failed: TemplateError`。只能确认下载/校验阶段失败，旧输出无法进一步证明具体异常类型。

新下载器显示阶段、大小、百分比、速率，使用 read1 防止慢速分块隐藏进度；45 秒 socket 空闲限制与 1800 秒单次下载限制。SHA 锁定文件在网络中断、Ctrl+C、期限到达后保留 partial；恢复必须满足严格的 206 Content-Range/长度，200 响应重启，错误范围/416/最终 SHA 不匹配拒绝提升。新增实际中断→恢复→完整哈希校验、缓存、截断、超长、超时、进度及隐私测试；模板模块 **32 项通过**。CLI 安全显示 TemplateError 具体信息，仍隐藏任意系统异常中的 URL/凭据；控制器模块 **61 项通过**。

另补齐安装与模板/控制器的共享锁、RuntimeDirectoryPreserve，以及全部运行时依赖文件清单。真实文件锁冲突测试证明模板仅在下载阶段时安装器也会在修改前拒绝；宿主模块 **41 项通过**。这些修复尚未应用到已安装程序，也未终止现场构建或修改下载内容。

## 2026-11-13 匿名 API 配额误判修复

更新安装后 `update-template` 的 HTTP 403 已现场定位：经 27892 和直接经 27891 请求 `api.github.com/meta` 均为 403，`X-RateLimit-Remaining: 0`，响应含 API rate limit exceeded，未出现 Squid ERR_ACCESS_DENIED。同一受限代理请求 `github.com/` 返回 200（约 0.92 秒），`github.com/robots.txt` 返回 200（约 0.67 秒）。因此代理链可用，旧探针消耗共享出口匿名配额并误判。

宿主与 guest 均改用 `https://github.com/robots.txt`，固定 User-Agent、单次 GET、限定响应读取，仍只经专用代理且必须 HTTP 200；403、TLS 或网络错误依旧拒绝，不回退直连。App 认证的注册/任务 REST API 不变。宿主真实新探针验证通过约 0.78 秒；控制器 61 项与模板/guest 34 项测试合计 **95 项通过**，覆盖 guest 同样不消耗匿名 API 配额及 403 不放行。

参考：[GitHub REST API rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) 明确匿名请求按来源 IP 计数，默认每小时 60 次；remaining=0 时 403/429 属于限流。此次修改尚需管理员重跑安装器应用。

## 2026-11-13 构建 guest 失败诊断修复

管理员安装更新后，构建已完成基础镜像下载、磁盘转换和扩容，进入隔离 VM 后收到 `template_build_failed`。当前无运行中的构建 VM。管理员查询 namespace journal 返回 No journal files；源码核对证明 guest 控制台被指向无人连接的 serial.sock，旧版随后回收磁盘，因此此前输出不可恢复。尚无证据将此次失败归因到具体依赖、下载或网络。

交付包增加可信模板构建专用串口采集，独立于实例目录留存：0600、8 MiB 滚动上限、最多 32 份、7 天过期规则；通过已有且现场 active 的 systemd-tmpfiles-clean.timer 自动清理，无须等下一次构建。tmpfiles `e` 规则不创建不存在的目录，按 mtime 过期。执行 job 的 VM 不启用该采集，也不挂载 guest 磁盘读取日志。

guest 通过既有事件通道发送固定阶段、固定错误类别及有界整数错误码；CLI 不接受任意异常、URL 或命令输出作为错误详情。guest 在发送失败事件前输出并刷新 traceback，避免宿主回收早于诊断输出。失败提示含具体日志路径，磁盘及生命周期状态仍在确认 cgroup 为空后回收。

runner 四组测试合计 **149 项通过**：控制器 61、宿主 42、模板/guest bootstrap/smoke 40、guest build 6。新增真实 Unix socket 采集、滚动大小/近期输出、数量/过期清理、软链接拒绝、失败不晋升/磁盘回收但日志保留、阶段/错误脱敏、先诊断后通知及关机顺序测试；真实 systemd-tmpfiles 在临时目录验证过期删除和缺失目录不创建。`git diff --check` 通过。尚需管理员重装并重跑以取得真实 guest 故障原因；本次没有自行启动 VM 或操作宿主业务容器。

## 2026-11-13 代理启动就绪等待

诊断包重装成功后，紧接 `update-template` 返回 proxy connection refused，尚未进入模板构建。事后只读检查：专用 Squid 从 10:40:07 起 active、NRestarts=0，127.0.0.1:27891/27892 均监听；同一 `Host.proxy_check()` 通过完整专用代理链实际成功，耗时约 1.15 秒。当前可以直接重试模板，不必再次重装。

代码确认安装器只用 `systemctl is-active` 检查 Type=simple 服务，存在进程启动早于监听建立的就绪缺口；与紧接安装的本地拒绝连接吻合，但不将事后健康状态视为已完整证明当时的故障过程。交付包修复为重启后每轮检查 active，并在 30 秒内等待固定 127.0.0.1:27892 接受连接；只重试本地 refusal/timeout，意外错误和服务退出立即失败，探测 socket 随即关闭。完整单次 HTTPS 检查仍独立执行，不隐藏上游或 HTTP 错误。

宿主 47 项及控制器 61 项相关测试合计 **108 项通过**，新增延迟 bind、固定期限、服务退出、非预期错误不重试/不泄露及连接关闭测试；`git diff --check` 通过。修复只更新交付包，未执行服务重启或修改已安装系统文件，将在下一次安装时生效。

## 2026-11-13 Docker Hub CDN 白名单遗漏

管理员导出实例 `65541069937e46be9e4da9c1c9bae222` 串口日志后，明确定位：smoke 已通过 DNS、代理 HTTPS、私网拒绝、直接出口拒绝检查；Docker 29.9.3 / Buildx 0.31.1 正常启动。固定 digest 的 hello-world 在拉取 blob 时跳转到 `production.cloudfront.docker.com`，返回 Forbidden，Docker 退出 125，外层 smoke Python 退出 1。此次约在 guest 启动后 526 秒失败，已正确回收实例。

只读对照：经专用 27892 对该 CDN 的 HTTPS CONNECT 返回 403；经现有上游 27891 能建立 HTTPS，根路径返回 CloudFront 自身 403，二者不是同一拒绝层。进一步通过官方 registry API 重新取得该失败 blob 的下载地址，仅在内存处理公共拉取授权与临时 URL，不输出查询参数；同一 blob 经既有上游实际返回 **HTTP 200、577 bytes、SHA256 匹配**。未使用宿主 Docker 拉取或运行容器。

[Docker 官方白名单](https://docs.docker.com/desktop/enterprise/allow-list/) 明确列出该精确域名用于 Pull/Push，[Docker Hub 2026-05-20 发布说明](https://docs.docker.com/docker-hub/release-notes/#2026-05-20) 记录新增 CloudFront CDN。依据这两项官方来源，在 `proxy/domains.txt` 精确补入该主机；不添加 `.docker.com` 或 `.cloudfront.net` 泛域名，保留原有 CDN、固定镜像 digest 和全部 smoke 验收。

更新后的候选配置经宿主 Squid `-k parse` 退出 0；既有宿主测试 **47 项通过**（包括白名单唯一性/父域重叠、拒绝规则和无直连策略），`git diff --check` 通过。交付包及清单已更新，尚需管理员重跑安装器加载专用代理配置并重新完成模板构建；静态验证及上游单文件验证不代替完整 guest Docker 验收。

## 2026-11-13 模板完成与首次上线准备

管理员报告模板构建完成。现场只读核验：templates/current.json 和 previous.json 存在；两份 root:root 0444 的独立 qcow2 均为 160 GiB、无 backing，实际分配均约 2.964 GiB，完整 SHA256 均与文件名匹配。镜像分别为 `base-053aef026f10952e6a881b4f81e3cf9c4c7d7aac6433686bcaaa8475ab35cf36.qcow2` 和 `base-46286883a4c2daf3b9cd0de44c6064fc71498d50a3ef94e5adb541afbf0335df.qcow2`。manifest 为 root 0600，当前账号不能读取其中的当前/前一版本绑定，保留给管理员 doctor 验证。

专用 ext4 挂载 /dev/loop6、含 nosuid/nodev/noexec；专用文件系统约 190 GiB 可用、/data 约 196 GiB 可用，storage/firewall/proxy 均 active。同一已安装代码的实际 HTTPS 检查通过约 1.10 秒；controller inactive，无活动 VM。配置 App IDs 仍为 0，app.pem 不存在，因此尚未执行真实 runner 注册，未推送 CI 迁移。

首次执行 VM 的只读审查发现 AppArmor 基础镜像规则缺少 `k`：构建 VM 使用 standalone 磁盘，执行 VM 使用 readonly qcow2 backing。对现场 base 的只读 qemu-io/strace 显示 O_RDONLY 后执行 F_OFD_SETLK/F_RDLCK；本机 apparmor.d(5) 明确 `k` 覆盖文件锁。交付包只为 `templates/base-*.qcow2` 补充 `rk`，保留其余模板文件的只读权限；未增加模板写权限，未修改已安装 profile。

新增安全边界回归验证非 owner 的封存镜像可读/可锁、其他模板文件只读、任何模板规则不得授予写入。宿主测试 **48 项通过**；渲染后的 AppArmor 配置经 `apparmor_parser -Q -T -K` 离线编译退出 0（不加载内核、不使用/写入缓存），`git diff --check` 通过。管理员首次启动控制器前需安装此宿主修复，现有模板无需重建。下一步由 lzq390 创建/安装目标仓库专用 App，配置两个 ID 和受保护 PEM 后运行 doctor；后续真实 runner/API/CI 仍待验收。

## 2026-11-13 专用 App 身份验证

用户提供 App ID `5740902` 和服务器上的 PEM 附件。先将该用户拥有的附件权限收紧至 0600；OpenSSL `pkey -check -noout` 返回 Key is valid。通过现有专用代理在内存生成短期 JWT、调用官方 GitHub API；未打印或持久化 PEM 内容、JWT、签名或任何 token，未向 guest 传递凭据。

`GET /app` 返回 HTTP 200：ID 5740902，name ZhijuPolyrunner，slug zhijupolyrunner，owner lzq390；权限精确为 administration write、actions read、metadata read，events=[]。`GET /repos/lzq390/ZhijuPoly/installation` 返回 404，`GET /app/installations?per_page=100` 返回 HTTP 200、空列表。由此确认 App 与私钥匹配，但尚无任何安装；需要所有者执行 Install App 并只选择目标仓库，之后再自动发现 Installation ID。

系统 ID 配置仍为 0，受保护 app.pem 尚未创建，controller inactive；没有提前启动未授权的 runner，没有复制私钥到 control 包，也未修改已安装系统文件。后续安装、App 安装范围、installation token、runner Online 及真实 CI 仍待完成。

## 2026-11-13 App 安装范围与实际 API 验证

用户执行 Install App 后，目标仓库 installation 返回 HTTP 200，Installation ID=181516914，App ID=5740902、账户 lzq390、权限与 events 均符合方案，未暂停。初次返回 repository_selection=all，与仅目标仓库的部署契约不符，未启动控制器；用户随后将设置改为 Only select repositories。

修改后重新获取 installation：repository_selection=selected，身份/权限检查通过。使用只含 metadata read 且未额外按仓库收窄的短期 installation token 查询 `/installation/repositories`，确认 total_count=1，唯一仓库为 lzq390/ZhijuPoly；随后撤销该验证 token。另申请与控制器相同的目标仓库/Administration write/Actions read token，查询 runner API 返回成功、total_count=0，随后也撤销；没有注册 runner 或启动 VM，没有持久化 token/JWT。

准备部署专用 config.app.toml 副本，通过两套配置加载器；解析后精确对照已安装配置，差异仅为 github.app_id=5740902 与 github.installation_id=181516914。该非凭据配置保存在 control，纳入交付清单；私钥不进入包。系统中的实际配置和 app.pem 尚待管理员 sudo 安装，controller 仍 inactive。当前工具会话 sudo 需要密码，因此不能代替管理员完成写入 /etc、重载 AppArmor 及启动 systemd；后续以 doctor 和 GitHub runner Online 验证为准。

## 2026-11-13 首次执行 VM 启动顺序修复

管理员完成 root 配置并启动服务后，控制器 active、NRestarts=0，App IDs 与 root:root 0600 私钥已正确安装，基础镜像 AppArmor rk 规则已加载。执行 VM `d7651a3a682d43339723db438e5334c6` 已实际启动，8 CPU 配额、MemoryMax=36507222016（34 GiB）、MemorySwapMax=0；注册前内存峰值约 1.54 GiB、增量盘约 24 MiB，仅为启动期采样，不代表 CI 峰值。GitHub runner API 仍为 0。

管理员导出 status 证明 phase=booting、bootstrap_attempted=false、registered=false、无 health/API error，当前 manifest 为 053aef026f10952e6a881b4f81e3cf9c4c7d7aac6433686bcaaa8475ab35cf36，与先前完整校验镜像一致。注册 token 尚未传入 guest。源码与本机 Ubuntu cloud-final.service 对照确认循环：bootstrap WantedBy=multi-user.target 且 After=cloud-final.service；cloud-final 又 After=multi-user.target。隔离 systemd 事务实际复现 ordering cycle 及启动任务被删除；verify 即使打破循环也可能返回 0，因此新测试检查诊断内容。

修复 guest unit：WantedBy=cloud-init.target、Requires=docker.service cloud-final.service、Type=exec。保留网络排序和 !build-mode 条件，长时间运行的 runner 不再将 boot target 的启动完成推迟到整个 job 结束。4 项新增回归运行真实隔离 systemd 事务及 condition 求值，证明旧循环、新事务保留 cloud-final/bootstrap、Type exec 和构建标记跳过注册。

此次完整相关测试 **265 项通过**，workflow policy 与 git diff --check 通过。管理员已 drain，后续启动实例 fe430a7943a440729ca189bf02a1f16a 也已回收，当前无活动 VM；控制器仍 active，应在确认 state.instance=null 后停止以释放锁，再安装修复并重建模板。仅重装宿主代码不足以更新旧镜像内的 unit。尚未创建迁移 PR，遵循先确认唯一 runner Online 再迁移的顺序。

## 2026-11-13 首个 runner Online

管理员重建后生成 `base-d3bd857a91806acc549ed09a8ed404c06dc63a2a210f017d886276ae607b93da.qcow2`，安装的 guest unit 已包含 Type=exec/cloud-init.target 修复。控制器自 16:42:32（北京时间）起 active、NRestarts=0；唯一执行 VM a0398f54c8a643a4aa30f79b8809b10f 自 16:43:18 起 active。systemd 实查 CPUQuotaPerSecUSec=8s、MemoryMax=36507222016、MemorySwapMax=0；注册后采样 MemoryCurrent 约 1.56 GiB、MemoryPeak 约 1.56 GiB，不代表 CI 峰值。

16:50:11（北京时间）使用 App 申请仅目标仓库 administration read 的短期 token，GitHub runner API 返回 total_count=1：runner ID21，name=nexpoly-ci-a0398f54c8a643a4aa30f79b8809b10f，status=online、busy=false，labels=self-hosted/Linux/X64/nexpoly-ci-ephemeral；检查 token 随后撤销。与宿主唯一活跃 VM UUID 对应，证明真实注册及监听成功，而非仅 systemd active。

下一步按已授权方案提交 CI 迁移 PR，完整执行 20 个本地 job，并核验每 job 新身份、回收、ci-gate、代理依赖和资源峰值。当前源分支另含 11 个既有开发提交，将从 origin/main 创建隔离工作树并仅带入本任务改动，避免把它们混入迁移 PR。完整 CI 与故障场景验收仍未完成，暂不合并。

## 2026-11-13 迁移 PR 提交前复验

新 managed worktree 从 origin/main 7dc8581 创建，5 个 tracked task diff 以 3-way 应用，新增 runner 包、测试及 actionlint 配置逐项复制；未带入原工作分支 11 个既有提交，原工作树保留。复核确认最新 main 的 server monitoring 测试、依赖锁、排除/收集规则与校验负测均保留，四个 hosted job 块与最新 main 完全一致。

首次本地校验发现新工作树受默认 umask 影响将受保护文件检出为 0664/0775；按已有 policy 恢复 0644/0755，仅修改本地权限、不改内容或放宽校验。随后完整相关测试 **266 项通过**，`validate_workflows.py`、actionlint v1.7.7（显式自定义标签配置）、ShellCheck v0.10.0（本任务修改/新增的全部 shell 文件）、git diff --check 均通过。暂存 39 个文件，检查未包含 .pem/.key 或 PRIVATE KEY 内容。实际 runner 已 Online，完整 PR CI 与每 job 回收仍待运行。

## 2026-11-13 PR #111 首轮执行与模板兼容修复

PR https://github.com/lzq390/ZhijuPoly/pull/111 已创建为 draft 并附到当前任务，base=main 7dc8581、head 初始 4dc8c2d，仅一个迁移提交。CI run 43397323016 已通过 hosted resolve-sha；首个本地 job 131791367723（Frontend Vitest and build）使用 runner ID21 / VM a0398f54c8a643a4aa30f79b8809b10f，于北京时间 16:57:45 开始。checkout/SHA 断言/Node 安装/npm 依赖/Vitest/Worker 与 SDK 检查已通过，前端 build 及后续检查尚在执行。其余本地任务排队，保持单并发。

实际任务启动后发现服务 UMask=0077 会被 run.sh 子进程继承；官方 runner v2.338.2 的 run.sh/run-helper 未重置 umask。真实临时 Git clone 复现 tracked 100755/100644 被检出为 0700/0600，会违背原有 exact-mode policy。修复只在 unprivileged job 子进程降权后设置 0022；注册子进程与父进程仍保持私有 umask。新回归真实创建/克隆 Git 仓库，证明工作目录 0755、脚本 0755、普通文件 0644，同时注册和父进程私有文件均 0600。未放宽校验器，也未修改已运行 guest。

另复核 Docker 29.9.3 官方签名 metadata 和准确 CLI deb：Compose 只在 Recommends，包内无插件，--no-install-recommends 会跳过，现有 policy 的 `docker compose config` 因此缺少前置条件。交付包锁定 docker-compose-plugin 5.7.1-1~ubuntu.24.04~noble，官方 URL/SHA256 c2f6549958ca64466da9e31a6d488e19f5a1bf475a4425df9e4c3a010d5950ac；8,483,736-byte deb 实查 SHA 匹配且含可执行 Compose 插件。四个包均重新通过 Docker 签名 metadata 校验。新增缺失/重复/未知包拒绝、Compose smoke 失败与超时阻止模板封存测试，保留 umask 回归。

两项兼容修复后完整相关测试 **270 项通过**，workflow policy 与 diff 检查通过。前端执行中采样 MemoryPeak=10561359872 bytes（约 9.84 GiB）、CPUUsageNSec=1320879487000、IOWriteBytes=5406766080，仅为截至采样时的下界，任务尚未结束。修复需要 drain 保留当前任务到完成，再安装并重建模板；当前 run 未因推送更新而取消，修复暂未推送，避免中断首个执行/回收验证。完整 CI、job 回收与最终资源数据仍待确认。

## 2026-11-13 首个 job 超时与回收观察

首个前端 job 131791367723 于 17:23:13（北京时间）结束，conclusion=cancelled。GitHub check-run annotation 明确为 `The job has exceeded the maximum execution time of 25m0s`，并非推送取消或内存不足。Vitest、两次前端构建、浏览器安装及 Chromium 双编辑器检查均通过；首个 nginx 镜像已构建并通过 nginx -t，随后触发总时限，第二个镜像检查未执行。

主要阶段：Vitest 3 分 15 秒、Playwright install --with-deps 6 分 31 秒（APT 更新约 1 分 25 秒、包下载安装约 2 分 48 秒、浏览器下载约 2 分 14 秒）、Chromium 检查 8 分 59 秒。日志显示浏览器系统依赖新增约 299 MB。后续修复优先将官方 Ubuntu 24.04 Chromium 系统依赖纳入基础模板，保留原有检查和 25 分钟超时；不得将此次 cancelled 记为 CI 成功。

17:24 左右只读核查对应 VM unit 为 inactive/dead、GitHub runner total_count=0，控制器仍 active/drained，后续本地 job 排队。未读取受保护的最终实例日志，因此增量盘删除与最终资源峰值仍需后续状态/日志确认。此前最后采样 MemoryPeak=15498309632 bytes（约 14.43 GiB）、CPUUsageNSec=2864973644000、IOReadBytes=958629376、IOWriteBytes=13645642752，均为退出前采样下界，不作为完整 CI 的峰值。

源码已预装 [Playwright 1.63.0 官方 Ubuntu 24.04 Chromium 依赖表](https://github.com/microsoft/playwright/blob/v1.63.0/packages/playwright-core/src/server/registry/nativeDeps.ts) 的 33 项系统库、字体与工具；另核实仓库使用的 1.62.1 对应集合相同。保留签名 APT 索引，apt-get clean 仍执行，只清理 deb 下载包；索引之后由各 VM 的 apt update 重新校验，写入独立增量盘。强化原有离线构建测试，以真实临时索引文件验证清理后仍存在、完整依赖集合传给 apt、deb 清理仍执行；guest build/template 相关 50 项测试通过。实际模板空间、执行耗时与完整 CI 仍待验证，尚未同步此项交付包以避免与管理员可能已启动的维护过程并发。

## 2026-11-13 模板维护流程

用户已执行 drain，当前前端任务继续运行，后续任务留在队列。新增 `maintain-template.sh`，管理员可用一条命令等待当前 VM 回收、停止控制器、安装交付包、重建模板并恢复接单。最长等待 6,600 秒，状态异常或超时不会停止活动 VM；安装/构建失败不会执行 resume/start。

7 项离线 fake-command 测试覆盖活动实例保留、暂停与空实例两条件、初始无 journal、安装/构建失败、错误状态、status 命令失败、等待期限及非 root 拒绝；不会操作宿主服务。完整相关测试现为 **277 项通过**，workflow policy、ShellCheck、bash 语法和 diff 检查通过。维护流程尚未在宿主执行，新模板和完整 CI 仍待验收。

## 2026-11-16 维护前交付同步

用户确认 maintain-template.sh 尚未执行。只读复核：控制器及专用代理 active、NRestarts=0，没有 VM unit 或安装/模板构建进程；专用代理访问 GitHub HTTPS 返回 200。`/data` 可用约 184 GiB、专用文件系统可用约 190 GiB、宿主 MemAvailable 约 192 GiB，均高于准入门槛。当前工具仍不能免密 sudo。

将 c192967 中的 Chromium 系统依赖与 APT 索引保留修复同步到 `/data/lzq/github-runner/control/guest/build.py`，同步 PLAN/VALIDATION 并重新生成 SHA256SUMS；交付源文件逐项核对，Python 源码静态解析通过。源码未新增功能变更，沿用此前已通过的 277 项相关测试。受保护的安装快照与封存镜像尚未更新，下一步由管理员执行交付包 maintain-template.sh，完成后再核验 runner 注册和完整 CI。

## 2026-11-16 模板维护完成与新 runner 接单

读取用户保存的 `/home/devuser/nexpoly-runner-maintenance.log`：安装成功，guest 从 apt_update/apt_dependencies、四个 Docker 包、Buildx/runner 到 smoke/seal 全阶段通过；模板于北京时间 09:29:44 封存为 `base-8508969b544f2c8ed77bb2c4820205718fb59c776b4522f659ea9fb0b37d6226.qcow2`。脚本正常执行 resume/start。末尾 status 的 paused=true 是 resume 请求尚未被新控制器消费的瞬时状态；随后真实 Online/busy 证明已恢复接单。

只读核验 `/usr/local/libexec/nexpoly-runner` 的 template.py、guest build/bootstrap/smoke/software-lock 与工作树 SHA256 一致，均 root 所有；实际包含 job umask 0022、Compose 5.7.1 与 30 秒 smoke、33 项 Chromium 系统依赖、保留 APT 索引及 apt-get clean。只读模板实际分配 3,741,048,832 bytes（3.484 GiB），较旧模板增加约 532.82 MiB；专用文件系统可用约 189 GiB，宿主 /data 可用约 184 GiB。

控制器自北京时间 09:29:49 起 active、NRestarts=0。唯一执行 VM UUID=9c81588cc087418b8d57f729ebbc3080，09:30:35 启动；QEMU 参数实际为 -m32768、-smp8、bps_rd/bps_wr=104857600、sandbox 开启、IPv6 关闭。cgroup cpu.max=800000/100000、memory.max=36507222016、swap.max=0，service 为 nexpoly-vm/AppArmor=nexpoly-runner-vm/NoNewPrivileges=yes；磁盘限速由 QEMU 实施，io.max 为空不代表无限速。

GitHub API 返回 total_count=1：runner ID22、name=nexpoly-ci-9c81588cc087418b8d57f729ebbc3080、online/busy，标签匹配。PR #111 当前 head=6b3cc6213229ed74c47ba62b1ef48ff312c8dc0f；CI run 43784833577 已通过 hosted resolve-sha，新模板首个 job 133030077936（Release and deployment script tests）于 09:31:15 开始，checkout/SHA/commit identity 均通过，完整 script tests 执行中。其余 19 个本地 job 排队。此轮完成前不推送仅文档更新，以免 concurrency 取消活动任务。

执行中采样 MemoryPeak=2,716,450,816 bytes（约 2.53 GiB）、CPUUsageNSec=865454996000、IOReadBytes=565125632、IOWriteBytes=1974495232，仅为采样时下界；最终 job 结果、回收及完整 CI 仍待验收。

## 2026-11-16 两个实际 job 的失败分类与下一批修复

script-tests job 133030077936 于北京时间 09:59:14 结束，运行 2363 tests / 1594.034 秒，9 failures、190 errors、31 skipped；总 job 耗时 27 分 59 秒，没有触发 40 分钟超时。189 errors 为 `/run/user/1000` 缺失导致 clean_control_environment 提前拒绝，另 1 error 为系统 Python 缺少 pip metadata。9 failures 中 4 个为实际 UID/GID 1000 与固定 1001 身份不符，3 个 PG16 清理断言和 1 个 DFT crash 检查均被缺少 user bus 提前阻断，1 个为遗漏的 DFT all-hosted 路由校验。

Monomer-MD Worker job 133030078008 成功安装 Python、从空重建生产 Worker 依赖锁并准备精确开发测试环境；测试于 10:08:39 结束，152 failed、945 passed、3 skipped，测试耗时 47.76 秒。全部失败集中在 broker（42）、systemd inventory 协议（4）、dev GPU session（106），日志及源码均可由实际 1000:1000 与固定 1001:1001 的文件所有权、真实 socket peer credentials、进程 UID 和 scope/lease 检查解释。未发现额外缺包、网络或真实 GPU 依赖；身份修复效果仍需新模板实测。

生命周期已实测：旧 runner22 从 API 消失、旧 VM MainPID=0/inactive/dead，下一台 runner23 使用不同 UUID 在线接单；Worker 结束后 runner23 亦消失，当前 total_count=0、无 VM unit，用户已 drain。实例目录为 root 私有，当前工具未读取或挂载 guest 磁盘；不能独立将目录删除记为已检查。宿主 Docker 自 11 月 3 日保持 active，现有 42 个容器均在运行；当前健康快照为 33 healthy、2 unhealthy、7 未配置健康状态，缺少部署前健康快照，不据此断言全部业务健康或将既有容器状态归因于 runner。

源码修复固定 cloud-init 用户 UID=1001、主组 GID=1001，组创建使用 cloud-init-per once。模板安装 python3-pip/dbus-user-session，启用 runner linger 并启动 user@1001.service；bootstrap 显式依赖该 user manager，并在读取注册材料前检查账户、runtime 归属/权限、真实 socket 类型/归属及普通用户 busctl 响应。smoke 验证用户 bus 和系统 pip metadata，失败不封存。隔离 systemd 事务与独立复核确认保留 cloud-final 顺序且无循环；cloud-init schema 通过。DFT 校验器改用共享精确路由/权限规则，保留 checkout SHA、DFT control markers 和 --require-committed。

本地先恢复 managed worktree 的标准 Git 文件权限与目录写权限，再按 job 的 umask 0022 复验。发现已有 MPS fixture 的中间 slot 目录依赖环境 umask，现显式创建为 0700，并增加独立 umask 0022 回归；生产 MPS 权限和固定身份检查不变。完整 runner/CI/DFT 相关 **312 项测试通过**，MPS 模块与 PG16/DFT crash/pip 既有回归 **24 项通过**；workflow/DFT policy、ShellCheck、diff 检查通过。受保护安装快照与当前基础镜像尚未包含这批修复，需要一次维护重建后重新运行完整 CI。

## 2026-11-16 身份模板维护完成与 Buildx CSV 修复

管理员已完成第二次维护，日志为 `/home/devuser/nexpoly-runner-maintenance-identity.log`。全部阶段（含新的账号、真实用户 bus 和系统 pip metadata smoke）通过，模板于北京时间 10:43:30 封存为 `base-9b1799eafd90bb18a3ae9653b0599b8f91a0c1979bccf86a15f59f18511769d1.qcow2`。只读 stat 得实际分配 3,762,876,416 bytes（3.504 GiB）；protected template.py、guest bootstrap/build/smoke/unit 与 254cf29 源码一致。控制器 10:43:34 恢复，NRestarts=0；新 VM 的真实注册还验证了全新启动时注册材料读取前的用户 bus 检查。

run 43791813121 的首台 runner ID24 / UUID b5eaa889d3dd4dabb88255f607d33e40 已 Online/busy，执行 web 镜像 job 133051795519；之后不同 UUID 88bae1dde6e144e8ae2e6972dd1d389f 执行 PostgreSQL 16 job 133051795491。该 PG16 job 于 10:54:27 成功，setup、checkout SHA、精确匹配镜像拉取、真实 matching-major 外部媒体集成及 mutable-audit 事务 CAS 检查全部通过。QEMU/cgroup 再次核验为 8 CPU、32 GiB guest、34 GiB host、swap 0、读写各 100 MiB/s，AppArmor/NoNewPrivileges 生效。完整 20 job 和 ci-gate 仍未通过。

web 镜像 job 在 Set up Buildx 失败，日志精确报 `invalid value "127.0.0.1", expecting k=v`：driver-opt 内未引用的逗号分隔 NO_PROXY 被当作多个 CSV 字段。仅修改两个本地 Buildx 步骤，将整个 `env.no_proxy=...` CSV 字段加双引号，并同步精确 policy 与三项语义回归。代理地址和 NO_PROXY 内容不变，四个 hosted job 块与修改前逐字节一致；该修复不涉及宿主配置或模板，无需再次安装/重建。

固定 [setup-buildx-action 源码](https://github.com/docker/setup-buildx-action/blob/8d2750c68a42422c14e847fe6c8ac0403b4cbd6f/src/context.ts#L34) 使用 `getInputList` 的 `ignoreComma:true, quote:false`，其锁定的 [actions-toolkit 0.63.0](https://github.com/docker/actions-toolkit/blob/v0.63.0/src/util.ts#L38) 保留整项引号，再由 [Buildx 0.31.1 的 CSV 解析](https://github.com/docker/buildx/blob/v0.31.1/builder/builder.go#L567) 处理。使用实际 csv-parse 5.6.0 和 Buildx 锁定的 go-csvvalue 030d3b2625d0 分别验证：新写法保留完整六个 NO_PROXY 主机，旧写法精确复现错误，仅引用等号后的值也失败。验证不调用宿主 Docker daemon。相关 workflow/DFT **128 项测试通过**，workflow policy、actionlint 1.7.7（含 ShellCheck）与 diff 检查通过；完整 CI 将随修复推送重新执行。

## 2026-11-16 完整 CI 的缓存与容器代理问题

run 43795108192 已真实通过全部 2383 项脚本测试（1681.328 秒，19 skipped）、三个后端分片、DFT Worker、policy、MCP、生产别名集成及 PG14/15/18 等 job。此前身份、用户 bus 和 pip 修复的主要失败项已通过；仍不能把本轮标为完整通过。

Web job 133062105529 的 npm/Vite 编译、镜像导出和 Docker load 均成功，失败发生在 GHA cache export：约 2011.9 秒后 CreateCacheEntry 返回 unexpected EOF。目标 results-receiver.actions.githubusercontent.com 已在原官方白名单内；同轮 OpenScience 的缓存导出成功，未发现放宽白名单的依据。两个本地 cache-to 增加 timeout=5m,ignore-error=true，保持 mode=max/scope 和 cache-from。仅缓存导出错误容错，镜像构建、导出、加载、身份和测试继续阻断；云端发布块不变。[Docker GHA 参数](https://docs.docker.com/build/cache/backends/gha/) 支持这两个选项。实际运行 BuildKit v0.34.2 的缓存库将 Timeout 用于请求重试预算，v2 Azure 上传不保证受该值限制，因此不能声称总上传硬截止为 5 分钟。

轻量 job 133062105440 在 Python 3.11.18 安装成功后恢复 196,643,524 bytes pip 缓存，停在 117,440,512 bytes / 59.7%，报告下载超时后未及时退出，最终触发原 10 分钟期限。固定 setup-python SHA 的捆绑 @actions/cache 4.0.3 超时包装器仅在成功路径清除计时器，拒绝路径可能遗留默认 10 分钟计时器；具体 Node 句柄没有采样，不能把该解释视为现场已证实。去除 lightweight-backend-tests 和发生相同缓存停滞的 worker-tests 两项可选 pip cache；保留 Python SHA/版本、10/25 分钟期限、全部哈希锁、空环境运行时准备及完整测试。其余已成功的 Python cache 保留，Node 24 提示没有被当作原因。

OpenScience job 133062105557 的镜像构建/加载/缓存导出及验证开头 4 项 Node、13 项 Python 测试通过，之后静默约 49.5 秒退出 1。固定父镜像的 healthcheck 为 BusyBox wget 请求 127.0.0.1:4454/healthz。通过 GHCR 匿名只读下载、校验 OCI layer，提取同镜像 BusyBox 1.37.0-r12 并用其 musl loader 在临时目录执行：即使 NO_PROXY/no_proxy 包含 127.0.0.1，它仍选用 http_proxy，明确复现本机请求误走代理。原日志没有失败断言，具体退出行仍需重跑证实。三个本地静态服务验证脚本对新建的本地 nginx/browser fixture 显式覆盖六个出站代理变量为空；镜像 build/pull 代理和既有 --container 目标配置保持原有行为。失败时输出阶段、行号、有限 State.Health/容器日志，并为启动/就绪查询设置期限，不输出 Config.Env 或凭据。

MD job 133062105566 同样停在 pip 缓存，06:16:28 UTC 报恢复超时，06:20:14 收到 runner shutdown signal；运行时准备及 pytest 均未开始。管理员导出 /home/devuser/nexpoly-runner-md-shutdown.log 显示控制器 06:20:46 才以 github_job_completed 发起关机，06:22:46 在宽限结束后终止 VM，06:22:49 完成回收；没有提前触发启动/执行期限。初始信号来自 guest/runner 的路径尚未定位，独立保留未解，不声称关闭 pip cache 已修复它，也不基于猜测重建模板。

修复后的 workflow/DFT policy、本地容器代理、OpenScience 静态准备及 Compose delivery 相关 **167 项测试通过**。新增 8 项离线测试用 Docker CLI 替身模拟真实 config.json 代理注入与 --env 覆盖优先级，移除覆盖参数可复现失败；检查 build/pull 仍继承代理、配置文件未改、健康/HTTP 等待失败仍返回非零且输出有限诊断、既有 --container 目标不被重建或输出日志、预期 grep 零匹配保持安静以及真实父层失败只报告一次。ShellCheck 0.10.0 默认级别、Bash 语法、workflow policy、actionlint 1.7.7 和 diff 检查通过；四个 hosted job 块与修改前逐字节一致。未调用宿主 Docker 或修改系统服务。实际容器验证及完整 CI 将随修复提交重跑；关闭信号与完整验收仍保留未解。

## 2026-11-17 已授权的 17891 上游迁移交付

用户在两条上游下载对照之后，明确授权将 runner 专用 Squid 的上游改为 `127.0.0.1:17891`。当前配置示例、common.Config 固定契约、host 配置校验和首次 APT bootstrap 已同步；专用 Squid parent 与 nft proxy UID 规则仍由同一 network.upstream_port 渲染，VM UID 仅允许27892，不新增直连上游或其他出口。旧27891实测和故障记录保留为历史证据，不能视为新路径已应用或已长期稳定。

既有安装器保留 `/etc/nexpoly-runner/config.toml`，因此只改 example 或重装并不能完成此迁移。新增 `switch-upstream.py` 在现有全局锁内要求已drain、controller停止、无活跃VM、journal.instance=null及实例目录为空；先验证候选配置/私有源码快照，仅替换现有network.upstream_port整数，保留App IDs等其他字节，并写0600旧config备份。之后通过正常安装器升级受保护代码并同步应用专用Squid/nft，精确检查防火墙与27892 HTTPS。失败保持drained/stopped，不自动恢复接单、重建模板、修改共享隧道或触碰App私钥。

原 maintain-template 仅等待 instance 消失，而 drain 明确不会将 registered idle/busy=false 视作任务完成，可能等至 110 分钟。本轮统一入口为 `sudo bash /data/lzq/github-runner/control/maintain-runner.sh`，配置与安装版本一致时能复用已 stopped/drained 状态。若配置已迁至 17891 但早期安装失败、受保护旧程序仍要求 27891，须保持无 VM 且 drained/stopped，先单独重跑 `switch-upstream.py` 修复版本一致性，再重跑统一入口，不能直接用旧 runnerctl 开始整体流程。它先使用受保护的已安装旧模块安全隔离待机路由，在至少两次、间隔不低于 30 秒的完整实际 jobs 检查后才退役；真实 job 保留并在旧 drained 控制器下自然完成，API 不确定时恢复路由后退出或保持停止。低层迁移 helper 遇到遗留 instance 仍立即拒绝。整个流程未在宿主执行。

离线测试覆盖配置字节/IDs/注释保留、旧/新端口契约、私有源码验证先于配置修改、安装失败保留备份、不报告防火墙假成功、重复应用及 registered idle 拒绝。历史 nftables 1.0.9 现场 fixture 保持原始 27891 端口，测试先拒绝旧规则，再仅迁移 proxy chain 端口验证数值表示仍兼容。退役 fake API/VM 测试覆盖路由隔离前落盘、派单竞争、busy=false 下实际 job、刚完成 run、秒精度时间边界、裸 404、API 失败、路由恢复、删除后中断重试、受保护 import 目录、正常 cgroup 清理及不补机；没有真实 API mutation、VM、Docker 或服务操作。

## 2026-11-17 两项关闭故障的调查与诊断交付

剩余 script-tests 和 backend0 的初始终止来源仍未知。前者 57 项测试通过后中断；后者在 pip 安装时结束，APT 与 pytest 尚未开始。对 test_adopt_runtime_prerequisites 的全部 117 项隔离测试独立复验，846.769 秒全通过；这些测试使用替身，未调用真实 Docker/systemctl，未发现终止用户 bus 或 Docker 的测试。两项 VM 内存峰值约 6.52/3.37 GiB；宿主 stop 分别晚于已观察到的报错约 18.8/23.1 秒，105 分钟执行期限及 600 秒启动期限均未触发。这些证据不支持“宿主资源上限或定时器先杀任务”的解释；旧 VM 已销毁，不能追溯确定最初触发者。

新增 guest 诊断只采集固定枚举的服务状态、退出/信号分类、OOM 计数与 guest uptime；控制器在回收前读取，最多保留 64 条到既有私有 JSON 资源记录。不保留原始日志、环境或任意错误文本，不读取 guest 文件系统，不以 guest 事件认定 job 完成。旧模板仍兼容既有窄 runner_exit 事件；完整新诊断须维护入口重建模板后生效。控制器原有 API 认证、期限、关机宽限和 cgroup 清理条件保持不变。guest 相关 72 项、controller 相关 70 项测试通过，最后补强后的 retirement/upstream/host/controller/diagnostics 与统一入口编排合并 157 项离线测试通过（4.928 秒），其中入口 10 项使用真实 source guard/函数及 fake 命令，未操作服务；Bash 语法和 diff 检查通过。实际原因与完整本地 CI 验收仍待确认。

最终整合复验共 **378 项通过（22.273 秒）**，包含控制器、模板、guest 诊断、上游迁移、待机退役、两套维护入口、工作流策略及镜像资源检查；actionlint、ShellCheck、workflow policy、monomer DFT 提交契约和 diff 检查通过。此前独立的 117 项脚本测试不重复计入该 378 项。交付前再次直接经 17891 请求 GitHub robots.txt 返回 HTTPS 200（约 0.49 秒）；这些结果不代表系统中的上游已切换，也不替代两项实际 CI 重跑。

## 2026-11-17 首次维护执行：代理迁移成功，模板因 HTTP 502 中止

用户执行统一维护入口后，原待机实例 `e9a57ce6656149a583bdca88db1af256` 已先隔离路由、三次核对实际 jobs，再按普通关机/完整 cgroup 流程回收，未执行任何 job。现有 `/etc` upstream 与 Squid parent 均为 `17891`；入口的专用 nft 精确校验和 `27892 → 17891 → GitHub` HTTPS 检查成功。随后只读复查专用代理 GitHub HTTPS 200、约 0.68 秒；控制器保持 inactive，未恢复接单。

候选模板在 apt_update 失败、退出 100。构建日志明确显示 `http://security.ubuntu.com/ubuntu/dists/noble-security/InRelease` 返回 HTTP 502，其他 archive.ubuntu.com 的 noble、updates、backports 索引成功；之后的 “no longer signed” 伴随该 InRelease 获取失败，没有证据表明官方签名或时钟错误。当前同一安全索引经 27892/17891 的 HTTP/HTTPS 四路完整 GET 全部恢复 200，不能将故障归为 HTTP 固定受阻、白名单拒绝或隧道永久失效。失败候选未作为新模板启用，剩余两项 CI 尚未重跑。

guest APT 保持原源地址和签名/TLS 校验，在既有专用代理配置中增加 `Acquire::Retries=3`、HTTP/HTTPS 单次连接/数据超时 30 秒，并要求 `APT::Update::Error-Mode=any`，元数据仍缺失则失败。沿用宿主一小时模板制作总期限，不另做无限/整轮重试。原生 APT 2.8.3 在完全隔离的 localhost fixture 中，两次 502 后第三次 200 实测成功（约 1 秒、2 秒退避，总 3.038 秒）；错误 SHA256 仍退出 100、拒绝接受文件。该证明仅使用临时配置和固定测试数据，无外网、root 或宿主 APT 更新/安装。实际生成的 guest 配置另以 apt-config 解析六个固定字段验证。

## 2026-11-17 第二次维护：APT 通过，Docker 下载出现半包风险

第二次执行统一入口后，apt_update 和 apt_dependencies 已成功，候选在 docker_packages 以 validation_error 退出。失败候选已回收；controller inactive、无活跃 VM，专用代理保持 17891 且 GitHub HTTPS 检查返回 200；未 resume、未重跑 script-tests/backend0。最初导出的 console tail120 只有关机尾部，缺少原始 traceback，关机序列中的 cloud-init signal15 不能当作 Docker 下载的初始失败原因。

四个锁定 deb URL、版本和 SHA256 均先经过 Docker 官方签名 InRelease → Packages.gz 哈希 → 精确包记录核验；本次已进入 VM 说明当时验证通过。只读经专用 27892 流式下载、不保存/安装：docker-ce 为 24,586,336 字节/7.865 秒，docker-ce-cli 为 17,584,604 字节/6.737 秒，containerd.io 为 23,271,468 字节/7.813 秒，docker-compose-plugin 为 8,483,736 字节/2.863 秒；全部 HTTP200、curl0、SHA256 与原锁一致。因此未修改版本、URL、hash 或签名规则。

但使用与旧 guest 相同的 Python urllib 分块读取首包时，实测 HTTP200、Content-Length=24,586,336，约4.714秒后只收到9,539,441字节便 EOF，无 urllib 网络异常；SHA256不匹配。旧下载器的固定 SHA 已阻止半包继续安装，缺口在于未检查长度、未作有限重试、未区分具体下载失败。该复现说明链路可能提前结束，不能在缺少原始 traceback 时断定原构建失败的具体包或网络节点。

另外各一次 curl 默认模式和 `--http1.1 --user-agent Python-urllib/3.12` 对照：前者协商 HTTP/2，8.203秒；后者 HTTP/1.1，8.224秒。两次均 HTTP200、Content-Length和实际字节数同为24,586,336，hash匹配；未复现短读，不能宣称 HTTP版本或 UA 必然触发/修复故障。这些小样本只证明当前请求成功，不保证隧道持续稳定。

guest 下载器现使用 curl，禁用用户 curl 配置，显式限定专用代理、清除代理绕过、仅允许 HTTPS 初始和重定向请求，保留 TLS 校验；每制品上限1 GiB，连接和低速停滞均有期限，最多3次尝试共享600秒预算，hash分块也检查同一期限。临时文件与目标同目录，检查 Content-Length（存在时）并始终校验原 SHA256，成功后才原子替换；没有哈希正确性或代理直连回退。短读、超时、可恢复传输错误和部分HTTP状态可重试；403/404、TLS信任失败、体积超限和文件系统错误立即失败。失败临时文件清理异常安全归类，不保留原始stderr。

新增制品事件仅输出固定枚举文件名、阶段/错误分类、attempt和有界状态/字节数字；宿主再次白名单过滤，不保留URL、远端正文、任意异常文本或令牌。10项新下载行为测试、6项原guest build测试与5项宿主诊断过滤测试合计21项通过（0.051秒），覆盖无异常短EOF、curl部分传输退出码18的模拟分类、三次上限、共享期限、错误hash、原目标保留、原子发布、清理失败及文本泄露防护；未调用真实curl/服务。实际网络探针与这些离线测试分开记录。新交付尚未应用到成功封存的新模板，不能替代实际维护和两项CI验收。

最终相关整合复验 **97 项通过（4.591 秒）**。将新 guest 下载器在宿主上仅把代理入口映射为同一专用链路 `127.0.0.1:27892`、其余 HTTPS/长度/SHA/临时文件/替换逻辑保持原样，真实复取 docker-ce：首次尝试即取得 24,586,336 字节，响应长度相同，SHA256 与锁匹配，耗时 5.180 秒。固定 started/verified 事件完整，验证记录为 `/data/lzq/jobs/runner-download-verification-20261117.json`，包文件已随临时目录清除。此探针不需要 root，未安装包或启动 VM；它验证下载器和当前链路，不等同于完整模板已构建成功。

## 2026-11-17 第三次维护成功与两项 CI 分别复验

管理员第三次执行统一入口，日志 `/home/devuser/nexpoly-runner-maintenance-retry2.log` 显示六个制品（四个Docker deb、Buildx、runner）均第1次尝试通过长度和原SHA256校验，随后smoke、seal成功。新模板 `base-13de146aa1b1c105703fa9882f3607b3a47267d8819353b9e158be10a460c3da.qcow2` 于北京时间14:32:21封存（06:32:21 UTC）。已安装build.py、template.py、controller.py与交付源码哈希相同；新诊断和下载加固已包含在该模板制作/当前控制器中，完整job中的诊断收集仍随本次CI验证。

控制器14:32:25恢复active、NRestarts=0；14:33:14唯一执行VM `d8468c50d212459b9b72664b199d1057` 启动，GitHub runner82为online，标签为self-hosted、Linux、X64、nexpoly-ci-ephemeral。8vCPU/32GiB guest、34GiB宿主上限、swap0、AppArmor、NoNewPrivileges与读写限速保持生效。实际config/Squid上游均为17891，专用规则精确校验成功，27892经17891到GitHub HTTPS实测200/0.827秒；这次成功不构成隧道持续可用性保证。

已实际POST单项重跑原run43817712995的script-tests job133133627388，attempt2的新job133867148568由runner82执行并成功，起止为06:35:56—07:07:24 UTC（北京时间14:35:56—15:07:24）。测试日志为2397 tests / 1694.205秒、19 skipped、0 failures/errors；旧中断附近的test_abort_refuses_completed_authority明确ok，未出现runner shutdown或实际warning/error。head未改，仍为fd459377e45aae46ea77fc3041986601abb7415a。

宿主采样记录 `/data/lzq/jobs/runner-ci-verification-20261117/script-metrics.jsonl` 的内存峰约3.041 GiB，OOM/KILL计数均0。这是本次script复验的采样结果，不替代所有job的资源峰值或完整压力验收。

script attempt整体结束后，仅启动backend0单项重跑，新job133881233680使用runner83、VM0dcd8018ced24398a7875c3f5063c1e9，07:11:06—07:51:34 UTC（北京时间15:11:06—15:51:34）结束为cancelled；GitHub annotation明确为exceeded maximum execution time40m0s。本次取消来源已确认是workflow既有40分钟期限，未提高超时或省略检查。

Set up Python步骤为07:19:38—07:49:34，共29分56秒。日志分段确认：官方Python发布包下载07:19:42.166—07:39:17.791，耗19分35.625秒；07:39:29.077安装成功。pip cache 140,922,306字节于07:39:31.578命中，07:41:15.190报“Failed to restore: The operation cannot be completed in timeout”，进度为0字节；直到07:49:34.297才进入pip。cache hit→pip为10分02.718秒，warning→pip为8分19.106秒。

旧backend0同样存在cache hit→pip 10分03.336秒、warning后再等待约7分48秒的现象；额外等待所对应的具体进程句柄未现场采样，不能据此确认旧shutdown最初触发。当前这次日志没有runner shutdown signal，仅有明确的40分钟workflow取消。pip安装07:49:34—07:51:29仅运行1分55秒，尚未到Installing collected packages或越过旧失败断点，PG客户端安装、pytest、离线MD检查均skipped，不能记为这些测试失败或通过。

backend的241条宿主metrics采样显示内存峰约4.103 GiB、CPU累计316秒、I/O读约0.561 GiB/写约2.595 GiB，OOM/KILL均0；数据仅覆盖准备阶段，不能代表完整backend测试资源峰值。runner83及旧VM已正常回收；当前唯一runner84/nexpoly-ci-eefe8e2f26cc4d4b85e491b67fb89dbe为online/idle，对应唯一active VM；控制器仍active、NRestarts=0，27892专用GitHub HTTPS复查200/0.894秒。

本轮script成功证明该任务已通过新模板的实际复验；backend0本轮因已知期限取消，仍未完成测试验收。旧shutdown的最初触发仍无法追溯，不能将本次有明确超时注释的取消用于解释旧信号。该轮另四项旧网络失败未重跑，PR整套CI未通过。随后用户选择保留Backend缓存并延长期限，后续配置与完整复验要求见下一节。

## 2026-11-17 保留缓存并将 Backend 期限调整为75分钟

用户明确授权调整上限并重跑完整CI。`backend-tests`唯一的job级timeout从40改为75分钟，覆盖原有三个矩阵分片；pip cache及依赖锁路径保留。script-tests仍为40分钟，所有其他job块（包括四个hosted job）保持原样，宿主105分钟兜底、资源和单VM并发约束不变。依据本轮准备耗时约38分钟及此前成功shard0主测试约11分钟，75分钟用于留出完整测试和额外依赖预算；这是验收预算调整，不证明下载或缓存链路已修复。

增加workflow policy精确验证唯一的75分钟job字段。4项新增回归覆盖旧40、105及更高值、其他未审阅值、表达式、重复、缺失和伪步级字段，并验证policy CLI拒绝旧配置。最终复验共397项通过：runner相关251项、镜像资源与DFT契约34项、workflow policy112项；actionlint1.7.7、ShellCheck0.10.0及workflow policy检查通过。提交后还需复核`--require-committed`，再推送新head触发完整20个本地job及hosted gate；旧run重跑不会读取新75分钟配置。当前没有将完整CI记为通过。

## 2026-11-18 CI #290 的失败定位与修复

run44045595151/head34cb3f3于北京时间11月17日22:37结束，20个本地job为17成功、1失败、2取消。三个Backend分片、两个镜像构建、OpenScience overlay、DFT/MD、policy、PostgreSQL等任务均通过；ci-gate明确汇总script-tests failure、property-filter-mcp-tests cancelled、frontend cancelled，并非新增独立错误。

script-tests运行2535项/1648.833秒，1 failure、1 error、19 skipped。两项GuestBootstrapTests只隔离RUNTIME而未隔离默认RUNNER，真实self-hosted job存在`/opt/actions-runner/_diag`，因此触发新增的注册诊断清理保护；本地无该目录时原测试会通过。纯临时路径/查询替身复现了同样的两处失败。本次修复测试夹具使用临时RUNNER/RUNTIME，实际清理临时诊断目录，禁止接触live runner日志；生产guard与凭据清理顺序不作调整。

frontend annotation明确超过25分钟期限。188个Vitest文件/2147项测试通过；浏览器安装耗5分59秒、浏览器功能验证8分31秒、两次镜像验证合计4分58秒。最后账户UI四例已输出passed，但随后的browser.close或外层清理是否完成无法确认，不能将被取消的step记为成功。仅将frontend job期限调至35分钟，policy精确限制唯一job级字面量；完整测试、双构建、浏览器与镜像验证、缓存和其他job期限保持原样。

MCP实际运行17分14秒，setup-python被取消，依赖安装和测试均skipped；annotation含cache restore timeout，却无20分钟最大期限信息。GitHub单job日志返回404、完整run日志包也缺该job。管理员提供的实例摘要显示runner_exit=0、unit_stop exited/success、无bootstrap_signal、guest全局及unit OOM计数和delta均0，Docker/user manager仍active；宿主observed_at晚于job API完成约16秒，只能作为收件时间。现有证据不支持提前杀VM或20分钟期限解释，但不足以判定取消发起源；保留该未知项，后续完整CI继续复验。分析摘要保存于`/data/lzq/jobs/runner-ci290-analysis-20261118/`。

本次修复整合验证403项通过（runner相关253项、workflow/镜像资源/DFT契约150项），workflow policy、actionlint1.7.7与集成ShellCheck检查通过。独立harness模拟live runner诊断目录存在时，原两项测试均通过，真实路径查询为0，所有删除调用均限制于临时路径。11月18日推送前预检为唯一runner104在线空闲，控制器与代理active、NRestarts0；仓库、交付包与受保护安装的30个核心文件哈希一致，无需重装或重建。提交后仍须执行`--require-committed`再触发新完整CI；此处未将待运行的CI标记为通过。

## 2026-11-18 CI #306 的 DFT 服务中断与模板维护

[run44220723057](https://github.com/lzq390/ZhijuPoly/actions/runs/44220723057)/head `7aea83b` 的 20 个本地 job 已全部结束，19 成功、1 失败。脚本 2559 项/1708.215 秒、19 skipped，原两个 GuestBootstrapTests 明确通过；frontend 的 2147 项 Vitest、最终生产镜像账户 UI 四例及后续清理均成功；MCP 的锁定依赖安装、134 项测试及 6 项验收测试成功。MCP 的可选 pip 缓存恢复超时未阻止安装和测试，随后成功保存新缓存。Production 0005 于北京时间 14:30:47—14:32:08 成功运行，先前排队显示来自旧列表状态。完整 run 的失败仅为 DFT 及依赖它的 ci-gate，两个发布任务在 PR 上正常跳过。

DFT job `134521642404` 使用 runner122、VM `f3d209a9add8497082039f56bfa73f35`。Python 3.12.16 安装成功，124,767,101 字节 pip 缓存仍在传输至 33.6% 时 runner 收到关闭信号。依赖安装与 Worker pytest 均未开始，不能将此次失败归为测试断言、缓存恢复超时或 35 分钟任务期限。

管理员导出的 VM 摘要与控制器 journal 按同一宿主时钟对照：14:26:26 已收到 bootstrap SIGTERM15 和 unit_stop killed/15，14:27:12 才记录 vm_stopping/github_job_completed，14:29:12 才由宿主 PID1 终止 QEMU，14:29:14 完成回收。初次信号早于控制器回收约 46 秒；回收不是最初中断原因。guest 全局与 unit OOM 计数及 delta 均为 0，QEMU 内存峰 4.57 GiB。`service_result=success` 不代表自然退出，也不提供发起者。APT 升级 activating/unattended-upgrades active 只是状态快照，不能据此证明 needrestart。

当前实现未保留普通 job VM 的 guest journal，实例盘和运行时串口已正常删除，无法继续追溯本轮信号发送者。安全分析摘要及证据索引位于 `/data/lzq/jobs/runner-ci306-analysis-20261118/`。本次在新模板中限制后台自动更新，并补充受界限约束的服务事务元数据，属于消除未受控后台行为和改进诊断，不宣称已证实旧 SIGTERM 的根因。

整合验证共 **423 项通过**：runner 全部 273 项（13.226 秒），workflow/镜像资源/DFT 契约 150 项（13.490 秒）。新增回归包含真实子进程的 POSIX/flock 锁占用、Perl 有效 needrestart 策略及冲突拒绝、64 KiB 实际管道上限、诊断共享 2 秒预算、查询/解析失败仍保留退出证据，以及注册前不查询、未知/恶意字段不转发。workflow policy、DFT `--require-committed`、actionlint 1.7.7、ShellCheck 0.10.0、Python 语法和 diff 检查通过，独立复核无阻断项。未执行宿主 APT、服务变更或部署；真实新模板及 DFT 复验仍待管理员维护。

管理员随后执行 `sudo bash /data/lzq/github-runner/control/maintain-runner.sh` 成功，输出保存在 `/home/devuser/nexpoly-runner-maintenance-dft.log`。旧 runner125 经派单核验和正常关机退役；安装及代理/防火墙验收通过，六个锁定制品均第 1 次尝试校验成功，`apt_quiesce`、smoke、seal 通过。新模板 `base-69909bf9c1034ce07e6c5d524b850355e41bd6ab63bdf05825ce93476ff2a03d.qcow2` 于北京时间 15:49:23 封存，controller/build/exit_diagnostics/smoke/template 五个核心文件在源码、control 和受保护安装中 SHA256 一致。runner126/VM `fa0ad51db6234667a53f46baed5a1d95` 在线，实际 AppArmor enforce、KVM 8 CPU/32 GiB、宿主 34 GiB/swap0 均通过只读验收。维护末尾的即时 status 曾显示 paused=true/instance=null，控制器随后已处理 resume 并补充了该 VM，不能把该瞬时输出误判为维护失败。

通过 GitHub 插件只重跑该 run 的失败项；attempt2 保持 head `7aea83b`，其余成功项由 GitHub 复用（job ID 复制、原执行时间与 runner 身份保留）。DFT 新 job [134662267111](https://github.com/lzq390/ZhijuPoly/actions/runs/44220723057/job/134662267111) 于北京时间 16:01:53—16:05:56 成功，共 4 分 03 秒；Python、124,767,101 字节 pip 缓存恢复、`--require-hashes` 锁定依赖安装和 `pip check` 均成功，Worker pytest 实际 collected=282、passed=282，耗时 30.01 秒，0 failed/error/skipped。缓存命中到完整恢复约 63.7 秒。11 个实际步骤全部 success，post Python/checkout、job-completed hook 与 orphan cleanup 均完成，无 runner shutdown、取消、缓存恢复超时或错误，仅有 Node 弃用警告。

host cgroup 每 5 秒采样共 32 次，覆盖 16:03:33—16:06:08 的有效窗口；内核 MemoryPeak 记录 5,630,296,064 字节（5.244 GiB），已采样 max/oom/oom_kill 与 CPU 节流均为 0。guest OOM 不可读，未记为 0；采样不能代替最后几秒的最终回收资源摘要。16:06:13 旧 VM/cgroup 已退出，随后出现新的唯一 VM `facf7db019744f49913257fb35ff128a`，证明控制器已完成旧实例回收并开始补机。控制器和代理 active、NRestarts0。

新 [ci-gate job134664184311](https://github.com/lzq390/ZhijuPoly/actions/runs/44220723057/job/134664184311) 于北京时间 16:06:00—16:06:05 成功，实际日志为 `all applicable CI jobs succeeded`。run44220723057 attempt2 completed/success：22 success、2 个 PR 发布任务 skipped，无失败或取消。20 个本地 job 均成功，20 个不同 runner 身份；其中只有 DFT 使用本次新模板重新执行。后续唯一 runner127 对应上述新 VM，GitHub online、busy=false，旧 runner126 已删除，回收与补机完成。结果与安全摘要保存在 `/data/lzq/jobs/runner-ci306-analysis-20261118/`。此次复验没有证明旧 SIGTERM 发起者，亦不替代 main 托管发布及其余未执行的故障注入验收。

后续同类修复需经统一维护入口重建模板，不能仅安装后使用旧镜像。安装或模板验收失败不会 resume；末尾 resume 后若 start/status 失败，需检查实际状态，不能假定已自动恢复 drain。

## 复验命令

在仓库根目录：

```bash
python3 -m unittest discover -s scripts/tests -p 'test_local_runner*.py' -q
python3 -m unittest scripts.tests.test_ci_image_resources scripts.tests.test_validate_workflows scripts.tests.test_validate_monomer_dft_release_contract -q
python3 scripts/ci/validate_workflows.py
python3 scripts/validate_monomer_dft_release_contract.py --require-committed
actionlint -config-file .github/actionlint.yaml .github/workflows/ci.yml
shellcheck -x -P ops/self_hosted_runner scripts/ci/prepare_image_build_disk.sh ops/self_hosted_runner/install.sh ops/self_hosted_runner/maintain-template.sh ops/self_hosted_runner/maintain-runner.sh ops/self_hosted_runner/guest/job-started.sh ops/self_hosted_runner/guest/job-completed.sh
git diff --check
```
