# 提交后的组件化画板加载分析

2026-09-14。已先完成验收并提交 `4793ce73f1de4a2d240c48412a95e0a98696607f`；修复与验证范围见 [SDK 提交验收](ketcher-sdk-compatibility.md#提交验收2026-09-14)，维护限制见[组件化说明](ketcher-componentization.md)。以下是提交之后的隔离实验和下一步建议，未把新性能原型写入产品源码或部署。

这是以该提交为基线的历史实验记录，后续优化仍为建议。配套 JSON 中 `finalVerification.preexistingWorkingFilesUnchangedAfterCommit` 原值为 `1000`，未找到可复核其含义的记录，文档整理时改为 `null`（未知），不据此声称原有工作区文件已校验未变。20个正式样本、性能统计及原始证据摘要保持原值；本地证据路径用于追溯，不随仓库交付。

**组件化可以沿用 iframe 的独立 SDK 产物和启动流程，并省掉一层文档加载。本次控制实验中确实略快；当前9001的主要差距仍来自启动依赖和 SDK 工作量，而不是 div 容器本身。**

## 1. 新实验：相同 SDK 放进 div 和 iframe

为分离容器因素，复用同一份非 lazy 的 Ketcher 3.8/r3 生产 SDK，包含相同的 React/ReactDOM 19.2.4、Macro、Worker 和样式。两臂访问同一来源、同一 SDK URL、同一制品字节；SDK 依赖的 11 个制品和 4 个补丁摘要与提交候选一致。使用此前已构建的 SDK 包，不宣称它是本次提交的新可复现构建。

- div 直接运行公共测试页面；iframe 内运行同一份页面，外层只有全视口 iframe。去掉外层额外探针请求，只在 SDK 所属文档运行相同探针。
- 两边均使用提交中的真实 NativeEditorSession、StructureWorkspace、KET 检查及两次 80ms 提交等待。没有把原生的正确性检查删掉后再比较。
- Chrome 151、1440×900、正常动画、持久化缓存、40ms 延迟、50Mbps 下载、10Mbps 上传，无 CPU 限速。各预热一次，再交替正序/倒序普通刷新，各10次。
- 实際 SVG 坐标均为 `x=47, y=80, width=1346, height=784`，文档可见。子文档延迟探针约45ms，并在已限速的 CDP 会话下确认 frame ID；吞吐量为 CDP 配置值，没有额外做带宽校准。
- UI 就绪要求实际可见 SVG、workspace ready 和公共清空按钮可用；随即等待首次真实空 SMILES 导出。计时后，每个样本都实际导入 CCO、鼠标拖动氧原子并核对 KET 坐标、导出、清空，最后确认唯一 Worker 回收到零。

| 相同生产 SDK 的最小测试页面 | UI 就绪 P50 / P95 | 首次真实 SMILES 导出完成 P50 / P95 |
| --- | ---: | ---: |
| div | 1.884 / 1.959s | 2.159 / 2.235s |
| 同源 iframe | 1.979 / 2.189s | 2.249 / 2.462s |

20个正式样本全部通过。本报告P50/P95均使用最近秩统计。div 的 UI P50 约快95ms（4.8%），首次导出完成 P50 约快89ms（4.0%）。按同轮配对计算，UI差值最近秩P50为94.5ms，10对中9对是div更快；按偶数样本中间两项平均定义的中位数则为107.1ms。P95为各10次中的最大值，不表示长期尾延迟保证。

这支持“相同条件下组件容器可以略快”的判断，**不能把上表的1.884秒当成当前完整9001的新成绩，也不能拿它直接与另一批9000数据相减。** 本实验没有业务 App、宿主开发 React、业务 HMR 和模块切换动画，也没有模拟跨域 iframe 的进程隔离。95ms差值包含额外文档加载及运行波动，不全是某个 iframe API 的纯CPU开销。

[正式统计与全部逐样本指标](verification/ketcher-component-boundary-20260914.json)；原始请求和时间线（本地证据：`/tmp/nexpoly-ketcher-component-boundary-20260914/formal-control/results.json`）。

## 2. 新确认的限制：提前 import 已经做了，完整 SDK 导入仍挡在 Worker 前面

当前 [main.tsx:8](../frontend/src/main.tsx) 触发 `preloadStructureEditor()`，它等待 [loadKetcherRuntime](../frontend/src/components/structure-workbench/preloadReactStructureEditor.ts)，后者执行动态 import。其静态依赖包括 React SDK、Standalone 和 CSS。因此预加载已经包含模块求值，不能再把“提前执行同一次 import”算作尚未实现的大收益。

尚未提前的是 [KetcherReactRuntime.tsx](../frontend/src/components/structure-workbench/KetcherReactRuntime.tsx) 在 SDK 创建请求中调用 provider，进而创建服务和 Worker。源码链为：

```text
初始路由 → 动态 import SDK/CSS
可见画板挂载 → SDK effect
  ├─ createApi → createStructService → new Worker → info（异步）
  └─ 2D / Macro UI 初始化 → onInit → 文档恢复
恢复、必要服务调用与状态检查完成 → shared ready → 真实导出
```

Worker准备与UI工作并行，当前样本中 `onInit` / 恢复开始可以早于首次Worker回复；恢复中的服务调用仍可能等待它。不能把这些区间当成互不重叠的串行成本。

本次 div 对照的逐样本区间 P50：

| 区间或指标 | 结果 | 对优化的含义 |
| --- | ---: | --- |
| SDK import 开始 → 完成 | 659ms | 暖缓存仍不能省掉模块导入过程；区间含获取、解析、求值，不能全称为纯CPU |
| SDK import 完成 → Worker 创建 | 59ms | 在这个最小页面中，等完整 SDK import 后再创建服务，最多前移这一小段启动间隔 |
| Worker 创建 → 首次回复 | 744ms | 仍需启动/服务/调度；与主线程工作重叠，不能整段当作可删除时间 |
| 文档恢复 | 263ms | 包含原160ms提交等待及其他检查 |
| UI就绪后的首次 getSmiles | 274ms | 同容器变化没有消除化学转换开销 |
| 暖刷新传输量 / 请求数 | 2,331字节 / 12 | 网络字节少，初始化仍需接近两秒 |

iframe 对应导入约673ms、导入后创建Worker约60ms、Worker回复约745ms，主要内部成本接近。SDK资源均复用缓存。由此得到的实施判断是：**仅在 `loadKetcherRuntime().then(...)` 中提早创建 service 适合作为低风险对照，不能据此承诺几百毫秒收益。** 当前完整9001的这个间隔还应独立打点；59ms是本次最小页面的边界，不是9001的实测上限。

真正的轻量服务入口必须绕开完整 React/Core/编辑器导入图。单独 `import('ketcher-standalone')` 仍会静态引入 `ketcher-core` 和内嵌的大型 Base64 Worker，不能仅凭包名认为已经轻量化。

## 3. 四项原因应如何排序

此前的全量业务加载、延迟 SDK 请求和未压缩资源已经有对应优化，本次提交包含这些实现。它们解释了最初的差距，不能在已优化基线上重复计算收益。剩余四项应按因果关系判断：

| 方向 | 评估 | 下一步 |
| --- | --- | --- |
| 隐藏 Macro 初始化 | 已有较强实测支持，是主要成本 | 完成真实按需初始化，覆盖首次切换、HELM/宏KET、formatter、自定义单体及失败重试 |
| Worker 握手/服务等待 | 处于关键路径，但受主线程长任务影响 | 优先提前唯一Worker的启动，并减少主线程阻塞；不把它与Macro/React收益相加 |
| 开发 React/运行时 | 会放大成本；收益取决于剩余工作量 | 独立生产 SDK 是可选交付边界，业务源码继续HMR；不能指望只换root就胜出 |
| 文档恢复与确认 | 真实次级成本，影响正确性 | 用可验证的新空会话快速路径或可靠提交信号减少重复工作，保留结构与revision保护 |

上一批完整应用对照中，仅延迟 Macro 已把 UI P50 从2.704秒降到2.179秒；再加独立生产root为2.133秒，增量只有约47ms，仍慢于同批9000的1.409秒。首次 `getSmiles` 从约295ms降至约19ms，说明减少未使用单体库工作也改善实际操作响应。数字来自[原批次报告](ketcher-refresh-beyond-iframe-analysis.md)，不与本次最小容器数据混算。

本次没有重新证明这四项各自占比，也没有证明四项优化完成后一定超越9000；它新增的因果证据是：容器本身没有解释秒级差距，完整SDK导入仍限制服务能够多早启动。

## 4. 如何仿照 iframe 的流程，同时保留组件化

可把编辑器做成独立版本的 SDK 产物，由业务组件调用 `mount(container, options)` 和 `dispose()`；宿主负责布局、文档与导航，SDK负责自己的生产运行时和UI。React支持在DOM节点建立root及显式卸载，但多root本身不是新的计算线程，也不是自动的性能优化。[React createRoot 官方接口](https://react.dev/reference/react-dom/client/createRoot)

下一阶段建议分开验证三步：

1. **先完成已证明有效的 Macro 按需化。** 当前原型仍存在 HELM 导入后直接切回小分子模式的 Promise 挂起等兼容性缺口，不能直接上线。保持完整 KET/文本/标注和宏结构导入语义；在实际需要时才建 Macro 界面、解析单体库。转换时按格式语义使用库，不能只因当前UI处于小分子模式就丢弃它。
2. **以唯一服务接管为模型，比较两种提前启动。** A组在现有SDK import完成后创建service并预热 `info()`；B组使用不依赖 React/Core 的Worker资源入口，使其与业务App及2D SDK加载并行。第二组需要新的资源边界，工程范围更大。先在隔离实例测单项与组合，保留正常动画和实际ready检查。
3. **依据数据再确定生产SDK打包和文档快速路径。** 避免重新形成一个25MB主包堵住启动；结合按需能力分块与压缩、缓存、版本清单。空会话必须确认当前session/revision有效、完整KET为空且没有待处理写入；URL `moll` 自动导入、文字/图形/标注等反例需要覆盖。非空文档继续保证坐标和内容恢复。

服务接管的边界可以复用现有 provider 和 NativeEditorSession，无须提前创建不可见画板：

```text
初始画板路由
  ├─ 业务 App / 可见容器准备
  ├─ 必需2D SDK与样式准备
  └─ 轻量化学服务准备（唯一Worker）
                 ↓
可见会话在 createStructService 请求时接管同一实例
                 ↓
结构恢复、订阅与正确性确认 → 可绘制、可导出
```

- 接管发生在实际 `createStructService()` 中；先检查session，再原子取走预热实例并交给 `session.ownService()`。不在render/useMemo中抢占资源，避免StrictMode废弃渲染破坏所有权。
- 接管前由启动租约管理；取消导航、超时、HMR、未被接管、初始化失败都要销毁。接管后归可见会话独占，退役后不放回池；provider重建另建实例。
- 仅预热无需ketcherId的 `info()`，配置与SDK默认值一致。失败Promise可重试；提前启动Worker时，接管前的错误/消息也不能丢失。普通页面和iframe回退引擎不创建预热Worker。
- 预热完成不提前点亮公共按钮。验收记录接管前后实例身份、Worker数量、最终ready、真实CCO/KET操作以及初始化中离开、快速导航、失败重试与超时。

仍以此前共同预算作为待验证目标：Worker开始≤450ms、服务路径≤600ms、必需2D模型/UI≤1050ms、后续文档确认≤200ms、首次导出≤50ms。满足这些并行约束才可能把UI压到约1.25秒、真实导出完成约1.30秒，并形成相对旧9000的余量。这是工程目标；不能把不同样本P50相加作为预测，更不能通过跳过原提交保障实现。

## 5. 保留的失败、实验开销与交付范围

- 首次冒烟在清空后过早销毁会话，尚未结束的按钮 `clear + settle` 操作报AbortError。修复测试等待顺序，所有错误仍保留；新冒烟两臂通过。不是通过忽略AbortError让结果通过。
- 第一组20次也全部通过，但iframe外层诊断脚本额外产生一次阻塞请求。该组完整保留为敏感性对照；移除该测量开销后的 `formal-control` 才是上表主结果。没有从旧组数字中手工减去40ms。
- 本轮没有覆盖冷访问、禁缓存、低端CPU、全部业务页性能或跨域iframe；CCO真实操作在空文档刷新计时终点之后验证。
- 所有实验服务和浏览器已停止。9000/9001容器ID、镜像和启动时间与此前记录一致，两个端口均HTTP200。提交中的review修复可能随现有源码绑定触发9001 HMR，但没有重新构建或重启部署。
- 提交后仅新增本分析文档与验证汇总；新性能实验仍在 `/tmp/nexpoly-ketcher-component-boundary-20260914`，没有新增产品优化代码。

完整产物：[统计与溯源摘要](verification/ketcher-component-boundary-20260914.json)、主结果（本地证据：`/tmp/nexpoly-ketcher-component-boundary-20260914/formal-control/results.json`）、额外探针敏感性组（本地证据：`/tmp/nexpoly-ketcher-component-boundary-20260914/formal/results.json`）、复现脚本说明（本地证据：`/tmp/nexpoly-ketcher-component-boundary-20260914/README.md`）。
