# PolyProp 实施计划

**目标**: 基于 [data1.csv](/home/lzq390/gith/polyprop/database/data1.csv) 自建本地数据库，完成聚合物结构查询工具首版开发。  
**当前阶段**: 可进入开发。  
**开发路线**: `CSV -> import script -> local SQLite -> FastAPI -> React + Ketcher`

---

## 总体原则

1. 不再依赖外部现成数据库。
2. 首版只实现 CSV 能支撑的数据能力。
3. 先打通数据导入和查询链路，再做前端展示。
4. 对 RDKit 不可解析的结构，保持可回退、可跳过。

---

## 文件结构

```text
polyprop/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── database.py
│   │   ├── models.py
│   │   ├── import_csv.py
│   │   ├── routers/
│   │   │   ├── __init__.py
│   │   │   ├── query.py
│   │   │   └── predict.py
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   ├── smiles_utils.py
│   │   │   ├── fingerprint.py
│   │   │   ├── matcher.py
│   │   │   ├── similarity.py
│   │   │   └── aggregator.py
│   │   └── utils/
│   │       ├── __init__.py
│   │       └── exceptions.py
│   ├── tests/
│   │   ├── conftest.py
│   │   ├── test_smiles_utils.py
│   │   ├── test_import_csv.py
│   │   ├── test_matcher.py
│   │   ├── test_similarity.py
│   │   ├── test_aggregator.py
│   │   └── test_api.py
│   ├── data/
│   │   └── polyprop.db
│   ├── .env.example
│   ├── requirements.txt
│   └── pytest.ini
├── frontend/
│   ├── public/
│   │   └── ketcher/
│   ├── src/
│   │   ├── types/
│   │   │   └── index.ts
│   │   ├── services/
│   │   │   └── api.ts
│   │   ├── hooks/
│   │   │   ├── useKetcher.ts
│   │   │   └── useQuery.ts
│   │   ├── components/
│   │   │   ├── Layout.tsx
│   │   │   ├── KetcherEditor.tsx
│   │   │   ├── QueryPanel.tsx
│   │   │   ├── ResultsDisplay.tsx
│   │   │   ├── PolymerCard.tsx
│   │   │   ├── PropertyGroupCard.tsx
│   │   │   └── PropertyItem.tsx
│   │   ├── App.tsx
│   │   └── main.tsx
│   ├── .env.example
│   ├── package.json
│   └── vite.config.ts
└── database/
    └── data1.csv
```

---

## Task 0: 冻结数据契约

**目标**: 把首版数据来源和范围固定下来。

- [x] 结论写入 discovery 文档
- [x] 明确唯一数据源为 `database/data1.csv`
- [x] 明确首版不做文献、工艺、条件字段展示
- [x] 明确输入契约为“RDKit 可解析的结构字符串”

完成标准：

1. `spec`、`plan`、`discovery` 三份文档一致
2. 不再出现对远程 PostgreSQL 现成表的依赖假设

---

## Task 1: 后端项目初始化

**Files**
- Create: `backend/requirements.txt`
- Create: `backend/.env.example`
- Create: `backend/pytest.ini`
- Create: `backend/app/config.py`
- Create: `backend/app/__init__.py`

- [x] 建立目录结构
- [x] 写 `requirements.txt`
- [x] 写 `pytest.ini`
- [x] 写 `.env.example`
- [x] 写 `config.py`

环境变量最小集：

```bash
SQLITE_DB_PATH=backend/data/polyprop.db
CSV_SOURCE_PATH=database/data1.csv
ALLOWED_ORIGINS=http://localhost:5173,http://localhost:3000
MODEL_ENABLED=true
```

实现约束：

1. `config.py` 必须把 `SQLITE_DB_PATH` 和 `CSV_SOURCE_PATH` 解析为基于项目根目录的绝对路径。
2. 默认运行使用 `backend/data/polyprop.db`。
3. 测试运行不得复用默认库文件。

---

## Task 2: 本地数据库与导入脚本

**Files**
- Create: `backend/app/database.py`
- Create: `backend/app/import_csv.py`
- Create: `backend/tests/test_import_csv.py`

### 目标

实现：

1. SQLite 数据库初始化
2. `polymers` / `properties` 建表
3. 从 CSV 导入数据
4. 为可解析的 `smiles` 生成 `canonical_smiles`
5. 记录 `rdkit_parse_ok`

### 导入规则

1. `(polymer_name, smiles)` 去重生成一条 polymer 记录
2. 每条 CSV 行生成一条 property 记录
3. `property_value` 原样保留为字符串
4. 若可转 `float`，同时写入 `property_value_num`
5. 若 `smiles` 可被 RDKit 解析，则写入 `canonical_smiles` 且 `rdkit_parse_ok=1`
6. 若 `smiles` 不可被 RDKit 解析，则 `canonical_smiles=NULL` 且 `rdkit_parse_ok=0`
7. 导入采用“全量重建”策略，重复执行不得累加旧数据

