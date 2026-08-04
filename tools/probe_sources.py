#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_sources.py — Est-ce que les sources du site répondent depuis une IP de datacenter ?

C'est LA question qui décide de la migration hors du Mac. Une source peut très bien
répondre depuis une box française et renvoyer 403/429/451 depuis une IP Azure (celles
des runners GitHub). Trois cas connus à surveiller de près :
  · Binance renvoie 451 aux IP américaines (les runners GitHub sont aux US) ;
  · Farside et Investing.com filtrent les IP de datacenter (protection anti-bot) ;
  · CoinGecko free rationne bien plus vite sur une IP partagée par des milliers de CI.

Le script est VOLONTAIREMENT autonome (aucun import des fetchers du site) pour pouvoir
tourner à l'identique sur le Mac ET sur un runner. On le lance des deux côtés : la
comparaison des deux tableaux dit exactement ce que coûte le changement d'IP — un
tableau seul ne prouverait rien, une source peut être cassée pour tout le monde ce
jour-là.

Sortie : tableau lisible sur stdout, et le même en Markdown dans $GITHUB_STEP_SUMMARY
quand on tourne dans Actions. Code de sortie toujours 0 : c'est un diagnostic, pas un test.
"""

import concurrent.futures as cf
import json
import os
import socket
import sys
import time

TIMEOUT = 25
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# La SEC EXIGE un User-Agent nominatif avec contact, sinon 403.
SEC_UA = "Capital Antifragile research contact@capital-antifragile.example"

try:
    from curl_cffi import requests as cr
    HAS_CURL_CFFI = True
except ImportError:
    cr = None
    HAS_CURL_CFFI = False

import urllib.error
import urllib.request


# ── Catalogue des sondes ──────────────────────────────────────────────────────
# (clé, libellé, url, options)
#   impersonate=True  → passe par curl_cffi chrome120 (usurpation TLS, comme les fetchers Yahoo)
#   headers           → en-têtes spécifiques (SEC, Investing…)
#   expect            → fragment attendu dans le corps ; son absence = réponse « 200 mais vide »
#   critical=True     → sa perte casserait une page du site
PROBES = [
    # -- Prix & marchés (le cœur du site) --------------------------------------
    ("yahoo_chart", "Yahoo chart v8 (prix quotidiens)",
     "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=5d&interval=1d",
     dict(impersonate=True, expect='"chart"', critical=True)),
    ("yahoo_quote", "Yahoo quote v7 + crumb (cotations live)",
     "https://query1.finance.yahoo.com/v7/finance/quote?symbols=AAPL",
     dict(crumb=True, expect='"quoteResponse"', critical=True)),
    ("coingecko_price", "CoinGecko /simple/price",
     "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
     dict(expect="bitcoin", critical=True)),
    ("coingecko_treasury", "CoinGecko /companies/public_treasury (trésoreries)",
     "https://api.coingecko.com/api/v3/companies/public_treasury/bitcoin",
     dict(expect="total_holdings", critical=True)),
    ("stooq", "Stooq (indices, CSV)",
     "https://stooq.com/q/l/?s=%5Espx&f=sd2t2ohlcv&h&e=csv",
     dict(expect="Symbol", critical=True)),

    # -- Dépôts réglementaires --------------------------------------------------
    ("sec_submissions", "SEC EDGAR submissions (8-K trésoreries)",
     "https://data.sec.gov/submissions/CIK0001050446.json",
     dict(headers={"User-Agent": SEC_UA}, expect="filings", critical=True)),
    ("sec_www", "SEC www.sec.gov (documents de dépôt)",
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001050446&type=8-K&count=1",
     dict(headers={"User-Agent": SEC_UA}, expect="EDGAR")),

    # -- Dérivés (le gros risque : Binance bloque les IP US) --------------------
    ("binance_premium", "Binance futures premiumIndex (funding)",
     "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
     dict(expect="lastFundingRate", critical=True)),
    ("binance_oi", "Binance futures openInterestHist (OI)",
     "https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=1h&limit=2",
     dict(expect="sumOpenInterest", critical=True)),
    ("hyperliquid", "Hyperliquid /info",
     "https://api.hyperliquid.xyz/info",
     dict(method="POST", body=b'{"type":"meta"}',
          headers={"Content-Type": "application/json"}, expect="universe", critical=True)),
    ("coinglass", "Coinglass (page publique)",
     "https://www.coinglass.com/", dict()),

    # -- Flux ETF & agrégateurs protégés (anti-bot probable) -------------------
    ("farside_btc", "Farside flux ETF Bitcoin",
     "https://farside.co.uk/bitcoin-etf-flow-all-data/",
     dict(impersonate=True, expect="Total", critical=True)),
    ("investing_earnings", "Investing.com calendrier des résultats",
     "https://www.investing.com/earnings-calendar/",
     dict(impersonate=True, expect="earnings", critical=True)),
    # Pages rendues côté navigateur : le HTML initial ne contient pas la donnée.
    # On ne juge donc que l'ACCÈS (200 + volume plausible), pas le contenu.
    ("macrotrends", "Macrotrends (historique P/E)",
     "https://www.macrotrends.net/stocks/charts/AAPL/apple/pe-ratio",
     dict(impersonate=True, min_size=20_000)),
    ("stockanalysis", "StockAnalysis.com",
     "https://stockanalysis.com/stocks/aapl/", dict(impersonate=True, min_size=20_000)),
    ("companiesmarketcap", "CompaniesMarketCap",
     "https://companiesmarketcap.com/", dict(impersonate=True)),
    ("nasdaq_api", "API Nasdaq (introductions en bourse)",
     "https://api.nasdaq.com/api/ipo/calendar?date=2026-08",
     dict(headers={"User-Agent": UA, "Accept": "application/json"})),

    # -- DeFi / on-chain --------------------------------------------------------
    ("defillama", "DefiLlama /protocols",
     "https://api.llama.fi/v2/chains", dict(expect="tvl", critical=True)),
    ("stablecoins_llama", "DefiLlama stablecoins",
     "https://stablecoins.llama.fi/stablecoins?includePrices=true", dict(expect="peggedAssets")),
    ("coinmetrics", "CoinMetrics community API",
     "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
     "?assets=btc&metrics=AdrActCnt&page_size=2", dict(expect="AdrActCnt")),

    # -- Marchés de prédiction (bloqué par DNS en France : devrait DÉBLOQUER en US) --
    ("polymarket", "Polymarket gamma-api",
     "https://gamma-api.polymarket.com/events?limit=1", dict(expect="[")),

    # -- Macro & institutionnel -------------------------------------------------
    ("fred_api", "FRED API (série DGS10)",
     "https://api.stlouisfed.org/fred/series/observations"
     "?series_id=DGS10&api_key=INVALID&file_type=json",
     dict(expect="api_key", accept_status=(400,))),  # 400 = joignable, clé refusée : c'est le but
    ("fred_csv", "FRED fredgraph.csv (voie historique)",
     "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10", dict()),
    ("worldbank", "Banque mondiale API",
     "https://api.worldbank.org/v2/country/FRA/indicator/NY.GDP.MKTP.CD?format=json&per_page=2",
     dict(expect="NY.GDP")),
    ("imf", "FMI (données)", "https://www.imf.org/external/datamapper/api/v1/NGDP_RPCH",
     dict(expect="values")),
    ("eurostat", "Eurostat (ec.europa.eu)",
     "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_manr"
     "?format=JSON&lastTimePeriod=1&geo=FR", dict(expect="value")),
    ("owid", "Our World In Data",
     "https://ourworldindata.org/grapher/co-emissions-per-capita.csv", dict()),
    ("eia", "EIA (énergie, clé requise)",
     "https://api.eia.gov/v2/petroleum/pri/spt/data/?api_key=INVALID",
     dict(accept_status=(403, 400))),
    ("jodi", "JODI (pétrole)", "https://www.jodidata.org/", dict()),
    ("finra", "FINRA (margin debt)",
     "https://www.finra.org/investors/insights/margin-statistics", dict(impersonate=True)),
    ("bis", "BIS (stats)", "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_SPP/1.0/", dict()),
    ("multpl", "multpl.com (P/E Shiller)", "https://www.multpl.com/s-p-500-pe-ratio", dict()),
    ("wikidata", "Wikidata (photos, souveraineté)",
     "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q42&format=json&props=claims",
     dict(expect="claims")),

    # -- Divers ------------------------------------------------------------------
    ("hypestrat", "hypestrat.xyz (IR Hyperliquid Strategies)",
     "https://hypestrat.xyz/data/dashboard.json", dict(expect="hype")),
    ("coinacademy", "CoinAcadémie (fil news)", "https://coinacademy.fr/feed/", dict()),
    ("github_raw", "raw.githubusercontent.com", "https://raw.githubusercontent.com/", dict()),
]


def _yahoo_crumb_session():
    """Session Yahoo authentifiée (cookie + crumb), copie fidèle de `_yahoo_session()`
    des fetchers : depuis 2024 l'endpoint quote v7 renvoie 401 sans crumb. Sonder v7
    sans ce préalable ferait passer un comportement NORMAL pour un blocage d'IP."""
    if not HAS_CURL_CFFI:
        return None, None
    s = cr.Session(impersonate="chrome120")
    for warm in ("https://fc.yahoo.com", "https://finance.yahoo.com"):
        try:
            s.get(warm, timeout=15)
        except Exception:
            pass
    try:
        crumb = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).text.strip()
    except Exception:
        crumb = None
    if not crumb or len(crumb) > 40 or "<" in crumb:
        crumb = None
    return s, crumb


