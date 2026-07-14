#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'EOF'
[nexpoly-worker-systemd] ERROR: this legacy installer is disabled and made no changes.

Production Worker installation is part of the reviewed one-time release bootstrap.
Follow "One-time production preparation" in docs/release-controller.md, beginning with:

  python3 scripts/bootstrap_release_root.py \
    --production-root /data/lzq/gith/nexpoly

Install the audited candidate unit only during that maintenance-window procedure,
then dispatch the CI workflow from main with operation=bootstrap as documented.
For local development, follow docs/monomer-md-worker.md and run:

  scripts/dev_server_gpu.sh worker-venv

This compatibility shim intentionally does not read .env.monomer-md-worker,
copy a systemd unit, reload systemd, or start a service.
EOF
exit 2
