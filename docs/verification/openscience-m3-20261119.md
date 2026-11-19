# OpenScience M3 开发双账号工具归属验收

2026-11-19 在独立环境 `dev-m1` 完成六项生产同版工具的逐用户验收。复用 M1 的 `test-user-a`、`test-user-b` 和原私有卷，通过 M2 网页登录，为每人新建一段 OpenScience 对话。两个账号分别成功完成预测、聚合及 DFT 提交，各计入本人三笔用量。双方的 DFT 列表与各自原生任务分页逐项一致，用已知任务或产物标识访问对方的状态、结果、单文件和整包均得到 HTTP 404。逐项状态、镜像 ID、用量增量及下载摘要见[机器记录](openscience-m3-20261119.json)。[M2 登录与工作区绑定](openscience-m2-20261119.md) · [隔离规划](../openscience-per-user-instances-plan.md)

## 从网页登录到原生接口

开发网关派生自 M0 核对的本机 11 月镜像 `openscience-capability-gateway:91f5d81-release-20261111`。离线构建校验基底、工具源码和依赖锁文件。六项 MCP 工具沿用原镜像的名称、参数和结果处理。网关按请求兑换授权，运行时无需共享业务账号的用户名、密码或登录缓存。[构建入口](../../ops/capability-gateway-per-user-dev/build_image.py) · [网关请求处理](../../ops/capability-gateway-per-user-dev/delegation.ts)

路由从已登录的页面和用户专属实例取得工作区、对话、消息及工具调用标识，将原登录、工具名和参数摘要绑定到本次操作。DFT 提交还需发起页的确认。网关通过独立 Unix socket 兑换一次性原生令牌，其范围限定到 HTTP 方法、路径、查询串和请求正文摘要。原生接口在当前登录用户的权限下处理请求。写操作使用由操作标识派生的稳定幂等键。权威服务检查原登录、工作区当前实例代次与轮次到期时间；原生令牌最长有效 30 秒，成功领取一次即消费。回收或重启会使旧代次的委托失效，已获原生启动授权的任务保留原 owner。[工具路由](../../ops/openscience-m3-dev/tool-router.mjs) · [授权校验](../../backend/app/auth/openscience_delegation_dev.py)

## 六项工具的双向核验

Chromium 的三个独立上下文分别登录 A、B 和 A 的第二台模拟设备。浏览器从已登录的 OpenScience 页面发送对话请求，读取持久化的工具调用和结果。下表的 HTTP 状态来自原生接口。跨账号查询在 MCP 工具结果中显示 `Job not found`，原生请求记录同时验证为 404。[浏览器用例](../../scripts/ci/verify_openscience_m3.mjs)

| MCP 工具 | A、B 分别观察到的结果 | 每个账号本轮的原生用量 |
|---|---|---|
| `predict_polymer_properties` | 分别对 `*CC*`、`*CCC*` 调用本机随机森林模型，分别返回 70.4328 °C 和 74.6038 °C，与各自原生记录的 Tg 一致 | HTTP 200，`predict` 成功计量 1 次 |
| `polymerize_monomers` | 分别输入乙烯和丙烯，真实 SMiPoly 在 `polyolefin` 类别各返回一个候选；候选结构、顺序和总数与各自原生记录一致 | HTTP 200，`polymerization` 成功计量 1 次 |
| `dft_job_submit` | 各自确认并提交单点计算，得到不同任务。A 的第二个登录尝试确认第一台设备的提交收到 403，原发起页仍可确认 | HTTP 202，原生任务、授权启动和完成记录各 1 条，`monomer_dft` 成功计量 1 次 |
| `dft_job_list` | 两个账号分别查询第一页（每页 50 项），完整任务 ID、顺序和总数与本人的数据库分页一致，对方新任务均未出现 | HTTP 200，新增 0 笔 |
| `dft_job_status` | 本人读取直至任务完成。A 查 B、B 查 A 均被拒绝 | 本人 200，对方 404，新增 0 笔 |
| `dft_job_result` | 本人能读取结构化结果、产物清单和下载入口，测试能量与本人原生任务记录一致。A 的第二个登录也能读取本人结果。A、B 互查均被拒绝 | 本人 200，对方 404，新增 0 笔 |

