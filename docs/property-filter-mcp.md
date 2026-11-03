# 数据库筛选 MCP：独立开发服务

本服务只开放 `list_filter_properties`、`search_polymers`、`get_property_histogram` 三个只读工具。它复用现有属性筛选查询，使用独立开发客户端凭据、客户端限流和审计，不接入 NexPoly 登录、会员或用户用量，也不修改现有 HTTP API 的权限。

## 固定开发边界

- MCP Compose 项目：`nexpoly_mcp_dev`，仅包含 `property-filter-mcp` 服务。
- 统一开发入口：`http://127.0.0.1:9001/mcp`；内部独立服务及本机诊断地址仍为 `http://127.0.0.1:18110/mcp`，健康检查在该直连地址的 `/healthz`。
- 数据库：既有 `nexpoly_dev-lab-postgres-1` 容器中的 `nexpoly_dev`，通过既有 `nexpoly_dev_default` 网络访问。
- 数据库角色：`nexpoly_mcp_dev_reader`，仅获四张筛选相关表的 `SELECT`，默认只读事务，无其他角色成员关系或管理员权限。
- 独立 MCP 启动器不启动、迁移、导入、停止或重建现有 NexPoly/数据库/Worker 服务；统一入口发布器仅重建 9001 开发前端，不连接生产数据库。

启动器会核对既有数据库容器的 Compose 项目、服务、工作目录 `/data/lzq/gith/nexpoly-dev`、配置文件来源、网络和数据库身份；同时核对 MCP 私有 DSN 的角色、主机、端口、数据库及本地密码是否完全符合该开发目标。身份不符时拒绝操作。MCP 自己的 Compose 命令显式使用 `/dev/null` 作为环境文件，避免加载当前目录的应用 `.env`。

## 数据依赖与权限

| 表 | 用途 |
|---|---|
| `core.polymer_property_filter_records` | 筛选结果、属性目录和直方图实时聚合 |
| `governance.property_filter_options_snapshots` | 属性目录、直方图及总记录数缓存 |
| `governance.import_batches` | 判断缓存是否落后于已完成的数据导入 |
| `governance.source_files` | `property_filter_csv` 来源状态 |

查询角色还需要数据库 `CONNECT`、上述两个 schema 的 `USAGE`。它没有表写入、序列、认证、私人资产或用量表权限。启动检查同时拒绝额外表的列级读取、列级写入、序列权限及数据库/schema/关系所有权，不能通过撤销普通 ACL 后保留所有权绕过检查。只读事务之外，数据库 ACL 继续限制写入。MCP 不修复或重建 catalog：快照缺失、损坏或与最新完成导入不一致时返回 `CATALOG_UNAVAILABLE`，由开发数据维护流程修复。现有 REST 保留实时回退、ETag 和浏览录制行为。

`setup` 只创建/维护带指定管理标记的专用角色。如果同名角色已存在但标记不符、拥有对象、继承角色或获得超出四表范围的权限，操作回滚。它不会撤销其他角色、PUBLIC 或现有应用的权限。

数据库角色及 MCP Compose 项目是此开发机上的固定资源。启动器会核对已有 MCP 容器的工作树和私有文件挂载，拒绝从另一工作树操作；若角色已存在而本地密码/DSN 缺失，也拒绝隐式重置。请在创建服务的本工作树执行启动、停止和令牌管理。

## 凭据与启动

所有命令在本工作树根目录执行，使用已有 Docker/Compose。`setup` 会修改上述开发数据库的专用角色，其他命令仅控制独立 MCP 项目。

```bash
python3 scripts/property_filter_mcp_dev.py setup
python3 scripts/property_filter_mcp_dev.py up
python3 scripts/property_filter_mcp_dev.py status
python3 scripts/property_filter_mcp_dev.py smoke
```

