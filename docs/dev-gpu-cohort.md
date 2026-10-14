Development GPU1 cohorts
=======================

This is an opt-in native lifecycle for one private Backend, MD Worker and DFT
Worker. Existing launches without a cohort retain their original project,
runtime paths, strict v1 records and scope command. A cohort uses full host
auditing; direct/trusted startup is rejected. An existing unrelated GPU1 claim
still blocks admission. This interface does not grant authority to stop it.

Configuration
-------------

Create `REPO/.runtime/cohorts/COHORT_ID/cohort.json` under owner-private 0700
directories. The file must be an owner-private 0600 regular file with one link.
Its exact SHA256 and device/inode identity are frozen through controller serve,
status, activation, worker startup, MPS control and recovery. Replacing the file,
even with identical content, invalidates the active binding.

The v1 configuration has exactly these fields:

* `schema_version`: integer 1.
* `cohort_id`: lowercase letters/digits/hyphens, matching its directory name.
* `repo_root`: the absolute development checkout path.
* `source_sha`, `source_tree`: full lowercase Git identities for startup.
* `compose_project`: a private `nexpoly_dev_cohort_...` or `nexpoly_accept_...`
  project; the default development and production projects are rejected.
* `frontend_port`, `backend_port`: distinct unprivileged loopback ports.
* `auth_env_file`: null, the existing `REPO/.env.dev.auth`, or an owner-private
  `backend.auth.env` within this cohort. Configuration validation reads only its
  metadata. The existing native launcher consumes the file internally.
* `md_resources`, `dft_resources`, `backend_resources`: complete numeric profiles
  with `cpu_quota_percent`, `memory_max_bytes`, `memory_swap_max_bytes` and
  `tasks_max`. Boolean, partial, nonnumeric and out-of-range profiles are rejected.
  Worker scope properties precede the existing CUDA registration gate. Backend
  limits use the same memory cap plus swap allowance for Docker memswap_limit.
* `prior_backend`: null for an empty project, or the exact `container_id` and
  `image_id` of an explicitly owned private CPU baseline. It cannot authorize a
  GPU container or another project/name.

Run `scripts/dev_gpu_cohort.py prepare --config ABSOLUTE_PATH --sha256 DIGEST`
using the fixed system Python. It prepares only the deterministic private
directories and secret-free Compose selection. Pass
`NEXPOLY_DEV_GPU_COHORT_CONFIG` and `NEXPOLY_DEV_GPU_COHORT_SHA256` to the ordinary
`scripts/dev_server_gpu.sh` lifecycle. Derived path/mode values are selected from
the validated configuration after dotenv import; unconfigured launches ignore
cohort-derived environment variables.

Ownership and recovery
----------------------

Each real controller session has its own immutable early Backend candidate
record. Backend create, image/config/session/operation binding and exact-ID start
precede health and activation. Status, controller inventory and Broker admission
retain that exact candidate identity, so a same-label replacement is rejected.

All cohort CPU/GPU recreation uses one private native lifecycle lock. It verifies
the current owned container and removes only that exact 64-character ID. Compose
uses `create --no-recreate --pull never`; it cannot stop a replacement by service
selection. A fresh operation label and pending native intent support safe failed
startup collection. The mutable service pointer is updated only after exact
candidate binding, under the same lock.

The first CPU baseline seals the native effective CPU configuration, exact image
ID, Backend build identities, both Worker Git lock hashes and a fixed allowlist
of public runtime paths. This owner-private record contains an opaque effective
environment and must never be exported or printed. CPU recovery consumes only
the frozen configuration, uses the pinned image ID and injects a fresh operation
nonce/config hash. Literal dollar signs are escaped for Compose. Private cohorts
run the image's Backend code without a development source mount or hot reload.
All private runtime binds keep `create_host_path: false` and their directories
must remain owned, mode 0700 and free of symlinks.

Cleanup/status for a configured cohort use the frozen source and Worker identities
and do not source the checkout's new dotenv files. Startup/restart continue to
verify the current checkout and dependencies. If the frozen cohort configuration
changes, admission stays drained and the controller remains isolation-waiting.
Maintenance retirement requires the exact Backend to be stopped and every owned
controller, Worker, Broker and MPS identity to have exited. A stopped maintenance
retry finishes a previously interrupted retirement under the same lifecycle lock.

MD keeps the existing full process record. DFT adds an early binding of PID/start,
full supervisor argv, Python, UDS, source/config/session and configuration file
identity before its health wait. Native drain, instance checks, PID fences,
protected MPS descriptors, immutable lease scope names/slice, GPU1 policy and GPU3
reservation remain in force. Recovery does not recreate old descriptor or lease
records and does not select the main project for a private cohort.
