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
    ("tsetmc_clienttype_all", "https://cdn.tsetmc.com/api/ClientType/GetClientTypeAll", "candidate: real/legal money flow"),
    ("codal_home", "https://www.codal.ir/", "home page"),
    ("codal_search", "https://search.codal.ir/api/search/v2/q?PageNumber=1", "candidate: latest disclosures"),
    ("fipiran_home", "https://www.fipiran.com/", "home page"),
    ("tgju_home", "https://www.tgju.org/", "home page"),
    ("tgju_ajax", "https://call1.tgju.org/ajax.json", "candidate: live prices"),
    ("tradersarena_home", "https://tradersarena.ir/", "home page"),
]

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
    out = Path("captures") / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    index = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=httpx.Timeout(40, connect=15), follow_redirects=True, trust_env=False) as http:
        for name, url, note in TARGETS:
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
    print(f"\nSaved in {out.resolve()}  -> zip this folder and send it.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
