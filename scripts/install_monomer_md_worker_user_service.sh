#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="${NEXPOLY_MONOMER_MD_WORKER_SYSTEMD_UNIT:-nexpoly-monomer-md-worker.service}"
UNIT_SOURCE="$ROOT_DIR/ops/systemd/$UNIT_NAME"
USER_SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_TARGET="$USER_SYSTEMD_DIR/$UNIT_NAME"
CURRENT_USER="$(id -un)"

log() {
  printf '[nexpoly-worker-systemd] %s\n' "$*"
}

die() {
  printf '[nexpoly-worker-systemd] ERROR: %s\n' "$*" >&2
  exit 1
}

[[ -f "$UNIT_SOURCE" ]] || die "Unit template is missing: $UNIT_SOURCE"
[[ -f "$ROOT_DIR/.env.monomer-md-worker" ]] || die "Worker env file is missing: $ROOT_DIR/.env.monomer-md-worker"
command -v systemctl >/dev/null 2>&1 || die "systemctl is required."

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
[[ -d "$XDG_RUNTIME_DIR" ]] || die "User systemd runtime is unavailable: $XDG_RUNTIME_DIR"

if command -v loginctl >/dev/null 2>&1; then
  if loginctl show-user "$CURRENT_USER" -p Linger 2>/dev/null | grep -qx 'Linger=no'; then
    log "Trying to enable linger for $CURRENT_USER so the user service can survive logout."
    loginctl enable-linger "$CURRENT_USER" >/dev/null 2>&1 || log "Could not enable linger without elevated permissions; continuing with the active user manager."
  fi
fi

mkdir -p "$USER_SYSTEMD_DIR"
cp "$UNIT_SOURCE" "$UNIT_TARGET"

log "Installed $UNIT_TARGET."
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"
systemctl --user --no-pager --full status "$UNIT_NAME"
