#!/usr/bin/env python3
"""Cache antifragile pour le chapitre Thèse · La contrainte physique (Jancovici).

Sources auditables (live) :
  1. EIA v2 API · RWTC — Prix WTI mensuel depuis 1986 (~480+ points)
  2. World Bank API · NY.GDP.MKTP.KD — PIB mondial annuel constant 2015 USD
  3. Our World in Data · annual-co2-emissions-per-country (60 points 1965-2024)
  4. Our World in Data · primary-energy-cons (60 points 1965-2024, méthode
     substitution alignée Energy Institute / BP Statistical Review)

Datasets stables (hardcoded — sources : IEA WEO, Hall et al EROI académique,
IEA Statistical Review pour le mix 2023) conservés en fallback.

Sortie : these_energie_cache.json + .js
"""
import csv
import io
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "these_energie_cache.json"
OUT_JS   = CACHE_DIR / "these_energie_cache.js"

UA = "Mozilla/5.0 SiteCryptoFinance-TheseEnergie/2.0"
TWH_TO_EJ = 0.0036

# ════════════════════════════════════════════════════════════════
# FALLBACKS (utilisés uniquement si la source live échoue)
# ════════════════════════════════════════════════════════════════

WORLD_ENERGY_CONSUMPTION_EJ_FALLBACK = [
    (1965, 156), (1970, 199), (1975, 232), (1980, 273),
    (1985, 295), (1990, 343), (1995, 366), (2000, 405),
    (2005, 477), (2010, 535), (2015, 581), (2018, 627),
    (2019, 632), (2020, 600), (2021, 634), (2022, 645),
    (2023, 655),
]

WORLD_GDP_CONST_USD_BN_FALLBACK = [
    (1965, 14326), (1970, 17800), (1975, 22500), (1980, 27600),
    (1985, 31700), (1990, 37800), (1995, 43400), (2000, 52800),
    (2005, 63100), (2010, 72500), (2015, 82400), (2020, 82800),
    (2023, 93800),
]

WORLD_CO2_GT_FALLBACK = [
    (1965, 11.3), (1970, 14.9), (1975, 16.4), (1980, 18.7),
    (1985, 19.7), (1990, 22.6), (1995, 23.6), (2000, 25.1),
    (2005, 29.6), (2010, 33.0), (2015, 35.5), (2020, 35.2),
    (2021, 36.9), (2022, 37.5), (2023, 38.1), (2024, 38.6),
]

# Énergie primaire mondiale par source (2023, share %)
# IEA Statistical Review 2024 + BP Energy Institute
# Note : "Renouvelables" = solaire + éolien + autres (hors hydro)
WORLD_ENERGY_MIX_2023 = [
    ("Pétrole",       31.6, "fossile"),
    ("Charbon",       26.8, "fossile"),
    ("Gaz naturel",   23.1, "fossile"),
    ("Hydro",          6.7, "renouvelable"),
    ("Nucléaire",      4.0, "bas-carbone"),
    ("Renouvelables", 7.8, "renouvelable"),
]

# EROI (Energy Return On Investment) — sources académiques agrégées
# Hall, Lambert & Balogh 2014 ; King et al. 2018
EROI_BY_SOURCE = [
    ("Pétrole conventionnel — années 1930",  100, 100, "Texas 'gusher era'"),
    ("Pétrole conventionnel — années 1970",   30,  30, "pic de l'EROI fossile"),
    ("Pétrole conventionnel — 2020",          15,  30, "déclin productivité"),
    ("Sables bitumineux Athabasca",            4,  None, "extraction extrême"),
    ("Schiste US (fracking)",                  5,  None, "non conventionnel"),
    ("Gaz naturel",                           20,  30, "transition fossile"),
    ("Charbon",                               30,  None, "stable"),
    ("Nucléaire",                             14,  None, "y compris construction"),
    ("Hydraulique",                          100,  None, "très efficient"),
    ("Éolien terrestre",                      20,  None, "renouvelable"),
    ("Solaire photovoltaïque",                 8,  None, "renouvelable moderne"),
]

# Pic du pétrole conventionnel — IEA WEO data (annual)
# Référence : IEA WEO 2010 identifie 2008 comme pic conventionnel à ~70 Mb/j
# (crude oil from existing + yet-to-be-developed fields, hors schiste/sables/deepwater)
OIL_CONVENTIONAL_MB_DAY = [
    (1965, 30), (1970, 45), (1973, 55), (1975, 53), (1979, 62),
    (1980, 60), (1983, 53), (1985, 55), (1990, 60), (1995, 62),
    (2000, 65), (2003, 67), (2005, 68), (2007, 69), (2008, 70),
    (2009, 68), (2010, 69), (2012, 69), (2015, 68), (2018, 69),
    (2020, 65), (2022, 66), (2023, 67), (2024, 67),
]

