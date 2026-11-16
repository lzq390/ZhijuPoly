# OpenScience 数据库筛选 MCP：独立开发接入

开发入口为 `http://127.0.0.1:19111/`，使用独立 OpenScience 后端、会话和文件目录，已配置数据库筛选 MCP。原有 `9011` 同时供 NexPoly 生产 `9000` 与开发 `9001` 使用，因此本次不修改或重启该共享实例，也不切换 `9001` 的 Agent 入口。

PolyTAO 参数板和聚合物生成现已接入同一独立网关，默认 Research 自然语言流程及 3/3 候选真实验收通过；结构、14/4 工具范围、部署命令与验收证据见 [PolyTAO 能力网关开发接入](polytao-capability-gateway-dev.md)。本页的 9 工具验收记录对应数据库筛选网关接入阶段；本次扩展后 Gateway 模型端为 14 工具，原三个筛选工具已重新通过自然语言实测。

2026-11-16 的版本同步以 9011 当前运行的前后端镜像为基底，保留 19111 的独立目录、数据库筛选权限和结果展示。默认模型与辅助模型从当前共享配置提取，分别为 `gpt-6.1-sol`、`gpt-6.1-terra`；同时保留已审核的模型列表（包括 DeepSeek）、Agent 配置与权限。默认使用 `research`，并增加专门的 `property-filter` Agent。新的网关接入配置仅保留两个 MCP 连接：复用共享 `knowledge_service`，并让 `capability_gateway` 指向独立开发网关，由开发网关统一提供原有六项能力和新增的三个数据库筛选工具。共享实例以后再次更新时，需要显式同步开发配置，不会自动改变运行中的 19111。

**网关接入验收（2026-11-16）：已部署，协议、自然语言和页面验收均通过。** 19111 现在仅连接知识库与独立开发网关；开发网关提供 9 个工具，原有 6 个完整工具定义与 9011 网关一致，新增 3 个筛选工具的完整 Schema、只读标注及结构化结果与上游筛选 MCP 一致。真实目录、Tg 100–200°C 查询的两页结果和全库分布均与直连结果逐项一致，错误结构、鉴权和未知工具拒绝也通过验证。

从 19111 默认 `research` 页面仅提交一次不含接口名称的自然语言需求，实际模型及保存的全部助手消息均为 `gpt-6.1-sol`，本轮三个筛选调用均使用 `capability_gateway_` 前缀。命中 5,026 个结构组、返回 2 组，全库直方图包含 45,160 条测量；最终 JSON、完整数据版本、材料卡、18 个直方图分箱及刷新保存均通过核对。新会话为 `http://127.0.0.1:19111/chat/ses_e1d95e6f4ffeePAYCzxjZ4O7Jg`，浏览器报告在 `.runtime/openscience-mcp-dev/browser-evidence/2026-11-16T03-32-45-029Z/`。

旧直连会话仍能显示和刷新；9011 的原网关仍只有原来的 6 个工具，生产配置、部署文件及相关容器指纹均未改变。51 项启动器、3 项代理、12 项网关及 20 项展示测试通过，另有 15 项真实协议检查。此次只执行筛选查询，没有提交预测、聚合或量化任务。部署备份、协议对照、凭据边界及界面证据保存在本次准备工作树的 `.runtime/openscience-gateway-20261116/`。

以下 6.1 历史验收发生在网关切换前，使用三个 MCP 连接及直接筛选前缀，不能替代以上新网关验收。

**切换前历史验收（2026-11-16 10:40–10:41，Asia/Shanghai）：6.1 升级部署与真实自然语言对话通过。** 从默认 `research` 的页面输入框只提交一次不含工具名或内部参数的业务提示，实际请求及本轮全部助手消息均为 `gpt-6.1-sol`。模型自主完成三个筛选操作，Tg 100–200°C（含边界）命中 5,026 个结构组、返回 2 组，全库分布包含 45,160 条测量；参数、三结果版本、最终 JSON、页面渲染及刷新保存均通过核对。会话为 `http://127.0.0.1:19111/chat/ses_e1dc5b769fferMAEjar4wL42fl`，私有报告与截图位于本次准备工作树的 `.runtime/openscience-mcp-dev/browser-evidence/2026-11-16T02-40-30-960Z/`。

