# 数据库筛选 MCP：独立开发服务

本服务只开放 `list_filter_properties`、`search_polymers`、`get_property_histogram` 三个只读工具。它复用现有属性筛选查询，使用独立开发客户端凭据、客户端限流和审计，不接入 NexPoly 登录、会员或用户用量，也不修改现有 HTTP API 的权限。

## 固定开发边界

- MCP Compose 项目：`nexpoly_mcp_dev`，仅包含 `property-filter-mcp` 服务。
- 地址：`http://127.0.0.1:18110/mcp`；健康检查：`/healthz`。
- 数据库：既有 `nexpoly_dev-lab-postgres-1` 容器中的 `nexpoly_dev`，通过既有 `nexpoly_dev_default` 网络访问。
- 数据库角色：`nexpoly_mcp_dev_reader`，仅获四张筛选相关表的 `SELECT`，默认只读事务，无其他角色成员关系或管理员权限。
- 不启动、迁移、导入、停止或重建现有 NexPoly/数据库/Worker 服务；不连接生产数据库。

启动器会核对既有数据库容器的 Compose 项目、服务、工作目录 `/data/lzq/gith/nexpoly-dev`、配置文件来源、网络和数据库身份；同时核对 MCP 私有 DSN 的角色、主机、端口、数据库及本地密码是否完全符合该开发目标。身份不符时拒绝操作。MCP 自己的 Compose 命令显式使用 `/dev/null` 作为环境文件，避免加载当前目录的应用 `.env`。

## 数据依赖与权限

| 表 | 用途 |
|---|---|
| `core.polymer_property_filter_records` | 筛选结果、属性目录和直方图实时聚合 |
| `governance.property_filter_options_snapshots` | 属性目录、直方图及总记录数缓存 |
| `governance.import_batches` | 判断缓存是否落后于已完成的数据导入 |
| `governance.source_files` | `property_filter_csv` 来源状态 |

查询角色还需要数据库 `CONNECT`、上述两个 schema 的 `USAGE`。它没有表写入、序列、认证、私人资产或用量表权限。只读事务之外，数据库 ACL 继续限制写入。MCP 不修复或重建 catalog：快照缺失、损坏或落后于完成导入时返回 `CATALOG_UNAVAILABLE`，由开发数据维护流程修复。现有 REST 保留实时回退、ETag 和浏览录制行为。

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

日志只记录客户端 ID、请求 ID、工具、耗时、结果数及状态；不将这些记录写入个人用户用量。不要把整个 `client.env` 或数据库连接信息复制到聊天、仓库或共享日志。

## 工具合同

工具同时返回 MCP `structuredContent` 和简短文本说明，失败设置 `isError: true` 并返回 `{"ok":false,"error":{"code":"…","message":"…"}}`。所有工具都禁止额外参数、SQL、DSN、角色或用户身份。

| 工具 | 参数 | 主要结果 |
|---|---|---|
| `list_filter_properties` | `q` 可选；`page=1`、`page_size=25`（最多 50）；第二页起必须带 `dataset_revision` | `options` 包含 key、名称、单位、范围、分位数、测量计数；不内嵌直方图 |
| `search_polymers` | `dataset_revision`；1–8 条 `filters`；可选 `q`；`page=1`、`page_size=10`（最多 25）；`measurement_limit_per_filter=1`（最多 3） | `matched_material_count` 为结构组数；每结构的 `evidence` 按条件列出完整命中测量计数、样本测量及 `truncated` |
| `get_property_histogram` | `dataset_revision`、`option_key` | 全库该选项的 `histogram`、单位及版本；不受前次搜索条件影响 |

每条筛选条件为 `{"option_key":"目录返回的原样 key","min_value":100,"max_value":200}`，上下界至少一个；拒绝未知选项、非有限数值和倒置区间。上下界包含端点。属性及单位严格绑定到目录选项，包括空单位 `""`，搜索阶段不换算单位。

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

客户端需要支持 MCP Streamable HTTP，并以服务端安全配置提供 `Authorization: Bearer …`；URL 和令牌从本机私有 `client.env` 读取，不应放入模型提示词或工具参数。`smoke` 使用该凭据完成初始化、工具发现和属性目录调用，只输出验证摘要。

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
    url = settings["NEXPOLY_MCP_URL"]
    assert url == "http://127.0.0.1:18110/mcp"
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

本仓库目前只有 OpenScience iframe 桥和 UI overlay，没有其后端 MCP 配置契约。因此此变更交付可验证的 MCP 服务和开发凭据，不声称已修改或接通现有 OpenScience 后端。实际接入要在已确认的开发后端中添加此远程 MCP，并验证会话真实调用这三个工具。

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

## 本次验收结果（2026-11-02）

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

这是一份独立开发部署合同；生产暴露、用户 OAuth、会员/计量绑定及生产 OpenScience 配置不在本期范围。
