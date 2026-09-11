# 固定样本：总结提示词对比

目标：在同一份操作和内容输入上调整提示词，改善阅读回顾的重点与连贯性；本轮不调整前端展示。

## 固定输入与来源

- 样本：[summary-session-20260909.json](../scripts/fixtures/summary-session-20260909.json)，9 个事件，原记录 ID `2187d677-7dcf-47de-86e5-67c2bfa026bc`。
- 输入 SHA-256：`b6eb5f29e62199a70bf8469c0c2e5242d08121c7e9f968a5efd8cfbc658b6393`。
- 配置重载后原内存记录返回 410。本样本由原日志恢复操作，完整摘要取自原导入 samples.json，并核对历史日志中的标题、摘要长度和前缀；不是原记录的直接导出。事件时间采用 HTTP 请求到达时间，原 append 时间已不可得。
- 浏览内容：检索 polyimide|absorbance、CPI，打开两篇文献及其反应信息；筛选 Tg 158–245 C，展开 POC-B 的原始 SMILES 与 Tg 240 C 测量。材料明确为合成演示数据。

## 比较方法

基线与候选均使用用户新配置的模型 `gpt-5.6-sol`。通过脚本直接调用同一总结服务，绕过已结束记录的总结缓存；每次保存完整提示词、模型名称、输入/证据/提示词哈希、耗时及输出，不保存密钥或服务地址。

候选提示词：[v1](../scripts/fixtures/summary-prompt-candidate.txt)、[v2](../scripts/fixtures/summary-prompt-v2.txt)、[v3](../scripts/fixtures/summary-prompt-v3.txt)。逐步减少按步骤复述、摘要摘抄和指标罗列，继续保留三段式与证据边界。v3 恢复原样引用查询表达式的要求，并明确不列文献性能数字。

评价维度：是否突出本次浏览的线索；是否留下有辨识度的内容；是否准确保留阈值、来源和引用；是否区分事实与推断；是否避免功能枚举和固定结尾。

在 codex-lab 项目根目录执行（输出路径需使用新文件名）：

```bash
.runtime/knowledge-summary/.venv/bin/python -B scripts/evaluate_summary_poc.py \
  --timeout 180 --output .runtime/knowledge-summary/evaluation-baseline.json
.runtime/knowledge-summary/.venv/bin/python -B scripts/evaluate_summary_poc.py \
  --prompt scripts/fixtures/summary-prompt-candidate.txt \
  --timeout 180 --output .runtime/knowledge-summary/evaluation-candidate.json
```

## 实测结果

首轮基线/候选并发请求均触发应用原有 60 秒超时，未获得文本；不能据此评价提示词。后续评测延长等待并改为顺序执行，页面默认超时仍为 60 秒。总结与记录相关 17 项测试通过。

| 提示词 | 顺序重测耗时 | 观察 |
|---|---:|---|
| 原应用提示词 | 59.21 秒 | 三段完整，但开头仍列功能和操作，中段只列主题，结尾套用无直接联系。换模型未消除这份样本的流水账感。 |
| v1 | 149.69 秒 | 增加了内容，但开头仍按流程复述、指标较多；错误地将系列最高热分解温度归给 CPI-6。拒绝采用。 |
| v2 | 81.86 秒 | 按主题组织有所改善，指标归属修正，但仍堆缩写和性能数字，且将查询中的 `|` 改成 `/`。未采用。 |
| v3 | 70.14 秒 | 原样保留查询，将重点改为透明聚酰亚胺的结构设计思路，解释两篇文献使用光学信息的不同目的；不再罗列文献性能数字。仍偏长，且材料处未带 citation。 |
| v3 原样复测 | 35.76 秒 | 同样突出内容思路与光学测量用途的差别；查询、阈值、测量值和引用正确。仍略有功能枚举，措辞与篇幅有波动。 |

以上均使用相同 9 事件、证据 SHA-256 `4bb0e3ebfd82203b3bfa00450e1869ed9b0736eabfc263f4f7149ecce216998e` 和配置模型。原始结果分别位于本地 `.runtime/knowledge-summary/prompt-evaluation/evaluation-20260909-{baseline,candidate,v2,v3,v3-repeat}.json`，虚拟机则位于 `.runtime/knowledge-summary/`；这些文件包含当时的完整提示词，便于在应用提示词变更后复现基线。

单次耗时不能用于判定哪版提示词更快；本轮未调整服务的推理参数，也没有比较不同模型。仅对这一份样本作人工质量评估，不代表其他检索或空结果场景。提示词效果通过真实固定输入回放判断，自动测试用于验证证据、接口、缓存和失败重试，不把提示词字面断言当作质量测试。

## 采用与限制

采用 v3 为当前应用 `SUMMARY_PROMPT`，提示词 SHA-256 为 `2bac30c7cf88d684c951c2ce88b31dd5095903de2a5b0d95b670eea0726e0864`。本地与 codex-lab 同步；前端和模型配置未改。现有缓存总结不会被改写，新记录才使用新提示词。

旧提示词要求“只说阅读对象和主题，不摘抄摘要结论”，并规定无直接联系时使用固定句式，这会限制内容提炼。固定输入不变、仅修改提示词后输出已有改善，因此本样本现阶段不需要继续增加 Hook；这不等于证明其他场景的信息也充分。

v3 两次仍偏长，开头和句式也有波动，不能视作效果已经完全验收。真实模型回放均使用 180 秒评测等待；应用默认仍是 60 秒，v3 一次超过、一次低于该限制。新模型页面调用可能超时，记录保留可重试；尚未在新模型下完成浏览器回归。下一步先由用户评价文本，再单独处理响应速度与等待展示。

相关验证：codex-lab 执行 `PYTHONPATH=backend .runtime/knowledge-summary/.venv/bin/python -B -m pytest scripts/tests/test_knowledge_poc_summary.py scripts/tests/test_property_filter_poc_observation.py -q --tb=short`，17 项通过。Python AST 语法检查与 `git diff --check` 通过；未重复运行前端构建。

## v3 第二次原始输出

以下是模型原文，未经人工润色：

> 使用文献检索模块，以 polyimide|absorbance 和 CPI 为检索词，并通过反应页查看文献；另用属性筛选模块查看玻璃化转变温度范围。阅读关注点集中在无色透明聚酰亚胺薄膜的光学、热稳定性，以及吸光度数据在反应动力学分析中的作用。
>
> 最值得记住的是，[文献 #1]说明，调整材料结构组合，可以在分子堆积与自由体积之间取得平衡，从而兼顾透明性、耐热性和耐弯折性。属性筛选将玻璃化转变温度限定在158–245 °C，实际查看的[材料 #1] POC-B 演示材料记录为240.0 °C；该记录属于合成测试数据，不是文献实验或验证。
>
> [文献 #2]关注的是用吸光度极值和酸碱度—速率关系解析连续反应中的步骤判定问题，重点在如何解释动力学信号，而不是设计聚合物材料。两篇内容都利用光学测量提取信息，但前者讨论材料性能，后者讨论反应过程分析；目前没有证据表明它们涉及同一材料或实验。
