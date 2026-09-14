# 15个模块页面统一修复与验收（2026-09-14）

本轮完成[复核报告](page-unification-review-20260914.md)中的收尾事项，并将原页面统一改动一起提交。范围仍为15个可见模块，不包含实验数据、实验流程 Demo 和 Agent 嵌入工作区。

## 修复结果

| 复核事项 | 最终处理 |
|---|---|
| 知识检索模式叠层 | 隐藏面板明确使用 `display:none`；本地／在线／PDF往返不再覆盖当前内容，保留各模式草稿 |
| 减少动态效果下结构反推抽屉焦点丢失 | 初始聚焦等待目标实际可见；关闭、切页、隐藏或卸载取消待执行聚焦，保留侧栏导航能力 |
| MD／DFT任务状态主题遗漏 | 排队／已取消、运行、取消中、完成、失败分别使用公共 neutral／info／warning／success／danger 文字、浅底和边框角色 |
| 单体正向聚合滚动规范冲突 | 保留后续业务要求的单次／批量整体滚动；MASTER与页面规范明确它是唯一例外，其余14页固定页头；15页初始标题几何一致 |
| 验收覆盖不足 | 正式脚本检查根节点、祖先和正文滚动、实际隐藏面板、任务状态、减少动态效果焦点；每档视口保存结果并即时记录失败 |

另外修复当前高通量S1错误CSV、加载与成功状态的公共配色，错误区聚焦时保留错误边框。科学目标类别色保持原语义。

## 验收结果

| 验证 | 结果与证据 |
|---|---|
| 15页×12档视口、首屏／滚动／业务子状态 | 汇总788项检查通过，180个首屏状态完整；[坐标表](../../.runtime/page-unification/fix-20260914/pages-final/coordinates.md)、[汇总JSON](../../.runtime/page-unification/fix-20260914/pages-final/report.json) |
| 单体正向聚合单次／批量及服务失败 | 服务就绪后重新获取根节点，12档视口共92项检查通过；[结果](../../.runtime/page-unification/fix-20260914/polymerization-final/report.json) |
| 结构反推抽屉焦点 | 390／1440×正常／减少动态效果×空／两候选共8组通过；包含Tab循环、关闭恢复、重开、快速关闭、打开中切页和任务不重放；[结果](../../.runtime/page-unification/fix-20260914/drawers/results.json) |
| 高通量S1错误、聚焦、加载与恢复 | 390／1920／2560三档通过；[结果](../../.runtime/page-unification/fix-20260914/high-throughput/report.json) |
| 全量组件／Hook测试 | 102个文件、950项通过；[日志](../../.runtime/page-unification/fix-20260914/tests-final.log) |
| 生产构建 | `npm run build`通过；[日志](../../.runtime/page-unification/fix-20260914/build.log) |
| 源码与差异 | 浏览器脚本语法、`git diff --check`及暂存差异检查通过 |

12档视口为1920×1080、2560×1440、1440×900、1024×768、390×844、899×900、900×900、901×900、1999×1120、2000×1119、2000×1120、2560×1119。矩阵包含数据库分析五类数据集、MD／DFT主要标签、知识模式往返，以及四档代表视口的高通量S0–S6。任务响应使用本地夹具，没有提交真实计算任务。

当前页面变化之外的结构同步逻辑未改动。此前清空后立即切页的问题已在修复前复核中通过结构工作区、导航和4次立即清空探针，见[结构复核记录](../../.runtime/page-unification/review-20260914/structure/README.md)。独立的DFT草稿来源改动保留在工作区，未混入本次提交；不将未覆盖的“无效草稿进入DFT”宣称为已验收场景。

## 验证中的时序修正

- 首轮组件检查有一次MD曲线断言失败。数据概览可以先于提交完成后的结果快照显示，测试原来立即点击仍禁用的曲线按钮；现改为等待按钮可操作。独立复测及后续全量950项通过。
- 首个长矩阵进程被终止（退出143），没有完整报告，因此未用它判定验收通过。后续拆为三组，保存每档检查点。
- 分组矩阵捕获了单体聚合的旧标题节点被卸载：服务能力返回后，页面增加批量表单的上层组件并重新挂载工作区。脚本原来跨帧持有旧节点，误把其空坐标当作标题移动。现保留加载前后两次几何检查，并在服务就绪后对最终根节点执行滚动，没有放宽坐标或滚动断言。
- 汇总报告采用三组中其它14页的结果，并以最终12档单体聚合报告完整替换该模块的旧样本；不把原失败记录直接改为通过。原始分组报告与被替换失败详情均保留。

## 可复现命令

在`frontend`目录，设置本地前端与浏览器路径后执行：

```bash
PAGE_BASE_URL=http://127.0.0.1:9001 npm run test:pages
DRAWER_BASE_URL=http://127.0.0.1:9001 npm run test:pages:focus
PAGE_BASE_URL=http://127.0.0.1:9001 npm run test:pages:states
npm test -- --maxWorkers=2
npm run build
git diff --check
```

长矩阵可用`PAGE_VIEWPORT=1920,1440,1024,899`、`2560,1999,2000`、`390,900,901`分组，并设置不同`PAGE_ARTIFACT_DIR`。`PAGE_PATH=/monomer-polymerization`可专项检查全部12档的单次／批量模式。浏览器路径与其它环境变量见[使用说明](../page-unification.md)。

代表截图：[知识往返后的手机页](../../.runtime/page-unification/fix-20260914/pages-narrow/knowledge-round-trip-390-844.png)、[DFT任务状态](../../.runtime/page-unification/fix-20260914/pages-desktop/monomer-dft-task-states-1920-1080.png)、[高通量错误态](../../.runtime/page-unification/fix-20260914/high-throughput/high-throughput-error-390-844.png)。所有运行产物保存在本地`.runtime/page-unification/fix-20260914/`，不属于产品资源包。
