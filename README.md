# ORCHESTRA AI

پلتفرم چندعاملی برای تولید و انتشار محتوای مالی. طراحی کامل: سند «ORCHESTRA AI — Architecture Proposal v0.2».

## وضعیت

| بخش | وضعیت |
| --- | --- |
| قراردادهای نوع‌دار، بستهٔ انتشار تغییرناپذیر و hash آن | ساخته شد |
| تأیید متصل به hash، نسخه‌گذاری بسته، دفتر ممیزی زنجیره‌ای | ساخته شد |
| ناشر: قفل پیش از ارسال، تلاش مجدد، صف خطا، وضعیت «نامعلوم»، توقف اضطراری | ساخته شد |
| اتصال تلگرام و بله | ساخته شد؛ هنوز روی کانال واقعی آزموده نشده |
| جمع‌آوری داده: Bourse Trader (از نمونهٔ واقعی صفحه) | ساخته شد |
| جمع‌آوری داده: TSE، Codal، FIPIran، TGJU، Traders Arena | منتظر نمونهٔ واقعی (`scripts/capture_sources.py`) |
| تحلیل (بدون مدل)، نویسنده (با مدل)، اتصال عدد به منبع، اسکنر عدد، قواعد | ساخته شد |
| workflow پایدار «تحلیل پایان بازار» با DBOS و انتظار برای تأیید | ساخته شد |
| خط فرمان برای اجرا روی سیستم شخصی | ساخته شد |
| ربات تأیید در تلگرام، Manager، طراحی تصویر و اسلاید | بعدی |

`legacy/content-agent-v3/` نسخهٔ قبلی است و فقط برای مرجع نگه داشته شده؛ کد تازه به آن وابسته نیست.

## راه‌اندازی روی ویندوز

1. **Python 3.12** را از python.org نصب کنید (گزینهٔ «Add python.exe to PATH» را بزنید).
2. **PostgreSQL 16** را از postgresql.org نصب کنید. رمز کاربر `postgres` را یادداشت کنید.
3. در **SQL Shell (psql)** پایگاه‌داده بسازید:

   ```sql
   CREATE USER orchestra WITH PASSWORD 'orchestra';
   CREATE DATABASE orchestra OWNER orchestra;
   CREATE DATABASE orchestra_dbos OWNER orchestra;
   CREATE DATABASE orchestra_test OWNER orchestra;
   ```

4. در PowerShell، داخل پوشهٔ پروژه:

   ```powershell
   py -3.12 -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -e ".[dev]"
   copy .env.example .env
   ```

   اگر PowerShell اجرای اسکریپت را نپذیرفت، یک بار بزنید: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

5. **اجرای تست‌ها:**

   ```powershell
   pytest
   ```

   تست‌ها روی SQLite و Postgres اجرا می‌شوند. اگر Postgres در دسترس نباشد، فقط SQLite اجرا می‌شود.

## اجرای «تحلیل پایان بازار»

پیش از اجرا در `.env` کلید مدل زبانی (`ORCHESTRA_LLM_API_KEY` و `ORCHESTRA_LLM_MODEL`) و در `orgs/pouyan/destinations.yaml` نام کانال‌های **آزمایشی** را بنویسید.

```powershell
python -m orchestra.cli init                  # یک بار: ساخت جدول‌ها و ثبت مقصدها
python -m orchestra.cli run                   # داده می‌گیرد، می‌نویسد، بررسی می‌کند و منتظر تأیید می‌ماند
python -m orchestra.cli show <RUN_ID>         # متن دقیق هر کانال و منبع هر عدد
python -m orchestra.cli approve <RUN_ID>      # یا: reject <RUN_ID> --reason "..."
python -m orchestra.cli publish               # ارسال کارهای تأییدشده
python -m orchestra.cli jobs                  # وضعیت ارسال‌ها
```

اگر ارسالی «نامعلوم» شد، کانال را ببینید: اگر پست هست `confirm-sent <JOB_ID>`، اگر نیست `retry <JOB_ID>`. توقف اضطراری: `pause`.

## گرفتن نمونهٔ واقعی منابع داده

```powershell
python scripts/capture_sources.py
```

پوشهٔ `captures` ساخته می‌شود؛ آن را zip کنید و بفرستید تا parser هر منبع از دادهٔ واقعی نوشته شود.

## اصول

- هیچ پستی بدون تأیید معتبر برای hash دقیق بسته و مقصد منتشر نمی‌شود.
- ارسالی که نتیجه‌اش معلوم نیست، خودکار دوباره فرستاده نمی‌شود؛ انسان تصمیم می‌گیرد.
- هیچ عددی را مدل نمی‌نویسد؛ مدل فقط توکن می‌گذارد و کد عدد را از دادهٔ ذخیره‌شده با منبعش جایگزین می‌کند.
- توکن‌ها فقط در `.env` هستند؛ پایگاه‌داده فقط نام متغیر را نگه می‌دارد.
