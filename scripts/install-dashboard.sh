#!/usr/bin/env bash
# Dashboards (read-only). Put HTTPS (Caddy / nginx) in front before exposing them; they listen on 127.0.0.1 only.
#   sudo scripts/install-dashboard.sh customer <name> <port>   dashboard of ONE customer (runs as that customer's own user;
#                                                              needs DASHBOARD_TOKEN in customers/<name>/secrets.env)
#   sudo scripts/install-dashboard.sh admin [port]             overview of all customers; reads only the published stats,
#                                                              never a customer's folder (token: /etc/content-agent/admin.env)
# DRY_RUN=1 prints the unit instead of installing it.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
STATS_DIR="${STATS_DIR:-/var/lib/content-agent/stats}"
DRY="${DRY_RUN:-}"
run() { if [[ -n "$DRY" ]]; then echo "+ $*"; else "$@"; fi; }
hard=('NoNewPrivileges=yes' 'PrivateTmp=yes' 'ProtectSystem=strict' 'ProtectHome=yes' 'ProtectProc=invisible' 'ProcSubset=pid' 'RestrictSUIDSGID=yes' 'UMask=0077')
mode="${1:-}"
case "$mode" in
  customer)
    name="${2:-}"; port="${3:-}"
    [[ "$name" =~ ^[a-z0-9][a-z0-9-]{1,27}$ && "$port" =~ ^[0-9]+$ && -f "customers/$name/config.yaml" ]] || { echo "استفاده: install-dashboard.sh customer <name> <port>"; exit 1; }
    unit="/etc/systemd/system/content-agent-dashboard@$name.service"
    body() {
      printf '%s\n' '[Unit]' "Description=Dashboard of $name" 'After=network.target' '' '[Service]' "User=ca-$name" "Group=ca-$name" "WorkingDirectory=$REPO" \
        "ExecStart=$REPO/.venv/bin/python main.py --customer $name dashboard --host 127.0.0.1 --port $port" 'Restart=always' "${hard[@]}" \
        "ReadOnlyPaths=$REPO/customers/$name" '' '[Install]' 'WantedBy=multi-user.target'
    }
    svc="content-agent-dashboard@$name" ;;
  admin)
    port="${2:-8786}"
    if ! id ca-admin >/dev/null 2>&1; then run useradd --system --no-create-home --shell /usr/sbin/nologin ca-admin; fi
    run install -d -m 700 /etc/content-agent
    [[ -n "$DRY" || -f /etc/content-agent/admin.env ]] || { echo "ADMIN_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" > /etc/content-agent/admin.env; chmod 600 /etc/content-agent/admin.env; echo "توکن مدیر ساخته شد: /etc/content-agent/admin.env"; }
    unit="/etc/systemd/system/content-agent-admin.service"
    body() {
      printf '%s\n' '[Unit]' 'Description=Content agent admin overview' 'After=network.target' '' '[Service]' 'User=ca-admin' 'Group=ca-admin' "WorkingDirectory=$REPO" \
        'EnvironmentFile=/etc/content-agent/admin.env' "Environment=STATS_DIR=$STATS_DIR" \
        "ExecStart=$REPO/.venv/bin/python main.py admin-dashboard --host 127.0.0.1 --port $port" 'Restart=always' "${hard[@]}" \
        "ReadOnlyPaths=$STATS_DIR" "InaccessiblePaths=$REPO/customers" '' '[Install]' 'WantedBy=multi-user.target'
    }
    svc="content-agent-admin" ;;
  *) echo "استفاده: install-dashboard.sh customer <name> <port> | admin [port]"; exit 1 ;;
esac
if [[ -n "$DRY" ]]; then echo "+ write $unit:"; body; else body > "$unit"; fi
run systemctl daemon-reload
run systemctl enable --now "$svc"
