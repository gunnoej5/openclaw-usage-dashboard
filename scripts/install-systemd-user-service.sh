#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="openclaw-usage-dashboard.service"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
SERVICE_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}"
PORT="${USAGE_DASHBOARD_PORT:-9393}"
STATE_DIR="${OPENCLAW_STATE_DIR:-${HOME}/.openclaw}"

mkdir -p "${SYSTEMD_DIR}"

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=OpenClaw Usage Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO_ROOT}
Environment=PYTHONUNBUFFERED=1
Environment=USAGE_DASHBOARD_PORT=${PORT}
Environment=OPENCLAW_STATE_DIR=${STATE_DIR}
ExecStart=${REPO_ROOT}/scripts/start-usage-dashboard.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "${SERVICE_NAME}"

if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "${USER}" >/dev/null 2>&1 || true
fi

echo "Installed ${SERVICE_FILE}"
systemctl --user --no-pager --full status "${SERVICE_NAME}"
