# OpenScience M0 开发基线核对（2026-11-19）

**两个独立的 OpenScience 后端可以并发冷启动，在普通和项目工作区分别保存并读回合成模型的回复。** 原 19111 开发镜像仍基于 10 月后端。本轮已在独立环境用 11 月基底构建带补丁的实验候选。将候选纳入正式构建、按账号路由工作区和逐登录撤权仍属 M1/M2。下文的 A/B 指两个测试容器。NexPoly 登录、跨账号访问与撤权仍待双账号验收。

本轮对应[隔离规划的 M0](../openscience-per-user-instances-plan.md)，源码基线为 `33861c0`。使用本地缓存镜像、专用临时卷、带 A/B 标记的合成会话和无外网的回环夹具。未调用生产 9011、现有开发服务、真实模型、知识库或科研执行器。

## 两实例实测

正式记录取自北京时间 15:49–15:52 的连续重放。三个档位合计启动 **18 个全新后端**。下表时间单位为秒，写作**中位数（最短–最长）**。健康计时从发起 `docker start` 算起，其余两列从发起 `docker create` 算起。

| 镜像和负载 | 启动样本数 | 健康接口就绪 | 工作区可读 | 工作区可读且两个 MCP 已连接 |
|---|---:|---:|---:|---:|
| 11 月基底 | 6 | 4.25（4.04–4.39） | 4.56（4.37–4.71） | 无 MCP |
| 11 月基底 + 双 MCP | 6 | 4.18（4.05–4.35） | 4.51（4.37–4.67） | 4.60（4.46–4.76） |
| 11 月补丁候选 + 双 MCP | 6 | 4.25（3.45–4.40） | 4.57（3.70–4.73） | 4.67（3.79–4.82） |

每个档位的对话负载为 A/B 各 4 轮。就绪后 6 秒一列取六个实例各自工作集中位数的范围，其余列取该档最后一对。内存单位均为 MiB，成对数值顺序为 A / B。

| 镜像和负载 | 就绪后 6 秒单实例工作集 | 对话时单实例峰值 | 对话时双实例合计峰值 | 对话时平均 CPU（单核） | 对话后 10 秒单实例工作集中位数 |
|---|---:|---:|---:|---:|---:|
| 11 月基底 | 215.3–227.4 | 271.2 / 275.7 | 543.6 | 58.1% / 61.8% | 258.5 / 263.5 |
| 11 月基底 + 双 MCP | 217.1–236.1 | 271.3 / 288.0 | 559.3 | 63.9% / 63.8% | 260.5 / 275.0 |
| 11 月补丁候选 + 双 MCP | 217.6–230.7 | 282.7 / 291.4 | 570.6 | 63.8% / 62.4% | 266.5 / 273.3 |

从启动到对话后观察窗口结束，含缓存的单实例 cgroup 内存高水位在基底档为 276.6 / 283.9 MiB，在基底加双 MCP 档为 282.0 / 293.3 MiB，在补丁候选档为 287.1 / 296.7 MiB。所有测量窗口的 OOM kill 均为 0。三个档位各读回 8 条合成回复，合计 24 条。所有临时容器和数据卷清理后计数均为 0。

- 11 月基底：无对话的新卷占盘 124 KiB/实例，各完成四轮对话后的新卷占盘 2.38 MiB/实例。
- 11 月基底 + 双 MCP：无对话的新卷占盘 128 KiB/实例，各完成四轮对话后的新卷占盘 2.38 MiB/实例。
- 11 月补丁候选 + 双 MCP：无对话的新卷占盘 128 KiB/实例，各完成四轮对话后的新卷占盘 2.43 MiB/实例。

占盘使用卷内 `du -s -B1` 的已分配块，未计共享镜像层及 Docker 日志。各轮使用不同新卷，四次短回复的总占盘不宜换算为长期每轮增长率。

