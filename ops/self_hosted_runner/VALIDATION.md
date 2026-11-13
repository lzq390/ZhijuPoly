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

## 复验命令

在仓库根目录：

```bash
python3 -m unittest scripts.tests.test_local_runner_controller scripts.tests.test_local_runner_host scripts.tests.test_local_runner_template scripts.tests.test_local_runner_guest_build scripts.tests.test_local_runner_guest_ordering scripts.tests.test_local_runner_maintenance scripts.tests.test_ci_image_resources scripts.tests.test_validate_workflows -q
python3 scripts/ci/validate_workflows.py
actionlint -config-file .github/actionlint.yaml .github/workflows/ci.yml
shellcheck scripts/ci/prepare_image_build_disk.sh ops/self_hosted_runner/install.sh ops/self_hosted_runner/maintain-template.sh ops/self_hosted_runner/guest/job-started.sh ops/self_hosted_runner/guest/job-completed.sh
git diff --check
```
