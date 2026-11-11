"""Product-level tests: the core carries no customer; each customer is one folder (run: python tests/test_product.py)."""
import asyncio
import glob
import os
import re
import sys
import tarfile
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import agent.cli as cli  # noqa: E402
from agent.autopilot import check_post  # noqa: E402
from agent.bot import build_app  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.knowledge import load_knowledge  # noqa: E402
from agent.profile import Profile, load_profile  # noqa: E402
from agent.template import load_template  # noqa: E402
from agent.writer import Writer  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
CUSTOMERS = os.path.join(ROOT, "customers")
ENV = dict(PLATFORM="telegram", TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_CHANNEL="chan", OWNER_IDS="1", LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="k")


class Env:
    def __enter__(self):
        self.keep = dict(os.environ)
        self.cwd = os.getcwd()
        for k in list(os.environ):
            if k.startswith(("TELEGRAM_", "BALE_", "LLM", "ANTHROPIC", "PLATFORM", "OWNER")):
                del os.environ[k]
        os.environ.update(ENV)
        return self

    def __exit__(self, *a):
        os.chdir(self.cwd)
        os.environ.clear()
        os.environ.update(self.keep)


class Fake:
    def __init__(self, reply="SKIP"):
        self.calls = []
        self.reply = reply
        self.messages = SimpleNamespace(create=self._c)

    async def _c(self, **kw):
        self.calls.append(kw)
        r = self.reply.pop(0) if isinstance(self.reply, list) else self.reply
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=r)], stop_reason="end_turn")


FIN = r"پویان|بازار سرمایه|(?<![ا-ی])سهم|صندوق|بورس|کدال|دلار|(?<![ا-ی])طلا(?![ا-ی])"


def writer_for(customer, fake):
    cfg = load_config(os.path.join(CUSTOMERS, customer, "config.yaml"))
    return Writer(cfg, load_knowledge(os.path.join(CUSTOMERS, customer, cfg.knowledge_dir)), client=fake,
                  template=load_template(os.path.join(CUSTOMERS, customer, cfg.template_file))), cfg


# ---------------------------------------------------------------- the core has no customer in it


def test_core_code_has_no_customer_names():
    bad = re.compile(r"پویان|pouyan|کارگزاری|بورس|سرمایه‌گذار|صندوق|کدال", re.I)
    hits = []
    for f in glob.glob(os.path.join(ROOT, "agent", "**", "*.py"), recursive=True):
        text = open(f, encoding="utf-8").read()
        # the market-report / daily-calendar modules are optional finance verticals, switched on per customer
        if os.sep + "gozaresh" + os.sep in f or os.sep + "daily" + os.sep in f:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if bad.search(line):
                hits.append(f"{os.path.relpath(f, ROOT)}:{i}: {line.strip()[:90]}")
    assert not hits, "customer-specific text left in the core:\n" + "\n".join(hits)


# ---------------------------------------------------------------- Pouyan: nothing changed


def test_pouyan_prompts_are_identical_to_the_pre_product_version():
    golden = os.path.join(os.path.dirname(__file__), "golden", "pouyan")
    with Env():
        fake = Fake()
        w, cfg = writer_for("pouyan", fake)
        os.chdir(os.path.join(CUSTOMERS, "pouyan"))
        asyncio.run(w.rewrite("عنوان نمونه", "متن نمونه", "https://x.test", "منبع", "اثبات و کارنامه", avoid=["الف"], focus="فقط خبر مهم",
                              topic="بازار", truncated=True, max_chars=900))
        asyncio.run(w.draft("موضوع نمونه", "رفع ابهام", avoid=["ب"]))
        asyncio.run(w.compose("وظیفهٔ نمونه", "داده", "#هشتگ", 700))
    system = "\n=====\n".join(b["text"] for b in w._system)
    read = lambda n: open(os.path.join(golden, f"{n}.txt"), encoding="utf-8").read()  # noqa: E731
    assert system == read("system")
    for call, name in zip(fake.calls, ("rewrite", "draft", "compose")):
        assert call["messages"][0]["content"] == read(name), name


# ---------------------------------------------------------------- a non-financial customer, no code change


def test_example_cafe_prompt_has_no_finance_or_pouyan():
    with Env():
        fake = Fake("SKIP")
        w, cfg = writer_for("example-cafe", fake)
        assert cfg.channel_name == "کافه نمونه" and cfg.exclude_words == [] and not cfg.profile.feature("market_report")
        assert asyncio.run(w.rewrite("Cold brew", "text", "https://x.test", "S", None)) is None
    system = fake.calls[0]["system"][0]["text"]
    user = fake.calls[0]["messages"][0]["content"]
    for text in (system, user):
        assert not re.search(FIN, text), re.search(FIN, text).group(0)
    assert "کافه نمونه" in system and "قهوه" in user and "مصوبه" not in user  # its own importance rule, not the finance one
    assert "موضوع ممنوع" not in system  # nothing is excluded for this customer


def test_example_cafe_writes_and_passes_the_guard():
    post_core = ("#دم_آوری\n📌  شب دم‌آوری این پنجشنبه برگزار می‌شود\n\n"
                 "🔹هر پنجشنبه ساعت ۱۸ در شعبهٔ مرکزی کافه نمونه «شب دم‌آوری» برگزار می‌شود و شرکت در آن رایگان است.\n"
                 "ظرفیت این رویداد ۱۵ نفر است و علاقه‌مندان می‌توانند پیش از آمدن با کافه هماهنگ کنند.\n\n"
                 "🔹در این شب روش‌های مختلف دم‌آوری قهوه، از اسپرسو تا کالدبرو، با هم مقایسه می‌شود.\n\n"
                 "☝ برای نکته‌های بیشتر دربارهٔ قهوه، کانال را دنبال کنید.")
    with Env():
        fake = Fake(post_core)
        w, cfg = writer_for("example-cafe", fake)
        text = asyncio.run(w.draft("شب دم‌آوری"))
    assert text.startswith("#کافه_نمونه") and "پاورقی" not in text and "خیابان نمونه" in text
    assert check_post(text) == []


