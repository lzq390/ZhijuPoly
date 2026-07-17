# Monomer MD health optimization provenance

The historical `optimize-monomer-md-health` branch is not a deployment
authority. Its eight behavior changes are carried by the governed B control
plane and Worker implementation as follows:

| Historical commit | Preserved behavior | Governed implementation and regression coverage |
| --- | --- | --- |
| `1089128` | Status and protocol requests start concurrently, share cancellation, and preserve independent outcomes. | `frontend/src/hooks/monomerMdStatusLoader.ts` and `monomerMdStatusLoader.test.ts`; unmount cancellation remains covered by `useMonomerMdSimulation.test.ts`. |
| `2f3b124` | Transport readiness is a strict, opt-in status-probe contract without leaking runtime errors. | `scripts/monomer_backend_status_probe.py`, `scripts/tests/test_monomer_backend_status_probe.py`, and the production-required Transport checks in `pull_deploy_controller.py`. |
| `91e2ef9` | ByteFF2/OpenMM/CUDA/Transport native runtime inputs are explicit and validated before work starts. | `workers/monomer_md_worker/app/config.py`, `byteff2_env.py`, `runtime_probe.py`, and their Worker test modules. |
| `4440057` | Expensive native runtime health is initialized once and served from bounded cached state. | `workers/monomer_md_worker/app/main.py`, `runtime_health.py`, `test_worker.py`, and `test_runtime_health.py`. |
| `9d2a221` | Runtime probes are bounded and child cancellation terminates the owned process group. | `runtime_probe.py`, `process_control.py`, `runner.py`, and the corresponding Worker tests; the host script remains a configuration-agnostic executable selected by the governed launcher. |
| `60ef8f8` | Frontend teardown aborts outstanding status work and prevents stale state updates. | `frontend/src/hooks/useMonomerMdSimulation.test.ts` and `monomerMdStatusLoader.test.ts`. |
| `dfa2738` | Probe and process cleanup have hard deadlines, identity checks, and escalation bounds. | `process_control.py`, `runner.py`, `runtime_health.py`, `runtime_probe.py`, and their regression tests. |
| `0143de7` | Release switching drains work, rejects ambiguous systemd/process state, prepares an immutable Worker environment, and can recover after a lost response. | The content-addressed `pull_deploy_controller.py`, `worker_slot_runtime.py`, bootstrap/takeover helpers, and `test_pull_deploy_controller.py` / `test_worker_slot_runtime.py`. |

The superseded `.github/workflows/nexpoly-deploy.yml` and its ad-hoc
`deploy_server.sh` implementation are intentionally not restored. CI only
publishes immutable artifacts; production switching is exclusively performed
by the owner-private Pull controller using the shared deployment lock.

The branch's repository-root `.env.monomer-md-worker` launcher and
`ops/systemd/nexpoly-monomer-md-worker.env.example` are also intentionally
excluded. Production configuration has one authority,
`ops/config/worker.env.example`; the control-runtime selector injects that
validated environment into the exact active A/B Worker launcher. Uvicorn
process cardinality remains controlled by that launcher contract rather than
an ad-hoc `--workers` flag.