镜像使用独立 Python 3.11 依赖锁 `backend/requirements-property-filter-mcp.lock`，不加载 GPU、模型或整个应用的启动生命周期；没有修改原有后端依赖锁。`up` 构建并等待 MCP 健康；不会重启现有开发服务。镜像构建使用宿主网络并接收标准 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` 构建参数，以适配当前开发机的下载代理；这些参数不写入运行容器环境，运行服务仍只连接开发数据库网络。

构建和启动输出保存在 `0600` 的 `.runtime/property-filter-mcp/docker-up.log`，启动器会先输出该日志路径，失败时保留诊断。SQL 配置及凭据输入不写入此日志。

运行文件位于被 Git 忽略的 `.runtime/property-filter-mcp/`，目录为 `0700`、文件为 `0600`：

| 文件 | 用途 |
|---|---|
| `server/dsn` | 专用角色的数据库连接信息 |
| `server/clients.json` | 客户端 ID、令牌 SHA-256、有效期、允许工具 |
| `client.env` | 供开发客户端使用的 MCP URL 与明文令牌 |
| `db-password` | 仅供重复执行角色配置使用的专用开发密码 |

容器只挂载 `server/` 子目录，无法读取 `client.env` 或 `db-password`。令牌和数据库密码不会出现在启动器的命令参数、输出或 Compose 环境值中。服务从 `NEXPOLY_MCP_DSN_FILE` 和 `NEXPOLY_MCP_CLIENTS_FILE` 读取私有文件。

初始客户端为 `openscience-dev`，令牌有效期 30 天，允许上述三个工具，每分钟最多 60 次工具调用、并发最多 2 个。`setup` 重复执行保留已有客户端状态和令牌；已撤销或过期的令牌不会被隐式重新启用。

限流及超时可在独立 Compose 的 `NEXPOLY_MCP_RATE_LIMIT`、`NEXPOLY_MCP_MAX_CONCURRENCY`、`NEXPOLY_MCP_STATEMENT_TIMEOUT_MS`、`NEXPOLY_MCP_TOOL_TIMEOUT_SECONDS`、`NEXPOLY_MCP_LOCK_TIMEOUT_MS` 中收紧；服务拒绝超过 60/分钟、2 并发、20 秒查询、30 秒调用、2 秒锁等待的配置。每个客户端独立计数，令牌轮换沿用同一 `client_id` 的限制；单进程运行，不支持靠增加进程数扩容限流。

```bash
# 下一次请求起拒绝现有令牌；服务继续运行。
python3 scripts/property_filter_mcp_dev.py revoke

# 生成新令牌并重设 30 天有效期；旧令牌立即失效。
python3 scripts/property_filter_mcp_dev.py rotate-token

# 只停止/删除独立 MCP 容器，保留凭据和专用只读角色。
python3 scripts/property_filter_mcp_dev.py down
```

日志只记录客户端 ID、请求 ID、工具、耗时、结果数及状态；不将这些记录写入个人用户用量。启动检查失败返回固定错误，不输出可能携带私有配置值的原始异常。不要把整个 `client.env` 或数据库连接信息复制到聊天、仓库或共享日志。

## 9001 统一入口、内部独立服务

开发入口采用以下路由，不合并后端进程或数据库身份：

```text
9001/       → 原 NexPoly 开发页面
9001/api/   → 原 NexPoly 开发后端
9001/mcp    → nexpoly_mcp_dev-property-filter-mcp-1:18110/mcp
```

当前 9001 使用只读源码快照。专用发布器保留当前快照的所有页面改动，只修改 Vite 配置并增加 MCP 代理模块；先在 `127.0.0.1:29101` 和独立依赖卷验证候选，再仅重建开发前端容器。它记录当前版本作为回滚目标，检查发布期间源码及容器身份没有漂移，不调用完整开发栈的启动或迁移命令。代理源码须先提交，以便快照记录准确的代码版本。

```bash
python3 scripts/property_filter_mcp_gateway_dev.py apply
python3 scripts/property_filter_mcp_gateway_dev.py status
# 当前仍为本次发布版本时，可恢复发布前的开发前端；MCP 服务继续运行。
python3 scripts/property_filter_mcp_gateway_dev.py rollback
```

私有发布状态位于 `.runtime/property-filter-mcp/gateway/`；源码快照仍保存在现有本机 `ui-releases/` 目录。发布器只支持已确认的开发容器、9001 端口及源码基线，遇到不同基线时拒绝覆盖，需要先审查并合并代理改动。后续页面发布接续该快照后，旧操作的 `apply` 和 `rollback` 都会拒绝覆盖；撤回代理需基于当前页面版本合并，不能回退掉后续页面改动。`status` 中 `phase` 表示已记录操作的阶段，`source_hash` 和 `gateway_configured` 则来自当前运行版本。

普通开发 Compose 模板也支持显式启用：在私有开发环境文件中设置 `NEXPOLY_DEV_MCP_PROXY_TARGET=http://nexpoly_mcp_dev-property-filter-mcp-1:18110`，并设置 `NEXPOLY_DEV_MCP_PROXY_ALLOWED_HOSTS` 为实际入口的 `host:port` 列表。默认不启用，避免其他开发项目意外连接本机 MCP；不要用普通 Compose 命令覆盖当前 9001 的源码快照。