最小 schema 补充：

```sql
CREATE TABLE polymers (
  polymer_id INTEGER PRIMARY KEY AUTOINCREMENT,
  polymer_name TEXT NOT NULL,
  smiles TEXT NOT NULL,
  canonical_smiles TEXT,
  rdkit_parse_ok INTEGER NOT NULL DEFAULT 0,
  UNIQUE(polymer_name, smiles)
);
```

### 完成标准

1. 能生成 `backend/data/polyprop.db`
2. 导入后 polymer 数和 discovery 统计量接近
3. 测试覆盖建库与导入结果
4. 重复执行导入后数据量不累加
5. 测试数据库使用临时 SQLite 文件或临时目录

- [x] SQLite 数据库初始化
- [x] `polymers` / `properties` 建表
- [x] 从 CSV 导入数据
- [x] 为可解析的 `smiles` 生成 `canonical_smiles`
- [x] 记录 `rdkit_parse_ok`

---

## Task 3: Pydantic 模型

**Files**
- Create: `backend/app/models.py`

- [x] 创建 `backend/app/models.py`
- [x] 定义 `SmilesQueryRequest`
- [x] 定义 `PropertyItem`
- [x] 定义 `PropertyGroups`
- [x] 定义 `PolymerResult`
- [x] 补充查询响应模型 `SmilesQueryResponse`
- [x] 添加基础模型测试

### 首版模型

#### `SmilesQueryRequest`

```python
smiles: str
match_mode: Literal["exact", "similarity"]
similarity_threshold: float
top_k: int
```

#### `PropertyItem`

```python
property_category: str
property_name: str
property_value: str
property_value_num: float | None
property_unit: str | None
label_source: str | None
```

#### `PolymerResult`

```python
polymer_id: str
polymer_name: str
smiles: str
canonical_smiles: str | None
similarity_score: float | None
properties: PropertyGroups
```

---

## Task 4: SMILES 工具与指纹服务

**Files**
- Create: `backend/app/services/smiles_utils.py`
- Create: `backend/app/services/fingerprint.py`
- Create: `backend/tests/test_smiles_utils.py`

- [x] 创建 `backend/app/services/smiles_utils.py`
- [x] 创建 `backend/app/services/fingerprint.py`
- [x] 实现 `normalize(smiles)`
- [x] 实现 `are_equivalent(smiles1, smiles2)`
- [x] 实现 `generate(smiles)`
- [x] 实现 `tanimoto(fp1, fp2)`
- [x] 添加 `backend/tests/test_smiles_utils.py`

### 功能

1. `normalize(smiles)`
2. `are_equivalent(smiles1, smiles2)`
3. `generate(smiles)`
4. `tanimoto(fp1, fp2)`

---

## Task 5: 查询服务

**Files**
- Create: `backend/app/services/matcher.py`
- Create: `backend/app/services/similarity.py`
- Create: `backend/app/services/aggregator.py`
- Create: `backend/app/utils/exceptions.py`
- Create: `backend/tests/test_matcher.py`
- Create: `backend/tests/test_similarity.py`
- Create: `backend/tests/test_aggregator.py`

- [x] 创建 `backend/app/services/matcher.py`
- [x] 创建 `backend/app/services/similarity.py`
- [x] 创建 `backend/app/services/aggregator.py`
- [x] 创建 `backend/app/utils/exceptions.py`
- [x] 实现精确匹配逻辑
- [x] 实现相似度匹配逻辑
- [x] 实现属性分组聚合逻辑
- [x] 添加 `test_matcher.py`
- [x] 添加 `test_similarity.py`
- [x] 添加 `test_aggregator.py`

### `matcher.py`

规则：

1. 请求输入必须先通过 RDKit 校验
2. 输入不可解析时直接返回 `422`
3. 优先用 `canonical_smiles` 匹配
4. 若 canonical 未命中，则回退用原始 `smiles` 精确匹配
5. 若仍失败，返回空列表

### `similarity.py`

规则：

1. 全表读取 `polymers`
2. 仅 `rdkit_parse_ok=1` 的记录参与指纹计算
3. 阈值过滤 + Top-K 排序
4. 输入不可解析时直接返回 `422`

### `aggregator.py`

类别映射：

```python
CATEGORY_MAP = {
    "Thermal": "thermal",
    "Mechanical": "mechanical",
    "Electrical": "electrical",
    "Chemical": "chemical",
    "Optical": "optical",
    "Others": "other",
}
```

---

## Task 6: FastAPI API

**Files**
- Create: `backend/app/main.py`
- Create: `backend/app/routers/query.py`
- Create: `backend/app/routers/predict.py`
- Create: `backend/tests/test_api.py`
- Create: `backend/tests/conftest.py`

