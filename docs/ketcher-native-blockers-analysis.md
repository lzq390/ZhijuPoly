# Ketcher 原生组件两项阻塞问题分析

> 历史基线分析：本次已通过版本化补丁修复 process、生命周期和 Worker 隔离问题，后续实现及最新验收入口见 [组件化维护文档](ketcher-componentization.md)。下文的失败数据保留用于说明修复依据。

核查日期：2026-09-10。基线为 `codex/ketcher-react-compat` 的 `4abb259`，Node 22.23.2、React 19.2.4、Vite 7.3.6、四个 Ketcher 包 3.8.0。

主要阻塞是 SDK 卸载后仍保留全局监听器；开发模式的 `process is not defined` 已定位，并通过临时定向配置验证了初始化和常用操作。两者均有明确修复路径，但本次分析没有修改正式应用的依赖或引擎，原生完整兼容门禁仍未通过。

**监听器累积的证据与影响。**

原生证明页只挂载官方 Editor，不包含 NexPoly 的业务 Hook 或共享画板逻辑。在生产构建中连续重挂载，比较相隔十次的采样：

| 观测项 | 起始采样 | 十次之后 | 每次增量 |
| --- | ---: | ---: | ---: |
| window.resize | 8 | 18 | 1 |
| document.mousemove | 6 | 26 | 2 |
| document.mouseup | 4 | 14 | 1 |
| document.mouseleave | 3 | 13 | 1 |
| GC 后 JS 堆，十进制 MB | 81.1 | 84.9 | 有波动，不据此外推 |
| CDP 统计 DOM 节点 | 2139 | 5829 | 369 |

卸载编辑器、删除证明页的全局实例引用并再次 GC 后，相关监听器仍存在。原始记录见 [兼容门禁摘要](verification/ketcher-native-gate.json)。堆数据只覆盖 JavaScript，不包括 WASM/WebGL 或浏览器进程总内存；DOM 数量也不等于页面上可见的节点数量。

全局监听器及其闭包可能继续持有旧编辑器、订阅、Redux store 和 SVG。页面消失后这些引用仍能保活旧对象；鼠标移动和窗口缩放还可能触发旧实例的处理逻辑。已确认的是资源累积，尚未测得可量化的卡顿、错误提交或结构丢失，不能把这些潜在影响写成已发生故障。

原生组件共享宿主的 window/document，随业务页面重建会持续遇到这个问题。现有 iframe 在实际移除时销毁其独立文档，具有更完整的隔离边界；本次原生证明页的结果不能直接解释为当前 iframe 版本出现了相同的宿主泄漏。

**源码中已确认的清理遗漏。**

| 位置 | 3.8.0 行为 | 需要补齐的工作 |
| --- | --- | --- |
| ketcher-react 的 `script/ui/state/toolbar/index.js` 与 `initApp.tsx` | `initResize` 注册节流回调，初始化 cleanup 只处理键盘和部分鼠标监听 | 返回属于该实例的 disposer；移除相同回调，并取消节流的尾部任务 |
| `script/editor/Editor.ts` 的 `domEventSetup` | 给 document 的 mousemove、mouseup、mouseleave 注册匿名闭包，没有保存对应移除函数 | 保存 target/type/handler/capture 或 disposer，卸载时逐项移除并释放内部订阅 |
| `script/editor/HoverIcon.ts` | 注册另一条 document.mousemove，以及容器 mouseover/mouseleave；没有 destroy | 增加并调用 destroy，移除三个监听，释放图形引用 |
| `script/ui/state/mouse.ts` | `removeMouseListeners` 的 mousedown 分支误写为 addEventListener | 改为与注册参数匹配的 removeEventListener |
| `StructEditor` 卸载路径 | 移除 React props 对应订阅、wheel 和画布尺寸观察；未清理上述小分子编辑器 DOM 监听 | 将完整 SDK 资源释放接入官方组件的卸载链 |

前三项的注册数量与本地观测增量一致。这里的 `script/editor/Editor.ts` 属于 **ketcher-react 内的小分子编辑器**；不能仅因为 ketcher-core 中另一个 Editor 类有 `destroy()` 就认定它们已被清理。

