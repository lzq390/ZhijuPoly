# 聚合物反向设计检索任务文档

**日期**: 2026-05-11
**对应规划**: `docs/superpowers/plans/2026-05-11-reverse-design-search-plan.md`
**当前前提**: PI 基础数据已导出，用户会先离线预测 Tg，再交给 PolyProp 构建独立本地 PI 候选数据库。运行时不连接 `screening_sys` / PostgreSQL。

---

## 0. 实施边界

### 0.1 本阶段目标

完成以下闭环：

```text
目标 Tg + Ketcher 聚合物结构
-> 本地 PI 候选库
-> Morgan fingerprint 相似度筛选
-> 随机抽样最多 200 条
-> Tg 接近度排序
-> 候选结果展示
-> mon1 / mon2 IUPAC 与本地知识库联动入口
```

### 0.2 明确不做

1. 不在运行时连接 `screening_sys` 或其他外部 PostgreSQL。
2. 不在 PolyProp 内预测 Tg。
3. 不在 API 请求中批量计算候选 fingerprint。
4. 不把 33M PI 数据直接混入现有 `polyprop.db` 主库。
5. 不强依赖在线 SMILES2IUPAC 服务。

---

## 0.3 当前进度

真实 Tg 文件生成前可以先做的基础能力已完成：

| 模块 | 状态 | 文件 |
|---|---|---|
| 配置 | 已完成 | `backend/app/config.py`, `backend/.env.example` |
| PI SQLite schema | 已完成 | `backend/app/pi_database.py` |
| fingerprint 序列化 | 已完成 | `backend/app/services/fingerprint.py` |
| PI CSV 导入 | 已完成 | `backend/app/import_pi_candidates.py` |
| 反向设计检索服务 | 已完成 | `backend/app/services/reverse_design.py` |
| 反向设计 API | 已完成 | `backend/app/routers/reverse_design.py`, `backend/app/main.py` |
| IUPAC 缓存占位 | 已完成 | `backend/app/services/smiles_to_iupac.py` |
| 后端测试 | 已完成 | `backend/tests/test_fingerprint_serialization.py`, `backend/tests/test_import_pi_candidates.py`, `backend/tests/test_reverse_design.py` |
| 前端类型/API/hook | 已完成 | `frontend/src/types/index.ts`, `frontend/src/services/api.ts`, `frontend/src/hooks/useReverseDesign.ts` |
| 前端独立子页面 | 已完成 | `frontend/src/App.tsx`, `frontend/src/components/ReverseDesignPage.tsx` |
| 前端结果展示 | 已完成 | `frontend/src/components/ReverseDesignResults.tsx` |

仍等待 Tg 文件后才能完成：

1. 全量 `pi_polymers_with_tg.csv` 导入。
2. 全量 33M 查询性能验证。
3. 真实 Tg 分布和字段质量检查。
4. 真实 SMILES2IUPAC 工具接入。

---

## 1. 数据合同确认

### 1.1 输入文件

等待用户提供离线预测后的文件，推荐路径：

```text
D:\database\PI\pi_polymers_with_tg.csv
/mnt/d/database/PI/pi_polymers_with_tg.csv
```

必需字段：

```text
id, mon1, mon2, polym, tg_celsius
```

推荐保留字段：

```text
dielectric_const_dc, static_dielectric_const, dipole_debye,
electrophilicity_index, homo_lumo_gap_ev, hardness,
mulliken_electronegativity, redox_window_v, linear_expansion,
refractive_index, created_at
```

### 1.2 验收标准

1. `tg_celsius` 字段存在，且单位已归一为 `°C`。
2. `id` 可作为 PI 候选主键。
3. `polym` 是用于相似度匹配的聚合物 SMILES。
4. `mon1` / `mon2` 是后续转 IUPAC 和知识库检索的单体 SMILES。
5. 抽样检查至少 1000 行，确认必需字段缺失率、Tg 数值范围、RDKit 可解析比例。

---

## 2. 本地 PI 候选库

### 2.1 后端配置

