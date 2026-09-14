# Ketcher 原生组件兼容与验收

以下保留 2026-09-10 的 SDK 验收基线及 2026-09-14 的提交验收，统计均对应各自版本与测试轮次，不代表实时运行状态。

## SDK 基线（2026-09-10）

默认已切换为 React。固定 React 19.2.4、Node 22、四个 Ketcher 包 3.8.0，使用 `nexpoly-3.8.0-r3` 补丁；iframe 3.7.0 保留一个发布周期的构建回退。本次完成代码和验收，没有发布到生产。

SDK 修复源于提交 `b02a863`，已整合进主线 `4793ce7`；复验不依赖保留原兼容分支或临时工作树。当时两个工作区分别进行了正常安装与 SDK 运行验证；以下基线报告使用主工作区锁文件 `606163d3c7dff96603823ffcae38b413590c4be32a974e4b56fae93206b3f861`。

## 验收结果

| 范围 | 结果与记录 |
|---|---|
| 正常安装 | 全新 `npm ci` 成功，四包补丁及 11 个 SDK 产物摘要校验通过；没有强制安装或 peer 声明修改。[安装记录](verification/ketcher-clean-install.json) |
| 测试与构建 | 前端 96 个测试文件、889 个用例，以及 Worker 执行器 ESM/CJS 共 12 个用例通过。TypeScript、两种引擎的 Vite 构建及默认 React 构建通过。 |
| SDK 运行 | 开发、生产均通过文本标注、Miew 分子加载、断言失败、util 异步、四格式并行导出、旧 Worker 回复隔离、StrictMode、初始化中卸载及失败重试。[开发](verification/ketcher-native-development.json) · [生产](verification/ketcher-native-production.json) |
| 内容与页面 | 六个深链及互切、真实原子拖动、相对坐标与文字标注恢复、无效草稿、清空、导航超时、聚合物端基、DFT 外部写入、识别失败回滚和外部 3D 通过。[原生开发](verification/ketcher-workspace-development.json) · [原生生产](verification/ketcher-workspace-browser.json) · [iframe 回退](verification/ketcher-iframe-fallback.json) |
| AI 输出 | PNG 实际像素、布局变化后的图片缓存失效、取消、SMILES 兜底及通配原子 PNG 通过。[报告](verification/ketcher-ai-output.json) |
| 交互与资源 | 390、1024、1440、2560 宽度下原子可见且点击命中，右键菜单、复制粘贴正常；宿主输入及隐藏宏分子问号快捷键隔离通过；对应引擎的资源无缺失。 |
| Docker | 默认 React 与显式 iframe 镜像均构建成功，nginx 配置、非 root 文件读取权限、HTTP 资源检查通过。[镜像记录](verification/ketcher-docker.json) |
| 默认产物 | 无引擎参数的构建已识别为 React，并在浏览器中成功输入、导出 CCO，保持一个编辑器和一个 Worker。[运行采样](verification/ketcher-runtime-samples.json) |

## 生命周期与体积

SDK 预热后分别连续十次重挂载，旧编辑器弱引用始终为 0，活动 Worker 保持 1，卸载后为 0；监听数量无持续增长。没有未处理异常或已识别的 React 渲染警告。

| SDK 环境 | 就绪 | GC 后 JS 堆：预热→第十次 | DOM 节点 | 监听器 |
|---|---:|---:|---:|---:|
| 开发 | 8547 ms | 95.16→97.46 MB | 3524→3524 | 1223→1223 |
| 生产 | 3912 ms | 81.05→83.72 MB | 2136→2136 | 1221→1221 |

默认应用的原生 SDK 主分块为 23.20 MB，gzip 6.81 MB；完整 Vite 生成资源共 27.22 MB。宏分子和 Miew 保持分块，官方 Worker/WASM 随产物交付；一个回退周期内仍保留旧静态应用。完整目录体积、各分块、摘要及 Worker/DOM/堆采样见 [最终矩阵](verification/ketcher-native-gate.json)。

这些数值是当前机器上的验收样本，含开发编译及缓存差异，不代表性能改善。JS 堆不等同于总浏览器、GPU 或 WASM 内存。一次开发深链探针超时的过程和后续通过记录保留在最终矩阵中；未将其归因于未经证实的原因。

## 复验和回退

在 `frontend` 目录运行 `npm run test:structure-engines` 可依次执行 SDK、原生开发/生产、iframe 和 AI 浏览器门禁；所有业务 API 由夹具拦截，不创建真实任务。单元测试通过公共编辑器边界验证业务，实际 SDK 由浏览器验收。

```bash
# 默认原生构建
npm run build

# 保留一个发布周期的构建回退
VITE_STRUCTURE_EDITOR_ENGINE=iframe npm run build
```

维护、内容传递边界和未来清理条件见 [组件化说明](ketcher-componentization.md) 与 [补丁维护说明](../frontend/patches/README.md)。

## 提交验收（2026-09-14）

以 `de6c52b` 为基线，在隔离候选工作树组合原生 Ketcher 3.8/r3、iframe 回退、共享文档与导航、SDK 生命周期、路由按需加载、预加载和开发压缩，最终集成为 `4793ce7`。当时并行的页面统一改动及 `/tmp` 中 lazy Macro、独立生产 root 性能原型未纳入候选。

| 范围 | 当轮结果 |
|---|---|
| 安装、单测与 Worker | 干净 `npm ci`、11 个 SDK 制品及 4 个补丁的 r3 校验通过；101 个测试文件、944 项单测通过，实际 patched Worker 的 ESM/CJS 回归 12 项通过。最后 CSS 修复另跑相关路由、重试与启动用例。 |
| 构建与浏览器 | React、iframe、SDK 兼容构建及引擎资源隔离通过；开发／生产 SDK、原生开发／生产、iframe 生产共享画板验证通过，覆盖六入口、真实拖动、KET 标注与布局、导出、清空、3D 和实例退役。 |
| 输出与加载 | AI PNG 缓存、布局失效、取消及端基输出通过；入口失败、CSS 失败、历史导航和开发 HMR 的对应浏览器复验通过。CSS 新增五项回归，镜像 manifest 校验新增 14 项 fixture。 |
| 压缩 | gzip、Brotli、正文等价、304、Vary、HEAD、Range、过期依赖版本及源码／HMR 处理通过；样本 15,595,692 字节压缩为 gzip 5,790,453 字节，只代表传输验证。 |
| Docker | 两种引擎镜像实际构建与 nginx、配置、资源、文件权限检查通过；最后 CSS 重试修复后单测、生产构建和浏览器另行通过，没有再次重建这两个镜像。 |

完整结果及保留的失败见[提交验收 JSON](verification/ketcher-review-20260914.json)。首次整套 gate 退出码仍为 1；修正 CSS、manifest 校验和浏览器探针后仅重跑对应项，不能视为最终版本重新执行过全部门禁。探针修正包括避免向跨源错误 iframe 写入 localStorage、使用候选页面实际标题及固定 CCO 夹具，并继续保留产品 `pageerror` 断言。

原始日志目录 `/tmp/nexpoly-ketcher-review-20260914-URNIPB` 仅为当时的本地证据位置，不随仓库交付。将 `git archive HEAD` 固定到候选 tree 的一次性 harness 不入库；正式校验脚本继续使用仓库提交。逐轮 review 正文已合并到本节与维护说明，Git 历史保留原文。

此次验收没有更新 9000，也没有重建或重启实际 9001；源码挂载可能触发当时的 HMR。功能验收不能代替相同条件的性能对照，也不能据此宣称快于 9000。依赖审查和未实现优化的限制见[维护说明](ketcher-componentization.md)。