产物下载沿用 NexPoly 父页的网页登录及原生下载接口。浏览器逐一下载本人初始结构、最终结构和结果 JSON，均收到 HTTP 200，实际字节数与 SHA-256 匹配结果清单。两个账号都成功下载本人 ZIP。对浏览器实际收到的 ZIP 解包，条目名称、数量、逐文件大小及 SHA-256 均与本人单文件下载相符。用另一账号的登录访问相同单文件地址和整包地址，两个方向均为 HTTP 404。[逐文件记录](openscience-m3-20261119.json)

## 幂等与用量核对

验收在每个账号的三种计算写入首次成功后模拟响应丢失，使用同一操作再次调用。预测、聚合的两次返回一致，DFT 两次返回同一任务。每人每项均有两条成功 HTTP 记录、一个计量操作和一次成功结算。DFT 另核对本人任务的授权启动次数、已完成用量记录及可用产物。只读查询和拒绝访问对应的新增计量均为零。[重试注入](../../scripts/ci/openscience_m3_retry.mjs)

对账程序在测试前记录独立 PostgreSQL 的基线，测试后用本轮新对话、调用标识、允许的登录及新任务逐项关联授权、原生请求、任务和用量。业务成功以该用户的原生计量操作成功结算为准。两名测试用户之外，登录、工具操作、DFT 任务、计量操作、计量事件、原生请求观察、用量事件、用量尝试八类记录增量均为零。[只读归属核对](../../scripts/ci/openscience_m3_ledger.py)

单独的 PostgreSQL 鉴权测试还覆盖了实例换代、旧凭据、轮次截止、同一原生令牌的并发领取、注销和错误目标。M2 原有浏览器用例回归通过，覆盖网页切号、单设备退出、撤权、校验服务中断和票据到期。[委托测试](../../backend/tests/test_openscience_m3_delegation.py) · [M2 浏览器用例](../../scripts/ci/verify_openscience_m2_browser.mjs)

## 在本机重跑

沿用 M2 的 Linux Docker、原 M1 注册表和两份私有卷、本机固定镜像、Node.js 22、前端锁文件依赖和 Playwright Chromium。M3 另需本机 Buildx、固定摘要的玻璃化转变温度模型及镜像内的科研依赖。从仓库根目录执行，报告路径须为新文件。模型存放位置可通过启动器的 `--m3-prediction-model` 指定。每次验收均在持久开发库保留两个新任务、对话与计量记录；每人消耗两笔化学计算和一笔 DFT 的开发月度额度。两个实例随后回收，私有卷和 DFT 产物保留。[开发启动器](../../scripts/openscience_m2_dev.py)

```bash
m3_image=$(python3 ops/capability-gateway-per-user-dev/build_image.py | python3 -c 'import json,sys; print(json.load(sys.stdin)["image_id"])')
python3 scripts/openscience_m2_dev.py \
  --m3-gateway-image "$m3_image" \
  --verify .runtime/openscience-m3/replay-01.json
```

## 验收边界

本轮用确定性的对话模型指定工具和参数，使两账号可以重复执行相同验收步骤。DFT 走原生任务创建、调度、授权启动、结构准备、落盘、计量和下载链路。单点能量与力使用 CPU 谐振计算夹具，本轮证据覆盖任务和产物归属。GPU/AIMNet2 的科学数值、真实对话模型的用量以及生产容量仍需独立验证。预测与聚合覆盖表中列出的输入。生产 9011 全程保持原状。[对话夹具](../../ops/openscience-m3-dev/model.py) · [DFT 开发计算配置](../../backend/app/auth/openscience_m3_worker.py)
