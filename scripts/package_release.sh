#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

INCLUDE_DATA="${INCLUDE_DATA:-0}"
RELEASE_DIR="$ROOT_DIR/release"
STAMP="$(date +%Y%m%d-%H%M%S)"
BUNDLE="$RELEASE_DIR/polyprop-release-$STAMP.tar.gz"

for file in Dockerfile frontend/Dockerfile docker-compose.yml nginx.conf backend/.env.example; do
  if [[ ! -f "$file" ]]; then
    echo "Missing deployment file: $file" >&2
    exit 1
  fi
done

missing_data=0
for db_file in backend/data/polyprop.db backend/data/fumol.db; do
  if [[ ! -s "$db_file" ]]; then
    if [[ "$INCLUDE_DATA" == "1" ]]; then
      echo "Missing required database for data-inclusive package: $db_file" >&2
      missing_data=1
    else
      echo "Warning: $db_file is missing or empty. Prepare it before deployment." >&2
    fi
  fi
done

if [[ "$missing_data" == "1" ]]; then
  exit 1
fi

npm --prefix frontend run build

mkdir -p "$RELEASE_DIR"

EXCLUDES=(
  --exclude=.git
  --exclude=.codex
  --exclude=.superpowers
  --exclude=AGENTS.md
  --exclude=CLAUDE.md
  --exclude=.env
  --exclude=backend/.env
  --exclude=backend/tests
  --exclude=frontend/node_modules
  --exclude=frontend/.vite
  --exclude=frontend/*.tsbuildinfo
  --exclude=frontend/vite.config.js
  --exclude=frontend/vite.config.d.ts
  --exclude=__pycache__
  --exclude=.pytest_cache
  --exclude=release
  --exclude=database
  --exclude=design-system
  --exclude=docs/superpowers
)

if [[ "$INCLUDE_DATA" != "1" ]]; then
  EXCLUDES+=(--exclude=backend/data)
fi

tar -czf "$BUNDLE" "${EXCLUDES[@]}" .

echo "Created $BUNDLE"
if [[ "$INCLUDE_DATA" != "1" ]]; then
  echo "Database files were not bundled. Copy backend/data/polyprop.db and backend/data/fumol.db to the deployment host."
fi
