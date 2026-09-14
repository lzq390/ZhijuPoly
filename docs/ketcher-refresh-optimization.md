# 画板刷新性能优化

文中标注的本地证据仅用于定位当时的检查，不随仓库交付；仓库内文件仍使用相对链接。

核验日期：2026-09-11。实施范围为开发环境 9001，保留业务源码热更新和 React 原生画板；9000 作为只读生产对照。

**已更新开发环境 9001，并完成实际端口复验。9000 的容器、镜像与启动时间均保持不变。**

本次交付 33 个前端文件，完整工作区源码与已测冻结版本一致；热更新探针的临时改动已恢复。实际 9001 的六个画板入口完成拖动、SMILES 导出、清空与资源隔离检查，另通过 11 项启动/失败重试/历史/热更新检查及压缩协议检查。主机 Node 为 v22.23.2，开发容器为 v22.23.0。详见部署核验（本地证据：`/tmp/nexpoly-refresh-implementation-_us4x7i5/delivery-result.json`）。

## 原因与 iframe 对照

9000 使用独立的 iframe / Ketcher 3.7 生产产物，9001 使用宿主 React 开发运行时中的 Ketcher 3.8。旧 iframe 内的代码已经完成打包和生产优化；组件化后，画板进入宿主的模块加载链，开发模式的模块请求、执行和初始化成本会共同影响显示时间。版本和就绪检查也不同，因此不能把两者全部差距归因于组件化或 Vite。