代理仅在 Vite 的 development serve 模式注册，不进入 build 或 preview。当前入口 Host 白名单为 `127.0.0.1:9001`、`localhost:9001`、`114.214.255.154:9001`；请求通过校验后才改写为内部目标 Host。Authorization、Origin、MCP 协议头、结构化响应和 SSE 内容按流转发；不注入凭据，不使用 NexPoly 登录 cookie。原服务的令牌校验、32 KiB 请求限制、只读权限和限流继续生效；代理总时限为 35 秒，服务调用时限仍为 30 秒。

客户端地址改为 `http://127.0.0.1:9001/mcp` 即可，Bearer 令牌仍从本机私有 `client.env` 读取；该文件保留直连 URL，供原有开发客户端和诊断使用。浏览器直接打开 `/mcp` 而没有令牌时返回 401，这是协议入口，不是结果展示页。Origin 仍由 MCP 服务校验，默认拒绝浏览器 Origin。

```bash
PYTHONPATH=backend .runtime/property-filter-mcp/venv/bin/python \
  scripts/verify_property_filter_mcp.py --endpoint gateway
```

验收同时验证真实客户端初始化、三个工具和开发库 SQL 基准，摘要原子写入 `acceptance-gateway.json`（0600）。`--endpoint direct` 保留直连验收并写入 `acceptance.json`。两种模式都禁止重定向和任意 URL；健康探针继续直连 `/healthz`，报告单独标明协议验收入口。独立开发 OpenScience 保持原内部连接；本次没有给共享 9011 配置 MCP。

## 工具合同

工具同时返回 MCP `structuredContent` 和简短文本说明，失败设置 `isError: true` 并返回 `{"ok":false,"error":{"code":"…","message":"…"}}`。所有工具都禁止额外参数、SQL、DSN、角色或用户身份。

| 工具 | 参数 | 主要结果 |
|---|---|---|
| `list_filter_properties` | `q` 可选；`page=1`、`page_size=25`（最多 50）；第二页起必须带 `dataset_revision` | `options` 包含 key、名称、单位、范围、分位数、测量计数；不内嵌直方图 |
| `search_polymers` | `dataset_revision`；1–8 条 `filters`；可选 `q`；`page=1`、`page_size=10`（最多 25）；`measurement_limit_per_filter=1`（最多 3） | `matched_material_count` 为结构组数；每结构的 `evidence` 按条件列出完整命中测量计数、样本测量及 `truncated` |
| `get_property_histogram` | `dataset_revision`、`option_key` | 全库该选项的 `histogram`、单位及版本；不受前次搜索条件影响 |

每条筛选条件为 `{"option_key":"目录返回的原样 key","min_value":100,"max_value":200}`，上下界至少一个；拒绝未知选项、非有限数值和倒置区间。上下界必须为 JSON 数字，不能使用布尔值或数字字符串；页码、页大小和证据上限必须为 JSON 整数，页码上限为 `2147483647`。上下界包含端点。属性及单位严格绑定到目录选项，包括空单位 `""`，搜索阶段不换算单位。

目录字段经过验证后以内部条件模型绑定，不套用 REST 用户输入的名称/单位限长或去空格规则；REST 原有参数限制保持不变。已存在的长 `option_key` 仍可原样提交，整个请求仍受 32 KiB 限制。新目录对会产生分隔符歧义或过长的标识使用紧凑不透明 key，普通 key 和既有唯一快照 key 保持兼容。NULL 与空单位按照已有搜索语义合并；重复 key、统计矛盾或无效批次的快照会被拒绝，不在 MCP 调用中自动修复。

第一次目录调用取得不透明 `dataset_revision`。目录后续翻页、搜索和直方图应持续使用同一版本；版本变化时重新获取第一页并重启查询。不要解析版本或跨版本使用 `record_ref` / `material_ref`。

AND 表示同一 canonical SMILES 结构组下，各条件分别存在满足要求的测量；canonical SMILES 缺失时沿用 SMILES，再回退到记录。不同条件不一定来自同一样品或实验。关键词沿用每个条件分支中的名称/SMILES 子串匹配。证据保留原始值、标准值、单位、来源类型与质量字段，不补造 DOI 或实验信息。

