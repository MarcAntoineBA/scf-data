#!/usr/bin/env python3
"""Cache antifragile pour le chapitre Thèse · Le monde post-dollar.

Sources auditables :
  1. FRED · DTWEXBGS — USD Broad Trade Weighted Index (live daily)
  2. FRED · DEXCHUS — China/US exchange rate
  3. US Treasury TIC — Major Foreign Holders monthly (CSV download)
  4. IMF COFER — Currency Composition of Official FX Reserves (hardcoded)
  5. World Gold Council — Central bank gold purchases (hardcoded)
  6. SWIFT RMB Tracker — Yuan share in cross-border payments (hardcoded)
  7. BRICS expansion timeline (hardcoded historique officiel)

Sortie : these_dollar_cache.json + .js
Lancé par scf.these_dollar.refresh (4×/jour).
"""
import csv
import io
import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "these_dollar_cache.json"
OUT_JS   = CACHE_DIR / "these_dollar_cache.js"

UA = "Mozilla/5.0 SiteCryptoFinance-TheseDollar/1.0"

# ════════════════════════════════════════════════════════════════
# HARDCODED DATASETS
# ════════════════════════════════════════════════════════════════

# IMF COFER — Part du dollar dans les réserves de change mondiales (allocated)
# Source: https://data.imf.org/regular.aspx?key=41175
# Annual % share of USD in identified official FX reserves
USD_RESERVES_SHARE = [
    # (year, share_pct)
    (1999, 71.0), (2000, 71.1), (2001, 71.5), (2002, 67.0), (2003, 65.9),
    (2004, 65.5), (2005, 66.5), (2006, 65.0), (2007, 63.9), (2008, 63.8),
    (2009, 62.2), (2010, 62.2), (2011, 62.6), (2012, 61.5), (2013, 61.2),
    (2014, 65.1), (2015, 65.7), (2016, 65.4), (2017, 62.7), (2018, 61.7),
    (2019, 60.7), (2020, 58.9), (2021, 58.8), (2022, 58.4), (2023, 58.4),
    (2024, 57.4), (2025, 57.8),
]

# Autres devises de réserve majeures (parts %)
RESERVES_BY_CURRENCY_2024 = [
    # (currency, pct, country)
    ("USD",  57.4, "USA"),
    ("EUR",  19.8, "Eurozone"),
    ("JPY",   5.6, "Japon"),
    ("GBP",   4.9, "UK"),
    ("CNY",   2.2, "Chine"),
    ("CAD",   2.6, "Canada"),
    ("AUD",   2.1, "Australie"),
    ("CHF",   0.2, "Suisse"),
    ("Autres", 5.2, "diverses"),
]

# World Gold Council — Achats nets d'or par les banques centrales (tonnes/an)
# Source: https://www.gold.org/goldhub/data/gold-demand-by-country
CB_GOLD_NET_BUYS = [
    # (year, tonnes_net)
    (2008,   235), (2009,  -34), (2010,   77), (2011,  481), (2012,  544),
    (2013,  408), (2014,  466), (2015,  580), (2016,  395), (2017,  379),
    (2018,  656), (2019,  605), (2020,  254), (2021,  450), (2022, 1082),
    (2023, 1037), (2024, 1045),
]

# Top central bank buyers of gold (cumulative since 2008, tonnes)
TOP_CB_GOLD_BUYERS = [
    # (country, tonnes_cumulative, share_pct)
    ("Russie",          1349, "Sud global"),
    ("Chine",            882, "Sud global"),
    ("Turquie",          563, "Sud global"),
    ("Inde",             480, "Sud global"),
    ("Kazakhstan",       227, "Sud global"),
    ("Pologne",          425, "OCDE"),
    ("Singapour",        140, "Sud global"),
    ("Mexique",          122, "Sud global"),
    ("Hongrie",           94, "OCDE"),
    ("Qatar",             68, "Golfe"),
]

# SWIFT RMB Tracker — Part du yuan dans les paiements internationaux (%)
# Source: https://www.swift.com/swift-rmb-tracker
YUAN_SHARE_SWIFT = [
    (2010, 0.03), (2012, 0.30), (2014, 1.59), (2016, 1.68), (2018, 1.61),
    (2020, 1.91), (2021, 2.70), (2022, 2.15), (2023, 4.69), (2024, 4.74),
    (2025, 4.85),
]

# BRICS+ expansion timeline (officiel)
# Source: https://infobrics.org/page/history-of-brics/
BRICS_EXPANSION = [
    # (year, event, members_count)
    (2001, "BRIC concept (Goldman Sachs · Jim O'Neill)", 4),
    (2009, "1er sommet officiel · Iekaterinbourg", 4),
    (2010, "Adhésion de l'Afrique du Sud · BRICS", 5),
    (2023, "Sommet Johannesburg · annonce 6 nouveaux pays", 5),
    (2024, "Adhésion : Iran, Émirats, Égypte, Éthiopie", 9),
    (2025, "Indonésie + observers étendus", 10),
]

# US Treasury TIC — China holdings approximatif (Md$)
# Source: https://ticdata.treasury.gov/Publish/mfh.txt
# Données semi-annuelles approximées (l'API n'est pas stable)
CHINA_TREASURY_HOLDINGS_BN = [
    # (year, holdings_bn_usd)
    (2008, 727), (2009, 894), (2010, 1160), (2011, 1152), (2012, 1203),
    (2013, 1270), (2014, 1244), (2015, 1246), (2016, 1058), (2017, 1184),
    (2018, 1124), (2019, 1070), (2020, 1062), (2021, 1069), (2022,  867),
    (2023,  759), (2024,  775), (2025,  790),
]

