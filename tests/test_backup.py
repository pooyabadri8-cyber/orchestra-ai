"""Backups: a consistent, checked copy of the databases, images and settings; restoring puts it back and keeps the
current files aside; the nightly run in the control bot's loop."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import Clock
from orchestra.backup import BackupError, NightlyBackup, backups, prune, restore, run_backup, sqlite_path, verify
from orchestra.db import Base, ControlState, Lead, Run, make_engine, make_sessionmaker

NOW = datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)  # 23:30 in Tehran


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "project"
    (p / "data").mkdir(parents=True)
    for url in ("sqlite:///data/app.db", "sqlite:///data/dbos.sqlite"):
        engine = make_engine(f"sqlite:///{p / sqlite_path(url)}")
        Base.metadata.create_all(engine)
        engine.dispose()
    sessions = make_sessionmaker(make_engine(f"sqlite:///{p / 'data' / 'app.db'}"))
    with sessions.begin() as s:
        s.add(Run(id="r1", org_id="pouyan", template="market_report", status="approved"))
        s.add(Lead(org_id="pouyan", channel="bale", user_id="5", fields={"company": "صنایع نمونه"}, stage="new"))
    put_asset(p, b"slide-one")
    (p / "orgs" / "pouyan").mkdir(parents=True)
    (p / "orgs" / "pouyan" / "brand.yaml").write_text("org_id: pouyan\n", encoding="utf-8")
    (p / ".env").write_text("ORCHESTRA_CONTROL_BOT_TOKEN=secret\n", encoding="utf-8")
    return p, sessions


def put_asset(project: Path, data: bytes) -> Path:
    sha = hashlib.sha256(data).hexdigest()
    f = project / "assets_store" / sha[:2] / sha
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


def kw(project: Path, backup_dir: Path, **extra) -> dict:
    return {"project": project, "backup_dir": backup_dir, "database_url": "sqlite:///data/app.db",
            "system_database_url": "sqlite:///data/dbos.sqlite", "asset_dir": Path("assets_store"),
            "orgs_dir": Path("orgs"), **extra}


def test_a_backup_is_complete_checked_and_without_secrets(project, tmp_path):
    p, _ = project
    res = run_backup(now=NOW, **kw(p, tmp_path / "bk"))
    assert res.folder.name == "2026-11-25_2000" and set(res.files) == {"app.db", "dbos.sqlite", "orgs.zip"}
    assert res.counts["runs"] == 1 and res.counts["leads"] == 1 and res.new_assets == 1
    manifest = verify(res.folder)
    assert manifest["files"]["app.db"]["sha256"] and not (res.folder / "env.txt").exists()
    assert any("همان دیسک" in w for w in res.warnings)  # the test's backup folder is on the same disk
    res2 = run_backup(now=NOW + timedelta(days=1), **kw(p, tmp_path / "bk", include_env=True))
    assert res2.new_assets == 0 and (res2.folder / "env.txt").read_text(encoding="utf-8").startswith("ORCHESTRA_CONTROL")


def test_the_copy_is_consistent_while_someone_is_writing(project, tmp_path):
    p, _ = project
    con = sqlite3.connect(p / "data" / "app.db")
    con.execute("BEGIN")
    con.execute("INSERT INTO runs (id, org_id, template, status, findings, detail, created_at, updated_at) "
                "VALUES ('r2', 'pouyan', 't', 'running', '[]', '{}', '2026-11-25', '2026-11-25')")
    try:
        res = run_backup(now=NOW, **kw(p, tmp_path / "bk"))
    finally:
        con.rollback()
        con.close()
    assert res.counts["runs"] == 1  # the uncommitted row is not in the copy, and the copy is whole


def test_a_damaged_image_stops_the_backup_and_leaves_nothing_half_made(project, tmp_path):
    p, _ = project
    put_asset(p, b"slide-two").write_bytes(b"changed on disk")
    with pytest.raises(BackupError):
        run_backup(now=NOW, **kw(p, tmp_path / "bk"))
    assert backups(tmp_path / "bk") == [] and not list((tmp_path / "bk").glob("*.part"))


def test_old_backups_are_removed_but_the_newest_is_kept(project, tmp_path):
    p, _ = project
    for days_ago in (20, 15, 3, 0):
        run_backup(now=NOW - timedelta(days=days_ago), **kw(p, tmp_path / "bk", keep_days=14))
    assert [d.name for d in backups(tmp_path / "bk")] == ["2026-11-22_2000", "2026-11-25_2000"]
    assert prune(tmp_path / "bk", NOW + timedelta(days=100), 14) == ["2026-11-22_2000"]  # long after: only the newest stays
    assert [d.name for d in backups(tmp_path / "bk")] == ["2026-11-25_2000"]


def test_restore_round_trip_and_tampering(project, tmp_path):
    p, sessions = project
    res = run_backup(now=NOW, **kw(p, tmp_path / "bk"))
    with sessions.begin() as s:  # something goes wrong after the backup
        s.query(Lead).delete()
        s.add(Run(id="r9", org_id="pouyan", template="t", status="failed"))
    sessions.kw["bind"].dispose()
    images = [f for f in (p / "assets_store").rglob("*") if f.is_file()]
    for f in images:
        f.unlink()
    done = restore(res.folder, project=p, database_url="sqlite:///data/app.db", system_database_url="sqlite:///data/dbos.sqlite",
                   asset_dir=Path("assets_store"), now=NOW + timedelta(hours=1))
    assert any("app.db" in d for d in done) and any("orgs.zip" in d for d in done)
    fresh = make_sessionmaker(make_engine(f"sqlite:///{p / 'data' / 'app.db'}"))
    with fresh() as s:
        assert s.query(Lead).count() == 1 and s.get(Run, "r9") is None
    assert list((p / "data").glob("app.db.before-restore-*"))  # the current database was kept aside
    assert all(f.is_file() for f in images)  # the deleted images are back
    data = (res.folder / "app.db").read_bytes()
    (res.folder / "app.db").write_bytes(data[:-10] + b"x" * 10)
    with pytest.raises(BackupError):
        restore(res.folder, project=p, database_url="sqlite:///data/app.db", system_database_url="sqlite:///data/dbos.sqlite",
                asset_dir=Path("assets_store"), now=NOW)


def test_postgres_is_said_not_silently_skipped(project, tmp_path):
    p, _ = project
    res = run_backup(now=NOW, **{**kw(p, tmp_path / "bk"), "database_url": "postgresql+psycopg://a@localhost/x"})
    assert "app.db" not in res.files and any("pg_dump" in w for w in res.warnings)


def test_nightly_schedule(project, tmp_path):
    p, sessions = project
    told = []
    clock = Clock(datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc))  # 13:30 in Tehran, no backup yet
    nightly = NightlyBackup(sessions=sessions, clock=clock, at="23:30", tell=told.append, **kw(p, tmp_path / "bk"))
    nightly.tick()  # never backed up: at once
    nightly.tick()
    assert len(backups(tmp_path / "bk")) == 1 and len(told) == 1 and "همان دیسک" in told[0]  # said once
    clock.now = datetime(2026, 11, 25, 19, 59, tzinfo=timezone.utc)  # 23:29
    nightly.tick()
    assert len(backups(tmp_path / "bk")) == 1
    clock.now = datetime(2026, 11, 25, 20, 1, tzinfo=timezone.utc)  # 23:31
    nightly.tick()
    nightly.tick()
    assert len(backups(tmp_path / "bk")) == 2 and len(told) == 1  # the same-disk warning is not repeated
    clock.now = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)  # the computer was off last night
    nightly.tick()
    assert len(backups(tmp_path / "bk")) == 3
    with sessions() as s:
        assert s.get(ControlState, "backup:last").value == clock.now.isoformat()


def test_a_failing_nightly_backup_is_told_once_a_day(project, tmp_path):
    p, sessions = project
    put_asset(p, b"x").write_bytes(b"damaged")
    told = []
    clock = Clock(NOW)
    nightly = NightlyBackup(sessions=sessions, clock=clock, at="23:30", tell=told.append, **kw(p, tmp_path / "bk"))
    nightly.tick()
    nightly.tick()
    assert len(told) == 1 and "انجام نشد" in told[0]


def test_cli_backup_and_restore(project, tmp_path, monkeypatch, capsys):
    from orchestra.cli import main
    p, sessions = project
    sessions.kw["bind"].dispose()
    monkeypatch.chdir(p)
    for k, v in {"ORCHESTRA_DATABASE_URL": "sqlite:///data/app.db", "ORCHESTRA_SYSTEM_DATABASE_URL": "sqlite:///data/dbos.sqlite",
                 "ORCHESTRA_BACKUP_DIR": str(tmp_path / "bk"), "ORCHESTRA_ORGS_DIR": "orgs"}.items():
        monkeypatch.setenv(k, v)
    main(["backup"])
    assert "پشتیبان گرفته شد" in capsys.readouterr().out
    main(["restore"])
    name = backups(tmp_path / "bk")[0].name
    assert name in capsys.readouterr().out
    main(["restore", name, "--yes"])
    out = capsys.readouterr().out
    assert "✅ app.db" in out and json.loads((tmp_path / "bk" / name / "manifest.json").read_text(encoding="utf-8"))["counts"]["runs"] == 1
