#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'MESSAGE'
The legacy checkout-first production deploy path is disabled.
Use the protected main branch CI workflow, which invokes release_controller.py
with an immutable release bundle and digest-pinned Backend/Web images.
MESSAGE
exit 2
