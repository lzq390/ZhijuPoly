# 聚合物性质预测功能实施文档

**日期**: 2026-04-20  
**来源设计**: [2026-04-20-property-prediction-design.md](/home/lzq390/gith/polyprop/docs/superpowers/specs/2026-04-20-property-prediction-design.md)  
**当前状态**: 可进入开发  
**目标**: 在现有 PolyProp 结构检索工作台上新增单结构、多属性的 ML 性质预测能力，打通 `Ketcher -> 预测 API -> 结果面板` 的完整链路。

---

## 1. 目标与范围

### 1.1 本次交付目标

1. 用户在 Ketcher 中绘制或编辑聚合物结构。
2. 用户切换到「预测」模式，勾选一个或多个性质。
3. 前端提交 `smiles + properties` 到后端 `POST /api/v1/predict`。
4. 后端基于 RDKit 描述符和本地 Random Forest `.pkl` 模型返回预测值。
5. 前端在结果面板的「预测结果」Tab 中展示预测卡片、耗时、错误或空状态。

### 1.2 明确纳入范围

1. 单条 SMILES 的同步预测。
2. 9 个已定义属性的枚举校验、模型加载、推理和结果展示。
3. 前后端错误处理、基础测试、集成联调。
4. 模型目录配置和运行时缓存。

### 1.3 明确不纳入范围

1. 批量结构预测。
2. 预测不确定性、置信区间、解释性分析。
3. 预测结果持久化、数据库落库或历史记录。
4. 模型训练、重训练、模型管理后台。
5. 对任意 BigSMILES 语法的额外增强支持。

---

## 2. 当前代码基线与差距

### 2.1 已存在基础

1. 后端 FastAPI 主应用已接入 `query` 与 `predict` 路由入口。
2. `backend/app/config.py` 已有项目根路径解析能力，可复用到模型目录。
3. `backend/app/models.py` 已有查询与 3D 结构相关 Pydantic 模型。
4. 前端已存在查询主流程：
   - `frontend/src/App.tsx`
   - `frontend/src/hooks/useQuery.ts`
   - `frontend/src/services/api.ts`
   - `frontend/src/components/QueryPanel.tsx`
   - `frontend/src/components/ResultsDisplay.tsx`
5. 本地 `model/` 目录中已存在 9 个 `.pkl` 模型文件。

### 2.2 当前缺口

1. `backend/app/routers/predict.py` 仍是 `501` 存根实现。
2. 后端缺少 `backend/app/services/predictor.py`。
3. 后端数据模型中缺少 `PredictRequest` / `PredictResponse`。
4. `backend/requirements.txt` 尚未声明 `joblib`、`scikit-learn`。
5. 前端类型实际位于 `frontend/src/types/index.ts`，与设计文档中的 `frontend/src/types.ts` 不一致，实施时必须按现状路径修改。
6. 前端没有预测 API、预测 hook、预测结果组件，也没有 Query / Predict 双 Tab。
7. 现有 API 测试仍断言预测接口返回 `501`，需要整体更新。
8. 现有 UI 组件库中没有现成的 `checkbox` 或 `tabs` 组件，实施时需要采用轻量自定义实现，避免无谓扩展。
9. 结果区外层标题、说明文案和摘要 badge 目前硬编码为查询语义，若只改 `ResultsDisplay` 内层内容，会出现“预测结果内容 + 查询结果外壳”不一致。

### 2.3 本次实施的代码落点修正

按当前仓库结构，设计稿中的文件落点应调整为：

| 设计稿路径 | 实际实施路径 |
|---|---|
| `frontend/src/types.ts` | `frontend/src/types/index.ts` |
| 新增 `frontend/src/hooks/usePredict.ts` | 保持不变 |
| 新增 `frontend/src/components/PredictionResults.tsx` | 保持不变 |

---

## 3. 实施原则

