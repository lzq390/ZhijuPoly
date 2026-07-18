#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -P "$SCRIPT_DIR/.." && pwd -P)"
expected_development_gpu_root="$REPO_ROOT/.runtime/gpu-resource"

action="${1:-}"
index="${2:-}"
break_glass_option="${3:-}"

if [[ "$action" != "start" && "$action" != "stop" ]]; then
  echo "usage: gpu_mps_control.sh start|stop 1|2|3 [--break-glass-without-broker]" >&2
  exit 2
fi
if [[ "$index" != "1" && "$index" != "2" && "$index" != "3" ]]; then
  echo "GPU0 is excluded; MPS is supported only on GPU1, GPU2, and GPU3" >&2
  exit 2
fi
if [[ $# -gt 3 || ( -n "$break_glass_option" && ( "$action" != "stop" || "$break_glass_option" != "--break-glass-without-broker" ) ) ]]; then
  echo "usage: gpu_mps_control.sh start|stop 1|2|3 [--break-glass-without-broker]" >&2
  exit 2
fi
if [[ "$(id -u)" != "1001" || "$(id -g)" != "1001" ]]; then
  echo "NexPoly MPS must run as the shared 1001:1001 service identity" >&2
  exit 1
fi

case "$index" in
  1) expected_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771" ;;
  2) expected_uuid="GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe" ;;
  3) expected_uuid="GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5" ;;
esac

state_root="${NEXPOLY_GPU_STATE_ROOT:-/data/lzq/gith/nexpoly-runtime/state/gpu-resource}"
external_reservations="${NEXPOLY_GPU_EXTERNAL_RESERVATIONS:-$state_root/external-reservations.json}"
CUDA_VISIBLE_DEVICES="$expected_uuid"
mps_slot_directory="${NEXPOLY_GPU_MPS_SLOT_DIRECTORY:-$state_root/mps-$index}"
CUDA_MPS_PIPE_DIRECTORY="${NEXPOLY_GPU_MPS_PIPE_DIRECTORY:-$mps_slot_directory/pipe}"
CUDA_MPS_LOG_DIRECTORY="${NEXPOLY_GPU_MPS_LOG_DIRECTORY:-$mps_slot_directory/log}"
descriptor_authority="${NEXPOLY_GPU_MPS_DESCRIPTOR_AUTHORITY:-0}"
descriptor_authority_pid="${NEXPOLY_GPU_MPS_AUTHORITY_PID:-}"
descriptor_authority_start_ticks="${NEXPOLY_GPU_MPS_AUTHORITY_START_TICKS:-}"
descriptor_expected_root="${NEXPOLY_GPU_MPS_EXPECTED_ROOT:-}"
broker_socket="${NEXPOLY_GPU_BROKER_SOCKET:-$state_root/broker.sock}"

