# 聚合物反向设计检索功能规划文档

**日期**: 2026-05-11
**当前状态**: 基础功能已先行实现，等待 PI Tg 预测结果文件做真实导入与全量验证
**目标**: 在 PolyProp 中新增基于目标 Tg 和用户绘制聚合物结构的反向设计检索能力，打通 `Ketcher 聚合物结构输入 -> 本地 PI 候选数据库 -> Morgan fingerprint 相似度筛选 -> Tg 接近度排序 -> 候选结果列表 -> 单体 IUPAC / 本地知识库检索` 的完整链路。

---

## 1. 结论更新

### 1.1 最新决策

1. 用户在画板中绘制的是**聚合物结构**，不是单体结构。
2. 相似度算法使用 Morgan fingerprint，不强制贴合流程图中的 MACCS keys。
3. 不在 PolyProp 运行时连接 `screening_sys` PostgreSQL。
4. 已从本地 `postgres-screening.public.pi_polymers` 导出 PI 数据 CSV：
   - Windows: `D:\database\PI\pi_polymers_no_fp.csv`
   - WSL: `/mnt/d/database/PI/pi_polymers_no_fp.csv`
5. 该 CSV 不包含 Tg，也不包含 `polym_morgan_fp` 二进制指纹列。
6. 用户会先基于该 CSV 预测 Tg，然后产出一份带 Tg 的 PI 数据文件。
7. PolyProp 后续基于“带 Tg 的 PI 数据文件”构建自己的本地 PI 候选数据库。

### 1.2 当前已先行实现

在真实 Tg 文件生成前，以下不依赖全量预测结果的部分已完成：

1. `PI_REVERSE_DB_PATH` / `PI_REVERSE_CSV_PATH` 配置。
2. 独立 PI SQLite schema：`pi_candidates` 与 `smiles_iupac_cache`。
3. Morgan fingerprint BLOB 序列化 / 反序列化 helper。
4. PI CSV 导入脚本，支持 `--limit`、批量写入、进度日志和重建导入。
5. `POST /api/v1/reverse-design/tg` 检索 API。
6. `POST /api/v1/reverse-design/knowledge` 知识库联动 API 骨架。
7. 后端测试覆盖小样本导入、Tg 排序、随机抽样、API 成功路径和错误路径。
8. 前端独立子页面 `/reverse-design`，不集成到 Explorer。
9. 前端 Tg Reverse Design 参数表单、候选卡片和知识库按钮。

### 1.3 文档 Review 发现的问题

原规划中以下内容需要修正，已在本版文档中调整：

1. **跨库连接不再采用**
   原方案计划新增 `screening_sys` PostgreSQL repository。现在改为本地导入，不需要 `SCREENING_SYS_DB_*` 配置，也不需要运行时连接外部库。

2. **PI Tg 不再是运行时 blocker**
   Tg 由用户先离线预测，导入 PolyProp PI 候选库时必须已有 `tg_celsius` 或等价字段。

3. **指纹不能直接读取现成字段**
   导出的 `pi_polymers_no_fp.csv` 不含 `polym_morgan_fp`。因此 Morgan fingerprint 应在导入 PI 候选库时预计算并存储。

4. **33M 行规模需要提前设计性能边界**
   每次请求现场计算全部候选指纹不可接受。导入阶段必须预计算，查询阶段只读取预计算指纹。

5. **候选库应与现有 PolyProp 主库解耦**
   当前 `backend/data/polyprop.db` 仍服务于原结构检索和知识库。PI 反向设计数据建议使用独立 SQLite 文件，避免污染现有 schema 和导入流程。

---

## 2. 功能目标

### 2.1 主流程

1. 用户输入目标 Tg，单位为 `°C`。
2. 用户在 Ketcher 中绘制目标聚合物结构。
3. 前端提交目标 Tg、聚合物 SMILES、相似度阈值、候选抽样数和结果数。
4. 后端生成输入结构的 Morgan fingerprint。
5. 后端从本地 PI 候选数据库中筛选 `similarity >= 0.7` 的候选。
6. 后端从高相似候选池中随机抽取最多 200 条。
7. 后端按 `abs(candidate_tg - target_tg)` 升序排序。
8. 前端展示候选列表。
9. 用户点击候选后，系统读取候选的 `mon1` / `mon2`，转换为 IUPAC 名称，并检索本地知识库。

### 2.2 第一阶段纳入范围

1. 新增 PI 候选数据库 schema。
2. 新增 PI 数据导入脚本。
3. 导入时预计算 `polym` 的 Morgan fingerprint。
4. 新增反向设计检索 API。
5. 新增前端反向设计面板和结果列表。
6. 新增候选详情中的 `mon1`、`mon2`、`polym` 展示。
7. 保留 IUPAC/知识库联动接口和 UI 入口，但允许转换器先返回空。

