"""Backups: the databases, the images and the organization's settings, copied, checked and kept for a while.

One backup is a folder <backup dir>/<YYYY-MM-DD_HHMM>/ holding
    app.db, dbos.sqlite      the databases, copied with SQLite's own backup API (consistent while the bot writes)
    orgs.zip                 orgs/ (brand, knowledge base, calendar, CRM, schedule ...)
    env.txt                  .env, only when backup_include_env is on (it holds tokens and keys)
    manifest.json            every file's size and SHA-256, the row counts of the main tables, when and from where
Images are content-addressed (named by their hash), so they go into one shared mirror <backup dir>/assets_store/ and
each backup copies only the new ones.

A backup is checked before it counts: SQLite's integrity check on every copied database, and the manifest's hashes.
Restoring checks the same again first and keeps the current files aside, so a restore can itself be undone.
Postgres is not backed up here (use pg_dump); the backup says so instead of pretending.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

STAMP = "%Y-%m-%d_%H%M"
TABLES = ("runs", "packages", "approvals", "publish_jobs", "audit_events", "leads", "content_plans", "lessons")


class BackupError(Exception):
    pass


def sqlite_path(url: str) -> Path | None:
    """The file of a sqlite:/// URL (relative to the working folder), or None for any other database."""
    if not url.startswith("sqlite"):
        return None
    path = urlparse(url.replace("sqlite+pysqlite", "sqlite")).path[1:]  # sqlite:///rel -> rel; sqlite:////abs -> /abs
    return Path(path) if path and path != ":memory:" else None


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_sqlite(src: Path, dst: Path) -> None:
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")  # one self-contained file: no -wal/-shm beside the copy
    finally:
        target.close()
        source.close()
    check(dst)


def check(db: Path) -> None:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    if result != "ok":
        raise BackupError(f"{db.name}: بررسی سلامت پایگاه‌داده رد شد ({result})")


def _counts(db: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES if t in have}
    finally:
        con.close()


def same_device(a: Path, b: Path) -> bool:
    """True when both folders are on the same disk: such a backup does not survive that disk failing."""
    try:
        a.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            return a.resolve().drive.lower() == b.resolve().drive.lower()
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