if [[ "$descriptor_authority" == "1" ]]; then
  if [[ "$index" == "2" ]]; then
    echo "formal development descriptor authority forbids production GPU2" >&2
    exit 1
  fi
  if [[ "$REPO_ROOT" == "/data/lzq/gith/nexpoly" ||
    "$REPO_ROOT" == /data/lzq/gith/nexpoly/* ]]; then
    echo "formal development descriptor authority forbids the production repository" >&2
    exit 1
  fi
  [[ "$descriptor_authority_pid" =~ ^[1-9][0-9]*$ &&
    "$descriptor_authority_start_ticks" =~ ^[1-9][0-9]*$ &&
    "$state_root" =~ ^/proc/"$descriptor_authority_pid"/fd/[0-9]+$ &&
    "$mps_slot_directory" =~ ^/proc/"$descriptor_authority_pid"/fd/[0-9]+$ &&
    "$CUDA_MPS_PIPE_DIRECTORY" =~ ^/proc/"$descriptor_authority_pid"/fd/[0-9]+$ &&
    "$CUDA_MPS_LOG_DIRECTORY" =~ ^/proc/"$descriptor_authority_pid"/fd/[0-9]+$ &&
    "$external_reservations" =~ ^/proc/"$descriptor_authority_pid"/fd/[0-9]+$ &&
    "$broker_socket" == "$state_root/broker.sock" &&
    "$descriptor_expected_root" == "$expected_development_gpu_root" ]] || {
    echo "MPS descriptor authority paths are invalid" >&2
    exit 1
  }
  /usr/bin/python3 - \
    "$descriptor_authority_pid" "$descriptor_authority_start_ticks" \
    "$index" "$descriptor_expected_root" "$state_root" \
    "$mps_slot_directory" "$CUDA_MPS_PIPE_DIRECTORY" \
    "$CUDA_MPS_LOG_DIRECTORY" "$external_reservations" <<'PY'
import os
import stat
import sys

authority_pid = int(sys.argv[1])
expected_start_ticks = int(sys.argv[2])
index = int(sys.argv[3])
if authority_pid == os.getpid():
    raise SystemExit("MPS descriptor authority must be held by its harness")
try:
    authority_metadata = os.stat(f"/proc/{authority_pid}")
except OSError as exc:
    raise SystemExit("MPS descriptor authority process is unavailable") from exc
if authority_metadata.st_uid != os.geteuid():
    raise SystemExit("MPS descriptor authority process has an unsafe owner")
try:
    process_stat = open(
        f"/proc/{authority_pid}/stat", encoding="ascii"
    ).read()
except OSError as exc:
    raise SystemExit("MPS descriptor authority process is unavailable") from exc
command_end = process_stat.rfind(")")
fields = process_stat[command_end + 2 :].split() if command_end >= 0 else []
if len(fields) <= 19 or int(fields[19]) != expected_start_ticks:
    raise SystemExit("MPS descriptor authority process identity changed")

expected_root = sys.argv[4]
directory_paths = sys.argv[5:9]
identities = []
for raw in directory_paths:
    metadata = os.stat(raw, follow_symlinks=True)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SystemExit("MPS descriptor authority directory is unsafe")
    identities.append((metadata.st_dev, metadata.st_ino))
if len(set(identities)) != len(identities):
    raise SystemExit("MPS descriptor authority directories are not distinct")
root, slot, pipe, log = directory_paths
expected_root_metadata = os.lstat(expected_root)
if (
    stat.S_ISLNK(expected_root_metadata.st_mode)
    or not stat.S_ISDIR(expected_root_metadata.st_mode)
    or (expected_root_metadata.st_dev, expected_root_metadata.st_ino)
    != identities[0]
):
    raise SystemExit("MPS descriptor authority differs from the development root")
relations = (
    (os.path.join(root, f"mps-{index}"), identities[1]),
    (os.path.join(slot, "pipe"), identities[2]),
    (os.path.join(slot, "log"), identities[3]),
)
for path, expected_identity in relations:
    metadata = os.stat(path)
    if (metadata.st_dev, metadata.st_ino) != expected_identity:
        raise SystemExit("MPS descriptor authority hierarchy changed")
reservations = os.stat(sys.argv[9])
root_reservations = os.stat(os.path.join(root, "external-reservations.json"))
if (
    not stat.S_ISREG(reservations.st_mode)
    or reservations.st_uid != os.geteuid()
    or reservations.st_gid != os.getegid()
    or reservations.st_nlink != 1
    or stat.S_IMODE(reservations.st_mode) != 0o600
    or (reservations.st_dev, reservations.st_ino)
    != (root_reservations.st_dev, root_reservations.st_ino)
):
    raise SystemExit("MPS reservation descriptor authority escaped its root")
PY
  state_root="/proc/self/fd/${state_root##*/}"
  external_reservations="/proc/self/fd/${external_reservations##*/}"
  mps_slot_directory="/proc/self/fd/${mps_slot_directory##*/}"
  CUDA_MPS_PIPE_DIRECTORY="/proc/self/fd/${CUDA_MPS_PIPE_DIRECTORY##*/}"
  CUDA_MPS_LOG_DIRECTORY="/proc/self/fd/${CUDA_MPS_LOG_DIRECTORY##*/}"
  broker_socket="$state_root/broker.sock"
elif [[ "$descriptor_authority" == "0" ]]; then
  for directory in \
    "$state_root" "$mps_slot_directory" \
    "$CUDA_MPS_PIPE_DIRECTORY" "$CUDA_MPS_LOG_DIRECTORY"; do
    if [[ -L "$directory" || ( -e "$directory" && ! -d "$directory" ) ]]; then
      echo "MPS runtime directory is unsafe: $directory" >&2
      exit 1
    fi
  done
else
  echo "NEXPOLY_GPU_MPS_DESCRIPTOR_AUTHORITY must be 0 or 1" >&2
  exit 1
fi

require_exact_idle_mps_inventory() {
  /usr/bin/python3 - <<'PY'
import os
import subprocess

try:
    completed = subprocess.run(
        ["nvidia-cuda-mps-control"],
        input=b"ps\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5,
        env=os.environ,
    )
except (OSError, subprocess.SubprocessError) as exc:
    raise SystemExit("MPS client inventory query failed") from exc
if completed.returncode != 0 or completed.stderr:
    raise SystemExit("MPS client inventory query failed")
output = completed.stdout
if len(output) > 1024 * 1024:
    raise SystemExit("MPS client inventory is oversized")
if output in {b"", b"Server not found", b"Server not found\n"}:
    raise SystemExit(0)
try:
    text = output.decode("ascii")
except UnicodeDecodeError:
    raise SystemExit("MPS client inventory is invalid") from None
lines = text.splitlines()
header = ["PID", "ID", "SERVER", "DEVICE", "NAMESPACE", "COMMAND"]
if (
    not lines
    or any(not line.strip() for line in lines)
    or lines[0].split() != header
):
    raise SystemExit("MPS client inventory is invalid")
if len(lines) == 1:
    raise SystemExit("MPS client inventory idle response is noncanonical")
raise SystemExit("MPS still has active clients")
PY
}

if [[ "$action" == "start" ]]; then
  gpu_record="$(nvidia-smi --query-gpu=uuid,compute_mode --format=csv,noheader,nounits -i "$index")"
  if [[ "$gpu_record" == *$'\n'* ]]; then
    echo "GPU$index identity query returned more than one record" >&2
    exit 1
  fi
  IFS=',' read -r actual_uuid compute_mode extra_field <<<"$gpu_record"
  actual_uuid="${actual_uuid//[[:space:]]/}"
  normalized_compute_mode="${compute_mode^^}"
  normalized_compute_mode="${normalized_compute_mode//_/ }"
  read -r compute_mode_word_1 compute_mode_word_2 compute_mode_extra <<<"$normalized_compute_mode"
  if [[ "$actual_uuid" != "$expected_uuid" ]]; then
    echo "GPU$index UUID mismatch: expected $expected_uuid, got ${actual_uuid:-missing}" >&2
    exit 1
  fi
  if [[ -n "${extra_field:-}" || "$compute_mode_word_1" != "EXCLUSIVE" || "$compute_mode_word_2" != "PROCESS" || -n "${compute_mode_extra:-}" ]]; then
    echo "GPU$index must already be drained and set to EXCLUSIVE_PROCESS" >&2
    exit 1
  fi
  /usr/bin/python3 - "$external_reservations" "$expected_uuid" <<'PY'
from pathlib import Path
import sys

from ops.gpu_broker.server import (
    load_external_reservations,
    query_docker_gpu_claims,
    query_systemd_gpu_claims,
)

policy = load_external_reservations(Path(sys.argv[1]))
expected_uuid = sys.argv[2]
if expected_uuid in policy.blocked_gpu_uuids:
    raise SystemExit("GPU remains blocked by the external reservation inventory")
for claim in query_docker_gpu_claims():
    if expected_uuid in claim.gpu_uuids:
        raise SystemExit(
            f"GPU remains claimed by Docker container {claim.container_id[:12]}"
        )
for claim in query_systemd_gpu_claims():
    if expected_uuid in claim.gpu_uuids:
        raise SystemExit(f"GPU remains declared by systemd service {claim.unit}")
PY
  compute_processes="$(nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader,nounits)"
  /usr/bin/python3 - "$expected_uuid" "$compute_processes" <<'PY'
import sys

expected_uuid = sys.argv[1]
for raw_line in sys.argv[2].splitlines():
    if not raw_line.strip():
        continue
    fields = [field.strip() for field in raw_line.split(",")]
    if len(fields) != 2:
        raise SystemExit("CUDA compute process inventory is invalid")
    uuid, raw_pid = fields
    try:
        pid = int(raw_pid)
    except ValueError:
        raise SystemExit("CUDA compute process inventory PID is invalid") from None
    if pid <= 0 or not uuid.startswith("GPU-"):
        raise SystemExit("CUDA compute process inventory identity is invalid")
    if uuid == expected_uuid:
        raise SystemExit(f"GPU still has CUDA compute PID {pid}")
PY
  if [[ -e "$CUDA_MPS_PIPE_DIRECTORY/control" || -L "$CUDA_MPS_PIPE_DIRECTORY/control" ]]; then
    if [[ -L "$CUDA_MPS_PIPE_DIRECTORY/control" || ! -p "$CUDA_MPS_PIPE_DIRECTORY/control" && ! -S "$CUDA_MPS_PIPE_DIRECTORY/control" ]]; then
      echo "existing MPS control channel is unsafe" >&2
      exit 1
    fi
    require_exact_idle_mps_inventory
  fi
fi

export CUDA_VISIBLE_DEVICES
export CUDA_MPS_PIPE_DIRECTORY
export CUDA_MPS_LOG_DIRECTORY
if [[ "$descriptor_authority" == "0" ]]; then
  install -d -m 0700 \
    "$state_root" \
    "$mps_slot_directory" \
    "$CUDA_MPS_PIPE_DIRECTORY" \
    "$CUDA_MPS_LOG_DIRECTORY"
fi

if [[ "$action" == "start" ]]; then
  # Compute mode is intentionally not changed here.  The later production
  # maintenance window must drain the card and set EXCLUSIVE_PROCESS first.
  exec nvidia-cuda-mps-control -d
fi

# Stop is a separate fail-closed operation.  A stale/missing control channel,
# an unexpected owner, an unparsable inventory, or any live client prevents
# `quit`; systemd must report failure rather than killing a shared MPS server
# out from under a governed workload.
actual_uuid="$(nvidia-smi --query-gpu=uuid --format=csv,noheader,nounits -i "$index" | tr -d '[:space:]')"
if [[ "$actual_uuid" != "$expected_uuid" ]]; then
  echo "GPU$index UUID mismatch while stopping MPS" >&2
  exit 1
fi
if [[ -L "$CUDA_MPS_PIPE_DIRECTORY/control" || ! -e "$CUDA_MPS_PIPE_DIRECTORY/control" ]]; then
  echo "MPS control channel is missing or unsafe" >&2
  exit 1
fi
if [[ ! -p "$CUDA_MPS_PIPE_DIRECTORY/control" && ! -S "$CUDA_MPS_PIPE_DIRECTORY/control" ]]; then
  echo "MPS control channel is not a FIFO/socket" >&2
  exit 1
fi
if [[ "$(stat -c '%u:%g' "$CUDA_MPS_PIPE_DIRECTORY/control")" != "1001:1001" ]]; then
  echo "MPS control channel has an unexpected owner" >&2
  exit 1
fi
require_exact_idle_mps_inventory
if [[ -L "$broker_socket" ]]; then
  echo "GPU Broker socket path is unsafe; refusing MPS quit" >&2
  exit 1
fi
if [[ ! -e "$broker_socket" ]]; then
  if [[ "$break_glass_option" != "--break-glass-without-broker" ]]; then
    echo "GPU Broker socket is missing; refusing MPS quit" >&2
    echo "An audited emergency stop requires --break-glass-without-broker and NEXPOLY_GPU_MPS_BREAK_GLASS_REASON" >&2
    exit 1
  fi
  break_glass_reason="${NEXPOLY_GPU_MPS_BREAK_GLASS_REASON:-}"
  audit_file="$state_root/mps-break-glass-audit.jsonl"
  /usr/bin/python3 - "$audit_file" "$break_glass_reason" "$index" "$expected_uuid" "$broker_socket" <<'PY'
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
from datetime import datetime, timezone

audit_path = Path(sys.argv[1])
reason = sys.argv[2].strip()
if not reason or len(reason) > 512 or not all(character.isprintable() for character in reason):
    raise SystemExit(
        "NEXPOLY_GPU_MPS_BREAK_GLASS_REASON must be 1-512 printable characters"
    )
if audit_path.is_symlink():
    raise SystemExit("MPS break-glass audit path is unsafe")
flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(audit_path, flags, 0o600)
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit("MPS break-glass audit target is not a regular file")
    if metadata.st_uid != os.getuid() or metadata.st_gid != os.getgid():
        raise SystemExit("MPS break-glass audit target has an unexpected owner")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SystemExit("MPS break-glass audit target permissions are too broad")
    record = {
        "schema_version": 1,
        "event": "mps_stop_without_broker_authorized",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "gpu_index": int(sys.argv[3]),
        "gpu_uuid": sys.argv[4],
        "broker_socket": sys.argv[5],
        "reason": reason,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "pid": os.getppid(),
    }
    payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    os.write(descriptor, payload)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
  printf 'SECURITY AUDIT: break-glass MPS stop without Broker; gpu=%s uuid=%s reason=%q\n' \
    "$index" "$expected_uuid" "$break_glass_reason" >&2
else
  if [[ -n "$break_glass_option" ]]; then
    echo "Break-glass is permitted only when the GPU Broker socket is absent" >&2
    exit 1
  fi
  if [[ ! -S "$broker_socket" ]]; then
    echo "GPU Broker socket path is not a socket; refusing MPS quit" >&2
    exit 1
  fi
  /usr/bin/python3 - "$broker_socket" "$expected_uuid" <<'PY'
import sys
from gpu_resource import GpuBrokerClient

status = GpuBrokerClient(sys.argv[1]).status()
if status.get("draining") is not True:
    raise SystemExit("GPU Broker is not drained; refusing MPS quit")
leases = status.get("leases")
if not isinstance(leases, list):
    raise SystemExit("GPU Broker lease inventory is invalid; refusing MPS quit")
if any(isinstance(lease, dict) and lease.get("gpu_uuid") == sys.argv[2] for lease in leases):
    raise SystemExit("GPU Broker still has a lease on this card; refusing MPS quit")
PY
fi
printf 'quit\n' | nvidia-cuda-mps-control