# ════════════════════════════════════════════════════════════════
# FETCH HELPERS
# ════════════════════════════════════════════════════════════════

def http_get_text(url, timeout=20, max_retries=5, accept="text/csv,*/*"):
    req = Request(url, headers={"User-Agent": UA, "Accept": accept})
    last_err = None
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="ignore")
        except HTTPError as e:
            if 500 <= e.code < 600 and attempt < max_retries - 1:
                time.sleep(5 * (2 ** attempt)); continue
            raise
        except (URLError, ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(5 * (2 ** attempt))
    raise last_err if last_err else RuntimeError("retries exhausted")


# FRED via API officielle (la version CSV graph est cassée depuis ~mai 2026)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fred_helpers import fetch_fred as fetch_fred_csv  # noqa: E402


# Fallback DXY broad trade weighted USD (mensuel) — FRED archives
DXY_FALLBACK = [
    ("2006-01-01",  99), ("2008-03-01",  86), ("2010-06-01",  95),
    ("2011-12-01",  99), ("2014-12-01", 105), ("2016-12-01", 117),
    ("2017-01-01", 117), ("2018-04-01", 109), ("2020-03-01", 119),
    ("2020-12-01", 105), ("2022-09-01", 124), ("2023-12-01", 117),
    ("2024-12-01", 122), ("2025-06-01", 119), ("2026-05-01", 121),
]

def build_payload():
    ok, failed = [], []

    # ── 1. USD Trade Weighted Index ──
    dxy = fetch_fred_csv("DTWEXBGS", start="2006-01-01")
    if dxy: ok.append("FRED:DTWEXBGS")
    else:
        failed.append("FRED:DTWEXBGS")
        dxy = {"dates":  [d for d, v in DXY_FALLBACK],
               "values": [v for d, v in DXY_FALLBACK],
               "source_url": "fallback hardcoded · FRED DTWEXBGS archives",
               "stale": True}

    # ── 2. CNY/USD exchange rate ──
    cny_usd = fetch_fred_csv("DEXCHUS", start="2000-01-01")
    if cny_usd: ok.append("FRED:DEXCHUS")
    else: failed.append("FRED:DEXCHUS")

    # ── 3. Real USD broad index (constants) ──
    rdxy = fetch_fred_csv("RTWEXBGS", start="2006-01-01")
    if rdxy: ok.append("FRED:RTWEXBGS")
    else: failed.append("FRED:RTWEXBGS")

    # ── 4. Datasets hardcodés ──
    usd_reserves   = [{"year": y, "pct": p} for y, p in USD_RESERVES_SHARE]
    reserves_mix   = [{"currency": c, "pct": p, "country": ct} for c, p, ct in RESERVES_BY_CURRENCY_2024]
    cb_gold        = [{"year": y, "tonnes_net": t} for y, t in CB_GOLD_NET_BUYS]
    top_buyers     = [{"country": c, "tonnes_cumulative": t, "region": r}
                      for c, t, r in TOP_CB_GOLD_BUYERS]
    yuan_swift     = [{"year": y, "pct": p} for y, p in YUAN_SHARE_SWIFT]
    brics_timeline = [{"year": y, "event": e, "members": m} for y, e, m in BRICS_EXPANSION]
    china_treas    = [{"year": y, "bn_usd": v} for y, v in CHINA_TREASURY_HOLDINGS_BN]

    meta = {
        "updated_at":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_at_unix": int(time.time()),
        "sources_ok":      ok,
        "sources_failed":  failed,
        "doc_version":     "1.0",
    }
    payload = {
        "meta": meta,
        "usd_dxy":           dxy,
        "cny_usd":           cny_usd,
        "real_dxy":          rdxy,
        "usd_reserves_share":  usd_reserves,
        "reserves_by_currency": reserves_mix,
        "cb_gold_net_buys":    cb_gold,
        "top_cb_gold_buyers":  top_buyers,
        "yuan_share_swift":    yuan_swift,
        "brics_timeline":      brics_timeline,
        "china_treasury":      china_treas,
    }
    return payload, len(ok), len(failed)


def write_outputs(payload):
    OUT_JSON.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    js = (
        f"/* these_dollar_cache.js — generated {payload['meta']['updated_at']} */\n"
        f"window.__THESE_DOLLAR__ = "
        f"{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)};\n"
    )
    OUT_JS.write_text(js)
    site_dir = Path.home() / "Desktop" / "Site_Crypto_Finance"
    if site_dir.exists():
        for name in ("these_dollar_cache.json", "these_dollar_cache.js"):
            link = site_dir / name
            target = CACHE_DIR / name
            try:
                if link.is_symlink() or link.exists(): link.unlink()
                link.symlink_to(target)
            except OSError as e:
                sys.stderr.write(f"[SYMLINK] {e}\n")
                shutil.copy2(target, link)


def main():
    t0 = time.time()
    try:
        payload, n_ok, n_fail = build_payload()
    except Exception as e:
        sys.stderr.write(f"[FATAL] {e}\n"); sys.exit(2)
    write_outputs(payload)
    dt = time.time() - t0
    sys.stdout.write(f"[these_dollar] OK · {n_ok} sources, {n_fail} failed · {dt:.1f}s\n")


if __name__ == "__main__":
    main()