WTI_OIL_FALLBACK_DATES = [
    "1986-12-01", "1990-12-01", "1995-12-01", "1998-12-01", "2000-12-01",
    "2003-12-01", "2005-12-01", "2008-06-01", "2008-12-01", "2010-12-01",
    "2012-12-01", "2014-06-01", "2014-12-01", "2016-01-01", "2017-12-01",
    "2018-10-01", "2020-04-01", "2020-12-01", "2021-12-01", "2022-06-01",
    "2022-12-01", "2023-12-01", "2024-06-01", "2024-12-01", "2025-06-01",
    "2025-12-01", "2026-04-01",
]
WTI_OIL_FALLBACK_VALUES = [16, 28, 19, 11, 26, 32, 59, 134, 41, 89, 88, 106, 53,
                           31, 60, 70, 16, 47, 75, 109, 76, 72, 78, 70, 65, 60, 100]

# ════════════════════════════════════════════════════════════════
# HTTP HELPER
# ════════════════════════════════════════════════════════════════

def http_get(url, timeout=20, max_retries=4, accept="text/csv,application/json,*/*"):
    req = Request(url, headers={"User-Agent": UA, "Accept": accept})
    last_err = None
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="ignore")
        except HTTPError as e:
            if 500 <= e.code < 600 and attempt < max_retries - 1:
                time.sleep(4 * (2 ** attempt)); continue
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(2 * (2 ** attempt)); continue
            break
        except (URLError, ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(3 * (2 ** attempt))
    raise last_err if last_err else RuntimeError("retries exhausted")


# ════════════════════════════════════════════════════════════════
# LIVE FETCHERS
# ════════════════════════════════════════════════════════════════

def fetch_wti_eia():
    """EIA v2 API — RWTC spot mensuel 1986-present.
    Pas besoin d'API key payante : DEMO_KEY suffit pour notre volume.
    """
    params = [
        ("frequency", "monthly"),
        ("data[0]", "value"),
        ("facets[series][]", "RWTC"),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "asc"),
        ("length", "5000"),
        ("api_key", "DEMO_KEY"),
    ]
    url = "https://api.eia.gov/v2/petroleum/pri/spt/data/?" + urlencode(params)
    try:
        txt = http_get(url, timeout=25, accept="application/json")
        data = json.loads(txt)["response"]["data"]
        if not data:
            return None
        dates  = [r["period"] + "-01" for r in data]  # YYYY-MM -> YYYY-MM-01
        values = [float(r["value"]) for r in data if r.get("value") is not None]
        if len(dates) != len(values):
            dates = dates[:len(values)]
        return {"dates": dates, "values": values,
                "source_url": "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=RWTC&f=M",
                "source_label": "EIA · RWTC monthly", "stale": False}
    except Exception as e:
        sys.stderr.write(f"[EIA WTI] {e}\n")
        return None


def fetch_world_gdp_worldbank():
    """World Bank API — NY.GDP.MKTP.KD (GDP constant 2015 USD), entité WLD.
    Filtre depuis 1965 pour aligner avec la base de la série énergie."""
    url = ("https://api.worldbank.org/v2/country/WLD/indicator/NY.GDP.MKTP.KD"
           "?format=json&per_page=200&date=1965:2030")
    try:
        txt = http_get(url, timeout=20, accept="application/json")
        d = json.loads(txt)
        if not isinstance(d, list) or len(d) < 2 or not d[1]:
            return None
        rows = sorted(
            [(int(r["date"]), r["value"] / 1e9) for r in d[1] if r["value"]]
        )
        if not rows:
            return None
        return [{"year": y, "gdp_bn_usd": round(v, 0)} for y, v in rows]
    except Exception as e:
        sys.stderr.write(f"[WorldBank GDP] {e}\n")
        return None


def fetch_world_co2_owid():
    """OWID · annual CO2 emissions per country (World only) — Gt CO2."""
    url = ("https://ourworldindata.org/grapher/annual-co2-emissions-per-country.csv"
           "?v=1&csvType=full&useColumnShortNames=true")
    try:
        txt = http_get(url, timeout=20, accept="text/csv")
        rows = []
        for row in csv.DictReader(io.StringIO(txt)):
            if row.get("entity") != "World":
                continue
            try:
                y = int(row["year"]); v = float(row["emissions_total"])
            except (KeyError, ValueError, TypeError):
                continue
            if y < 1965:
                continue
            rows.append({"year": y, "co2_gt": round(v / 1e9, 2)})
        rows.sort(key=lambda r: r["year"])
        return rows or None
    except Exception as e:
        sys.stderr.write(f"[OWID CO2] {e}\n")
        return None


