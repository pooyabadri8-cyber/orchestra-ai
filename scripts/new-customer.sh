#!/usr/bin/env bash
# Create a new customer folder from the template:  scripts/new-customer.sh <name>
set -euo pipefail
cd "$(dirname "$0")/.."
name="${1:-}"
[[ "$name" =~ ^[a-z0-9][a-z0-9-]{1,27}$ ]] || { echo "نام مشتری: فقط حروف کوچک انگلیسی، عدد و خط تیره، حداکثر ۲۸ کاراکتر (مثل acme-cafe)"; exit 1; }
[[ -e "customers/$name" ]] && { echo "customers/$name از قبل هست."; exit 1; }
cp -r customers/_template "customers/$name"; chmod -R u+w "customers/$name"
cp "customers/$name/secrets.env.example" "customers/$name/secrets.env"
chmod 600 "customers/$name/secrets.env"
mkdir -p "customers/$name/data" "customers/$name/logs"
cat <<MSG
مشتری «$name» ساخته شد: customers/$name
گام‌های بعد:
  1) customers/$name/profile.yaml را با برند، لحن و قواعد مشتری پر کنید.
  2) customers/$name/knowledge/ را با اطلاعات مشتری پر کنید (ربات فقط از همین‌ها واقعیت می‌نویسد).
  3) customers/$name/post_template.yaml و sources.yaml را تنظیم کنید.
  4) customers/$name/secrets.env را پر کنید (توکن ربات، کانال، OWNER_IDS، کلید مدل).
  5) python main.py --customer $name check
  6) sudo scripts/install.sh $name
MSG
