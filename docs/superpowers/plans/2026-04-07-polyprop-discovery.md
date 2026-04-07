# PolyProp Discovery 文档

**项目名称**: PolyProp - 聚合物结构式查询工具  
**创建日期**: 2026-04-07  
**状态**: Completed  
**目的**: 在正式开发前确认输入契约、样例数据结构、首版数据库构建方式与实现边界。

---

## 1. 结论摘要

### 1.1 Go / No-Go
- 当前状态: `Go with local database build`
- 结论:
  - [x] 可以进入开发
  - [ ] 需要调整 spec / plan 后再开发
  - [ ] 需要降级范围后再开发

### 1.2 核心结论
- 输入格式结论: `Verified`
- 样例数据结构结论: `Verified`
- 首版数据库方案: `Use database/data1.csv to build local DB`
- 相似度基线方案: `Use CSV-built local DB with full-table scan`

### 1.3 最终方向
1. 当前没有现成数据库，不再依赖外部 PostgreSQL。
2. 首版以 [data1.csv](/home/lzq390/gith/polyprop/database/data1.csv) 为唯一数据源。
3. 先导入本地数据库，再开发查询 API 和前端展示。
4. 远程数据库兼容留作后续扩展，不纳入首版。

---

## 2. 核验范围

本次 discovery 回答以下问题：

1. `database/data1.csv` 是否足够作为首版数据源。
2. CSV 字段能否支撑首版查询和展示。
3. 查询输入和 CSV 中的 `smiles` 是否属于同一类可处理字符串。
4. 首版数据库应采用什么最小 schema。
5. 首版哪些能力可以做，哪些必须砍掉或延期。

本次 discovery 不做：

1. 远程数据库接入。
2. BigSMILES 完整语法解析。
3. 文献与工艺流程表设计。
4. 性能优化实现。

---

## 3. 输入契约核验

### 3.1 已验证样例

本地 RDKit 验证结果：

| 输入 | RDKit 可解析 | 标准化结果 | 结论 |
|---|---|---|---|
| `CCO` | Yes | `CCO` | 支持 |
| `c1ccccc1` | Yes | `c1ccccc1` | 支持 |
| `[*]CC[*]` | Yes | `*CC*` | 支持 |

### 3.2 CSV 中 `smiles` 样本观察

CSV 中存在如下模式：

| 样本 | 观察 |
|---|---|
| `**(=O)C1=C(*)OC=C(O1)C(=O)*` | 含 `*` 连接位点 |
| `**(=O)c1cc(ccc1*)C(=O)*` | 含 `*` 连接位点 |
| `**CC(=O)O[As](OC(=O)C(C*)*)OC(=O)C(C*)*` | 含杂原子与连接位点 |

### 3.3 输入契约结论

1. 首版支持 RDKit 可解析的结构字符串。
2. 含 `*` 连接位点的聚合物片段表达并不天然不支持。
3. 首版不承诺支持所有 BigSMILES 语法，只支持 RDKit 实际可解析者。
4. 接口行为：
   - 输入不可解析时返回 `422`
   - 数据库中不可解析记录在相似度匹配时跳过

---

## 4. 样例数据核验

数据源文件：[data1.csv](/home/lzq390/gith/polyprop/database/data1.csv)

### 4.1 CSV 字段

字段数：`7`

| 字段名 | 含义 | 是否可用于首版 |
|---|---|---|
| `polymer_name` | 聚合物名称 | Yes |
| `smiles` | 结构字符串 | Yes |
| `property_category` | 属性类别 | Yes |
| `property_name` | 属性名称 | Yes |
| `property_value` | 属性值 | Yes |
| `property_unit` | 属性单位 | Yes |
| `label_source` | 标签来源 | Yes |

### 4.2 数据规模

| 指标 | 值 |
|---|---|
| 总行数 | `48340` |
| polymer 数 | `13195` |
| 唯一 `smiles` 数 | `13158` |

### 4.3 类别分布

| 类别 | 行数 |
|---|---|
| `Thermal` | `28568` |
| `Chemical` | `5976` |
| `Mechanical` | `5902` |
| `Others` | `3826` |
| `Electrical` | `3380` |
| `Optical` | `688` |

### 4.4 单位与来源质量

| 指标 | 值 |
|---|---|
| 空 `property_value` 行数 | `0` |
| 空 `property_unit` 行数 | `2694` |
| `label_source=exp` 行数 | `48340` |

### 4.5 `smiles` 可解析率

| 指标 | 值 |
|---|---|
| 总行数 | `48340` |
| 可解析行数 | `48281` |
| 不可解析行数 | `59` |
| 唯一 `smiles` 数 | `13158` |
| 可解析唯一 `smiles` 数 | `13142` |
| 不可解析唯一 `smiles` 数 | `16` |

### 4.6 数据结构结论

1. CSV 足够支撑首版“聚合物 + 属性”的核心查询。
2. CSV 不包含文献表、工艺表、复杂条件字段。
3. 因此首版数据模型必须收缩，不能继续假设 `papers` / `processes` / `condition_json` 已存在。
4. CSV 中绝大多数 `smiles` 可被 RDKit 解析，但存在少量不可解析记录，导入阶段必须容错。

