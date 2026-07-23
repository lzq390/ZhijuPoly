# GPU resource governance

The host GPU Broker and NVIDIA MPS units in this release are installed
capabilities only. They are **disabled by default** and release automation does
not start, enable, or configure them. In particular, this release does not
change GPU compute mode and does not start a production MPS daemon.

## Fixed accounting policy

The Broker reserves capacity atomically; it never treats instantaneous free
memory reported by `nvidia-smi` as schedulable capacity.

| Component | Reservation | Lease kind |
| --- | ---: | --- |
| Backend | 8192 MiB | process residency |
| DFT executor | 4096 MiB | residency; child execution does not double count |
| MD job | 8192 MiB | per-job execution |

Each governed RTX 4090 has a 20736 MiB schedulable ceiling. Backend + DFT + MD
therefore reserves 20480 MiB. GPU0 is absent from every policy and cannot be
selected.

The host index-to-UUID map is checked with `nvidia-smi` before the Broker opens
its socket:

- GPU1: `GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771`
- GPU2: `GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe`
- GPU3: `GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5`

Any mismatch blocks Broker startup. The immutable policy is recorded in
`ops/config/gpu-broker-policy.json` and must exactly match the compiled policy.
GPU3 is currently listed in `ops/config/gpu-external-reservations.json`; it
remains unavailable until a later host audit proves the unmanaged Docker claim
has gone and an authorized release changes that inventory. Unknown CUDA PIDs
also make a GPU unavailable.

At runtime the external reservation inventory is copied into the private state
directory as a regular, non-symlink `0600` file owned by `1001:1001`. MPS start
and Broker start independently require the exact V1 schema, governed UUIDs,
and a non-empty reason for every claim. Missing, malformed, oversized, unsafe,
or policy-foreign inventory blocks start. MPS stop deliberately does not read
the inventory, so a damaged admission file cannot prevent a safe shutdown.

Static inventory is not treated as a substitute for live host discovery.
Every admission takes an uncached initial NVIDIA-compute snapshot, queries
running Docker containers (`DeviceRequests` and `NVIDIA_VISIBLE_DEVICES`) and
active or transitioning user/system systemd service environments, then
requires unchanged Docker, systemd, MPS and trailing NVIDIA authority before
using the result. Docker, systemd, compute and MPS authority are therefore
evaluated in the same live admission CAS; none is reused from an earlier
request. A Docker or systemd query failure blocks allocation. A GPU visibility
claim with no CUDA PID still blocks the card unless its registration ID,
component, environment, Compose project/service, and exact UUID set match the
managed allowlist. This prevents an idle, unlabelled Dev Backend DeviceRequest
from being mistaken for free capacity. GPU3's current static block remains an
additional independent gate.

Systemd identities are scope-qualified (`user:<unit>` or `system:<unit>`) and
bind the complete recursive `ControlGroup`, so a user unit cannot inherit the
registration of a same-named system unit and `MainPID=0` does not hide child
processes. UID 1001 processes are checked against their live environment;
`Environment=`, stable `EnvironmentFile=`, `PassEnvironment=` manager values
and final `UnsetEnvironment=` are also interpreted. The root system manager is
the trusted host configuration boundary: an unrelated, unmarked cross-UID
service is not rejected merely because UID 1001 cannot ptrace its environment.
Independent global NVIDIA discovery still rejects the exact GPU as soon as any
such process creates a compute context. This software inventory does not claim
to replace a future root-managed `DevicePolicy=strict`/cgroup device fence.

An NVIDIA PID is never exempted merely because its process name resembles an
MPS server. The Broker queries `get_server_list` through that GPU's exact
private control pipe and binds the singleton server to the stable control PID
file, root-owned NVIDIA executables, four UID/GID identities, process start
ticks, a shared non-root cgroup, the exact GPU UUID and pipe environment, and a
second unchanged control-plane snapshot. Every reported client must name the
same server and device. Missing or conflicting evidence leaves the PID
unmanaged and blocks admission.

UID/GID `1001:1001` is one trusted runtime principal, not a security boundary
between the Broker, MPS daemon and pinned Workers. Private modes prevent access
by other identities, while same-UID code can inherently address the Broker
state and MPS paths; path/inode CAS detects accidental or persistent
replacement but cannot prove absence of a malicious same-UID ABA swap. Any
future untrusted Worker must first move to a separate identity and receive only
the minimum socket/pipe access instead of the shared writable state root.

## Lease safety