三个档位分别做三轮，每轮 A/B 并发创建和启动。每个后端都使用新容器、新数据卷和不同的非特权 UID。镜像已在本机，不清空宿主页缓存。冷启动从发起 `docker create` 起算，含创建和启动，到实际工作区读取成功。MCP 档另计两个合成 MCP 均报告已连接的时间。创建和初始化卷、启动回环夹具在计时前完成。健康探针在后端 cgroup 之外运行，探测轮询间隔约 50 ms。这里的时延没有包含平台登录、镜像下载或将来编排服务的排队时间。

每个档位最后一轮，A 和 B 同时各建一个普通会话、一个项目会话，每个会话连续两轮，并从服务端重新读取已保存回复。模型返回短固定标记。MCP 档模拟能力网关和知识服务的协议连接，不执行实际工具。每个后端限 **1 vCPU、2 GiB 内存、256 PID**，无 GPU，无宿主发布端口，禁用自动更新。每用户的后端只加入其测试夹具的回环网络命名空间。

资源数据来自各后端的 Linux cgroup v2，间隔约 200 ms。工作集按 `memory.current − inactive_file` 计算，CPU 是测量窗口内相对单核的平均使用率。`memory.peak` 是从进程启动以来的 cgroup 计量峰值，含文件缓存。两实例合计的采样峰值取同次采样之和。数字只计后端，未计测试夹具、共享 UI、网关、排队中的科研作业或生产流量。

## 镜像与补丁

本轮使用本地镜像库，按 Docker image ID 锁定输入。表中短 ID 唯一对应本轮本地镜像。基线核对只读取离线临时容器里的软件文件；Bun 转译在无网、只读的一次性容器内执行，未连接生产 9011。

| 软件 | 参考镜像标签 / ID 前缀 | 19111 开发派生 / 核对结果 |
|---|---|---|
| OpenScience 后端 | `53293fc-release-20261119` / `a828b3723cdf` | `43ecd2db66b8` 的基础层匹配 10 月基底 `release-20261029` / `7e4c055b4641`，与 11 月 19 日参考镜像不同 |
| OpenScience UI | `9e342b5-production-dual` / `fea8a3c294bb` | `1137b90ac10a` 的基础层匹配；按仓库资源生成的入口 HTML 与派生镜像逐字节一致 |
| 能力网关 | `91f5d81-release-20261111` / `fb7fdd70e46d` | `c0d701e0d556` 的基础层匹配；仓库补丁生成的 `mcp.ts`、`server.ts` 均与派生镜像逐字节一致 |
| 知识服务 | `7840bcd-release-20261103` / `421b18fa6fb4` | 标签含 `7840bcd`，但 OCI revision 为 `a917165`。本轮以镜像 ID 和打包源码定位 |

**原基底可复现的范围。** 用当前分支的补丁函数从旧基底生成后端两份 TypeScript、网关两份 TypeScript 和 UI 入口 HTML，5/5 份产物与现有开发派生镜像相同。新旧后端的 `/app/bun.lock` SHA-256 均为 `702eecf22d18e468484f40351ad5f0a7d40fc645784ff93b882c8a63588b4bb1`，根 `package.json` 的字节也相同。

**最新后端的实测结果。** 镜像 ID 为 `sha256:a828b3723cdf0ebe067339b88bad1ef9384ad596f50f1d448b7dfa45f28ec853`。`server.ts` 通过仓库的原始哈希与锚点检查；`prompt.ts` SHA-256 为 `e46ab01515c64c97682dd4a78a79ef79c1dc0bcb156329811b6531b0433b417f`，原门禁按预期拒绝。新旧源码的七处细分变化中，四处涉及按会话注册 MCP 工具及知识库检索参数，三处涉及 CSV 附件的识别、保存和回填。四个补丁锚点均保留且各出现一次，工具注册锚点与变化后的知识库分支共用同一循环。

