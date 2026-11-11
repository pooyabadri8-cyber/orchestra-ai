"""Offline tests of the /gozaresh module (run: python tests/test_gozaresh.py)."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.gozaresh.handlers import Gozaresh, normalize_times, send_due  # noqa: E402
from agent.gozaresh.parse import GozareshError, parse_money, parse_page  # noqa: E402
from agent.gozaresh.report import format_report, gregorian_to_jalali, money_text  # noqa: E402
from agent.gozaresh.settings import GozareshSettings, load_settings  # noqa: E402

# Structure copied from the real page (bourse-trader.ir home), shortened to a few rows per table.
PAGE = """<html><body>
<table class="table marketTable mb-0"><tbody>
<tr><td class="text-right">شاخص کل</td><td dir="ltr" class="bl-colu text-nowrap"> 7,976,330 <span class="{cls}">({sign}186,425) {sign}2.3932%</span> </td></tr>
<tr><td class="text-right">شاخص هم وزن</td><td dir="ltr" class="bl-colu text-nowrap"> 2,111,890 <span class="text-success">(46,110) 2.2321%</span> </td></tr>
<tr><td class="text-right">ارزش بازار</td><td dir="ltr" class="bl-colu"> 27,322.9T (101.4B $) </td></tr>
<tr><td class="text-right text-nowrap">معاملات خرد</td><td class="bl-colu"><div class="d-flex justify-content-between align-items-center">
 <div class="text-center"><div class="fw-bold"><a href="charts#trade-value" class="text-dark">{trade}</a></div><small class="text-muted">معاملات</small></div>
 <div class="text-center"><div class="fw-bold"><a href="charts#money-inflow" class="text-success">{flow}</a></div><small class="text-muted">ورود پول</small></div>
</div></td></tr>
<tr><td class="text-right text-nowrap">صندوق درآمدثابت</td><td class="bl-colu"><div class="d-flex"><div class="text-center"><div class="fw-bold">9.6T</div><small class="text-muted">معاملات</small></div>
 <div class="text-center"><div class="fw-bold text-danger">-3.1T</div><small class="text-muted">ورود پول</small></div></div></td></tr>
