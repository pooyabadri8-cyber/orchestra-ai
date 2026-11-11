#!/usr/bin/env bash
# For cron / monitoring:  scripts/healthcheck.sh <name>   -> exit 0 = healthy and the bot's heartbeat is fresh.
# Example crontab (every 5 minutes, restart if dead):
#   */5 * * * * /opt/content-agent/scripts/healthcheck.sh acme-cafe || systemctl restart content-agent@acme-cafe
cd "$(dirname "$0")/.." || exit 2
exec .venv/bin/python main.py --customer "${1:?customer name}" check --live >/dev/null
