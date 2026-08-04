#!/usr/bin/env python3
"""Cache antifragile pour le chapitre Thèse · Le mur de la dette.

Sources auditables (aucune clé API requise) :
  * FRED — CSV public fredgraph (US + zone euro + Japon + UK + Allemagne + Italie)
  * US Treasury Fiscal Data — debt to the penny (daily, prod)
  * Yahoo Finance v8 — DGS-like fallback pour rates (overlap diagnostic)
  * Insee BDM SDMX — dette publique France trimestrielle (Maastricht)

Écrit two outputs:
  ~/Library/Caches/site_crypto_finance/these_dette_cache.json
  ~/Library/Caches/site_crypto_finance/these_dette_cache.js
(le JS injecte window.__THESE_DETTE__ pour le Rmd côté navigateur).

Lancé par scf.these_dette.refresh (StartInterval 21600 = 6h, RunAtLoad).
Robustesse: retry exponentiel sur erreurs DNS / timeout / 5xx ; tout source qui
rate ne casse pas les autres ; meta.sources_failed liste les sources HS ;
si TOUT rate, conserve l'ancien cache au lieu d'écraser.
"""
import csv
import io
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "these_dette_cache.json"
OUT_JS   = CACHE_DIR / "these_dette_cache.js"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) SiteCryptoFinance-These/1.0"
START_DATE = "1995-01-01"

# ─── Series catalog ───────────────────────────────────────────────────
# Each entry: FRED id → (display label, ISO country, units enum)
# units enum: "pct_gdp" | "pct" | "level_bn_lcu" | "yield_pct"
FRED_DEBT_GDP = {
    # USA — Federal Debt: Total Public Debt as Percent of GDP (Q, ultra fresh)
    "GFDEGDQ188S": ("États-Unis",  "US",  "pct_gdp"),
    # Royaume-Uni (annuel OCDE via FRED — Eurostat ne couvre plus le UK)
    "GGGDTAGBA188N": ("Royaume-Uni","GB",  "pct_gdp"),
    # Japon (annuel OCDE via FRED)
    "GGGDTAJPA188N": ("Japon",      "JP",  "pct_gdp"),
}

FRED_RATES = {
    "DGS10":   ("US 10Y Treasury yield",     "pct"),
    "DGS2":    ("US 2Y Treasury yield",      "pct"),
    "DGS30":   ("US 30Y Treasury yield",     "pct"),
    "IRLTLT01FRM156N": ("France 10Y benchmark yield", "pct"),
    "IRLTLT01DEM156N": ("Allemagne 10Y bund yield",   "pct"),
    "IRLTLT01ITM156N": ("Italie 10Y BTP yield",       "pct"),
    "IRLTLT01JPM156N": ("Japon 10Y JGB yield",        "pct"),
}

# Charge des intérêts US — interest outlays as % of GDP (annual, BEA via FRED)
FRED_INTEREST = {
    # Federal government interest payments as % of GDP (annual)
    "FYOIGDA188S": ("US Interest Outlays / GDP", "pct"),
    # Federal government current expenditures: Interest payments (qtl, $bn SAAR)
    "A091RC1Q027SBEA": ("US Interest payments level", "level_bn"),
    # Federal receipts, total (qtl, $bn SAAR) — for context
    "FGRECPT": ("US Federal current tax receipts", "level_bn"),
}

# Croissance nominale pour le différentiel r-g (boule de neige)
FRED_GROWTH = {
    "GDP":   ("US Nominal GDP (qtl SAAR)",        "level_bn"),
    "CLVMNACSCAB1GQFR": ("France GDP volume",      "level_bn"),
}

# US Treasury Fiscal Data — debt to the penny (daily, depuis 1993)
TREASURY_DEBT_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/"
    "accounting/od/debt_to_penny"
    "?fields=record_date,tot_pub_debt_out_amt"
    "&sort=-record_date&page[size]=10000"
)