1. 尽量复用现有查询链路的设计模式，避免为预测单独引入第二套风格。
2. 后端预测服务与路由分层，路由只负责参数接收、异常映射、耗时统计。
3. 模型按需加载并缓存，不在每次请求中重复反序列化。
4. 前端预测与查询状态解耦，但页面层统一由 `App.tsx` 编排。
5. 先打通最小可用闭环，再补测试与错误边界。
6. 文档和实现均以当前仓库实际结构为准，不机械照搬设计稿中的旧路径。

---

## 4. 任务拆解

## Task 0: 冻结预测数据契约与属性字典

**目标**: 把请求/响应契约、属性枚举、单位映射和模型文件映射固定下来，避免前后端各自维护两套名字。

**Files**
- Update: `backend/app/models.py`
- Create or Update: `backend/app/services/predictor.py`
- Update: `frontend/src/types/index.ts`

- [ ] 固定 9 个属性的英文枚举值
- [ ] 固定英文属性名 -> 中文展示名映射
- [ ] 固定英文属性名 -> 单位映射
- [ ] 固定英文属性名 -> 模型文件名映射
- [ ] 明确请求/响应字段的最终结构

### 实施说明

1. 以后端 `PROPERTY_MODELS` 作为唯一事实来源。
2. 后端请求校验不得再手写第二份独立属性名单；`PredictRequest` 的合法属性集合必须直接复用共享常量，或通过基于共享常量的 validator 校验。
3. 前端展示名称与单位使用本地常量表，但内容必须与后端枚举一一对应。
4. `PredictRequest.properties` 必须是非空列表，且元素只能来自已支持的属性集合。
5. `PredictResponse.predictions` 采用 `{ [propertyName]: number }` 的扁平字典结构，不包装成数组。

### 完成标准

1. 前后端属性名完全一致，不存在大小写、空格或化学式写法漂移。
2. 任意非法属性名都在后端模型校验阶段直接返回 `422`。
3. 文档、后端枚举、后端校验规则、前端映射表四者一致。

---

## Task 1: 后端配置与运行时依赖补齐

**目标**: 让后端具备定位模型目录和加载 sklearn/joblib 模型的运行条件。

**Files**
- Update: `backend/app/config.py`
- Update: `backend/requirements.txt`

- [ ] 为 `Settings` 新增 `model_dir`
- [ ] 新增 `model_dir_path` 或等价 `Path` 访问器
- [ ] 补充 `joblib`
- [ ] 补充 `scikit-learn`

### 实施说明

1. `MODEL_DIR` 默认值设为项目根目录下的 `model`。
2. 路径解析逻辑继续复用 `_resolve_from_root()`，与 `SQLITE_DB_PATH` 保持一致。
3. 不新增新的环境文件格式；仅在 `.env.example` 中按需补充说明即可。
4. 应用启动时对 `PROPERTY_MODELS` 对应文件做一次可用性探测，记录缺失模型 warning，并生成最终 `AVAILABLE_PROPERTIES` 集合。
5. 若模型目录整体不存在，启动时应给出清晰 warning；预测服务仅允许请求 `AVAILABLE_PROPERTIES` 中的属性。

### 完成标准

1. `get_settings().model_dir_path` 能正确解析到 `/home/lzq390/gith/polyprop/model`。
2. 安装依赖后可正常导入 `joblib` 与 `sklearn`。
3. 本地、测试、CI 环境都不依赖硬编码绝对路径。
4. 缺失模型不会继续以“可选属性”身份暴露到正式预测流程中。

---

## Task 2: 实现预测服务层 `predictor.py`

**目标**: 完成特征提取、模型缓存、模型推理和运行时校验。

**Files**
- Create: `backend/app/services/predictor.py`
- Update: `backend/app/services/__init__.py`（如需导出）

- [ ] 实现属性字典与模型文件映射
- [ ] 实现模块级模型缓存
- [ ] 实现模型加载函数
- [ ] 实现 `smiles_to_features(smiles)`
- [ ] 实现 `predict(smiles, properties)`
- [ ] 处理非法 SMILES、模型缺失、预测失败场景

