"""Phase 5: numbers, read-only dashboards and tenant isolation (run: python tests/test_dashboard.py)."""
import http.client
import json
import os
import shutil
import sqlite3
import stat as statmod
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import agent.bot as bmod  # noqa: E402
import agent.dashboard as dash  # noqa: E402
import agent.stats as st  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.store import Store  # noqa: E402
from test_product import CUSTOMERS  # noqa: E402
from _profile import P  # noqa: E402
import test_auto as ta  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "t0ken-0123456789-abcdef-A"
TOKEN_B = "t0ken-0123456789-abcdef-B"


def seed(store: Store, tag="A"):
    """A bit of life: posts in several states and the owner's decisions."""
    now = datetime.now(timezone.utc)
    p1 = store.add_draft("t1", None, ta.good(1, f"تیتر {tag} یک"), now)       # approved unchanged, published
    store.log_event(p1.id, "approved"); store.begin_send(p1.id); store.mark_published(p1.id, 11)
    p2 = store.add_draft("t2", None, ta.good(2, f"تیتر {tag} دو"), now)       # edited, then approved, published
    store.update_text(p2.id, ta.good(2, f"تیتر {tag} دو ویرایش")); store.log_event(p2.id, "approved"); store.begin_send(p2.id); store.mark_published(p2.id, 12)
    p3 = store.add_draft("t3", None, ta.good(3, f"تیتر {tag} سه"), now)       # rejected
    store.reject(p3.id)
    p4 = store.add_draft("t4", None, ta.good(4, f"تیتر {tag} چهار"), now)     # auto, published
    store.log_event(p4.id, "auto"); store.begin_send(p4.id); store.mark_published(p4.id, 13)
    p5 = store.add_draft("t5", None, ta.good(5, f"تیتر {tag} پنج x&y"), now)  # held, then dead
    store.log_event(p5.id, "held"); store.begin_send(p5.id); store.mark_dead(p5.id, "قطع <img src=x onerror=1>")
    p6 = store.add_draft("t6", None, ta.good(6, f"تیتر {tag} شش"), now)       # waiting in the queue
    store.schedule(p6.id, now + timedelta(hours=1))
    store.mark_seen("k1", "S", "t", "u", "drafted"); store.mark_seen("k2", "S", "t", "u", "skipped")
    return [p1, p2, p3, p4, p5, p6]


def test_numbers_are_counted_from_the_event_log():
    s = Store(":memory:")
    seed(s)
    n = st.compute(s.db)
    assert n["posts"]["published_today"] == 3 and n["posts"]["queue"] == 1 and n["posts"]["by_status"]["dead"] == 1
    rv = n["review_30d"]
    assert (rv["approved"], rv["approved_unchanged"], rv["approved_after_edit"], rv["rejected"]) == (2, 1, 1, 1)
    assert rv["approval_rate"] == round(1 / 3, 3)  # 1 unchanged of 3 decisions (2 approved + 1 rejected)
    au = n["automation_30d"]
    assert au["auto"] == 1 and au["held"] == 1 and au["auto_share"] == round(1 / 4, 3)
    assert n["reliability"]["dead"] == 1 and n["sources"]["skip_rate"] == 0.5
    assert n["per_day"][-1]["published"] == 3 and len(n["per_day"]) == 14
    assert any(r["status"] == "dead" and "قطع" in r["error"] for r in n["parked"])
    assert st.compute(Store(":memory:").db)["review_30d"]["approval_rate"] is None  # no decisions yet: no invented percentage
    old = sqlite3.connect(":memory:")  # a database from before the dashboard existed
    old.execute("CREATE TABLE posts (id INTEGER, status TEXT, text TEXT, topic TEXT, pillar TEXT, published_at TEXT, created_at TEXT, attempts INT, error TEXT)")
    assert st.compute(old)["posts"]["published_30d"] == 0


def make_customer(tag, token=TOKEN):
    d = Path(tempfile.mkdtemp()) / f"cust-{tag.lower()}"
    shutil.copytree(os.path.join(CUSTOMERS, "example-cafe"), d)
    (d / "secrets.env").write_text(f"PLATFORM=telegram\nTELEGRAM_BOT_TOKEN=999:SECRET-{tag}\nDASHBOARD_TOKEN={token}\n", encoding="utf-8")
    (d / "data").mkdir(exist_ok=True)
    s = Store(str(d / "data" / "agent.db"))
    seed(s, tag)
    s.db.close()
    return d


