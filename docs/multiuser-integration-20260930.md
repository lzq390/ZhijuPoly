# 多用户隔离 v2 本地整合记录（2026-09-30）

本次仅整合本地代码及文档；没有推送、创建 PR、部署、迁移实际开发/生产数据库、重启项目服务或提交科学计算。

## 代码范围

- 主工作区：`/data/lzq/gith/nexpoly-dev`，合入前 `main` 为 `402415b1696df8a7c6975c92b35b8a61df3edbf2`。
- 来源：`/data/lzq/gith/nexpoly-multiuser-scope-v2-20260929`，分支 `codex/multiuser-scope-v2-service-0019`，提交 `ac97ba175e21f2eefc3652d7cfb80ec7212fa8e0`。
- 候选验证工作区：`/data/lzq/gith/nexpoly-integration-20260930`，分支 `codex/integrate-multiuser-20260930`。
- 采用一次本地非快进合并，保留原提交历史。来源提交涉及 36 个文件；本记录是唯一额外跟踪文件，没有新增功能补丁。
- 范围包括 active_child v2 版本化验收合同、service 身份表最小权限 0019、R1/R2 权限补修、迁移/预检/恢复/发布契约适配，以及相应测试。九张私人表原有 RLS 与 0017/0018 原 SQL 保持不变。

## 本轮实测

以下均为 2026-09-30 新运行的结果，不沿用旧日志作为本轮通过依据。

| 验证范围 | 结果 |
| --- | --- |
| 一次性 PG16：service 权限、0019 迁移、治理、就绪 | 246 passed，0 failed，0 skipped |
| 一次性 PG16：登录、会话撤销、任务事件、私人产物、业务隔离与恢复 | 125 passed，0 failed，0 skipped |
| 发布隔离合同 | 3 passed，0 failed，0 skipped |
| active_child v2 纯文件验收合同 | 20 tests，OK |
| Compose、开发启动、生产演练、DFT 发布合同静态回归 | 101 passed，1 skipped；跳过项需要 owner-local `.env.dev` 的公共 CLI dry-run，本轮没有复制实际环境文件 |
| 最终组合工作区的 DFT 假进程监督器回归 | 10 passed，0 failed，0 skipped；验证原有未提交修复与本次整合并存 |
| 实际运行镜像烟测 | 通过；0017/0018 拒绝新版启动，0019 通过，六类权限漂移被拒绝，未认证私有请求为 401 |

运行镜像的 179 份文件（176 份应用/迁移/资源及 3 份依赖锁）与候选 `ac97ba1` 逐一核对一致。测试使用本轮新建的无网络 PG16 / Unix socket 和合成身份；未连接共享实际数据库，未挂载 GPU。测试容器、临时卷、socket 及烟测网络均已清理。

工作流策略、后端分片完整性、依赖锁、迁移策略、DFT 发布合同、提交身份及差异空白检查通过；两份修改的 shell 脚本分别通过 `bash -n`。候选 checkout 的九个文件因主机 umask 得到 775/664，规范化到 CI 要求的 755/644 后工作流策略通过，Git 内容无变化。首次发布合同测试因容器用户与挂载文件属主不一致触发原有保护；保留失败日志，从同一提交提取私有快照后重跑通过，没有放宽保护。

DFT 额外回归首次使用的通用镜像缺少标准路径 Python/curl，且默认 Broker 模式需要 systemd，得到 5 passed / 5 failed。第二次使用已有工具完整的镜像，但私有 `/tmp` 默认 noexec 导致 fixture 脚本无法执行。最终采用无网络、源码只读、无 GPU/host PID/systemd 的临时容器，显式使用源码支持的 standalone 假进程模式及容器私有可执行 `/tmp`，原测试源码保持不变。此结果验证监督器假进程行为，不代表 Broker 或真实 GPU 验收；全部尝试日志保留。

ShellCheck 在主机和现有测试镜像中均不可用，未安装额外软件。前端没有变更，未重跑完整前端构建/类型检查或完整远端 CI。上述结果仅支持本地整合，不等同于整个平台科学验收或生产发布。

## 保留的独立 DFT 改动

以下三份原有改动未暂存或纳入本次合并；前后字节哈希一致，备份在本轮证据目录：

- `workers/monomer_dft_worker/run_host_worker.sh`
- `workers/monomer_dft_worker/tests/test_worker_supervisor.py`
- `docs/monomer-dft-recovery-20260929.md`

DFT 的临时恢复措施转为正式发布仍是独立工作，不随本次代码合入部署。

## 运行与部署边界

当前开发后端绑定主工作区源码，但 Uvicorn 没有 `--reload`；现存 Web 进程继续使用已加载的旧代码。实际周期健康检查显式使用 `--schema-target user-isolation-0018 --service-context`，新版保留该历史检查，因此合入不需要重启现存容器。合入后仍须核对容器健康、PID、启动时间和重启计数。

这不表示新版已在服务中启用。以后重启/重建应用，或进程退出后自动恢复，都会加载新代码；新版 Web 就绪要求精确 0019 ledger 和实际 service 登录角色的有效权限。应先单独安排并审查实际库 0019 维护与发布，遵循 [0019 维护说明](service-auth-least-privilege.md)。不得删除 ledger、重放 0018 扩权或默认回退不认识 0019 的镜像。

## 未完成的科学验收与证据缺口

- `fixed128-v1` 的历史口径仍为 126 PASS / 1 FAIL / 1 BLOCKED，不因本次整合改写。
- `acceptance-scope-v2` 仍为 0 PASS / 0 FAIL / 126 BLOCKED / 2 NOT_RUN。
- 126 BLOCKED 表示旧执行原件已删除、无法完整复核，不是 126 个功能故障。两个新真实故障实例及其恢复门禁尚未执行。
- `SCI-MIX-v1` 科学混合长测仍为 NOT_RUN，独立于 128 项分母；本轮新增科学计算为 0。
- 本轮权限/CPU 回归不能填补历史科学执行原件，也不能替代真实 GPU 验收。

后续应分别安排实际库迁移/发布、两个真实故障实例与科学混合长测；不得因本地整合自动执行这些工作。

## 本轮证据

证据根目录：`/data/lzq/gith/nexpoly-integration-20260930/.runtime/local-integration-20260930/`。

- `pg-validation/validation-summary.json`、原始 PG16/发布合同日志、镜像哈希清单、清理回执。
- `static-results.json`、`workflow-policy-mode-normalized.log`、`scope-contract.log`、`static-regression.log` 及各策略日志。初次工作流失败日志保留；最终结果见规范化后的日志。
- `combined-dft-supervisor*.log`、`main-dft-before.sha256`、两份 DFT 修改补丁与恢复报告快照；最终通过结果见 `combined-dft-supervisor-attempt3.log`。
- `merge-verification.json` 保存最终合并提交、父提交、DFT 保留验证及服务前后状态。

9 月 29 日独立副本报告继续作为当时的阶段记录；本记录补充本地合入状态，不把其“当时尚未合入”的历史描述倒改为新结论。
