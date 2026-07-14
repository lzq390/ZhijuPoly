# NexPoly / 智聚万物

NexPoly 是面向聚合物研发的内部 Web 平台，覆盖结构处理、数据检索、性质预测、聚合物设计、知识检索和分子模拟。当前项目已经不再是早期基于 SQLite 的单一 PolyProp 查询器。

本文档是仓库级内部开发入口，会同时出现在 dev 与 prod 工作区。README 的存在不代表当前环境是 dev；执行任何命令前，必须根据 Compose 项目名、使用的配置文件和 `docker compose config` 解析结果确认操作对象。部署、数据库迁移和 Worker 的操作细节以文末专项文档及代码清单为准。

## 当前架构

```text
React 19 / Vite / Ketcher / 3Dmol
                  |
                  v
          Nginx（生产入口）
                  |
                  v
                    FastAPI API
          /             |             |              \
 PostgreSQL      本地模型运行时      在线知识服务      Monomer-MD Worker
                                                  （HTTP / UDS，ByteFF2 / OpenMM / GROMACS）
```

- 前端负责工作台、数据分析、任务轮询、Ketcher 结构编辑和 3D 展示。
- FastAPI 后端提供 `/api/v1` 接口，承载 RDKit 结构处理、随机森林预测和本地生成模型。
- PostgreSQL 是唯一在线结构化数据后端；SQLite 仅保留为历史导入、迁移或审计输入。
- OCSR、条件生成、逆合成和 PolyTAO 等能力依赖本地模型资产与功能开关。
- Monomer-MD 通过 HTTP 或 Unix Domain Socket 连接独立 Worker，作业状态写入 PostgreSQL。

## 功能边界

| 类型 | 模块 |
|---|---|
| 真实业务能力 | 结构工作台、SMILES 标准化与检索、性质预测、数据库分析、实验数据、知识检索、Tg 逆向设计、条件生成、PolyTAO、单体正向聚合与逆合成 |
| 依赖外部运行时 | 结构图片识别、本地大模型生成、在线知识抽取、正式 Monomer-MD 协议 |
| Demo 能力 | 内置轨迹 MD Demo、LocalStorage 实验流程、固定场景高通量优化、预制结果 PDF 相似度预览 |

功能是否可用应以对应 `/status` 接口、环境开关和模型资产检查结果为准，不应仅根据页面是否显示判断。Demo 结果不得描述为在线计算结果。

## Dev 与 Prod 隔离

本文件由 dev 和 prod 共用，不能根据仓库路径、README 内容或本地文件是否存在来推断当前运行环境。所有环境操作都必须显式使用对应的 Compose 项目名和配置文件，并以解析后的 Compose 配置为准。

| 环境 | Compose 项目名 | 前端入口（默认值） | 后端（默认值） | PostgreSQL（默认值） | 端口覆盖 |
|---|---|---|---|---|---|
| dev | `nexpoly_dev` | `127.0.0.1:15173` | `127.0.0.1:18000` | `127.0.0.1:15532` | `NEXPOLY_DEV_FRONTEND_PORT`、`NEXPOLY_DEV_BACKEND_PORT`、`NEXPOLY_DEV_POSTGRES_PORT` |
| prod | `nexpoly` | `:9000` | Compose 内部 `:8000` | `127.0.0.1:55432` → 容器 `:5432` | `NEXPOLY_WEB_PORT`、`NEXPOLY_POSTGRES_PORT` |

dev 的默认宿主机端口仅绑定 loopback，并使用独立数据库和开发配置。dev 端口可由表中的 `NEXPOLY_DEV_*_PORT` 变量覆盖；prod 前端和 PostgreSQL 宿主机端口可分别由 `NEXPOLY_WEB_PORT`、`NEXPOLY_POSTGRES_PORT` 覆盖。未经明确授权，不得从 dev 流程启动、停止、迁移或探测 prod 服务。`9000` 和 `55432` 都只是 prod Compose 的默认值，不是识别 prod 的固定标志；端口被覆盖后仍须以 Compose 项目和解析配置判断环境。

`scripts/dev_server_gpu.sh` 只能用于已配置的 dev 工作区，不得在 prod 工作区执行；prod 操作统一遵循生产部署文档。

## Dev 工作流（仅适用于已配置的 Dev 工作区）

内部 dev 工作区可使用以下本机文件：

- `docker-compose.dev.yml`
- `.env.dev` 与可选的 `.env.dev.ai`
- `scripts/dev_server_gpu.sh`

Compose overlay、无密钥环境示例和启动脚本已纳入版本控制；`.env.dev`、`.env.dev.ai` 与 Worker 运行目录仍保持忽略并应为 mode `0600`。复制 `.env.dev.example` 后必须把 `NEXPOLY_ASSET_ROOT` 固定到只读资产 release，并确认解析后的 Compose 项目为 `nexpoly_dev`，才使用这些命令：