该次切换前部署确认三个 MCP 均已连接，知识库列表与 9011 一致；五个配置/provider 接口不暴露实际模型或服务凭据，私有配置文件读取返回 403。旧会话消息完整保留，9011 配置与前后端容器指纹未变。41 项启动器测试、3 项代理测试、13 项补丁/HTTP 边界测试及 18 项展示组件测试通过。当时未提交预测或计算作业；复用服务的连接与原有审批配置已核对，不能将该次筛选验收当作计算流程验收。升级前的开发镜像、配置和完整运行目录备份保留在本次准备工作树的 `.runtime/openscience-sync-20261116/`。

此前验收状态（2026-11-03）：真实对话验收通过。更新开发模型凭据后，从独立页面提交了一次提示，三个工具均真实完成、结果版本一致，最终回答已渲染且刷新后内容保持一致。Tg 全范围搜索命中 18,614 个结构组、返回 2 组，直方图包含 45,160 条测量；MCP 审计中的三个调用均为 `ok`。此历史记录使用 5.6，不能作为 6.1 的验收记录。

开发页面已增加直接读取原始工具结果的图形展示：查询概览、材料与测量证据卡片、可悬停和键盘聚焦的直方图。无需要求模型输出 JSON；已有对话刷新后也会自动展示。模型原始回答若为完整 JSON，默认收起，可通过“查看原始回答”展开。

浏览器不在开发机上时，先建立 SSH 转发，再访问本机的 `http://127.0.0.1:19111/`：

```bash
ssh -N -L 19111:127.0.0.1:19111 <开发机SSH地址>
```

## 开发服务与隔离边界

| 资源 | 配置 |
|---|---|
| OpenScience Compose 项目 | `nexpoly_openscience_dev` |
| UI | 服务 `openscience-dev-ui`，`127.0.0.1:19111`，独立派生镜像 `nexpoly-openscience-mcp-ui:dev`，基底 image ID 固定 |
| 后端 API | 服务 `openscience-dev-backend`，`127.0.0.1:14096`，独立派生镜像 `nexpoly-openscience-mcp:dev` |
| 默认开发 Agent | `research`，默认模型继承已配置的 `lab-openai-compatible/gpt-6.1-sol`，辅助模型为 `gpt-6.1-terra` |
| 专用筛选 Agent | `property-filter`，仅允许下列三个只读数据库筛选工具 |
| MCP 配置名 | `knowledge_service`、`capability_gateway`；共 10 个工具（知识库 1 个、开发网关 9 个） |
| 开发能力网关 | 服务 `capability-gateway-dev`，独立派生镜像 `nexpoly-capability-gateway:dev`，无宿主端口 |
| 筛选 MCP 客户端 | 由开发网关使用 `openscience-dev` 令牌，沿用限流和只读数据库角色 |
| 私有运行目录 | `.runtime/openscience-mcp-dev/`，目录 `0700`、配置/凭据 `0600`，Git 忽略 |

数据库筛选调用链为 UI → 独立 OpenScience 后端 → 开发能力网关 → 只读筛选 MCP → `nexpoly_dev`。UI 与后端使用自己的网络 `nexpoly_openscience_dev_default`；后端另加入 `ks-agent` 访问 `knowledge-service:8082/mcp`，不再加入筛选客户端网络，也不直接连接筛选 MCP。`capability_gateway` 配置名在 19111 中指向 `http://capability-gateway-dev:3100/mcp`，后端经私有默认网络连接该服务。

开发网关加入默认网络、内部筛选客户端网络 `nexpoly_mcp_clients_dev` 和既有 NexPoly 开发网络 `nexpoly_dev_default`，分别连接 OpenScience、只读筛选 MCP 及原有预测/计算业务接口；它不加入 `ks-agent`，不发布宿主端口，也不注册共享网关的 DNS 别名。UI 继续加入 `ks-page`，通过同源 `/knowledge-api/` 访问共享知识库页面接口。后端自身不加入数据库所在的 `nexpoly_dev_default`；筛选 MCP 保留原有开发数据库网络。

独立实例不挂载共享业务文件、会话、auth、config/state/cache 卷，不携带数据库凭据。开发会话、文件与运行状态保存在自己的私有目录；知识库复用共享服务，开发能力网关沿用已审核的 NexPoly 业务连接。开发服务使用当前用户 UID/GID，去掉容器 capabilities；UI 和开发网关使用只读根文件系统。UI 通过同源 `/openscience-api/` 连接独立后端，通过 `/knowledge-api/` 连接共享知识库页面接口。此接入不修改或重启 9011、共享知识库或原有能力网关容器。

