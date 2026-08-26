#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 2
fi

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_SCRIPT="$REPOSITORY_ROOT/scripts/tunnel_proxy_firewall.py"
SOURCE_DROP_IN="$REPOSITORY_ROOT/ops/systemd/nexpoly-tunnel-proxy-firewall.service.d/10-stable-address-rules.conf"
TARGET_SCRIPT="/usr/local/libexec/nexpoly-tunnel-proxy-firewall-stable"
TARGET_DROP_IN_DIR="/etc/systemd/system/nexpoly-tunnel-proxy-firewall.service.d"
TARGET_DROP_IN="$TARGET_DROP_IN_DIR/10-stable-address-rules.conf"
SERVICE="nexpoly-tunnel-proxy-firewall.service"
SOCKET="nexpoly-tunnel-proxy.socket"

[[ -f "$SOURCE_SCRIPT" && ! -L "$SOURCE_SCRIPT" &&
  -f "$SOURCE_DROP_IN" && ! -L "$SOURCE_DROP_IN" ]] || {
  echo "Tunnel proxy firewall installation assets are incomplete." >&2
  exit 2
}
[[ -f "/etc/systemd/system/$SERVICE" ]] || {
  echo "The root-managed tunnel proxy firewall service is not installed." >&2
  exit 2
}

/usr/bin/python3 -I -B -c \
  'import ast, pathlib, sys; ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))' \
  "$SOURCE_SCRIPT"

BACKUP_DIR="$(mktemp -d /tmp/nexpoly-tunnel-firewall-install.XXXXXX)"
HAD_SCRIPT=false
HAD_DROP_IN=false
WAS_ACTIVE=false
SOCKET_WAS_ACTIVE=false
[[ -e "$TARGET_SCRIPT" ]] && {
  HAD_SCRIPT=true
  cp --preserve=mode,ownership,timestamps "$TARGET_SCRIPT" "$BACKUP_DIR/script"
}
[[ -e "$TARGET_DROP_IN" ]] && {
  HAD_DROP_IN=true
  cp --preserve=mode,ownership,timestamps "$TARGET_DROP_IN" "$BACKUP_DIR/drop-in"
}
systemctl is-active --quiet "$SERVICE" && WAS_ACTIVE=true
systemctl is-active --quiet "$SOCKET" && SOCKET_WAS_ACTIVE=true

rollback() {
  local status=$?
  trap - ERR
  systemctl stop "$SOCKET" || true
  systemctl stop "$SERVICE" || true
  if [[ -x "$TARGET_SCRIPT" ]]; then
    /usr/bin/python3 -I -B "$TARGET_SCRIPT" stop || true
  fi
  if [[ "$HAD_SCRIPT" == true ]]; then
    install -o root -g root -m 0755 "$BACKUP_DIR/script" "$TARGET_SCRIPT"
  else
    rm -f -- "$TARGET_SCRIPT"
  fi
  if [[ "$HAD_DROP_IN" == true ]]; then
    install -o root -g root -m 0644 "$BACKUP_DIR/drop-in" "$TARGET_DROP_IN"
  else
    rm -f -- "$TARGET_DROP_IN"
  fi
  systemctl daemon-reload || true
  if [[ "$WAS_ACTIVE" == true ]]; then
    systemctl start "$SERVICE" || true
  fi
  if [[ "$SOCKET_WAS_ACTIVE" == true ]]; then
    systemctl start "$SOCKET" || true
  fi
  rm -rf -- "$BACKUP_DIR"
  echo "Tunnel proxy firewall installation failed; previous service configuration was restored." >&2
  exit "$status"
}
trap rollback ERR

# Stop the listener first, then stop the firewall so the currently loaded
# legacy ExecStop removes interface-bound rules without an unfiltered socket.
if [[ "$SOCKET_WAS_ACTIVE" == true ]]; then
  systemctl stop "$SOCKET"
fi
if [[ "$WAS_ACTIVE" == true ]]; then
  systemctl stop "$SERVICE"
fi
install -d -o root -g root -m 0755 /usr/local/libexec "$TARGET_DROP_IN_DIR"
install -o root -g root -m 0755 "$SOURCE_SCRIPT" "$TARGET_SCRIPT"
install -o root -g root -m 0644 "$SOURCE_DROP_IN" "$TARGET_DROP_IN"
systemctl daemon-reload
systemctl start "$SERVICE"
if [[ "$SOCKET_WAS_ACTIVE" == true ]]; then
  systemctl start "$SOCKET"
fi
/usr/bin/python3 -I -B "$TARGET_SCRIPT" status

trap - ERR
rm -rf -- "$BACKUP_DIR"
echo "Stable tunnel proxy firewall rules installed."
