"""Designer: branded carousel slides from verified content, rendered to PNG by a headless browser.

- The layout is a versioned HTML/CSS template; the brand kit gives colours, the bundled Vazirmatn font is embedded,
  so the same input renders the same way on any machine.
- Text on a slide is the verified, already-rendered text; numbers come from tokens, charts from ChartSpecs.
- Automatic QC: the font must load, nothing may overflow its box (the template first tries smaller type), and the
  image must have the exact Instagram portrait size. A slide that fails QC stops the run; nothing is guessed.
- A data manifest per slide lists the claims and chart sources it shows.
"""
from __future__ import annotations

import base64
import hashlib
import html
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..brand import Brand
from ..content.numbers import FA
from .svg import render_svg

TEMPLATE_VERSION = "carousel@1"
WIDTH, HEIGHT = 1080, 1350
FONT_DIR = Path(__file__).resolve().parent / "fonts"
SCALES = (1.0, 0.92, 0.84, 0.76)


class RenderError(Exception):
    pass


@dataclass(frozen=True)
class SlideImage:
    order: int
    png: bytes
    width: int
    height: int
    sha256: str
    kind: str
    chart_id: str | None
    manifest: dict = field(default_factory=dict)


def _font_face() -> str:
    faces = []
    for weight, name in ((400, "Regular"), (700, "Bold"), (800, "ExtraBold")):
        data = base64.b64encode((FONT_DIR / f"Vazirmatn-{name}.woff2").read_bytes()).decode()
        faces.append(f"@font-face{{font-family:Vazirmatn;font-weight:{weight};src:url(data:font/woff2;base64,{data}) format('woff2');}}")
    return "".join(faces)


def _css(brand: Brand) -> str:
    c = brand.colors
    return _font_face() + f"""
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{width:{WIDTH}px;height:{HEIGHT}px;overflow:hidden;background:{c.background}}}
body{{font-family:Vazirmatn,sans-serif;color:{c.text};direction:rtl;--s:1}}
.slide{{position:relative;width:{WIDTH}px;height:{HEIGHT}px;padding:84px 80px 150px;display:flex;flex-direction:column;gap:calc(34px*var(--s));
  background:radial-gradient(1200px 700px at 100% 0%, {c.surface} 0%, {c.background} 70%)}}
.bar{{position:absolute;top:0;right:0;left:0;height:14px;background:linear-gradient(90deg,{c.accent},{c.primary})}}
.brand{{font-weight:800;font-size:30px;color:{c.accent};letter-spacing:0}}
h1{{font-weight:800;font-size:calc(66px*var(--s));line-height:1.35}}
.cover h1{{font-size:calc(84px*var(--s));line-height:1.3;margin-top:auto}}
.sub{{font-size:calc(38px*var(--s));line-height:1.6;color:{c.muted}}}
.cover .sub{{margin-bottom:auto}}
.stats{{display:grid;grid-template-columns:1fr 1fr;gap:22px}}
.stat{{background:{c.surface};border-radius:24px;padding:calc(26px*var(--s)) 30px;border-right:8px solid {c.primary}}}
.stat .v{{font-weight:800;font-size:calc(46px*var(--s));line-height:1.3;color:{c.text}}}
.stat .l{{font-size:calc(27px*var(--s));color:{c.muted};margin-top:6px}}
ul{{list-style:none;display:flex;flex-direction:column;gap:calc(20px*var(--s))}}
li{{font-size:calc(36px*var(--s));line-height:1.65;padding-right:34px;position:relative}}
li::before{{content:"";position:absolute;right:0;top:.72em;width:14px;height:14px;border-radius:50%;background:{c.accent}}}
.chart{{background:{c.surface};border-radius:28px;padding:26px 20px 14px}}
.chart .ct{{font-weight:700;font-size:28px;color:{c.muted};margin:0 10px 12px}}
.chart svg{{width:100%;height:auto;display:block}}
.foot{{position:absolute;bottom:58px;right:80px;left:80px;display:flex;justify-content:space-between;font-size:26px;color:{c.muted}}}
.end{{justify-content:center;align-items:flex-start}}
.end h1{{font-size:calc(64px*var(--s))}}
.end .links{{display:flex;flex-direction:column;gap:18px;font-size:34px}}
"""


def _e(s: str) -> str:
    return html.escape(s)


def _page(n: int, total: int) -> str:
    return f"{str(n).translate(FA)} / {str(total).translate(FA)}"