来源：[3.8.0 resize 注册](https://github.com/epam/ketcher/blob/v3.8.0/packages/ketcher-react/src/script/ui/state/toolbar/index.js)、[初始化清理](https://github.com/epam/ketcher/blob/v3.8.0/packages/ketcher-react/src/script/ui/App/initApp.tsx)、[小分子 DOM 监听](https://github.com/epam/ketcher/blob/v3.8.0/packages/ketcher-react/src/script/editor/Editor.ts)、[HoverIcon](https://github.com/epam/ketcher/blob/v3.8.0/packages/ketcher-react/src/script/editor/HoverIcon.ts)。本地安装包 sourcemap 与发布代码也已核对。

**监听器问题的解决路径。**

建议在兼容分支维护针对 3.8.0 的可审计补丁，参考上游修复，并补全仍遗漏的匿名 DOM 监听。资源应由创建它的实例释放，cleanup 应支持重复调用；初始化尚未结束就卸载时，迟到的初始化结果也必须立即释放，不能再次触发业务 onInit。

不应仅在业务适配器中取消 change 订阅，因为 SDK 的全局回调仍然存在。也不应通过改写宿主 EventTarget 原型、遍历并删除宿主全部监听器解决问题，这无法可靠区分 Ketcher 与其他功能的监听。Shadow DOM 隔离样式，也不能清理注册在宿主 document/window 上的事件。

App 级单实例保活可以减少正常导航造成的重建，适合作为之后的性能优化；它仍需处理错误重试、热更新、退出和真正卸载，不能代替销毁修复。当前共享文档在不可取消写入超时后会淘汰旧实例，这一安全机制也要求底层能完整卸载。

**开发模式报错的根因。**

实际依赖链为 `ketcher-core@3.8.0 → assert@2.1.0 → util@0.12.5`，已发布的 ketcher-react 产物也含相关代码。浏览器异常栈指向 `util/util.js` 在模块加载时执行 `process.env.NODE_DEBUG`；浏览器没有 Node.js 的 process 全局，因此 onInit 尚未触发就终止初始化。

Vite 7 的开发依赖预构建与生产 define 转换不同。生产应用构建默认对 process.env 做静态替换，而开发预构建主要提供 process.env.NODE_ENV 等定义，NODE_DEBUG 访问因此暴露。生产版的正常运行不能证明所有 process API 分支都可用。参考 [Vite 7.3.6 define 实现](https://github.com/vitejs/vite/blob/v7.3.6/packages/vite/src/node/plugins/define.ts) 和 [assert 的浏览器兼容说明](https://github.com/browserify/commonjs-assert#usage-with-bundlers-that-dont-automatically-include-polyfills-for-nodejs-apis)。

本次在隔离证明页做了两组诊断，未修改正式前端配置：

| 诊断方式 | 结果 | 能证明的范围 |
| --- | --- | --- |
| 原配置，无 process 补充 | 原样复现异常，编辑器不就绪 | 确认基线问题 |
| 浏览器脚本临时提供 `process = { env: {} }` | 初始化成功，CCO 写入和读回成功 | 确认启动异常由缺失 process 引起 |
| 在原 Vite 配置上仅追加 `define['process.env.NODE_DEBUG'] = '""'` | 初始化、CCO 读写、文字标注、内置 Miew 3D 通过，未捕获未处理异常 | 确认存在小范围配置修复路径 |

最后一组证明页仍包裹 React.StrictMode。控制台还记录到两个跨组件渲染时 setState 的 React 警告，所以仅能报告开发模式启动和这些操作通过，不能报告 StrictMode 完整生命周期验收通过。无效输入探针的 SDK Promise 没有抛错，未覆盖 assert 的失败分支。完整诊断记录见 [process 分析结果](verification/ketcher-process-analysis.json)。

最小诊断配置是在兼容分支已有配置上追加这一项，不是完整可交付的原生配置：

```ts
define: {
  global: 'globalThis',
  'process.env.NODE_DEBUG': JSON.stringify(''),
}
```

正式修复可以选择回移上游的浏览器断言实现，或在相关依赖中按需注入 `process/browser`。后一条路线应同时覆盖 Vite 开发预构建和生产构建，校验错误分支及异步工具调用。仅定义 NODE_DEBUG 不提供 nextTick、stderr 等其他访问；也不要用空对象冒充完整 process，或把构建机 process.env 整体注入浏览器。

**上游已有修复，但覆盖范围与版本要求不同。**

| 上游变更 | 日期 | 与本项目的关系 |
| --- | --- | --- |
| [PR #9785](https://github.com/epam/ketcher/pull/9785)，提交 `6aaa82c` | 2026-04-27 | 修复 HoverIcon、mousedown 清理及部分观察器/弹窗生命周期；不能据此认为所有 document 监听已解决 |
| [PR #10810](https://github.com/epam/ketcher/pull/10810)，提交 `fa29d66` | 2026-08-03 | 用内部浏览器断言替换 Node assert；与本次 process 堆栈直接对应，需连同后续测试修正核查 |
| [PR #11555](https://github.com/epam/ketcher/pull/11555)，提交 `dd02c20` | 2026-09-03 | 增加 resize 移除逻辑与空 editor 保护；回移时还应核查实例所有权及节流尾任务取消 |

已检查的 3.18.0 tag 包含 HoverIcon.destroy，但 toolbar/initApp 仍缺上述 resize 清理。其 [package.json](https://github.com/epam/ketcher/blob/v3.18.0/packages/ketcher-react/package.json) 声明 Node ≥24.14.1，超出本期 Node 22 边界。部分修复已在主干，不代表当前锁定版本或任一稳定版本已经包含且通过我们的验证。因此不建议为这两项问题直接全量升级并默认切换引擎。

**建议采用的实施次序与验收。**

1. 保持 React 19、Node 22、四包 3.8.0 基线。在兼容分支固化浏览器依赖适配，覆盖 dev/build 两条构建路径；继续处理已显现的渲染警告。
2. 参考上游补丁修复生命周期，再补齐小分子编辑器匿名 DOM 监听。补丁纳入版本管理和干净 npm ci 流程；若形成内部派生包，明确标识来源与版本，避免长期手改 node_modules。
3. 开发 StrictMode 与生产分别执行至少十次完整重挂载；增加初始化中卸载、立即重挂载、弹窗/文本/3D 开关和错误重试。比较预热后同等状态，实例专属全局监听器必须无持续增量，旧实例不能再响应人工派发的鼠标/resize 事件。
4. 检查 GC 后引用链、脱离文档节点与堆曲线，允许运行时缓存和 GC 波动，但不能保留随重挂载持续增长的旧编辑器。观察器、定时器和内置 3D 资源按实际创建路径核查，不能只看四个监听器计数。
5. 门禁通过后接入已完成的 StructureEditorHandle / 共享文档，并复跑六页面、KET 布局/标注、草稿、清空、超时淘汰实例、图片回滚、AI PNG、3D 与多尺寸验收。最后才启用原生默认引擎和一个发布周期的 iframe 回退。

此次补充分析不重新定义既有 855 个测试的结论：这些测试及现有浏览器结果验证的是已交付的共享文档与 iframe 实现；原生 SDK 的修复仍需独立通过上述验收。
