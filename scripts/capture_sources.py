"""Save real responses from the market-data sources, so their parsers are written from real data, not guesses.

Run on your own computer (these sites are reachable from Iran):

    python scripts/capture_sources.py

It writes captures/<date>/<name>.<ext> and captures/<date>/index.json (status, size, content type for every URL).
Zip the captures folder and send it. Nothing is posted anywhere and no login is used.

The URLs marked "candidate" are endpoints believed to exist; a 404 there is useful information, not a failure.
Add or fix URLs in TARGETS below if you know better ones.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import httpx

TARGETS = [
    # name, url, note
    ("bourse_trader_home", "https://bourse-trader.ir/", "known page; current V3 source"),
    ("tsetmc_home", "https://www.tsetmc.com/", "home page"),
    ("tsetmc_market_overview_1", "https://cdn.tsetmc.com/api/MarketData/GetMarketOverview/1", "candidate: bourse overview"),
    ("tsetmc_market_overview_2", "https://cdn.tsetmc.com/api/MarketData/GetMarketOverview/2", "candidate: farabourse overview"),
    ("tsetmc_index_b1", "https://cdn.tsetmc.com/api/Index/GetIndexB1LastAll/All/1", "candidate: index list"),
    ("tsetmc_index_financial", "https://old.tsetmc.com/tsev2/chart/data/IndexFinancial.aspx?i=32097828799138957&t=ph", "used: index history (OHLCV)"),
    ("tsetmc_index_b2", "https://cdn.tsetmc.com/api/Index/GetIndexB2History/32097828799138957", "used: index history (levels)"),
    ("tsetmc_clienttype_all", "https://cdn.tsetmc.com/api/ClientType/GetClientTypeAll", "candidate: real/legal money flow"),
    ("codal_home", "https://www.codal.ir/", "home page"),
    ("codal_search", "https://search.codal.ir/api/search/v2/q?PageNumber=1", "candidate: latest disclosures"),
    ("fipiran_home", "https://www.fipiran.com/", "home page"),
    ("tgju_home", "https://www.tgju.org/", "home page"),
    ("tgju_ajax", "https://call1.tgju.org/ajax.json", "candidate: live prices"),
    ("tradersarena_home", "https://tradersarena.ir/", "home page"),
    # stage 5: currency, gold and coin (tgju), disclosures (codal), market pages (fipiran, tradersarena)
    ("tgju_dollar_profile", "https://www.tgju.org/profile/price_dollar_rl", "dollar page"),
    ("tgju_gold18_profile", "https://www.tgju.org/profile/geram18", "18k gold page"),
    ("tgju_coin_profile", "https://www.tgju.org/profile/sekee", "Emami coin page"),
    ("tgju_api_dollar", "https://api.tgju.org/v1/market/indicator/summary-table-data/price_dollar_rl", "candidate: dollar history"),
    ("tgju_ajax_3", "https://call3.tgju.org/ajax.json", "candidate: live prices (mirror)"),
    ("codal_search_full", "https://search.codal.ir/api/search/v2/q?&Audited=true&AuditorRef=-1&Category=-1&Childs=true"
     "&CompanyState=-1&CompanyType=-1&Consolidatable=true&IsNotAudited=false&Length=-1&LetterType=-1&Mains=true"
     "&NotAudited=true&NotConsolidatable=true&PageNumber=1&Publisher=false&TracingNo=-1&search=false", "candidate: latest disclosures"),
    ("codal_report_list", "https://codal.ir/ReportList.aspx", "disclosures page"),
    ("fipiran_market", "https://www.fipiran.com/Market", "candidate: market page"),
    ("tsetmc_inst_effect", "https://cdn.tsetmc.com/api/MarketData/GetInstEffect/0/1/7", "candidate: index effect of stocks"),
    ("tsetmc_sector_overview", "https://cdn.tsetmc.com/api/ClosingPrice/GetIndustryGroupOverview", "candidate: sectors"),
    ("tradersarena_industries", "https://tradersarena.ir/industries", "candidate: industries page"),
    # stage 6: news sources of the research agent (orgs/pouyan/research.yaml)
    ("news_sena_home", "https://sena.ir/", "candidate: capital-market news (find its feed)"),
    ("news_sena_rss", "https://www.sena.ir/rss", "candidate: capital-market news feed"),
]


def news_targets() -> list[tuple[str, str, str, bool]]:
    """Every source in orgs/*/research.yaml: (name, url, note, foreign). Foreign ones go through the VPN."""
    import yaml
    out = []
    for f in sorted(Path("orgs").glob("*/research.yaml")):
        for src in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("sources") or []:
            url = src.get("url") or f"https://t.me/s/{str(src.get('channel', '')).lstrip('@')}"
            out.append((f"news_{src['id']}", url, f"research source ({f.parent.name})", bool(src.get("foreign"))))
    return out

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "fa,en;q=0.8",
}


def ext_for(content_type: str) -> str:
    ct = content_type.lower()
    if "json" in ct:
        return "json"
    if "html" in ct:
        return "html"
    if "xml" in ct:
        return "xml"
    return "bin"


async def main() -> int:
    os.chdir(Path(__file__).resolve().parent.parent)
    out = Path("captures") / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    index = []
    local = httpx.AsyncClient(headers=HEADERS, timeout=httpx.Timeout(40, connect=15), follow_redirects=True, trust_env=False)
    vpn = httpx.AsyncClient(headers=HEADERS, timeout=httpx.Timeout(40, connect=15), follow_redirects=True, trust_env=True)
    async with local, vpn:
        for name, url, note, foreign in [(*t, False) for t in TARGETS] + news_targets():
            http = vpn if foreign else local
            entry = {"name": name, "url": url, "note": note, "fetched_at": datetime.now().astimezone().isoformat()}
            try:
                r = await http.get(url)
                ctype = r.headers.get("content-type", "")
                path = out / f"{name}.{ext_for(ctype)}"
                path.write_bytes(r.content)
                entry.update(status=r.status_code, bytes=len(r.content), content_type=ctype, file=path.name, final_url=str(r.url))
                print(f"{r.status_code}  {len(r.content):>9,} bytes  {name}")
            except httpx.HTTPError as e:
                entry.update(error=f"{type(e).__name__}: {e}")
                print(f"ERR  {name}: {type(e).__name__}")
            index.append(entry)
            await asyncio.sleep(1.0)  # be polite
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    archive = Path(shutil.make_archive(str(Path.cwd() / f"captures_{out.name}"), "zip", out))
    ok = sum(1 for e in index if e.get("status") == 200)
    print(f"\n{ok} of {len(index)} answered. Everything is in one file:\n  {archive}\nAttach this file in the chat with Claude.")
    if sys.platform == "win32":
        import subprocess
        subprocess.run(["explorer", f"/select,{archive}"])  # show the file so it can be dragged into the chat
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
