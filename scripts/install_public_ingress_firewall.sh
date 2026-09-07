#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 2
fi
if [[ "$#" -ne 1 ]]; then
  echo "Usage: $0 /absolute/path/to/public-ingress-allowlist.conf" >&2
  exit 2
fi

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_SCRIPT="$REPOSITORY_ROOT/scripts/public_ingress_firewall.py"
SOURCE_UNIT="$REPOSITORY_ROOT/ops/systemd/nexpoly-public-ingress-firewall.service"
SOURCE_CONFIG="$1"
TARGET_SCRIPT="/usr/local/libexec/nexpoly-public-ingress-firewall"
TARGET_UNIT="/etc/systemd/system/nexpoly-public-ingress-firewall.service"
TARGET_CONFIG="/etc/nexpoly/public-ingress-allowlist.conf"
SERVICE="nexpoly-public-ingress-firewall.service"

[[ "$SOURCE_CONFIG" = /* ]] || {
  echo "The allowlist configuration path must be absolute." >&2
  exit 2
}
[[ -f "$SOURCE_SCRIPT" && ! -L "$SOURCE_SCRIPT" &&
  -f "$SOURCE_UNIT" && ! -L "$SOURCE_UNIT" &&
  -f "$SOURCE_CONFIG" && ! -L "$SOURCE_CONFIG" ]] || {
  echo "Public-ingress firewall installation assets are incomplete or unsafe." >&2
  exit 2
}

/usr/bin/python3 -I -B "$SOURCE_SCRIPT" validate --config "$SOURCE_CONFIG" >/dev/null
/usr/bin/python3 -I -B -c \
  'import ast, pathlib, sys; ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))' \
  "$SOURCE_SCRIPT"

install -d -o root -g root -m 0755 /usr/local/libexec /etc/nexpoly
install -o root -g root -m 0755 "$SOURCE_SCRIPT" "$TARGET_SCRIPT"
install -o root -g root -m 0644 "$SOURCE_UNIT" "$TARGET_UNIT"
install -o root -g root -m 0600 "$SOURCE_CONFIG" "$TARGET_CONFIG"

systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
/usr/bin/python3 -I -B "$TARGET_SCRIPT" status --config "$TARGET_CONFIG"

echo "NexPoly public-ingress source-IP allowlist installed and active."