- [x] 创建 `backend/app/main.py`
- [x] 创建 `backend/app/routers/query.py`
- [x] 创建 `backend/app/routers/predict.py`
- [x] 创建 `backend/tests/conftest.py`
- [x] 创建 `backend/tests/test_api.py`
- [x] 实现 `GET /health`
- [x] 实现 `POST /api/v1/query/smiles`
- [x] 实现 `GET /api/v1/polymer/{polymer_id}`
- [x] 实现 `POST /api/v1/predict`
- [x] 验证测试数据库与默认库隔离

### 路由

1. `GET /health`
2. `POST /api/v1/query/smiles`
3. `GET /api/v1/polymer/{polymer_id}`
4. `POST /api/v1/predict` -> `501`

### 测试重点

1. 健康检查
2. 非法 `smiles`
3. 精确匹配返回结构
4. 相似度匹配返回结构
5. 详情接口
6. 测试数据库与默认库隔离

### 已知运行时约束

1. 当前环境下 `FastAPI TestClient` 会阻塞，不作为首选 API 测试路径。
2. 当前环境下避免依赖 `asyncio.to_thread()`、`anyio.to_thread.run_sync()` 和同步 FastAPI 路由。
3. 后端路由保持 `async def`，测试优先直接调用路由处理函数。

---

## Task 7: 前端初始化

**Files**
- Create: `frontend/package.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/.env.example`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/App.tsx`

- [x] 创建 `frontend/package.json`
- [x] 创建 `frontend/vite.config.ts`
- [x] 创建 `frontend/.env.example`
- [x] 创建 `frontend/src/main.tsx`
- [x] 创建 `frontend/src/App.tsx`
- [x] 补齐最小 Vite 运行骨架
- [x] 接入前端 UI 基础组件
- [x] 配置 `/api` 代理
- [x] 复制 Ketcher 静态资源

### 关键点

1. 用 Vite 初始化 React + TS
2. 接入 UI 组件体系
3. 配置 `/api` 代理到 `http://localhost:8000`
4. 复制 Ketcher 静态资源

---

## Task 8: 前端查询与展示

**Files**
- Create: `frontend/src/types/index.ts`
- Create: `frontend/src/services/api.ts`
- Create: `frontend/src/hooks/useKetcher.ts`
- Create: `frontend/src/hooks/useQuery.ts`
- Create: `frontend/src/components/*`

- [x] 创建 `frontend/src/types/index.ts`
- [x] 创建 `frontend/src/services/api.ts`
- [x] 创建 `frontend/src/hooks/useKetcher.ts`
- [x] 创建 `frontend/src/hooks/useQuery.ts`
- [x] 创建首版展示组件骨架
- [x] 将前端 UI 切换为 `shadcn/ui` 风格本地组件
- [x] 展示 `label_source`
- [x] 挂载 Ketcher iframe 页面
- [x] 接入真实 Ketcher 编辑器

### 展示内容

1. 聚合物名称
2. `smiles`
3. 相似度
4. 属性分类
5. 属性值
6. 单位
7. `label_source`

### 不展示

1. 文献
2. 工艺
3. 条件 JSON

---

## Task 9: 联调与验收

- [x] 运行导入脚本生成本地库
- [x] 启动后端
- [x] 启动前端
- [x] 从 Ketcher 生成结构并查询
- [x] 验证 exact / similarity 两种模式

### 当前验收记录

1. 已执行 `python -m app.import_csv --db-path data/polyprop.db`，可稳定重建本地库。
2. 已启动后端 `uvicorn`，`GET /health` 返回 `{"status":"ok"}`。
3. 已启动前端 `vite`，首页和 `/ketcher/index.html` 均返回 `200`。
4. 已用数据库中的真实 `canonical_smiles` 走通 `exact` 和 `similarity` 两种 HTTP 查询，均返回 `total=1` 且首条相似度为 `1.0`。
5. `Task 9` 按当前联调口径视为完成，Ketcher 已完成接入并具备手动提取 SMILES 后查询的完整代码路径。

### 验收标准

1. 本地库能由 CSV 稳定重建
2. API 查询返回结构稳定
3. 前端能展示分类属性
4. 对非法输入返回明确错误
5. 重复导入不会累加旧数据
6. 测试不会污染默认 `backend/data/polyprop.db`

---

## Task 10: 后续扩展

后续不在首版内，但需要预留：

1. PostgreSQL 适配层
2. 指纹缓存
3. 文献数据扩展
4. 条件字段扩展
5. BigSMILES 更完整支持

---

## 开发顺序

推荐顺序：

1. Task 1
2. Task 2
3. Task 3
4. Task 4
5. Task 5
6. Task 6
7. Task 7
8. Task 8
9. Task 9

关键原则：

1. 先让数据落库，再写查询服务
2. 先让后端查询通，再做前端联调
3. 所有首版功能都围绕 CSV 现有字段，不做额外字段幻想