`lab-openai-compatible` 模型出口通过独立网络内的 `model-proxy:17892` → 私有 Unix socket → `model-egress` → 开发机已有的 `127.0.0.1:17891` HTTP 代理。两端只接受 `CONNECT api.relayrouter.ai:443`，不透传代理认证头，不记录隧道数据。`model-egress` 使用 host network，但只监听 `.runtime/openscience-mcp-dev/proxy-socket/model.sock`（目录 `0700`、socket `0600`），没有宿主 TCP 监听端口。此路径绕开新 Docker bridge 到宿主代理的访问限制，无需调整共享代理、共享容器网络或防火墙。DeepSeek 使用从 9011 核验并复制的既有 HTTP 模型端点直连，不经过这个只接受 HTTPS CONNECT 的代理；知识库和能力网关也使用各自的直接连接。启动依赖这台开发机已有的代理；更换模型服务商时需明确更新并验收出口名单。

## 配置与凭据

启动器只读核验已知共享实例的镜像、标签、挂载来源和本地基底镜像 ID，使用支持注释和尾逗号的 JSONC 解析器，复制已审核的模型 provider、默认/辅助模型、模型列表、Agent、全局权限及计费配置。MCP 连接保留已核验的 `knowledge_service`，将 `capability_gateway` 改为独立开发网关的地址和凭据，不再生成 `nexpoly_property_filter` 直接连接；不复制共享项目或用户会话。模型凭据和知识库认证头保留在独立 `0600` 配置中，未加入代码或镜像；不代表模型或计算调用免费。

可选 `.runtime/openscience-mcp-dev/model-api-key` 仅覆盖独立实例的 `lab-openai-compatible` 模型 API key，仍使用已固定的 `api.relayrouter.ai` 和模型配置，不覆盖 DeepSeek 凭据。该文件必须由当前用户拥有、为普通文件且权限 `0600`；允许文件末尾换行，拒绝空值、内部空白和配置展开表达式。启动器只更新独立配置副本，不写共享配置。请通过本机私有文件提供凭据，不在聊天、命令行参数或 Git 中填写密钥。更换模型服务商或模型需要另行调整开发配置及出口名单。

此次完整配置复刻同时将当前共享模型凭据同步到独立 `model-api-key`，旧开发凭据已随私有备份保留，重建后新配置继续有效。后续轮换请更新该覆盖文件，再执行 `down → setup → up`；直接修改生成的 JSON 会被下一次 `setup` 重建。

开发网关使用远程 Streamable HTTP，OpenScience 的容器内连接地址为：

```text
http://capability-gateway-dev:3100/mcp
```

OpenScience 使用 `Bearer {file:/run/openscience/gateway-token}` 认证开发网关。`setup` 只在开发项目停止且该文件不存在时生成独立随机 `gateway-token`，之后重用既有值。私有 `gateway.env` 为开发网关提供相同的入口认证令牌，以及从已核验共享网关读取的 NexPoly 业务地址和服务凭据；文件权限均为 `0600`，不手工在命令行或聊天中填写这些值。

筛选 MCP 的上游地址仍为 `http://nexpoly_mcp_dev-property-filter-mcp-1:18110/mcp`。启动器从其私有 `client.env` 读取只读客户端令牌，写入私有 `mcp-token`，仅挂载到开发网关的 `/run/capability-gateway/property-filter-token`；`gateway.env` 只引用此文件路径。OpenScience 后端不再挂载上游筛选令牌，仅挂载自己的 `gateway-token`。两种令牌用途独立，不进入模型提示、工具参数、URL 或命令行。

以下三个数据库筛选工具只读访问开发数据库。默认 `research` Agent 在继承 9011 权限的基础上允许它们；专用 `property-filter` Agent 则只允许这三个完整工具名，并为 MCP 调用的 `mcp` 权限检查设置相同的精确名单：

- `capability_gateway_list_filter_properties`
- `capability_gateway_search_polymers`
- `capability_gateway_get_property_histogram`

`property-filter` 的其他工具默认拒绝。`research` 及其他普通 Agent 沿用 9011 的已配置权限，能够使用获准的知识库和能力网关工具；计算作业提交等需要审批的操作保留 9011 的 `ask` 规则，在审批后执行。三个筛选工具的只读属性不表示整个开发实例只允许只读操作，也不表示所有工具输入输出都经过工作区沙箱。`OPENSCIENCE_DISABLE_PROJECT_CONFIG=1` 禁止工作区配置覆盖本次开发配置。没有接入 NexPoly 用户登录、会员或用户用量；开发配置保留共享源中已审核的计费设置。

