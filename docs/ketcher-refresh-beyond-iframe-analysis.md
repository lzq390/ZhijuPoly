# 四项剩余原因能否解释慢刷新，以及超越9000的路径

文中标注的本地证据与安装后的 SDK 源码仅用于定位当时的检查，不随仓库交付；仓库内验证汇总仍使用相对链接。

2026-09-14。本轮为分析与隔离原型实验，未修改产品前端源码、未更新9001、未操作9000部署。业务源码HMR、原生画板方向及现有160ms提交检查均保留。

**四项确实命中了重要成本，但不是四笔可以相加的独立耗时。隐藏大分子初始化和开发React造成主线程长任务，长任务又拖延Worker初始消息；文档恢复部分与这些工作重叠。此外，App、SDK和Worker启动偏晚，是要超越9000必须同时解决的前段约束。**

本轮已经实际做了生产React独立root、真正延迟大分子初始化、两者组合和源码图预加载原型。结果证明可以明显改善9001，尚未证明能超越9000。不能把“有优化空间”当成已实现目标。

## 1. 同批对照：两项组合后仍有约0.72秒差距

主对照是结构工作台、空文档、持久化Chrome151.0.7922.34、1440×900、正常动画、40ms延迟/50Mbps下载/10Mbps上传，无CPU限速。预热后普通刷新，四个环境交替正序/倒序各10次，共40个正式样本。全部通过初始空SMILES、CCO导入、实际拖动氧原子并确认KET坐标改变、再次SMILES导出及清空；每次一个Worker。

本轮同时记录两个终点：

- **UI就绪**：实际可见SVG、公共清空按钮可用，当前引擎还要求共享状态ready且不在inert/隐藏容器中。沿用此前UI口径。
- **首次导出完成**：UI就绪后立即调用真实SDK `getSmiles()`，等待成功返回。四个版本均执行，避免仅让按钮提前亮起。它含一次真实API往返，不能与上轮1.438秒UI口径直接混算。

| 环境 | UI就绪 P50 / P95 | 首次真实SMILES导出完成 P50 / P95 |
| --- | ---: | ---: |
| 实际9000：iframe 3.7、生产React | 1.409 / 1.618s | 1.579 / 1.783s |
| 实际9001：原生3.8、开发React | 2.704 / 3.028s | 3.007 / 3.295s |
| 仅真正延迟大分子初始化 | 2.179 / 2.446s | 2.199 / 2.465s |
| 延迟大分子 + SDK独立生产React root | 2.133 / 2.724s | 2.152 / 2.743s |

组合相对9001，UI P50改善约21.1%，首次导出完成P50改善约28.4%；组合的UI P95仍为2.724秒，明显慢于9000。加上生产运行时后，相对仅延迟大分子的P50净改善只有约47ms，不能据此认定值得直接采用当前打包形态，也没有证明组合的尾部优于单项。

这些是原型数据，不能宣称原方案“暖刷新至少改善30%”的UI目标已经达到。10个样本的最近秩P95是最大值，能暴露本批慢样本，不能替代长期尾延迟保证。

