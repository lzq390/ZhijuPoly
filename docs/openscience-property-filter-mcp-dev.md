# OpenScience 数据库筛选 MCP：独立开发接入

开发入口为 `http://127.0.0.1:19111/`，使用独立 OpenScience 后端、会话和文件目录，已配置数据库筛选 MCP。原有 `9011` 同时供 NexPoly 生产 `9000` 与开发 `9001` 使用，因此本次不修改或重启该共享实例，也不切换 `9001` 的 Agent 入口。

**当前验收状态（2026-11-03）：真实对话验收通过。** 更新开发模型凭据后，从独立页面提交了一次提示，三个工具均真实完成、结果版本一致，最终回答已渲染且刷新后内容保持一致。Tg 搜索命中 18,614 个结构组、返回 2 组，直方图包含 45,160 条测量；MCP 审计中的三个调用均为 `ok`。生产、共享 OpenScience 与原有 `9001` 入口保持不变。

浏览器不在开发机上时，先建立 SSH 转发，再访问本机的 `http://127.0.0.1:19111/`：

```bash
ssh -N -L 19111:127.0.0.1:19111 <开发机SSH地址>
```

## 开发服务与隔离边界

| 资源 | 配置 |
|---|---|
| OpenScience Compose 项目 | `nexpoly_openscience_dev` |
| UI | `127.0.0.1:19111`，独立使用现有 UI 镜像的固定 image ID |
| 后端 API | `127.0.0.1:14096`，独立派生镜像 `nexpoly-openscience-mcp:dev` |
| 开发 Agent | `property-filter`，默认模型继承已配置的 `lab-openai-compatible/gpt-5.6-sol` |
| MCP 配置名 | `nexpoly_property_filter` |
| MCP 客户端 | `openscience-dev`，沿用开发令牌、限流和只读数据库角色 |
| 私有运行目录 | `.runtime/openscience-mcp-dev/`，目录 `0700`、配置/凭据 `0600`，Git 忽略 |

调用链为 UI → 独立 OpenScience 后端 → 筛选 MCP → `nexpoly_dev`。OpenScience 的 UI 与后端使用自己的网络 `nexpoly_openscience_dev_default`；后端与 MCP 另用内部网络 `nexpoly_mcp_clients_dev`。OpenScience 不加入数据库所在的 `nexpoly_dev_default`，也不加入共享实例的 `openscience-poc`。MCP 保留开发数据库网络，并明确允许开发客户端使用其容器 DNS 的 Host。

独立实例不挂载共享业务文件、会话、auth、config/state/cache 卷，不携带数据库凭据。它使用当前用户 UID/GID，去掉容器 capabilities。UI 只读根文件系统，只通过同源 `/openscience-api/` 连接独立后端。

模型出口通过独立网络内的 `model-proxy:17892` → 私有 Unix socket → `model-egress` → 开发机已有的 `127.0.0.1:17891` HTTP 代理。两端只接受 `CONNECT api.relayrouter.ai:443`，不透传代理认证头，不记录隧道数据。`model-egress` 使用 host network，但只监听 `.runtime/openscience-mcp-dev/proxy-socket/model.sock`（目录 `0700`、socket `0600`），没有宿主 TCP 监听端口。此路径绕开新 Docker bridge 到宿主代理的访问限制，无需调整共享代理、共享容器网络或防火墙。启动依赖这台开发机已有的代理；更换模型服务商时需明确更新并验收出口名单。

## 配置与凭据

启动器只读核验已知共享实例的镜像、标签、挂载来源和本地基底镜像 ID，使用支持注释和尾逗号的 JSONC 解析器，只提取模型 provider、默认模型和辅助模型。不复制已有插件、MCP、全局权限、项目或用户会话。模型凭据保留在独立 `0600` 配置中，未加入代码或镜像；不代表模型调用免费。

可选 `.runtime/openscience-mcp-dev/model-api-key` 覆盖独立实例的模型 API key，仍使用已固定的 `api.relayrouter.ai` 和模型配置。该文件必须由当前用户拥有、为普通文件且权限 `0600`；允许文件末尾换行，拒绝空值、内部空白和配置展开表达式。启动器只更新独立配置副本，不写共享配置。请通过本机私有文件提供凭据，不在聊天、命令行参数或 Git 中填写密钥。更换模型服务商或模型需要另行调整开发配置及出口名单。

本次用户在开发 `openscience.json` 中更新的密钥已同步到 `model-api-key`，重建后继续有效。后续轮换请更新该覆盖文件，再执行 `down → setup → up`；直接修改生成的 JSON 会被下一次 `setup` 重建。

开发 MCP 使用远程 Streamable HTTP，容器内地址为：

```text
http://nexpoly_mcp_dev-property-filter-mcp-1:18110/mcp
```

Authorization 配置为 `Bearer {file:/run/openscience/mcp-token}`。启动器从数据库筛选 MCP 的私有 `client.env` 读取令牌，写入单独的 `mcp-token` 文件；令牌不进入模型提示、工具参数、URL 或命令行。OpenScience 从服务器端文件读取令牌。

