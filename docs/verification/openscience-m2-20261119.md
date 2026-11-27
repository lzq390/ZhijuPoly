# OpenScience M2 开发双账号浏览器验收

2026-11-19 在 `dev-m1` 完成网页登录、一次性票据交接和按用户路由。两个 Chromium 浏览器上下文分别保存各自的 Cookie，用于模拟两台设备；在同一上下文中另开标签验证切号和注销传播。父页运行仓库中的 NexPoly，嵌入页使用 11 月 OpenScience 静态包，登录由真实 NexPoly 认证服务和独立 PostgreSQL 16 处理。[机器记录](openscience-m2-20261119.json) · [隔离规划](../openscience-per-user-instances-plan.md)

## 登录如何找到 M1 的数据

| 环境内用户 | NexPoly 开发账号 UUID | M1 工作区 | 固定 UID/GID |
|---|---|---|---:|
| `test-user-a` | `10000000-0000-4000-8000-000000000001` | `463fc2f89367468bb7fc6e048d522c9e` | 58161656 |
| `test-user-b` | `10000000-0000-4000-8000-000000000002` | `3c137e9c236d4998bed842786c0f4548` | 43620582 |

M1 保留的是用户键、UID 和卷。M2 在开发账号库中将两个原用户键一对一绑定到上述可登录的 UUID。签票端根据 NexPoly 登录 Cookie、当前登录标识、CSRF、会员能力、环境开关和试点名单查出绑定。浏览器只向固定 Agent 来源提交票据。数据库以摘要保存票据，60 秒有效，并以条件更新保证只兑换一次。[签票与复核](../../backend/app/auth/openscience_dev.py) · [开发账号初始化](../../backend/app/auth/openscience_seed.py)

父页和 Agent 分别使用 `https://app.m2.test:18760` 和 `https://agent.m2.test:18761`，各写自己的 Secure、HttpOnly、host-only Cookie。工作台页面还持有该页的上下文。路由同时核验二者，按原登录创建时间拒绝迟到交接，并通过 M1 管理器启动对应实例。每个实例独享 Unix socket，路由重新构造上游请求头并加入该实例的凭据。实例没有宿主发布端口。[开发路由](../../ops/openscience-per-user-dev/m2-router.mjs) · [前端交接](../../frontend/src/components/PrivateAgentFrame.tsx)

OpenScience UI 镜像 ID 为 `sha256:fea8a3c294bb324a98041f8f8354448e6ca5c6502dedc565c4b884994c567bb0`。静态文件取自本机固定镜像。开发适配器按完整哈希核对主 JS，再将嵌入父页设置为本次开发来源。父页启用 `VITE_OPENSCIENCE_PER_USER=true`，并单独指定 HTTPS 的 `VITE_OPENSCIENCE_ROUTER_URL`。两个后端继续使用 M1 的 11 月模板与合成模型。[静态适配](../../ops/openscience-per-user-dev/m2-ui.mjs) · [M1 镜像与恢复记录](openscience-m1-20261119.md)

## 浏览器观察

- A、B 在相同路径各自看到 `same-project`。从 NexPoly 侧栏进入普通对话，再打开项目内对话，标题和消息均为当前账号的合成标记。打开项目的“文件”页签及 `notebook.txt`，同名文件分别显示两份不同的本人正文。消息接口也读到了本人项目会话中的文件附件。
- 两台设备登录 A 时读到相同的原会话。第一台退出并登录 B 后，第二台仍能读取 A。双方各自查询对方的测试项目会话均得到 HTTP 404。B 在项目页单独刷新 iframe 后仍能使用父页侧栏；刷新整个父页也能找回原会话。各开启一条事件流并新建一条短期测试会话，双方都收到自己的实时事件，观察期间对方标记为零。随后删除本次新建的两条测试会话。
- 重复提交已成功兑换的票据收到拒绝。超过 60 秒才首次提交的票据也被拒绝，同一有效登录随后取得的新票据可兑换。路由回归另覆盖迟到的旧登录交接、同一页面标识与错误 Cookie 的组合、浏览器凭据过滤。PostgreSQL 集成回归覆盖并发兑换和按原登录撤销。[浏览器用例](../../scripts/ci/verify_openscience_m2_browser.mjs) · [路由用例](../../ops/openscience-per-user-dev/m2-router.test.mjs) · [权威服务用例](../../backend/tests/test_openscience_m2_authority.py)