`protected-baseline.json` 仅存共享实例配置文件和容器身份的 SHA-256，用于操作前后检查。容器挂载按目标路径排序后计算指纹，避免 Docker 返回顺序造成误报。启动器拒绝其他工作树的同名项目、非预期挂载、被更换的基底镜像及源配置变化。

开发网关从已核验的 `openscience-capability-gateway:91f5d81-release-20261111` 派生，基底 image ID 为 `sha256:fb7fdd70e46d7544d0816f042bd0a4324cd7a6c1e86a4a4606031f5108199df0`。它保留原有六个工具，只新增三个筛选代理，完整转发上游输入/输出 Schema、`structuredContent`、文本内容及错误结果，不重新拼装筛选业务数据。9011 仍连接原来的六工具网关；其代码、镜像、容器和配置保持不变。

## 为什么需要开发镜像补丁

当前 OpenScience 的 MCP 适配器只把 `content` 送入模型，忽略 `structuredContent`。数据库筛选 MCP 的属性 key、数据版本和证据都在结构化结果中；只注册连接会导致模型无法继续使用真实版本查询。

`ops/openscience-mcp-dev/patch.mjs` 仅在新镜像构建时，把结构化结果序列化为一条附加文本，供模型使用并保存在工具调用记录中；原有文本、媒体和工具 Schema 保留。补丁严格校验原文件 SHA-256 和唯一替换位置，未知源码、重复补丁均拒绝。构建时还执行 Bun TypeScript 语法检查。

当前 OpenScience 的配置读取接口会返回 provider 凭据和展开后的 MCP 请求头，因此开发镜像还在 API 路由前加入 `dev-boundary.mjs`：对配置响应做脱敏，禁止通过 HTTP 修改配置或 MCP 连接、打开终端和执行会话 shell/command，限制文件 API 及本地附件在开发工作区内，禁止提示或会话通过请求参数扩大工具权限。内部模型和 MCP 配置保留真实凭据；普通 Agent 的实际工具权限仍由同步后的 9011 配置决定。此补丁只约束这些 HTTP 入口，不为模型工具或共享服务提供完整 I/O 沙箱，也不替代生产认证；UI/API 仅绑定 loopback。

共享后端和 UI 镜像未修改。当前后端基底为 9011 的 `openscience-backend-poc:release-20261029`，image ID 为 `sha256:7e4c055b4641bfcc0d329cbb2b61ef473b89e8435b3505dabc7b2895d11f0435`；保留该版本的知识库会话绑定与执行前绑定检查。UI 基底为 `openscience-ui-poc:9e342b5-production-dual`，image ID 为 `sha256:fea8a3c294bb324a98041f8f8354448e6ca5c6502dedc565c4b884994c567bb0`，包含问题/审批展示、输入草稿、知识库选择及文档分页、导航和历史静态资源缓存修复。升级后必须重新评估补丁和固定哈希，不能跳过校验。

用户可以用自然语言提出属性范围、结果数量和分布需求，由模型依据工具描述及 Agent 指引选择调用。验收提示中写出工具名用于逐项验证接口，正常查询不要求用户掌握工具名或内部参数。

结构化结果有两层：筛选 MCP 本身返回经过数据模型校验的 `structuredContent` JSON 对象及 `outputSchema`，包含材料、测量证据、单位、计数和版本；开发网关完整转发该结果，OpenScience 适配层再把该对象序列化后保存在工具输出文本中，供模型读取。聊天最终回复是模型生成的文本，它与 MCP 原始结果的契约不同。开发 UI 的卡片和直方图直接读取对应工具原始结果，不把模型最终 JSON 作为展示数据源。其他 MCP 客户端可直接消费结构化结果，并按自身能力展示。

`UI.Dockerfile` 从已核验的 UI 基底派生镜像，仅在已知 SHA-256 的 `index.html` 前置新增模块与样式；原应用 bundle、Nginx 配置和共享部署不改。新增静态资产位于共同内容哈希目录，浏览器产物使用 `.js` 以匹配现有 Nginx MIME。启动器同时核验基底、构建阶段镜像和派生 UI 的标签、原始层及运行用户。

