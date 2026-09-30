# NexPoly 工作区整理记录（2026-09-30）

这是多用户隔离 v2 本地合入后的独立整理阶段。原合入记录见
[多用户隔离整合记录](multiuser-integration-20260930.md)；其中的未推送状态和测试结果是当时的快照。

## 保留代码与独立 DFT 修复

多用户隔离来源 `ac97ba175e21f2eefc3652d7cfb80ec7212fa8e0` 已包含在本地合并
`496c558e22b4f7a1930ebc2268e2c04103f028fa` 中。原先留在主工作区的 DFT runner、
supervisor 测试和事故报告按独立修复提交
`5c1a13fa98316892dbc6ee153d6cb70f2ad78b70` 保存，不改写多用户合并。

DFT 审查补齐了“活 socket 已 bind、尚未 listen”的边界：ECONNREFUSED 不能单独
证明 socket 失效。清理前还须确认内核 Unix socket 登记表中没有该路径；登记表
不可读或损坏时拒绝清理。原有监听、symlink、类型与 inode 检查保留。
本轮新增回归先在旧 runner 上复现失败，修复后完整假进程监督器套件 13 passed；
日志位于主工作区 `.runtime/workspace-cleanup-20260930/dft/`。

## 工作区归档与清理

盘点范围为 12 个 NexPoly checkout 和一份历史 Git 元数据快照。各本地及远程跟踪
分支的 11 个不同顶点均已被本地合并主线包含。清理前重新核对候选目录 Git 状态，
限定目录 lsof 没有发现打开文件或 cwd 引用，已查 Docker 挂载及 systemd 属性没有引用。

归档根目录（权限 0700）：

`/data/lzq/gith-archive/nexpoly-workspaces-20260930/`

- `nexpoly-integration-20260930`：完整 tar 归档并用 `tar --compare` 验证后，
  通过正常 `git worktree remove` 移除。已合入分支
  `codex/integrate-multiuser-20260930` 通过 `git branch -d` 删除。
- 来源提交另有本地保护 tag `archive/multiuser-integration-20260930`；清理前
  全部 Git 引用保存在已验证的 `nexpoly-dev-before-cleanup.bundle`。
- `nexpoly-multiuser-scope-v2-20260929` 是独立仓库，不是上述 linked worktree。
  整个目录原子移动到归档根目录的 `workspaces/`，约 32.12 GiB 的历史证据、
  环境、依赖及产物全部保留。移动前后目录 inode 与 Git refs 一致，Git 状态干净。
- integration 原 `.runtime/local-integration-20260930/` 证据另存为归档根目录的
  `evidence/local-integration-20260930/`；完整 tar 中也保留原位置。
  因此旧合入记录中的证据路径应从此归档位置读取。
- `cleanup-verification.json` 保存归档 SHA-256、保护引用和清理后的工作树状态。

主开发仓库只保留一个活动 worktree 和 `main` 分支；归档 tag 只作本地恢复引用。
没有使用 force、reset --hard 或 git clean，也没有丢弃未提交内容。

生产 checkout、5 个历史 release checkout、3 个 private release checkout、
运行目录与历史部署 Git 快照保留。生产及开发 checkout 有服务引用；历史发布副本
尚未完成保留策略核对，不能仅因提交已合入就删除。assets、lab 与结构工具也不是
本次冗余工作树清理对象。

## 远端验证与部署边界

整理后的代码后续通过独立 PR 按仓库 squash 策略合入；PR 和最终 main SHA 的实际
Actions 结果为远端验证依据，不能用这里的本地定向测试代替。失败的适用 CI 在确认
没有生产副作用后先重试一次，再区分偶发失败与可复现缺陷。

squash 后若需将本地 main 对齐远端，将原本地整合历史保存在明确命名的归档分支，
再从远端新建跟踪 main；不以强制重置丢弃本地 merge 和验证来源提交。

main 的既有 CI 在通过检查后会发布 Backend、Web、OpenScience UI 镜像并执行
隔离烟测；它没有生产 SSH、生产数据库迁移或生产服务重启步骤。本轮不执行实际
0019 迁移、生产部署、真实科学计算，也不移除 9 月 29 日的 DFT 事故缓解措施。

只读检查另发现生产 runtime 中 8 月 20 日的首次部署 authority 链保留旧目标，
没有 current-deployment v3 文件，也没有进行中的部署标记；远端 main 此前已超过
旧目标。这是后续生产发布前需要另行审查的历史兼容事项，本轮不修改这些状态文件。