以该精确哈希作为单次实验输入，在内存中调用原补丁函数，确认七项上游变化保留，再用 11 月基底内的 Bun 1.3.5 转译。两份 TypeScript 均通过语法检查；补丁结果 SHA-256 分别为 `0b8c811b1872384f2ca22a576eb655fca8eb468a5d31aae307cb1b82f66704a1`（prompt）和 `6cd905fbbe5ae5986ac3fa6d409b6e5b8a09dc691cf08c0868e73d964e240d4a`（server）。该检查发生在构建前，明确了正式构建时应保留的哈希门禁。

**实际构建。** 用本地 11 月基底与仓库六份经摘要校验的构建输入创建临时上下文，仅在副本中替换确切的旧基底 ID、构建引用和 `prompt.ts` 输入哈希。现有补丁的源码校验保留。构建时两份 TypeScript 的转译和默认非 root 用户导入三个模块均通过。得到实验候选 `sha256:f7c104629c94530285aa8b283a48de0c9c79ff52aa8c2b3123df6a6c1900144c`。候选测量按开发边界要求，将该用户的独占卷挂到 `/openscience`；基底对照组挂到 `/m0`。两者的数据结构与负载相同。

**双账号联测前的镜像工作。** 将本次固定版本和构建输入纳入可维护的开发模板，并验证两会话各自的知识集绑定、工具参数与回执、CSV 附件和恢复。上游后端与网关镜像含相关 TypeScript 与依赖锁；从维护者的 Git 检出完整重建上游的来源和责任人仍待确认。本次候选基于缓存镜像构建。

## 工具链的实际覆盖

| 检查 | 本轮证据 | 到双账号联测之间的差距 |
|---|---|---|
| 生产同版网关的六工具 | 在隔离容器内向镜像自带的 MCP handler 发进程内 `tools/list`，返回 200。六项为 `predict_polymer_properties`、`polymerize_monomers`、`dft_job_submit`、`dft_job_status`、`dft_job_list`、`dft_job_result` | 打包源码仍共用一个配置账号的 `ZhijuClient`。M3 需逐项核对真实用户、任务、结果、权限和用量，不能用目录发现代替业务验收 |
| 19111 的开发适配 | 定向执行网关分派、角色和草稿等契约 9 项，以及参数板确认、会话绑定和回执恢复等 8 项，全部通过。分派使用合成业务客户端和任务 ID | PolyTAO、逆合成另需逐用户委托、独立人工确认和幂等对账；此次结果没有产生实际科研任务 |
| 知识服务 | 镜像内的 `src/service.ts` 校验单一服务凭据，创建知识库写入固定 `LOCAL_OWNER`，网页和 MCP 有独立入口 | 本轮没有经授权开放的知识集，双账号基础试点的真实知识集清单为空。后续仅开放能在网页与 MCP 同时执行范围校验的共享只读集合 |
| 模型链路 | 单独执行开发模型代理的 3 项本地回环/Unix socket 测试，全部通过。后端实测使用本地兼容流式模型，并验证了回复落盘 | 现有受限 HTTPS 隧道与 DeepSeek 直连 HTTP 出口需分别接入同一用户预算。真实供应商额度、流式用量和断流结算本轮未验证 |

这 **20 项定向离线测试**均通过，另有一次真实网关 handler 的工具目录查询。Bun 执行 [PolyTAO](../../ops/capability-gateway-dev/polytao.test.ts)、[性质筛选](../../ops/capability-gateway-dev/property-filter.test.ts)和[逆合成](../../ops/capability-gateway-dev/retrosynthesis.test.ts)三个套件的 9 项，筛选六原生工具保留、模型与 UI 目录、草稿分派等用例。Node 执行 [PolyTAO 表单](../../ops/openscience-mcp-dev/polytao-form-bridge.test.mjs)、[逆合成表单](../../ops/openscience-mcp-dev/retrosynthesis-form-bridge.test.mjs)和[补丁](../../ops/openscience-mcp-dev/patch.test.mjs)三个套件的 8 项，覆盖确认、续接和回执。Python 执行[模型代理测试](../../scripts/tests/test_openscience_model_proxy.py)全部 3 项。三组各用独立的无网测试容器。

