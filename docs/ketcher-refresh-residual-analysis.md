# 9001 画板刷新剩余性能差距复核

核验日期：2026-09-14。9000、9001 均只读；本轮未修改产品源码、重启这两个服务或部署优化。诊断改动全部位于独立 `/tmp` 副本。

**9001 仍明显慢于9000，已复现；上次优化仍然有效。剩余重点已从全量业务模块下载，转向隐藏大分子编辑器的主线程长任务、由此拖延的Worker初始消息投递，以及开发React和文档恢复成本。仅继续压缩、预取，不能解决当前大部分差距。**

## 1. 当前环境与同源码对照

工作区424个已测前端文件与9月11日最终冻结版本逐一哈希一致，源码摘要为 `2d0ac6ef2c4cb45bc6e9398d4ccdabe3a9f08340adf7de5eaaf23e9e8f66ee01`。9001实际HTML保留三个开发入口预加载，页面懒加载与SDK预取仍在运行；容器、镜像、启动时间与上次交付后相同。9000仍是原镜像。详见[当前源码核验](/tmp/nexpoly-refresh-residual-20260914/current-source-verification.json)。

本轮复用该冻结源码已验证的两种生产构建，分别启动临时预览5922、5923，避免用不同业务源码解释差距。主测量采用结构工作台，四个版本各测本机及模拟远程普通刷新10轮，共80个正式样本；全部通过空文档读取、CCO导入/模型提交/SMILES导出和清空验证。

| 环境 | 画板运行方式 | 本机 P50 / P95 | 模拟远程 P50 / P95 |
| --- | --- | ---: | ---: |
| 9000 实际生产 | iframe，Ketcher 3.7 / React 18.2生产运行时 | 1.345 / 1.557s | 1.438 / 1.509s |
| 9001 实际开发 | 原生组件，Ketcher 3.8 / React 19.2.4开发运行时 | 2.455 / 2.698s | 2.728 / 2.819s |
| 当前源码原生生产构建 | 原生组件，Ketcher 3.8 / React 19.2.4生产运行时 | 2.005 / 2.152s | 2.333 / 2.541s |
| 当前源码iframe生产构建 | 当前宿主，iframe / Ketcher 3.7 | 1.156 / 1.248s | 1.537 / 1.704s |

模拟远程为40ms延迟、50Mbps下载、10Mbps上传，无CPU限速；Chrome151.0.7922.34、1440×900、正常动画。每种网络使用持久化浏览器配置，先预热四个来源，再交替正序/倒序普通刷新，保留全部样本。P50/P95采用最近秩，10样本的P95即最大值，不能当成长时间运行的尾部保证。冷访问在此仅用于预热，不纳入新的冷启动验收。

就绪要求实际可见画布、公共清空按钮可用；当前两个引擎还要求共享状态ready，旧生产沿用其实际可操作状态。旧版内部也会读取SMILES检查，并非只看iframe的load事件。计时后才导入CCO并等待模型达到3个原子再导出；本轮不以导入Promise返回代替实际模型提交。

**实际9001比9000远程暖刷新多约1.29秒，约慢90%。** 同源码原生开发改为完整生产构建仅改善约0.40秒，本机约0.45秒；因此剩余差距不能全部归因Vite。当前宿主下，两种生产画板仍差约0.80秒，说明SDK版本、运行集成与就绪路径同样重要。这个对照没有控制SDK版本和补丁，不能把0.80秒全部称为“组件化的固有损耗”。

生产/开发对照的净差也不是严格的开发CPU成本上限：两者模块组织和加载顺序不同，网络、主线程与Worker存在重叠，不能据此承诺独立SDK方案必然节省相同时间。

## 2. 剩余差距发生在哪里

下表为相对导航开始的阶段时间P50，属于实际端口的正式样本：

| 模拟远程指标 | 9000 | 9001 |
| --- | ---: | ---: |
| App出现 | 262ms | 728ms |
| Worker创建 | 673ms | 1210ms |
| 画布建立 | 1080ms | 1322ms |
| 主线程首次收到Worker回复 | 1254ms | 2396ms |
| 工具可用 / ready | 1438ms | 2728ms |
| 最大主线程长任务 | 194ms | 723ms |
| 同源请求数 | 12 | 95 |
| 同源实际传输量 | 2.1KB | 13.2KB |