原始记录：40个正式样本及请求（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/combined-empty/results.json`）；[汇总与逐样本指标](verification/ketcher-refresh-beyond-iframe-analysis.json)。

## 2. 四项原因的权重与关系应如何修正

### 隐藏大分子初始化：主要原因，优先级应提高

3.8 SDK在小分子画布建立后仍会导入并挂载隐藏Macro界面、建立CoreEditor、解析单体库，公共onInit又等待它。上轮诊断的小分子初始化到宿主onInit中位数约865ms，包含主线程、调度和Worker等待，不能把865ms全算成纯Macro CPU。

真正延迟Macro的原型没有挂载该界面、没有首次Macro资源请求；小分子onInit仍等待真实Worker info，不提前冒充完整可用。主对照UI P50从2.704降到2.179秒、最大长任务P50从724降到241ms，证明这是有较大净收益的方向。

首次进入Macro或导入HELM/宏分子KET时才初始化，仍需要等待这些工作。它优化的是此前被迫承担未使用功能成本的小分子启动，不能保证首次宏分子使用也同幅度变快。

### Worker握手等待：主要关键路径，但与长任务是因果关系

主对照按每个样本先算“Worker创建→首次收到回复”，再取P50：9001为1172ms，延迟Macro后626ms，组合后579ms，9000为574ms。组合后的这一段已经接近旧生产。

上轮Chrome151时间线与对应版本源码说明：Worker脚本求值完成的通知要先回到父线程，父线程执行后才放行早期消息；主线程长任务可以让已空闲的Worker继续等待。这与本轮减少Macro工作后该区间显著缩短一致。该区间包含脚本启动、消息队列、WASM服务准备及回复投递，不能全部称为WASM计算。

因此，“减少Macro/React长任务”和“缩短Worker握手”不是两份独立收益。组合原型已经把这条尾段压到接近9000后，再针对WASM启动计算投入，优先级应低于提前整个启动链。

详见[上一轮精确版本时间线分析](ketcher-refresh-residual-analysis.md)。

### 开发React：重要放大因素，收益随其他优化改变

独立生产SDK实验保留宿主开发React、StrictMode和业务HMR，SDK自带匹配的生产React/ReactDOM，在同一div内另建root。第一组各5次筛选中，UI P50为2.396秒，对照9001为2.715秒；最大长任务713→396ms。它确实有效，但单项不足追平9000。

在隐藏Macro已经移出启动路径后，正式组合相对lazy-only只有约47ms的P50净收益。主要开发React热点已随隐藏UI减少，收益不能再与单项约319ms相加。当前生产SDK原型还形成约25.23MB未压缩主块，Worker启动没有提前；其模块组织和执行时机也需优化。

关闭全局StrictMode不适合作为主要解决方案：上轮独立诊断只改善约184ms。SDK内部CoreEditor确有effect重放，昂贵单体解析第二次复用了缓存，Worker也始终只有一个；不应描述成SDK完整加载两遍。

### 文档恢复：真实的次级成本，不能按整段直接扣除

[workspace初始化](../frontend/src/structure/workspace.ts)对初始空文档仍调用clear和settle；[原生会话](../frontend/src/structure/nativeSession.ts)还检查清空后的KET，[提交等待](../frontend/src/structure/editor.ts)有两个80ms等待。历史恢复阶段约514ms，部分时间在等Worker或调度，不能再独立承诺节省514ms。

本轮组合首次Worker回复→UI可用的逐样本P50约305ms，旧9000约176ms；这一尾段仍值得精简，但小于前段启动差距。即使把组合每个样本直接减去160ms，UI P50也仍约1.973秒，比本批9000慢约0.564秒。该算式仅检验“删除等待足不足够”，不代表允许删除检查。

## 3. 新补证：单体库还影响每次转换与实际操作响应

隐藏Macro的代价不限于首次界面。当前standalone转换入口（本地 SDK 源码：`frontend/node_modules/ketcher-standalone/dist/main.js:871`）在每次convert时，都会序列化现有CoreEditor的整份单体库，传入Worker。对应WASM C++入口每次转换创建IndigoSession，并重新加载所传库；底层解析JSON并建立模板对象。

版本已核对：本地standalone依赖固定Indigo1.36.0，实际嵌入WASM版本串为`1.36.0.0-g85e2a6d74`，与引用源码提交一致。[对应转换实现](https://github.com/epam/Indigo/blob/85e2a6d744b2c8aa18f2f6c94c28dedcdf9638fd/api/wasm/indigo-ketcher/indigo-ketcher.cpp#L592)、[库加载实现](https://github.com/epam/Indigo/blob/85e2a6d744b2c8aa18f2f6c94c28dedcdf9638fd/api/c/indigo/src/indigo_molecule.cpp#L635)。

| UI就绪后立即真实getSmiles的P50 | 延迟 |
| --- | ---: |
| 9000 | 166.5ms |
| 9001 | 294.9ms |
| 仅延迟Macro | 19.0ms |
| 延迟Macro + 生产SDK | 19.2ms |

这说明原生方案已经在这个具体API响应指标上快于旧iframe；整页首次可用时间仍更慢。不能把一次真实API的改善当成整体刷新已经胜出，也不能把差值全部归为单体库CPU——仍包含调度、其他序列化和初始化并发。旧3.7也有附库机制，不能说它是组件化新引入的专属成本。

后续要维持“曾进入Macro后的小分子操作”也快，需要对可证明无需库的转换按需传库，或在Worker/WASM侧按库版本复用解析对象。仅缓存主线程JSON无法消除WASM内每次重建；缓存对象需处理IndigoSession句柄寿命和库版本失效。仅按当前UI是小分子模式就去掉库会误伤自定义单体、HELM/序列和宏结构，必须保留格式语义。

完整源码链、精确版本证据及边界（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/restore-fastpath-analysis.md`）。