# On garde tout le corps (plafonné) : chercher le fragment attendu dans les seuls
# 400 premiers octets faisait passer des réponses PARFAITEMENT valides pour des pages
# de blocage (la SEC ouvre par 15 lignes de métadonnées avant « filings »).
BODY_MAX = 300_000


def _fetch(url, opts):
    """Renvoie (code_http, taille, corps, erreur)."""
    headers = dict(opts.get("headers") or {})
    headers.setdefault("User-Agent", UA)
    method = opts.get("method", "GET")
    body = opts.get("body")

    if opts.get("crumb"):
        s, crumb = _yahoo_crumb_session()
        if s is None:
            return 0, 0, "curl_cffi absent", None
        if crumb:
            url = f"{url}&crumb={crumb}"
        r = s.request(method, url, headers=headers, data=body, timeout=TIMEOUT)
        return r.status_code, len(r.content or b""), (r.text or "")[:BODY_MAX], None

    if opts.get("impersonate") and HAS_CURL_CFFI:
        r = cr.request(method, url, headers=headers, data=body,
                       impersonate="chrome120", timeout=TIMEOUT)
        return r.status_code, len(r.content or b""), (r.text or "")[:BODY_MAX], None

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            return resp.status, len(raw), raw[:BODY_MAX].decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:
            pass
        return e.code, len(raw), raw[:BODY_MAX].decode("utf-8", "replace"), None


