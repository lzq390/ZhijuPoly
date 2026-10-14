# Proposed GPU1 system-manager configuration provider

Status: source development and nonprivileged review only. This change does not
install a service, read a real Root EnvironmentFile, or prove GPU admission.

UID1001's current native guard fails with `gpu_claim_inventory_unavailable`
when it opens `/etc/nexpoly-tunnel-proxy.env` (Root:Root, mode 0600). This is an
OS access denial. An administrator's one-time report cannot replace the live
unit/manager/file CAS needed for each admission.

## Privilege boundary and native integration

The proposed Root service reads only system-manager configuration. It does not
read process environments, call NVIDIA or Docker, operate MPS, acquire a lease,
start a workload, change a password, or modify a configured file. UID1001 keeps
the existing cgroup membership, PID/start-time, UID, process-environment,
NVIDIA, Docker and MPS checks. Legacy/default inventories retain their behavior.

`scripts/dev_gpu_cohort.py` accepts an explicit schema 2 with the exact extra
field `"systemd_inventory_provider": "root-unix-v1"`. Schema 1 remains legacy;
unknown fields/providers and incomplete selection fail. The controller's first
and trailing audits and the descriptor-bound private Broker pass an explicit
factory from the verified cohort. Ambient environment variables cannot select
the provider. It is unavailable for non-GPU1 Broker candidates. Each query gets
its own connection and snapshot; no admission cache is introduced.

The fixed endpoint is
`/run/nexpoly-gpu-systemd-inventory/inventory.sock`. Linux `SOCK_SEQPACKET` and
`SO_PASSCRED` bind every capture and validate request to kernel-provided sender
PID/UID/GID. The provider permits only UID/GID 1001 and checks unchanged PID
start time/cgroup around capture and validate. Noncredential ancillary data,
truncation and duplicate JSON keys are rejected; received file descriptors are
closed before rejection. Request fields are exactly protocol, operation, GPU1,
nonce, and (only for validate) the connection's capture handle. No path, command,
unit selection, variable name, timeout override, or UID selector is accepted.

Responses expose every active/transitional system service's public unit identity
(Id, state/substate, InvocationID, MainPID, cgroup and User), plus boolean
`static_gpu1` and `statically_relevant`. These are distinct: a configured `none`
or other-GPU declaration can still require UID1001's live-process checks.
Mixed GPU1/other-device static declarations fail closed rather than being
projected into a weaker GPU1 claim. No raw Environment, manager variables,
EnvironmentFile paths/contents, or secret-derived hashes are returned or logged.

Root retains full unit metadata, relevant manager capture and all file
fingerprints privately. Capture is immediately rechecked; validate compares the
full original authorities after the caller's final native membership/PID CAS.
Even a non-GPU variable change with unchanged projection invalidates the proof.
Optional missing files, creation, inode/parent identity, compatibility-link
target and content changes are covered. Every validate attempt consumes the
handle; disconnection/error/timeout discards the snapshot.

The privileged reader is stricter than legacy: all directory ancestors and final
regular files must be Root-owned, non-writable by group/others, and free of
ancestor symlinks. Only a Root-owned final compatibility symlink with a secured
target chain is accepted. Files have one hard link. Noncanonical paths, FIFO,
device nodes, unsupported ownership or permissions fail closed without skipping
the unit. Open uses `O_NOFOLLOW | O_NONBLOCK`; fstat and post-read CAS reject
replacement. Transitional services with EnvironmentFiles remain blocked.

Limits: one connection at a time; 10-second capture-to-validation lifetime;
4 KiB request / 128 KiB response; 256 services / 256 files; 1 MiB per file and
16 MiB total file bytes per capture; 2 MiB per fixed systemctl output; bounded
read-only absolute `/usr/bin/systemctl` commands with `--` before unit operands.
Exceeding a bound closes admission. Root reads files only from trusted manager
metadata, never from request data.

The client checks socket ownership/mode, kernel Root peer identity, service
MainPID, InvocationID, cgroup and fixed ExecStart, caller identity, and the
Root-owned source manifest before/after each exchange. The manifest's commit
and tree must equal the verified cohort's source. Root starts with isolated
`/usr/bin/python3 -I -S -B` and imports only from the verified fixed installation;
it cannot import a UID1001 checkout or user-site. Source hashes in this public
manifest are hashes of reviewed code, never environment content.

