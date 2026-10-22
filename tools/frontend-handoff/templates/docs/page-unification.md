# 当前16个科研模块的统一页面验收

完整视觉规范以 design-system/polyprop/MASTER.md 为准。当前验证脚本覆盖16页和13档视口，包含新增单体逆合成页面及1023px导航边界。历史15页统计不作为本包验收结果。

公共页头来自 ModulePageHeader 和 module-page.css，主题来自 workbench-theme.css。普通视口标题位置为工作区左20/上22px，20px字、30px行高、62px页头；宽≥2000且高≥1120时为左36/上30px、28px字、42px行高、84px页头；宽≤899时为左16/上20px、20px字、30px行高、60px页头。页面标题字重750。

15页固定页头、正文独立滚动；单体正向聚合的单次/批量页面根节点整体滚动是唯一例外。平台移动导航的断点为1024px，与标题的899px断点分别检查。每页仅一个可见h1，状态变化和抽屉打开不得遮挡标题或引入横向溢出。

## 两类验证

`npm run test:handoff` 对实际交付模式的浏览器Mock做检查，包括原生页面、四业务状态、认证、编辑器、批量操作、任务流以及本地网络边界。先运行 `npm run dev:handoff`，通过 HANDOFF_BASE_URL 指定地址。

原有页面几何、焦点、动效脚本使用Playwright专用HTTP响应夹具。它们需要另启动 `npm run dev:fixtures -- --port 5174`，再运行：

```bash
PAGE_BASE_URL=http://127.0.0.1:5174 npm run test:pages
DRAWER_BASE_URL=http://127.0.0.1:5174 npm run test:pages:focus
PAGE_BASE_URL=http://127.0.0.1:5174 npm run test:pages:states
MOTION_BASE_URL=http://127.0.0.1:5174 npm run test:motion
```

fixtures模式也是本地合成服务，没有真实后端代理。使用它是为了让原有浏览器测试的响应拦截生效；不会作为实际交付模式的替代验收。

13档视口：1920×1080、2560×1440、1440×900、1024×768、1023×768、390×844、899×900、900×900、901×900、1999×1120、2000×1119、2000×1120、2560×1119。

PAGE_PATH、PAGE_VIEWPORT可筛选路由及宽度，PAGE_ARTIFACT_DIR设置截图和JSON报告目录；焦点脚本使用DRAWER_ARTIFACT_DIR。完整本次结果见包根目录VALIDATION.json。所有测试只操作合成数据，不进行真实科学计算。