展示模块只观察同源 API 的会话目录与原始消息，补充请求也仅为读取已有消息；不会发送提示或重新调用 MCP。它仅识别新 `capability_gateway_` 与历史 `nexpoly_property_filter_` 前缀下三个精确的筛选工具名，保留旧会话展示，不把网关预测、DFT 或相似名称的输出当成筛选结果。缓存按工作区目录和会话区分，结果绑定用户消息及数据版本，旧请求回包不能覆盖新快照。搜索零命中正常显示；目录/版本或数据校验失败会提示重新查询。材料保留完整 SMILES、原始值、标准值、来源与证据截断信息；直方图明确标注全库该属性的测量口径及范围外计数。极小/极大数值采用科学计数，避免显示为零。

## 启动、停止和令牌轮换

在本工作树执行：

```bash
# 筛选 MCP 必须已完成其 setup；up 会创建客户端内部网络。
python3 scripts/property_filter_mcp_dev.py up

# 只读同步已审核配置，并生成独立目录、gateway.env 及开发网关凭据。
# 开发项目须先停止；既有 gateway-token 会重用，不在每次 setup 时轮换。
python3 scripts/openscience_mcp_dev.py setup
python3 scripts/openscience_mcp_dev.py up
python3 scripts/openscience_mcp_dev.py smoke
python3 scripts/openscience_mcp_dev.py verify-protected
```

当共享实例已经升级，先核对正在运行的镜像、源码和补丁，并更新启动器与 Dockerfile 中的固定镜像及源码校验；保留旧开发镜像、配置和会话备份。仅停止独立开发项目后，使用 `python3 scripts/openscience_mcp_dev.py setup --refresh-baseline` 显式采纳新基线，再执行 `up` 和 `smoke`。该选项仅支持 `setup`，运行中的项目不能刷新基线，旧基线会以私有文件保留。普通 `setup` 继续拒绝意外的共享实例漂移。

实际开发 Compose 所属 checkout 是 `/data/lzq/gith/nexpoly-dev`。从其他工作树准备更新时，先核对其部署标签及源码差异，再将本次受审阅的部署文件同步到该 checkout；不能绕过工作树与挂载归属检查接管同名项目。

`smoke` 验证独立 UI、API、反向代理，以及知识库和独立开发能力网关两个 MCP 连接；筛选能力通过开发网关提供，不再作为第三个 OpenScience MCP 连接。它不代替真实模型对话或计算作业审批验收。构建/启动日志在私有运行目录的 `docker-up.log` 中，失败会报告该路径而不打印配置或凭据。

网关代理上游查询的部署超时为 15 秒，低于 OpenScience 对网关请求的 30 秒超时；筛选服务不可达时，注册接口保留原有六工具，避免新增上游故障阻断原有工具发现。上游地址和令牌挂载路径固定，禁止跟随重定向，限制响应体为 8 MiB；正常业务结果继续遵守原筛选 MCP 的 256 KiB 上限。

提供有效开发模型凭据后，按以下步骤重新加载（`/path/to/private-model-key` 是已有的私有密钥文件）：

```bash
python3 scripts/openscience_mcp_dev.py down
install -m 600 /path/to/private-model-key .runtime/openscience-mcp-dev/model-api-key
python3 scripts/openscience_mcp_dev.py setup
python3 scripts/openscience_mcp_dev.py up
python3 scripts/openscience_mcp_dev.py smoke
```

停止独立 OpenScience 并保留其会话和文件：

```bash
python3 scripts/openscience_mcp_dev.py down
```

如还要停止筛选 MCP，先停止 OpenScience，再执行 `property_filter_mcp_dev.py down`，以释放共享的开发客户端网络。两套命令都不控制现有 NexPoly 前后端或共享 OpenScience。

上游筛选令牌撤销仍使用筛选 MCP 的 `revoke`，开发网关下一次上游请求即拒绝。轮换后需更新独立项目的私有文件并重建开发网关容器；运行中的单文件挂载不会自动切换。OpenScience 的独立 `gateway-token` 保持不变：

```bash
python3 scripts/openscience_mcp_dev.py down
python3 scripts/property_filter_mcp_dev.py rotate-token
python3 scripts/openscience_mcp_dev.py setup
python3 scripts/openscience_mcp_dev.py up
python3 scripts/openscience_mcp_dev.py smoke
```

## 复现测试与真实对话

