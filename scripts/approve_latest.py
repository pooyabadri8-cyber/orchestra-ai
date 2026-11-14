"""Approve the newest content that waits for approval and publish it, after the owner confirms in this window.

Only destinations whose bot token is set in .env are approved (the others would just fail). Everything is saved to
last_publish.txt so the result can be read without copying anything out of the console.
"""
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_ask import ROOT, run  # noqa: E402

LOG = ROOT / "last_publish.txt"


def env_values() -> dict:
    values = dict(os.environ)
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values.setdefault(k.strip(), v.strip())
    return values


def main() -> int:
    env = env_values()
    url = env.get("ORCHESTRA_DATABASE_URL", "")
    if not url.startswith("sqlite:///"):
        print("این راه‌انداز فقط با پایگاه‌دادهٔ SQLite کار می‌کند؛ از دستور approve استفاده کنید.")
        return 1
    db = sqlite3.connect(ROOT / url[len("sqlite:///"):])
    row = db.execute("select id, detail from runs where status='awaiting_approval' order by created_at desc limit 1").fetchone()
    if row is None:
        print("هیچ محتوایی منتظر تأیید نیست. اول run_ask.bat را اجرا کنید.")
        return 0
    run_id = row[0]
    dests = db.execute("select id, platform, secret_env from destinations where enabled=1").fetchall()
    ready = [d for d, _, secret in dests if env.get(secret)]
    skipped = [d for d, _, secret in dests if not env.get(secret)]
    if not ready:
        print("برای هیچ مقصدی توکن در .env نیست؛ چیزی منتشر نمی‌شود.")
        return 1
    with open(LOG, "w", encoding="utf-8") as log:
        log.write(f"اجرا: {run_id}\n\n")
        run(["show", run_id], log)
        print(f"\nمنتشر می‌شود در: {', '.join(ready)}" + (f"   (بدون توکن، منتشر نمی‌شود: {', '.join(skipped)})" if skipped else ""))
        answer = input("برای تأیید و انتشار، حرف y را بنویسید و Enter بزنید (هر چیز دیگر = لغو): ").strip().lower()
        if answer not in ("y", "yes", "بله", "ب"):
            log.write("\nلغو شد؛ چیزی منتشر نشد.\n")
            print("لغو شد؛ چیزی منتشر نشد.")
            return 0
        log.write("\n\n======== approve ========\n")
        out = run(["approve", run_id, "--only", *ready], log)
        if not re.search(r'"approved"', out):
            print("\nتأیید ثبت نشد؛ جزئیات در last_publish.txt است.")
            return 1
        log.write("\n\n======== publish ========\n")
        run(["publish"], log)
        log.write("\n\n======== jobs ========\n")
        run(["jobs"], log)
    print("\nنتیجه در فایل last_publish.txt ذخیره شد.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