### 服务层建议接口

```python
PROPERTY_MODELS: dict[str, str]
PROPERTY_UNITS: dict[str, str]
PROPERTY_LABELS_ZH: dict[str, str]
AVAILABLE_PROPERTIES: tuple[str, ...]

def get_available_properties() -> list[str]: ...
def initialize_available_properties() -> tuple[str, ...]: ...
def load_model(property_name: str): ...
def smiles_to_features(smiles: str) -> np.ndarray: ...
def predict(smiles: str, properties: list[str]) -> dict[str, float]: ...
```

### 实施细节

1. `smiles_to_features()` 使用 `rdkit.Chem.MolFromSmiles()` 解析输入。
2. 使用 `rdkit.Chem.Descriptors.descList` 按既定顺序提取全部描述符。
3. 对每个描述符结果做数值归一化处理：
   - `NaN -> 0.0`
   - `Inf/-Inf -> 0.0` 或先 `nan_to_num`
   - 统一 `clip` 到 `[-1e6, 1e6]`
4. 返回值必须是 `shape == (1, n_features)` 的 `numpy.ndarray`。
5. 启动时遍历 `PROPERTY_MODELS`，把模型文件存在的属性写入 `AVAILABLE_PROPERTIES`，缺失项记录 warning 并从可用集合中排除。
6. 模型首次请求时 `joblib.load()`，之后复用 `_model_cache[property_name]`。
7. 如果请求了不在 `AVAILABLE_PROPERTIES` 中的属性，应抛出明确异常；如果运行中模型文件丢失，也应抛出可定位错误。
8. 预测结果统一转为原生 `float`，避免 `numpy.float32/64` 直接进入 JSON。

### 关键实现约束

1. 模型缓存必须是惰性加载，避免应用启动即加载全部模型。
2. 描述符顺序必须稳定，不能每次重新排序。
3. “属性是否支持”与“属性对应模型文件是否存在”必须统一收敛到 `AVAILABLE_PROPERTIES`，不能让请求模型、服务层和前端各自维护不同名单。
4. 对非法 SMILES 应抛出 `ValueError` 或项目内部统一异常，再由路由层映射为 `422`。
5. 不要在服务层拼接 HTTP 语义；服务层只暴露 Python 异常。

### 完成标准

1. 对合法 SMILES 和任意属性子集都能返回同结构的字典结果。
2. 同一属性的第二次调用不会重复从磁盘加载模型。
3. 非法 SMILES 时服务层抛出明确异常。
4. 启动探测后，缺失模型对应属性不会出现在 `get_available_properties()` 返回值中。
5. 运行中如出现模型文件缺失，错误信息包含属性名和模型路径。

---

## Task 3: 后端请求模型与预测路由改造

**目标**: 将 `/api/v1/predict` 从存根替换为正式接口。

**Files**
- Update: `backend/app/models.py`
- Update: `backend/app/routers/predict.py`

- [ ] 新增 `PredictRequest`
- [ ] 新增 `PredictResponse`
- [ ] 将 `predict.py` 改为正式实现
- [ ] 加入请求耗时统计
- [ ] 完成异常到 HTTP 状态码的映射

### 请求模型建议

```python
class PredictRequest(BaseModel):
    smiles: str = Field(min_length=1)
    properties: list[str] = Field(min_length=1)
```

随后通过共享常量做校验：

```python
@field_validator("properties")
@classmethod
def validate_properties(cls, value: list[str]) -> list[str]:
    ...
```

### 响应模型建议

```python
class PredictResponse(BaseModel):
    predictions: dict[str, float]
    query_time_ms: float
```

### 路由行为要求

1. 路由签名使用 `response_model=PredictResponse`。
2. 路由内仅负责：
   - 记录开始时间
   - 调用服务层
   - 捕获服务层异常并翻译为 `HTTPException`
   - 返回 `PredictResponse`