---

## 5. 首版数据库设计结论

### 5.1 推荐方案

首版使用本地 SQLite 数据库。

原因：

1. 项目从零开始，没有现成数据库。
2. CSV 单文件导入 SQLite 最快，依赖最少。
3. FastAPI 查询层实现简单。
4. 后续若需要迁移到 PostgreSQL，可保留相同逻辑层并替换数据库实现。

导入策略：

1. 首版采用“删库或清表后全量重建”的可重复导入策略。
2. 不做增量导入和 upsert。
3. 每次导入结果必须只由当前 CSV 决定。

### 5.2 首版最小 schema

建议拆为两张表：

#### `polymers`

```sql
CREATE TABLE polymers (
  polymer_id INTEGER PRIMARY KEY AUTOINCREMENT,
  polymer_name TEXT NOT NULL,
  smiles TEXT NOT NULL,
  canonical_smiles TEXT,
  UNIQUE(polymer_name, smiles)
);
```

#### `properties`

```sql
CREATE TABLE properties (
  property_id INTEGER PRIMARY KEY AUTOINCREMENT,
  polymer_id INTEGER NOT NULL,
  property_category TEXT NOT NULL,
  property_name TEXT NOT NULL,
  property_value TEXT NOT NULL,
  property_value_num REAL,
  property_unit TEXT,
  label_source TEXT,
  FOREIGN KEY (polymer_id) REFERENCES polymers(polymer_id)
);
```

### 5.3 索引建议

```sql
CREATE INDEX idx_polymers_smiles ON polymers(smiles);
CREATE INDEX idx_polymers_canonical_smiles ON polymers(canonical_smiles);
CREATE INDEX idx_properties_polymer_id ON properties(polymer_id);
CREATE INDEX idx_properties_category ON properties(property_category);
```

---

## 6. 首版功能边界

### 6.1 首版明确支持

1. 基于 `smiles` / `canonical_smiles` 的精确匹配
2. 基于 RDKit 指纹的相似度匹配
3. 聚合物基本信息展示
4. 属性按类别分组展示
5. `label_source` 展示

### 6.2 首版明确不支持

1. 文献展示
2. 工艺流程展示
3. 测试条件折叠
4. 外部 PostgreSQL 接入
5. BigSMILES 全量语法支持

### 6.3 非法输入与不可解析记录语义

1. 用户只能在合法输入上选择 `exact` 或 `similarity`。
2. 若请求输入本身不可被 RDKit 解析，接口始终返回 `422`。
3. 导入时若 CSV 某条 `smiles` 不可解析：
   - 该 polymer 仍可入库
   - `canonical_smiles` 置空
   - 相似度匹配时跳过
   - 精确匹配时仅参与原始 `smiles` 字符串匹配（若产品决定保留该能力）

### 6.4 对 spec / plan 的影响

必须修改：

1. 去掉首版对 `dwd_polymer` / `dwd_compound_property` / `dwd_paper` 的依赖假设。
2. 去掉首版 `papers` / `processes` 的必做要求。
3. 把数据接入改为 `CSV -> import script -> local SQLite`.
4. 把 discovery 的数据库阻塞结论改成“自建本地数据库可开工”。

---

## 7. 性能判断

### 7.1 当前结论

`13158` 个唯一结构字符串对首版全表扫描来说是可以接受的基线规模。

### 7.2 首版性能策略

1. 精确匹配优先使用 `canonical_smiles`
2. 相似度匹配允许全表扫描
3. 若实际体验过慢，再增加指纹缓存

### 7.3 测试与路径约束

1. 运行时路径应在 `config.py` 中统一转成基于项目根目录的绝对路径。
2. 测试不得复用默认 `backend/data/polyprop.db`。
3. 测试应使用临时 SQLite 文件或临时目录数据库。
4. 导入测试必须验证重复执行后结果不累加。

---

## 8. 执行记录

| 时间 | 动作 | 结果 | 备注 |
|---|---|---|---|
| 2026-04-07 | 创建 discovery 文档 | 完成 |  |
| 2026-04-07 | 本地 RDKit 样例验证 | 完成 | `CCO` / `c1ccccc1` / `[*]CC[*]` 可解析 |
| 2026-04-07 | 检查 CSV 表头 | 完成 | 共 `7` 个字段 |
| 2026-04-07 | 统计 CSV 数据规模 | 完成 | `48340` 行，`13195` 个 polymer |
| 2026-04-07 | 确认项目现状 | 完成 | 当前无现成数据库，决定自建本地数据库 |

---

## 9. 最终开发准入结论

### 9.1 准入判断

- [x] 准许进入开发
- [ ] 不准许，需先修改 spec
- [ ] 不准许，需先修改 plan
- [ ] 不准许，需先降级需求

### 9.2 进入开发前必须满足

1. 以 `database/data1.csv` 为首版唯一数据源
2. 先实现导入脚本和本地数据库 schema
3. 先按“无文献、无工艺、无条件字段”的首版范围开发

### 9.3 当前状态

可以进入开发，但必须按“CSV 自建本地数据库”的路线推进。