9001的SDK暖缓存正常，业务源码/CSS请求为64个，上轮优化前约193个。业务模块请求与缓存重验证仍使App出现较晚，但**画布出现只比9000晚约0.24秒，完整可用却晚约1.29秒**。需要优先解决画布建立之后的初始化尾段。

同源码生产对照也有类似现象：原生与iframe画布出现分别约1.362秒、1.336秒，几乎接近；可用时间分别为2.333秒、1.537秒。继续只盯下载开始或“看见SVG”会误判用户真正等待的时间。

### 2.1 小分子画布建立后，还要等隐藏的大分子编辑器

[当前SDK入口](/data/lzq/gith/nexpoly-dev/frontend/src/components/structure-workbench/KetcherReactRuntime.tsx:20)使用完整的Ketcher `Editor`。本地3.8 SDK的行为是：

- 小分子实例创建并设置`ketcherId`后，才触发内部宏分子模块的动态导入。
- 大分子编辑器容器默认`display:none`，仍会挂载、建立CoreEditor、解析单体库并构建UI。
- 最外层`onInit`等待小分子、大分子编辑器都存在，宿主此时才开始恢复共享文档。

对应实现见[ketcher-react Editor](/data/lzq/gith/nexpoly-dev/frontend/node_modules/ketcher-react/dist/index.js:37610)、[完整onInit条件](/data/lzq/gith/nexpoly-dev/frontend/node_modules/ketcher-react/dist/index.js:37675)。这是真实的隐藏功能初始化依赖，并非仅有CSS遮挡。

独立诊断副本添加`performance.mark`后，严格模式5个暖样本中：小分子初始化完成到宿主onInit，中位数约**865ms**；其中宏模块求值完成到宿主onInit约734ms。单体库首次解析/转换约216ms，其余还有React UI、调度与effect处理。这些区间与Worker初始化部分重叠，不能全部当作可直接节省的时间。

`disableMacromoleculesEditor`也不是直接修复：当前SDK该选项只影响切换按钮和onInit条件，隐藏宏组件的挂载条件仍是`ketcherId`。单纯开启这个选项会改变功能与就绪语义，却仍可能承担宏初始化CPU成本。

### 2.2 开发React开销真实存在；StrictMode只解释其中一部分

9001容器实际预构建依赖包含`react.development.js`和`react-dom-client.development.js`，旧iframe包含React18.2生产运行时。生产iframe本来就是React应用，迁移并非从“非React”变成React。

独立CPU采样中，实际9001的`createElement / jsx / jsxs / jsxDEV`自身采样时间合计约475ms；诊断副本约468ms。它们包含正常元素构建与开发检查，且覆盖宿主和SDK，**不等于可以消除475ms**。但结果支持将SDK及其React运行方式作为优化重点。Redux不可变检查的`trackProperties / detectMutations`采样接近零，本轮没有证据将它列为主要瓶颈。

还需修正上轮“一个Worker”的解释边界：它只能证明没有重复建立两个Worker，不能证明SDK内部所有初始化只执行一次。