3. 非法 SMILES 返回 `422`。
4. 属性名不在共享可用集合中时返回 `422`。
5. 运行中模型文件缺失或模型推理失败返回 `500`，错误信息应面向开发定位，不要返回栈信息。

### 完成标准

1. `POST /api/v1/predict` 对有效请求返回 `200` 和预测字典。
2. 非法属性名由 Pydantic 拦截为 `422`。
3. 非法 SMILES 返回 `422`。
4. 缺失模型对应属性不会被当作合法可预测属性。
5. 原先测试中对 `501` 的断言全部移除。

---

## Task 4: 后端测试补齐

**目标**: 让预测能力具备最小可信测试覆盖。

**Files**
- Update: `backend/tests/test_api.py`
- Create: `backend/tests/test_predictor.py`
- Update: `backend/tests/test_models.py`

- [ ] 为 `PredictRequest` / `PredictResponse` 添加模型测试
- [ ] 为 `predictor.py` 添加单元测试
- [ ] 为 `/api/v1/predict` 添加接口测试
- [ ] 删除旧的 `501` 断言

### 推荐测试矩阵

#### `test_predictor.py`

1. `smiles_to_features("CCO")` 返回二维数组且列数大于 0。
2. `smiles_to_features()` 对非法 SMILES 抛异常。
3. `predict()` 可对属性子集返回仅包含该子集的结果。
4. 连续两次请求同一属性时模型只加载一次。
5. 初始化后 `get_available_properties()` 不包含缺失模型对应属性。
6. 运行中模型文件缺失时抛出明确异常。

#### `test_models.py`

1. `PredictRequest` 接受合法属性列表。
2. `PredictRequest` 拒绝空属性数组。
3. `PredictRequest` 拒绝非法属性名。
4. `PredictRequest` 拒绝请求不在 `AVAILABLE_PROPERTIES` 中的属性。

#### `test_api.py`

1. 成功预测返回 `predictions` 和 `query_time_ms`。
2. 非法 SMILES 返回 `422`。
3. 非法属性返回 `422`。
4. 不可用属性返回 `422`。
5. 模型异常返回 `500`。

### 测试实现建议

1. 接口测试不要强耦合真实模型数值，可对服务层做 `monkeypatch`，断言响应结构与状态码。
2. 服务层测试可在少量场景下使用真实模型文件，但不建议让所有测试依赖磁盘中的 `.pkl`。
3. 若使用真实模型，测试应只断言返回类型和值可被序列化，不要断言某个固定预测数值。

### 完成标准

1. 后端测试在本地能稳定通过。
2. 测试既覆盖 happy path，也覆盖输入非法和模型缺失路径。
3. 不再存在“接口预留但未实现”的过时测试。

---

## Task 5: 前端类型、API 与 `usePredict` 状态流

**目标**: 在前端建立独立的预测请求链路。

**Files**
- Update: `frontend/src/types/index.ts`
- Update: `frontend/src/services/api.ts`
- Create: `frontend/src/hooks/usePredict.ts`

- [ ] 新增 `PredictRequest` 类型
- [ ] 新增 `PredictResponse` 类型
- [ ] 新增 `predictSmiles()` API 封装
- [ ] 新增 `usePredict()` hook

### `frontend/src/types/index.ts`

新增：

```ts
export type PredictableProperty = ...;

export type PredictRequest = {
  smiles: string;
  properties: PredictableProperty[];
};

export type PredictResponse = {
  predictions: Record<PredictableProperty, number>;
  query_time_ms: number;
};
```

### `frontend/src/services/api.ts`

新增：

```ts
export function predictSmiles(payload: PredictRequest): Promise<PredictResponse>
```

复用现有 `postJSON()`，不再单独引入新的请求工具。

### `frontend/src/hooks/usePredict.ts`

状态结构建议保持与 `useQuery()` 对齐：

```ts
type PredictState = {
  isLoading: boolean;
  error: string | null;
  data: PredictResponse | null;
};
```

