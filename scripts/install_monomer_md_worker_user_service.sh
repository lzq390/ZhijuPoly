#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'EOF'
[nexpoly-worker-systemd] ERROR: this legacy installer is disabled and made no changes.

Production Worker installation is part of the reviewed one-time release bootstrap.
Follow the bootstrap procedure in docs/deployment.md, beginning with a read-only plan:

  ./scripts/bootstrap_pull_deploy.py \
    --sha <40-character-main-sha> \
    --production-root /data/lzq/gith/nexpoly \
    --runtime-root /data/lzq/gith/nexpoly-runtime

Do not chmod, replace, reload, or install the Worker unit manually. The reviewed
bootstrap command takes over the exact confirmed unit and records its authority.
Then run the owner-only pull-deploy command on the production host as documented.
The controller state machine and recovery contract are detailed in
docs/release-controller.md.
For local development, follow docs/monomer-md-worker.md and run:

  scripts/dev_server_gpu.sh worker-venv

This compatibility shim intentionally does not read .env.monomer-md-worker,
copy a systemd unit, reload systemd, or start a service.
EOF
exit 2