## 30 秒撤权窗口

最终一轮于北京时间 16:53:17 至 16:54:56 完成，Chromium 153.0.8010.12。

| 导致失效的操作 | 旧工作台失效 | 父页侧栏清空 | 旧上下文请求被拒 | 已打开 SSE 断流 | SSE 重连 |
|---|---:|---:|---:|---:|---:|
| A 在第一台设备退出 | 0.273 秒 | 0.386 秒 | 2.059 秒 | 2.063 秒 | HTTP 401 |
| 从试点名单移除 A | 2.777 秒 | 2.862 秒 | 0.740 秒 | 0.742 秒 | HTTP 401 |
| 开发认证服务中断 | 20.140 秒 | 20.068 秒 | 2.857 秒 | 23.729 秒 | HTTP 401 |

计时在点击退出、提交名单变更或暂停开发认证容器之前开始。旧页失效以原 iframe 被卸载或进入失效页为准。另一个持续打开的浏览器探针用同一页的授权订阅真实 SSE，先收到事件再开始测撤权，避免将 iframe 卸载当成服务端断流。表中的新请求检测和断流独立计时。名单恢复后，原页面上下文仍被拒绝；认证服务中断演练结束后恢复了开发认证容器。[完整时序](openscience-m2-20261119.json)

前端四组回归共 72 项通过，TypeScript 编译通过。认证服务的真实 PostgreSQL 及 Unix socket 测试 2 项通过，流式路由综合回归和 M1 管理器 6 项回归通过。前端另覆盖标签页后台隐藏时清除侧栏，回到前台后只有原 iframe 经权威校验并响应本次随机质询才能恢复快照。

## 同一登录遇到实例回收

另用一套临时注册表、真实 11 月后端和独立合成签发方复核了路由恢复。Chromium 只兑换一次票据，工作区自动回收后刷新，6.633 秒读回原文件；单独结束该测试后端并保留旧连接入口，同一页面再次读取耗时 16.963 秒。三代实例的 UID、卷和文件 SHA-256 一致。只有读取可以自动重试一次，若写入已发出而结果不明，路由返回 503 供调用者核对。此专项单独建立和清理开发资源。[路由恢复回归](../../ops/openscience-per-user-dev/m2-router.test.mjs)

## 在本机重跑

需要本地 Linux Docker Engine、原 M1 注册表及两份已初始化的测试卷、启动器固定的本地镜像、Python 3、PostgreSQL 客户端、nginx、OpenSSL、Node.js 22、与 `frontend/package-lock.json` 一致的依赖和该版 Playwright 的 Chromium。在仓库根目录执行，报告路径须为新文件。

```bash
python3 scripts/openscience_m2_dev.py --verify .runtime/openscience-m2/replay-01.json
```

启动器仅绑定本机回环地址。Chromium 为上述两个开发域名配置本地解析、代理旁路和本机证书公钥信任；可执行验收无需修改系统 hosts 或信任库。只启动开发入口可省略 `--verify`。交互使用时需在开发浏览器配置这两个域名和开发证书。专用 PostgreSQL 卷和 `~/.local/state/nexpoly-openscience-m2-dev` 下的账号凭据、证书由开发机保留，凭据和票据不写入报告。验收结束后关闭临时服务，并将初始状态为已回收的两组后端重新回收；原 M1 私有卷与绑定保留。[启动器](../../scripts/openscience_m2_dev.py)

本轮覆盖一个独立环境、一组真实父页与 Agent 来源及 Chromium 的两个设备上下文。第二套部署来源、生产科研工具逐用户归属、真实模型计量、磁盘硬配额、物理设备/其他浏览器兼容和生产灰度各需相应环境的证据。此次没有访问、变更或切换生产 9011。
