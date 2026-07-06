#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

INCLUDE_DATA="${INCLUDE_DATA:-0}"
RELEASE_DIR="$ROOT_DIR/release"
STAMP="$(date +%Y%m%d-%H%M%S)"
BUNDLE="$RELEASE_DIR/nexpoly-release-$STAMP.tar.gz"

for file in Dockerfile frontend/Dockerfile docker-compose.yml nginx.conf backend/.env.example; do
  if [[ ! -f "$file" ]]; then
    echo "Missing deployment file: $file" >&2
    exit 1
  fi
done

mapfile -t REQUIRED_MODEL_FILES < <(PYTHONPATH=backend python -m app.model_asset_manifest --format paths)

missing_model=0
for model_file in "${REQUIRED_MODEL_FILES[@]}"; do
  if [[ ! -s "$model_file" ]]; then
    echo "Missing required model artifact for deployment package: $model_file" >&2
    missing_model=1
  fi
done

if [[ "$missing_model" == "1" ]]; then
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
  --exclude=*.log
  --exclude=*.pid
  --exclude=online_retrieval/data
  --exclude=release
  --exclude=database
  --exclude=design-system
  --exclude=docs/superpowers
)

if [[ "$INCLUDE_DATA" != "1" ]]; then
  EXCLUDES+=(--exclude=backend/data)
fi

tar --dereference -czf "$BUNDLE" "${EXCLUDES[@]}" .

echo "Created $BUNDLE"
echo "Model artifacts under model/ were bundled; symlinks were dereferenced into real files."
if [[ "$INCLUDE_DATA" != "1" ]]; then
  echo "Legacy SQLite files were not bundled. Deploy with Postgres migrations/imports and restore data from a governed dump when needed."
fi