本轮隔离打点确认，每次严格模式刷新，大分子CoreEditor会创建→销毁→重建；关闭严格模式后只创建一次。小分子SDK使用另一个React root，并有取消围栏，Worker始终只有一个。React开发模式额外渲染与effect重放的机制见[React官方StrictMode说明](https://react.dev/reference/react/StrictMode)。

| 独立诊断，远程暖刷新，各5轮 | StrictMode | 关闭StrictMode |
| --- | ---: | ---: |
| ready P50 | 2.735s | 2.551s |
| 最大长任务P50 | 722ms | 573ms |
| 大分子Core创建次数 | 2 | 1 |
| Worker数 | 1 | 1 |

关闭StrictMode约改善184ms，仍远未追平9000。严格模式首次Core构造中位数约226ms，第二次约10ms，第二次复用了单体库缓存；不能说昂贵解析完整执行了两遍，也不建议把关闭全局StrictMode作为最终方案。

### 2.3 文档恢复不仅是160ms定时器

[宿主onInit](/data/lzq/gith/nexpoly-dev/frontend/src/components/structure-workbench/ReactStructureEditor.tsx:76)之后才执行[workspace恢复](/data/lzq/gith/nexpoly-dev/frontend/src/structure/workspace.ts:154)。新空文档也执行清空、KET导出确认、两个80ms提交等待，随后订阅结构变化并发布ready。

独立严格模式诊断的恢复阶段中位数约514ms，其中两个提交等待约160ms，其余包括清空/转换、服务响应、KET确认和调度。旧生产对空共享文档读取SMILES成功后便可开放编辑；当前实现承担了更多恢复和一致性保证。

[清空结果校验](/data/lzq/gith/nexpoly-dev/frontend/src/structure/nativeSession.ts:122)与[提交等待](/data/lzq/gith/nexpoly-dev/frontend/src/structure/editor.ts:108)不能直接删除。应研究确定的新空会话是否能减少重复清空、复用服务初始化结果，以及可靠的实际提交完成条件。非空文档、布局恢复、异常和快速导航必须维持同样正确性标准。250ms居中和首次自动保存均在ready之后，不属于本轮ready关键路径。

### 2.4 关键新增原因：主线程长任务还挡住了Worker的初始消息

独立Chrome时间线没有改写Blob代码，并按消息traceId关联发送与实际处理，发现一个此前阶段计时没有区分的问题：**Worker大部分时间已经空闲，第一条请求仍在等待投递。**

| Worker脚本求值完成→首次消息处理，专项trace各1次 | 9000 | 9001 |
| --- | ---: | ---: |
| 总间隔 | 118.5ms | 706.1ms |
| Worker idle采样时间 | 20.5ms | 624.5ms |
| 非idle采样时间 | 98.0ms | 81.6ms |

9001同期主线程有769.8ms长任务，它结束约4.1ms后，Worker才处理第一条消息。两个Worker的后续实际初始化任务分别约98ms、81ms，结果不支持“9001的WASM多计算了600ms”。

Chromium **151.0.7922.34** 的实现解释了这个现象：Worker脚本尚未被父线程确认完成时，早期`postMessage`进入队列；Worker求值完成后，把通知投递给父线程；父线程执行该通知后才放行早期消息。因此，即使Worker线程已空闲，父线程上的大任务也会拖延第一条请求。见[消息排队及放行实现](https://raw.githubusercontent.com/chromium/chromium/refs/tags/151.0.7922.34/third_party/blink/renderer/core/workers/dedicated_worker_messaging_proxy.cc)与[Worker完成通知回到父线程的实现](https://raw.githubusercontent.com/chromium/chromium/refs/tags/151.0.7922.34/third_party/blink/renderer/core/workers/dedicated_worker_object_proxy.cc)。

源码确认这一机制，时间线中的长任务与idle区间与其一致；trace没有直接命名父线程的`DidEvaluateScript`回调，不能逐微秒把所有空闲归给单个内部任务。两份trace中的CPU profiler自身还各有约100ms启动开销，所以这组数据只用于解释原因，不与80个普通刷新样本混算。完整关联和CPU归因见[Worker启动分析](/tmp/nexpoly-refresh-residual-20260914/timeline/worker-startup-analysis.json)。

这使优化方向更明确：减少或拆分SDK主线程长任务，也能改善Worker启动后的请求等待。把大分子模块更早下载，并不保证主线程能更早处理启动通知；不能把“已经创建Worker”视为已经建立有效并行。

### 2.5 iframe并没有消除Worker和WASM成本

旧、新SDK均采用Base64→Blob→Worker，并内嵌WASM；两者解码后的Worker脚本分别约11.69MB、11.66MB。刷新都会新建Worker，不能把这一点视为组件化新增问题。

缓存解释也需要按实际响应修正：9000的iframe主JS为`no-cache`并支持gzip，9001预构建SDK则是一年`immutable`。事实不支持“旧iframe强缓存更好”。iframe也不自动获得独立主线程；其主要优势来自当时独立生产应用的运行边界，以及不同的SDK与就绪流程。

本轮额外Worker打点显示，暖样本中的`WebAssembly.instantiate`约26–28ms；它只是实例化API区间，不能替代整个Worker脚本解析、模块初始化和消息处理时间。此前“Worker创建→首次收到回复”约1.2秒是复合时段，不能全部称为WASM编译。

## 3. 哪些优化值得继续

又做了一组内部宏模块提前导入实验，保持StrictMode、挂载、完整onInit和文档恢复条件不变，各5轮远程暖刷新：对照2.707秒，提前导入2.742秒，最大长任务691→693ms。本轮未显示收益，差异也不足以判定长期退化。**不建议继续靠增加预取入口作为下一阶段主方案。**

| 方向 | 本轮证据与预期作用 | 实施边界 |
| --- | --- | --- |
| SDK连同匹配的生产React/ReactDOM在当前div建立独立root | 直接针对开发React热点；完整生产对照观测净改善约0.40–0.45s，独立SDK收益仍须单测 | 业务源码继续HMR；通过普通对象、回调和dispose连接宿主，保留取消、错误、portal、CSS和资源版本管理 |
| 大分子编辑器真正按首次使用初始化 | 当前隐藏宏功能进入小分子就绪路径，其长任务还会挡住Worker初始消息；有超出单纯开发构建优化的空间 | 明确小分子与大分子能力就绪，保留模式切换和宏结构导入；需SDK改造，不能提前调用旧onInit冒充完成 |
| 减少恢复阶段冗余操作 | 约514ms阶段可检查新空会话重复清空、重复info调用及提交检测 | 用可靠的状态/提交证据替代冗余等待；保留失败、取消、布局和内容验证，不能直接节省整段514ms |
| Worker/WASM拆为独立资源 | 减少大字符串处理、冷传输和资源维护成本 | 暖刷新没有秒级收益证据；当前binaryWasm入口未含r3生命周期补丁，不可直接切换入口 |

仅把SDK预打包、但将React全部external，仍会调用宿主开发React；对当前热点的覆盖不完整。更有针对性的实验应包含SDK自己的生产React、ReactDOM及JSX运行时，用独立root管理。不能把自带另一份React的组件直接塞进宿主React树，否则可能触发Hooks运行时不一致；见[React官方重复React说明](https://react.dev/warnings/invalid-hook-call-warning)。独立root仍共享页面、DOM和主线程，不能代替iframe的文档隔离。

现有`StructureEditorHandle`与`NativeEditorSession`不依赖React，可作为这条边界的基础。但这属于新的实现工作，本轮只分析，没有新增SDK构建流水线或修改9001。也不承诺单项改造能从2.73秒直接达到1.44秒；各阶段有重叠，应在相同就绪标准下逐项测量，避免把收益机械相加。针对Worker握手还可实验调整隐藏宏UI的挂载时机、拆分同步初始化任务，让父线程先处理启动通知；必须测量是否损失原有并行，不能用固定延时猜测Worker已经就绪。

## 4. 证据与范围

- [汇总、80个正式逐样本指标、诊断打点和CPU聚合](/data/lzq/gith/nexpoly-dev/docs/verification/ketcher-refresh-residual-analysis.json)。
- [正式原始请求和阶段记录](/tmp/nexpoly-refresh-residual-20260914/comparison/results.json)、[正式统计](/tmp/nexpoly-refresh-residual-20260914/comparison/summary.json)。
- [StrictMode诊断](/tmp/nexpoly-refresh-residual-20260914/diagnosis/results.json)、[内部预取实验](/tmp/nexpoly-refresh-residual-20260914/macro-experiment/results.json)、[Worker诊断](/tmp/nexpoly-refresh-residual-20260914/worker-diagnosis/results.json)。
- [CPU profiles](/tmp/nexpoly-refresh-residual-20260914/cpu)、[Chrome时间线](/tmp/nexpoly-refresh-residual-20260914/timeline)。
- [临时诊断差异](/tmp/nexpoly-refresh-residual-20260914/diagnostic/frontend/residual-instrumentation.patch)、[宏预取实验差异](/tmp/nexpoly-refresh-residual-20260914/macro-preload/frontend/residual-macro-preload.patch)。

正式对照没有修改Blob Worker代码；独立StrictMode/Worker诊断注入了打点前缀，CPU/Chrome时间线也属于独立观测，不与正式80样本混算。宏预取实验使用相同SDK打点的两份副本，未注入Blob前缀。所有实验保留原始样本，未以删除失败或异常值改善统计。

初次探针试跑曾在iframe3.7遇到“导入Promise已返回、模型尚未提交”导致的导出断言失败；保留在`comparison-pilot`。探针修正为等待模型3个原子后，另开完整正式批次，80个样本无失败；中断试跑造成的关闭错误也保留。该修正发生在计时结束后的验证环节，未放宽ready。

本轮量化范围为结构工作台的空文档普通刷新，不能直接代表大结构、复杂宏分子、其他设备或用户真实网络。其余五个画板共用本轮定位的SDK和共享workspace路径；前一轮已覆盖六入口，但本轮没有重新宣称完成六入口全场景性能验收，也没有重复冷/禁缓存验收。

本轮5个临时Vite/预览实例均已停止；诊断副本、失败试跑和原始记录保留。完成时再次校验424个产品文件无变化，9000/9001容器状态未变且HTTP均为200。
