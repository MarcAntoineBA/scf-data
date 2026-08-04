#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_alternatives.py — Cherche un remplaçant aux sources qui refusent les IP cloud.

La sonde principale a isolé trois pertes réelles au passage sur runner :
  · Binance (451) — géoblocage des IP américaines, et les runners sont aux États-Unis ;
  · Farside (403) et Investing.com (403) — filtrage des IP de datacenter.
Cinq collecteurs en dépendent : les deux radars, les flux ETF, et les deux calendriers.

On ne remplace pas une source sans mesurer son remplaçant DANS LES MÊMES CONDITIONS.
Ce script teste donc les candidats depuis le runner lui-même, et vérifie qu'ils
renvoient bien la GRANDEUR ATTENDUE — un 200 ne prouve rien si le corps ne contient
pas la donnée cherchée. Chaque candidat déclare le champ qui doit s'y trouver.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_sources import UA, _fetch  # noqa: E402  (même client, mêmes conditions)

# (besoin, candidat, url, fragment attendu, options)
CANDIDATES = [
    # ── Remplacer Binance : taux de financement et intérêt ouvert ──────────────
    ("funding", "Binance data-api.binance.vision",
     "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT", "price", {}),
    ("funding", "Binance fapi via miroir fapi1",
     "https://fapi1.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", "lastFundingRate", {}),
    ("funding", "Bybit v5 funding",
     "https://api.bybit.com/v5/market/funding/history?category=linear&symbol=BTCUSDT&limit=2",
     "fundingRate", {}),
    ("funding", "OKX funding-rate",
     "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP", "fundingRate", {}),
    ("funding", "Coinbase Intl / Deribit (repli marché)",
     "https://www.deribit.com/api/v2/public/get_funding_rate_value"
     "?instrument_name=BTC-PERPETUAL&start_timestamp=1754179200000&end_timestamp=1754265600000",
     "result", {}),
    ("oi", "Bybit open interest",
     "https://api.bybit.com/v5/market/open-interest?category=linear&symbol=BTCUSDT"
     "&intervalTime=1h&limit=2", "openInterest", {}),
    ("oi", "OKX open interest",
     "https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP",
     "oi", {}),

    # ── Remplacer Farside : flux quotidiens des ETF ────────────────────────────
    ("etf", "Farside sans déguisement (contrôle)",
     "https://farside.co.uk/bitcoin-etf-flow-all-data/", "Total", {}),
    ("etf", "CoinGlass ETF (page publique)",
     "https://www.coinglass.com/bitcoin-etf", "ETF", dict(impersonate=True)),
    ("etf", "SoSoValue ETF api",
     "https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart", "data", {}),
    ("etf", "Fonds iShares IBIT (émetteur, source primaire)",
     "https://www.ishares.com/us/products/333011/fund/1467271812596.ajax"
     "?fileType=json&tab=all", "aladdin", {}),

    # ── Remplacer Investing.com : calendriers résultats et macro ───────────────
    ("earnings", "Nasdaq calendrier résultats",
     "https://api.nasdaq.com/api/calendar/earnings?date=2026-08-05", "rows",
     dict(headers={"User-Agent": UA, "Accept": "application/json"})),
    ("earnings", "Yahoo calendrier résultats (visualization)",
     "https://query1.finance.yahoo.com/v1/finance/visualization"
     "?lang=en-US&region=US", "finance", dict(impersonate=True)),
    ("earnings", "Financial Modeling Prep (démo)",
     "https://financialmodelingprep.com/api/v3/earning_calendar?apikey=demo", "[", {}),
    ("macrocal", "Nasdaq calendrier économique",
     "https://api.nasdaq.com/api/calendar/economicevents?date=2026-08-05", "rows",
     dict(headers={"User-Agent": UA, "Accept": "application/json"})),
    ("macrocal", "Trading Economics (flux public)",
     "https://tradingeconomics.com/calendar", "calendar", dict(impersonate=True)),
    ("macrocal", "FRED releases (dates officielles)",
     "https://api.stlouisfed.org/fred/releases/dates?file_type=json&api_key=INVALID",
     "api_key", dict(accept_status=(400,))),
]


def main():
    where = "runner GitHub" if os.environ.get("GITHUB_ACTIONS") else "Mac"
    try:
        ip = urllib.request.urlopen("https://api.ipify.org", timeout=10).read().decode()
    except Exception:
        ip = "inconnue"
    print(f"\nCandidats de remplacement — depuis : {where} (IP {ip})\n")

    results, current = [], None
    for need, label, url, expect, opts in CANDIDATES:
        if need != current:
            print(f"── {need}")
            current = need
        t0 = time.time()
        try:
            code, size, body, _ = _fetch(url, opts)
            ms = int((time.time() - t0) * 1000)
            ok = code in ((200,) + tuple(opts.get("accept_status", ()))) and expect in body
            verdict = "OK" if ok else (f"HTTP {code}" if code != 200 else "200 sans la donnée")
        except Exception as e:
            ok, code, ms, size = False, "—", int((time.time() - t0) * 1000), 0
            verdict = f"{type(e).__name__}"
        print(f"   {'✓' if ok else '✗'} {label:44} {str(code):>5} {ms:>6}ms  {verdict}")
        results.append(dict(need=need, label=label, url=url, ok=ok, code=code, verdict=verdict))

    retenus = {}
    for r in results:
        if r["ok"] and r["need"] not in retenus:
            retenus[r["need"]] = r["label"]
    print("\nRemplaçant retenu par besoin :")
    for need in ("funding", "oi", "etf", "earnings", "macrocal"):
        print(f"   {need:10} {retenus.get(need, '— AUCUN CANDIDAT NE PASSE —')}")

    out = os.environ.get("SCF_PROBE_OUT")
    if out:
        with open(out, "w") as f:
            json.dump(dict(where=where, ip=ip, results=results, retenus=retenus), f,
                      indent=1, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