### 2.3 第一阶段不纳入范围

1. 不在请求时连接 `screening_sys` PostgreSQL。
2. 不在请求时批量计算候选 fingerprint。
3. 不做聚合物生成，只做本地库检索。
4. 不训练或调用 Tg 预测模型；Tg 数据由离线预测文件提供。
5. 不强依赖在线 SMILES2IUPAC 服务。

---

## 3. 数据输入与本地库

### 3.1 输入文件

已导出的基础 PI CSV：

```text
D:\database\PI\pi_polymers_no_fp.csv
/mnt/d/database/PI/pi_polymers_no_fp.csv
```

当前字段：

```text
id, mon1, mon2, polym,
dielectric_const_dc, static_dielectric_const, dipole_debye,
electrophilicity_index, homo_lumo_gap_ev, hardness,
mulliken_electronegativity, redox_window_v, linear_expansion,
refractive_index, created_at
```

用户预测 Tg 后，建议产出新文件：

```text
D:\database\PI\pi_polymers_with_tg.csv
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

### 3.2 本地数据库建议

建议新增独立 SQLite 数据库：

```text
backend/data/pi_reverse_design.db
```

新增配置：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PI_REVERSE_DB_PATH` | `backend/data/pi_reverse_design.db` | PI 反向设计候选数据库 |
| `PI_REVERSE_CSV_PATH` | 空 | 可选，导入脚本默认输入 |

### 3.3 Schema

建议表：

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

### 3.4 导入规则

1. `tg_celsius` 必须存在且可转为数字。
2. `polym` 使用 RDKit 解析并 canonicalize。
3. 解析失败的行可以导入，但 `rdkit_parse_ok = 0`，相似度检索时跳过。
4. `morgan_fp` 在导入阶段预计算并存为 BLOB。
5. 重复 `id` 采用覆盖或拒绝策略，推荐导入前重建表，保证幂等。
6. 导入统计必须输出：
   - 总行数
   - 成功导入行数
   - RDKit 解析成功数
   - RDKit 解析失败数
   - 缺失 Tg 行数

---

## 4. 后端 API 设计

### 4.1 Tg 反向设计检索

```http
POST /api/v1/reverse-design/tg
```

Request:

```json
{
  "target_tg": 120,
  "smiles": "*CC(c1ccccc1)*",
  "similarity_threshold": 0.7,
  "candidate_sample_size": 200,
  "top_k": 50,
  "random_seed": null
}
```

Response:

```json
{
  "target_tg": 120,
  "query_time_ms": 38.4,
  "candidate_pool_size": 384,
  "sampled_candidate_count": 200,
  "total": 50,
  "data_source": "pi_reverse_design",
  "results": [
    {
      "rank": 1,
      "pi_id": 123,
      "polymer_smiles": "*CC(c1ccccc1)*",
      "canonical_polym": "*CC(c1ccccc1)*",
      "monomer_a_smiles": "O=C1OC(=O)c2ccc(*)c(*)c21",
      "monomer_b_smiles": "Nc1ccc(*)cc1",
      "tg_value": 123,
      "tg_unit": "°C",
      "tg_difference": 3,
      "similarity_score": 0.86,
      "structure_svg": "<svg>...</svg>",
      "knowledge_available": true
    }
  ]
}
```

### 4.2 候选知识库联动

```http
POST /api/v1/reverse-design/knowledge
```

Request:

```json
{
  "pi_id": 123,
  "top_k": 10
}
```

Response:

```json
{
  "pi_id": 123,
  "monomer_a_smiles": "COC(=O)Cc1ccc(cc1)O",
  "monomer_b_smiles": "Oc1ccccc1",
  "monomer_a_iupac": null,
  "monomer_b_iupac": null,
  "knowledge_query": null,
  "knowledge": null
}
```

第一阶段允许 IUPAC 转换为空，但 API 契约要稳定，便于后续接入真实转换器。

---

## 5. 相似度与排序策略

### 5.1 指纹

沿用现有 Morgan fingerprint：

```text
radius = 2
fpSize = 2048
```

复用：

```text
backend/app/services/fingerprint.py
```

导入阶段需要固定 `morgan_fp` 的序列化格式，并提供 round-trip 测试。推荐在 `fingerprint.py` 中新增显式 helper，例如：

```text
fingerprint_to_bytes(fp) -> bytes
fingerprint_from_bytes(data) -> ExplicitBitVect
```

### 5.2 查询流程