### 实施约束

1. `usePredict()` 仅负责预测状态，不混入查询状态。
2. 每次发起新预测前清空旧错误。
3. 失败时保留 `data: null`，避免用户误以为旧结果仍然有效。

### 完成标准

1. 前端可独立触发 `POST /api/v1/predict`。
2. hook 能正确维护加载、成功、失败三种状态。
3. 类型定义与后端返回结构一致。

---

## Task 6: QueryPanel 增加「检索 / 预测」双模式交互

**目标**: 在现有控制面板中加入预测模式与属性选择能力。

**Files**
- Update: `frontend/src/components/QueryPanel.tsx`
- Update: `frontend/src/App.tsx`

- [ ] 为 QueryPanel 增加顶部模式 Tab
- [ ] 保留现有检索 UI 不变
- [ ] 新增预测模式下的属性勾选区
- [ ] 新增预测按钮状态控制
- [ ] 在 `App.tsx` 新增 `selectedProperties` 与模式状态

### 交互设计要求

1. QueryPanel 顶部提供两个模式：
   - `检索`
   - `预测`
2. 默认进入 `检索` 模式。
3. 切换到 `预测` 模式时：
   - 隐藏检索参数卡片
   - 显示 9 个属性复选项
   - 显示 `立即预测` 按钮
4. 未勾选任何属性或 SMILES 为空时，预测按钮禁用。
5. 预测进行中按钮文案变为 `预测中...`。

### 状态建议

`App.tsx` 新增：

```ts
const [panelMode, setPanelMode] = useState<"query" | "predict">("query");
const [selectedProperties, setSelectedProperties] = useState<PredictableProperty[]>([]);
const predict = usePredict();
```

### 实施说明

1. 现有组件库没有 `Tabs` / `Checkbox` 组件，可先用语义化按钮和原生 `<input type="checkbox" />` 完成。
2. 样式上保持与现有 QueryPanel 卡片语言一致，不另起一套视觉规范。
3. `App.tsx` 负责把 `smiles` 同时传给查询与预测流程，但不要让两者共用 loading/error/data。
4. 预测属性列表应以后端共享可用集合为准；若当前阶段不新增专门配置接口，则前端常量必须与后端 `AVAILABLE_PROPERTIES` 同步维护，并在文档中明确这是临时约束。

### 完成标准

1. 用户可在不离开当前面板的情况下切换查询和预测。
2. 预测模式下能选择多个属性并触发提交。
3. 检索模式现有功能无回归。

---

## Task 7: 新增 `PredictionResults` 与结果面板双 Tab

**目标**: 在结果区同时承载查询结果和预测结果，并由页面层自动切换到预测结果。

**Files**
- Create: `frontend/src/components/PredictionResults.tsx`
- Update: `frontend/src/components/ResultsDisplay.tsx`
- Update: `frontend/src/App.tsx`

- [ ] 新建 `PredictionResults` 组件
- [ ] 为 `ResultsDisplay` 增加双 Tab
- [ ] 支持 `predictData / isPredicting / predictError`
- [ ] 在预测成功后自动切换到预测结果 Tab

### `PredictionResults` 组件职责

接收：

```ts
type PredictionResultsProps = {
  data: PredictResponse | null;
  isLoading: boolean;
  error: string | null;
};
```

负责展示：

1. 空状态
2. 加载骨架屏
3. 错误提示
4. 预测结果卡片网格

### 展示要求

1. 卡片按两列网格展示。
2. 每张卡片显示：
   - 中文属性名
   - 两位小数的预测值
   - 单位
3. 顶部可显示本次预测耗时。
4. 如果结果为空，显示引导文案，而不是空白区域。

### `ResultsDisplay` 新增职责

1. 结果面板顶部切换：
   - `检索结果`
   - `预测结果`
2. `App.tsx` 控制当前激活 Tab：
   - 手动切换可用
   - 预测成功后自动切换到 `predict`
