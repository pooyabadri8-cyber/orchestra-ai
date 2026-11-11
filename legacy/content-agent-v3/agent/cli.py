"""Command line of the product: run / check / backup / restore one customer.

    python main.py --customer <name>            run the bot of customers/<name>
    python main.py --customer <name> check      configuration + files + database, offline (exit 0 = healthy)
    python main.py --customer <name> check --live   also: the heartbeat of the running bot is fresh
    python main.py --customer <name> backup     data/ + settings -> backups/<customer>-<time>.tar.gz (never the secrets)
    python main.py --customer <name> restore FILE
    python main.py --customer <name> dashboard [--port 8787]   read-only dashboard of ONE customer (DASHBOARD_TOKEN in its secrets.env)
    python main.py admin-dashboard [--port 8786]               overview of all customers from their published stats (ADMIN_TOKEN)

A customer is a folder (customers/<name>) holding its own settings, secrets.env, data/ and logs/; the code is shared.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HEARTBEAT = "data/heartbeat.json"
HEARTBEAT_STALE = 300  # seconds without a heartbeat = the bot is considered down
SECRET_FILES = {"secrets.env", ".env"}


def resolve_customer(name: str) -> Path:
    p = Path(name)
    cand = p if p.is_dir() and (p / "config.yaml").is_file() else REPO / "customers" / name
    if not (cand / "config.yaml").is_file():
        have = sorted(d.name for d in (REPO / "customers").iterdir() if (d / "config.yaml").is_file()) if (REPO / "customers").is_dir() else []
        raise SystemExit(f"مشتری «{name}» پیدا نشد (config.yaml نیست). مشتری‌های موجود: {', '.join(have) or '—'}")
    return cand.resolve()


# ---------------------------------------------------------------- check


def run_checks(live: bool = False) -> list[tuple[str, bool, str]]:
    """Offline health check of the customer in the current directory. Returns (what, ok, detail) rows."""
    from .config import load_config
    from .knowledge import load_knowledge
    from .sources import load_sources
    from .store import Store
    from .template import load_template

    rows: list[tuple[str, bool, str]] = []

    def step(what: str, fn):
        try:
            detail = fn()
            rows.append((what, True, str(detail or "")))
            return True
        except SystemExit as e:  # the loaders report problems this way
            rows.append((what, False, str(e)))
        except Exception as e:  # noqa: BLE001
            rows.append((what, False, f"{type(e).__name__}: {e}"))
        return False

    box: dict = {}
    if not step("تنظیمات و secrets.env", lambda: box.setdefault("cfg", load_config("config.yaml")) and f"پلتفرم {box['cfg'].platform}، کانال {box['cfg'].channel}"):
        return rows
    cfg = box["cfg"]
    def perms():
        for name in ("secrets.env", ".env"):
            f = Path(name)
            if f.is_file():
                mode = f.stat().st_mode & 0o777
                if mode & 0o077:
                    raise RuntimeError(f"{name} برای دیگران قابل‌خواندن است (دسترسی {oct(mode)[2:]})؛ بزنید: chmod 600 {name}")
        return "فقط مالک"
    step("دسترسی فایل secrets.env", perms)
    step("پروفایل مشتری", lambda: f"{cfg.profile.name}؛ ماژول‌ها: " + (", ".join(k for k, v in cfg.profile.features.items() if v) or "هیچ"))
    step("پایگاه دانش", lambda: f"{len(load_knowledge(cfg.knowledge_dir)):,} کاراکتر")
    step("قالب پست", lambda: load_template(cfg.template_file).brand_hashtag)
    step("منابع", lambda: f"{len(load_sources(cfg.sources_file))} منبع")
    if cfg.profile.feature("content_calendar"):
        def cal():
            from .planner import load_settings
            st = load_settings("calendar.yaml")
            if not st.entries:
                raise RuntimeError("calendar.yaml نیست یا برنامه‌ای در آن تعریف نشده، ولی content_calendar در پروفایل روشن است.")
            return f"{len(st.entries)} مورد در برنامهٔ هفتگی"
        step("برنامهٔ هفتگی (calendar.yaml)", cal)
    step("سطح تأیید", lambda: f"{cfg.approval if cfg.auto_publish else 'review'}؛ تلاش انتشار تا {cfg.max_publish_attempts} بار")
    def db():
        Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
        s = Store(cfg.db_path)
        return f"{s.count_scheduled()} پست در صف"
    step("پایگاه‌داده", db)
    def writable():
        d = Path(cfg.db_path).parent
        with tempfile.NamedTemporaryFile(dir=d):
            return str(d)
    step("پوشهٔ data قابل‌نوشتن", writable)
    if live:
        step("ضربان ربات (heartbeat)", heartbeat_status)
    return rows


def heartbeat_status() -> str:
    p = Path(HEARTBEAT)
    if not p.is_file():
        raise RuntimeError("فایل heartbeat نیست؛ ربات تا حالا اجرا نشده یا خاموش است.")
    age = time.time() - json.loads(p.read_text(encoding="utf-8")).get("ts", 0)
    if age > HEARTBEAT_STALE:
        raise RuntimeError(f"آخرین ضربان {int(age // 60)} دقیقه پیش بوده؛ ربات متوقف شده است.")
    return f"آخرین ضربان {int(age)} ثانیه پیش"


def write_heartbeat(store=None, extra: dict | None = None) -> None:
    """Called every minute by the running bot (agent/bot.py)."""
    data = {"ts": time.time(), "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "pid": os.getpid()}
    if store is not None:
        data["queue"] = store.count_scheduled()
    data.update(extra or {})
    p = Path(HEARTBEAT)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------- backup / restore


def backup(customer_dir: Path, out_dir: Path | None = None) -> Path:
    """data/ (SQLite copied consistently) + every settings file + knowledge/. Secrets are NOT included."""
    out_dir = out_dir or REPO / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{customer_dir.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(out, "w:gz") as tar:
        for f in sorted(customer_dir.rglob("*")):
            rel = f.relative_to(customer_dir)
            if not f.is_file() or rel.parts[0] in ("logs", "__pycache__") or f.name in SECRET_FILES or f.suffix == ".pyc":
                continue
            if f.suffix == ".db":  # a live database: take a consistent snapshot through SQLite itself
                snap = Path(tmp) / f.name
                src, dst = sqlite3.connect(f), sqlite3.connect(snap)
                with dst:
                    src.backup(dst)
                src.close(); dst.close()
                tar.add(snap, arcname=str(rel))
            elif rel.parts[0] == "data" and f.name == "heartbeat.json":
                continue
            else:
                tar.add(f, arcname=str(rel))
    return out


def restore(customer_dir: Path, archive: Path) -> int:
    """Unpack a backup into the customer folder (existing files are overwritten; secrets.env is never touched)."""
    n = 0
    with tarfile.open(archive) as tar:
        for m in tar.getmembers():
            target = (customer_dir / m.name).resolve()
            if not m.isfile() or customer_dir.resolve() not in target.parents or Path(m.name).name in SECRET_FILES:
                continue  # also blocks ../ paths inside a tampered archive
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as src, open(target, "wb") as dst:
                dst.write(src.read())
            n += 1
    return n


# ---------------------------------------------------------------- entry


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="main.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--customer", "-c", default=os.environ.get("CUSTOMER", ""), help="نام پوشه در customers/ یا مسیر کامل")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "check", "backup", "restore", "dashboard", "admin-dashboard"])
    ap.add_argument("file", nargs="?", help="restore: فایل پشتیبان")
    ap.add_argument("--live", action="store_true", help="check: ضربان ربات در حال اجرا را هم بسنج")
    ap.add_argument("--host", default="127.0.0.1", help="dashboard: آدرس شنود (پیش‌فرض فقط همین سرور)")
    ap.add_argument("--port", type=int, default=0, help="dashboard: پورت (پیش‌فرض ۸۷۸۷؛ نمای مدیر ۸۷۸۶)")
    a = ap.parse_args(argv)
    if a.command == "admin-dashboard":  # needs no customer: it only reads the published snapshots
        from . import dashboard, stats
        dashboard.serve(dashboard.admin_server(stats.default_stats_dir(), a.host, a.port or 8786))
        return 0
    if a.customer:
        folder = resolve_customer(a.customer)
        os.chdir(folder)  # everything the bot reads or writes (settings, secrets, data/, logs) is relative to this folder
    elif not Path("config.yaml").is_file():
        raise SystemExit("مشتری مشخص نشده: python main.py --customer <نام> (پوشه‌های customers/ را ببینید).")
    folder = Path.cwd()
    if a.command == "run":
        from .bot import main as run_bot
        run_bot()
        return 0
    if a.command == "dashboard":
        from . import dashboard
        dashboard.serve(dashboard.customer_server(folder, a.host, a.port or 8787))
        return 0
    if a.command == "check":
        rows = run_checks(a.live)
        for what, ok, detail in rows:
            print(f"{'✅' if ok else '❌'} {what}" + (f" — {detail}" if detail else ""))
        return 0 if all(ok for _, ok, _ in rows) else 1
    if a.command == "backup":
        print(backup(folder))
        return 0
    if not a.file:
        raise SystemExit("restore: فایل پشتیبان را بدهید.")
    print(f"{restore(folder, Path(a.file))} فایل بازیابی شد.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