1. 校验输入 SMILES。
2. 生成输入 Morgan fingerprint。
3. 从 `pi_candidates` 读取 `rdkit_parse_ok = 1` 且 `morgan_fp IS NOT NULL` 的候选。
4. 计算 Tanimoto similarity。
5. 保留 `similarity >= threshold` 的候选。
6. 从候选池随机抽样最多 `candidate_sample_size`。
7. 排序：

```text
tg_difference ASC
similarity_score DESC
pi_id ASC
```

8. 返回前 `top_k`。

这里的语义是**先从相似度达标候选中随机抽样，再对抽样结果按 Tg 接近度排序**。如果传入 `random_seed`，同一请求参数应返回可复现的抽样结果。

### 5.3 性能注意

33M 行候选库很大，第一版可以先做正确性闭环，但需要明确风险：

1. SQLite 全表扫描即使使用预计算 BLOB，也可能较慢。
2. 查询服务应按 chunk 流式读取，避免一次性加载所有候选。
3. 如果相似度达标候选很多，随机抽样应使用 reservoir sampling 或等价流式算法，避免把完整候选池留在内存中。
4. 如果交互延迟不可接受，第二阶段应引入专用 fingerprint index，例如 FPSim2/HDF5 或等价位向量索引。
5. 不应在 API 请求中现场计算候选 fingerprint。

---

## 6. 前端设计

### 6.1 工作区入口

新增独立子页面：

```text
/reverse-design
```

反向设计不集成到 Explorer 的 `query` / `predict` 模式中。首页新增 `Tg Reverse Design` 模块入口，Explorer 保持原有结构相似度与属性预测工作区。

### 6.2 控制面板

字段：

| 控件 | 默认值 | 说明 |
|---|---:|---|
| Target Tg | 空 | 必填，单位 °C |
| Similarity Threshold | `0.7` | 范围 `0 - 1` |
| Random Candidates | `200` | 范围 `1 - 200` |
| Result Limit | `50` | 范围 `1 - 100` |

禁用条件：

1. SMILES 为空。
2. Target Tg 无效。
3. 参数越界。
4. 请求进行中。

### 6.3 结果列表

字段：

| 字段 | 说明 |
|---|---|
| Rank | Tg 接近度排序 |
| PI ID | `pi_id` |
| Tg | 候选预测 Tg |
| Tg Difference | `abs(candidate_tg - target_tg)` |
| Similarity | Morgan fingerprint 相似度 |
| Polymer | `polym` 结构图和 SMILES |
| Monomers | `mon1` / `mon2` |
| Knowledge | 触发 IUPAC / 知识库联动 |

---

## 7. 本地知识库联动

### 7.1 IUPAC 转换

建议新增可插拔转换器：

```text
backend/app/services/smiles_to_iupac.py
```

优先级：

1. 查 `smiles_iupac_cache`。
2. 调用本地或内网 SMILES2IUPAC 服务。
3. 不可用时返回 `None`。

### 7.2 知识库检索增强

现有知识库搜索主要查 `abstract`。建议扩展：

```text
abstract
polymer_iupac
formulation
title_en
title_zh
claim
analysis
```

排序优先级：

1. `polymer_iupac`
2. `formulation`
3. `title`
4. `abstract`
5. `claim` / `analysis`

---

## 8. 风险与决策点

1. **Tg 文件字段名需固定**
   开发前必须确认离线预测结果中的 Tg 字段名，推荐 `tg_celsius`。

2. **33M 行导入耗时较长**
   需要导入进度日志和可重复执行策略。

3. **SQLite 查询性能可能不足**
   第一版可先完成闭环；若查询慢，再引入 fingerprint index。

4. **IUPAC 转换工具未确认**
   不阻塞主检索，但知识库联动会先显示转换不可用。

5. **CSV 很大，不适合 Excel 打开**
   数据准备和导入应使用脚本、pandas chunk 或标准库 CSV 流式处理。

6. **Tg 单位必须归一**
   API 和 UI 按 `°C` 展示。若离线预测输出为 Kelvin 或其他单位，导入前必须转换为 `tg_celsius`。

7. **fingerprint BLOB 格式必须稳定**
   一旦数据导入后再修改序列化格式，旧库会不可读；实现时需要版本内固定并加测试。

---

## 9. 推荐实施顺序

1. 确认 `pi_polymers_with_tg.csv` 字段。
2. 新增 PI 本地库 schema。
3. 新增 PI CSV 导入脚本。
4. 导入时预计算 Morgan fingerprint。
5. 实现后端反向设计服务。
6. 实现后端 API。
7. 接入真实 SMILES2IUPAC 工具。
8. 跑真实 Tg 文件导入和全量查询。
9. 评估查询性能，决定是否引入 fingerprint index。