```bash
# 校验固定资产 release，构建后端，应用增量迁移并启动 dev 服务
./scripts/dev_server_gpu.sh up

# 仅在明确需要刷新源数据时执行全量导入
./scripts/dev_server_gpu.sh refresh-data

# 查看状态和日志
./scripts/dev_server_gpu.sh ps
./scripts/dev_server_gpu.sh logs backend
./scripts/dev_server_gpu.sh logs frontend-dev

# 验证数据库、API 和静态资源
./scripts/dev_server_gpu.sh preflight
./scripts/dev_server_gpu.sh smoke

# 仅在审阅 destructive contract 后显式执行；普通 up 永不自动执行
./scripts/dev_server_gpu.sh contract-migrate

# 运行当前 dev 快速测试与前端构建
./scripts/dev_server_gpu.sh test-backend
./scripts/dev_server_gpu.sh build-frontend

# 停止服务；down 会同时删除 Compose 网络，但不会删除命名卷
./scripts/dev_server_gpu.sh stop
./scripts/dev_server_gpu.sh down
```

首次构建统一 CUDA 镜像可能耗时较长。普通 `up` 只应用非破坏性增量迁移，不重复导入数据；检测到待执行 contract 时会失败并要求显式运行 `contract-migrate`。该命令会先生成全库和目标表归档、SHA-256 与行数证据，并在临时数据库真实恢复核对后才迁移。`refresh-data` 的首次或全量导入可能耗时较长。在 `postgres-init` 完成前，不要把尚未启动的后端或前端判断为代码故障。

开发后端使用 Docker Compose 的默认 builder 构建，不创建常驻的专用
Buildx/BuildKit 容器。四类 GPU 模型共享严格 FIFO 的推理准入，默认同一
时刻只执行一个模型加载或 GPU 推理；HTTP、数据库和轮询请求仍可并发。
PolyTAO 的最大公开参数保持不变，但内部按最多 2 条序列的微批次采样，避免
单个任务一次分配 100 条解码缓存而耗尽 24GB 显存。
条件生成和 PolyTAO 保留 `202 + job_id + 轮询`，但作业状态存放在 Backend
单进程的有界内存中；重启或终态淘汰后查询返回 410，前端会保留输入并提示重提。

## 干净克隆与外部资产

仓库不包含完整的生产数据、模型 checkpoint、密钥或宿主机 Worker 环境。干净克隆不能直接假定所有模块可用，需要单独准备：

- PostgreSQL 与所需数据源；
- OCSR、条件生成、逆合成及 PolyTAO 等模型资产；
- 在线知识服务所需密钥；
- 正式 Monomer-MD 所需的 ByteFF2/OpenMM/GROMACS 环境；
- 与目标机器匹配的 dev overlay 或生产环境配置。

不要在 README 复制容易漂移的完整资产列表。模型清单以 [`backend/app/model_asset_manifest.py`](backend/app/model_asset_manifest.py) 为准，生产资产以只读 `ASSET-MANIFEST.json` 为准，部署检查以 [`scripts/release_controller.py`](scripts/release_controller.py) 及发布控制器文档为准。

## 仓库目录

| 目录 | 用途 |
|---|---|
| `frontend/` | React/Vite 前端、Ketcher 与 3Dmol 静态资源 |
| `backend/` | FastAPI、领域服务、PostgreSQL 迁移、导入工具和测试 |
| `workers/` | 需要独立科学计算环境的 Monomer-MD Worker；PolyTAO 直接运行在 backend 内 |
| `database/` | 可跟踪的基础 CSV；大体量运行数据通常由部署环境提供 |
| `model/` | 随仓库提供的 RF 模型和外部模型目录入口 |
| `docs/` | 部署、数据库治理、Worker 和历史设计文档 |
| `design-system/` | 平台 UI 规范与页面级设计约束 |
| `scripts/` | 发布、部署、数据准备及本机开发辅助脚本 |

## 测试与基本验证

后端测试是 PostgreSQL 集成测试。测试账户必须能够创建和删除临时数据库；不得让测试静默回退到 SQLite。完整要求见 [`backend/tests/README.md`](backend/tests/README.md)。

前端先运行单元测试，再执行作为主要门禁的 TypeScript/Vite 构建：

```bash
cd frontend
npm ci
npm test
npm run build
```

生产 Compose 配置可用以下命令做无副作用检查：

```bash
docker compose config --quiet
```

## 开发约束

- 运行时代码只面向 PostgreSQL；不要重新引入 SQLite 在线分支。
- `backend/migrations/postgres/` 是 append-only 合约。已应用迁移不得直接改写，也不得通过手工更新账本绕过 checksum。
- 未经明确授权，不得在 prod 执行 `--rebuild`、截断业务表、修改迁移账本或清理运行资产。
- 模型、数据导入和 Worker 状态必须通过现有 preflight/status 机制判断。
- 修改功能时应明确区分真实后端能力与纯前端 Demo，避免把预制结果描述为在线计算结果。

## 权威文档

- [生产部署与不可变发布控制器](docs/release-controller.md)
- [CI/CD 与生产部署入口](docs/deployment.md)
- [PostgreSQL 迁移治理](docs/postgres-migration-governance.md)
- [Monomer-MD Worker](docs/monomer-md-worker.md)
- [后端测试环境](backend/tests/README.md)
- [UI 设计系统](design-system/polyprop/MASTER.md)

`docs/superpowers/` 保存历史设计和实施方案，其中大量内容基于旧 SQLite 架构，只用于追溯决策，不作为当前架构、运行方式或开发约束。