The client independently checks the peer's actual complete `/proc/PID/cmdline`
and public fixed interpreter filesystem authority. Root separately checks its
own `/proc/self/exe` device/inode against that interpreter and returns only
public PID/start/device/inode evidence. This executable proof is Root-attested;
UID1001 is not given ptrace access to Root. Boot-time package/interpreter CAS is
retained across connections. Replacing a running service's installed package or
interpreter closes the proof until a restart; current disk source cannot relabel
already-loaded old code as a new source commit. Imports have before/after CAS.
The installed directory layout is closed: only the nine source modules and
manifest are permitted. Bytecode caches, legacy `.pyc`, native extensions, extra
modules and alternate pycache prefixes are refused before Root imports;
directory layout remains checked before/after imports and on every request.

## Separate authorization required before installation or execution

No item below is authorized by development/review approval. The next approval
must explicitly cover both installation and temporary execution, using the final
reviewed commit and its exported manifest. It does not authorize GPU science,
deployment, production/GPU2 changes, or stopping an existing service.

1. **Public source only:** create
   `/usr/local/lib/nexpoly-gpu-systemd-inventory/` with Root:Root directories
   0755 and the following Root:Root regular single-link files 0644:
   `ops/__init__.py`, `ops/gpu_broker/__init__.py`,
   `ops/gpu_broker/broker.py`, `ops/gpu_broker/server.py`,
   `ops/gpu_broker/systemd_provider.py`, `gpu_resource/__init__.py`,
   `gpu_resource/client.py`, `gpu_resource/transient_scope.py`,
   `gpu_resource/systemd_inventory.py`, and `manifest.json`.
   Refuse existing installation paths unless separately approved for replacement.
   Administrator must verify the reviewed whitelist/hashes and transfer public
   bytes as an unprivileged reader; do not execute/import a user-owned bundle,
   follow user-owned source links, or run Root Git against user repository config.
   No secret or credential file is in the bundle.
2. **One new service file:** install the reviewed template as Root:Root 0644 at
   `/etc/systemd/system/nexpoly-gpu-systemd-inventory.service`; refuse an existing
   name without replacement approval. Run only a system-manager daemon reload
   and explicit start of this new service. Do not enable it at boot. No existing
   service is stopped/restarted. Its identity is UID0/GID1001, capabilities empty,
   readonly system/home, private network/devices, AF_UNIX only, no new privileges,
   1 CPU, 256 MiB RAM, zero swap, 32 tasks and 64 file descriptors. Root identity
   can read its own 0600 configuration without DAC or chown capabilities.
3. **Temporary runtime endpoint:** systemd creates Root-owned
   `/run/nexpoly-gpu-systemd-inventory/` mode 0755; service Group=1001 makes its
   socket Root:1001 mode 0660. No shared-secret token is created. UID1001 may
   connect; parent directory remains unwritable to it. Requests are only the
   capture/validate protocol above. Root runs only the fixed readonly commands
   and internally reads manager-declared configuration.
4. **Execution scope:** permit UID1001's verified schema-2 private cohort to
   perform read-only capture/validate and native GPU1 admission diagnosis. The
   proposed service reads configuration for all active system services to avoid
   omissions; this can include services unrelated to NexPoly. Approval must
   include that internal privileged reading, despite redacted outputs. A later
   successful diagnosis is evidence only; it does not authorize scientific
   execution or deployment.
5. **Persistent changes and removal:** the only persistent additions are the
   fixed code tree and one new unit file. Existing env permissions, passwords,
   hashes, flags, policies, network/security settings and source checkouts are
   unchanged. To uninstall, stop only this new service, confirm its exact unit
   and MainPID, let systemd remove its RuntimeDirectory, then remove only the
   manifest-verified new files/directories and the new unit, and reload the
   manager. Refuse substituted or unexpected paths/files; no broad recursive
   deletion. Never stop an existing Broker, MPS, worker or Backend. Schema-2
   admission stays closed while the provider is absent; no automatic fallback.

Reviewable public bundle generation is nonprivileged:
`python3 scripts/build_systemd_inventory_bundle.py`. It exports the fixed
whitelist from one clean tracked Git commit into an unused
`.runtime/systemd-inventory-bundles/<commit>/` directory and writes source hashes.
It performs no install, privileged action, service start, or file permission
change outside this private export directory. The unit template has no boot
enablement section. A privileged installer is deliberately not invoked here.

Remaining risks/limits: this Root code is a trusted reader, so an implementation
defect could expose data or make admission unavailable. Bounded sequential
connections can be occupied by an authorized UID1001 caller for at most ten
seconds. Legitimate system-manager files under user-writable directories or
outside these bounds remain unsupported and blocked; no permission relaxation
is proposed. Root-process metadata restrictions or source/service updates close
the client proof. The real host's native admission has not been tested with this
provider; fixture passes do not establish scientific acceptance.

Rollback preserves earlier commit `633551740077287652e7935ced8e6d0bd363c9d3`
and its private build/dependency artifacts. This development commit neither
rebuilds that candidate nor changes its schema-1 config or runtime state.
