# ORCHESTRA AI

پلتفرم چندعاملی برای تولید و انتشار محتوای مالی. طراحی کامل: سند «ORCHESTRA AI — Architecture Proposal v0.2».

## وضعیت

| بخش | وضعیت |
| --- | --- |
| قراردادهای نوع‌دار (`orchestra/contracts.py`) | ساخته شد |
| بستهٔ انتشار تغییرناپذیر و hash آن (`orchestra/package.py`) | ساخته شد |
| تأیید متصل به hash، نسخه‌گذاری بسته، دفتر ممیزی زنجیره‌ای | ساخته شد |
| ناشر: قفل پیش از ارسال، تلاش مجدد، صف خطا، وضعیت «نامعلوم»، توقف اضطراری | ساخته شد |
| adapter تلگرام و بله | ساخته شد (هنوز روی کانال واقعی آزموده نشده) |
| موتور workflow (DBOS)، عامل‌ها، Manager، ربات تأیید | بعدی |

`legacy/content-agent-v3/` نسخهٔ قبلی است و فقط برای مرجع نگه داشته شده؛ کد تازه به آن وابسته نیست.

## راه‌اندازی روی ویندوز

1. **Python 3.12** را از python.org نصب کنید (گزینهٔ «Add python.exe to PATH» را بزنید).
2. **PostgreSQL 16** را از postgresql.org نصب کنید. رمز کاربر `postgres` را یادداشت کنید.
3. در **SQL Shell (psql)** پایگاه‌داده بسازید:

   ```sql
   CREATE USER orchestra WITH PASSWORD 'orchestra';
   CREATE DATABASE orchestra OWNER orchestra;
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

## اصول

- هیچ پستی بدون تأیید معتبر برای hash دقیق بسته و مقصد منتشر نمی‌شود.
- ارسالی که نتیجه‌اش معلوم نیست، خودکار دوباره فرستاده نمی‌شود؛ انسان تصمیم می‌گیرد.
- توکن‌ها فقط در `.env` هستند؛ پایگاه‌داده فقط نام متغیر را نگه می‌دارد.