## 4. 要超越9000，必须同时前移UI和Worker启动

下表为正式主对照的绝对时刻P50，单位ms；区间必须另按逐样本计算，不能直接相加这些P50。

| 阶段 | 9000 | 9001 | 组合原型 |
| --- | ---: | ---: | ---: |
| App出现 | 249 | 686 | 723 |
| Worker创建 | 664 | 1180 | 1262 |
| 画布SVG建立 | 1112 | 1284 | 1343 |
| 首次收到Worker回复 | 1232 | 2369 | 1855 |
| UI可用 | 1409 | 2704 | 2133 |

组合已经把Worker创建后的回复等待压到约579ms，仍比9000晚约598ms才创建Worker。这与剩余约724ms整体差距处于相同量级，是下一阶段最大的启动约束；不是把598ms精确归为某个CPU函数。

只提前Worker也不够：若画布仍在约1.343秒才建立，还要完成文档确认，服务提早就绪会把瓶颈转移到UI。因此必须同时减少“路由启动→SDK执行→可见画布”的串行等待，而不是只改postMessage位置。

追加的源码图预加载筛选没有显示收益：保留HMR，把工作台64个源码依赖全部放到HTML modulepreload中，各5次对照UI P50从2.694变成2.926秒。样本不足以证明长期退化，但足以否定“把所有源码都提前请求就肯定更快”的实施依据。该副本自定义cacheDir未命中原压缩过滤，冷预热不与实际服务比较；暖阶段使用缓存。

推荐按以下顺序继续实施独立实验：

