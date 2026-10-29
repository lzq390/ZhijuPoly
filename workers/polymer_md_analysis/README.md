# Private polymer MD analysis

This service reads completed-stage artifacts. It has no simulation manager,
execution admission, database credentials, job submission, or journal writer.
Run a newly pinned source release using the existing scientific HTTP interpreter:

```
python -B -m workers.polymer_md_analysis --uds /private/socket-directory/analysis.sock
```

For deployment, freeze the source and dependency identity independently of the
running simulation release:

```
python scripts/polymer_md_analysis_dev.py prepare \
  --python /private/worker-environment/bin/python \
  --release-base /private/analysis-releases \
  --dependency-lock /private/worker-http.lock \
  --dependency-lock /private/worker-analysis.lock
python scripts/polymer_md_analysis_dev.py run \
  --release /private/analysis-releases/CONTENT_SHA256 \
  --uds /private/socket-directory/analysis.sock
```

The launcher verifies the manifest fingerprint, the complete source inventory,
dependency versions and lock digests before starting. It never modifies or
reseals the execution Worker's release. Configure Backend
`POLYMER_MD_ANALYSIS_UDS` to the reader socket as seen inside its existing socket
directory mount. The jobs mount remains read-only. A readonly Backend source
mount must contain directories for any nested asset mounts before replacement.
Keep the production 90/95/100-second service/Backend/browser budgets aligned;
custom short budgets are intended for isolated service tests.

Required environment: `POLYMER_MD_ANALYSIS_JOB_ROOT` (existing read-only jobs)
and `POLYMER_MD_ANALYSIS_SPOOL_ROOT` (separate, private, owned scratch directory).
`POLYMER_MD_ANALYSIS_UDS` may replace `--uds`. Other settings:

| Environment | Default |
|---|---:|
| `POLYMER_MD_ANALYSIS_TIMEOUT_SECONDS` | 90 seconds, including queue/copy/compute |
| `POLYMER_MD_ANALYSIS_SPOOL_BUDGET_BYTES` | 48 GiB |
| `POLYMER_MD_ANALYSIS_MIN_FREE_BYTES` | 1 GiB |
| `POLYMER_MD_ANALYSIS_FREE_FRACTION` | 0.05 of filesystem capacity |
| `POLYMER_MD_ANALYSIS_PYTHON` | current interpreter |

Apply one CPU, 2 GiB memory, no GPU and control-group cleanup in the independent
service unit. The application permits one active child and one queued request.
Queued work has no temporary files. Healthy capacity is separate from execution
Worker readiness. A stopped or unavailable analysis service must not fall back
to the execution Worker's old analysis route.

## Internal API

`GET /health` returns `protocol_version: 1`, `service: "polymer-md-analysis"`,
`ready`, `active`, `queued`, `max_running_analyses: 1`,
`max_pending_analyses: 1`, and `request_timeout_seconds`.

`POST /distance` and `POST /trajectory` accept a trusted Backend context:

```json
{
  "job_id": "UUID",
  "attempt_token": "64 lowercase hex characters",
  "stage_run_id": "eq1-1",
  "stage": "eq1",
  "artifacts": ["the complete stage's existing Artifact descriptors"],
  "distance": {"atom_id_1": 320, "atom_id_2": 1253, "start_frame": 0,
               "stop_frame": 2021, "stride": 1, "use_pbc": true}
}
```

For `/trajectory`, replace `distance` with `frame_index` (default 0) and `limit`
(default 10000). Artifact descriptors use `polymer_md.contracts.Artifact` and
include `storage_ref`; strip the database-only per-artifact `attempt_token`.
There may be 1–500 descriptors, all for this same execution/stage. Internal
request bodies are bounded to 2 MiB. Frontend clients cannot choose paths or
metadata. Backend must enforce ownership, current attempt and completed stage
before calling, and recheck ownership/artifact availability before returning.

The child reads at most 16 MiB from `stages/<stage_run_id>/result.json`, matches
its stage, and resolves its two trajectory paths to exact manifest entries.
Only those two inputs are opened through directory descriptors without symlinks,
then copied once with SHA256/size checks into private scratch. Their source FDs
remain valid across unlink. No Worker lease extension is needed. The child
closes original FDs before native analysis and never reopens the original paths.

Successful payloads retain the existing measured trajectory/distance formats,
adding `job_id` and `stage_run_id`. Distance values are Å; native PBC and the
saved physical time schedule remain authoritative. Results are not published to
the simulation journal or task center as a new calculation.

Errors use `{code, message, retryable}`. Queue overflow is `analysis_busy`/429;
deadline is `analysis_timeout`/504; unavailable service/storage is 503; changed
inputs are 409; invalid data/ranges are 422. Errors do not describe the original
simulation as stopped or awaiting reconciliation. HTTP disconnect cancels this
service's child; TERM has a two-second grace before KILL. Child exit is reaped
before removing scratch or releasing capacity. No analysis cache is retained.

## Tests

The existing interpreter needs no pytest installation:

```
python -B -m unittest discover -s workers/polymer_md_analysis/tests -v
```

Tests create synthetic trajectories and subprocesses in temporary directories;
they never query real jobs, start a simulation, or alter an execution release.
