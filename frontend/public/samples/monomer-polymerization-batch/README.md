# SMiPoly 双表批量聚合手工验证样本

- 表 A：`smipoly-diamines-120.csv`，120 个不同的二胺结构。
- 表 B：`smipoly-dianhydrides-120.csv`，120 个不同的二酐结构。
- 目标类型：聚酰亚胺（`polyimide`）。预计原始、有效和独立结构组合数均为 14,400。
- 编码：UTF-8 BOM。字段映射：`SMILES`、编号 `id`、名称 `name`。

来源为上游 SMiPoly 项目的 `classified_monomers.csv`，源文件 SHA-256 与筛选条件见 [selection.json](selection.json)。按原文件顺序选择前 120 个通过当时 RDKit 标准化且不重复的结构；二胺同时要求 `diamin=true` 和 `pridiamin=true`，二酐要求 `dicAnhyd=true`，并使用当时的 SMiPoly 引擎重新确认分类。

`source_row` 是来源 CSV 的记录行号（表头为第 1 行）；`source_label` 保留原数据的标签，不要求唯一。`id` 和 `name` 是本次样本编号与显示名称，名称不代表化学命名。

进入 9001 开发站点的 `/monomer-polymerization?mode=batch`，上传 A/B 文件并预检，确认每表有效数与唯一结构数均为 120、错误数为 0 后开始聚合。完成后下载 CSV ZIP 或 Excel，核对 `pairs` 中共有 14,400 个原始行组合。候选结果数量由引擎返回值决定，一对单体可产生多个候选。

`selection.json` 保存源文件哈希、选择条件及样本文件校验和。