新增配置项：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PI_REVERSE_DB_PATH` | `backend/data/pi_reverse_design.db` | 反向设计候选 SQLite |
| `PI_REVERSE_CSV_PATH` | 空 | 导入脚本默认输入文件 |

任务：

1. 在 `backend/app/config.py` 增加配置解析。
2. 复用现有 Windows 路径到 WSL 路径转换逻辑。
3. 在 `backend/.env.example` 补充变量说明。

### 2.2 Schema

建议新增独立模块：

```text
backend/app/pi_database.py
```

核心表：

```sql
CREATE TABLE pi_candidates (
  pi_id INTEGER PRIMARY KEY,
  mon1 TEXT NOT NULL,
  mon2 TEXT NOT NULL,
  polym TEXT NOT NULL,
  canonical_polym TEXT,
  rdkit_parse_ok INTEGER NOT NULL DEFAULT 0,
  tg_celsius REAL NOT NULL,
  dielectric_const_dc REAL,
  static_dielectric_const REAL,
  dipole_debye REAL,
  electrophilicity_index REAL,
  homo_lumo_gap_ev REAL,
  hardness REAL,
  mulliken_electronegativity REAL,
  redox_window_v REAL,
  linear_expansion REAL,
  refractive_index REAL,
  morgan_fp BLOB,
  created_at TEXT
);
```

索引：

```sql
CREATE INDEX idx_pi_candidates_tg ON pi_candidates(tg_celsius);
CREATE INDEX idx_pi_candidates_parse_ok ON pi_candidates(rdkit_parse_ok);
```

可选缓存表：

```sql
CREATE TABLE smiles_iupac_cache (
  smiles TEXT PRIMARY KEY,
  iupac_name TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

验收标准：

1. PI 数据库与 `backend/data/polyprop.db` 分离。
2. schema 创建可重复执行。
3. 测试使用 `tmp_path`，不写默认数据库。

---

## 3. Fingerprint 序列化

### 3.1 修改范围

扩展：

```text
backend/app/services/fingerprint.py
```

新增能力：

```text
fingerprint_to_bytes(fp) -> bytes
fingerprint_from_bytes(data) -> ExplicitBitVect
```

### 3.2 要求

1. 继续使用 Morgan `radius=2`、`fpSize=2048`。
2. BLOB 格式必须稳定。
3. 导入和查询共用同一组 helper。
4. 增加 round-trip 测试：序列化前后 Tanimoto 自相似度为 `1.0`。

---

## 4. PI CSV 导入脚本

### 4.1 新增脚本

建议新增：

```text
backend/app/import_pi_candidates.py
```

运行方式：

```bash
cd backend
python -m app.import_pi_candidates --csv /mnt/d/database/PI/pi_polymers_with_tg.csv --rebuild
```

### 4.2 导入流程

1. 流式读取 CSV，避免一次性加载 33M 行。
2. 校验必需字段。
3. 将 Tg 转为 `float`，写入 `tg_celsius`。
4. 对 `polym` 做 RDKit 解析和 canonicalize。
5. 解析成功时生成 Morgan fingerprint 并序列化为 BLOB。
6. 解析失败时仍可记录行，但 `rdkit_parse_ok = 0`、`morgan_fp = NULL`。
7. 分批写入 SQLite。
8. 输出导入统计和耗时。

### 4.3 验收标准

1. 支持 `--rebuild` 幂等重建。
2. 支持 `--limit` 先导入小样本测试。
3. 支持进度日志，例如每 100000 行输出一次。
4. 输出总行数、导入行数、缺失 Tg 行数、RDKit 成功数、RDKit 失败数。
5. 小样本导入测试通过。

---

## 5. 后端检索服务

### 5.1 新增服务

建议新增：

```text
backend/app/services/reverse_design.py
```

核心函数：

```text
search_reverse_design_by_tg(
    connection,
    smiles,
    target_tg,
    similarity_threshold=0.7,
    candidate_sample_size=200,
    top_k=50,
    random_seed=None,
)
```

### 5.2 查询语义

1. 校验输入聚合物 SMILES。
2. 生成查询 Morgan fingerprint。
3. 按 chunk 从 `pi_candidates` 读取 `rdkit_parse_ok = 1` 且 `morgan_fp IS NOT NULL` 的候选。
4. 反序列化候选 fingerprint。
5. 计算 Tanimoto similarity。
6. 保留 `similarity >= similarity_threshold` 的候选。
7. 从达标候选中随机抽样最多 `candidate_sample_size`。
   - 实现时优先用 reservoir sampling，边扫描边维护样本和 `candidate_pool_size`，避免达标候选过多时占用大量内存。
8. 按以下顺序排序：

```text
tg_difference ASC
similarity_score DESC
pi_id ASC
```

9. 返回前 `top_k`。

### 5.3 验收标准

1. 非法 SMILES 返回 422。
2. 阈值、抽样数、结果数越界返回 422。
3. `random_seed` 相同时结果可复现。
4. 候选少于 200 时返回全部达标候选。
5. 返回包含 `candidate_pool_size`、`sampled_candidate_count`、`query_time_ms`。
6. 达标候选很多时内存使用不随候选池线性增长。

---

## 6. 后端 API

### 6.1 模型

扩展：

```text
backend/app/models.py
```

新增模型：

```text
ReverseDesignTgRequest
ReverseDesignTgCandidate
ReverseDesignTgResponse
ReverseDesignKnowledgeRequest
ReverseDesignKnowledgeResponse
```

### 6.2 Router

新增：

```text
backend/app/routers/reverse_design.py
```

端点：

```http
POST /api/v1/reverse-design/tg
POST /api/v1/reverse-design/knowledge
```

并在：

```text
backend/app/main.py
```

注册 router。

### 6.3 验收标准

1. API 响应字段与规划文档一致。
2. 数据库未初始化时返回可读错误。
3. `knowledge` 端点在 IUPAC 工具不可用时不报 500，返回 `null` 状态。
4. 后端测试覆盖成功路径、非法输入、空结果、随机种子。

---

## 7. IUPAC 与知识库联动

### 7.1 SMILES 转 IUPAC

新增可插拔服务：

```text
backend/app/services/smiles_to_iupac.py
```

优先级：

1. 查 `smiles_iupac_cache`。
2. 调用本地或内网 SMILES2IUPAC 工具。
3. 工具不可用时返回 `None`。

### 7.2 知识库搜索

现有搜索主要查 `abstract`。建议扩展：

```text
abstract
polymer_iupac
formulation
title_en
title_zh
claim
analysis
```

验收标准：

1. 候选详情能展示 `mon1` / `mon2`。
2. IUPAC 转换失败时前端显示“未转换”状态。
3. 知识库检索无结果时返回空列表，不影响候选结果展示。

---

## 8. 前端 API 与状态

当前状态：

```text
frontend/src/types/index.ts: WorkspaceMode = "query" | "predict"
frontend/src/types/index.ts: ResultsTab = "query" | "predict"
独立页面路由: /reverse-design
```

反向设计已改为独立子页面，不再挂到 Explorer 的 `QueryPanel` 或 `ResultsDisplay` tab 中。

### 8.1 类型

扩展：

```text
frontend/src/types/index.ts
frontend/src/services/api.ts
```

新增类型与请求函数：

```text
ReverseDesignTgRequest
ReverseDesignTgCandidate
ReverseDesignTgResponse
ReverseDesignKnowledgeResponse
searchReverseDesignByTg()
fetchReverseDesignKnowledge()
```

### 8.2 Hook

新增：

```text
frontend/src/hooks/useReverseDesign.ts
```

状态：

```text
loading
error
data
selectedCandidate
knowledgeLoading
knowledgeError
knowledgeData
```

验收标准：

1. 请求失败能显示后端错误。
2. 新查询会清空旧知识库详情。
3. 空结果状态清晰。

---

## 9. 前端界面

### 9.1 独立子页面

新增：

```text
frontend/src/components/ReverseDesignPage.tsx
```

已完成：

1. 首页新增 `Tg Reverse Design` 模块入口。
2. 新增 `/reverse-design` 路由。
3. Explorer 保持 `query` / `predict` 两模式，不集成反向设计。

页面字段：

| 字段 | 默认值 |
|---|---:|
| Target Tg | 空 |
| Similarity Threshold | `0.7` |
| Random Candidates | `200` |
| Result Limit | `50` |

### 9.2 结果列表

建议新增：

```text
frontend/src/components/ReverseDesignResults.tsx
frontend/src/components/ReverseDesignCandidateCard.tsx
```

展示字段：

```text
rank
pi_id
tg_value
tg_difference
similarity_score
polymer_smiles
monomer_a_smiles
monomer_b_smiles
knowledge action
```

验收标准：

1. 输入聚合物结构仍来自 Ketcher 当前 SMILES。
2. Target Tg 必填且按数字校验。
3. 结果按后端 rank 展示。
4. 点击候选能请求知识库联动。
5. 移动端和桌面端文本不重叠。

---

## 10. 测试与验证

### 10.1 后端测试

建议新增：

```text
backend/tests/test_import_pi_candidates.py
backend/tests/test_reverse_design.py
backend/tests/test_fingerprint_serialization.py
```

覆盖：

1. PI CSV 小样本导入。
2. RDKit 解析失败行跳过相似度检索。
3. fingerprint 序列化 round-trip。
4. 相似度阈值过滤。
5. 随机抽样与 `random_seed`。
6. Tg 排序。
7. API 422 / 空结果 / 成功结果。
8. 达标候选多于 `candidate_sample_size` 时只保留样本并正确统计候选池。

### 10.2 前端验证

运行：

```bash
cd frontend
npm run build
```

手工验证：

1. Ketcher 画结构后可以发起反向设计查询。
2. 参数越界时按钮禁用或显示错误。
3. 候选结果能展示 Tg、similarity、monomers。
4. IUPAC 不可用时 UI 不崩溃。

### 10.3 性能验证

分三档验证：

1. `--limit 10000` 小样本。
2. `--limit 1000000` 中样本。
3. 全量 33M。

记录：

```text
导入耗时
数据库大小
单次查询耗时
内存峰值
候选池大小
```

若全量单次查询不可接受，进入第二阶段引入 fingerprint index。

---

## 11. 推荐执行顺序

1. 确认 `pi_polymers_with_tg.csv` 字段和 Tg 单位。
2. 实现配置与 PI SQLite schema。
3. 实现 fingerprint 序列化 helper 和测试。
4. 实现 PI CSV 小样本导入。
5. 实现后端检索服务。
6. 实现后端 API 与测试。
7. 接入真实 SMILES2IUPAC 工具。
8. 做 10k、1M、全量三级性能验证。

---

## 12. 当前 Blocker

1. 需要用户提供带 Tg 的 PI 文件，推荐字段名 `tg_celsius`。
2. 需要确认 Tg 预测输出单位是否为 `°C`。
3. 需要确认 SMILES2IUPAC 工具形式：本地命令、HTTP 服务、Python 包或暂不接入。
