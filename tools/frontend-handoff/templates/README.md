# NexPoly 前端统一修改交付包

这是当前完整原生前端的可编辑源码快照。当前16个科研模块的统一界面、公共导航、登录交互、实验数据和实验流程演示均保留。接口返回值是合成演示数据，不代表科学计算结果。智能体首页保留本地占位。

## 启动与构建

使用 Node.js 22.12 或更新的22.x、npm 10+。首次安装依赖需要 npm 网络访问；安装完成后页面运行不需要后端、账号、数据库、GPU或外部服务。

```bash
cd frontend
npm ci
npm run dev:handoff
```

打开 http://127.0.0.1:5173/ 。修改源码后自动刷新。`npm run dev` 同样启动交付模式。

```bash
npm test -- --maxWorkers=2
npm run build:handoff
npm run preview:handoff
```

构建预览默认地址为 http://127.0.0.1:4173/ 。不要直接双击 dist/index.html；编辑器模块和路由需要本地HTTP服务。

## 演示状态

在初次打开页面时添加 `mockScenario=success|loading|empty|error`，分别对应合成成功数据、可取消的加载、空结果和受控错误；默认success。场景在当前页面生命周期的模块导航中保持，切换场景请修改地址参数并刷新。

默认使用合成登录态。添加 `mockAuth=guest` 可查看游客界面并操作登录弹窗。认证操作不随业务loading/error场景失败；所有演示身份、会话、密码和资料仅用于本地交付界面，无真实账号或服务器。刷新会重新建立演示状态。

例如：

- `/database-filter?mockScenario=empty`
- `/monomer-dft?mockScenario=error`
- `/structure-workbench?mockAuth=guest`

未知Mock接口返回501，表示需要补齐适配，不会转发给真实服务。登录检查不受业务加载/错误场景影响；服务能力及业务请求按所选场景呈现。

## 页面与修改基线

16个科研模块：结构工作台、知识检索、聚合物生成、单体逆合成反推、聚合物相似性探索、数据库查询、数据库筛选、数据库分析、均聚物性质预测、单体正向聚合、MD模拟、单体MD模拟、单体DFT、条件聚合物生成、Tg逆向设计、高通量优化演示。

此外保留首页、实验数据采集/看板和实验流程演示。完整路由以 `frontend/src/routing.ts` 为准，公共规范见 `design-system/polyprop/MASTER.md`。数据库分析的五种数据子页面、批量聚合、任务中心与登录界面也可修改。

统一字体、颜色、间距、表单、表格、抽屉和响应式时保持路由、接口字段、校验、任务状态、结构编辑和键盘可访问性语义。单体正向聚合整体滚动是公共页头规范的明确例外。2D/3D编辑器是真实本地组件；科学计算和查询结果为Mock。

## 浏览器检查

安装测试浏览器后，启动交付模式并在另一个终端执行：

```bash
npx playwright install chromium
HANDOFF_BASE_URL=http://127.0.0.1:5173 npm run test:handoff
```

几何、焦点与动效测试需要另开本地fixtures服务，步骤见 `docs/page-unification.md`。以上变量写法用于bash；Windows PowerShell可先使用 `$env:HANDOFF_BASE_URL="http://127.0.0.1:5173"` 设置变量。详见 `docs/page-unification.md`。页面几何测试有独立夹具，`test:handoff`专门验证交付包实际Mock及网络边界。

本次实际验证结果见 `VALIDATION.json`。文件清单见 `FILE-MANIFEST.sha256`；改动回传说明见 `RETURN_GUIDE.md`。