`total_measurement_count` 为数据集属性记录数，`matched_material_count` 为命中结构组数，二者不可混用。`matching_measurement_count` 为该结构该条件的完整测量计数，`matching_evidence_count` 是各条件计数之和；同一条测量满足多个条件时可能重复计入。证据排序沿用现有 observed / median / imputed 优先级、质量分和记录 ID，SQL 在完成 AND 与结构分页后才限制证据条数。

每次调用使用只读事务，先取得筛选表 `ACCESS SHARE` 锁，再读取目录和数据；锁等待最多 2 秒，单条数据库查询最多 20 秒，调用总时限 30 秒。请求体最多 32 KiB，总响应最多 256 KiB，超限会报错并提示缩小页大小、证据数或范围，不静默丢弃结构或截断 SMILES。响应头 `X-Request-ID` 可关联审计日志。

| 错误码 | 客户端处理 |
|---|---|
| `INVALID_ARGUMENT` / `UNKNOWN_OPTION` | 修正参数；从目录获取准确 option key |
| `CATALOG_UNAVAILABLE` | 等待开发数据维护恢复有效目录；MCP 不自动重建 |
| `DATASET_REVISION_CHANGED` | 重新获取目录并重启分页/查询 |
| `QUERY_TIMEOUT` | 缩小筛选范围；导入锁冲突时稍后重试 |
| `RESPONSE_TOO_LARGE` | 减少页大小、每条件证据数或缩小范围 |
| `DATA_UNAVAILABLE` | 数据或查询不可用，携请求 ID 排查服务日志 |
| `PERMISSION_DENIED` | 检查该客户端的工具授权 |
| `RATE_LIMITED` / `CONCURRENCY_LIMITED` | 等待窗口或正在执行的调用完成 |

零命中正常返回 `ok: true` 和空结果。无效、过期或撤销令牌在 HTTP 层返回 401；Host、Origin、URL 查询参数及请求体也在入口层校验。默认不接受浏览器 Origin。

## 开发客户端与 OpenScience

客户端需要支持 MCP Streamable HTTP，并以服务端安全配置提供 `Authorization: Bearer …`；令牌从本机私有 `client.env` 读取，不应放入模型提示词或工具参数。下例使用 9001 统一入口，该文件中的 URL 则保留为 18110 直连地址。`smoke` 使用直连地址和该凭据完成初始化、工具发现和属性目录调用，只输出验证摘要。

以下示例使用独立锁中的官方 Python SDK 2.3.0；在仓库根目录保存后用下述独立虚拟环境运行。`trust_env=False` 和关闭重定向可避免本地令牌转发给代理或其他主机。

```python
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

async def main():
    settings = dict(line.split("=", 1) for line in
        Path(".runtime/property-filter-mcp/client.env").read_text().splitlines())
    assert settings["NEXPOLY_MCP_URL"] == "http://127.0.0.1:18110/mcp"
    url = "http://127.0.0.1:9001/mcp"
    async with httpx2.AsyncClient(trust_env=False, follow_redirects=False,
        headers={"Authorization": "Bearer " + settings["NEXPOLY_MCP_TOKEN"]}) as http:
        @asynccontextmanager
        async def transport():
            async with streamable_http_client(url, http_client=http) as streams:
                yield streams
        async with Client(transport(), read_timeout_seconds=35) as client:
            result = await client.call_tool("list_filter_properties", {"page_size": 5})
            catalog = result.structured_content
            assert not result.is_error, catalog
            option = next(row for row in catalog["options"] if row["min_value"] is not None)
            revision = catalog["dataset_revision"]
            search = await client.call_tool("search_polymers", {
                "dataset_revision": revision, "page_size": 2,
                "filters": [{"option_key": option["option_key"], "min_value": option["min_value"]}]})
            histogram = await client.call_tool("get_property_histogram", {
                "dataset_revision": revision, "option_key": option["option_key"]})
            print({"search_ok": not search.is_error, "histogram_ok": not histogram.is_error})

asyncio.run(main())
```

独立 OpenScience 开发接入已实现，入口为 `http://127.0.0.1:19111/`，操作及真实对话验证见 [开发接入说明](openscience-property-filter-mcp-dev.md)。现有 `9001` 与生产 `9000` 共用的 `9011` OpenScience 保持不变。独立后端仅通过 `nexpoly_mcp_clients_dev` 内部网络访问 MCP，不直接进入数据库网络。

容器中的 `127.0.0.1` 指向容器自己。若 OpenScience 后端在另一个容器内，应先确定开发网络/代理或隧道路径；默认的宿主 loopback 地址不能直接当作跨容器地址使用。任何额外监听地址和 Host/Origin 白名单调整都应限制在所选开发环境。