def fetch_world_energy_owid():
    """OWID · primary-energy-cons (TWh substitution method, World)."""
    url = ("https://ourworldindata.org/grapher/primary-energy-cons.csv"
           "?v=1&csvType=full&useColumnShortNames=true")
    try:
        txt = http_get(url, timeout=20, accept="text/csv")
        rows = []
        for row in csv.DictReader(io.StringIO(txt)):
            if row.get("entity") != "World":
                continue
            try:
                y = int(row["year"])
                twh = float(row["primary_energy_consumption__twh"])
            except (KeyError, ValueError, TypeError):
                continue
            if y < 1965:
                continue
            rows.append({"year": y, "ej": round(twh * TWH_TO_EJ, 1)})
        rows.sort(key=lambda r: r["year"])
        return rows or None
    except Exception as e:
        sys.stderr.write(f"[OWID energy] {e}\n")
        return None


# ════════════════════════════════════════════════════════════════
# BUILD
# ════════════════════════════════════════════════════════════════

def build_payload():
    ok, failed = [], []

    # ── 1. WTI Oil price (EIA v2 monthly) ──
    wti = fetch_wti_eia()
    if wti and wti.get("values"):
        ok.append("EIA:RWTC")
    else:
        failed.append("EIA:RWTC")
        wti = {"dates": list(WTI_OIL_FALLBACK_DATES),
               "values": list(WTI_OIL_FALLBACK_VALUES),
               "source_url": "https://fred.stlouisfed.org/series/DCOILWTICO",
               "source_label": "fallback hardcoded", "stale": True}

    # ── 2. World GDP (World Bank) ──
    gdp = fetch_world_gdp_worldbank()
    if gdp:
        ok.append("WorldBank:NY.GDP.MKTP.KD")
    else:
        failed.append("WorldBank:NY.GDP.MKTP.KD")
        gdp = [{"year": y, "gdp_bn_usd": v} for y, v in WORLD_GDP_CONST_USD_BN_FALLBACK]

    # ── 3. World CO2 (OWID) ──
    co2 = fetch_world_co2_owid()
    if co2:
        ok.append("OWID:co2")
    else:
        failed.append("OWID:co2")
        co2 = [{"year": y, "co2_gt": v} for y, v in WORLD_CO2_GT_FALLBACK]

    # ── 4. World energy primary (OWID, méthode substitution) ──
    energy = fetch_world_energy_owid()
    if energy:
        ok.append("OWID:primary-energy")
    else:
        failed.append("OWID:primary-energy")
        energy = [{"year": y, "ej": v} for y, v in WORLD_ENERGY_CONSUMPTION_EJ_FALLBACK]

    # ── 5. Datasets stables (référence académique / IEA) ──
    mix    = [{"source": s, "pct": p, "category": c} for s, p, c in WORLD_ENERGY_MIX_2023]
    eroi   = [{"label": l, "eroi_now": n, "eroi_1970": h, "note": nt}
              for l, n, h, nt in EROI_BY_SOURCE]
    oil_conv = [{"year": y, "mb_day": v} for y, v in OIL_CONVENTIONAL_MB_DAY]

    meta = {
        "updated_at":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_at_unix": int(time.time()),
        "sources_ok":      ok,
        "sources_failed":  failed,
        "doc_version":     "2.0",
    }
    payload = {
        "meta": meta,
        "wti_oil":            wti,
        "world_energy_ej":    energy,
        "world_gdp_const":    gdp,
        "world_co2_gt":       co2,
        "world_energy_mix":   mix,
        "eroi_by_source":     eroi,
        "oil_conventional":   oil_conv,
    }
    return payload, len(ok), len(failed)


def write_outputs(payload):
    OUT_JSON.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    js = (
        f"/* these_energie_cache.js — generated {payload['meta']['updated_at']} */\n"
        f"window.__THESE_ENERGIE__ = "
        f"{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)};\n"
    )
    OUT_JS.write_text(js)
    site_dir = Path.home() / "Desktop" / "Site_Crypto_Finance"
    if site_dir.exists():
        for name in ("these_energie_cache.json", "these_energie_cache.js"):
            link = site_dir / name
            target = CACHE_DIR / name
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
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
    sys.stdout.write(
        f"[these_energie] OK · {n_ok} live, {n_fail} fallback · {dt:.1f}s\n"
    )


if __name__ == "__main__":
    main()
