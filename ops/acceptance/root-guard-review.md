# Finite inventory guard: action-time review required

This source is preparation only. The default command prints the boundary. No
root installation, unit creation, provider start, or manifest change has run.

The sole controlled unit is `nexpoly-gpu-systemd-inventory.service`; this is the
root inventory provider, not a GPU Main service. The original unit and nine
installed Python modules must retain their recorded hashes. Only its manifest
source commit/tree pointer may change, using a stopped-service double-check and
atomic compare-and-swap. No user source is imported by the root guard.

At action time the reviewer must approve the exact guard hash, request hash,
candidate and old manifest hashes, source commit/tree, and an exclusive start
window for the fixed inventory provider. A root-owned start intent admits only
the candidate manifest and a native activation within 20 seconds if the guard
dies before recording the InvocationID. After binding, any changed PID,
InvocationID or start timestamp is unowned and cleanup refuses to stop it.
An independent administrator starting this same fixed provider within that
20-second window cannot be distinguished; exclusive ownership of that window
is therefore a necessary action-time condition, not an offline test claim.

The security review must also explicitly approve the new stop-only Unix
datagram socket `/run/nexpoly-r2-acceptance-20261019/stop.sock`, owned root:1001
with mode 0660. Kernel `SCM_CREDENTIALS` must report UID/GID 1001:1001 and a
positive PID. The only accepted document is the fixed scope, candidate manifest
hash and `operation=stop_restore`; arbitrary units, start/exec, other peers,
duplicate fields and oversized messages are rejected. There is no caller code
or command argument to execute as root. This permits early retirement without
a later sudo cache. It has not been installed or enabled. The unprivileged
`acceptance_md_control.py root-stop` requests this action and checks the root-
owned old-manifest receipt. This is an explicit new stop permission boundary,
not a feature of the old authorization by itself.

Use a transient guard unit with `Restart=no`, `RuntimeMaxSec=93600`,
`TimeoutStopSec=300`, and a fixed `ExecStopPost` invoking the installed guard
`cleanup`. Run as root with an empty capability set, `NoNewPrivileges=yes`,
`PrivateNetwork=yes`, `RestrictAddressFamilies=AF_UNIX`, `ProtectSystem=strict`,
`ProtectHome=yes`, `PrivateTmp=yes`, `MemoryMax=128M`, `CPUQuota=10%`,
`TasksMax=32`. The only writable paths are the fixed provider directory, the
new guard installation directory and its new `/run` directory. Directory write
permission is needed for manifest rename; the guard validates all nine module
hashes before and after and never writes a module. The original unit, sudoers,
firewall and Main services are not modified. There is no boot enablement.

Install the three public reviewed files (`guard.py`, `request.json`,
`manifest.candidate.json`) into the fixed new root-owned directory with native
`install`, then verify the copied bytes against the review packet before
executing them. Never execute the user-writable copy through sudo. `seal` refuses
an existing campaign seal or active provider. `serve` is one-shot and the lock
prevents overlapping actions. The execution limit is 93,900 seconds, including
300 seconds reserved for shutdown, and cannot exceed the approval's fixed UTC
deadline. Re-sealing or resetting a campaign is forbidden.

On normal exit, interruption, startup failure or RuntimeMaxSec, systemd invokes
the fixed cleanup command without needing a sudo cache. Cleanup stops only the
owned provider epoch and restores the exact old manifest. A conflicting hash,
unowned epoch or changed fixed module produces a precise fail-closed blocker
instead of overwriting another actor's work. This is not a guarantee against
an administrator changing root files or a machine power failure.

After reboot the non-enabled provider and transient guard must remain stopped;
the persistent root-owned backup and recovery request allow the same reviewed
cleanup command to restore the manifest. Reboot recovery has not been exercised
and requires an owner action. The fixed inactive installation and public
receipts remain for review after cleanup; no recursive deletion or automatic
uninstall is performed. This retained inert installation must be disclosed in
the final resource inventory. Do not claim automatic uninstall or reboot PASS.
