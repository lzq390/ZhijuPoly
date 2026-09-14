# Ketcher 组件化与维护

已完成统一编辑器接口、App 级共享 SMILES/KET 文档和 Ketcher 3.8.0 原生 React 接入。原生 SDK、业务页面及 iframe 回退通过验收后，默认引擎切换为 React；iframe 3.7.0 保留一个发布周期的构建回退。验收矩阵与摘要见 [最终记录](verification/ketcher-native-gate.json)。

## 实现入口

| 文件 | 职责 |
|---|---|
| `frontend/src/components/structure-workbench/StructureEditor.tsx` | 唯一业务入口，加载状态、交互锁定、同引擎重试；构建别名选择叶子实现。 |
| `ReactStructureEditor.tsx` / `KetcherReactRuntime.tsx` | 按容器可见尺寸启动，懒加载官方 Editor 与 CSS，使用 onInit 接入共享文档。 |
| `IframeStructureEditor.tsx` | 私有 iframe 与同源 SDK 访问，仅回退构建使用。 |
| `frontend/src/structure/editor.ts` | 稳定的 `StructureEditorHandle` 与双引擎适配。业务不持有 iframeRef，也不读取全局 SDK。 |
| `frontend/src/structure/workspace.ts` | App 生命周期内的文档、版本、草稿、最后有效快照、页面所有者和导航恢复。 |
| `frontend/src/structure/nativeSession.ts` / `editorLifetime.ts` | 初始化与业务请求的会话中止、结果验证、同步销毁 Worker；不扩大业务接口。 |
| `frontend/build` / `frontend/patches` | 构建兼容、CSS 作用域、引擎清单、版本化 SDK 补丁及产物摘要。 |

六个页面共用此入口：结构工作台、均聚物性质预测、相似性探索、数据库查询、条件生成、Tg 逆向设计。保留单编辑器实例与原有页面保活策略；官方组件内部的小分子、宏分子视图共用一个结构服务。

## 内容和导航

- SMILES 用于业务接口，KET 保存布局及格式支持的标注；另存文本草稿、版本和最后成功快照。相同 SMILES 下的布局修改仍触发 300ms 快照保存及 AI 截图缓存失效。
- 文本输入保留 500ms 防抖、8000 字符上限、串行处理和最新输入优先。无效草稿不覆盖画板，聚合物 `*` 端基仍受保护。Indigo 导出的空 CX 标签尾段不进入业务 SMILES，有意义的扩展字段保留。
- 恢复优先使用 KET；通过官方 KET 解码器和所属编辑器的提交方法保留坐标，跳过 `setMolecule` 对键长的自动重算。解码返回后再次检查会话，旧页面不能迟到写入。失败时提示并尝试 SMILES；两者均失败保留草稿和快照。DFT 等外部写入经校验后使旧 KET 失效。
- 明确清空提交空文档；初始化暂空或读取失败不能清掉已有快照。保存与写入检查所有者和版本，过期回复不能覆盖新页。
- 初始化 15 秒、业务操作 8 秒；导航为 1.5 秒上限、重复请求去重、最新目标优先。`waitForGuard` 独立持有这一个导航截止时间，通过 AbortSignal 区分取消与超时；超时先处理旧会话，再放行路由。真实保存失败或超时后恢复最后成功快照并提示；无快照时明确说明。失败恢复会同步退役 SDK 和其 Worker，包括尚未触发 onInit 的初始化任务。取消切页只退出该导航的保存等待，不回滚或销毁保留的画板，也不取消其他消费者正在等待的自动保存。
- 离页使用 `saveForNavigation()`：共享文档与最后有效快照的版本及 SMILES/KET 完全一致、且没有进行中的写入时，可直接切页。正在恢复的编辑器不因此误报“保存失败”；文本草稿独立保留，由目标页就绪后继续处理。布局编辑照常增加版本并触发保存，不能只比较 SMILES。自动保存与同一会话、同版本的离页捕获共用进行中的 Promise；“生成SMILES”等明确读取操作仍导出实际画板。具体修复和验收见 [连续导航修复记录](ketcher-navigation-sync-analysis.md)。
- 保留图片识别失败回滚、外部 3D、复制、AI 的图片缓存与取消。KET 中纯分子通配原子的 Indigo 渲染错误可从同一捕获源转换为 Molfile 后生成 PNG。含文本、图形等节点时不做会丢失这些内容的 Molfile 降级；图片仍失败则显示既有 SMILES 兜底提示。

