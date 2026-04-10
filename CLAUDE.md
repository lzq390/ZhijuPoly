# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

PolyProp 是一个聚合物结构检索工具。用户在 Ketcher 中绘制聚合物结构，生成 SMILES 字符串后在本地 SQLite 数据库（由 `database/data1.csv` 导入构建）中执行精确或相似度查询，结果按属性类别（Thermal、Mechanical、Electrical、Chemical、Optical、Others）分组展示。

数据流：`CSV → import_csv → SQLite → FastAPI → React 前端`

## 常用命令

### 后端

```bash
# 安装依赖（从项目根目录执行）
cd backend && pip install -r requirements.txt

# 构建数据库（启动服务前必须先执行）
cd backend && python -m app.import_csv

# 启动 API 服务（监听 :8000）
cd backend && uvicorn app.main:app --reload

# 运行所有测试
cd backend && pytest

# 运行单个测试文件
cd backend && pytest tests/test_matcher.py

# 运行单个测试用例
cd backend && pytest tests/test_matcher.py::test_exact_match_canonical
```

### 前端

```bash
# 安装依赖
cd frontend && npm install

# 启动开发服务器（端口 5173，/api 请求代理到 localhost:8000）
cd frontend && npm run dev

# 构建生产产物
cd frontend && npm run build
```

### 环境配置

首次运行前将 `backend/.env.example` 复制为 `backend/.env`。关键变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SQLITE_DB_PATH` | `backend/data/polyprop.db` | 相对于项目根目录的路径 |
| `CSV_SOURCE_PATH` | `database/data1.csv` | 相对于项目根目录的路径 |
| `MODEL_ENABLED` | `false` | 控制 `/predict` 端点（目前仅为存根） |

## 架构说明

### 后端（`backend/app/`）

- **`main.py`** — FastAPI 应用工厂（`create_app`）。Settings 和 SQLite 连接工厂存储在 `app.state` 上，路由通过 `request.app.state` 访问。
- **`config.py`** — `Settings` 类。所有路径（DB、CSV）在构造时解析为相对于项目根目录的绝对路径。通过 `get_settings()` 提供 `@lru_cache` 单例。
- **`database.py`** — SQLite schema（两张表：`polymers` + `properties`）、连接上下文管理器、`rebuild_schema`（全量删除重建）。
- **`import_csv.py`** — CLI 脚本，读取 `data1.csv`，对聚合物去重，执行 RDKit 规范化，写入 SQLite。导入采用全量重建策略（幂等）。运行方式：`python -m app.import_csv`。
- **`models.py`** — Pydantic 响应模型（`PolymerResult`、`SmilesQueryResponse`、`PropertyGroups` 等）。
- **`routers/query.py`** — 三个端点：`POST /api/v1/query/smiles`（精确或相似度查询）、`GET /api/v1/polymer/{id}`、`POST /api/v1/structure/3d`。
- **`routers/predict.py`** — 存根，固定返回 501，端点为未来 ML 预测预留。
- **`services/matcher.py`** — 精确匹配：先用 RDKit 规范化查询 SMILES，优先匹配 `canonical_smiles`，回退到原始 `smiles`。
- **`services/similarity.py`** — 全表扫描相似度搜索。仅处理 `rdkit_parse_ok=1` 的记录，计算 Morgan 指纹 + Tanimoto，返回阈值以上的 Top-K。
- **`services/fingerprint.py`** — Morgan 指纹生成器（radius=2，2048 位）和 Tanimoto 相似度。模块级单例生成器。
- **`services/aggregator.py`** — 从数据库加载属性行，用 `CATEGORY_MAP` 分组为 `PropertyGroups`。
- **`services/smiles_utils.py`** — SMILES 规范化（canonicalization）工具函数。
- **`services/structure_3d.py`** — 用 RDKit 从 SMILES 生成 3D mol-block。

### 前端（`frontend/src/`）

- **`App.tsx`** — 根布局，包含三个区域：概览仪表板、主工作区（Ketcher 编辑器 + 3D 预览 + 查询面板）、结果面板。
- **`hooks/useKetcher.ts`** — 管理 Ketcher iframe 生命周期和 SMILES 状态，通过 `postMessage` 与 Ketcher 通信。
- **`hooks/useQuery.ts`** — 封装 `POST /api/v1/query/smiles` 的请求状态（loading、error、data）。
- **`services/api.ts`** — 所有 API 端点的 fetch 封装。
- **`components/KetcherEditor.tsx`** — 将 Ketcher 渲染为指向 `/ketcher/index.html` 的 `<iframe>`（静态文件位于 `public/ketcher/`）。SMILES 流向：Ketcher → hook → App 状态 → QueryPanel。
- **`components/StructurePreview3D.tsx`** — 调用 `POST /api/v1/structure/3d` 并用 3Dmol.js 渲染 mol-block。
- **`components/QueryPanel.tsx`** — 匹配模式选择器、阈值与 Top-K 输入、提交按钮。
- **`components/ResultsDisplay.tsx`** — 用 `PolymerCard` 渲染结果列表。
- **`components/PolymerCard.tsx`** / **`PropertyGroupCard.tsx`** / **`PropertyItem.tsx`** — 分层结果展示组件。

### 关键约束

- **相似度搜索是全表扫描** — 无预计算指纹列，性能随数据库规模线性下降。
- **Ketcher 以预构建静态文件嵌入** — 位于 `public/ketcher/`，不通过 npm 安装。
- **测试必须使用基于 `tmp_path` 的 SQLite 数据库** — `conftest.py` 中的 `test_app` fixture 为每个测试构建独立的临时数据库，禁止在测试中使用默认数据库文件。
- **非法 SMILES 始终返回 422** — 查询时输入校验失败返回 422；导入时该行记录 `rdkit_parse_ok=0`，在相似度搜索中跳过。
- **`MODEL_ENABLED=false`** 为默认值，predict 端点无论如何都返回 501 存根。
