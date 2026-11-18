#!/usr/bin/env bash
set -euo pipefail
# This entry point never acquires root via Docker or another service. Run it with
# an administrator's sudo session after reviewing the staged package.
package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec /usr/bin/python3 -I -B "${package_dir}/host.py" install --source "${package_dir}" "$@"
