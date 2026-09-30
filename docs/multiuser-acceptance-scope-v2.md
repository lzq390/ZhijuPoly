# 多用户验收范围 v2：保守故障合同与 OpenMM

用户已选择修正验收合同，并按现有 OpenMM 动力学修正规范。本版本不增加证明失败后的自动恢复能力，也不建设 GROMACS 动力学。规范、纯文件裁定器及其 CPU 测试可以实施；科学执行须另有具名计划、预算和窗口。本工具不会提交科学任务、连接数据库或操作服务。

## 范围与历史

| 范围 | fixed128-v1 历史 | acceptance-scope-v2 |
|---|---|---|
| MD Worker | 15 项 | 14 项：移除指定 GROMACS 要求，已有 `WF-MD-formal_openmm_child.C` 只保留一次 |
| DFT Worker | 14 项 | 15 项：旧 active_child 要求由两个不同的新合同实例接替 |
| GPU 页面认证 | 99 项 | 同一 99 项定义 |
| 业务分母 | 128 | **128，必须标明是 v2** |
| 受管恢复 | 各运行的清理/恢复条件 | 每个真实 run 必需的独立环境门禁，不计业务分母 |

旧固定128的已报告结论仍是 **126 PASS、1 FAIL、1 BLOCKED**。原 `WF-DFT-active_child.A` FAIL 和 `WF-MD-formal_gromacs_child.C` BLOCKED 保留在历史摘要。其原件已由用户删除；现有交接摘要及并入回执可以证明历史曾报告什么，不能重新生成原始观察或冒充原 `unified128.json`。预期原报告 SHA 仍登记为 `15c0170336ad19d80959025ad49b0cba6fea3e2389fcbba8105ceea82a42f196`，`replayed=false`。

`contracts/multiuser_scope_v2.json` 保存全部版本化 ID、原范围摘要、新清单、范围关系及来源文件 SHA。清单定义从幸存工具的 AST 常量提取并核对：共同13个 Worker 场景、MD 的 GROMACS/OpenMM 两项、DFT queued-restart 项，以及11种页面场景×ABC×before/headers/body。未导入旧执行器，没有恢复整套验收工具。定义的重建不代表执行证据的重建。`md_engine_policy` 固定动力学为 ByteFF2/OpenMM，GROMACS仅为成盒工具；正式Density保持150万步和2fs，不能把成盒工具改称mdrun能力。

干净checkout的CPU测试只读取受版本管理的 `scripts/tests/fixtures/multiuser_scope_v2_definitions.json`。此小型 `definition_only_fixture` 保存上述字面量摘录、原路径/行号及本地已核对的完整原文件SHA；摘录内容有另一个独立SHA。完整文件摘要不冒充摘录摘要，摘录也不冒充执行原件。原 `.runtime` 文件的完整SHA核验在本地摘录时完成；CI仅核对冻结定义和摘录完整性，不读取 `.runtime`、不skip且不恢复旧运行器。

两个新 AC 业务实例为：

| ID | 前提及通过要求 |
|---|---|
| `AC-v2.proven-recovery.A` | CCO single_point 有限结果产生后、IPC 交付前，保持精确执行器身份并等待原生600秒 timeout。由 DFT residency 的终止权限依次完成身份复核、MPS 终止、冻结前查询、冻结、再次查询、scope kill、scope归零。每次清理使用独立新鲜证明，证明完成后才释放容量。故障任务 failed；同一 Broker/MPS 控制平面中 ABC 新任务真实完成。 |
| `AC-v2.unproven-containment.A` | 在相同结果后/IPC前阶段，对精确执行器实施 pidfd SIGKILL，身份复核出现 `workload_identity_mismatch` 后不再继续终止链。失败断面可确认 residency suspect、4096 MiB 保留和GPU1隔离；相关新驻留拒绝、外部身份未受影响。允许后续按既有规则清理 lease，但不由晚期 lease=0推断解除隔离或全程占额时间线。 |

二者都要求当前运行 ABC 基线、六向跨用户拒绝、本人 owner/已提交产物不变、无重复执行或发布；并共同要求 `terminal_status=failed`、`uncommitted_result_published=false`、整数 `fault_attempt_publication_count=0`。仅“无重复发布”不能排除第一次错误发布。不把结果后 IPC 故障表述为 CUDA kernel 被中断。正常取消可能是协作取消，不自动等同于上述原生 timeout 实例。