3. 查询结果区继续复用当前 `PolymerCard` 渲染逻辑，不做大改。
4. `ResultsDisplay` 外层壳子之外，`App.tsx` 中包裹结果区的标题、说明文案和摘要 badge 也必须根据 `activeResultsTab` 切换，避免“查询结果外壳 + 预测内容”的不一致。

### 完成标准

1. 查询和预测两套结果都能在同一结果容器中切换查看。
2. 预测成功后自动跳转到 `预测结果`。
3. 查询结果原有摘要区和明细区不受影响。

---

## Task 8: 页面级状态编排与联调

**目标**: 在 `App.tsx` 中打通查询、预测、主结果面板三者的编排逻辑。

**Files**
- Update: `frontend/src/App.tsx`

- [ ] 接入 `usePredict()`
- [ ] 管理 QueryPanel 模式状态
- [ ] 管理 ResultsDisplay 激活 Tab 状态
- [ ] 预测成功后自动切换结果面板 Tab
- [ ] 避免查询和预测的状态相互污染
- [ ] 根据结果区激活 Tab 改写外层标题、说明文案和 badge 摘要

### 页面层建议状态

```ts
const [panelMode, setPanelMode] = useState<"query" | "predict">("query");
const [activeResultsTab, setActiveResultsTab] = useState<"query" | "predict">("query");
const [selectedProperties, setSelectedProperties] = useState<PredictableProperty[]>([]);
```

### 页面层行为要求

1. 用户发起查询时，不清空最近一次预测结果。
2. 用户发起预测时，不清空最近一次查询结果。
3. 预测成功后自动 `setActiveResultsTab("predict")`。
4. 若预测失败，也应切到 `预测结果` Tab，以便用户看到错误提示。
5. 若用户手动切回 `检索结果`，系统不应强行反复抢焦点，只有新的预测请求完成后再自动切换一次。
6. `App.tsx` 结果区外层标题、描述和 badge 必须与当前激活 Tab 保持一致：
   - `query` 时显示查询语义与查询指标
   - `predict` 时显示预测语义与预测指标

### 完成标准

1. 页面层状态清晰，没有把查询状态和预测状态混在同一个对象中。
2. 结果区切换行为符合预期，没有闪烁和错位。
3. 外层结果区文案和摘要不会在预测模式下继续显示“查询结果面板”等错误语义。
4. 操作顺序支持：
   - 先查后预测
   - 先预测后查
   - 连续多次预测不同属性组合

---

## Task 9: 端到端验收与回归检查

**目标**: 在代码完成后验证新增能力没有破坏现有结构检索主流程。

**Files**
- Update: `backend/tests/test_api.py`
- Update: `frontend` 相关实现文件
- Optional: 新增前端组件测试文件（若仓库已具备测试框架）

- [ ] 后端单元测试通过
- [ ] 预测接口联调通过
- [ ] 前端交互手测通过
- [ ] 检索主流程回归通过

### 后端验收清单

1. `POST /api/v1/predict` 对合法请求返回 `200`。
2. 返回体中包含 `predictions` 和 `query_time_ms`。
3. 非法 SMILES 返回 `422`。
4. 非法属性或不可用属性返回 `422`。
5. 启动探测后，缺失模型对应属性不会进入可预测属性集合。
6. 模型运行中异常返回 `500`。

### 前端验收清单

1. QueryPanel 可切换到预测模式。
2. 9 个属性可多选。
3. 无 SMILES 或未选属性时预测按钮不可点。
4. 预测成功后结果区自动切到「预测结果」。
5. 预测失败时结果区显示错误，不影响查询结果。
6. 外层结果区标题、说明和摘要 badge 与当前结果 Tab 一致。
7. 查询模式原功能、3D 预览、结果展示不回退。

### 手动联调场景