</tbody></table>
<h2>بیشترین ورود پول حقیقی</h2>
<table class="table"><thead><tr><th>نماد</th><th title="ورود پول حقیقی (تومان)">ورود پول</th><th>%آخرین</th><th>خرید حقیقی</th><th>حجم</th></tr></thead><tbody>
<tr><td><a href="symbol/فولاد">فولاد</a></td><td class="bl-colu text-success" title="536.5B">503.6B</td><td>2.9</td><td>1.4B</td><td>1.8B</td></tr>
<tr><td><a href="symbol/دوایکس">دوایکس</a></td><td class="bl-colu text-success">378.5B</td><td>4</td><td>107.1M</td><td>110.1M</td></tr>
<tr><td><a href="symbol/فملی">فملی</a></td><td class="bl-colu text-success">150.9B</td><td>3</td><td>80.2M</td><td>88.1M</td></tr>
</tbody></table>
<table class="table"><thead><tr><th>نماد</th><th>خروج پول</th><th>%آخرین</th><th>خرید حقوقی</th><th>حجم</th></tr></thead><tbody>
<tr><td><a>نوری</a></td><td class="bl-colu text-danger">-37.3B</td><td>2.8</td><td>13.2M</td><td>30.4M</td></tr>
<tr><td><a>شگویا</a></td><td class="bl-colu text-danger">-34.9B</td><td>2</td><td>27.5M</td><td>42.2M</td></tr>
</tbody></table>
<table class="table"><thead><tr><th>گروه</th><th>ورود پول</th><th>ارزش معاملات</th><th>قدرت خرید</th></tr></thead><tbody>
<tr><td class="text-right"><a>فلزات</a></td><td class="bl-colu text-success">1T</td><td>2.1T</td><td>3.4</td></tr>
<tr><td class="text-right"><a>صندوق سهامی</a></td><td class="bl-colu text-success">430.7B</td><td>1T</td><td>1.8</td></tr>
</tbody></table>
<table class="table"><thead><tr><th>گروه</th><th>خروج پول</th><th>ارزش معاملات</th><th>قدرت خرید</th></tr></thead><tbody>
<tr><td class="text-right"><a>صندوق درآمد ثابت</a></td><td class="bl-colu text-danger">-3.1T</td><td>9.6T</td><td>1</td></tr>
<tr><td class="text-right"><a>سیمان</a></td><td class="bl-colu text-danger">-29.3B</td><td>352.9B</td><td>1.6</td></tr>
</tbody></table>
</body></html>"""


def page(cls="text-success", sign="", trade="18.2T", flow="3.9T"):
    return PAGE.format(cls=cls, sign=sign, trade=trade, flow=flow)


def test_parse_money_and_jalali():
    m = parse_money("-3.1T")
    assert m.value == -3.1 and m.unit == "T"
    assert parse_money("503.6B").value == 503.6 and parse_money("0").is_zero
    assert parse_money("۲۰.۵M").value == 20.5
    assert gregorian_to_jalali(2026, 10, 6) == (1405, 7, 14)
    assert gregorian_to_jalali(2025, 3, 21) == (1404, 1, 1)
    assert normalize_times(["۱۰:۳۰", "9:5".replace("9:5", "9:05"), "10:30"]) == ["09:05", "10:30"]
    for bad in (["25:00"], ["abc"], ["10:7"]):
        try:
            normalize_times(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_parse_page_values():
    r = parse_page(page(), top_n=2)
    assert (r.index.value, r.index.change, r.index.pct) == (7976330, 186425, 2.3932)
    assert (r.retail_value.value, r.retail_value.unit) == (18.2, "T")
    assert (r.retail_flow.value, r.retail_flow.unit) == (3.9, "T")
    assert [x.name for x in r.top_in_stocks] == ["فولاد", "دوایکس"]  # top_n respected
    assert [x.name for x in r.top_out_stocks] == ["نوری", "شگویا"] and r.top_out_stocks[0].amount.value == -37.3
    assert [x.name for x in r.top_in_groups] == ["فلزات", "صندوق سهامی"]
    assert r.top_out_groups[0].name == "صندوق درآمد ثابت" and r.top_out_groups[0].amount.negative
    assert not r.warnings


def test_parse_negative_index_and_outflow():
    r = parse_page(page(cls="text-danger", flow="-1.2T"))
    assert r.index.change == -186425 and r.index.pct == -2.3932
    assert r.retail_flow.value == -1.2
    text = format_report(r, datetime(2026, 10, 6, 10, 52), True)
    assert "🔴" in text and "خروج پول حقیقی (معاملات خرد)" in text and "−۱۸۶٬۴۲۵" in text


def test_parse_failures_are_explicit():
    for bad in ("<html></html>", page().replace("marketTable", "other")):
        try:
            parse_page(bad)
            raise AssertionError("expected GozareshError")
        except GozareshError:
            pass
    # a missing list is only a warning, the rest still works
    no_groups = page().replace("<th>گروه</th><th>خروج پول</th>", "<th>x</th><th>y</th>")
    r = parse_page(no_groups)
    assert r.top_out_groups == [] and any("گروه" in w for w in r.warnings)


def test_format_report_text():
    r = parse_page(page(), top_n=5)
    text = format_report(r, datetime(2026, 10, 6, 10, 52), True)
    for needle in ["۱۴۰۵/۰۷/۱۴", "۱۰:۵۲", "شاخص کل", "۷٬۹۷۶٬۳۳۰", "+۱۸۶٬۴۲۵", "+۲.۳۹٪", "ارزش معاملات خرد",
                   "۱۸.۲ همت", "ورود پول حقیقی (معاملات خرد)", "۳.۹ همت", "فولاد: ۵۰۳.۶ میلیارد تومان",
                   "نوری: ۳۷.۳ میلیارد تومان", "فلزات: ۱ همت", "bourse-trader.ir"]:
        assert needle in text, needle
    assert "-" not in text.split("بیشترین خروج")[1].split("منبع")[0]  # outflows are shown as positive amounts
    ascii_text = format_report(r, datetime(2026, 10, 6, 10, 52), False)
    assert "7,976,330" in ascii_text and "503.6" in ascii_text
    assert "صفر است" in format_report(parse_page(page(trade="0", flow="0")), datetime(2026, 10, 6, 10, 0), True)
    assert money_text(parse_money("12.5M"), True) == "۱۲.۵ میلیون تومان"


def test_settings_defaults_and_validation():
    s = load_settings("/nonexistent.yaml")
    assert s.top_n == 5 and s.off_weekdays == [3, 4]
    s = load_settings(os.path.join(os.path.dirname(__file__), "..", "customers", "pouyan", "gozaresh.yaml"))
    assert s.off_weekdays == [3, 4] and s.url.startswith("https://bourse-trader.ir")


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class FakeCtx:
    def __init__(self, svc):
        self.application = type("A", (), {"bot_data": {"gozaresh": svc}})()
        self.bot = FakeBot()


def make_svc(tmp, owners=(1, 2)):
    s = GozareshSettings(state_file=os.path.join(tmp, "g.json"))
    return Gozaresh(s, ZoneInfo("Asia/Tehran"), set(owners))


def test_fetch_cache_and_error_messages():
    import httpx

    import agent.gozaresh.handlers as hm

    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, text=page())

    real = httpx.AsyncClient
    hm.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        svc = make_svc(tempfile.mkdtemp())
        asyncio.run(svc.fetch())
        asyncio.run(svc.fetch())  # inside the cache window -> no second request
        assert len(calls) == 1
        asyncio.run(svc.fetch(force=True))
        assert len(calls) == 2

        def blocked(request):
            return httpx.Response(403, text="no")

        hm.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(blocked), **kw)
        svc2 = make_svc(tempfile.mkdtemp())
        try:
            asyncio.run(svc2.fetch())
            raise AssertionError("expected error")
        except GozareshError as e:
            assert "403" in str(e)
    finally:
        hm.httpx.AsyncClient = real


def test_schedule_sends_once_per_time_and_respects_closed_days():
    import httpx

    import agent.gozaresh.handlers as hm

    class FixedDT(datetime):
        now_value = None

        @classmethod
        def now(cls, tz=None):
            return cls.now_value

    real_client, real_dt = httpx.AsyncClient, hm.datetime
    hm.httpx.AsyncClient = lambda **kw: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page())), **kw)
    hm.datetime = FixedDT
    tz = ZoneInfo("Asia/Tehran")
    try:
        svc = make_svc(tempfile.mkdtemp(), owners=(1, 2))
        svc.set_times(1, ["10:30", "18:30"])  # user 2 sets nothing
        ctx = FakeCtx(svc)

        FixedDT.now_value = datetime(2026, 10, 6, 10, 31, tzinfo=tz)  # Tuesday
        asyncio.run(send_due(ctx))
        assert [c for c, _ in ctx.bot.sent] == [1]
        asyncio.run(send_due(ctx))  # same minute again -> not duplicated
        assert len(ctx.bot.sent) == 1

        FixedDT.now_value = datetime(2026, 10, 6, 12, 0, tzinfo=tz)  # no slot now
        asyncio.run(send_due(ctx))
        assert len(ctx.bot.sent) == 1

        FixedDT.now_value = datetime(2026, 10, 6, 18, 50, tzinfo=tz)  # 20 min late (> 15) -> skipped
        asyncio.run(send_due(ctx))
        assert len(ctx.bot.sent) == 1

        FixedDT.now_value = datetime(2026, 10, 8, 10, 31, tzinfo=tz)  # Thursday: closed
        asyncio.run(send_due(ctx))
        assert len(ctx.bot.sent) == 1

        # restart keeps the "already sent" memory
        svc_b = Gozaresh(svc.s, tz, {1, 2})
        FixedDT.now_value = datetime(2026, 10, 6, 10, 32, tzinfo=tz)
        ctx_b = FakeCtx(svc_b)
        asyncio.run(send_due(ctx_b))
        assert ctx_b.bot.sent == []
        assert svc_b.times_for(1) == ["10:30", "18:30"] and svc_b.times_for(2) == []
    finally:
        hm.httpx.AsyncClient = real_client
        hm.datetime = real_dt


def test_command_parsing_and_owner_only():
    import httpx

    import agent.gozaresh.handlers as hm

    class Msg:
        chat_id = 10

        def __init__(self):
            self.replies = []

        async def reply_text(self, text, **kw):
            self.replies.append(text)
            m = self

            class W:
                async def edit_text(self, t, **k):
                    m.replies.append(t)

                async def delete(self):
                    m.replies.append("<deleted>")

            return W()

    def update(uid, msg):
        return type("U", (), {"effective_user": type("X", (), {"id": uid})(), "effective_message": msg})()

    real = httpx.AsyncClient
    hm.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page())), **kw)
    try:
        svc = make_svc(tempfile.mkdtemp(), owners=(1,))
        ctx = FakeCtx(svc)

        def run(uid, *args):
            ctx.args = list(args)
            m = Msg()
            asyncio.run(hm.cmd_gozaresh(update(uid, m), ctx))
            return m.replies

        assert run(99) == []  # strangers get nothing
        assert "ذخیره شد" in run(1, "۱۰:۳۰", "18:30")[0] and svc.times_for(1) == ["10:30", "18:30"]
        assert "10:30" in run(1, "times")[0]
        assert "نفهمیدم" in run(1, "25:99")[0]
        assert "خاموش شد" in run(1, "off")[0] and svc.times_for(1) == []
        assert svc.subs["1"] == []  # explicit off survives default_times
        run(1)  # report now
        assert ctx.bot.sent and "گزارش روزانهٔ بازار سرمایه" in ctx.bot.sent[-1][1]
    finally:
        hm.httpx.AsyncClient = real


def test_real_page_sample():
    """Fixture saved from the live site (markup exactly as served)."""
    path = os.path.join(os.path.dirname(__file__), "fixtures", "bourse_trader_sample.html")
    r = parse_page(open(path, encoding="utf-8").read(), top_n=3)
    assert (r.index.value, r.index.change, r.index.pct) == (7976400, 186492, 2.394)
    assert (r.retail_value.value, r.retail_value.unit) == (18.6, "T") and (r.retail_flow.value, r.retail_flow.unit) == (4.0, "T")
    assert [x.name for x in r.top_in_stocks] == ["فولاد", "دوایکس", "فملی"]
    assert [(x.name, x.amount.value) for x in r.top_out_stocks][0] == ("نوری", -39.5)
    assert r.top_in_groups[0].name == "فلزات" and r.top_in_groups[0].amount.unit == "T"
    assert r.top_out_groups[0].name == "صندوق درآمد ثابت" and not r.warnings


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