@dataclass
class BackupResult:
    folder: Path
    files: dict[str, int] = field(default_factory=dict)  # name -> bytes
    counts: dict[str, int] = field(default_factory=dict)
    new_assets: int = 0
    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def run_backup(*, project: Path, backup_dir: Path, database_url: str, system_database_url: str, asset_dir: Path,
               orgs_dir: Path, now: datetime, keep_days: int = 14, include_env: bool = False) -> BackupResult:
    project, backup_dir = Path(project), Path(backup_dir)
    if not backup_dir.is_absolute():
        backup_dir = project / backup_dir
    folder = backup_dir / now.strftime(STAMP)
    work = folder.with_name(folder.name + ".part")  # complete or nothing: renamed once everything is checked
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    res = BackupResult(folder=folder)
    try:
        manifest: dict = {"made_at": now.isoformat(), "project": str(project.resolve()), "files": {}}
        for name, url in (("app.db", database_url), ("dbos.sqlite", system_database_url)):
            src = sqlite_path(url)
            if src is None:
                res.warnings.append(f"{name}: پایگاه‌داده SQLite نیست؛ از آن جدا با pg_dump پشتیبان بگیرید")
                continue
            src = src if src.is_absolute() else project / src
            if not src.is_file():
                res.warnings.append(f"{name}: فایل {src} پیدا نشد")
                continue
            _copy_sqlite(src, work / name)
            if name == "app.db":
                res.counts = manifest["counts"] = _counts(work / name)
        orgs = orgs_dir if Path(orgs_dir).is_absolute() else project / orgs_dir
        if orgs.is_dir():
            with zipfile.ZipFile(work / "orgs.zip", "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(orgs.rglob("*")):
                    if f.is_file():
                        z.write(f, f.relative_to(orgs.parent).as_posix())
        env = project / ".env"
        if include_env and env.is_file():
            shutil.copy2(env, work / "env.txt")
        for f in sorted(work.iterdir()):
            manifest["files"][f.name] = {"bytes": f.stat().st_size, "sha256": _sha(f)}
        res.new_assets = _mirror_assets(asset_dir if Path(asset_dir).is_absolute() else project / asset_dir, backup_dir / "assets_store")
        manifest["new_assets"] = res.new_assets
        (work / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        if folder.exists():
            shutil.rmtree(folder)
        work.rename(folder)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    res.files = {k: v["bytes"] for k, v in manifest["files"].items()}
    res.removed = prune(backup_dir, now, keep_days)
    if same_device(backup_dir, project):
        res.warnings.append("پوشهٔ پشتیبان روی همان دیسک برنامه است؛ اگر این دیسک خراب شود، پشتیبان هم از دست می‌رود. "
                            "ORCHESTRA_BACKUP_DIR را روی دیسک یا فلش دیگر، یا پوشهٔ همگام‌شده با OneDrive/Google Drive بگذارید.")
    return res


def _mirror_assets(src: Path, dst: Path) -> int:
    """Copy the images the mirror does not have yet; each is checked against its name (its hash)."""
    if not src.is_dir():
        return 0
    n = 0
    for f in src.rglob("*"):
        if not f.is_file() or len(f.name) != 64:
            continue
        target = dst / f.relative_to(src)
        if target.exists():
            continue
        if _sha(f) != f.name:
            raise BackupError(f"تصویر {f.name[:12]} روی دیسک تغییر کرده است (خراب)؛ پشتیبان متوقف شد")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        shutil.copy2(f, tmp)
        tmp.replace(target)
        n += 1
    return n


def backups(backup_dir: Path) -> list[Path]:
    """Completed backups, oldest first."""
    if not backup_dir.is_dir():
        return []
    out = []
    for d in backup_dir.iterdir():
        try:
            datetime.strptime(d.name, STAMP)
        except ValueError:
            continue
        if d.is_dir() and (d / "manifest.json").is_file():
            out.append(d)
    return sorted(out, key=lambda d: d.name)


def prune(backup_dir: Path, now: datetime, keep_days: int) -> list[str]:
    """Remove backups older than keep_days; the newest one is always kept."""
    limit = (now - timedelta(days=keep_days)).strftime(STAMP)
    done = backups(backup_dir)
    removed = []
    for d in done[:-1]:
        if d.name < limit:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    return removed


def verify(folder: Path) -> dict:
    """Check a backup against its manifest (sizes, hashes, integrity). Returns the manifest."""
    mf = folder / "manifest.json"
    if not mf.is_file():
        raise BackupError(f"{folder} پشتیبان کامل نیست (manifest.json ندارد)")
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    for name, info in manifest["files"].items():
        f = folder / name
        if not f.is_file() or f.stat().st_size != info["bytes"] or _sha(f) != info["sha256"]:
            raise BackupError(f"{name} در پشتیبان با manifest نمی‌خواند (ناقص یا تغییرکرده)")
        if name.endswith((".db", ".sqlite")):
            check(f)
    return manifest


def restore(folder: Path, *, project: Path, database_url: str, system_database_url: str, asset_dir: Path, now: datetime) -> list[str]:
    """Put a checked backup back. The current files are kept beside the originals (*.before-restore-<time>).
    orgs.zip and env.txt are not overwritten: they are left for the owner to compare. Returns what was done."""
    folder, project = Path(folder), Path(project)
    verify(folder)
    done = []
    tag = now.strftime(STAMP)
    for name, url in (("app.db", database_url), ("dbos.sqlite", system_database_url)):
        src = folder / name
        dst = sqlite_path(url)
        if not src.is_file() or dst is None:
            continue
        dst = dst if dst.is_absolute() else project / dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            cur = dst.with_name(dst.name + suffix)
            if cur.exists():
                cur.rename(cur.with_name(f"{cur.name}.before-restore-{tag}"))
        shutil.copy2(src, dst)
        check(dst)
        done.append(f"{name} ← {folder.name}")
    mirror = folder.parent / "assets_store"
    assets = asset_dir if Path(asset_dir).is_absolute() else project / asset_dir
    if mirror.is_dir():
        n = _mirror_assets(mirror, assets)
        done.append(f"{n} تصویر برگردانده شد")
    for extra in ("orgs.zip", "env.txt"):
        if (folder / extra).is_file():
            done.append(f"{extra} دست نخورد؛ اگر لازم است، از {folder / extra} خودتان باز کنید")
    return done


class NightlyBackup:
    """Run by the control bot's loop: one backup a day at `at` (Tehran time), or at once when the last good one is
    older than a day (the computer was off at that hour). A failure is reported once a day; a success is silent
    (/status shows the last one)."""

    def __init__(self, *, sessions, clock, at: str, tell, **backup_kw):
        self.sessions, self.clock, self.tell, self.kw = sessions, clock, tell, backup_kw
        h, m = at.split(":")
        self.at = (int(h), int(m))

    def _state(self, key: str) -> str:
        from .db import ControlState
        with self.sessions() as s:
            row = s.get(ControlState, key)
            return row.value if row else ""

    def _set(self, key: str, value: str) -> None:
        from .db import ControlState
        with self.sessions.begin() as s:
            s.merge(ControlState(key=key, value=value))

    def due(self) -> bool:
        from zoneinfo import ZoneInfo
        now = self.clock().astimezone(ZoneInfo("Asia/Tehran"))
        last = self._state("backup:last")
        if not last:
            return True
        last_dt = datetime.fromisoformat(last)
        slot = now.replace(hour=self.at[0], minute=self.at[1], second=0, microsecond=0)
        return (now >= slot > last_dt) or now - last_dt > timedelta(hours=26)

    def tick(self) -> None:
        if not self.due():
            return
        now = self.clock()
        day = now.date().isoformat()
        if self._state("backup:failed") == day:
            return  # failed today already: told once; tried again tomorrow (or by hand: backup.bat)
        try:
            res = run_backup(now=now, **self.kw)
        except Exception as e:  # noqa: BLE001
            self._set("backup:failed", day)
            self.tell(f"⚠️ پشتیبان‌گیری امشب انجام نشد: {str(e)[:300]}\nبا backup.bat دوباره امتحان کنید.")
            return
        self._set("backup:last", now.isoformat())
        self._set("backup:last_folder", str(res.folder))
        if res.warnings:
            warned = self._state("backup:warned")
            if warned != "\n".join(res.warnings):  # a lasting warning (same disk) is said once, not every night
                self._set("backup:warned", "\n".join(res.warnings))
                self.tell("💾 پشتیبان گرفته شد، با این هشدار:\n" + "\n".join(f"• {w}" for w in res.warnings))
