#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'MESSAGE'
The legacy ad-hoc production deploy path is disabled.
Production is updated manually through the owner-only pull-deploy controller:

  /data/lzq/gith/nexpoly-runtime/bin/nexpoly-pull-deploy plan ...
  /data/lzq/gith/nexpoly-runtime/bin/nexpoly-pull-deploy prepare ...
  /data/lzq/gith/nexpoly-runtime/bin/nexpoly-pull-deploy apply ...

CI only publishes and smokes digest-pinned Backend/Web images.  It never
connects to production and no release tar bundle is produced.
MESSAGE
exit 2
