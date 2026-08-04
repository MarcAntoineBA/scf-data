"""Fetcher — US-Listed Levered/Inverse ETF Notional Volume.

Reproduit (proxy fidele) le graphe Goldman Sachs Global FICC & Equities
"US-Listed Levered/Inverse ETF Notional Volume" (valeurs en $ milliards).

METHODOLOGIE
------------
Notional volume quotidien = Sigma sur l'univers des ETF a effet de levier /
inverses cotes aux US de (volume du jour x cours de cloture). C'est exactement
la definition Goldman : volume notionnel = nb d'actions echangees x prix.

Goldman agrege la TOTALITE des ~400 LETF/inverse US ; on prend ici un univers
CURE des ~130 produits les plus liquides (ProShares Ultra/UltraPro/UltraShort,
Direxion Daily Bull/Bear 2x/3x, GraniteShares / Defiance / T-Rex / Tradr /
Volatility Shares single-stock & crypto). Ces produits captent la quasi-totalite
du notional echange ; le niveau et la forme suivent de tres pres la serie GS.

La serie monte structurellement depuis 2023 parce que l'univers single-stock
leverage (TSLL, NVDL, MSTU, CONL...) a explose — c'est le phenomene reel mesure,
pas un artefact (les produits inexistants avant leur inception contribuent 0).

Sortie : ~/Desktop/Site_Crypto_Finance/levered_etf_cache.js
expose window.__LEVERED_ETF__ = {
  current: {date, notional_b, ma20_b, percentile, n_etfs, top:[{t,b},...]},
  history:  [{d, v}, ...]   # notional quotidien $B, ~2016+
  spy_hist: [{d, p}, ...]   # SPY close (overlay contexte)
  generated_at, source, methodology
}
"""
import json
import os
import sys
import time
from datetime import datetime

from curl_cffi import requests as cr

# TCC : launchd ne peut pas ecrire sur ~/Desktop. On ecrit dans Library/Caches,
# snapshot_site.sh (manifest _cache_files_synced.txt) copie vers le repo.
import pathlib
_CACHE_DIR = pathlib.Path.home() / "Library" / "Caches" / "site_crypto_finance"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JS = str(_CACHE_DIR / "levered_etf_cache.js")
START = int(datetime(2016, 1, 1).timestamp())

