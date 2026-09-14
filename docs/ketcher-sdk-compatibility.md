# Ketcher 原生组件验收（2026-09-10）

默认已切换为 React。固定 React 19.2.4、Node 22、四个 Ketcher 包 3.8.0，使用 `nexpoly-3.8.0-r3` 补丁；iframe 3.7.0 保留一个发布周期的构建回退。本次完成代码和验收，没有发布到生产。

SDK 修复保存在 `codex/ketcher-react-compat`，提交 `b02a863`。主工作区保留其他任务的改动，使用当前依赖锁文件。两个工作区分别进行了正常安装与 SDK 运行验证；以下最终报告使用主工作区锁文件 `606163d3c7dff96603823ffcae38b413590c4be32a974e4b56fae93206b3f861`。

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
