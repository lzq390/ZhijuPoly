# `propclass.txt` 字段重复整理

源文件：[propclass.txt](/home/lzq390/gith/polyprop/propclass.txt)

说明：
- 本文档按“字段是否应视为同一属性”进行整理。
- 行号均对应源文件中的实际行号，包含表头后的正文位置。
- `明确重复` 表示建议直接合并。
- `疑似重复` 表示语义接近，但是否合并取决于你的数据口径。
- `相关但不重复` 表示名字相近，但物理意义不同，不建议合并。

## 1. 明确重复，建议合并

### 1.1 折射率
- 规范名建议：`Refractive index`
- 别名：
  - `Refractive index`，第 16 行
  - `refractive index`，第 56 行
  - `Refractive Index`，第 57 行
- 重复原因：仅大小写差异。

### 1.2 玻璃化转变温度
- 规范名建议：`Glass transition temperature`
- 别名：
  - `Glass transition temperature`，第 2 行
  - `Tg K`，第 15 行
  - `Tg`，第 113 行
- 重复原因：全称、缩写、单位后缀混用。

### 1.3 熔融温度
- 规范名建议：`Melting temperature`
- 别名：
  - `Melting temperature`，第 5 行
  - `Tm K`，第 122 行
- 重复原因：全称与缩写加单位后缀混用。

### 1.4 热分解温度
- 规范名建议：`Thermal decomposition temperature`
- 别名：
  - `Thermal decomposition temperature`，第 6 行
  - `decomposition temperature`，第 29 行
  - `Td K`，第 124 行
- 重复原因：全称、简写表达、单位后缀混用。

### 1.5 热导率
- 规范名建议：`Thermal conductivity`
- 别名：
  - `Thermal conductivity`，第 17 行
  - `Thermal Conductivity W Per Mk`，第 123 行
- 重复原因：单位后缀不同。

### 1.6 带隙
- 规范名建议：`bandgap`
- 别名：
  - `bandgap`，第 114 行
  - `Bandgap Ev`，第 132 行
- 重复原因：单位后缀不同。

### 1.7 结晶温度
- 规范名建议：`Crystallization temperature`
- 别名：
  - `Crystallization temperature`，第 61 行
  - `Crystallization Temperature K`，第 133 行
- 重复原因：单位后缀不同。

### 1.8 氧指数
- 规范名建议：`Oxygen index`
- 别名：
  - `Oxygen index`，第 45 行
  - `Limiting Oxygen Index Percentage`，第 125 行
- 重复原因：全称与带单位写法混用。

### 1.9 断裂伸长率
- 规范名建议：`Elongation at break`
- 别名：
  - `Elongation at break`，第 35 行
  - `Elongation At Break Percentage`，第 116 行
- 重复原因：单位后缀不同。

### 1.10 介电常数
- 规范名建议：`Dielectric constant`
- 别名：
  - `Dielectric constant ac`，第 18 行
  - `dielectric constant`，第 58 行
- 重复原因：大小写差异加测试条件后缀混入字段名。

## 2. 疑似重复，建议人工确认后再决定

### 2.1 接触角
- 候选规范名：`Contact angle`
- 候选别名：
  - `Contact angle`，第 25 行
  - `Water Contact Angle`，第 128 行
- 说明：如果你的库默认接触角均指水接触角，可以合并；如果 `Contact angle` 包含不同测试液体，则不应合并。

### 2.2 吸水相关
- 候选规范名：`Water absorption`
- 候选别名：
  - `Water absorption`，第 46 行
  - `Water Uptake Percentage`，第 127 行
- 说明：两者在很多数据源里会混用，但严格上测试方法和定义口径可能不同。

### 2.3 介电损耗相关
- 候选规范名：暂不建议直接统一
- 相关字段：
  - `Dielectric loss factor`，第 19 行
  - `Dielectric loss tangent`，第 27 行
- 说明：两者高度相关，但不是完全相同的物理量，通常不建议直接并成一个字段。

## 3. 相关但不重复，不建议合并

### 3.1 定压与定容比热
- `Specific heat capacity cp`，第 31 行
- `Specific heat capacity cv`，第 107 行
- 说明：`cp` 和 `cv` 不是同一物理量。

### 3.2 不同尺度的带隙
- `bandgap (chain)`，第 26 行
- `bandgap (bulk)`，第 40 行
- `bandgap`，第 114 行
- 说明：前两者通常表示不同建模或测量对象，不应简单并入通用 `bandgap`，除非你明确接受信息损失。

### 3.3 泛化强度字段与具体强度字段
- `Strength`，第 129 行
- `Tensile Strength Mpa`，第 120 行
- `Flexural Strength Mpa`，第 117 行
- `Compressive Strength Mpa`，第 115 行
- 说明：`Strength` 过于泛化，不能自动视为某一类具体强度。

### 3.4 软化温度相关
- `Softening temperature`，第 28 行
- `Vicat softening temperature`，第 77 行
- 说明：后者是特定测试标准下的软化温度，不建议直接合并。

## 4. 汇总建议

如果后续要做数据清洗，建议将字段分成三层：
- `canonical_name`：统一后的标准字段名，例如 `Glass transition temperature`
- `raw_name`：原始字段名，例如 `Tg K`
- `condition_or_unit`：从字段名中拆出的附加信息，例如 `K`、`ac`

优先合并的字段组如下：
- `Refractive index`
- `Glass transition temperature`
- `Melting temperature`
- `Thermal decomposition temperature`
- `Thermal conductivity`
- `bandgap`
- `Crystallization temperature`
- `Oxygen index`
- `Elongation at break`
- `Dielectric constant`

## 5. 机器可读映射草案

```yaml
Refractive index:
  - Refractive index
  - refractive index
  - Refractive Index
Glass transition temperature:
  - Glass transition temperature
  - Tg K
  - Tg
Melting temperature:
  - Melting temperature
  - Tm K
Thermal decomposition temperature:
  - Thermal decomposition temperature
  - decomposition temperature
  - Td K
Thermal conductivity:
  - Thermal conductivity
  - Thermal Conductivity W Per Mk
bandgap:
  - bandgap
  - Bandgap Ev
Crystallization temperature:
  - Crystallization temperature
  - Crystallization Temperature K
Oxygen index:
  - Oxygen index
  - Limiting Oxygen Index Percentage
Elongation at break:
  - Elongation at break
  - Elongation At Break Percentage
Dielectric constant:
  - Dielectric constant ac
  - dielectric constant
```