<details>
<summary>离线契约测试筛选条件</summary>

Bun 和 Node 两组命令分别在只读挂载的网关测试目录和 OpenScience 适配器目录运行。Python 命令从仓库布局的根目录运行。

```sh
bun test --test-name-pattern 'all six pre-existing tool branches|lists all original six tools|model lists exactly original six|enabling retrosynthesis adds|shared draft route retains structured output|UI lists only UI tools' polytao.test.ts property-filter.test.ts retrosynthesis.test.ts
node --test --test-name-pattern='confirmation persists|UI actions map|resolve is read only|parameter confirmation crosses|a continuation failure preserves|continue creates a fresh|trusted receipt and continuation' polytao-form-bridge.test.mjs retrosynthesis-form-bridge.test.mjs patch.test.mjs
python -B scripts/tests/test_openscience_model_proxy.py
```

</details>

**UI 和凭据的版本边界。** 从 UI 参考镜像的 `/etc/nginx/nginx.conf` 核对了 `/openscience-api/`、`/knowledge-api/` 和兼容入口 `/api/`。打包入口 `index-production-52b20bca83f6.js` 检查 `document.referrer` 和 9000、9001 两个精确父来源。本轮读取的是镜像内文件，两个父页与新开发路由的真实浏览器换票留给 M2。

在 11 月后端的无网 Bun 进程内，给 MCP 的 `Bearer {file:…}` 配置使用合成凭据。调用镜像内的 `Instance.provide` 和 `Config.get()`，普通目录首次解析到 v1；将凭据文件原子替换为 v2 后，同一目录仍读到 v1，首次打开的另一个目录读到 v2。重启 Bun 后，原目录也读到 v2。已打开的工作区会缓存旧配置，实测仅覆盖解析层。M1 要验证实例代次切换和排空，M2/M3 要在路由与下游验证旧令牌失效和活动连接被关闭。

## 资源预算与首批范围

- M0 的临时卷使用 Docker 本地卷存储。只为两个后端配置各 2 GiB 上限，合计上限 4 GiB；此次没有创建聚合父 cgroup 或每用户磁盘硬配额。测试卷按新卷创建，结束后删除。该介质选择仅适用于合成基准。
- 当日 15:34 的主机快照为 128 个逻辑 CPU、约 251.5 GiB 总内存、185.8 GiB 可用内存。拟用于持久工作区的 `/data` 为 ext4，普通可用约 168.6 GiB，挂载选项未显示 `prjquota`。双账号联调可以先使用两个隔离开发卷；若该环境要承载真实成员或宣称 2 GB 用户硬配额，须在所选介质完成写满、清理与恢复测试。
- 两个合成用户只启用普通/项目对话、本地模型和测试夹具。首条**待验证**的科研链是生产同版 DFT 提交、状态、列表和结果，后续补齐其余原生工具。M0 未启用私人知识库、真实模型或写入科研任务的工具。
- 这些数据不能外推 20 个同时活跃用户。32 GiB 总预算最多完整承诺 16 个 2 GiB 实例，且还没扣除共享开销；后续需按真实工具、附件、SSE 与模型流负载重新测量并发档位。

## 双账号验证前的交接