The dependency-free, newline-delimited JSON V1 protocol runs over a private
Unix socket owned by the shared `1001:1001` service identity. It supports
residency/execution acquire, activate/register, heartbeat, release, fatal GPU
quarantine, drain, resume, stable acquire request IDs, explicit waiter
cancellation, and status. It never accepts pickle or PT payloads. Pending
waiter IDs and FIFO sequence numbers are persisted with Broker state, so a
transport retry cannot silently jump the queue.

Placement is an explicit fail-closed contract. Backend and DFT residency use
`preferred`; a DFT execution lease with a residency parent is fenced to the
same card and does not reserve the 4 GiB twice. A transient DFT executor uses
`overflow`, which excludes the environment's primary card (Prod GPU3 then
GPU1; Dev GPU3 only). MD uses `any` and follows its environment policy.

Every lease records the GPU UUID, fixed memory and thread budget, exact host
owner PID/start-time/boot identity, Broker instance, monotonic fencing token,
request ID, and preferred/overflow decision. Backend residency binds to the
Backend process itself. DFT residency is owned by its CPU-only supervisor but
defers workload registration until the long-lived executor child exists; a
parented DFT execution lease is logical admission and inherits that exact
executor identity without double registration. MD and overflow DFT leases
register a dedicated per-attempt workload PID/start-time, process group, and
cgroup. Namespace PIDs are
translated through `NSpid` and accepted only for an exact live descendant that
is a `start_new_session` group leader. The host Broker then moves it into the
lease-specific cgroup-v2 subtree before the exec gate opens. State is
atomically persisted in a `0600` file inside a `0700` directory. A missed
heartbeat marks a live owner `suspect` and retains its reservation. Capacity is
reclaimed only after the exact owner is dead; a surviving unknown CUDA child
continues to quarantine the card.

Process authority is deliberately asymmetric. A DFT residency lease owns the
long-lived primary executor process, cgroup, and 4096 MiB reservation. Its
parented execution leases are zero-accounting attempt/fencing grants and never
have process-termination authority. An unparented DFT overflow or MD execution
lease owns its transient process, cgroup, reservation, and termination. A
stable queued acquire remains locally single-owned until its original Broker
request has reached an authoritative terminal response; a lost cancellation
response cannot create a second same-ID waiter.

Production requests outrank development requests while equal priorities use
FIFO order. Running work is never preempted.

Missing or unsafe per-GPU MPS control pipes make that card ineligible. Fatal,
Xid, uncorrectable ECC, or runtime-corruption reports require a live lease and
its fencing token, then persistently quarantine the entire GPU. There is no
automatic clear path. If the Broker cannot prove that an orphaned MPS client
is gone, it retains the reservation.

