"""Offline tests of the fixed post layout (run: python tests/test_template.py)."""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.config import Config  # noqa: E402
from agent.template import SEP, Template, load_template  # noqa: E402
from agent.writer import Writer, add_attribution  # noqa: E402

from _profile import P  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
CUSTOMER = os.path.join(ROOT, "customers", "pouyan")
TPL = Template(
    brand_hashtag="#کارگزاری_پویان", default_topic_hashtag="#بازار_سرمایه", cta_url="https://pouyancrowd.ir",
    default_cta="ثبت درخواست", footer_text="نیاز به راهنمایی دارید؟",
    footer_links=[{"text": "🌐 سایت", "url": "https://pouyancrowd.ir"}, {"text": "📞 ۰۲۱"},
                  {"text": "📱 اینستاگرام", "url": ""}],
)
MODEL = "#تامین_مالی_جمعی\n📌 تیتر نمونه\n🔹بند اول\nادامهٔ بند اول\n\n🔹بند دوم\n☝ فراخوان نمونه"


def test_exact_layout():
    out = TPL.finalize(MODEL)
    expected = (
        "#کارگزاری_پویان\n#تامین_مالی_جمعی\n\n\n"
        "📌  <b>تیتر نمونه</b>\n\n"
        "🔹بند اول\nادامهٔ بند اول\n\n🔹بند دوم\n\n\n"
        '☝ <a href="https://pouyancrowd.ir">فراخوان نمونه</a>\n\n\n'
        f"{SEP}\nنیاز به راهنمایی دارید؟\n\n"
        '<a href="https://pouyancrowd.ir">🌐 سایت</a>\n📞 ۰۲۱'
    )
    assert out == expected, out
    assert "اینستاگرام" not in out  # empty url -> line hidden until the link is filled in


def test_repairs_model_drift():
    messy = "```\n#بازار_سرمایه #دیگر\n📌 تیتر\nمتن بدون علامت\n🔹بند\n☝️ فراخوان اول\n☝ فراخوان دوم\n#کارگزاری_پویان\n```"
    out = TPL.finalize(messy)
    assert out.startswith("#کارگزاری_پویان\n#بازار_سرمایه\n")  # one brand tag, one topic tag, no duplicates
    assert "#دیگر" not in out and "```" not in out and out.count("#کارگزاری_پویان") == 1
    assert "🔹متن بدون علامت" in out and "🔹بند" in out and "فراخوان اول" in out and "فراخوان دوم" not in out
    # missing pieces get safe defaults, never a broken layout
    bare = TPL.finalize("فقط یک خط")
    assert "📌  <b>فقط یک خط</b>" in bare and "☝ " in bare and "#بازار_سرمایه" in bare and SEP in bare
    # model that tried to write the footer itself: ours replaces it
    again = TPL.finalize(TPL.finalize(MODEL) + "\nاضافه")
    assert again.count(SEP) == 1 and again.count("نیاز به راهنمایی") == 1


def test_source_line_and_idempotence():
    base = TPL.finalize(MODEL)
    a = add_attribution(base, "سنا", "https://sena.ir/x")
    assert a.index("منبع: سنا") < a.index(SEP) and a.index("فراخوان نمونه") < a.index("منبع: سنا")
    core, src = TPL.split(a)
    assert "منبع" not in core and src.startswith("<a ") and TPL.finalize(core, src) == a
    b = add_attribution(a, "دیگری")  # replaces, never stacks
    assert b.count("منبع:") == 1 and "دیگری" in b
    assert add_attribution("متن ساده", "X") == "متن ساده\n\nمنبع: X"  # untemplated text still works


def test_headline_plain_and_real_config_file():
    t = Template(headline_bold=False)
    assert "📌  تیتر" in t.finalize("📌 تیتر\n🔹بند")
    real = load_template(os.path.join(CUSTOMER, "post_template.yaml"))
    assert real.brand_hashtag == "#کارگزاری_پویان" and real.footer_links
    out = real.finalize(MODEL)
    assert "📞 ۰۲۱-۹۱۰۰۵۷۷۷" in out
    assert 'href="https://www.instagram.com/pouyanbrokerage"' in out and 'href="https://ble.ir/pouyanbrokerage"' in out
    # Pouyan Crowd is NOT advertised under every post: no site link in the fixed parts or in a neutral CTA
    neutral = real.finalize("#بازار_سرمایه\n📌 خبر\n🔹بند")
    assert "pouyancrowd" not in neutral and "پویان کراد" not in neutral and "☝ برای دیدن" in neutral
    # ...but a related post can link it itself
    promo = real.finalize('#تامین_مالی_جمعی\n📌 معرفی\n🔹بند\n☝ <a href="https://pouyancrowd.ir">ثبت درخواست</a>')
    assert promo.count("pouyancrowd.ir") == 1
    assert load_template("/nonexistent.yaml").brand_hashtag.startswith("#")  # missing file -> defaults


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.replies.pop(0))], stop_reason="end_turn")


def test_writer_uses_template_and_revise_protects_footer():
    cfg = Config(**P, bot_token="x", api_key="x", channel="@c", owner_ids={1})
    fake = FakeClient([MODEL, "#بازار_سرمایه\n📌 تیتر کوتاه‌تر\n🔹بند فشرده\n☝ فراخوان"])
    w = Writer(cfg, "<kb>", client=fake, template=TPL)
    post = add_attribution(asyncio.run(w.draft("موضوع")), "S", "https://s.test/1")
    assert post.startswith("#کارگزاری_پویان") and SEP in post
    new = asyncio.run(w.revise(post, "کوتاه‌ترش کن"))
    sent = fake.calls[1]["messages"][0]["content"]
    assert SEP not in sent and "نیاز به راهنمایی" not in sent and "منبع:" not in sent  # model never sees the fixed parts
    assert "تیتر کوتاه‌تر" in new and "منبع: S" in new and new.count(SEP) == 1 and "نیاز به راهنمایی" in new
    rules = fake.calls[0]["system"][0]["text"]
    assert "☝" in rules and "📌" in rules
    assert "به پویان کراد اشاره نکن" in rules and "فقط» وقتی" in rules  # promotion only on related posts


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
