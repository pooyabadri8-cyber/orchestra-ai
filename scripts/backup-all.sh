#!/usr/bin/env bash
# Daily backup of every customer (settings + knowledge + data/, never secrets). Keeps the last 14 per customer.
# crontab:  15 3 * * * /opt/content-agent/scripts/backup-all.sh
cd "$(dirname "$0")/.." || exit 2
for d in customers/*/; do
  n="$(basename "$d")"; [[ "$n" == _* || ! -f "$d/config.yaml" ]] && continue
  .venv/bin/python main.py --customer "$n" backup >/dev/null && ls -1t backups/"$n"-*.tar.gz | tail -n +15 | xargs -r rm -f
done