`AC-v2.managed-environment-recovery` 是每个真实 GPU run 的强制门禁：请求、任务/attempt、进程、租约/waiter、scope、scratch和所有数据库消费者退出后清理夹具；宿主恢复入口位于协调器 scope 外，恢复原生会话、资源限制、GPU-only及实际服务健康。证明失败分支保留旧 quarantine 记录，`fault_failure_closed=false`。新环境恢复不能关闭原自动恢复 FAIL。只使用既有科学产物的浏览器轮适用独立 `UI-v2.fixture-environment-preserved` 门禁：确认本轮CPU夹具收尾和开发环境保持，不要求伪造本轮GPU租约/MPS恢复。

## 裁定字段与关系

工具将以下事实分开输出：

- `reported_historical_status`：旧摘要中的结论，承接126项为PASS；两个新实例为null。
- `execution_status`：本版本有没有提供已验证的新真实执行。历史继承或无新执行时仍为NOT_RUN。
- `evidence_status`：`SUMMARY_ONLY_RAW_DELETED`、`NOT_PRODUCED`、`UNAVAILABLE`、`INVALID_PACKAGE`、`VERIFIED_ORIGINAL`。
- `current_status`：新scope目前能否通过；缺原件为BLOCKED，新实例未执行为NOT_RUN，输入证据矛盾为FAIL。

因此不提供新的真实证据时，v2为 **0 PASS、0 FAIL、126 BLOCKED、2 NOT_RUN**，历史仍是126P/1F/1B。证据校验FAIL属于包/断言未通过，不自动认定产品新缺陷。状态为PASS只表示提交的文件包满足本工具的合同校验；工具不能认证采集者、从布尔字段独立观测硬件，或恢复已删除文件。

关系键是 `(scope_version, case_id)`；范围节点名为 `$scope`。`continues_requirement` 只承接要求；`contract_supersedes` 只承接新版AC合同；`covered_by_existing_requirement` 指向已有OpenMM要求，不表示OpenMM与GROMACS科学等价。旧要求通过 `scope_removes_requirement` 指向新范围节点；新范围通过 `requires_environment_gate` 指向按执行层适用的两个环境门禁。关系必须无环，全部 `closes_failure=false`。`removed_requirements` 只作用于v2。只有完整文件、参数、执行层、实现和恢复链校验通过，才输出 `verified_evidence_inheritance`，不会根据摘要建立该关系。

`contract_valid=true / contract_status=VALID` 表示规范结构、范围与关系校验成功，与真实验收BLOCKED可以同时成立。本工具的顶层 `status` 仅裁定此128项业务范围；`overall_multiuser_status=NOT_ASSESSED`。service最小权限和SCI-MIX独立保留，不能因128项通过而宣布整个多用户验收完成。

## 纯文件接口

默认仅检查规范并输出当前证据不足的报告：

```sh
python3 scripts/multiuser_scope_v2.py --output /absolute/new-scope-v2-report.json
```

输出路径必须是新文件，禁止覆盖既有文件。退出码：0表示本scope128项全部具备通过证据；1表示报告FAIL/BLOCKED；2表示输入或输出无效。不指定输出时写标准输出。缺原件不会触发网络查找、服务启动或科学重测。

显式文件包入口：

```sh
python3 scripts/multiuser_scope_v2.py --evidence /absolute/evidence-manifest.json --output /absolute/new-report.json
```

manifest结构如下；其中路径相对于manifest目录，SHA必须对应文件原字节：

```json
{
  "schema_version": 1,
  "scope_version": "acceptance-scope-v2",
  "entries": [{
    "scope_version": "acceptance-scope-v2",
    "case_id": "AC-v2.proven-recovery.A",
    "evidence_kind": "original_package",
    "qualification": {"path": "qualification.json", "sha256": "<64 lowercase hex>"},
    "raw": {"path": "raw.json", "sha256": "<64 lowercase hex>"},
    "cleanup": {"path": "cleanup.json", "sha256": "<64 lowercase hex>"},
    "restore": {"path": "restore.json", "sha256": "<64 lowercase hex>"}
  }]
}
```

未来采集器必须在执行前锁定并独立评审 qualification，含 `review_status=APPROVED`、精确scope/case、完整科学与场景 `parameters`、`engine`、`execution_layer`、`fault_point`、source/model/image三个SHA。每项的fault_point及最小参数由合同固定；raw和qualification即使同时自选其他故障点或缩短正式MD步数也会拒绝。它是评审输入，不能在看见结果后从raw自动反推来通过校验。无评审qualification则BLOCKED。