## 验证

启动器测试不访问 Docker 或数据库：

```bash
python3 -m unittest discover -s scripts/tests -p test_property_filter_mcp_dev.py
```

安装独立测试环境并运行隔离测试（需 `uv` 和 Docker）：

```bash
uv venv --python 3.11 .runtime/property-filter-mcp/venv
uv pip sync --python .runtime/property-filter-mcp/venv/bin/python \
  --require-hashes backend/requirements-property-filter-mcp-test.lock
python3 scripts/test_property_filter_mcp.py
```

测试启动器自己创建专用临时 PostgreSQL 容器、随机 loopback 端口、合成账号和数据，退出时清理容器；不会接收应用数据库 DSN。测试覆盖真实 MCP 客户端、无效/过期/撤销令牌、工具权限、限流、并发、SQL 写入拒绝、单位、AND、canonical 分组、版本变化、失效目录、大量证据、响应上限、真实锁超时与查询取消、并发 TRUNCATE 导入一致性。现有 backend PostgreSQL fixture 会创建和删除临时数据库，禁止对现有业务库运行该 fixture。

真实开发库只读验收使用另外的脚本，不使用上述初始化/清理 fixture：

```bash
PYTHONPATH=backend .runtime/property-filter-mcp/venv/bin/python \
  scripts/verify_property_filter_mcp.py
```

该脚本先核验固定开发目标及已发布的 loopback 端口，再使用专用只读凭据；比较 MCP、共享查询和直接 SQL 的目录计数、标准/raw/AND 命中、每条件证据和直方图，并输出不含凭据或完整数据的耗时/响应体积报告。

## 首次验收结果（2026-11-02）

- 隔离 MCP 单元与真实协议/数据库测试：**24 passed**，约 13 秒；一次命令自行建立并清理独立 PostgreSQL。
- 原有筛选与浏览录制回归：**40 passed**，约 60 秒；在另一个临时测试容器及独立 PostgreSQL 运行，含 REST、ETag、`search_id` 兼容检查。仅有既有依赖弃用警告。
- 启动器测试：**19 passed**，包括代理/重定向拒绝、文件权限、跨工作树归属保护及错误 DSN 拒绝。
- 独立镜像约 **78 MiB**，服务健康检查通过。既有开发前后端、数据库容器未重启；测试容器已清理。

真实开发库验收使用 `nexpoly_mcp_dev_reader`，数据版本为 `1c8bf0f452a6383023989e0715c63123cf699bd4d11a748ea34dc32f0799800a`，完整报告保存在私有 `.runtime/property-filter-mcp/acceptance.json`。以下为本次开发机的观测值，不是延迟保证；搜索页大小为 3、每条件最多 2 条测量，范围使用目录的完整 min/max。

| 验收项 | 基准核对结果 | 耗时 | 响应体积 |
|---|---|---|---|
| 目录全部 4 页 | 615,159 条测量，188 个选项 | 每页 123–298 ms | 每页 14,131–18,538 B |
| 标准 Tg (`std:tg:C`) | 18,614 个结构组，证据计数/排序/截断一致 | 439 ms | 7,204 B |
| 目录首个原始属性 | 18,558 个结构组，证据一致 | 312 ms | 4,789 B |
| Tg + Td 跨测量 AND | 12,114 个结构组，两条件证据一致 | 448 ms | 8,629 B |
| Tg 全库直方图 | 45,160 条测量，所有桶及上下溢出计数一致 | 251 ms | 775 B |

脚本固定检查本次已确认的 615,159 / 188 快照数量；未来导入变更后需先确认新验收基准，不能把版本变化视为结果可直接比较。

## 独立服务复审与修复（2026-11-03）

本轮仅更新 NexPoly 独立 MCP 的实现和开发容器，未更新 OpenScience 镜像、配置或生产部署。依赖锁、三个只读工具及每客户端 60 次/分钟、2 并发保持不变。

已修复：目录长字段无法搜索、目录原文被归一化、分隔符造成选项键混淆、NULL/空单位产生重复选项、损坏/错误批次快照被接受、数字字符串和布尔值被隐式转换、过大页码进入 SQL，以及启动权限漏检和私有配置异常日志泄露。内部目录绑定支持有效的空字符串标识；缺失标识仍作为损坏目录拒绝。既有唯一快照 key 和数据版本保持兼容，没有重建开发目录。

最终验证：