Cancellation, timeout, lease loss, and residual-child cleanup use NVIDIA's
safe [client early-termination
sequence](https://docs.nvidia.com/deploy/mps/latest/when-to-use-mps.html#client-early-termination)
plus cgroup-v2 containment. The Broker first revalidates the dedicated
lease-named scope while holding its allocation lock, queries the selected
card's MPS `ps` view, uses the host-namespace client PID reported there, and
issues `terminate_client <server-pid> <host-client-pid>` for every client
owned by that scope. The client remains schedulable while MPS completes this
request; freezing it first can block the control protocol. Only a
`0`/`CUDA_SUCCESS` response permits the Broker to freeze the exact scope. It
then queries MPS again while the workload is frozen, rejecting any surviving
or newly connected client, before issuing `cgroup.kill` and proving
`cgroup.procs` empty. Evidence is regenerated for every cleanup attempt and
is never reusable. Any identity, query, termination, freeze, re-query, kill,
or emptiness failure marks the lease suspect, keeps its capacity reserved,
and persistently quarantines the GPU as runtime-corrupt.

An MPS `ps` query has only two accepted idle responses: rc=0 with exactly
empty stdout for a live server with no clients, or rc=0 with the single line
`Server not found` before a server exists. A header-only response, whitespace,
extra lines, nonzero exit, or any unparsable client row fails closed.

Normal process exit is also fail-closed: release queries MPS again and refuses
to delete a process-owning execution lease while any client remains in its
registered dedicated cgroup, or while that cgroup is non-empty. Once workload
registration succeeds, scoped cgroup membership is mandatory; mutable process
ancestry or PGID can no longer substitute for it. A parented DFT execution is
different: the resident MPS client is expected to survive, so release accepts
clients belonging to any live unparented governed workload on the shared card,
including Backend and MD, but rejects an additional unmanaged client. Query
failure has the same retained lease/quarantine result, so release can never
create unaccounted capacity. Similarly, an expired lease is retained only by
its own exact MPS client, never by a peer component's client.

The Broker refuses to start without the deploy user's live systemd user
manager and exact cgroup-v2 identity. A governed Worker starts each executor
as `nexpoly-gpu-job-<complete-lease-id>.scope` below
`nexpoly-gpu-jobs.slice`; the Broker verifies the unit and scope identity and
never writes `cgroup.procs` or moves a child across cgroup ownership
boundaries. Missing user-bus, scope controls, or identity evidence fails
closed.

## Application behavior

- Backend Broker support is controlled by `GPU_BROKER_ENABLED`, which is fixed
  to `false` in the production Compose override. When enabled later, Backend
  must acquire its 8192 MiB residency lease before required model preload; an
  unavailable Broker blocks startup.
- MD Broker support is controlled by `MONOMER_MD_GPU_BROKER_ENABLED`, fixed to
  `false` in the production Worker template. When enabled, each real job stays
  submitted while it waits for an 8192 MiB execution lease. The selected host
  device is injected into the ByteFF2/OpenMM child only after admission, and
  the whole dedicated cgroup is gone before release. Once per Worker process,
  startup acquires a temporary execution lease and records an immutable
  ByteFF2/OpenMM/CUDA/Transport snapshot; hot health checks only read that
  snapshot plus live Broker/capacity state. A one-byte exec gate prevents the
  probe or job child from importing CUDA code until its workload identity is
  registered and persisted by the Broker. The configured startup deadline
  stops admission of further probe work; mandatory cgroup and lease cleanup
  is still awaited if host safety needs longer than that deadline.
- Backend and Worker independently reject formal MD configurations with more
  than 10000 atoms.

Both application images and all MPS clients use UID/GID `1001:1001`. Broker
leases drive the client environment before CUDA initialization: UUID device
selection, the card-specific pipe, the fixed pinned-device-memory limit,
active-thread percentage, and client priority. Backend uses 100% active
threads at normal priority; MD/DFT use 50% at below-normal priority. These
client limits follow NVIDIA's [MPS control interface](https://docs.nvidia.com/deploy/mps/610/appendix-tools-and-interface-reference.html);
atomic Broker reservations remain the capacity admission authority.

## Separately authorized activation

The shipped `nexpoly-gpu-mps@.service` and `nexpoly-gpu-broker.service` units
are not enabled by installation or deployment. A later production maintenance
change must perform, and verify rollback for, the ordered transition:

1. drain all CUDA clients on the selected card;
2. stop every Docker/systemd GPU visibility claimant and verify the private
   external reservation inventory;
3. set `EXCLUSIVE_PROCESS` during the maintenance window (the shipped helper
   only verifies this mode; it never changes it);
4. verify the deploy user's systemd user manager and transient
   `nexpoly-gpu-jobs.slice` scope controls;
5. start the card-specific MPS unit, then the Broker;
6. start Backend and validate required preload and its active residency lease;
7. enable MD governance, then separately enable the resident DFT executor;
8. run concurrent GPU smoke tests before reopening admission.

Backend and DFT CUDA 12.8 clients must pass a real test against
the host Driver/CUDA MPS server. Failure blocks activation; it is not a reason
to bypass the Broker or MPS.

Docker governance is opt-in and must be rendered last for Backend, using
`docker-compose.gpu-governed.yml` while retaining its one policy-primary GPU.
Broker-governed MD is deliberately host-only: its executor is launched through
an exact lease-named `systemd-run --user --scope`, then registered by PID,
start ticks, UID, process group, cgroup and systemd unit. OCI Workers cannot
securely create or control that host scope and must not bind the host user bus.
The normal MD Compose file therefore hard-codes Broker-disabled, and the old
governed MD Compose overrides are not shipped. Production remains unchanged
and Broker-disabled until the separately authorized maintenance.

The exact registered MD Docker DeviceRequest is a visibility declaration for
its CPU-only idle supervisor, not an execution reservation. It therefore does
not block another governed component by itself; any CUDA/MPS client below that
container still requires an exact live MD execution lease. Unknown, mismatched,
or partially registered Docker claims remain blocking.

Stopping an MPS unit normally requires a live Broker socket, global drain, and
zero leases on the target GPU. A missing Broker fails closed. The helper has a
separate emergency `--break-glass-without-broker` path only when accompanied by
`NEXPOLY_GPU_MPS_BREAK_GLASS_REASON`; it writes and `fsync`s a private audit
record before issuing `quit`. Shipped systemd units never select this path.