顶层权限和 `property-filter` Agent 权限都只允许以下完整工具名，同时为 MCP 调用的 `mcp` 权限检查设置相同的精确名单：

- `nexpoly_property_filter_list_filter_properties`
- `nexpoly_property_filter_search_polymers`
- `nexpoly_property_filter_get_property_histogram`

其他工具默认拒绝。`OPENSCIENCE_DISABLE_PROJECT_CONFIG=1` 禁止工作区配置覆盖本次开发配置。没有接入 NexPoly 用户登录、会员或用户用量；开发实例使用独立 BYOK 配置。

`protected-baseline.json` 仅存共享实例配置文件和容器身份的 SHA-256，用于操作前后检查。容器挂载按目标路径排序后计算指纹，避免 Docker 返回顺序造成误报。启动器拒绝其他工作树的同名项目、非预期挂载、被更换的基底镜像及源配置变化。

## 为什么需要开发镜像补丁

当前 OpenScience 的 MCP 适配器只把 `content` 送入模型，忽略 `structuredContent`。数据库筛选 MCP 的属性 key、数据版本和证据都在结构化结果中；只注册连接会导致模型无法继续使用真实版本查询。

`ops/openscience-mcp-dev/patch.mjs` 仅在新镜像构建时，把结构化结果序列化为一条附加文本，供模型使用并保存在工具调用记录中；原有文本、媒体和工具 Schema 保留。补丁严格校验原文件 SHA-256 和唯一替换位置，未知源码、重复补丁均拒绝。构建时还执行 Bun TypeScript 语法检查。

当前 OpenScience 的配置读取接口会返回 provider 凭据和展开后的 MCP 请求头，因此开发镜像还在 API 路由前加入 `dev-boundary.mjs`：对配置响应做脱敏，禁止配置修改、终端和会话 shell/command，限制文件 API 及本地附件在开发工作区内，禁止提示或会话扩大工具权限。内部模型和 MCP 配置保留真实凭据。这是固定三工具开发客户端的边界，不是生产认证方案；UI/API 仅绑定 loopback。

共享后端和 UI 镜像未修改。补丁适配的后端基底 image ID 为 `sha256:41a2b6929bc74a1bf3c0bac54df09f8e5f835954b3ba58dfba81f703b3ffe17a`；升级 OpenScience 后必须重新评估该补丁，不能跳过校验。

## 启动、停止和令牌轮换

在本工作树执行：

```bash
# 筛选 MCP 必须已完成其 setup；up 会创建客户端内部网络。
python3 scripts/property_filter_mcp_dev.py up

# 只读提取最小模型配置，并生成独立开发目录和凭据文件。
python3 scripts/openscience_mcp_dev.py setup
python3 scripts/openscience_mcp_dev.py up
python3 scripts/openscience_mcp_dev.py smoke
python3 scripts/openscience_mcp_dev.py verify-protected
```

`smoke` 验证独立 UI、API、反向代理和 MCP 连接；它不代替真实模型对话验收。构建/启动日志在私有运行目录的 `docker-up.log` 中，失败会报告该路径而不打印配置或凭据。

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

令牌撤销仍使用筛选 MCP 的 `revoke`，下一次请求即拒绝。轮换后需更新开发 OpenScience 的私有文件并重新创建它的容器；运行中的单文件挂载和实例配置不会自动切换：

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
/home/devuser/.nvm/versions/node/v22.23.2/bin/node scripts/verify_openscience_mcp_browser.mjs
```

探针只允许 `127.0.0.1:19111` / `localhost:19111`，新建浏览器上下文并从真实输入框发送提示，不通过 API 代发、不替换模型或工具输出。API 只读核对该 UI 创建的会话。三个工具必须真实完成、结构化结果 `ok: true` 且版本一致，最终回答必须在页面显示，刷新后仍存在。报告和截图保存到 `.runtime/openscience-mcp-dev/browser-evidence/`，不提交完整对话结果到仓库。

探针以本轮唯一标记定位用户消息，只统计对应 `parentID` 的 assistant/tool parts；精确限定三个完整工具名，避免把历史对话或其他 MCP 的同名工具当成本轮成功。它还核验属性来自本轮目录、搜索与直方图使用同一属性及版本、范围和分页符合提示要求。

实现阶段已通过 32 项启动器测试、3 项双端代理测试、12 项开发补丁/边界测试。真实 HTTP 检查确认五个配置/provider 接口不返回实际模型密钥或 MCP 令牌，私有 `/run` 文件读取返回 `403`。更新凭据后再次检查五个配置接口，新模型密钥和 MCP 令牌均未出现在响应中；开发服务健康、MCP 已连接，共享配置与容器身份校验未变化。

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

工具的单位、AND、版本、证据计数和错误码合同见 [数据库筛选 MCP 文档](property-filter-mcp.md)。独立开发实例验收不代表已接入共享或生产 OpenScience。