- `python3 scripts/test_property_filter_mcp.py`：**134 passed**，17.88 秒；包括真实 MCP 协议、只读角色、认证、限流、并发、超时取消、导入锁一致性及新增目录/权限回归。临时 PostgreSQL 已自动清理。
- 启动器测试：**19 passed**。新增角色配置 SQL 的权限拒绝、事务回滚和正常配置用例包含在上述隔离 MCP 测试中。
- `backend/tests/test_database_browser.py` 与 `backend/tests/test_browsing_recording.py`：**40 passed**，69.04 秒；使用独立临时 PostgreSQL 和测试容器，保留 REST、ETag、`search_id` 和浏览录制兼容性。只有既有依赖弃用警告；测试容器和网络已清理。
- 已构建并启动独立 `nexpoly_mcp_dev`，健康检查及真实客户端发现三个工具通过；共享 OpenScience 的 `verify-protected` 返回 `shared_unchanged: true`。
- 实际开发库只读验收通过，数据版本仍为上文版本，测量数 **615,159**、选项数 **188**；目录、命中结构组数、证据计数/排序/截断及直方图均与共享查询和直接 SQL 一致。

本次开发库观测：目录四页各 127–288 ms、14,131–18,538 B；标准 Tg 搜索 441 ms / 7,204 B，原始属性搜索 236 ms / 4,789 B，Tg + Td AND 搜索 416 ms / 8,629 B，Tg 直方图 230 ms / 775 B。计数分别保持 18,614、18,558、12,114 个结构组及 45,160 条直方图测量。最新摘要报告位于私有 `.runtime/property-filter-mcp/acceptance.json`，不包含凭据或完整测量数据。

本轮独立服务验收使用直连地址 `http://127.0.0.1:18110/mcp`，通过本机私有 `client.env` 提供 Bearer 凭据。

## 9001 统一入口验收（2026-11-03）

已发布并验证 `http://127.0.0.1:9001/mcp`。独立 MCP 容器、数据库角色、三个工具及客户端限流保持不变；本次入口发布只重建开发前端。候选端口与正式 9001 均通过三个工具调用，原 `/`、`/health`、`/api/v1/auth/session` 的状态码和响应类型一致。候选容器和独立依赖卷已清理。

- 新增相关测试 **57 passed**：真实 Vite 代理测试 8 项、发布器测试 33 项、Compose 交付约束 10 项、验收脚本测试 6 项；代理 TypeScript 检查通过。
- 真实 9001 请求检查通过：缺少/无效令牌返回 401，不受信任的 Origin 返回 403，未列入白名单的 Host 返回 421，URL 查询参数返回 400，超过 32 KiB 的请求返回 413；带有效凭据的 `/mcp/` 无重定向初始化成功。
- 官方 MCP 客户端只发现三个工具，目录、标准/原始属性搜索、跨测量 AND、证据和直方图与共享服务及独立 SQL 基准一致。数据版本、615,159 条测量和 188 个选项未变，仍使用专用只读角色。
- 入口发布保留原快照的 664 个其他文件，仅修改 Vite 配置并新增代理模块。随后独立的 `usage-activity` 页面发布接续了该版本；已核对其代理源码和配置完整保留，并于 11:07（北京时间）对该当前运行版本重跑客户端和 SQL 验收通过，没有覆盖后续页面改动。
- 未修改生产配置或共享 9011；`verify-protected` 返回 `shared_unchanged: true`。本次复审未发现阻断项。

当前运行版本的实测数据如下，耗时不作为延迟保证：

| 验收项 | 基准核对结果 | 耗时 | 响应体积 |
|---|---|---|---|
| 目录全部 4 页 | 615,159 条测量，188 个选项 | 每页 123–200 ms | 每页 14,131–18,538 B |
| 标准 Tg | 18,614 个结构组，证据一致 | 431 ms | 7,204 B |
| 原始属性 | 18,558 个结构组，证据一致 | 296 ms | 4,789 B |
| Tg + Td 跨测量 AND | 12,114 个结构组，两条件证据一致 | 413 ms | 8,629 B |
| Tg 全库直方图 | 45,160 条测量，所有桶及上下溢出计数一致 | 131 ms | 775 B |

私有验收摘要为 `.runtime/property-filter-mcp/acceptance-gateway.json`，入口边界记录为同目录 `gateway-edge-report.json`，均为 0600，不包含凭据或完整测量数据。18110 直连诊断入口继续可用。

这是一份独立开发部署合同；生产暴露、用户 OAuth、会员/计量绑定及生产 OpenScience 配置不在本期范围。