def get(port, path="/", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read().decode()


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def test_customer_dashboard_auth_content_and_escaping():
    d = make_customer("A")
    srv = dash.customer_server(d, "127.0.0.1", 0)
    port = serve(srv)
    try:
        assert get(port, "/healthz")[0] == 200
        assert get(port, "/")[0] == 403 and get(port, "/api/stats")[0] == 403           # no token
        assert get(port, "/?key=wrong-wrong-wrong-wrong")[0] == 403                          # wrong token
        code, h, _ = get(port, f"/?key={TOKEN}")
        assert code == 303 and h["Location"] == "/" and "HttpOnly" in h["Set-Cookie"] and "SameSite=Strict" in h["Set-Cookie"]
        cookie = h["Set-Cookie"].split(";")[0]
        code, h, body = get(port, "/", {"Cookie": cookie})
        assert code == 200 and "کافه نمونه" in body and "تیتر A یک" in body
        assert "x&amp;y" in body and "x&y" not in body and "<img src=x" not in body and "&lt;img" in body  # every text is escaped
        assert "999:SECRET-A" not in body and TOKEN not in body                               # no secrets on the page
        assert h["Cache-Control"] == "no-store" and "frame-ancestors 'none'" in h["Content-Security-Policy"] and h["X-Frame-Options"] == "DENY"
        code, _, js = get(port, "/api/stats", {"Authorization": f"Bearer {TOKEN}"})
        data = json.loads(code == 200 and js)
        assert data["posts"]["published_today"] == 3 and data["customer"] == d.name and "SECRET" not in js
        assert get(port, "/nope", {"Authorization": f"Bearer {TOKEN}"})[0] == 404
        # read-only: even the dashboard's own connection cannot change the data
        db = st.read_only(d / "data" / "agent.db")
        try:
            db.execute("INSERT INTO events (ts, kind) VALUES ('x','y')")
            raise AssertionError("must be read-only")
        except sqlite3.OperationalError as e:
            assert "readonly" in str(e).lower()
    finally:
        srv.shutdown()


def test_dashboard_refuses_to_start_without_a_real_token():
    d = make_customer("A")
    (d / "secrets.env").write_text("PLATFORM=telegram\n", encoding="utf-8")
    for fn, expect in [(lambda: dash.customer_server(d, "127.0.0.1", 0), "DASHBOARD_TOKEN")]:
        try:
            fn()
            raise AssertionError
        except SystemExit as e:
            assert expect in str(e)
    (d / "secrets.env").write_text("DASHBOARD_TOKEN=short\n", encoding="utf-8")
    try:
        dash.customer_server(d, "127.0.0.1", 0)
        raise AssertionError
    except SystemExit as e:
        assert "16" in str(e)
    keep = os.environ.pop("ADMIN_TOKEN", None)
    try:
        dash.admin_server(Path(tempfile.mkdtemp()), "127.0.0.1", 0)
        raise AssertionError
    except SystemExit as e:
        assert "ADMIN_TOKEN" in str(e)
    finally:
        if keep:
            os.environ["ADMIN_TOKEN"] = keep
    assert "DASHBOARD_TOKEN" not in os.environ  # reading the token never exports a customer's secrets into this process


def test_tenants_cannot_see_each_other():
    a, b = make_customer("A", TOKEN), make_customer("B", TOKEN_B)
    sa, sb = dash.customer_server(a, "127.0.0.1", 0), dash.customer_server(b, "127.0.0.1", 0)
    pa, pb = serve(sa), serve(sb)
    try:
        assert get(pa, "/", {"Authorization": f"Bearer {TOKEN_B}"})[0] == 403  # B's token does not open A
        assert get(pb, "/", {"Authorization": f"Bearer {TOKEN}"})[0] == 403
        _, _, body_a = get(pa, "/", {"Authorization": f"Bearer {TOKEN}"})
        _, _, body_b = get(pb, "/", {"Authorization": f"Bearer {TOKEN_B}"})
        assert "تیتر A" in body_a and "تیتر B" not in body_a and "SECRET-B" not in body_a
        assert "تیتر B" in body_b and "تیتر A" not in body_b
    finally:
        sa.shutdown(); sb.shutdown()


def _cfg(**kw):
    return Config(**{**P, "bot_token": "999:SECRET-TOKEN", "api_key": "sk-SECRET-KEY", "channel": "@c", "owner_ids": {1}, "auto_publish": True, **kw})


def test_snapshot_has_numbers_only_and_admin_overview_never_touches_customers():
    s = Store(":memory:")
    posts = seed(s)
    s.update_text(posts[5].id, ta.good(6, "تیتر شش") + "\nBODY-SENTINEL-FULL-TEXT")
    snap = st.snapshot("acme", _cfg(), s.db)
    raw = json.dumps(snap, ensure_ascii=False)
    for secret in ("SECRET-TOKEN", "SECRET-KEY", "BODY-SENTINEL", "knowledge", "اطلاعات"):
        assert secret not in raw, secret
    assert snap["customer"] == "acme" and snap["approval"] == "auto" and snap["posts"]["published_today"] == 3
    assert not any(r["headline"] for r in st.snapshot("acme", _cfg(), s.db, headlines=False)["recent"])  # headlines can be switched off
    sd = Path(tempfile.mkdtemp())
    out = st.write_snapshot(sd, "acme", snap)
    assert statmod.S_IMODE(out.stat().st_mode) == 0o640
    st.write_snapshot(sd, "old-co", {**snap, "customer": "old-co", "name": "شرکت قدیمی", "generated_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")})
    (sd / "junk.json").write_text("{not json", encoding="utf-8")
    (sd / "Evil Name.json").write_text(json.dumps(snap), encoding="utf-8")  # a name outside the allowed pattern is ignored
    srv = dash.admin_server(sd, "127.0.0.1", 0, token=TOKEN)
    port = serve(srv)
    try:
        assert get(port, "/")[0] == 403
        code, _, body = get(port, "/", {"Authorization": f"Bearer {TOKEN}"})
        assert code == 200 and P["channel_name"] in body and "شرکت قدیمی" in body and "آمار قدیمی" in body and "Evil" not in body
        _, _, js = get(port, "/api/stats", {"Authorization": f"Bearer {TOKEN}"})
        api = json.loads(js)
        assert {x["customer"] for x in api} == {"acme", "old-co"} and all("recent" not in x for x in api)  # the overview API carries no headlines
    finally:
        srv.shutdown()