1. **先完成真实Macro按需初始化及转换语义覆盖。** 保留小分子真实服务就绪；模式切换、HELM/宏KET、直接formatter导入、自定义单体入口统一经过可重试的能力初始化。已经验证较大净收益，先解决原型中模式Promise挂起问题。
2. **建立不依赖React/App挂载的轻量化学服务启动入口。** 初始画板URL可在轻启动层识别；预初始化租约拥有唯一Worker，SDK挂载后原子接管同一实例。业务页面继续HMR，文档写入和可见画布仍由现有workspace/session控制。取消导航、StrictMode丢弃、初始化失败、超时和未接管都要销毁；普通路由不启动。不能创建一个预热Worker后再创建正式Worker。
3. **继续前移SDK求值完成和必要UI挂载，精简首屏路径。** 旧生产root原型的HTML modulepreload（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/island/frontend/build/island-poc-preload.ts:13`）预加载模块而不执行；但main.tsx（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/island/frontend/src/main.tsx:8`）随后主动预加载runtime，其顶层import（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/island/frontend/src/components/structure-workbench/KetcherReactRuntime.tsx:13`）已经执行生产SDK并等待CSS，与App加载并行。仍待可见画板的effect调用mount（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/island/frontend/src/components/structure-workbench/KetcherReactRuntime.tsx:25`）后，才创建provider及其服务（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/island/frontend/island-sdk-entry.tsx:23`）并启动Worker，不能将旧原型描述成一直只预取而不执行。后续需要分别验证更早的模块执行、服务启动和UI挂载，重功能按需分块。可将Worker/WASM拆为独立版本资源，让轻服务入口无需解析整个React/编辑器包；直接拆资源本身的暖刷新收益仍要单测。业务模块不为了预打包而牺牲HMR。
4. **增加有证据的新空会话快速路径，并减少重复转换。** 只有当前session/revision确为初始空文档、无待处理写入，并取得有效空KET证据，才省掉重复clear和相关等待；否则走现有恢复。保持ready前订阅、错误回退和完整快照。非空结构用可靠提交完成信号和内容校验优化，不靠任意减小定时器。

第4项的反例是SDK会读取URL `moll`并发起不等待的自动导入；“新实例+workspace空”不等于没有初始化写入。仅文字、图形和标注也不能靠空SMILES判断为空。必须收口或排除这些路径，防止以丢失内容换速度。

作为下一轮需要验证的共同预算，可设置：

| 同一次导航的阶段目标 | 预算 |
| --- | ---: |
| Worker开始 | ≤450ms |
| Worker开始后实际可用 | ≤600ms |
| 必需2D模型/UI初始化完成 | ≤1050ms |
| 之后文档确认 | ≤200ms |
| 首次真实SMILES导出 | ≤50ms |

按`max(必要UI完成, Worker开始+服务可用路径)+剩余文档确认+首次导出`，预算对应UI约1.25秒、真实导出约1.30秒，才有相对本批9000的可见余量。**这是共同约束下的工程目标，不是由各项P50机械相加得出的预测，也不是已实现性能。** 2D模型可用与SVG出现需分别核验；200ms确认预算不得以绕过原160ms保障来达成。实际应以同批9000为对照，要求UI与功能指标P50均快、P95不恶化，并覆盖空/非空及宏结构能力。

## 5. 原型限制、失败记录和交付范围

所有PoC位于`/tmp/nexpoly-refresh-beyond-9000-20260914`，未替换实际9001。

- lazy Macro的CCO导入、SMILES/KET/Molfile/PNG导出、首次Macro切入切回、HELM及宿主直接KET formatter恢复已通过定向验证。高级monomer wizard等全部公开入口尚未完成兼容性覆盖。
- 组合烟测发现：HELM导入后直接调用`switchToMoleculesMode()`可能挂起。已有测试覆盖的是先进入Macro再切回，不能将新失败归为组合独有退化。失败日志保留，未为了测量修补或隐藏。因此不能把本PoC直接部署。
- 初次测试脚本曾错误假设共享CCO跨整页刷新持久化。实际[workspace生命周期设计](../frontend/src/structure/workspace.ts)只在一次App生命周期内保留，实际9000、9001和组合均刷新为空。已停止该场景；13个断言失败和3个预热记录完整保留，标为不适用的测试，不混入上述40个正式样本，不当成产品回归。非空验证采用App内切页恢复。
- 最终App内非空验证完成10个往返、共20次所有者切换，均恢复CCO；真实鼠标拖动后KET坐标改变，活动Worker始终为1，旧实例写入均以AbortError拒绝，普通页面没有新增SDK实例，无pageerror。完整记录（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/island-lazy/navigation-final.json`）。首个探针把已释放的canvas空引用当成对象，保留失败记录后仅修正探针重测，未修改PoC。
- 生产root还需要完整错误边界、portal/样式、HMR、快速导航、失败重试及SDK版本发布验证。定向构建/烟测不替代全产品发布验收。
- 本轮不宣称冷启动、禁缓存刷新、低端CPU、复杂宏结构及全部六入口的性能目标已通过；普通页资源隔离和共享所有者转换另有功能烟测记录。

主要产物：

- [正式统计、所有逐样本指标、预热和失效场景记录](verification/ketcher-refresh-beyond-iframe-analysis.json)。
- 5环境筛选原始记录（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/screening-base/results.json`）、正式40次对照（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/combined-empty/results.json`）、无效非空刷新场景说明（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/combined-cco/scenario-status.json`）。
- 测量脚本（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/compare-functional.mjs`）、统计脚本（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/summarize-evidence.py`）。
- lazy Macro补丁与限制（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/lazy-macro/README.md`）、SDK补丁（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/lazy-macro/SDK-lazy-macro.patch`）、生产SDK原型（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/island/POC.md`）。
- 组合烟测含失败（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/island-lazy/smoke-results.json`）、所有者与普通页面补测（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/island-lazy/smoke-lifecycle.json`）。

已检查424个产品前端文件与此前冻结版本一致。实际9000/9001容器ID、镜像和启动时间保持不变。原有SDK的11个制品、4个补丁验证通过，两个实际端口HTTP均为200。5个本轮临时服务均已停止，浏览器已关闭。本轮只新增分析文档和验证汇总；临时原型及其失败记录保留用于评审。最终环境核验（本地证据：`/tmp/nexpoly-refresh-beyond-9000-20260914/final-environment.json`）。