def slide_html(brand: Brand, slide, n: int, total: int) -> str:
    """slide: RenderedSlide, or None for the brand's closing slide."""
    foot = f'<div class="foot"><span>{_e(brand.template.brand_hashtag)}</span><span>{_page(n, total)}</span></div>'
    if slide is None:
        t = brand.template
        links = "".join(f"<div>{_e(link.text)}</div>" for link in t.footer_links)
        body = (f'<div class="slide end"><div class="bar"></div><div class="brand">{_e(brand.name)}</div>'
                f'<h1 class="fit">{_e(t.default_cta)}</h1><div class="sub fit">{_e(t.footer_text)}</div>'
                f'<div class="links">{links}</div>{foot}</div>')
    elif slide.kind == "cover":
        sub = "".join(f'<div class="sub">{_e(b)}</div>' for b in slide.bullets)
        body = (f'<div class="slide cover"><div class="bar"></div><div class="brand">{_e(brand.name)}</div>'
                f'<h1>{_e(slide.title)}</h1>{sub}{foot}</div>')
    else:
        stats = ""
        if slide.stats:
            tiles = "".join(f'<div class="stat"><div class="v">{_e(v)}</div><div class="l">{_e(label)}</div></div>' for label, v in slide.stats)
            stats = f'<div class="stats">{tiles}</div>'
        chart = ""
        if slide.chart is not None:
            chart = f'<div class="chart"><div class="ct">{_e(slide.chart.title)}</div>{render_svg(slide.chart, brand.colors)}</div>'
        bullets = f'<ul>{"".join(f"<li>{_e(b)}</li>" for b in slide.bullets)}</ul>' if slide.bullets else ""
        body = (f'<div class="slide"><div class="bar"></div><div class="brand">{_e(brand.name)}</div>'
                f'<h1>{_e(slide.title)}</h1>{stats}{chart}{bullets}{foot}</div>')
    return f'<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><style>{_css(brand)}</style></head><body>{body}</body></html>'


_QC_JS = """async () => {
  for (const w of [400, 700, 800]) { try { await document.fonts.load(w + ' 40px Vazirmatn', 'سلام'); } catch (e) {} }
  await document.fonts.ready;
  const fontOk = [400, 700, 800].every(w => document.fonts.check(w + ' 40px Vazirmatn', 'سلام'));
  const slide = document.querySelector('.slide');
  const foot = document.querySelector('.foot').getBoundingClientRect();
  let overflow = [];
  for (const el of slide.children) {
    if (el.classList.contains('foot') || el.classList.contains('bar')) continue;
    const r = el.getBoundingClientRect();
    if (r.bottom > foot.top - 12 || r.left < 40 || r.right > %d - 40) overflow.push(el.tagName + '.' + el.className);
    if (el.scrollWidth > el.clientWidth + 1) overflow.push('wide:' + el.tagName);
  }
  return {fontOk, overflow};
}""" % WIDTH


def png_size(png: bytes) -> tuple[int, int]:
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise RenderError("output is not a PNG")
    return struct.unpack(">II", png[16:24])


async def render_carousel(brand: Brand, slides, claims=(), *, browser=None) -> list[SlideImage]:
    """Render the verified slides plus the brand's closing slide. `browser`: an open Playwright browser (optional)."""
    if not slides:
        raise RenderError("no slides to render")
    total = len(slides) + 1
    items = list(slides) + [None]
    own = browser is None
    pw = None
    if own:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.launch()
    try:
        page = await browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
        out = []
        for n, slide in enumerate(items, 1):
            doc = slide_html(brand, slide, n, total)
            for scale in SCALES:
                await page.set_content(doc.replace("--s:1", f"--s:{scale}"), wait_until="load")
                qc = await page.evaluate(_QC_JS)
                if not qc["fontOk"]:
                    raise RenderError("قلم وزیرمتن بارگذاری نشد")
                if not qc["overflow"]:
                    break
            else:
                raise RenderError(f"اسلاید {n}: متن در کادر جا نشد ({', '.join(qc['overflow'])}); متن باید کوتاه‌تر شود")
            png = await page.screenshot(type="png", full_page=False)
            w, h = png_size(png)
            if (w, h) != (WIDTH, HEIGHT):
                raise RenderError(f"اسلاید {n}: اندازهٔ {w}×{h} به‌جای {WIDTH}×{HEIGHT}")
            texts = [] if slide is None else [slide.title, *slide.bullets, *(v for _, v in slide.stats)]
            shown = [c.claim_id for c in claims if any(c.rendered in t for t in texts)]
            manifest = {"template": TEMPLATE_VERSION, "scale": scale, "claims": shown,
                        "chart_sources": list(slide.chart.sources) if slide is not None and slide.chart else []}
            out.append(SlideImage(order=n, png=png, width=w, height=h, sha256=hashlib.sha256(png).hexdigest(),
                                  kind="end" if slide is None else slide.kind,
                                  chart_id=slide.chart.id if slide is not None and slide.chart else None, manifest=manifest))
        await page.close()
        return out
    finally:
        if own:
            await browser.close()
            await pw.stop()
