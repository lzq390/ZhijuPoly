# 15个模块页面统一复核（2026-09-14）

后续状态：本报告保留修复前发现与证据；以下待收尾事项现已处理，见[修复与验收记录](page-unification-completion-20260914.md)。单体正向聚合按后续业务规范保留整体滚动例外。

## 结论

不能继续使用“仅剩清空后切页的结构同步问题”这一结论。该历史问题在当前工作区复测通过；本轮确认3项实际界面／交互问题、1处滚动规范冲突，以及验收脚本覆盖不足。

复核对象为 HEAD `4793ce7` 加当前未提交的页面统一改动，浏览器访问 `http://127.0.0.1:9001`。范围仍为已约定的15个可见模块；不包含实验数据、实验流程 Demo 和 Agent 嵌入工作区。本轮仅复核并更新验收记录，未修改产品代码或原有测试。以下问题均指当前状态，不据此判断引入时间。

## 待收尾事项

### 1. [P1] 知识检索切换模式后，隐藏面板仍覆盖当前内容

- 位置：[knowledge-retrieval.css](../../frontend/src/styles/knowledge-retrieval.css#L242)、[KnowledgeSearch.tsx](../../frontend/src/components/KnowledgeSearch.tsx#L99)。
- 复现：进入知识检索，依次切换“本地知识库 → 在线 → 本地知识库”。390×844、1920×1080、2560×1440均复现。
- 结果：本地标签已选中，但在线工作面仍显示，正文重叠。离开的在线面板具有 `hidden=true`、`inert=true`，计算样式却为 `display:block`、`visibility:visible`、`opacity:1`。
- 原因：`.ks-mode-panel` 的 `display:block` 覆盖浏览器对 `hidden` 的默认隐藏样式；`inert` 不能阻止绘制。
- 收尾：恢复未激活面板不显示，并补三种模式的往返切换检查，验证活动内容、隐藏状态和点击可达性。只改测试选择器不能修复此问题。
- 证据：[浏览器数据](../../.runtime/page-unification/review-20260914/source/report.json)、[1920截图](../../.runtime/page-unification/review-20260914/source/knowledge-local-again-1920.png)。

### 2. [P2] 减少动态效果时，结构反推结果抽屉未接收焦点

- 位置：[StructureWorkbenchPage.tsx](../../frontend/src/components/StructureWorkbenchPage.tsx#L258)、[WorkbenchDrawerShell.tsx](../../frontend/src/components/structure-workbench/WorkbenchDrawerShell.tsx#L84)、[useModalFocus.ts](../../frontend/src/hooks/useModalFocus.ts#L96)。
- 复现：启用 `prefers-reduced-motion: reduce`，进入结构工作台，打开“功能参数”，进入单体逆合成反推，输入 CCO 并运行。请求使用本地空结果／两候选结果夹具。
- 结果：390×844、1440×900下，结果抽屉打开并等待2秒后，焦点仍为 `BODY`；首次 Tab 落到对话框外的“关闭单体反推结果背景”按钮。默认动效对照正常。
- 焦点调用记录显示，首次尝试聚焦关闭按钮时该元素仍为 `visibility:hidden`，之后未成功重试。需处理抽屉可见时机与初始焦点交接，并补减少动态效果下的真实键盘验证；本轮不将未完全定位的时序细节作为最终根因。
- 证据：[复现说明](../../.runtime/page-unification/review-20260914/drawers/README.md)、[手机焦点报告](../../.runtime/page-unification/review-20260914/drawers/focus-probe-mobile.json)、[默认动效对照](../../.runtime/page-unification/review-20260914/drawers/focus-probe.json)。

### 3. [P2] 单体正向聚合的滚动例外与统一规范冲突

- 实现：[monomer-polymerization.css](../../frontend/src/styles/monomer-polymerization.css#L18) 将模块根节点设为 `overflow-y:auto`，正文使用自然高度。
- 实测：单次／批量两种模式在12档视口均随根节点滚动移动标题。例如手机滚动240px后，标题相对工作区从 `(16,20)` 变为 `(16,-220)`；2K从 `(36,30)` 变为 `(36,-210)`。
- **此行为已有后续页面规范依据**：[单体正向聚合规范](../../design-system/polyprop/pages/monomer-polymerization.md#L5) 明确要求“页面整体滚动，标题随页面滚动”，第19行也指定由根节点滚动。不能将其直接认定为无意回归并撤销。
- 但本任务方案及 [MASTER](../../design-system/polyprop/MASTER.md#L103) 仍要求15页固定页头，且不允许页面规范覆盖公共页头。两处口径需要统一。
- 收尾：若保留后续业务例外，则在公共规范和验收中明确该例外；若仍要求全部15页固定，则需要将本页滚动迁回正文。尚未完成这一步前，不能宣称“15页固定页头全部通过”。
- 证据：[192个页面状态的滚动报告](../../.runtime/page-unification/review-20260914/scroll-report.json)。

### 4. [P2] 现有浏览器验收会漏掉根节点滚动及部分交互问题

- [verify-page-consistency.mjs](../../frontend/scripts/verify-page-consistency.mjs#L75) 只枚举 `root.querySelectorAll("*")`，未覆盖模块根节点及影响标题的祖先滚动。
- 标签切换检查只测标题几何，没有断言离开面板不可见、当前内容与标签匹配。当前原脚本在知识检索处已发生点击被覆盖面板阻挡的失败，不能仅通过缩小选择器将其记作修复。
- 错误主题检查目前聚焦服务状态，未覆盖任务中心的状态徽标；抽屉验证也需要加入从初始加载就开启减少动态效果的焦点交接。
- 收尾：补以上可复现行为检查，并按统一后的滚动规范处理模块例外。此次诊断脚本保存在证据目录，尚未合入正式验收脚本。

### 5. [P3] 单体 MD／DFT 任务状态尚未完全采用公共主题

- 位置：[monomer-dft.css](../../frontend/src/styles/monomer-dft.css#L588)、[monomer-md-simulation.css](../../frontend/src/styles/monomer-md-simulation.css#L638)。
- 用本地失败任务夹具进入两页任务中心后，两者失败文字实际计算色均为 `#b91c1c`，而 [workbench-theme.css](../../frontend/src/styles/workbench-theme.css#L18) 的公共错误文字色为 `#991b1b`。MD状态还保留旧粉色渐变和边框。
- 这些样式确实作用于可见任务状态，属于普通状态语义，不是图表或原子元素的科学语义色。DFT浅错误背景本身已与公共值相同，不应将其误报为颜色差异。
- 收尾：将任务状态的文字、背景和边框映射至公共角色，同时检查成功、取消和取消中等同用途状态。保留现有控件尺寸。
- 证据：[样式计算值](../../.runtime/page-unification/review-20260914/source/report.json)、[DFT截图](../../.runtime/page-unification/review-20260914/source/monomer-dft-failed.png)、[单体MD截图](../../.runtime/page-unification/review-20260914/source/monomer-md-simulation-failed.png)。

## 已通过的复核

| 检查 | 当前结果 |
|---|---|
| 15页首屏标题坐标、字号、行高、字重及颜色 | 12档视口通过；另对单体聚合两种模式分别检查，合计192个初始状态坐标通过 |
| 根节点／祖先／正文滚动 | 192个状态中24项标题移动，全部为单体聚合两种模式×12档；其余被检查的可滚动区域未发现标题移动 |
| 页面外层横向溢出 | 上述192个状态均未发现 |
| 扩展几何与子状态矩阵 | 记录696项成功检查、12项失败；失败均为单体聚合根节点滚动 |
| 数据库分析五类数据集、MD／DFT主要标签 | 扩展矩阵覆盖；任务徽标颜色问题由额外任务夹具确认 |
| 高通量S0–S6标题与滚动 | 1920×1080、2560×1440、390×844、2560×1119通过 |
| 抽屉开关、拖拽、打断反转、编辑器保留、标题稳定 | 默认动效下1440×900、1024×768、390×844、2560×1440通过 |
| 知识详情／PolyTAO抽屉长内容 | 四档视口真实滚动通过，未发现额外标题偏移或裁切；减少动态效果的焦点问题单列 |
| 结构工作区原有功能脚本 | 通过，包含此前失败的 `navigationFallback`、`emptyRoundTrip` |
| 结构导航脚本 | 10项通过，包含超时、取消、迟到结果隔离 |
| 清空后不等待即切页 | 正常／减少动态效果各重复2次，4次通过，旧结构未重新出现 |
| 组件／Hook测试 | 101个文件、946项通过 |
| 生产构建及差异检查 | `npm run build`、`git diff --check`通过 |

视口为：1920×1080、2560×1440、1440×900、1024×768、390×844、899×900、900×900、901×900、1999×1120、2000×1119、2000×1120、2560×1119。

**统计边界：**扩展矩阵使用临时脚本，加入根节点滚动，并暂将知识检索的标签选择器限定在活动面板，以继续检查其它模块；这不代表知识检索重叠问题通过。单体聚合在根滚动断言失败后，该模块后续检查被跳过，因此不能将696项成功记录等同于原先764项完整验收。单次／批量的标题及根滚动另由192状态探针补齐。未提交真实计算任务；异步结构交互和任务状态使用本地夹具。

## 证据与复现

本轮产物在 [review-20260914](../../.runtime/page-unification/review-20260914/)：

- [完整几何／子状态矩阵](../../.runtime/page-unification/review-20260914/pages-full/report.json)及[临时脚本](../../.runtime/page-unification/review-20260914/verify-pages-adapted.mjs)。
- [根与祖先滚动探针](../../.runtime/page-unification/review-20260914/probe-scroll.mjs)、[结果](../../.runtime/page-unification/review-20260914/scroll-report.json)。
- [知识检索及任务主题探针](../../.runtime/page-unification/review-20260914/source/review-source.mjs)。
- [结构复测命令和结果说明](../../.runtime/page-unification/review-20260914/structure/README.md)。
- [抽屉复测命令和结果说明](../../.runtime/page-unification/review-20260914/drawers/README.md)。
- [组件测试日志](../../.runtime/page-unification/review-20260914/tests.log)、[构建日志](../../.runtime/page-unification/review-20260914/build.log)。

在仓库根目录可重跑专项探针：

```bash
node .runtime/page-unification/review-20260914/probe-scroll.mjs
node .runtime/page-unification/review-20260914/source/review-source.mjs
```

这些本机诊断脚本绑定上述9001服务和现有Chromium／Playwright安装路径；完整命令及夹具见各脚本和专项README。证据目录不属于产品资源包。