共享范围仍仅限 App 内存；刷新、账号、项目切换不新增保存行为。KET 恢复不包含撤销栈、缩放和选区。后端 API、数据库和 OpenScience iframe 通信桥保持原有边界。

## SDK 兼容与隔离

React 19、Node 22；四个 Ketcher 包精确锁定 3.8.0，另锁定 process 0.11.10、Rollup inject 5.0.5、patch-package 8.0.1。正常 `npm ci` 自动应用补丁并校验版本、补丁及产物 SHA-256；构建再次校验，失败终止。保留上游 peer 声明，禁止用强制解析或未验证的依赖替换绕过门禁。

补丁同时覆盖 ESM、CJS、实际加载的宏分子内嵌块及 standalone 官方类型入口。生命周期清理跟踪实例的 DOM 回调、订阅、观察器、节流尾任务与计时器；旧 root 清理结束后才允许新初始化。宏分子实例标识与菜单状态更新在 effect 中完成。全局 SDK 桥、provider 和注册表按实例身份清理。

Standalone 为每个结构服务创建独立 Worker，将 14 个请求方法串行执行。普通化学错误只拒绝当前请求；错误事件、异常消息、超时或销毁会终止整个服务并结束所有排队 Promise。保留官方内嵌 Worker/WASM 字节和消息协议；SDK 异步写入在恢复执行时检查所属会话。

开发预构建和生产打包共用模块级 process 注入，不写 `window.process`，不暴露构建机环境变量；保留 `global → globalThis` 和混合 CommonJS 转换。SDK 自身仍使用 `window.ketcher` 桥，业务层通过 onInit 注册的适配器访问。

官方 CSS 懒加载并限定在编辑器及所属弹层。原生实现按真实容器尺寸工作，缩放补偿仅用于 iframe。快捷键、复制粘贴和右键处理受实例可见性及事件来源限制；拖拽开始后允许跨出画板。焦点使用局部元素与 preventScroll，没有宿主全局原型补丁。

补丁来源和更新要求见 [补丁维护说明](../frontend/patches/README.md)。原始阻塞问题保留在 [历史分析](ketcher-native-blockers-analysis.md)，当前验收记录见 [SDK 门禁](ketcher-sdk-compatibility.md) 和 `docs/verification/ketcher-*`。

## 构建与复验

在 `frontend` 目录执行：

```bash
npm ci
npm test
npm run test:ketcher-worker
npm run build
npm run test:structure-resources
VITE_STRUCTURE_EDITOR_ENGINE=iframe npm run build
npm run test:structure-resources
npx playwright install --with-deps chromium
npm run test:structure-engines
```

完整浏览器门禁依次构建两种引擎和证明页，启动本机开发/生产服务器，校验 SDK 生命周期、六页面、布局、输出与资源。所有业务请求由测试夹具拦截，不创建真实任务；结果默认写入 `/tmp/nexpoly-structure-gate`，可用 `STRUCTURE_GATE_ARTIFACT_DIR` 指定。已有 Chromium 可通过 `STRUCTURE_CHROMIUM_PATH` 指定。

异步 SDK 条件使用显式轮询，不能直接把 async 谓词交给本项目 Playwright 1.63 的 waitForFunction；它会将 Promise 当作真值提前结束。iframe 探针先等待公开 UI 的同步完成状态，再读取旧 SDK，避免测试自身的原始导出请求与旧 Worker 的导入请求交叉。内存探针释放 CDP 句柄并检查旧实例弱引用，防止探针本身保留编辑器。

Docker 使用 `--build-arg VITE_STRUCTURE_EDITOR_ENGINE=react|iframe`；补丁及校验脚本在 npm ci 前复制。非法引擎值直接报错。构建输出 `structure-editor.json` 记录引擎、SDK/补丁/锁文件摘要和资源列表。React 运行时不得请求旧静态应用；iframe 构建不得包含原生 SDK 和其 CSS。

本次只交付代码与验证，不自动发布。第一次原生版本上线且完整运行一个正常发布周期、没有发生回退后，再另行清理 iframe 实现、旧静态应用和回退配置。