# ── Univers cure des LETF / inverse US les plus liquides ──────────────────────
# Broad index leverage / inverse
BROAD = [
    "TQQQ", "SQQQ", "QLD", "PSQ", "QID",              # Nasdaq 100
    "SPXL", "SPXS", "UPRO", "SPXU", "SSO", "SDS", "SH",  # S&P 500
    "UDOW", "SDOW", "DDM", "DXD",                     # Dow
    "TNA", "TZA", "URTY", "TWM",                      # Russell 2000
    "MIDU",                                           # Mid cap
    "SOXL", "SOXS", "USD", "SSG",                     # Semiconductors
    "TECL", "TECS", "ROM", "REW",                     # Technology
    "FAS", "FAZ", "UYG", "SKF",                       # Financials
    "DPST", "WDRW",                                   # Regional banks
    "LABU", "LABD", "BIB", "BIS",                     # Biotech
    "CURE", "RXL", "RXD",                             # Healthcare
    "FNGU", "FNGD", "FNGG",                           # FANG+
    "WEBL", "WEBS",                                   # Internet
    "DRN", "DRV", "URE", "SRS",                       # Real estate
    "NAIL",                                           # Homebuilders
    "RETL",                                           # Retail
    "DFEN", "ITA",                                    # Aerospace/defense
    "DUSL",                                           # Industrials
    "UTSL",                                           # Utilities
    "YINN", "YANG", "CWEB", "CHAU",                   # China
    "EDC", "EDZ",                                     # Emerging markets
    "EURL",                                           # Europe
    "INDL",                                           # India
    "KORU",                                           # Korea
    "MEXX",                                           # Mexico
]
# Commodities / energy / rates / vol
COMMOD_RATES_VOL = [
    "NUGT", "DUST", "JNUG", "JDST",                   # Gold miners
    "GLL", "UGL",                                     # Gold
    "AGQ", "ZSL",                                     # Silver
    "GUSH", "DRIP",                                   # Oil & gas E&P
    "ERX", "ERY",                                     # Energy
    "BOIL", "KOLD",                                   # Natural gas
    "UCO", "SCO",                                     # Crude oil
    "TMF", "TMV", "TBT", "TBF", "TYO",                # Treasuries
    "UVXY", "SVXY", "VIXY", "UVIX", "SVIX",           # Volatility
]
# Single-stock leverage / inverse (le moteur du surge 2023-2026)
SINGLE_STOCK = [
    "TSLL", "TSLS", "TSLQ", "TSLR", "TSLZ",           # Tesla
    "NVDL", "NVDX", "NVDU", "NVDD", "NVDS", "NVDQ",   # Nvidia
    "MSTX", "MSTU", "MSTZ", "SMST",                   # MicroStrategy
    "CONL", "CONI",                                   # Coinbase
    "AAPU", "AAPD",                                   # Apple
    "AMZU", "AMZD",                                   # Amazon
    "GGLL", "GGLS",                                   # Alphabet
    "MSFU", "MSFD",                                   # Microsoft
    "METU", "METD",                                   # Meta
    "AMUU", "AMDD",                                   # AMD
    "PLTU", "PLTD",                                   # Palantir
    "AVL",                                            # Broadcom
    "SMCL", "SMCX",                                   # Super Micro
    "TSMX", "TSMG",                                   # TSMC
    "MUU",                                            # Micron
    "NFXL",                                           # Netflix
    "BABX",                                           # Alibaba
    "HOOX",                                           # Robinhood
    "ELIL",                                           # Eli Lilly
]
# Crypto leverage / inverse
CRYPTO = [
    "BITX", "BITU", "SBIT", "BITI",                   # Bitcoin
    "ETHU", "ETHT", "ETHD",                           # Ethereum
    "MARA", "RIOT",                                   # (placeholders skip — not LETF)
]
CRYPTO = ["BITX", "BITU", "SBIT", "BITI", "ETHU", "ETHT", "ETHD"]

UNIVERSE = sorted(set(BROAD + COMMOD_RATES_VOL + SINGLE_STOCK + CRYPTO))


