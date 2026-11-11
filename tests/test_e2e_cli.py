"""The user's real flow, end to end, the way it runs on their computer:

a fresh copy of the project, a .env with SQLite databases and an OpenAI-compatible model address, `scripts/run_ask.py`
run as a separate process (exactly what run_ask.bat does), then show / approve / reject / publish / jobs from the
command line. The model and the market page are served by a local HTTP server, so the real network code is used.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from test_request_workflow import CLEAN, DRAFT, PLAN
from test_specialists import synthetic_csv

pytest.importorskip("playwright")
ROOT = Path(__file__).resolve().parent.parent
PAGE = (Path(__file__).parent / "fixtures" / "bourse_trader_sample.html").read_bytes()
TECH_PLAN = {**PLAN, "params": {"technical_subject": "شاخص کل"}}
TECH_DRAFT = {**DRAFT, "post": {**DRAFT["post"], "paragraphs": [
    *DRAFT["post"]["paragraphs"], "از نگاه تکنیکال، روند شاخص بر پایهٔ میانگین‌های متحرک بررسی شد."]}}


class FakeServices(BaseHTTPRequestHandler):
    """/page: the market page.  /v1/chat/completions: the model, answering by role (max_tokens tells which agent)."""
    plan = PLAN
    draft = DRAFT
    calls: list[str] = []

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/page":
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, b"{}", "application/json")

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        role = {800: "manager", 3500: "writer", 1500: "reviewer"}[req["max_tokens"]]
        FakeServices.calls.append(role)
        answer = {"manager": FakeServices.plan, "writer": FakeServices.draft, "reviewer": CLEAN}[role]
        text = "```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```"  # Gemini often wraps JSON in a fence
        body = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10}}
        self._send(200, json.dumps(body).encode(), "application/json")


@pytest.fixture
def project(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeServices)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    FakeServices.plan, FakeServices.draft, FakeServices.calls = PLAN, DRAFT, []
    work = tmp_path / "orchestra-ai-main"
    try:
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):  # a copy without git (e.g. unpacked from a ZIP)
        files = [p.relative_to(ROOT).as_posix() for d in ("orchestra", "orgs", "registry", "scripts")
                 for p in (ROOT / d).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    for f in files:
        if f.startswith(("legacy/", "tests/")) or f.startswith('"'):
            continue
        (work / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f, work / f)
    (work / ".env").write_text("﻿" + "\n".join([  # Notepad on Windows saves a BOM
        "ORCHESTRA_DATABASE_URL=sqlite:///data/app.db",
        "ORCHESTRA_SYSTEM_DATABASE_URL=sqlite:///data/dbos.sqlite",
        "ORCHESTRA_LLM_PROVIDER=openai_compatible",
        f"ORCHESTRA_LLM_BASE_URL=http://127.0.0.1:{port}/v1/",
        "ORCHESTRA_LLM_API_KEY=test-key",
        "ORCHESTRA_LLM_MODEL=model-a,model-b",
        f"ORCHESTRA_BOURSE_TRADER_URL=http://127.0.0.1:{port}/page",
        "TELEGRAM_BOT_TOKEN=", "BALE_BOT_TOKEN=", "",
    ]), encoding="utf-8")
    (work / "data").mkdir()
    yield work
    server.shutdown()


def env_for(work: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORCHESTRA_")}
    return {**env, "PYTHONPATH": "", "PYTHONUTF8": "1"}


def cli(work: Path, *args: str, ok: bool = True) -> str:
    p = subprocess.run([sys.executable, "-m", "orchestra.cli", *args], cwd=work, env=env_for(work),
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    out = p.stdout + p.stderr
    if ok:
        assert p.returncode == 0, out
    return out


def run_ask(work: Path, request: str) -> str:
    (work / "request.txt").write_text(request, encoding="utf-8")
    p = subprocess.run([sys.executable, "scripts/run_ask.py"], cwd=work, env=env_for(work), stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert p.returncode == 0, p.stdout + p.stderr
    return (work / "last_run.txt").read_text(encoding="utf-8")


def run_id_of(log: str) -> str:
    m = re.search(r"اجرا: (\S+) → (\S+)", log)
    assert m and m.group(2) == "awaiting_approval", log
    return m.group(1)


def test_first_run_on_a_fresh_computer_reaches_approval_and_publishes(project):
    log = run_ask(project, "گزارش پایان بازار امروز را برای همهٔ کانال‌ها آماده کن")
    rid = run_id_of(log)
    assert "Traceback" not in log and "{{" not in log
    assert "===== tg_test (telegram) =====" in log and "===== ig_test (instagram) =====" in log
    assert "منبع هر عدد" in log
    previews = project / "previews" / rid.replace(":", "_")
    slides = sorted(previews.glob("*.jpg"))
    assert len(slides) >= 3 and all(s.stat().st_size > 20_000 for s in slides)
    assert FakeServices.calls == ["manager", "writer", "reviewer", "reviewer"]

    # same request again: a new attempt with its own slides (the old one keeps waiting, untouched)
    log2 = run_ask(project, "گزارش پایان بازار امروز را برای همهٔ کانال‌ها آماده کن")
    rid2 = run_id_of(log2)
    assert rid2 != rid and (project / "previews" / rid2.replace(":", "_")).is_dir()

    out = cli(project, "show", rid)
    assert "awaiting_approval" in out

    out = cli(project, "reject", rid, "--reason", "تست")
    assert '"rejected"' in out
    out = cli(project, "approve", rid2)
    assert '"approved"' in out
    out = cli(project, "show", rid2)
    assert "approved" in out

    out = cli(project, "publish")
    assert "Traceback" not in out
    jobs = cli(project, "jobs")
    assert jobs.count("tg_test") == 1 and "ig_test" in jobs and "Traceback" not in jobs

    # a third process can still read everything; nothing left half-done
    assert "approved" in cli(project, "show", rid2)


def test_technical_analysis_with_imported_prices(project):
    csv = project / "index.csv"
    csv.write_bytes(synthetic_csv())
    out = cli(project, "import-prices", "--subject", "شاخص کل", str(csv))
    assert "وارد شد" in out
    FakeServices.plan, FakeServices.draft = TECH_PLAN, TECH_DRAFT
    log = run_ask(project, "گزارش بازار امروز با تحلیل تکنیکال شاخص کل")
    run_id_of(log)
    assert "تکنیکال" in log


def test_missing_price_history_is_a_clear_message(project):
    FakeServices.plan = TECH_PLAN
    log = run_ask(project, "گزارش بازار امروز با تحلیل تکنیکال شاخص کل")
    assert "needs_data" in log and "Traceback" not in log
    assert "import-prices" in log or "سابقهٔ قیمت" in log


def test_unreachable_model_fails_fast(project):
    env = (project / ".env").read_text(encoding="utf-8-sig")
    env = re.sub(r"ORCHESTRA_LLM_BASE_URL=.*", "ORCHESTRA_LLM_BASE_URL=http://127.0.0.1:9/v1/", env)
    (project / ".env").write_text(env, encoding="utf-8")
    log = run_ask(project, "گزارش پایان بازار")
    assert "VPN" in log and "Traceback" not in log


def test_waiting_run_can_be_approved_after_the_code_is_updated(project):
    rid = run_id_of(run_ask(project, "گزارش پایان بازار امروز را برای همهٔ کانال‌ها آماده کن"))
    wf = project / "orchestra" / "workflows" / "request.py"
    src = wf.read_text(encoding="utf-8")
    m = re.search(r"(async def handle_request\([^)]*\)[^:]*:\n)", src)
    wf.write_text(src.replace(m.group(1), m.group(1) + "    _updated = 1  # a code update while the run waits\n", 1), encoding="utf-8")
    out = cli(project, "approve", rid)
    assert '"approved"' in out


def test_run_from_an_older_incompatible_version_gives_a_clear_message(project):
    rid = run_id_of(run_ask(project, "گزارش پایان بازار امروز را برای همهٔ کانال‌ها آماده کن"))
    c = project / "orchestra" / "cli.py"
    c.write_text(c.read_text(encoding="utf-8").replace('APP_VERSION = "orchestra-1"', 'APP_VERSION = "orchestra-2"'), encoding="utf-8")
    out = cli(project, "approve", rid, ok=False)
    assert "نسخهٔ قدیمی‌تر" in out and "Traceback" not in out


def test_an_unexpected_error_in_a_middle_step_ends_the_run_instead_of_hanging(project):
    (project / "orchestra" / "visual" / "fonts" / "Vazirmatn-Bold.woff2").unlink()  # the designer will crash
    log = run_ask(project, "گزارش پایان بازار امروز را برای همهٔ کانال‌ها آماده کن")
    assert "→ failed" in log and "FileNotFoundError" in log