| 阶段 | 需要补齐的事项 | 可以开始下一步的证据 |
|---|---|---|
| M1 镜像与工作区 | 把已构建实测的实验镜像纳入开发模板，补齐附件与工具回归；实现 `(环境, user_id)` 到稳定 UID、私有卷和进程的映射、幂等启动与资源准入 | 候选镜像可重建；A、B 用同名项目各自写入，实例回收和重启后仍找到本人的卷 |
| M1 网络与生命周期 | 建立独立开发路由到实例的网络，在该网络验证互相隔离；汇总普通目录及项目内的忙碌状态，区分被动 SSE 与活跃工作 | A/B 实例只能与授权上游通信；被动订阅可回收，活跃轮次及待确认工具不会被误停。M0 的无外网回环拓扑只验证了实验边界 |
| M2 账号与通行 | 配置独立账号库中的两个测试会员、环境总开关和按人名单，接入一次性票据、会话上下文与逐登录撤权 | A 换设备仍能恢复；A 的一台设备退出不影响另一台。禁用或撤权后 30 秒内拒绝对应旧请求并断开旧流。[当前能力策略](../../backend/app/auth/policy.py) |
| M2 真实用户界面 | 让父页完成 iframe 最终导航，处理兼容 API、草稿、旧标签与迟到的交换响应，在两个独立浏览器上下文及同一浏览器切号 | 按 [AC 矩阵](../openscience-per-user-instances-plan.md) 双向核对项目、对话、附件、侧栏和事件；现有[合成工作区测试](../../scripts/ci/verify_frontend_account_ui.mjs)不计作该项通过 |
| M3–M5 正式能力和放量 | 工具逐用户换票与计量、模型预算、配额、备份恢复和目标并发实测 | 每项开放的真实能力均能按 owner 对账。未验收项继续关闭；这些事项与 M1/M2 的基础双账号验收分别记录 |

**工期判断。** 本次测量支持按计划启动 M1 和 M2。两项原估合计 **4.5–6.5 人周**，可并行。补丁候选解决了新基底能否应用现有开发补丁的疑问；权限接口、用户卷和浏览器均还需实现，尚无依据缩减这两项工期。生产同版网关的源码维护人、模型额度和硬配额介质是后续正式放量的外部依赖。

## 重放与证据

在本地具备 Docker、Linux cgroup v2、Python 3 和相同镜像 ID 的独立开发机运行。夹具只在回环内通信，驱动不拉取镜像。原始测量保留每轮每实例的冷启动、各阶段聚合采样、合成回复读回数、占盘和资源清理结果。

[逐轮 JSON 证据](openscience-m0-20261119.json)包括镜像 ID、时间、每档三对实例的观测值、对话读回和清理结果。[构建脚本](openscience-m0-fixture/build-candidate.py)固定上游镜像及六份仓库构建输入的摘要；[测量驱动](openscience-m0-fixture/run.py)使用[探针和模型夹具](openscience-m0-fixture/fixture.py)、[普通配置](openscience-m0-fixture/config.json)和[含两个 MCP 的配置](openscience-m0-fixture/config-mcp.json)。需要已缓存的后端和 Python 夹具镜像、可访问其 cgroup 的本地 Linux Docker Engine，以及构建候选所用的 Docker Buildx。

在仓库根目录依次执行，以免两个测量任务互相干扰。构建输出的最后一行是 JSON，`image_id` 和 `tag` 都是本次本机构建专用值。

```bash
m0_fixture=docs/verification/openscience-m0-fixture
m0_results=$(mktemp -d)
python3 "$m0_fixture/build-candidate.py" > "$m0_results/build.log"
m0_id=$(tail -n 1 "$m0_results/build.log" | python3 -c 'import json,sys; print(json.load(sys.stdin)["image_id"])')
python3 "$m0_fixture/run.py" --output "$m0_results/base"
python3 "$m0_fixture/run.py" --output "$m0_results/mcp" --mcp
python3 "$m0_fixture/run.py" --output "$m0_results/candidate" --mcp --candidate-image "$m0_id"
```

驱动按本次唯一标签回收临时容器和数据卷。输出 JSON 留在所选目录。本轮已移除临时镜像标签，复测可按以上步骤生成新的本地候选。若不再需要重放生成的镜像，可按本次构建返回的唯一 `tag` 在本地删除。