def test_running_bot_publishes_its_snapshot():
    keep = os.environ.get("STATS_DIR")
    sd = tempfile.mkdtemp()
    os.environ["STATS_DIR"] = sd
    cwd = os.getcwd()
    d = Path(tempfile.mkdtemp()) / "acme-cafe"
    d.mkdir()
    os.chdir(d)
    try:
        s = Store(":memory:")
        seed(s)
        ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"store": s, "cfg": _cfg()}))
        import asyncio
        asyncio.run(bmod.heartbeat(ctx))
        f = Path(sd) / "acme-cafe.json"
        assert f.exists() and json.loads(f.read_text(encoding="utf-8"))["customer"] == "acme-cafe"
        assert (d / "data" / "heartbeat.json").exists()
        mtime = f.stat().st_mtime_ns
        asyncio.run(bmod.heartbeat(ctx))  # within 5 minutes: heartbeat yes, snapshot not rewritten
        assert f.stat().st_mtime_ns == mtime
    finally:
        os.chdir(cwd)
        if keep is None:
            os.environ.pop("STATS_DIR", None)
        else:
            os.environ["STATS_DIR"] = keep


def test_install_scripts_isolate_each_customer():
    work = Path(tempfile.mkdtemp()) / "repo"
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".venv", "data", "logs", "backups"))
    (work / "customers" / "example-cafe" / "secrets.env").write_text("X=1\n", encoding="utf-8")
    env = {**os.environ, "DRY_RUN": "1", "PATH": os.environ["PATH"]}
    out = subprocess.run(["bash", "scripts/install.sh", "example-cafe"], cwd=work, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    t = out.stdout
    assert "useradd --system --no-create-home --shell /usr/sbin/nologin ca-example-cafe" in t
    assert "chmod 700 customers/example-cafe" in t and "chmod 600 customers/example-cafe/secrets.env" in t
    assert "chown -R ca-example-cafe:ca-example-cafe customers/example-cafe" in t
    for line in ("User=ca-%i", "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "ProtectProc=invisible", "UMask=0077"):
        assert line in t, line
    assert f"ReadWritePaths={work}/customers/%i /var/lib/content-agent/stats" in t  # may write only its own folder + the stats drop
    assert "install -d -m 1733 /var/lib/content-agent/stats" in t and "setfacl -d -m u:ca-admin:r" in t
    bad = subprocess.run(["bash", "scripts/install.sh", "../etc"], cwd=work, env=env, capture_output=True, text=True)
    assert bad.returncode != 0 and "نامعتبر" in bad.stdout
    adm = subprocess.run(["bash", "scripts/install-dashboard.sh", "admin"], cwd=work, env=env, capture_output=True, text=True).stdout
    assert "User=ca-admin" in adm and f"InaccessiblePaths={work}/customers" in adm and "ReadOnlyPaths=/var/lib/content-agent/stats" in adm
    assert "--host 127.0.0.1" in adm and "EnvironmentFile=/etc/content-agent/admin.env" in adm
    cu = subprocess.run(["bash", "scripts/install-dashboard.sh", "customer", "example-cafe", "8791"], cwd=work, env=env, capture_output=True, text=True).stdout
    assert "User=ca-example-cafe" in cu and "--port 8791" in cu and f"ReadOnlyPaths={work}/customers/example-cafe" in cu


def test_check_warns_when_secrets_are_readable_by_others():
    import agent.cli as cli
    d = make_customer("A")
    keep = dict(os.environ)
    cwd = os.getcwd()
    try:
        for k in list(os.environ):
            if k.startswith(("TELEGRAM_", "BALE_", "LLM", "ANTHROPIC", "PLATFORM", "OWNER")):
                del os.environ[k]
        (d / "secrets.env").write_text("PLATFORM=telegram\nTELEGRAM_BOT_TOKEN=1:a\nTELEGRAM_CHANNEL=c\nOWNER_IDS=1\nANTHROPIC_API_KEY=k\n", encoding="utf-8")
        os.chdir(d)
        os.chmod("secrets.env", 0o644)
        rows = dict((w, (ok, det)) for w, ok, det in cli.run_checks())
        ok, det = rows["دسترسی فایل secrets.env"]
        assert not ok and "644" in det
        os.chmod("secrets.env", 0o600)
        assert dict((w, ok) for w, ok, _ in cli.run_checks())["دسترسی فایل secrets.env"]
    finally:
        os.chdir(cwd)
        os.environ.clear(); os.environ.update(keep)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