def probe(entry):
    key, label, url, opts = entry
    t0 = time.time()
    try:
        code, size, head, _ = _fetch(url, opts)
        ms = int((time.time() - t0) * 1000)
    except Exception as e:  # DNS, TLS, timeout, reset…
        return dict(key=key, label=label, ok=False, code="—", ms=int((time.time() - t0) * 1000),
                    size=0, verdict="INJOIGNABLE", detail=f"{type(e).__name__}: {e}"[:120],
                    critical=opts.get("critical", False))

    ok_codes = (200,) + tuple(opts.get("accept_status", ()))
    expect = opts.get("expect")

    if code in ok_codes:
        min_size = opts.get("min_size", 0)
        if expect and expect not in head:
            # 200 mais le contenu attendu manque : page de blocage ou réponse vide.
            verdict, ok = "VIDE/BLOQUÉ", False
            detail = f"200 sans « {expect} » — {head[:80]!r}"
        elif min_size and size < min_size:
            verdict, ok = "TRONQUÉ", False
            detail = f"200 mais {size} o < {min_size} o attendus (page de blocage ?)"
        else:
            verdict, ok, detail = "OK", True, ""
    elif code in (401, 403):
        verdict, ok, detail = "REFUSÉ (403)", False, head[:90]
    elif code == 429:
        verdict, ok, detail = "QUOTA (429)", False, head[:90]
    elif code == 451:
        verdict, ok, detail = "GÉOBLOQUÉ (451)", False, head[:90]
    else:
        verdict, ok, detail = f"HTTP {code}", False, head[:90]

    return dict(key=key, label=label, ok=ok, code=code, ms=ms, size=size,
                verdict=verdict, detail=detail, critical=opts.get("critical", False))