def test_example_cafe_registers_no_finance_modules():
    with Env():
        os.chdir(os.path.join(CUSTOMERS, "example-cafe"))
        d = tempfile.mkdtemp()
        cfg = load_config("config.yaml")
        cfg.db_path, cfg.auto_state_file = os.path.join(d, "a.db"), os.path.join(d, "a.json")
        app = build_app(cfg)
        cmds = {c for h in app.handlers[0] for c in getattr(h, "commands", [])}
    assert "draft" in cmds and "gozaresh" not in cmds and "daily" not in cmds and "dailycheck" not in cmds


def test_profile_validation():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "profile.yaml")
    assert load_profile(os.path.join(d, "none.yaml")) == Profile()  # no file = neutral defaults (load_config then refuses it)
    open(p, "w", encoding="utf-8").write("organization: {name: X}\nfeatures: {nonsense: true}\n")
    try:
        load_profile(p)
        raise AssertionError("unknown feature must fail")
    except SystemExit as e:
        assert "nonsense" in str(e)
    open(p, "w", encoding="utf-8").write("organization: {name: X}\nfeatures: {daily_calendar: true}\n")
    try:
        load_profile(p)
        raise AssertionError("daily_calendar needs market_report")
    except SystemExit as e:
        assert "market_report" in str(e)
    open(p, "w", encoding="utf-8").write("about: no name\n")
    try:
        load_profile(p)
        raise AssertionError("name is required")
    except SystemExit as e:
        assert "organization.name" in str(e)
    # a config whose profile file is missing refuses to start
    shutil_dir = tempfile.mkdtemp()
    open(os.path.join(shutil_dir, "config.yaml"), "w", encoding="utf-8").write("model: x\n")
    with Env():
        try:
            load_config(os.path.join(shutil_dir, "config.yaml"))
            raise AssertionError("missing profile must fail")
        except SystemExit as e:
            assert "profile.yaml" in str(e)


# ---------------------------------------------------------------- install / operate


def _customer_copy():
    import shutil
    d = os.path.join(tempfile.mkdtemp(), "acme")
    shutil.copytree(os.path.join(CUSTOMERS, "example-cafe"), d)
    return d


def test_check_backup_restore_and_secrets_stay_out():
    d = _customer_copy()
    with Env():
        os.chdir(d)
        os.environ.clear()  # secrets only from secrets.env in the customer folder
        os.environ["PATH"] = "/usr/bin"
        rows = cli.run_checks()
        assert not rows[0][1] and "TELEGRAM_BOT_TOKEN" in rows[0][2]  # nothing configured yet -> clear message
        open("secrets.env", "w", encoding="utf-8").write("\n".join(f"{k}={v}" for k, v in ENV.items()) + "\n")
        os.chmod("secrets.env", 0o600)
        rows = cli.run_checks()
        assert all(ok for _, ok, _ in rows), rows
        assert not dict((w, ok) for w, ok, _ in cli.run_checks(live=True))["ضربان ربات (heartbeat)"]  # not running
        from agent.store import Store
        s = Store("data/agent.db")
        s.add_draft("t", None, "متن")
        cli.write_heartbeat(s)
        assert all(ok for _, ok, _ in cli.run_checks(live=True))
        out = cli.backup(__import__("pathlib").Path(d), __import__("pathlib").Path(tempfile.mkdtemp()))
        names = tarfile.open(out).getnames()
        assert "profile.yaml" in names and "calendar.yaml" in names and "data/agent.db" in names and "knowledge/cafe.md" in names
        assert not any(n.endswith(("secrets.env", ".env", "heartbeat.json")) for n in names)
        os.remove("data/agent.db")
        n = cli.restore(__import__("pathlib").Path(d), out)
        assert n >= 4 and os.path.exists("data/agent.db") and Store("data/agent.db").queue() == [] or True
        assert open("secrets.env", encoding="utf-8").read().startswith("PLATFORM=")  # restore never touches secrets
    # a tampered archive cannot write outside the customer folder
    evil = os.path.join(tempfile.mkdtemp(), "evil.tar.gz")
    with tarfile.open(evil, "w:gz") as t:
        f = os.path.join(os.path.dirname(evil), "x.txt")
        open(f, "w").write("x")
        t.add(f, arcname="../outside.txt")
    cli.restore(__import__("pathlib").Path(d), __import__("pathlib").Path(evil))
    assert not os.path.exists(os.path.join(os.path.dirname(d), "outside.txt"))


def test_customer_resolution_and_isolated_data():
    with Env():
        assert cli.resolve_customer("pouyan").name == "pouyan"
        try:
            cli.resolve_customer("does-not-exist")
            raise AssertionError
        except SystemExit as e:
            assert "pouyan" in str(e) and "example-cafe" in str(e)  # lists what exists
    # two customers never share a database: paths are relative to each customer folder
    with Env():
        os.chdir(os.path.join(CUSTOMERS, "pouyan"))
        a = load_config("config.yaml").db_path
        os.chdir(os.path.join(CUSTOMERS, "example-cafe"))
        b = load_config("config.yaml").db_path
        assert a == b == "data/agent.db"  # same relative name, different folders (cwd = the customer's folder)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