# Eurostat JSON-stat — Quarterly Government Debt (gov_10q_ggdebt)
# Documentation: https://ec.europa.eu/eurostat/databrowser/view/gov_10q_ggdebt
# Plus à jour et plus granulaire que les ré-exports FRED (T4 2025 vs ~2023-2024)
EUROSTAT_BASE = (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
    "gov_10q_ggdebt?format=JSON&sector=S13&na_item=GD"
)
# Pays UE prioritaires : FR + Allemagne + Italie + Espagne (comparables UE)
EUROSTAT_GEO = ["FR", "DE", "IT", "ES", "EA20"]
EUROSTAT_GEO_LABEL = {
    "FR": "France", "DE": "Allemagne", "IT": "Italie",
    "ES": "Espagne", "EA20": "Zone euro (20)",
}


def http_get_text(url, timeout=20, max_retries=5, accept="text/csv,*/*"):
    req = Request(url, headers={"User-Agent": UA, "Accept": accept})
    last_err = None
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="ignore")
        except HTTPError as e:
            # 5xx → retry, 4xx → fatal
            if 500 <= e.code < 600 and attempt < max_retries - 1:
                wait = 5 * (2 ** attempt)
                sys.stderr.write(f"[HTTP {e.code}] retry {attempt+1}/{max_retries} after {wait}s {url}\n")
                time.sleep(wait)
                continue
            raise
        except (URLError, ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
            wait = 5 * (2 ** attempt)
            sys.stderr.write(f"[NET] retry {attempt+1}/{max_retries} after {wait}s {url}: {e}\n")
            time.sleep(wait)
            continue
    raise last_err if last_err else RuntimeError("http_get_text exhausted retries")


def fetch_fred_csv(series_id):
    """Récupère une série FRED via l'API officielle (clé dans _fred_helpers).

    L'ancien endpoint public fredgraph.csv est instable et bloque les longues
    séries quotidiennes (DGS10/DGS2/DGS30 échouaient systématiquement, ce qui
    cassait le graphe des taux ET le diagnostic coût-vs-croissance r-g). L'API
    JSON officielle les sert sans problème. Fallback sur fredgraph.csv si le
    helper est introuvable, pour rester autonome.
    """
    try:
        from _fred_helpers import fetch_fred
        s = fetch_fred(series_id, start=START_DATE)
        if s and s.get("dates"):
            return {"dates": s["dates"], "values": s["values"]}
        return None
    except ImportError:
        pass
    # Fallback legacy : fredgraph.csv (peut échouer sur les séries quotidiennes)
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={START_DATE}&fam=lin"
    try:
        txt = http_get_text(url, timeout=20)
    except Exception as e:
        sys.stderr.write(f"[FRED {series_id}] {e}\n")
        return None
    dates, values = [], []
    reader = csv.reader(io.StringIO(txt))
    header = next(reader, None)
    if not header:
        return None
    for row in reader:
        if len(row) < 2:
            continue
        d = row[0].strip()
        v = row[1].strip()
        if v in ("", ".", "NA"):
            continue
        try:
            values.append(float(v))
            dates.append(d)
        except ValueError:
            continue
    if not dates:
        return None
    return {"dates": dates, "values": values}


def fetch_us_treasury_debt():
    """Pagination via /v2 Fiscal Data — daily debt-to-the-penny since 1993.
    On agrège au dernier point de chaque trimestre pour la série courbe."""
    try:
        # On veut tout l'historique mais l'API page-size max ~10k, donc on fait
        # une seule requête sur ordre desc, ce qui couvre largement 1993→today.
        txt = http_get_text(TREASURY_DEBT_URL, timeout=30, accept="application/json")
        payload = json.loads(txt)
        data = payload.get("data", [])
        # data: list of {"record_date":"YYYY-MM-DD","tot_pub_debt_out_amt":"..."}
        recs = []
        for row in data:
            try:
                d = row["record_date"]
                v = float(row["tot_pub_debt_out_amt"])
                recs.append((d, v))
            except (KeyError, ValueError, TypeError):
                continue
        recs.sort()
        if not recs:
            return None
        # Décimer en fin-de-trimestre pour un graphique propre
        quarterly = {}
        for d, v in recs:
            q_key = d[:7]  # YYYY-MM
            quarterly[q_key] = (d, v)  # garde le dernier point du mois
        q_recs = sorted(quarterly.values())
        return {
            "dates":  [r[0] for r in q_recs],
            "values": [r[1] for r in q_recs],
            "latest_date":  recs[-1][0],
            "latest_value": recs[-1][1],
        }
    except Exception as e:
        sys.stderr.write(f"[TREASURY] {e}\n")
        return None


def fetch_imf_weo_debt_gdp(country_iso3_list):
    """IMF DataMapper API — General Government Gross Debt as % of GDP (GGXWDG_NGDP).
    Données annuelles 1980-2031 incluant projections IMF.
    Retourne dict {ISO3: {years: [...], values: [...], cutoff_observed_year: 2024}}.
    Le cutoff_observed indique l'année à partir de laquelle les valeurs sont
    des projections IMF (octobre/avril du WEO en cours)."""
    url = (
        "https://www.imf.org/external/datamapper/api/v1/GGXWDG_NGDP/"
        + "/".join(country_iso3_list)
    )
    try:
        txt = http_get_text(url, timeout=25, accept="application/json")
        d = json.loads(txt)
    except Exception as e:
        sys.stderr.write(f"[IMF WEO {country_iso3_list}] {e}\n")
        return None
    out = {}
    vals_dict = d.get("values", {}).get("GGXWDG_NGDP", {})
    # Cutoff approximatif des données observées : WEO de mai 2026 a données
    # observées jusqu'en 2024, projections à partir de 2025.
    CUTOFF = 2024
    # On filtre pour ne garder que les pays demandés (IMF retourne tout sinon)
    requested = set(country_iso3_list)
    for iso, year_dict in vals_dict.items():
        if iso not in requested:
            continue
        if not year_dict:
            continue
        years = sorted([int(y) for y in year_dict.keys()
                        if year_dict[y] is not None])
        if not years:
            continue
        out[iso] = {
            "years": years,
            "values": [year_dict[str(y)] for y in years],
            "cutoff_observed_year": CUTOFF,
            "source": "IMF WEO",
            "source_id": "GGXWDG_NGDP",
            "source_url": "https://www.imf.org/external/datamapper/datasets/WEO",
        }
    return out


def fetch_eurostat_debt(geo, unit):
    """Eurostat JSON-stat : dette publique trimestrielle Maastricht (gov_10q_ggdebt).

    geo : "FR", "DE", "IT", "ES", "EA20" (Zone euro à 20), etc.
    unit: "MIO_EUR" (niveau en millions d'euros) ou "PC_GDP" (% PIB)
    Retourne {dates: ["YYYY-MM-DD"], values: [float]} ou None.
    """
    url = f"{EUROSTAT_BASE}&geo={geo}&unit={unit}"
    try:
        txt = http_get_text(url, timeout=25, accept="application/json")
        d = json.loads(txt)
    except Exception as e:
        sys.stderr.write(f"[EUROSTAT gov_10q_ggdebt {geo}/{unit}] {e}\n")
        return None
    time_dim = d.get("dimension", {}).get("time", {})
    cat = time_dim.get("category", {})
    indices = cat.get("index", {})
    values = d.get("value", {})
    if not indices:
        sys.stderr.write(f"[EUROSTAT {geo}/{unit}] empty time dimension\n")
        return None
    QUARTER_END = {"Q1": "03-31", "Q2": "06-30", "Q3": "09-30", "Q4": "12-31"}
    recs = []
    for time_label, idx in indices.items():
        v = values.get(str(idx))
        if v is None:
            continue
        if "-Q" in time_label:
            y, q = time_label.split("-Q")
            month_end = QUARTER_END.get(f"Q{q}")
            if not month_end:
                continue
            date_iso = f"{y}-{month_end}"
        else:
            date_iso = time_label
        try:
            recs.append((date_iso, float(v)))
        except (ValueError, TypeError):
            continue
    if not recs:
        return None
    recs.sort()
    return {
        "dates":        [r[0] for r in recs],
        "values":       [r[1] for r in recs],
        "latest_date":  recs[-1][0],
        "latest_value": recs[-1][1],
        "source_url":   f"https://ec.europa.eu/eurostat/databrowser/view/gov_10q_ggdebt?geo={geo}&unit={unit}",
    }


def latest_point(series, default=None):
    """Helper: dernier point non-nul d'une série {dates, values}."""
    if not series or not series.get("values"):
        return default
    return series["values"][-1]


def latest_pair(series, default=None):
    """(date, value) du dernier point."""
    if not series or not series.get("values"):
        return default
    return series["dates"][-1], series["values"][-1]


def build_payload():
    failed = []
    ok = []

    # ─── 1. Debt/GDP series (multi-pays) ──
    debt_gdp_series = {}
    # 1a) FRED pour US (trimestriel BEA), UK (annuel OCDE), JP (annuel OCDE)
    for fred_id, (label, iso, _u) in FRED_DEBT_GDP.items():
        s = fetch_fred_csv(fred_id)
        if s is None:
            failed.append(f"FRED:{fred_id}")
            continue
        debt_gdp_series[iso] = {
            "label": label,
            "source": "FRED",
            "source_id": fred_id,
            "source_url": f"https://fred.stlouisfed.org/series/{fred_id}",
            "freq": "Q" if fred_id == "GFDEGDQ188S" else "A",
            **s,
        }
        ok.append(f"FRED:{fred_id}")

    # 1b) Eurostat pour FR/DE/IT/ES/EA20 — trimestriel et beaucoup plus frais
    eurostat_debt_levels = {}
    for geo in EUROSTAT_GEO:
        s_pct = fetch_eurostat_debt(geo, "PC_GDP")
        if s_pct is not None:
            debt_gdp_series[geo] = {
                "label": EUROSTAT_GEO_LABEL[geo],
                "source": "Eurostat",
                "source_id": f"gov_10q_ggdebt · {geo} · PC_GDP",
                "source_url": s_pct.pop("source_url", None),
                "freq": "Q",
                "dates":  s_pct["dates"],
                "values": s_pct["values"],
            }
            ok.append(f"EUROSTAT:{geo}/PC_GDP")
        else:
            failed.append(f"EUROSTAT:{geo}/PC_GDP")

        s_lvl = fetch_eurostat_debt(geo, "MIO_EUR")
        if s_lvl is not None:
            eurostat_debt_levels[geo] = {
                "label": EUROSTAT_GEO_LABEL[geo],
                "source": "Eurostat",
                "source_id": f"gov_10q_ggdebt · {geo} · MIO_EUR",
                "source_url": s_lvl.pop("source_url", None),
                "freq": "Q",
                "unit": "M€",
                "dates":  s_lvl["dates"],
                "values": s_lvl["values"],
                "latest_date":  s_lvl.get("latest_date"),
                "latest_value": s_lvl.get("latest_value"),
            }
            ok.append(f"EUROSTAT:{geo}/MIO_EUR")
        else:
            failed.append(f"EUROSTAT:{geo}/MIO_EUR")

    # ─── 2. Yields (rates) — daily/monthly ──
    yields_series = {}
    for fred_id, (label, _u) in FRED_RATES.items():
        s = fetch_fred_csv(fred_id)
        if s is None:
            failed.append(f"FRED:{fred_id}")
            continue
        yields_series[fred_id] = {
            "label": label,
            "fred_id": fred_id,
            "source_url": f"https://fred.stlouisfed.org/series/{fred_id}",
            **s,
        }
        ok.append(f"FRED:{fred_id}")

    # ─── 3. Charge d'intérêts US ──
    interest_series = {}
    for fred_id, (label, _u) in FRED_INTEREST.items():
        s = fetch_fred_csv(fred_id)
        if s is None:
            failed.append(f"FRED:{fred_id}")
            continue
        interest_series[fred_id] = {
            "label": label,
            "fred_id": fred_id,
            "source_url": f"https://fred.stlouisfed.org/series/{fred_id}",
            **s,
        }
        ok.append(f"FRED:{fred_id}")

    # ─── 4. Croissance nominale (pour r-g) ──
    growth_series = {}
    for fred_id, (label, _u) in FRED_GROWTH.items():
        s = fetch_fred_csv(fred_id)
        if s is None:
            failed.append(f"FRED:{fred_id}")
            continue
        growth_series[fred_id] = {
            "label": label,
            "fred_id": fred_id,
            "source_url": f"https://fred.stlouisfed.org/series/{fred_id}",
            **s,
        }
        ok.append(f"FRED:{fred_id}")

    # ─── 4d. IMF WEO General Government Gross Debt (annuel 1980-2031, projections incl.)
    imf_iso = ["JPN", "USA", "GBR", "FRA", "DEU", "ITA", "ESP"]
    imf_weo = fetch_imf_weo_debt_gdp(imf_iso)
    if imf_weo:
        for iso in imf_iso:
            if iso in imf_weo:
                ok.append(f"IMF:WEO:{iso}")
            else:
                failed.append(f"IMF:WEO:{iso}")
    else:
        for iso in imf_iso:
            failed.append(f"IMF:WEO:{iso}")
        imf_weo = {}

    # ─── 5. US Treasury debt to the penny ──
    treasury = fetch_us_treasury_debt()
    if treasury:
        ok.append("TREASURY:debt_to_penny")
    else:
        failed.append("TREASURY:debt_to_penny")

    # ─── 7. KPI synthétiques (dérivés des séries au-dessus) ──
    kpi = {}

    # FR Debt/GDP : dernier point (Eurostat trimestriel)
    if "FR" in debt_gdp_series:
        d, v = latest_pair(debt_gdp_series["FR"], (None, None))
        kpi["fr_debt_gdp_pct"]  = round(v, 1) if v else None
        kpi["fr_debt_gdp_date"] = d

    # FR debt total (Eurostat MIO_EUR, converti en Md€)
    if "FR" in eurostat_debt_levels:
        lvl = eurostat_debt_levels["FR"]
        if lvl.get("latest_value"):
            # M€ → Md€
            kpi["fr_debt_total_bn_eur"] = round(lvl["latest_value"] / 1000)
            kpi["fr_debt_date"]         = lvl["latest_date"]

    # Eurozone aggregate Debt/GDP
    if "EA20" in debt_gdp_series:
        d, v = latest_pair(debt_gdp_series["EA20"], (None, None))
        kpi["ea_debt_gdp_pct"]  = round(v, 1) if v else None
        kpi["ea_debt_gdp_date"] = d

    # IT Debt/GDP (Italie = cas extrême UE)
    if "IT" in debt_gdp_series:
        d, v = latest_pair(debt_gdp_series["IT"], (None, None))
        kpi["it_debt_gdp_pct"]  = round(v, 1) if v else None
        kpi["it_debt_gdp_date"] = d

    # DE Debt/GDP (Allemagne = contre-exemple "vertueux")
    if "DE" in debt_gdp_series:
        d, v = latest_pair(debt_gdp_series["DE"], (None, None))
        kpi["de_debt_gdp_pct"]  = round(v, 1) if v else None
        kpi["de_debt_gdp_date"] = d

    # US Debt/GDP
    if "US" in debt_gdp_series:
        d, v = latest_pair(debt_gdp_series["US"], (None, None))
        kpi["us_debt_gdp_pct"]  = round(v, 1) if v else None
        kpi["us_debt_gdp_date"] = d

    # US debt total ($)
    if treasury:
        kpi["us_debt_total_usd"]    = round(treasury["latest_value"])
        kpi["us_debt_total_tn_usd"] = round(treasury["latest_value"] / 1e12, 2)
        kpi["us_debt_date"]         = treasury["latest_date"]

    # Charge d'intérêts US (% GDP, last)
    fy = interest_series.get("FYOIGDA188S")
    if fy:
        d, v = latest_pair(fy, (None, None))
        kpi["us_interest_gdp_pct"]  = round(v, 2) if v else None
        kpi["us_interest_gdp_date"] = d

    # Charge d'intérêts US ($bn SAAR) sur dernier qtl
    ip = interest_series.get("A091RC1Q027SBEA")
    if ip:
        d, v = latest_pair(ip, (None, None))
        kpi["us_interest_bn_usd_saar"] = round(v) if v else None
        kpi["us_interest_date"]        = d

    # Charge d'intérêts US : part dans les recettes fédérales (interest / receipts)
    if ip and "FGRECPT" in interest_series:
        recs = interest_series["FGRECPT"]
        if recs.get("values") and ip.get("values"):
            kpi["us_interest_pct_receipts"] = round(
                100 * ip["values"][-1] / recs["values"][-1], 1
            )

    # JP Debt/GDP
    if "JP" in debt_gdp_series:
        d, v = latest_pair(debt_gdp_series["JP"], (None, None))
        kpi["jp_debt_gdp_pct"] = round(v, 1) if v else None

    # US 10Y rate
    if "DGS10" in yields_series:
        d, v = latest_pair(yields_series["DGS10"], (None, None))
        kpi["us_10y_pct"]  = round(v, 2) if v else None
        kpi["us_10y_date"] = d

    # France 10Y rate
    if "IRLTLT01FRM156N" in yields_series:
        d, v = latest_pair(yields_series["IRLTLT01FRM156N"], (None, None))
        kpi["fr_10y_pct"]  = round(v, 2) if v else None
        kpi["fr_10y_date"] = d

    # ─── 8. Assembly ──
    meta = {
        "updated_at":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_at_unix": int(time.time()),
        "sources_ok":      ok,
        "sources_failed":  failed,
        "start_date":      START_DATE,
        "doc_version":     "1.0",
    }
    payload = {
        "meta": meta,
        "kpi":  kpi,
        "debt_gdp":  debt_gdp_series,
        "imf_weo_debt_gdp": imf_weo,
        "debt_levels_eur": eurostat_debt_levels,
        "yields":    yields_series,
        "interest":  interest_series,
        "growth":    growth_series,
        "treasury_total_debt": treasury,
    }
    return payload, len(ok), len(failed)


def write_outputs(payload):
    # JSON canonique
    OUT_JSON.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    # JS bundle pour ingestion navigateur
    js = (
        f"/* these_dette_cache.js — generated {payload['meta']['updated_at']} */\n"
        f"window.__THESE_DETTE__ = "
        f"{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)};\n"
    )
    OUT_JS.write_text(js)
    # Symlinks dans le repo public (le Rmd les attend en racine du site)
    site_dir = Path.home() / "Desktop" / "Site_Crypto_Finance"
    if site_dir.exists():
        for name in ("these_dette_cache.json", "these_dette_cache.js"):
            link = site_dir / name
            target = CACHE_DIR / name
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(target)
            except OSError as e:
                # En cas d'échec de symlink (FS exotique), fallback copie
                sys.stderr.write(f"[SYMLINK {name}] {e} — falling back to copy\n")
                try:
                    shutil.copy2(target, link)
                except Exception as e2:
                    sys.stderr.write(f"[COPY {name}] {e2}\n")


def main():
    t0 = time.time()
    try:
        payload, n_ok, n_fail = build_payload()
    except Exception as e:
        sys.stderr.write(f"[FATAL] build_payload crashed: {e}\n")
        sys.exit(2)

    # Garde-fou antifragile : si TOUTES les sources principales ont échoué et
    # qu'on a un cache existant, on garde l'ancien plutôt qu'écraser avec vide.
    primary_ok = n_ok >= 5
    if not primary_ok and OUT_JSON.exists():
        sys.stderr.write(
            f"[GUARD] only {n_ok} sources OK / {n_fail} failed — keeping previous cache\n"
        )
        # On marque tout de même que le run a eu lieu en touchant un sidecar
        (CACHE_DIR / "these_dette_last_attempt.txt").write_text(
            json.dumps({
                "tried_at": payload["meta"]["updated_at"],
                "ok": payload["meta"]["sources_ok"],
                "failed": payload["meta"]["sources_failed"],
            })
        )
        sys.exit(1)

    write_outputs(payload)
    dt = time.time() - t0
    sys.stdout.write(
        f"[these_dette] OK · {n_ok} sources, {n_fail} failed · {dt:.1f}s · "
        f"cache → {OUT_JSON}\n"
    )


if __name__ == "__main__":
    main()