def main():
    where = os.environ.get("SCF_PROBE_LABEL") or (
        "runner GitHub" if os.environ.get("GITHUB_ACTIONS") else "Mac (IP résidentielle)")
    try:
        ip = urllib.request.urlopen("https://api.ipify.org", timeout=10).read().decode()
    except Exception:
        ip = "inconnue"

    print(f"\nSonde des sources — depuis : {where}")
    print(f"IP publique : {ip}   ·   curl_cffi : {'oui' if HAS_CURL_CFFI else 'NON (usurpation TLS indisponible)'}")
    print(f"Hôte : {socket.gethostname()}   ·   {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n")

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(probe, PROBES))

    w = max(len(r["label"]) for r in results)
    print(f"{'':2} {'SOURCE'.ljust(w)}  {'CODE':>5}  {'MS':>6}  {'TAILLE':>8}  VERDICT")
    print("─" * (w + 44))
    for r in sorted(results, key=lambda r: (r["ok"], not r["critical"], r["label"])):
        mark = "✓" if r["ok"] else ("✗" if r["critical"] else "!")
        size = f"{r['size']/1024:.0f} Ko" if r["size"] else "—"
        print(f"{mark:2} {r['label'].ljust(w)}  {str(r['code']):>5}  {r['ms']:>6}  {size:>8}  {r['verdict']}")
        if r["detail"]:
            print(f"{'':2} {'':{w}}  └─ {r['detail']}")

    ko = [r for r in results if not r["ok"]]
    ko_crit = [r for r in ko if r["critical"]]
    print(f"\n{len(results) - len(ko)}/{len(results)} sources OK · "
          f"{len(ko)} en échec dont {len(ko_crit)} CRITIQUES")
    if ko_crit:
        print("Critiques en échec : " + ", ".join(r["key"] for r in ko_crit))

    out = os.environ.get("SCF_PROBE_OUT")
    if out:
        with open(out, "w") as f:
            json.dump(dict(where=where, ip=ip, ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           curl_cffi=HAS_CURL_CFFI, results=results), f, indent=1)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"## Sonde des sources — {where}\n\nIP `{ip}` · curl_cffi "
                    f"`{HAS_CURL_CFFI}` · {len(results)-len(ko)}/{len(results)} OK\n\n")
            f.write("| | Source | Code | Verdict |\n|---|---|---|---|\n")
            for r in sorted(results, key=lambda r: (r["ok"], not r["critical"], r["label"])):
                mark = "✓" if r["ok"] else ("**✗**" if r["critical"] else "!")
                f.write(f"| {mark} | {r['label']} | {r['code']} | {r['verdict']} |\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
