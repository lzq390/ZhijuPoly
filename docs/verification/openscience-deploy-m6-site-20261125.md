# 当前宿主的 OpenScience 双账号部署候选

`deploy-m6` 在当前宿主使用独立的网页登录入口、平台账号库、工作区注册表及两份强制配额卷。父站为 `https://app.deploy-m6.test:18860`，工作区为 `https://agent.deploy-m6.test:18861`。这两个入口与内部的 18862、18863 均只监听 IPv4 回环。浏览器验收按当前候选证书公钥及域名固定到回环，账号使用自己的登录会话、一次性票据和工作区。

## 站点绑定

运行目录为当前部署用户的 `~/.local/state/nexpoly-m6-deploy-01`。其中 `members.json` 登记两个新平台账号，`ws` 绑定各自的工作区、UID 和卷，`site` 保存该站独立的 TLS、登录凭据及 PostgreSQL 部署标识。登录和管理员凭据只在本机私有目录保存。候选站登记名册摘要及注册表部署 ID，重新启动时核对两者。账号库名为 `nexpoly_deploy_m6`，PostgreSQL 只通过该站私有 Unix socket 提供服务。候选启动前核对每个已登记 UID 在实际目标卷上的 2,000,000,000 字节强制硬限。[存储记录](openscience-deploy-m6-storage-20261125.json)

## 启动与复验

使用已配置好的源卷和独立站点。该宿主的系统 Node 为 18，候选 Vite 使用本机已安装的 Node 22.23.2。将它放到当前进程的 `PATH`，从仓库根目录以前台进程启动。正在运行的站点独占站点锁。要切换验证配置，先正常停止这一候选的 launcher，等它回收工作区并释放站点锁。

```bash
export PATH="$HOME/.nvm/versions/node/v22.23.2/bin:$PATH"
gateway_image=sha256:6b6257257ed4c31a879ad2f525c288fc423121aeef55340da0825174a2338d1f
python3 -B scripts/openscience_m2_dev.py --environment deploy-m6 \
  --m3-gateway-image "$gateway_image" --m4
```

以 `site/running.json` 为启动就绪标记。六工具验收使用同一个站点、账号库与配额卷，以本地确定性工具选择模型启动 M3 配置。模型及知识验收在启用 `--m4` 的组合配置上使用本地 Qwen2.5-7B。结果路径使用新文件。

该候选将单个后端就绪等待上限设为 120 秒，允许 NFS 配额检查与两个并发请求的串行准入完成。正式容量验收仍按目标介质和拟开放负载实测从提交到就绪的 30 秒 P95。网页登录与功能结果只覆盖双账号功能。本轮持续运行的功能包括工作区、六工具网关、本地模型和私有知识服务，PolyTAO 与逆合成两个适配器保持关闭。

```bash
site="$HOME/.local/state/nexpoly-m6-deploy-01/site"
node scripts/ci/verify_openscience_m3.mjs --state "$site" --output /tmp/deploy-m6-six-tools.json
# 在同一候选切换到 --m4 并就绪后
node scripts/ci/verify_openscience_m4.mjs --state "$site" --output /tmp/deploy-m6-model-knowledge.json
```

## 两个登录的实测结果

A、B 在各自的浏览器会话中创建了同名项目和同名 notebook，文件读回摘要分别匹配本人内容。两边各创建一个项目会话，工作区只列出本人的会话。另一个同名文本文件由 A、B 各自修改及删除，操作彼此独立。[双账号网页和模型回执](openscience-deploy-m6-model-knowledge-20261125.json)

两名用户经原生对话执行六项工具，DFT 列表、状态、结果和下载均已双向核对。本轮的确定性工具选择模型与隔离的计算夹具不会访问生产作业队列。[六工具回执](openscience-deploy-m6-six-tools-20261125.json)

在组合站点，两人分别上传同名私有文档。网页与知识 MCP 均各命中一份本人的内容，双方检索对方标记均为零。两人的引用和下载匹配各自上传的原文。A 通过 Chat Completions，B 通过 Responses 发起 Qwen 对话，计入每人各两条实际模型请求（包括辅助请求）。A 共计 10,171 输入、24 输出 token，B 共计 10,189 输入、24 输出 token。四条请求的登录、工作区代次和 token 数均与模型工作进程保存的回执及 PostgreSQL 账本逐条一致，其他账号的模型和知识增量均为零。[逐项核账](openscience-deploy-m6-model-knowledge-20261125.json)

网页验收完成后先停止新候选。两户工作区从源盘分户备份，在本机的另一块物理盘上恢复并校验整树。随后重新从原登记源卷和原独立 PostgreSQL 卷启动同一站点，开启新的 Chromium，A、B 重新登录。两份同名项目的会话标识、各自 notebook 内容、原始模型答复和各自知识源文档与停站前的摘要逐项相符。知识库和账本沿用候选数据库，本次归档范围是两个工作区。最终保留回环组合站，正常运行期间未启用回执故障注入。[分户恢复](openscience-deploy-m6-web-checkpoint-20261125.json) · [新登录读回](openscience-deploy-m6-web-readback-20261125.json)
