"""Read-only dashboards, standard library only.

* customer dashboard  (`main.py --customer X dashboard`): one customer's numbers, from ITS OWN folder, read-only. Token: DASHBOARD_TOKEN
  in that customer's secrets.env. It cannot see any other customer.
* admin overview      (`main.py admin-dashboard`): one card per customer, read from the snapshot files the bots publish into
  STATS_DIR (numbers + headlines only). It never opens a customer's folder, database or secrets. Token: ADMIN_TOKEN.

Both refuse to start without a token, bind to 127.0.0.1 by default (put HTTPS in front: Caddy / nginx), compare tokens in
constant time, send no-store + strict security headers, and escape every piece of text they print.
"""
from __future__ import annotations

import hmac
import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from . import stats

log = logging.getLogger("content-agent.dashboard")
COOKIE = "ca_dash"
MIN_TOKEN = 16
STATUS_FA = {"draft": "پیش‌نویس", "scheduled": "در صف", "published": "منتشر شد", "rejected": "ردشده", "deleted": "حذف‌شده",
             "failed": "ناموفق", "dead": "صف خطا", "unconfirmed": "نامعلوم", "sending": "در حال ارسال"}
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2430;--mute:#667085;--line:#e4e7ec;--ok:#0f7b4b;--warn:#b45309;--bad:#b42318;--bar:#3b6fd4}
@media (prefers-color-scheme:dark){:root{--bg:#0f141b;--card:#171e28;--ink:#e6eaf0;--mute:#98a2b3;--line:#273142;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--bar:#6c9bff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.7 Vazirmatn,Tahoma,system-ui,sans-serif}
main{max-width:1000px;margin:0 auto;padding:20px 16px 48px}h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.sub{color:var(--mute);font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}.n{font-size:26px;font-weight:700;line-height:1.2}
.l{color:var(--mute);font-size:13px}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{padding:8px 12px;text-align:right;border-bottom:1px solid var(--line);font-size:14px;vertical-align:top}th{color:var(--mute);font-weight:500;font-size:13px}
tr:last-child td{border-bottom:0}.bars{display:flex;align-items:flex-end;gap:6px;height:110px;padding:10px 12px 0;background:var(--card);border:1px solid var(--line);border-radius:12px}
.bar{flex:1;background:var(--bar);border-radius:4px 4px 0 0;min-height:2px;position:relative}.bar span{position:absolute;top:-18px;width:100%;text-align:center;font-size:11px;color:var(--mute)}
.dates{display:flex;gap:6px;padding:4px 12px;color:var(--mute);font-size:11px}.dates i{flex:1;text-align:center;font-style:normal}
.pill{display:inline-block;padding:1px 9px;border-radius:99px;border:1px solid var(--line);font-size:12px}
"""


def e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def pct(x) -> str:
    return "—" if x is None else f"{round(x * 100)}٪"


def fa(n) -> str:
    return str(n).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def page(title: str, body: str) -> bytes:
    return (f'<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex"><title>{e(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>').encode()


def card(value, label, cls="") -> str:
    return f'<div class="card"><div class="n {cls}">{e(value)}</div><div class="l">{e(label)}</div></div>'


def hb_state(age: float | None) -> tuple[str, str]:
    if age is None:
        return "بدون ضربان", "bad"
    if age > stats.HEARTBEAT_STALE:
        return f"متوقف ({fa(int(age // 60))} دقیقه پیش)", "bad"
    return "در حال کار", "ok"


def bars(per_day: list[dict]) -> str:
    top = max([d["published"] for d in per_day] + [1])
    cells = "".join(f'<div class="bar" style="height:{max(2, round(d["published"] / top * 88))}px" title="{e(d["date"])}"><span>{fa(d["published"])}</span></div>' for d in per_day)
    dates = "".join(f"<i>{fa(d['date'][8:])}</i>" for d in per_day)
    return f'<div class="bars">{cells}</div><div class="dates">{dates}</div>'


def post_table(rows: list[dict], tz: ZoneInfo, with_error: bool = False) -> str:
    if not rows:
        return '<div class="card l">موردی نیست.</div>'
    out = ['<table><tr><th>#</th><th>وضعیت</th><th>تیتر</th><th>زمان</th>' + ("<th>علت</th>" if with_error else "") + "</tr>"]
    for r in rows:
        try:
            at = fa(datetime.fromisoformat(r["at"]).astimezone(tz).strftime("%m/%d %H:%M")) if r["at"] else ""
        except ValueError:
            at = ""
        cls = {"published": "ok", "dead": "bad", "failed": "bad", "unconfirmed": "warn"}.get(r["status"], "")
        out.append(f'<tr><td>{fa(r["id"])}</td><td><span class="pill {cls}">{e(STATUS_FA.get(r["status"], r["status"]))}</span></td>'
                   f'<td>{e(r["headline"])}</td><td>{at}</td>' + (f'<td class="l">{e(r["error"])}</td>' if with_error else "") + "</tr>")
    return "".join(out) + "</table>"


def customer_html(snap: dict, tz_name: str = "Asia/Tehran") -> bytes:
    tz = ZoneInfo(tz_name)
    p, rv, au, rl, sr = snap["posts"], snap["review_30d"], snap["automation_30d"], snap["reliability"], snap["sources"]
    hb, hbc = hb_state(snap.get("heartbeat_age"))
    parked_n = rl["dead"] + rl["unconfirmed"] + rl["failed"]
    body = (
        f'<h1>{e(snap["name"])}</h1><div class="sub">ربات: <b class="{hbc}">{e(hb)}</b> · سطح تأیید: {e(snap["approval"])} · '
        f'پلتفرم: {e(snap["platform"])} · به‌روزرسانی: {fa(snap["generated_at"][:16].replace("T", " "))} UTC</div>'
        '<div class="grid">'
        + card(fa(p["published_today"]), "منتشرشده امروز") + card(fa(p["published_7d"]), "۷ روز اخیر") + card(fa(p["published_30d"]), "۳۰ روز اخیر")
        + card(fa(p["queue"]), "در صف انتشار") + card(fa(p["drafts"]), "پیش‌نویس منتظر تأیید")
        + card(fa(parked_n), "گیرکرده (خطا/نامعلوم)", "bad" if parked_n else "ok")
        + "</div><h2>انتشار در ۱۴ روز اخیر</h2>" + bars(snap["per_day"])
        + "<h2>کیفیت (۳۰ روز اخیر)</h2><div class=\"grid\">"
        + card(pct(rv["approval_rate"]), "تأییدِ بدون ویرایش (از پست‌های بررسی‌شده)") + card(fa(rv["approved_after_edit"]), "تأیید بعد از ویرایش")
        + card(fa(rv["rejected"]), "ردشده") + card(pct(au["auto_share"]), "سهم انتشار خودکار") + card(fa(au["held"]), "نگه‌داشته‌شده توسط بررسی ایمنی", "warn" if au["held"] else "")
        + card(fa(rl["retries_30d"]), "تلاش مجدد انتشار") + card(pct(sr["skip_rate"]), "مطالب رد‌شده توسط مدل")
        + "</div><h2>گیرکرده‌ها</h2>" + post_table(snap["parked"], tz, True)
        + "<h2>آخرین پست‌ها</h2>" + post_table(snap["recent"], tz)
    )
    return page(f'{snap["name"]} — داشبورد', body)


def admin_html(snaps: list[dict]) -> bytes:
    now = datetime.now(timezone.utc)
    rows = []
    for s in sorted(snaps, key=lambda x: x.get("customer", "")):
        age = (now - datetime.fromisoformat(s["generated_at"])).total_seconds()
        stale = age > stats.STALE_AFTER
        hb, hbc = ("آمار قدیمی", "bad") if stale else hb_state(s.get("heartbeat_age"))
        p, rl, rv, au = s["posts"], s["reliability"], s["review_30d"], s["automation_30d"]
        parked = rl["dead"] + rl["unconfirmed"] + rl["failed"]
        rows.append(
            f'<tr><td><b>{e(s["name"])}</b><div class="l">{e(s["customer"])}</div></td><td class="{hbc}">{e(hb)}</td><td>{e(s["approval"])}</td>'
            f'<td>{fa(p["published_today"])} / {fa(p["published_7d"])}</td><td>{fa(p["queue"])}</td>'
            f'<td class="{"bad" if parked else ""}">{fa(parked)}</td><td>{pct(rv["approval_rate"])}</td><td>{pct(au["auto_share"])}</td></tr>')
    table = ('<table><tr><th>مشتری</th><th>ربات</th><th>سطح تأیید</th><th>امروز / ۷ روز</th><th>صف</th><th>گیرکرده</th>'
             '<th>تأیید بدون ویرایش</th><th>خودکار</th></tr>' + "".join(rows) + "</table>") if rows else '<div class="card l">هنوز هیچ مشتری آمار نفرستاده است.</div>'
    return page("نمای مدیر", f'<h1>نمای مدیر</h1><div class="sub">{fa(len(snaps))} مشتری · فقط عددها؛ این صفحه به پوشه، پایگاه‌داده یا secrets مشتری‌ها دسترسی ندارد.</div><h2>مشتری‌ها</h2>{table}')


# ---------------------------------------------------------------- server


def valid_token(token: str) -> str:
    if len(token or "") < MIN_TOKEN:
        raise SystemExit(f"توکن داشبورد باید دست‌کم {MIN_TOKEN} کاراکتر باشد (مثلاً: python -c \"import secrets;print(secrets.token_urlsafe(24))\").")
    return token


def make_server(host: str, port: int, token: str, provider, renderer, api=None) -> ThreadingHTTPServer:
    """`provider()` -> data (called per request); `renderer(data)` -> html bytes; `api(data)` -> JSON-able."""
    token = valid_token(token)

    class H(BaseHTTPRequestHandler):
        server_version = "ContentAgentDashboard"

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8", extra: dict | None = None) -> None:
            self.send_response(code)
            for k, v in {"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                         "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                         "Content-Security-Policy": CSP, **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            auth = self.headers.get("Authorization", "")
            given = auth[7:] if auth.startswith("Bearer ") else ""
            if not given:
                c = SimpleCookie(self.headers.get("Cookie", ""))
                given = c[COOKIE].value if COOKIE in c else ""
            return bool(given) and hmac.compare_digest(given.encode(), token.encode())

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            key = (parse_qs(u.query).get("key") or [""])[0]
            if key:  # first visit with ?key=: set a cookie and drop the token from the address bar and history
                if hmac.compare_digest(key.encode(), token.encode()):
                    return self._send(303, b"", extra={"Location": u.path, "Set-Cookie": f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"})
                return self._send(403, page("دسترسی", "<h1>دسترسی ندارید</h1>"))
            if not self._authed():
                return self._send(403, page("دسترسی", "<h1>دسترسی ندارید</h1><p class='sub'>آدرس را با ?key=توکن باز کنید.</p>"))
            if u.path == "/":
                return self._send(200, renderer(provider()))
            if u.path == "/api/stats" and api:
                return self._send(200, json.dumps(api(provider()), ensure_ascii=False).encode(), "application/json")
            self._send(404, page("پیدا نشد", "<h1>پیدا نشد</h1>"))

        do_HEAD = do_GET

    return ThreadingHTTPServer((host, port), H)


def customer_server(customer_dir: Path, host: str, port: int) -> ThreadingHTTPServer:
    """Dashboard of ONE customer. The token comes from that customer's secrets.env (read as text; nothing is exported to the environment)."""
    cust = Path(customer_dir).resolve()
    token = read_env_value(cust / "secrets.env", "DASHBOARD_TOKEN") or read_env_value(cust / ".env", "DASHBOARD_TOKEN")
    if not token:
        raise SystemExit("DASHBOARD_TOKEN در secrets.env این مشتری نیست؛ داشبورد بدون توکن راه نمی‌افتد.")
    import yaml
    from .profile import load_profile
    conf = yaml.safe_load((cust / "config.yaml").read_text(encoding="utf-8")) or {}
    prof = load_profile(cust / str(conf.get("profile_file", "profile.yaml")))
    tz = str(conf.get("timezone", "Asia/Tehran"))
    db_path = cust / str(conf.get("db_path", "data/agent.db"))
    approval = str(conf.get("approval") or ("auto" if conf.get("auto_publish") else "review"))

    def provider() -> dict:
        db = stats.read_only(db_path)
        try:
            s = stats.compute(db, tz)
        finally:
            db.close()
        return {"customer": cust.name, "name": prof.name, "platform": os.environ.get("PLATFORM", "") or str(conf.get("platform", "telegram")),
                "approval": approval, "heartbeat_age": stats.heartbeat_age(cust / "data" / "heartbeat.json"), **s}

    return make_server(host, port, token, provider, lambda d: customer_html(d, tz), api=lambda d: d)


def admin_server(stats_dir: Path, host: str, port: int, token: str | None = None) -> ThreadingHTTPServer:
    token = token or os.environ.get("ADMIN_TOKEN", "")
    if not token:
        raise SystemExit("ADMIN_TOKEN تنظیم نشده؛ نمای مدیر بدون توکن راه نمی‌افتد.")
    sd = Path(stats_dir)

    def provider() -> list[dict]:
        out = []
        for f in sorted(sd.glob("*.json")):
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}\.json", f.name):
                continue
            try:
                s = json.loads(f.read_text(encoding="utf-8"))
                datetime.fromisoformat(s["generated_at"])
                out.append(s)
            except (OSError, ValueError, KeyError):
                continue  # a half-written or foreign file is ignored, never shown
        return out

    return make_server(host, port, token, provider, admin_html, api=lambda d: [{k: v for k, v in s.items() if k not in ("recent", "parked")} for s in d])


def read_env_value(path: Path, key: str) -> str:
    """One value from an env file, parsed as text (so a customer's secrets never enter this process's environment)."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", line)
            if m:
                return m.group(1).strip("'\"")
    except OSError:
        pass
    return ""


def serve(server: ThreadingHTTPServer) -> None:
    host, port = server.server_address[:2]
    print(f"داشبورد: http://{host}:{port}/?key=<توکن>  (برای دسترسی بیرونی، HTTPS جلوی آن بگذارید)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
