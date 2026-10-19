# Finite inventory guard: exact action-time review required

Preparation only. No root installation, unit creation, manifest change or
provider start has executed. The default request is timer-only and creates no
owner-stop socket or new UID-1001 stop permission.

Only `nexpoly-gpu-systemd-inventory.service` is controlled. Its original unit
and nine root-installed modules retain their exact recorded hashes. Only the
manifest source commit/tree pointer changes, with a stopped-service double
check and atomic CAS. No user-writable source is imported as root.

The review packet pins guard, request, candidate/old manifest, source commit,
source tree and the complete transient unit properties. Copy the three reviewed
public files to the fixed new root-owned installation, verify the copied
hashes, then execute only those installed bytes. No sudoers, firewall, Main
unit or boot-enable change is included. Existing campaign seals cannot reset.

A root-owned 20-second start intent covers a crash before InvocationID binding.
The provider activation window must be exclusive: another administrator
starting the same fixed provider within that window cannot be distinguished.
After binding, PID/InvocationID/start timestamp changes are unowned; cleanup
refuses to stop that epoch.

The reviewed unit is finite: RuntimeMaxSec=93600, TimeoutStopSec=300, Restart=no,
fixed ExecStopPost cleanup, CPUQuota=10%, MemoryMax=128MiB, swap=0, TasksMax=32,
empty capability sets, NoNewPrivileges, PrivateNetwork/Devices/Tmp,
ProtectSystem=strict, ProtectHome and AF_UNIX only. The only writable paths are
the fixed provider directory, new guard installation and new runtime directory.
Directory writes support manifest CAS; module hashes are checked before/after.

The one-shot plan permits 93900 seconds including the stop reserve and cannot
pass 2026-10-20T18:53:34Z. Normal exit, interruption, startup failure and timeout
invoke root-owned cleanup without another sudo interaction. It stops only the
owned provider epoch and restores the exact old manifest. Conflicts fail
closed. In the default timer-only plan, successful tests do not trigger early
retirement: this auxiliary provider remains charged until the fixed deadline.
The short window must explicitly retain Root for the approved independent MD
phase; a non-retaining short binding is rejected before touching Main unless
the separate owner-stop permission was explicitly selected.

An optional request can set `owner_stop_enabled=true`. Only that additional
approval creates `/run/nexpoly-r2-acceptance-20261019/stop.sock`, root:1001 0660,
Unix datagram with kernel SCM_CREDENTIALS. It accepts UID/GID 1001:1001, positive
PID, fixed scope/candidate and stop_restore only. There is no secret token;
other processes of that UID can request the bounded early stop. This is a new
permission relative to the previous read-only inventory API. It is not needed
for scientific correctness or inventory, and is not part of the default
request. The supplied optional request has a separate exact hash; no arbitrary
unit/start/exec action is accepted. The CLI root-stop helper refuses timer-only
plans before looking for a socket.

Cleanup retains inert installation files and public receipts. No recursive
uninstall or reboot enablement occurs. Reboot recovery requires owner action
with the reviewed root-owned cleanup command and is not tested. No guarantee
is claimed against root-file modification or power failure. Root inactive,
old-manifest rollback and unchanged modules/unit must be observed before final
resource retirement is declared.
