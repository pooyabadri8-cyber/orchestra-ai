# Content Agent — ادمین هوشمند کانال (محصول)

ربات ادمین کانال برای تلگرام و بله: از منابع خبری و جدول محتوا، پست فارسی با قالب ثابت می‌نویسد، با قواعد ایمنی چک می‌کند و (خودکار یا با تأیید شما) منتشر می‌کند. هسته یکی است؛ هر مشتری یک پوشه است.

```
agent/                 هستهٔ مشترک (بدون هیچ نام برند و قاعدهٔ صنعتی)
customers/
  _template/           قالب مشتری جدید
  example-cafe/        مشتری نمونهٔ غیرمالی (کافهٔ خیالی)
  pouyan/              کارگزاری پویان (مشتری اول؛ ماژول‌های بازار سرمایه روشن)
scripts/               new-customer.sh · install.sh · healthcheck.sh · backup-all.sh
tests/                 تست‌های آفلاین (python -m pytest)
docs/                  PRODUCT-FA.md (نقشه و مرزها) · PILOT-FA.md (پایلوت مشتری دوم) · features-fa.md (راهنمای قابلیت‌ها)
```

هر پوشهٔ مشتری: `profile.yaml` (برند، لحن، قواعد) · `config.yaml` (زمان‌بندی و حالت‌ها) · `post_template.yaml` · `sources.yaml` · `knowledge/` · `secrets.env` (فقط روی سرور؛ هرگز در گیت یا زیپ) · `data/` و `logs/` (خودکار).

## مشتری جدید در ۶ دستور

```bash
scripts/new-customer.sh acme-cafe          # پوشه از قالب ساخته می‌شود
# profile.yaml، knowledge/، post_template.yaml، sources.yaml و secrets.env را پر کنید
python main.py --customer acme-cafe check  # بررسی سلامت آفلاین
sudo scripts/install.sh acme-cafe          # venv + سرویس systemd جدا (content-agent@acme-cafe)
python main.py --customer acme-cafe check --live   # ضربان ربات در حال اجرا
python main.py --customer acme-cafe backup         # پشتیبان (بدون secrets)
```

برای پشتیبان روزانه و پایش: `scripts/backup-all.sh` و `scripts/healthcheck.sh` را در cron بگذارید (نمونه داخل خود فایل‌هاست).

دستورهای صاحب کانال: `/auto` وضعیت · `/pause` توقف اضطراری · `/dead` صف خطا (`/retry` و `/sent`) · `/calendar` برنامهٔ هفتگی.

جزئیات مرزها، مهاجرت پویان و نقشهٔ فازهای بعدی: `docs/PRODUCT-FA.md`.

## داشبورد (فاز ۵)

```
python main.py --customer X dashboard --port 8791        # داشبورد یک مشتری (فقط‌خواندنی، نیازمند DASHBOARD_TOKEN)
ADMIN_TOKEN=... python main.py admin-dashboard --port 8790   # نمای مدیر؛ فقط عکس‌های عددی STATS_DIR
sudo scripts/install.sh X && sudo scripts/install-dashboard.sh customer X 8791
```
جزئیات و محدودیت‌ها: `docs/PRODUCT-FA.md`.
