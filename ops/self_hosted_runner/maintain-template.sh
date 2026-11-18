#!/usr/bin/env bash
set -euo pipefail

# Run from the reviewed deployment package with an administrator's sudo session.
package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

runnerctl() {
    /usr/local/bin/nexpoly-runnerctl "$@"
}

maintenance_now() {
    /usr/bin/python3 -c 'import time; print(int(time.monotonic()))'
}

wait_for_idle() {
    local started now next_progress status_json status_result
    started="$(maintenance_now)"
    next_progress="$started"
    while true; do
        status_json="$(runnerctl status)"
        status_result=0
        /usr/bin/python3 -c '
import json, sys
try:
    status = json.load(sys.stdin)
    if (not isinstance(status, dict) or "paused" not in status or "state" not in status
            or not isinstance(status["paused"], bool)):
        raise ValueError("invalid controller status")
    state = status["state"]
    # The CLI emits null when its initial controller journal does not exist.
    if state is not None:
        if not isinstance(state, dict) or "instance" not in state:
            raise ValueError("invalid controller state")
        if state["instance"] is not None and not isinstance(state["instance"], dict):
            raise ValueError("invalid controller instance")
except (ValueError, TypeError):
    sys.exit(2)
sys.exit(0 if status["paused"] and (state or {}).get("instance") is None else 1)
' <<< "$status_json" || status_result=$?
        case "$status_result" in
            0) return 0 ;;
            1) ;;
            *) echo "Cannot validate controller status; leaving the controller drained." >&2; return 1 ;;
        esac
        now="$(maintenance_now)"
        if (( now - started >= 6600 )); then
            echo "Timed out waiting for VM cleanup; controller remains drained and the active VM is retained." >&2
            return 1
        fi
        if (( now >= next_progress )); then
            echo "Waiting for drain and VM cleanup ($((now - started))/6600 seconds); active VM/job is retained."
            next_progress=$((now + 30))
        fi
        sleep 5
    done
}

maintain_template() {
    echo "Template maintenance: drain the controller and wait for VM cleanup."
    runnerctl drain
    wait_for_idle
    echo "Template maintenance: stop the idle controller."
    systemctl stop nexpoly-runner-controller.service
    # install.sh and update-template take their own lifecycle lock.
    echo "Template maintenance: install the reviewed host package."
    bash "${package_dir}/install.sh"
    echo "Template maintenance: build and validate a new template."
    runnerctl update-template
    echo "Template maintenance: resume runner replenishment."
    runnerctl resume
    echo "Template maintenance: start the controller."
    systemctl start nexpoly-runner-controller.service
    runnerctl status
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    if (( EUID != 0 )); then
        echo "Run this maintenance helper with sudo; administrator execution is required." >&2
        exit 1
    fi
    if (( $# != 0 )); then
        echo "Usage: sudo bash ${BASH_SOURCE[0]}" >&2
        exit 1
    fi
    maintain_template
fi