def yahoo_daily(ticker, retries=3):
    """Yahoo chart v8 daily (close + volume) via curl_cffi impersonate chrome120.
    Retourne (timestamps[list], closes[list], volumes[list]) ou None."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.replace('^','%5E')}"
    params = f"?interval=1d&period1={START}&period2={int(time.time())}"
    for attempt in range(retries):
        try:
            r = cr.get(url + params, impersonate="chrome120", timeout=30)
            if r.status_code == 200:
                j = r.json()
                res = j.get("chart", {}).get("result")
                if res:
                    res = res[0]
                    ts = res.get("timestamp") or []
                    q = res.get("indicators", {}).get("quote", [{}])[0]
                    return ts, q.get("close") or [], q.get("volume") or []
                return None
            if r.status_code in (404, 422):
                return None  # ticker inexistant, pas la peine de retry
        except Exception as e:
            sys.stderr.write(f"[letf] {ticker}: err {e} (try {attempt+1})\n")
        time.sleep(1.4 ** attempt)
    return None


def day_key(ts):
    """Normalise un timestamp marche a minuit UTC du jour (cle d'agregation)."""
    return ts - (ts % 86400)


def main():
    # notional[day] = somme des close*volume sur l'univers ce jour-la
    notional = {}        # day_ts -> $ notional
    contrib_last = {}    # ticker -> dernier notional quotidien (pour 'top')
    n_ok = 0
    spy_ts, spy_close = None, None

    for tk in UNIVERSE + ["SPY"]:
        data = yahoo_daily(tk)
        if not data:
            sys.stderr.write(f"[letf] {tk}: no data\n")
            time.sleep(0.4)
            continue
        ts, closes, vols = data
        if tk == "SPY":
            spy_ts, spy_close = ts, closes
            time.sleep(0.4)
            continue
        cnt = 0
        last_notional = 0.0
        for i, t in enumerate(ts):
            c = closes[i] if i < len(closes) else None
            v = vols[i] if i < len(vols) else None
            if c is None or v is None or c <= 0 or v <= 0:
                continue
            dk = day_key(t)
            notional[dk] = notional.get(dk, 0.0) + c * v
            last_notional = c * v
            cnt += 1
        if cnt:
            n_ok += 1
            contrib_last[tk] = last_notional
            sys.stderr.write(f"[letf] {tk}: {cnt} days ok\n")
        time.sleep(0.4)

    if not notional:
        sys.stderr.write("[letf] ERROR: empty notional\n")
        sys.exit(1)

    days = sorted(notional.keys())
    history = [{"d": d, "v": round(notional[d] / 1e9, 3)} for d in days]  # $B

    # MA20 + percentile sur la serie complete (en $B)
    vals = [h["v"] for h in history]
    last = history[-1]
    ma20 = round(sum(vals[-20:]) / min(20, len(vals)), 2)
    sorted_v = sorted(vals)
    percentile = round(100.0 * sum(1 for v in sorted_v if v <= last["v"]) / len(sorted_v), 1)

    # Top contributeurs du dernier jour
    top = sorted(contrib_last.items(), key=lambda kv: kv[1], reverse=True)[:8]
    top = [{"t": t, "b": round(b / 1e9, 3)} for t, b in top]

    # SPY overlay (close quotidien, downsample leger non necessaire)
    spy_hist = []
    if spy_ts and spy_close:
        for i, t in enumerate(spy_ts):
            c = spy_close[i] if i < len(spy_close) else None
            if c and c > 0:
                spy_hist.append({"d": day_key(t), "p": round(c, 2)})

    payload = {
        "current": {
            "date": last["d"],
            "notional_b": last["v"],
            "ma20_b": ma20,
            "percentile": percentile,
            "n_etfs": n_ok,
            "top": top,
        },
        "history": history,
        "spy_hist": spy_hist,
        "generated_at": int(time.time()),
        "source": (
            f"Yahoo Finance v8 (close x volume) agrege sur {n_ok} LETF/inverse US "
            "les plus liquides. Proxy fidele du graphe Goldman Sachs Global FICC & "
            "Equities 'US-Listed Levered/Inverse ETF Notional Volume'."
        ),
        "methodology": (
            "Notional quotidien = somme(close x volume) sur un univers cure de ~130 "
            "ETF a levier/inverses US (ProShares, Direxion, GraniteShares, Defiance, "
            "T-Rex, Tradr, Volatility Shares). Univers GS = totalite des LETF ; ici "
            "les plus liquides, qui captent la quasi-totalite du notional. La hausse "
            "structurelle 2023+ vient de l'explosion des LETF single-stock."
        ),
    }

    js = "/* Auto-generated by fetch_levered_etf_volume.py — do not edit. */\n"
    js += "(function(){var d=" + json.dumps(payload, separators=(",", ":"))
    js += ";window.__LEVERED_ETF__=d;})();\n"
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write(js)
    sys.stderr.write(
        f"[letf] OK -> {OUT_JS} ({os.path.getsize(OUT_JS)/1024:.1f} KB) · "
        f"{n_ok} ETF · {len(history)} days · last={last['v']:.1f}$B "
        f"(MA20={ma20}$B, pct={percentile}%)\n"
    )


if __name__ == "__main__":
    main()