正式MD故障和完整Density来源的参数保留原工具请求结构：顶层 `protocol=Density`、`run_mode=formal`；`config_json` 原样包含 `protocol=Density`、`temperature=298`、`natoms=2000`、`components={"CCO":1}`、`smiles={"CCO":"CCO"}` 及 `managed_params/managed_output/managed_working` 三个目录占位值，另记录实际协议 `steps=1500000`、`timestep_fs=2`。正式MD的smiles是原始config_json中的字典，不能替换成DFT输入的字符串 `CCO`。两份资格/原始结果同时改温度、目标原子数、组分或该smiles字典也会拒绝；测试与从幸存原 `body(formal=True)` 提取并冻结的AST字面量比对，未自造科学输入映射。

raw采用 `document_kind=original_case_result`，携带真实run/case/scope、参数原值及其规范JSON SHA、同一实现/模型/镜像SHA、真实执行层、`status=PASS`和 `simulated=false`。参数SHA算法是 UTF-8 JSON、键排序、无空格分隔、保留非ASCII字符。GPU运行层为 `real_gpu_worker_fault` 或 `native_ui_real_gpu`，必须绑定GPU1 UUID；既有产物页面层为 `native_ui_owned_gpu_artifact`，GPU来源绑定在独立科学source中。普通CPU合同或模拟浏览器不能充当任一真实执行层。

AC raw还必须记录上述boundary、owner A、精确 PID/start_ticks/boot/cgroup/GPU1身份、residency lease/fencing/Broker实例及target identity。正常分支每次 `termination_attempts` 含唯一proof_id、同一权限和身份、完整 `stages`、单调 `stage_times`及 `CUDA_SUCCESS`；时间介于fault和容量release之间；Broker和MPS实例不得更换。隔离分支记录受控failure stage/code、其后空操作列表及带lease/fencing/GPU身份的失败时占额快照。具体受控字段和顺序集中定义在 `validate_ac`，测试中的包是合成数据，仅用于验证拒绝逻辑。

cleanup/restore须是同run的独立、逐项证明的原始回执，`document_kind`分别为 `cleanup_receipt`/`managed_recovery_receipt`，`simulated=false`、`status=PASS`。必需检查集中列在 `CLEANUP_CHECKS`/`RESTORE_CHECKS`；restore还绑定环境gate、GPU1和 `fault_failure_closed=false`。缺任一回执为BLOCKED；回执中失败、不完整或run不符为FAIL。

上段适用于实际GPU运行。formal-md/dft的结果、轨迹、bundle页面采用已拥有科学产物层：manifest还必须提供 `scientific_source`、`scientific_source_cleanup`、`artifact` 三个文件及SHA；source原件为 `original_scientific_source`，其独立原科学run/清理、GPU1、引擎和产物SHA必须可核验，产物字节SHA还必须等于本轮UI观察SHA。本轮raw明确 `gpu_services_started=false`、`scientific_submissions=0`，不填本轮GPU UUID。qualification另锁 `scientific_source_parameters`、`scientific_source_sha256`、`scientific_model_sha256`、`scientific_image_sha256`和 `artifact_sha256`。MD轨迹source强制正式Density150万步/2fs；MD普通结果可明确锁定DensityDemo300步或完整Density；DFT轨迹source须为optimization，其他DFT产物须锁定实际计算类型。

这类浏览器轮的本轮cleanup为 `cpu_fixture_cleanup_receipt`（请求、CPU进程、数据库消费者、夹具、账号会话退出，未启动GPU资源），restore字段提供 `environment_preservation_receipt`（无服务修改、GPU-only保持、开发健康未变），gate ID为 `UI-v2.fixture-environment-preserved`。既有科学source缺失仍然BLOCKED，不能只凭最新浏览器PASS或原摘要补出source。

当前没有旧原始报告格式的自动转换器。不能把遗留摘要包装成上述raw格式；如后来发现幸存旧证据，要先单独评审其原件、格式及适用性，再增加最小只读适配器并保留原字节及全部SHA。不得为使报告PASS编造original字段。

## 本阶段验证

运行以下纯文件测试，不需要第三方依赖：

```sh
python3 -m unittest discover -s scripts/tests -p test_multiuser_scope_v2.py -v
```

覆盖准确分母/版本、历史不可覆盖、关系无环、摘要和缺原件阻断、SHA及重复JSON键、CPU/错引擎/参数/来源/版本拒绝、恢复链、原生600秒生命周期、终止身份和权限、证明新鲜性、容量释放顺序、隔离失败断面及不继续终止操作、原完整MD请求参数锁定、两分支失败终态与零错误发布。正向fixture也只存在测试临时目录，不计真实实例PASS；测试不会生成真实验收报告或科学产物。