```bash
python3 -m unittest discover -s scripts/tests -p test_openscience_mcp_dev.py
python3 -m unittest discover -s scripts/tests -p test_openscience_model_proxy.py
node --test ops/openscience-mcp-dev/patch.test.mjs ops/openscience-mcp-dev/dev-boundary.test.mjs
```

浏览器探针需要 Node 20+、Playwright 和 Chromium；优先使用仓库现有 `ops/openscience-ui-overlay` 锁定的 Playwright 包，也可用 `--playwright-package` 指定同版本安装。当前开发机已安装 Node 22 与该浏览器：

```bash
/home/devuser/.nvm/versions/node/v22.23.2/bin/node scripts/verify_openscience_mcp_browser.mjs --agent research --expected-model gpt-6.1-sol --tool-prefix capability_gateway_
```

自然语言模式不在用户提示中出现工具名或内部参数，验证模型自主查询 Tg 100–200°C 的材料并输出业务 JSON：

```bash
/home/devuser/.nvm/versions/node/v22.23.2/bin/node scripts/verify_openscience_mcp_browser.mjs --natural-language --agent research --expected-model gpt-6.1-sol --tool-prefix capability_gateway_
```

6.1 验收使用默认 `research` Agent，并通过 `--expected-model gpt-6.1-sol` 同时校验 UI 实际提交的模型、本轮保存的助手消息和刷新读回的模型。`--tool-prefix capability_gateway_` 也是默认值，严格要求本轮及刷新保存的三个筛选工具均来自开发网关；历史直连验收可显式选择 `--tool-prefix nexpoly_property_filter_`，不会与新网关验收混算。可另用 `--agent property-filter` 验证专用筛选 Agent；它不能替代默认 Agent 的验收。该选项仅控制验收端的来源核验，自然语言模式的用户提示仍不含接口名或内部参数。探针会在截图前按需展开默认折叠的 JSON 回答，避免把不可见节点的截图超时误判成筛选失败。

该模式检查实际提交文本没有内部接口名、搜索边界与单位准确、三工具版本一致；并解析最终 JSON，核对业务字段、命中数、数据版本和本轮标记。报告明确将该 JSON 标为 `model_generated_text`，以区别 MCP 原始结构化结果。编辑器可能合并换行，因此业务提示按忽略空白后的文本比较，仍校验实际发送内容。

探针只允许 `127.0.0.1:19111` / `localhost:19111`，新建浏览器上下文并从真实输入框发送提示，不通过 API 代发、不替换模型或工具输出。API 只读核对该 UI 创建的会话。三个工具必须真实完成、结构化结果 `ok: true` 且版本一致，最终回答必须在页面显示，刷新后仍存在。报告和截图保存到 `.runtime/openscience-mcp-dev/browser-evidence/`，不提交完整对话结果到仓库。

探针以本轮唯一标记定位用户消息，只统计对应 `parentID` 的 assistant/tool parts；精确限定三个完整工具名，避免把历史对话或其他 MCP 的同名工具当成本轮成功。它还核验属性来自本轮目录、搜索与直方图使用同一属性及版本、范围和分页符合提示要求。

以下为 2026-11-03 的历史验收记录，使用当时的 5.6 模型、专用筛选 Agent 与配置，不能作为本次 6.1 默认 `research`、共享服务接入或新权限的验收结果。

当时实现阶段已通过 32 项启动器测试、3 项双端代理测试、12 项开发补丁/边界测试。真实 HTTP 检查确认五个配置/provider 接口不返回实际模型密钥或 MCP 令牌，私有 `/run` 文件读取返回 `403`。更新凭据后再次检查五个配置接口，新模型密钥和 MCP 令牌均未出现在响应中；开发服务健康、MCP 已连接，共享配置与容器身份校验未变化。

2026-11-03 09:28–09:29（Asia/Shanghai）的真实对话验收仅提交一次提示，三个工具各调用一次：

| 工具 | 实际结果 | OpenScience 调用耗时 | MCP 审计耗时 | 保存的工具输出文本大小 |
|---|---|---:|---:|---:|
| `list_filter_properties` | Tg 目录选项 1 项，`std:tg:C` | 79 ms | 64.46 ms | 664 B |
| `search_polymers` | 命中 18,614 个结构组，返回 2 组 | 291 ms | 275.52 ms | 3,327 B |
| `get_property_histogram` | 全库 45,160 条测量 | 177 ms | 169.04 ms | 542 B |

