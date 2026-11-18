# PolyTAO MCP：19111 独立能力网关接入

本次将 PolyTAO 聚合物生成和参数板流程接入 `http://127.0.0.1:19111/` 的独立能力网关。用户继续使用默认 Research 对话，以自然语言提出生成需求，无需选择专用 Agent、记忆工具名或编写 JSON。

**2026-11-16 已部署并完成真实验收。** 默认 Research 对话成功触发参数板，用户确认后仅提交一个生成任务，返回 **3/3 个候选**；保存、刷新、继续查询与重复确认去重均通过。真实会话为 [19111 聚合物生成验收](http://127.0.0.1:19111/chat/ses_e1c9e0ddbffeOY2VtreK5oh4b4)。

`19112` 保持原来的 PolyTAO 隔离验证入口，其会话、文件、配置和容器不随本次接入迁移。`19111` 是本次用户要求接入的现有独立入口，保留知识库、原有能力和历史对话。共享 `9011`、原共享能力网关及生产入口不在本次变更范围内。

## 调用链与工具范围

```text
浏览器 19111
  ├─ 自然语言 → 独立 OpenScience 后端 14096
  │               → capability_gateway（模型凭据）
  └─ 参数板操作 → 同源 /openscience-api/nexpoly/polytao/draft
                  → 独立 OpenScience 后端的参数板桥接（专用 UI 凭据）
                          ↓
             capability-gateway-dev:3100/mcp
                          ↓
        nexpoly_polytao_mcp_dev-polytao-mcp-1:18111/mcp
                          ↓
                 NexPoly 开发后端与现有 GPU 调度
```

`3100` 仅为独立 Gateway 的容器内端口，不发布宿主端口。Gateway 加入 `nexpoly_polytao_mcp_clients_dev` 访问上游 PolyTAO；OpenScience 后端不加入该网络，也不直接连接 PolyTAO。后端仍通过 `ks-agent` 访问既有知识库，UI 保持既有知识库页面连接。

模型与参数板使用不同的 Gateway 入站凭据，并分别转发到上游限定工具的模型客户端和 UI 客户端。工具名称及返回的结构化结果保持上游合同；OpenScience 模型看到的完整工具名前缀为 `capability_gateway_`。

| 模型端新增工具 | 用途 |
|---|---|
| `get_polytao_capabilities` | 查询服务状态、15 项必需描述符与采样参数约束。 |
| `calculate_polytao_descriptors` | 从参考 SMILES 计算完整描述符，供参数板预填。 |
| `prepare_polytao_generation` | 保存已知条件、创建或更新生成草稿，返回参数板；不启动生成。 |
| `get_polytao_draft` | 查询草稿状态、参数、缺失项和提交回执。 |
| `get_polytao_job` | 查询已提交任务的进度、候选 SMILES、SA 分数和分页结果。 |

| 参数板专用工具 | 用途 |
|---|---|
| `get_polytao_draft` | 读取当前对话关联的草稿，恢复参数板。 |
| `update_polytao_draft` | 校验并保存参数编辑、参考结构计算结果和草稿修订版本。 |
| `confirm_polytao_generation` | 用户确认后按已保存版本提交一次任务，返回任务回执。 |
| `cancel_polytao_draft` | 取消尚未提交的草稿；不代表取消运行中的 GPU 任务。 |

参数板的四个工具不会作为模型可调用的确认权限暴露。参数板桥接仅允许这四个精确工具名，校验会话、草稿及修订版本；浏览器和模型都收不到认证令牌。

原 Gateway 提供 9 个工具：6 项既有能力加 3 项数据库筛选。加入模型端 PolyTAO 5 工具后，**模型凭据已发现 14 个 Gateway 工具，UI 凭据仅发现上述 4 个参数板工具**。知识库保持另一个 MCP 连接，不计入这 14 个工具。

| 保留的能力组 | 工具 |
|---|---|
| 既有 6 工具 | `predict_polymer_properties`、`dft_job_submit`、`dft_job_status`、`dft_job_list`、`dft_job_result`、`polymerize_monomers` |
| 数据库筛选 3 工具 | `list_filter_properties`、`search_polymers`、`get_property_histogram` |

默认 Agent、模型列表、Research 的既有配置和 DFT 作业审批策略保持原样。Research 增加 PolyTAO 的工具权限及使用说明；原有专用 `property-filter` Agent 仍只允许三个只读筛选工具。不会新增 `Polytao-Generation` 模式。

`polymerize_monomers` 是单体正向聚合；PolyTAO 是按描述符生成聚合物候选。两者不能互相替代。

## 自然语言、参数板与继续执行

在默认 Research 对话中输入，例如：

> 帮我生成 3 个聚合物候选，先让我填写参数。

模型查询能力并调用 `prepare_polytao_generation`，页面显示中文参数板。用户已给出的数值可预填；未知描述符保持缺失并说明含义，不能用零或推测值补齐。也可提供参考 SMILES 计算描述符，或直接填写完整的 15 项数值。采样参数有明确默认值和边界。

参数板支持保存与刷新恢复。参数完整、通过校验后，用户点击“确认并生成”，桥接才调用 `confirm_polytao_generation`；编辑草稿、打开参数板或模型文字声称确认都不会启动任务。重复确认沿用原任务回执，不重复创建 GPU 任务。

确认回执包含原草稿关联的 `job_id`。页面将回执交回当前对话，模型使用 `get_polytao_job` 继续查询同一任务并展示候选结果；不能重新创建草稿或提交，也不能用 DFT 或单体聚合工具续查 PolyTAO。参数板和结果卡片读取保存的原始工具结果，刷新后继续恢复。

直接输入定性要求、部分描述符或区间并不意味着上游支持这些条件。当前生成仍要求完整的数值描述符向量；生成结果也不保证精确满足全部目标。SA 分数是合成可及性指标，不是成功概率。

## 私有配置与上游来源

运行目录为 `/data/lzq/gith/nexpoly-dev/.runtime/openscience-mcp-dev/`，目录权限 `0700`，配置与认证文件权限 `0600`，不提交到 Git。

首次接入通过 `setup --polytao-runtime` 显式指定现有上游的私有运行目录。目前已授权的来源是：

```text
/home/devuser/.codex-desktop-lzq/worktrees/bce3/nexpoly-dev/.runtime/polytao-mcp
```

启动器只读核验该来源的 `server/identity.json`、`server/clients.json`、`client.env` 与 `ui-client.env`。核验内容包括专用开发账号 `zhiju002` 对应的固定账号身份、固定 NexPoly 开发后端、`127.0.0.1:18111/mcp`、客户端 ID、UI 客户端所属模型客户端、工具范围、凭据哈希及私有权限。不会读取或复制原生后端的会话 Cookie、CSRF Token 或密码。

私有 `polytao-source.json` 记录经核验的来源路径和身份，后续 `setup`、`up`、`smoke` 据此重新验证。两入口复用既有上游 `openscience-polytao-dev` 与 `openscience-polytao-dev-ui` 客户端；同一上游所有者的去重规则继续生效，参数板按各自对话的会话 ID 关联。

| 私有文件 | 挂载范围 |
|---|---|
| `gateway-token` | OpenScience 的模型 MCP 配置使用；保留现有值，不因接入轮换。 |
| `gateway-ui-token` | Gateway `/run/capability-gateway/gateway-ui-token` 与 OpenScience `/run/openscience/gateway-ui-token`，仅供参数板桥接。 |
| `polytao-token` | 仅 Gateway `/run/capability-gateway/polytao-token`，上游模型 5 工具。 |
| `polytao-ui-token` | 仅 Gateway `/run/capability-gateway/polytao-ui-token`，上游参数板 4 工具。 |
| `mcp-token` | 保留原 Gateway 的只读数据库筛选上游凭据。 |

模型、UI 和两组 PolyTAO 上游凭据必须彼此独立。上游令牌和原生账号认证不能放入模型提示、工具结果、页面响应或仓库。模型只获得正常的工具定义及业务结果。

## 部署与只读检查

所有命令在 `/data/lzq/gith/nexpoly-dev` 执行。先保留现有独立项目的代码、运行目录和镜像备份；原会话与文件位于该独立项目自己的持久目录，`down` 不删除这些数据。

```bash
python3 scripts/openscience_mcp_dev.py down
python3 scripts/openscience_mcp_dev.py setup \
  --polytao-runtime /home/devuser/.codex-desktop-lzq/worktrees/bce3/nexpoly-dev/.runtime/polytao-mcp
python3 scripts/openscience_mcp_dev.py up
python3 scripts/openscience_mcp_dev.py smoke
python3 scripts/openscience_mcp_dev.py verify-protected
```

`down` 仅允许精确匹配已有旧挂载和网络布局的本项目容器，供此次迁移停止使用。`up` 和 `smoke` 严格要求新的凭据挂载、PolyTAO 客户端网络及镜像身份；不接受缺少 UI 凭据或混合旧新网络的容器。更换来源、挂载配置或令牌必须先停止独立项目。

镜像构建将新增 OpenScience HTTP 边界与参数板桥接模块通过 `COPY --chmod=644` 写入，避免本机私有权限随构建上下文进入镜像后，非 root 运行用户无法读取。构建随后切回运行用户检查两个模块可导入；实际部署继续使用启动器指定的本机 UID/GID，而不是以 root 启动应用。权限与导入检查失败会阻止构建。

后续重复 `setup` 可省略 `--polytao-runtime`，复用保存的来源。若账号会话或上游客户端令牌过期，应在上游原有启动器续期，再执行 `down → setup → up → smoke`，而不是在本脚本或聊天中填写密码。详细上游运维方式见原 PolyTAO 开发文档。

`smoke` 检查 UI 与 API 健康、知识库和 Gateway 两个 MCP 的连接，并在 Gateway 内使用实际模型/UI 凭据只读检查 14/4 工具清单，通过 Gateway 调用 `get_polytao_capabilities`。它不会发模型提示、确认草稿或创建生成任务，不能替代真实浏览器验收。

`verify-protected` 校验共享 OpenScience 与原共享 Gateway 的配置、部署文件及容器身份。不要通过刷新共享保护基线掩盖未授权变化。本次仅修改 `19111` 的独立项目，不重启 `19112`、共享 `9011`、知识库或原生 NexPoly 服务。

## 测试与真实验收

实现阶段已通过：

- 60 项启动器测试，覆盖来源身份、限定工具、凭据隔离、旧布局迁移、新布局验证和 14/4 清单检查。
- 66 项 OpenScience 补丁、HTTP 边界、参数板桥接及结果展示测试。
- 28 项独立 Gateway 代理测试，包含既有筛选工具兼容、模型/UI 认证与工具范围、上游错误及转发边界。
- 6 项真实浏览器验收脚本测试，覆盖固定 19111 入口、只读续查、验收消息与工具结果归属、终态及确认回执检查。

以上共 **160 项测试通过**。

另已通过实际 Docker Compose 配置解析、现有上游私有身份与客户端核验、隔离镜像构建检查；这些检查没有提交真实生成任务。

测试可按以下命令复现。Node 命令需在 Node 22 环境执行；本机已验证 `/home/devuser/.nvm/versions/node/v22.23.2/bin/node`，默认 Node 18 不支持完整套件。

```bash
python3 -m unittest scripts.tests.test_openscience_mcp_dev
node --test ops/openscience-mcp-dev/*.test.mjs
node --test scripts/tests/verify_openscience_polytao_gateway_browser.test.mjs
```

Gateway 测试使用其 Dockerfile 中锁定的 Bun 与 MCP SDK 环境，运行 `property-filter.test.ts` 和 `polytao.test.ts`；不直接以宿主 Node 执行 TypeScript 测试，也不需要网络或真实账号。

实际部署的 5 个服务均健康。`smoke` 已确认 Gateway 模型端 14 工具、UI 端 4 工具，知识库与 Gateway 两个 MCP 均已连接，PolyTAO 能力查询成功；最终 `verify-protected` 再次通过。原有 9 工具的完整 Schema 与接入前逐项深度比对一致，既有审批配置保留，清单证据为 `.runtime/polytao-gateway-integration/20261116T074924Z/gateway-tools-after.json`。本次没有新提交 DFT 或单体正向聚合任务，不能将 Schema 比对当作这些计算功能的真实执行验收。

真实浏览器验收在默认 Research 中完成，报告模式为 `real_ui`：

1. 自然语言触发空草稿，显示 15 项缺失输入，“确认并生成”保持禁用。
2. 使用参考结构计算描述符，手动清空并重新填写字段，保存后刷新页面，参数和草稿保持一致。
3. 确认后产生一次提交回执，当前对话继续调用 `get_polytao_job`，返回 3/3 个候选。
4. 双击确认按钮只发送一次确认请求；另行重放同一确认 HTTP 请求仍返回同一任务。上游 SQLite 的 `accepted` 记录从 3 增至 4，恰好新增一个任务，没有重复生成。
5. 在 320、452 与 1440 像素宽度下，任务摘要均可读，候选结果与原始工具输出相符。

浏览器报告、截图和工具输出保存在本项目私有目录：

```text
.runtime/openscience-mcp-dev/polytao-browser-evidence/2026-11-16T08-03-27-512Z/report.json
```

生成结果另通过原生 RDKit 接口只读复核：3 个结构均可解析，各有至少两个 wildcard 连接原子，标准化后互不重复，SA 分数有限。此步骤未提交第二个生成任务，报告位于原 PolyTAO 工作树的：

```text
/home/devuser/.codex-desktop-lzq/worktrees/bce3/nexpoly-dev/.runtime/polytao-mcp/acceptance-98716e21e13e8db60e3b.json
```

原有三个数据库筛选工具也在 19111 默认对话中再次通过自然语言实测，报告为：

```text
.runtime/openscience-mcp-dev/browser-evidence/2026-11-16T08-03-27-514Z/report.json
```

跨会话边界也通过真实 HTTP 核验：从原筛选会话 `ses_e1c9e0ddcffevTEVSPBlrB0qMl` 请求本次生成草稿，被 `403 DRAFT_NOT_IN_SESSION` 拒绝。证据为 `.runtime/polytao-gateway-integration/20261116T074924Z/bridge-cross-session.json`。

`19112` 未重启，其原有 smoke 通过。由于该验证入口的保护快照包含此次授权更新的 5 个 `19111` 容器，先确认其保护的所有文件及非 `19111` 容器指纹完全相同，再仅将这 5 个容器的身份元数据更新为本次部署身份；没有修改其业务配置、会话或运行容器。更新前的保护快照已备份至：

```text
.runtime/polytao-gateway-integration/20261116T074924Z/19112-protected-baseline-before-authorized-19111-update.json
```

共享 `9011`、原共享能力网关和原生 NexPoly 服务未因本次部署重启或改动。真实会话、截图、工具输出与保护快照保持在私有运行目录，不提交完整候选和对话数据到 Git。

本次服务状态、160 项测试与各项真实验收的汇总记录为 `.runtime/polytao-gateway-integration/20261116T074924Z/acceptance-summary.json`。
