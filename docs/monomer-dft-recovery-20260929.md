# Production DFT recovery, 2026-09-29

Port 9000 DFT was restored at approximately 16:58 CST on 2026-09-29.

## Cause

At 21:47:53 CST on 2026-09-24, systemd-oomd killed the production DFT
service. Its Unix socket remained on disk. The runner rejected any existing
socket before reaching its existing `remove_verified_stale_socket` function,
so every fresh systemd start failed with status 2. The recorded restart count
reached 68,822 before recovery.

## Production recovery and temporary mitigation

The service was stopped before recovery. The old socket was verified to be an
owned Unix socket with no kernel registration and a refused connection.

An incident helper now runs as an additional `ExecStartPre` through:

`/home/devuser/.config/systemd/user/nexpoly-monomer-dft-worker.service.d/50-stale-socket-recovery.conf`

The original four pre-start checks and the existing production runner remain
in place. The helper uses a fixed production path, verifies the canonical
parent directory's owner and mode 0700, binds deletion to its directory file
descriptor, rejects any socket still registered in `/proc/net/unix`, requires
ECONNREFUSED, and rechecks the socket and parent identities before unlinking.
Active sockets, symlinks, foreign sockets, non-sockets, and uncertain states
are refused. This mitigation also runs on future automatic service starts.

The helper, original unit snapshot, incident log, API responses, smoke-test
request and results, and artifact verification are stored under:

`/data/lzq/gith/nexpoly-runtime/manual-operations/dft-recovery-20260929T085615Z/`

The production checkout remains clean at
`db0fd67320bc43f2ed0e8ed9c294bbab92831670`. Production application images,
runtime manifests, GPU placement, memory policy, and other services were not
changed by this operation. This recovery addresses restart failure after
termination; it does not resolve the host memory pressure that triggered
systemd-oomd.

## Source correction

`workers/monomer_dft_worker/run_host_worker.sh` no longer rejects a socket
before the supervisor loop. Its first iteration now reaches the existing
verified stale-socket cleanup, which preserves the listening-socket, symlink,
file-type, and inode checks.

Six new regression cases directly invoke the runner, matching systemd's
entry point: recovery from a stale socket; preservation of a listening
socket, regular and dangling symlinks, a regular file, and a directory.
Supervisor tests passed 10/10; existing runner/controller tests passed 23
with 9 deselected. The production mitigation passed 7 additional tests,
including preservation of a bound socket that is not yet listening.

## Source review follow-up, 2026-09-30

The review found that a live Unix socket can return `ECONNREFUSED` after
`bind()` and before `listen()`. Connection refusal alone must not authorize
deletion. The runner now also requires the pathname to be absent from the
kernel's `/proc/net/unix` registry. A registered socket, unreadable registry,
or malformed registry is preserved and startup is refused. Existing symlink,
file-type, connection, and inode checks remain in place.

Three regressions cover a still-open bound socket before listen and registry
read/parse failures. The bound-socket regression failed against the previous
runner; after the correction, all 13 supervisor cases passed in 11.84 seconds.
`bash -n` and `git diff --check` also passed. This is separate from the
September 29 incident verification and its earlier 10/23/7 test counts above.

The new run used only fake uvicorn/nvidia processes in a networkless,
read-only-root CPU container, with private executable temporary storage and
read-only source mounts. It selected the supported standalone branch with
`MONOMER_DFT_GPU_BROKER_ENABLED=0` and
`MONOMER_DFT_STANDALONE_GPU_SMOKE=1`. No host systemd, real Worker, Broker,
GPU calculation, database, or production service was used. This does not
constitute real GPU or Broker acceptance.

The reproducible scripts and before/after logs are retained in the development
workspace under `.runtime/workspace-cleanup-20260930/dft/`, including
`dft-bound-socket-before-fix.log` and `dft-supervisor-after-fix.log`.

## Verification (2026-09-29)

- The service is active/running; MainPID 2857262 and NRestarts 68822 remained
  stable during verification.
- The 9000 status API returned `available=true`, `runtime_ready=true`, and
  `worker_status=ok`; all six models reported available.
- A real CCO single-point job submitted through port 9000 completed on GPU2:
  `a71a080d-0d4b-4229-b6cb-4b628fa296fb`.
- Energy, charges, and forces were returned for nine atoms; energy was finite
  and charges were conserved. All three output artifacts were downloaded
  through port 9000 and verified against their reported length and SHA-256.
- GPU contention remains observed under the existing `observe` policy and
  did not prevent the calculation.

## Transition to the normal release

The source correction is a development-workspace change, not a new production
release. Keep the temporary mitigation until the reviewed runner fix is ready
to deploy. The production deployment controller requires an empty
`DropInPaths`, so this incident drop-in must be removed as a coordinated part
of that release before its no-drop-in validation. Run `systemctl --user
daemon-reload` after removing only this drop-in. Do not replace the installed
unit with the repository template as an incidental cleanup.

After the fixed release is active, verify the unit's normal start path and
the public status, six model capabilities, and an actual small calculation.
Retain this operation directory as incident evidence. If the release is
cancelled while the old runner is still active, restore the saved drop-in and
reload systemd so the recovery mitigation remains effective.
