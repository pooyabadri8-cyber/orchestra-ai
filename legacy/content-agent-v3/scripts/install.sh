#!/usr/bin/env bash
# Install / update the service of ONE customer:  sudo scripts/install.sh <name>
# Isolation: every customer runs as its OWN system user (ca-<name>) that owns its folder (mode 700) and nothing else,
# so one customer's process can never read another's secrets or data. The code and the virtualenv are shared, read-only.
# DRY_RUN=1 prints what would be done (user, permissions, unit file) without changing anything.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
STATS_DIR="${STATS_DIR:-/var/lib/content-agent/stats}"
DRY="${DRY_RUN:-}"
run() { if [[ -n "$DRY" ]]; then echo "+ $*"; else "$@"; fi; }

name="${1:-}"
[[ "$name" =~ ^[a-z0-9][a-z0-9-]{1,27}$ ]] || { echo "نام مشتری نامعتبر (حروف کوچک انگلیسی، عدد، خط تیره؛ حداکثر ۲۸ کاراکتر)"; exit 1; }
[[ -f "customers/$name/config.yaml" ]] || { echo "مشتری پیدا نشد. استفاده: scripts/install.sh <name>  (پوشه‌های customers/ را ببینید)"; exit 1; }
[[ -f "customers/$name/secrets.env" ]] || { echo "customers/$name/secrets.env نیست (scripts/new-customer.sh یا secrets.env.example را ببینید)."; exit 1; }
user="ca-$name"

# 1) own user, own folder, nobody else may enter it
if ! id "$user" >/dev/null 2>&1; then run useradd --system --no-create-home --shell /usr/sbin/nologin "$user"; fi
run mkdir -p "customers/$name/data" "customers/$name/logs"
run chown -R "$user:$user" "customers/$name"
run chmod 700 "customers/$name"
run chmod 600 "customers/$name/secrets.env"

# 2) shared code + virtualenv (root-owned, readable by all, writable by nobody else)
if [[ -z "$DRY" ]]; then
  [[ -x .venv/bin/python ]] || python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
  echo "== بررسی سلامت پیش از راه‌اندازی =="
  runuser -u "$user" -- "$REPO/.venv/bin/python" main.py --customer "$name" check
fi

# 3) the folder where each bot publishes its numbers for the admin overview: anyone may add a file, nobody may list or
#    read the others' files; only the admin dashboard user may read them (default ACL)
run install -d -m 1733 "$STATS_DIR"
run setfacl -d -m u:ca-admin:r "$STATS_DIR"

# 4) the service: sandboxed, may write only to its own folder and the stats folder
UNIT=/etc/systemd/system/content-agent@.service
unit() {
  printf '%s\n' '[Unit]' 'Description=Content agent (%i)' 'After=network-online.target' 'Wants=network-online.target' ''
  printf '%s\n' '[Service]' 'User=ca-%i' 'Group=ca-%i' "WorkingDirectory=$REPO" "Environment=STATS_DIR=$STATS_DIR" \
    "ExecStart=$REPO/.venv/bin/python main.py --customer %i" 'Restart=always' 'RestartSec=5' 'UMask=0077' \
    'NoNewPrivileges=yes' 'PrivateTmp=yes' 'ProtectSystem=strict' 'ProtectHome=yes' 'ProtectProc=invisible' 'ProcSubset=pid' \
    'ProtectKernelTunables=yes' 'ProtectControlGroups=yes' 'RestrictSUIDSGID=yes' \
    "ReadWritePaths=$REPO/customers/%i $STATS_DIR" ''
  printf '%s\n' '[Install]' 'WantedBy=multi-user.target'
}
if [[ -n "$DRY" ]]; then echo "+ write $UNIT:"; unit; else unit > "$UNIT"; fi
run systemctl daemon-reload
run systemctl enable "content-agent@$name"
run systemctl restart "content-agent@$name"
if [[ -z "$DRY" ]]; then
  sleep 5
  systemctl --no-pager --lines=5 status "content-agent@$name" || true
  echo "== ضربان ربات =="
  runuser -u "$user" -- "$REPO/.venv/bin/python" main.py --customer "$name" check --live || echo "(تا چند ثانیهٔ اول ممکن است ضربان هنوز نیامده باشد؛ دوباره بزنید)"
  echo "انجام شد. گزارش زنده: journalctl -u content-agent@$name -f"
fi
