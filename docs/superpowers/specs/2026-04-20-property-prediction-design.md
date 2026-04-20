# 聚合物性质预测功能设计

**日期：** 2026-04-20
**状态：** 已确认

## 概述

在现有的结构检索工作台上新增 ML 性质预测功能。用户在 Ketcher 中绘制聚合物结构后，切换到预测 Tab，勾选感兴趣的性质，点击预测，系统调用对应的 Random Forest 模型返回预测值，结果显示在结果面板的「预测结果」Tab 中。

---

## 后端设计

### 新增 / 修改文件

| 文件 | 变化 |
|---|---|
| `backend/app/services/predictor.py` | 新建，核心服务层 |
| `backend/app/routers/predict.py` | 改造，替换 501 存根 |
| `backend/app/models.py` | 新增 `PredictRequest` / `PredictResponse` |
| `backend/app/config.py` | 新增 `MODEL_DIR`（指向项目根 `model/` 目录） |

### `services/predictor.py`

- `PROPERTY_MODELS`：9 个属性名 → `.pkl` 文件名的映射字典
- `_model_cache`：模块级 dict，首次请求时 `joblib.load` 并缓存，后续复用
- `smiles_to_features(smiles: str) -> np.ndarray`：
  - 使用 `rdkit.Chem.Descriptors.descList` 提取全量分子描述符（约 210 维）
  - 清洗 NaN / Inf，`np.clip` 到 ±1e6，返回 shape `(1, n_features)`
- `predict(smiles: str, properties: list[str]) -> dict[str, float]`：
  - 调用 `smiles_to_features`，对每个勾选属性分别调用对应模型的 `.predict(X)[0]`
  - SMILES 非法时抛 `ValueError`

### API 端点

```
POST /api/v1/predict
```

请求体（`PredictRequest`）：
```json
{
  "smiles": "*/C(=C(/*)c1ccc(CCCC)cc1)c1ccccc1",
  "properties": ["Glass transition temperature", "O2 Permeability Barrer"]
}
```

响应体（`PredictResponse`）：
```json
{
  "predictions": {
    "Glass transition temperature": 312.4,
    "O2 Permeability Barrer": 0.82
  },
  "query_time_ms": 45.2
}
```

`properties` 字段为枚举子集校验，非法属性名返回 422。

### 支持的 9 个属性

| 属性名（枚举值） | 中文 | 单位 | 模型文件 |
|---|---|---|---|
| `Glass transition temperature` | 玻璃化转变温度 | K | `rf_Glass transition temperature_exp.pkl` |
| `Melting temperature` | 熔融温度 | K | `rf_Melting temperature_exp.pkl` |
| `Thermal decomposition temperature` | 热分解温度 | K | `rf_Thermal decomposition temperature_exp.pkl` |
| `Thermal decomposition weight loss` | 热分解失重率 | % | `rf_Thermal decomposition weight loss_exp.pkl` |
| `Elongation at break` | 断裂伸长率 | % | `rf_Elongation at break_exp.pkl` |
| `Tensile stress strength at break` | 断裂拉伸强度 | MPa | `rf_Tensile stress strength at break_exp.pkl` |
| `O2 Permeability Barrer` | O₂ 渗透性 | Barrer | `rf_O2 Permeability Barrer_exp.pkl` |
| `Co2 Permeability Barrer` | CO₂ 渗透性 | Barrer | `rf_Co2 Permeability Barrer_exp.pkl` |
| `H2 Permeability Barrer` | H₂ 渗透性 | Barrer | `rf_H2 Permeability Barrer_exp.pkl` |

### `config.py` 新增

```python
MODEL_DIR: Path  # 默认：项目根目录 / "model"
```

`predictor.py` 通过 `get_settings().MODEL_DIR` 获取模型目录，与现有 `SQLITE_DB_PATH` 的解析方式一致。

### 依赖项

`backend/requirements.txt` 需新增（如未包含）：
- `joblib` — 模型反序列化
- `scikit-learn` — RF 模型运行时依赖

---

## 前端设计

### 新增 / 修改文件

| 文件 | 变化 |
|---|---|
| `frontend/src/types.ts` | 新增 `PredictRequest` / `PredictResponse` 类型 |
| `frontend/src/services/api.ts` | 新增 `predictSmiles()` 函数 |
| `frontend/src/hooks/usePredict.ts` | 新建，封装 loading / error / data 状态 |
| `frontend/src/components/QueryPanel.tsx` | 顶部加「检索 / 预测」Tab |
| `frontend/src/components/PredictionResults.tsx` | 新建，预测结果卡片网格 |
| `frontend/src/components/ResultsDisplay.tsx` | 顶部加「检索结果 / 预测结果」Tab |
| `frontend/src/App.tsx` | 接入 `usePredict`，管理 `selectedProperties` |

### QueryPanel Tab 行为

- 默认显示「检索」Tab（现有内容不变）
- 切换到「预测」Tab 显示：
  - 9 个 checkbox，标签为性质中文名
  - 「立即预测」按钮，未勾选任何性质或 SMILES 为空时禁用
  - 预测进行中时按钮显示「预测中...」

### PredictionResults 组件

- 接收 `PredictResponse | null`、`isLoading`、`error` 作为 props
- 结果以卡片网格展示（2 列），每张卡片：属性中文名 + 预测值（2 位小数）+ 单位
- 加载中显示骨架屏；出错显示错误提示；未预测时显示空状态引导文案

### ResultsDisplay 新增 Props

```typescript
type ResultsDisplayProps = {
  // 原有 props 不变
  data: SmilesQueryResponse | null;
  error: string | null;
  isLoading: boolean;
  request: SmilesQueryRequest;
  // 新增
  predictData: PredictResponse | null;
  isPredicting: boolean;
  predictError: string | null;
  activeTab: "query" | "predict";          // 由 App.tsx 控制
  onTabChange: (tab: "query" | "predict") => void;
};
```

### ResultsDisplay Tab 行为

- 「检索结果」Tab：现有内容不变
- 「预测结果」Tab：渲染 `PredictionResults`
- **自动切换由 `App.tsx` 控制**：`usePredict` 返回数据后，`App.tsx` 将 `activeTab` 置为 `"predict"`，传给 `ResultsDisplay`

### App 状态新增

```typescript
const [selectedProperties, setSelectedProperties] = useState<string[]>([]);
const { isLoading, error, data, submit } = usePredict();
```

---

## 数据流

```
Ketcher → smiles (App state)
              ↓
    QueryPanel「预测」Tab
    selectedProperties (App state)
              ↓
        usePredict.submit()
              ↓
    POST /api/v1/predict
    { smiles, properties }
              ↓
    predictor.predict()
      ├─ smiles_to_features()  ← RDKit Descriptors
      └─ model_cache[prop].predict(X)  ← joblib RF
              ↓
    { predictions: {prop: float}, query_time_ms }
              ↓
    ResultsDisplay「预测结果」Tab
    PredictionResults 卡片网格
```

---

## 错误处理

| 场景 | 处理方式 |
|---|---|
| SMILES 无法被 RDKit 解析 | 后端返回 422，前端在预测结果区显示错误提示 |
| `properties` 包含非法属性名 | 后端 Pydantic 422 校验拦截 |
| 未勾选任何性质直接点预测 | 前端禁用按钮 |
| 模型文件不存在 | 后端启动时检查，缺失则记录 warning 并从可用列表中移除 |

---

## 范围限制（YAGNI）

- 不做预测置信区间 / 不确定性估计
- 不缓存 SMILES 的预测结果
- 不支持批量 SMILES 预测
