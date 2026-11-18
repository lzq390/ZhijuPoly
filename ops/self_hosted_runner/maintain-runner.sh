#!/usr/bin/env bash
set -euo pipefail

package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

# Reuse the existing bounded wait after actual job evidence, not for an unused
# registered standby (which drain alone intentionally keeps alive).
source "${package_dir}/maintain-template.sh"
maintenance_python() {
    /usr/bin/python3 -I -B "$@"
}

maintain_runner() {
    echo "Runner maintenance: drain and stop the installed controller; retain its VM."
    runnerctl drain
    systemctl stop nexpoly-runner-controller.service
    local retirement_result=0
    maintenance_python "${package_dir}/retire-idle.py" || retirement_result=$?
    case "$retirement_result" in
        0) ;;
        75)
            echo "A real assigned/busy job remains; let the drained installed controller finish and recycle it."
            systemctl start nexpoly-runner-controller.service
            wait_for_idle
            systemctl stop nexpoly-runner-controller.service
            maintenance_python "${package_dir}/retire-idle.py"
            ;;
        76)
            systemctl start nexpoly-runner-controller.service
            echo "Retirement could not obtain complete API evidence; VM retained under the drained installed controller. Retry maintenance after API recovery." >&2
            exit 1
            ;;
        *)
            echo "Retirement failed; controller remains drained/stopped and VM/state are retained. No proxy or template change attempted." >&2
            exit "$retirement_result"
            ;;
    esac

    echo "Runner maintenance: migrate installed upstream and apply dedicated Squid/firewall together."
    maintenance_python "${package_dir}/switch-upstream.py"
    echo "Runner maintenance: rebuild the diagnostic guest template."
    runnerctl update-template
    echo "Runner maintenance: resume and start the controller after all acceptance stages passed."
    runnerctl resume
    systemctl start nexpoly-runner-controller.service
    runnerctl status
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    if (( EUID != 0 )); then
        echo "Run this maintenance entry point with sudo; administrator execution is required." >&2
        exit 1
    fi
    if (( $# != 0 )); then
        echo "Usage: sudo bash ${BASH_SOURCE[0]}" >&2
        exit 1
    fi
    maintain_runner
fi