本轮优化前，同一套原生源码在开发服务上的模拟远程普通刷新约需 4 秒。App 静态导入 17 个业务页面及其 CSS，结构工作台刷新也要解析不相关页面。一次普通刷新约有 193 个业务源码/CSS 请求，缓存命中后传输量虽小，仍要完成依赖发现和缓存重验证。SDK 又等到页面挂载、有尺寸之后才开始导入，形成额外串行等待。开发模块请求瀑布的背景见 [Vite 性能说明](https://vite.dev/guide/performance.html)。

刷新会销毁页面中的编辑器和 Worker。HTTP 缓存可以复用资源，不能保留这些活对象。本轮正常样本检查每次只创建一个 Worker，未观察到因 StrictMode 重复建立两套画板。保留的约 160ms 提交等待负责结构正确性；就绪后的居中也没有移入就绪标准或被删除。

冷访问还有传输成本：最大的 standalone 依赖约 15.60MB，另外两个大型 SDK 分块约 7.56MB 和 5.57MB。standalone 内嵌 Worker 的 Base64 数据，压缩响应比单纯压缩 JavaScript 语法更有效。压缩改善冷访问和缓存未命中，不是暖刷新的主要收益来源。

此前实际 9000、9001 与同源码生产构建的三次普通刷新对照，保留在 `/tmp/nexpoly-refresh-persistent-20260911/results.json` 和 `/tmp/nexpoly-refresh-matched-20260911/results.json`。本轮验收使用优化前的原生源码快照作基线，两侧 SDK 均为 3.8、补丁均为 `nexpoly-3.8.0-r3`，不以旧 iframe 的速度计算优化幅度。

## 实现

1. **页面和专属 CSS 按路由加载。** `routing.ts` 提取原有路径、别名及历史处理，逻辑逐段比对保持一致。`pages.ts` 注册 17 个动态页面。AppShell、共享结构文档和导航事务留在 App 父层；导航发出时即预取目标页面，与现有保存及退出流程并行。取消的页面加载不会创建编辑器。
2. **轻量入口并行启动。** `main.tsx` 读取当前路由后，并行加载应用、当前页面和画板预取模块。六个画板深链调用内部 `preloadStructureEditor(): Promise<void>`；普通页面不预取 SDK。React 引擎复用 SDK loader，iframe 引擎为空实现。预取只获取模块和样式，实例、Worker 与文档恢复仍由可见会话管理。开发 HTML 额外提示三个轻量启动入口 `routing`、`pages`、`mountApp`，减少入口自身的串行请求；URL 对齐 Vite 的 HMR 时间戳，刷新后不重复下载。真正的 SDK 下载仍由画板路由触发。这些提示不进入生产构建，也不预载全部业务页面。
3. **页面资源到达后立即显示。** 使用共享加载状态及页面局部错误边界。试跑发现 Suspense 回退节流会在模块已到达后继续延迟画板挂载，因此页面加载状态使用 `useSyncExternalStore` 通知完成，编辑器内部保护条件不变。
4. **真实资源失败可以重试。** 失败 Promise 不永久缓存；页面入口和 SDK 入口重试使用新的同源模块 URL，CSS 重试等待样式加载完成。构建阶段为这些动态入口保留导出接口，防止 Rollup 的共享分块命名空间转换导致重试拿到错误对象。正常成功导入保持缓存和组件身份。插件使用的 `emitFile({ preserveSignature: "strict" })` 见 [Rollup 官方接口](https://rollupjs.org/plugin-development/#this-emitfile)。
5. **压缩开发依赖响应。** 标准 `compression` 中间件只处理预构建依赖 JS 和 Ketcher CSS，阈值 32KiB、gzip level 4，浏览器支持时可协商 Brotli。保留 Vite 自身的 ETag、版本校验和缓存处理；304 和 Range 分支保留 `Vary: Accept-Encoding`。API、HMR 和业务源码继续使用原有响应内容。
6. **资源清单在构建末尾生成。** 路由拆分后 Vite 会移除 CSS 对应的空 JS 分块，清单生成移到该处理之后，避免记录已经移除的资源。

模块入口重试不等同于任意依赖图都能在原页面恢复：浏览器还可能缓存传递依赖的导入失败，旧部署资源被删除也无法靠同一文件加查询参数恢复。此时保持错误边界和当前文档，不自动刷新丢失用户内容；恢复服务或刷新应用仍可能是必要操作。浏览器级限制见 [Vite 动态导入故障说明](https://vite.dev/guide/troubleshooting#failed-to-fetch-dynamically-imported-module)。本轮真实网络回归明确覆盖页面入口、SDK 入口和生产 CSS 的失败恢复。

## 正式测量方法

- Chrome for Testing 151.0.7922.34，1440×900，正常动画；40ms 延迟、50Mbps 下载、10Mbps 上传，无 CPU 限速。该条件模拟网络往返，不能替代用户实际网络。
- 使用独立源码快照和独立 Vite 实例：基线 5911、优化 5912。先预热服务器的模块转换；浏览器冷访问仍使用新建的持久化配置目录。
- 六个画板入口，每个入口优化前后各 10 轮；每轮依次执行冷访问、普通刷新和禁缓存刷新，共 360 个正式样本。每轮交换基线和优化实例的执行顺序；所有失败样本保留。
- 就绪要求可见 SDK 画布存在、共享状态为 ready、公共清空按钮可用且画板不受交互锁限制。计时完成后导入 CCO、实际拖动氧原子并导出 SMILES 验证。
- 记录 App 出现、SDK 请求开始、Worker 创建及首次收到回复、画布建立、就绪、请求数、传输量和长任务。逐帧探针用于测量可见状态，具有帧采样精度限制。
- P50/P95 使用最近秩法；每组 10 个样本的 P95 等于该组最大值，不应当作长期尾部上限。失败计入完整性检查，缺失值不会被静默删除。
- 使用浏览器正常的内容编码协商。单独验证 gzip 与 Brotli 解码字节完全一致；压缩示例为 standalone 的 15,595,692 字节压到 gzip 5,790,451 字节、Brotli 5,034,787 字节。

## 持久化浏览器正式结果

| 画板 | 暖刷新 P50：前 → 后 | 降幅 | 暖刷新 P95：前 → 后 | 冷传输 MB：前 → 后 | 验收 |
| --- | ---: | ---: | ---: | ---: | --- |
| 结构工作台 | 4.337s → 2.834s | 34.65% | 4.456s → 3.029s | 44.86 → 9.58 | 通过 |
| 数据库查询 | 4.213s → 2.824s | 32.97% | 4.534s → 3.035s | 44.86 → 9.54 | 通过 |
| 聚合物相似性探索 | 4.231s → 2.836s | 32.97% | 4.331s → 2.966s | 44.86 → 9.56 | 通过 |
| 均聚物性质预测 | 4.291s → 2.881s | 32.86% | 4.360s → 3.088s | 44.86 → 9.50 | 通过 |
| 条件生成 | 4.242s → 2.805s | 33.88% | 4.492s → 2.931s | 44.86 → 9.61 | 通过 |
| 逆向设计 | 4.212s → 2.837s | 32.64% | 4.467s → 2.988s | 44.86 → 9.87 | 通过 |

正式样本共 360 个，失败 0 个。MB 使用十进制；判定使用未四舍五入的数据。

| 画板 | 冷访问 P50：前 → 后 | 禁缓存刷新 P50：前 → 后 |
| --- | ---: | ---: |
| 结构工作台 | 11.704s → 4.859s | 10.612s → 4.269s |
| 数据库查询 | 11.710s → 4.803s | 10.561s → 4.277s |
| 聚合物相似性探索 | 11.642s → 4.833s | 10.581s → 4.294s |
| 均聚物性质预测 | 11.657s → 4.801s | 10.564s → 4.249s |
| 条件生成 | 11.669s → 4.887s | 10.584s → 4.272s |
| 逆向设计 | 11.726s → 4.870s | 10.492s → 4.301s |

结构工作台的暖刷新阶段计时进一步说明收益来自哪里：

| 指标（P50） | 优化前 | 优化后 |
| --- | ---: | ---: |
| App 出现 | 1904ms | 771ms |
| SDK 入口请求开始 | 1929ms | 314ms |
| Worker 创建 | 2695ms | 1310ms |
| 画布建立 | 2814ms | 1416ms |
| 工具可用 / ready | 4337ms | 2834ms |
| 请求总数 | 224 | 94 |
| 业务源码/CSS 请求 | 193 | 64 |
| 最大主线程长任务 | 746ms | 714ms |

阶段时间是相对导航开始的时间，最大长任务是单次任务持续时间。SDK 请求和画板建立明显提前，单个最大长任务仍然较长，表明剩余 SDK 初始化成本没有被消除。

各场景的完整 P50/P95 和全部样本指标见配套验证 JSON；原始网络记录、阶段计时及所有试跑记录保留在验证目录。

六个入口的暖刷新 P50 降幅为 **32.64%～34.65%**，P95 均改善，冷传输量减少 **77.99%～78.83%**。各入口均达到事先规定的目标。量化结果来自独立实例 5911/5912；随后将相同源码更新到 9001，并完成上述部署复验。


## 隔离上下文对照

结构工作台另测 10 轮，冷访问、普通刷新和禁缓存刷新共 60 个样本，全部通过；与上面的持久化浏览器结果分开统计。

| 普通刷新指标 | 基线 | 优化后 |
| --- | ---: | ---: |
| 就绪 P50 | 7.810 秒 | 2.755 秒 |
| 就绪 P95 | 8.014 秒 | 2.896 秒 |
| 网络传输 P50 | 23.196 MB | 0.013 MB |

基线在这种上下文中重复传输了约 23.2 MB SDK 资源，压缩后该现象未在本轮样本中出现。Chromium 151 的默认内存缓存总量上限为 50 MiB，单项上限为总量的 1/8；两份大型未压缩依赖超过该默认单项上限，Brotli 后最大的响应约 5.03 MB。这与测试中的缓存差异相符，但本轮没有用 NetLog 直接确认缓存拒绝原因，不能把容量解释当作逐项诊断结论。[对应版本的 Chromium 实现](https://raw.githubusercontent.com/chromium/chromium/refs/tags/151.0.7922.34/net/disk_cache/memory/mem_backend_impl.cc)

持久化浏览器的 SDK 暖缓存本来就有效；本次普通刷新优化幅度仍按前述 360 个正式样本计算，隔离上下文的额外收益不并入验收。

## 功能与构建验证

| 验证范围 | 结果与记录 |
| --- | --- |
| 单元与组件回归 | 101 个文件、941 项测试通过；`full-tests-final.log` |
| React / iframe 正式构建与资源清单 | 两种引擎均通过；React 131 个交付资源，iframe 142 个且 `nativeAssets` 为空；`build-react-final.log`、`build-iframe-final.log`、`build-final-results.json` |
| 普通页面和旧路径别名 | 首页、知识检索、数据库筛选、DFT 及两个旧路径首次打开不加载画板 SDK、不创建画板 Worker；`refresh-interactions-final-complete/result.json` |
| 开发与生产资源失败恢复 | 页面入口、SDK 入口、生产 CSS 的真实请求失败后点击重试成功；共享文档和已存在画板保留；`refresh-interactions-production-entries/result.json` |
| 业务源码 HMR 与历史 | 修改再恢复工作台标题，页面 `timeOrigin`、SDK、画布节点和 CCO 均保持；前进后退通过；`refresh-interactions-final-complete/result.json` |
| 加载期间的交互 | 正常动画、快速导航、取消、延迟 SDK、失败 Worker 重试、输入焦点与抽屉通过，覆盖 390/1024/1440/2560 宽度；`native-loading-predeploy/result.json` |
| 原生完整画板流程 | 六入口、共享结构、布局与标注、粘贴复制、清空、无效草稿、DFT、3D、PNG 和聚合物等检查通过；`native-sdk-completion/result.json` |
| iframe 完整画板流程 | 相同扩展流程通过；另外六入口逐一绘制、导出、清空及资源隔离通过；`iframe-sdk-completion/result.json`、`iframe-entries-verified/result.json` |
| 页面样式 | 15 个业务页面、1440 宽度的 49 项几何检查通过；画板尺寸另由两种引擎完整流程和抽屉回归覆盖；`pages-1440/report.json` |
| 压缩协议 | gzip/Brotli 字节等价、ETag、304、Vary、HEAD、Range、过期依赖拒绝、源码与 HMR 旁路通过；`compression-final.log` |

上述文件统一位于本次验证目录（本地证据：`/tmp/nexpoly-refresh-implementation-_us4x7i5`）。最终构建用 `--emptyOutDir` 清理独立输出目录后生成，避免旧文件混入资源隔离结果。

试跑和回归阶段的失败同样保留：Chrome 151 在强制内容编码的 CDP 设置下崩溃，最终协议改用正常协商；原有 favicon 404 保留在网络记录及字节数中，不作为画板资源失败；错误重试的浏览器缓存和构建导出接口问题已修复；历史检查等待导航完成后再断言稳定历史项，快速取消另行验证。

一个中间实现完成了 174 个性能样本，功能均通过，但部分路由的暖刷新中位数接近或未达 30% 目标。该批次在浏览器已关闭、原始记录已写入的轮组间停止，记录于 `performance-final/interrupted.json`。随后加入开发入口预加载，另开完整批次重新测量，没有把两版样本混合计算。预加载的首次 HMR 复验发现入口 URL 未携带当前时间戳，会重复获取；修复后的最终版本由 `refresh-interactions-preload-timestamps/result.json` 确认三个入口各请求一次，失败记录 `refresh-interactions-preload/result.json` 仍保留。另外预加载引擎预取函数的单轮实验没有显示额外收益，数据库查询未达性能目标，最终未采用；记录为 `performance-pilot-entry-preload-final/result.json`，选择依据见 `selected-implementation.json`。

扩展剪贴板检查曾在两种引擎出现间歇失败。最终探针等待 SDK 的 `copyOrCutComplete` 后才读系统剪贴板，粘贴后先确认画布模型提交再导出；原生与 iframe 的完整流程均通过。失败日志、诊断记录及最终通过结果均保留，不以延长固定等待或修改产品保存逻辑替代验证。

## 数据与复现

- [验证摘要与逐样本指标](verification/ketcher-refresh-optimization.json)：包含 420 个正式/隔离样本的指标及所有验收判定。
- 持久化浏览器原始记录（本地证据：`/tmp/nexpoly-refresh-implementation-_us4x7i5/performance-final-preload/samples.json`）与判定结果（本地证据：`/tmp/nexpoly-refresh-implementation-_us4x7i5/performance-final-preload/result.json`）。
- 隔离上下文原始记录（本地证据：`/tmp/nexpoly-refresh-implementation-_us4x7i5/performance-isolated/samples.json`）与判定结果（本地证据：`/tmp/nexpoly-refresh-implementation-_us4x7i5/performance-isolated/result.json`）。
- 冻结源码校验（本地证据：`/tmp/nexpoly-refresh-implementation-_us4x7i5/source-snapshots-preload.json`）、测量后未变更校验（本地证据：`/tmp/nexpoly-refresh-implementation-_us4x7i5/source-freeze-verification.json`）、本次改动的独立差异（本地证据：`/tmp/nexpoly-refresh-implementation-_us4x7i5/implementation-review.diff`）。
- 实际 9001 六入口（本地证据：`/tmp/nexpoly-refresh-implementation-_us4x7i5/delivery-entries/result.json`）、热更新与重试（本地证据：`/tmp/nexpoly-refresh-implementation-_us4x7i5/delivery-interactions/result.json`）、压缩协议（本地证据：`/tmp/nexpoly-refresh-implementation-_us4x7i5/delivery-compression.log`）。

完整目录为 `/tmp/nexpoly-refresh-implementation-_us4x7i5`，包含优化前源码、已测实现、全部失败与中断记录、截图和构建日志。名称含 `final` 的早期试跑同样保留；最终采用的批次为 `performance-final-preload`，源码摘要为 `source-snapshots-preload.json`。部署前的原文件备份位于 `delivery-backup/frontend`。

临时对照服务和预览服务已停止，所有记录与源码副本保留。复测时须先在该目录的 `baseline/frontend` 与 `optimized/frontend` 分别启动 Vite 5911/5912，代理目标设置为 `http://127.0.0.1:18000`，再运行下述测量脚本。

关键实现：[路由](../frontend/src/routing.ts)、[页面加载](../frontend/src/pages.ts)、[启动入口](../frontend/src/main.tsx)、[失败重试](../frontend/src/retryModuleImport.ts)、[开发响应压缩](../frontend/build/development-compression.ts)、[开发入口预加载](../frontend/build/development-entry-preload.ts)。

`frontend/scripts/verify-structure-refresh-performance.mjs` 可通过 `REFRESH_BASELINE_URL`、`REFRESH_OPTIMIZED_URL`、`REFRESH_ROUNDS`、`REFRESH_ROUTES`、`REFRESH_CONTEXT`、`REFRESH_OUTPUT` 和 `STRUCTURE_CHROMIUM_PATH` 复现。默认参数为六入口、10 轮、持久化上下文。隔离上下文使用 `REFRESH_CONTEXT=isolated`，应与普通浏览器结果分别报告。

生产发布不属于本次实施；没有新增独立 SDK 构建流水线或 Worker 共享机制。剩余主线程初始化开销和 Worker 资源拆分可作为独立后续优化评估。
