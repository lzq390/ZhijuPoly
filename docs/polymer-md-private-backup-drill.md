# Private MD backup drill helper

This Linux development helper accepts pre-existing owner-only runtime receipts.
It is not a production backup or an automatic deployment/role-management tool.
The dedicated database/container and stopped API/Worker are checked before and
inside the Worker root lock, with terminal cleanup of every formal journal.
Plans contain hashes and paths, never a password or DSN.

New plan files and the final evidence path are created exclusively through
no-follow dirfds. Existing paths and their permissions are preserved. Evidence
uses its reserved inode and keeps immutable, fsynced private write-ahead JSON
receipts. If interrupted during final-file refresh, recover the last complete
receipt from the recorded history directory; do not delete it or rerun blindly.

A confirmed-owned temporary role is disabled before reassignment/drop cleanup.
Uncertain creation and cleanup failures remain explicitly unconfirmed; no
unverified existing role is adopted, and no restore database or DROP OWNED data
is deleted automatically. Real operation would create a temporary privileged
role and restore database; that operation requires separate explicit approval.

Tests use synthetic paths, journal fixtures and a fake SQL/restore boundary.
They execute no SQL, role creation, service change or scientific engine and do
not establish real PostgreSQL backup/concurrency/recovery correctness.