搜索下界直接使用目录返回的 `-273.04999999999995 C`，第 1 页、每页 2 组，每条件每结构最多 1 条证据；搜索和直方图均提交本轮目录的完整版本，三个结果共用：

```text
1c8bf0f452a6383023989e0715c63123cf699bd4d11a748ea34dc32f0799800a
```

最终中文回答展示了来源、证据截断及跨测量 AND 语义；页面刷新后回答哈希一致。会话为 `http://127.0.0.1:19111/chat/ses_e60fa0a7bffeWAkIwUeY6JjKyR`。私有证据位于 `.runtime/openscience-mcp-dev/browser-evidence/2026-11-03T01-28-48-388Z/`，含 `report.json`、`conversation.png`、`response.png`；合并审计与健康检查的验收记录为 `.runtime/openscience-mcp-dev/real-dialog-acceptance.json`。此前模型 `401` 的失败记录仍保留在原私有证据目录中。

同日的自然语言实测会话为 `http://127.0.0.1:19111/chat/ses_e60eb9693ffei3Y8AvJIX0I20T`，用户提示只提出玻璃化转变温度 100–200°C、两个结构、每结构一条测量证据及来源、全库分布和 JSON 输出等业务要求。模型自主完成目录解析、搜索和直方图查询：命中 5,026 个结构组，返回 2 组，全库直方图仍为 45,160 条测量。最终 JSON 可解析，命中数与数据版本均与工具原始结果一致。

自然语言证据目录为 `.runtime/openscience-mcp-dev/browser-evidence/2026-11-03T01-44-36-155Z/`。原探针曾因编辑器合并换行而触发文本比较断言，失败的 `report.json` 保留；修正比较方式后，只读打开同一个已提交会话完成核对，未发送第二条提示。`readback-report.json` 记录工具与最终 JSON 核对、页面渲染及刷新一致性均通过。`mcp-results.json` 保存三工具原始结构化字段，`answer.json` 保存模型生成的业务 JSON；两者均为私有 `0600` 文件。界面证据为 `natural-language-conversation.png`。

当时图形界面相关检查包括 36 项启动器测试和 18 项前端解析、目录绑定、构建及浏览器组件测试。复现命令：

```bash
python3 -m unittest scripts.tests.test_openscience_mcp_dev
/home/devuser/.nvm/versions/node/v22.23.2/bin/node --test ops/openscience-mcp-dev/ui-patch.test.mjs ops/openscience-mcp-dev/results-controller.test.mjs ops/openscience-mcp-dev/results-view.test.mjs
/home/devuser/.nvm/versions/node/v22.23.2/bin/node scripts/verify_openscience_results_ui.mjs
```

当时的真实页面只读验收使用已有会话，依次检查桌面、刷新、切换历史会话、返回原会话和 390px 手机宽度，五阶段均通过；没有发送提示、调用工具或写入数据。卡片展示完整结构、原始/标准值、来源和截断说明；18 根 SVG 柱的数值及高度比例与真实直方图一致，悬停和键盘聚焦均显示真实区间计数。原模型 JSON 默认收起且可切换查看，普通文本回答保留。当时 Tg 100–200°C 会话显示 5,026 个结构组、2 个材料卡片，全库分布为 45,160 条测量；切换到全范围会话显示 18,614 组，返回后恢复原查询。

该次历史私有验收目录为 `.runtime/openscience-mcp-dev/results-ui-evidence/2026-11-03T02-06-20-141Z/`，含 `report.json`、清晰图表 `histogram.png`、材料卡片 `material-card.png` 和各阶段页面截图。用户可在开发页面直接提问“找 Tg 在 100 到 200 摄氏度之间的聚合物，展示两个材料及测量来源，并画出 Tg 的全库分布”，无需指定工具名或 JSON。

另以隔离的本地 mock 验证六个消息回包边界：跨工作区的旧响应、同工作区的旧空快照均不能覆盖当前结果，最新空快照正常移除面板并恢复原回答。10 个浏览器请求均为本地拦截的 GET，不修改服务数据；此证据与真实界面验收分开，记录在 `.runtime/openscience-mcp-dev/friendly-results-evidence/2026-11-03T02-08-05-680Z/local-fixture-report.json`。

工具的单位、AND、版本、证据计数和错误码合同见 [数据库筛选 MCP 文档](property-filter-mcp.md)。独立开发实例的筛选验收不代表数据库筛选 MCP 已接入 9011 共享入口或生产入口。