1. 输入 `*CC*`，预测 `Glass transition temperature`。
2. 输入合法芳香族聚合物结构，预测 2 到 3 个属性。
3. 输入 `not-a-smiles`，验证错误提示。
4. 人工改错一个模型文件名，验证该属性在可用集合中被移除，且前端不应继续暴露为正常可选项。
5. 在启动后人为删除某个已探测通过的模型文件，验证运行时错误路径。

### 完成标准

1. 后端测试通过。
2. 前端开发环境中可完整走通预测闭环。
3. 检索和预测两个主流程都可独立使用。

---

## 5. 推荐开发顺序

1. Task 0：冻结数据契约与属性字典。
2. Task 1：补配置和依赖。
3. Task 2：实现 `predictor.py`。
4. Task 3：打通 `/api/v1/predict`。
5. Task 4：补齐后端测试。
6. Task 5：补前端类型、API、hook。
7. Task 6：改 QueryPanel。
8. Task 7：实现 `PredictionResults` 和结果面板双 Tab。
9. Task 8：在 `App.tsx` 完成页面编排。
10. Task 9：联调、回归、收尾。

原因：

1. 先稳定后端契约，前端才不会反复改类型。
2. 先把服务层和路由测通，再做 UI，能缩短联调往返。
3. 查询主流程已存在，新增功能最好以“旁路插入”的方式推进，降低回归风险。

---

## 6. 风险与处理策略

### 风险 1: `scikit-learn` 版本与模型序列化版本不兼容

处理：

1. 优先使用训练模型时兼容的 sklearn 主版本。
2. 在服务层加载模型时输出明确错误。
3. 测试阶段尽早执行一次真实模型加载，而不是完全依赖 mock。

### 风险 2: RDKit 描述符顺序与训练时不一致

处理：

1. 使用 `Descriptors.descList` 的原始顺序，不自行重排。
2. 如模型训练侧有固定特征顺序文件，应尽快补充并改为显式顺序。
3. 在文档中标记这是当前实现的关键隐含前提。

### 风险 3: 前端结果切换逻辑混乱

处理：

1. 将 QueryPanel 模式和结果面板 Tab 分开建状态。
2. 自动切换只在预测请求完成时触发。
3. 不让查询提交动作覆盖预测结果状态。
4. 同时改造 `App.tsx` 中结果区外层文案和摘要，避免只切内层内容。

### 风险 4: 可用属性集合在前后端定义不一致

处理：

1. 后端以 `PROPERTY_MODELS` 与启动探测结果生成 `AVAILABLE_PROPERTIES` 作为唯一事实来源。
2. 请求模型校验不得再维护第二份手写名单。
3. 如果当前阶段前端仍使用本地常量渲染属性列表，必须在实施文档与代码注释中明确其来源于后端共享集合，并在联调阶段逐项核对。

### 风险 5: 真实模型推理耗时影响交互感知

处理：

1. 初期不做额外优化，先用按钮 loading 和骨架屏承接。
2. 依赖模型缓存减少重复磁盘加载。
3. 后续如果需要，可再增加 warmup 或异步任务机制。

---

## 7. 最终验收定义

以下条件全部满足，视为本功能完成：

1. 后端存在正式可用的 `POST /api/v1/predict`。
2. 支持 9 个既定属性的任意子集预测。
3. 前端 QueryPanel 支持检索/预测双模式切换。
4. 前端结果面板支持检索结果/预测结果双 Tab。
5. 预测成功可展示属性中文名、两位小数结果和单位。
6. 非法输入、非法属性、模型缺失都有明确错误反馈。
7. 现有结构检索主流程无功能回退。

---

## 8. 备注

1. 当前仓库中 `model/` 为未跟踪目录，但其下 9 个 `.pkl` 文件已到位，实施时可直接作为本地运行输入。
2. `model/` 目录中的推理模型文件应随仓库一起跟踪和提交，保证服务器侧可通过 `git pull` 直接获取运行所需模型。
3. 如果后续改为制品仓库或对象存储分发模型，需要单独补充发布与部署策略文档；本实施文档暂不覆盖该迁移方案。
