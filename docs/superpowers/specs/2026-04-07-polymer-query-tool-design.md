# 聚合物查询工具设计文档

**项目名称**: PolyProp - 聚合物结构式查询工具  
**创建日期**: 2026-04-07  
**设计版本**: v2.0  
**状态**: 已按样例 CSV 数据源重构

---

## 1. 项目目标

构建一个基于 Web 的聚合物查询工具。用户在 Ketcher 中绘制结构，生成结构字符串后在本地数据库中执行：

1. 精确匹配
2. 相似度匹配
3. 按属性类别分组展示结果

首版数据库不依赖外部 PostgreSQL，而是由 [data1.csv](/home/lzq390/gith/polyprop/database/data1.csv) 自建。

---

## 2. 数据源与总体架构

### 2.1 数据源

首版唯一数据源：

- [data1.csv](/home/lzq390/gith/polyprop/database/data1.csv)

字段：

1. `polymer_name`
2. `smiles`
3. `property_category`
4. `property_name`
5. `property_value`
6. `property_unit`
7. `label_source`

### 2.2 架构

```text
CSV(data1.csv)
  -> import script
  -> local SQLite database
  -> FastAPI query service
  -> React + Ketcher frontend
```

### 2.3 技术栈

#### 后端
- Python 3.12
- FastAPI 0.115
- Uvicorn 0.32
- Pydantic 2.10
- SQLite
- RDKit 2023.9

#### 前端
- React 19
- TypeScript 5.9
- Vite 7
- MUI 7
- Axios

---

## 3. 输入契约

### 3.1 支持范围

首版支持 RDKit `Chem.MolFromSmiles` 可解析的结构字符串。

包括但不限于：

1. 普通小分子 SMILES
2. 含 `*` 连接位点的聚合物片段表达

### 3.2 不支持范围

1. RDKit 无法解析的 BigSMILES 表达
2. 复杂重复单元语法

### 3.3 行为约束

1. 用户可选择 `exact` 或 `similarity` 两种查找模式。
2. 请求输入若不可解析，接口始终返回 `422`，不进入精确或相似度流程。
3. 数据库中不可解析的 `smiles` 在相似度匹配时跳过。
4. 导入阶段为每个 polymer 尝试生成 `canonical_smiles`。

---

## 4. 数据库设计

### 4.1 设计目标

由于 CSV 将“聚合物”和“属性”混在同一行，首版数据库应拆成：

1. 聚合物主表
2. 属性表

### 4.2 推荐 schema

#### `polymers`

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

### 4.3 索引

```sql
CREATE INDEX idx_polymers_smiles ON polymers(smiles);
CREATE INDEX idx_polymers_canonical_smiles ON polymers(canonical_smiles);
CREATE INDEX idx_polymers_parse_ok ON polymers(rdkit_parse_ok);
CREATE INDEX idx_properties_polymer_id ON properties(polymer_id);
CREATE INDEX idx_properties_category ON properties(property_category);
```

---

## 5. API 设计

### 5.1 查询接口

`POST /api/v1/query/smiles`

请求体：

```json
{
  "smiles": "CCO",
  "match_mode": "exact",
  "similarity_threshold": 0.7,
  "top_k": 10
}
```

响应体：

```json
{
  "match_type": "exact",
  "query_time_ms": 120.5,
  "total": 1,
  "results": [
    {
      "polymer_id": "1",
      "polymer_name": "example polymer",
      "smiles": "*CC*",
      "canonical_smiles": "*CC*",
      "similarity_score": 1.0,
      "properties": {
        "thermal": [],
        "mechanical": [],
        "electrical": [
          {
            "property_name": "Electric conductivity",
            "property_value": "1.00E-10",
            "property_value_num": 1e-10,
            "property_unit": "1/(ohm*cm)",
            "label_source": "exp"
          }
        ],
        "chemical": [],
        "optical": [],
        "other": []
      }
    }
  ]
}
```

### 5.2 详情接口

`GET /api/v1/polymer/{polymer_id}`

### 5.3 预测接口

`POST /api/v1/predict`

首版固定返回：

```json
{
  "detail": "预测功能暂未启用,接口已预留"
}
```

状态码 `501`。

---

## 6. 后端模块设计

### 6.1 导入模块

新增导入脚本：

1. 读取 `database/data1.csv`
2. 去重生成 `polymers`
3. 写入 `properties`
4. 尝试为每个 `smiles` 生成 `canonical_smiles`
5. 无法解析时保留原始 `smiles`，把 `canonical_smiles` 置空，并将 `rdkit_parse_ok=0`
6. 导入采用“全量重建”策略，重复执行不会累加脏数据

### 6.2 查询模块

#### 精确匹配

优先匹配顺序：

1. `canonical_smiles`
2. 原始 `smiles`

约束：

1. 请求输入必须先通过 RDKit 校验
2. 非法输入直接 `422`

#### 相似度匹配

1. 对输入生成 RDKit Morgan 指纹
2. 全表扫描 `polymers`
3. 仅对 `rdkit_parse_ok=1` 的记录计算指纹
4. 返回阈值以上 Top-K

### 6.3 聚合模块

按 `property_category` 映射分组：

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

## 7. 前端设计

### 7.1 核心组件

1. `KetcherEditor`
2. `QueryPanel`
3. `ResultsDisplay`
4. `PolymerCard`
5. `PropertyGroupCard`
6. `PropertyItem`

### 7.2 展示字段

首版展示：

1. `polymer_name`
2. `smiles`
3. `similarity_score`
4. 属性分组
5. `property_name`
6. `property_value`
7. `property_unit`
8. `label_source`

首版不展示：

1. 文献
2. 工艺
3. 条件 JSON

---

## 8. 首版范围约束

### 8.1 明确支持

1. CSV 导入本地库
2. 精确匹配
3. 相似度匹配
4. 分类属性展示

### 8.2 明确不支持

1. 外部 PostgreSQL
2. 文献与工艺
3. BigSMILES 全语法支持
4. 条件字段折叠

---

## 9. 开发准入结论

当前可以进入开发，但前提是：

1. 先实现 CSV 导入和本地数据库构建
2. 所有首版设计都以 CSV 现有字段为准
3. 不再按旧版远程数据库设计继续实现
4. `config.py` 必须把 DB 路径和 CSV 路径解析为项目根目录下的绝对路径
5. 测试必须使用临时 SQLite 数据库，不得污染默认库文件
