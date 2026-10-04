#!/usr/bin/env python3
"""Cache antifragile pour le chapitre Thèse · Le mur de la dette.

Sources auditables (aucune clé API requise) :
  * FRED — CSV public fredgraph (US + zone euro + Japon + UK + Allemagne + Italie)
  * US Treasury Fiscal Data — debt to the penny (daily, prod)
  * Yahoo Finance v8 — DGS-like fallback pour rates (overlap diagnostic)
  * Insee BDM SDMX — dette publique France trimestrielle (Maastricht)
  * FMI DataMapper — dette publique WEO (réalisé + projections) ET édition du WEO
    (« April 2026 ») : la frontière réalisé/estimation/projection en découle
  * BRI WS_TC — crédit total (productivité marginale de la dette)
  * Eurostat gov_10a_main — France : intérêts versés, solde, recettes (annuel)
  * Trésor US TIC — Treasuries détenus par l'étranger ; Banque mondiale — crédit privé

RÈGLE DE REPLI (04/10/2026) : une source qui tombe garde la DERNIÈRE valeur
réellement collectée (relue dans le cache précédent, listée dans
meta.stale_blocks) ; meta.last_success date le dernier succès de chaque source.

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
    # Défense nationale, dépenses + investissement (trimestriel, Md$ SAAR, NIPA) :
    # même base comptable que les intérêts — sert à la comparaison du texte.
    "FDEFX": ("US National defense consumption expenditures and gross investment", "level_bn"),
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


MOIS_EN = {"January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
           "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
           "December": 12}
MOIS_FR = ["", "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]


def fetch_imf_weo_edition():
    """Édition du WEO servie par le DataMapper : « World Economic Outlook (April 2026) ».

    Sert à placer la frontière réalisé / estimation / projection SANS année codée
    en dur (l'ancienne constante CUTOFF = 2024 aurait survécu à l'édition
    d'octobre). Règle FMI : l'édition d'avril Y publie Y−1 en ESTIMATION (comptes
    pas encore définitifs pour une partie des pays) ; celle d'octobre Y intègre
    Y−1 comme réalisé. L'année Y et les suivantes sont toujours des projections."""
    try:
        d = json.loads(http_get_text(
            "https://www.imf.org/external/datamapper/api/v1/indicators",
            timeout=25, accept="application/json"))
        meta = (d.get("indicators") or {}).get("GGXWDG_NGDP") or {}
    except Exception as e:                                      # noqa: BLE001
        sys.stderr.write(f"[IMF WEO edition] {e}\n")
        return None
    src = meta.get("source", "")
    m = re.search(r"\((\w+)\s+(\d{4})\)", src)
    if not m or m.group(1) not in MOIS_EN:
        return None
    month, year = MOIS_EN[m.group(1)], int(m.group(2))
    last_actual = year - 2 if month <= 6 else year - 1
    return {
        "source_label": src,
        "edition": f"{MOIS_FR[month]} {year}",
        "edition_year": year,
        "edition_month": month,
        "last_modified": meta.get("last-modified"),
        "last_actual_year": last_actual,
        "estimate_years": list(range(last_actual + 1, year)),
        "first_projection_year": year,
    }


def fetch_imf_weo_debt_gdp(country_iso3_list, cutoff=None):
    """IMF DataMapper API — General Government Gross Debt as % of GDP (GGXWDG_NGDP).
    Données annuelles 1980 → horizon WEO, projections FMI comprises.
    Retourne dict {ISO3: {years: [...], values: [...], cutoff_observed_year}}.
    cutoff_observed_year = dernière année RÉALISÉE (déduite de l'édition)."""
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
    # Repli si l'édition n'a pu être lue : année courante − 2 (règle d'avril,
    # la plus prudente : on ne présente jamais une estimation comme réalisée).
    CUTOFF = cutoff if cutoff else datetime.now(timezone.utc).year - 2
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


# ══════════════════════════════════════════════════════════════════════════════
# PRODUCTIVITÉ MARGINALE DE LA DETTE — crédit TOTAL (BRI)
# ══════════════════════════════════════════════════════════════════════════════
# Combien de PIB une économie obtient pour chaque unité de dette nouvelle :
#
#       MPD(t) =   PIB(t) − PIB(t−10 ans)
#                ──────────────────────────
#                 Dette(t) − Dette(t−10 ans)
#
# POURQUOI LA BRI ET PAS LA DETTE PUBLIQUE
#   Le PIB est nourri par TOUT le crédit de l'économie, pas seulement celui de
#   l'État. Ne regarder que la dette publique surestime massivement le rendement :
#   VÉRIFIÉ sur les données du 06/08/2026 — États-Unis 0,78 en dette publique
#   seule contre 0,40 en dette totale, France 0,96 contre 0,26. On calcule donc
#   les trois périmètres et on montre l'écart au lecteur.
#   La série BRI « credit to the non-financial sector » couvre ménages +
#   entreprises + administrations publiques, remonte à 1947 pour les États-Unis,
#   et exclut le secteur financier — ce qui évite de compter deux fois le même
#   euro (une banque qui emprunte pour prêter).
#
# POURQUOI ON RECONSTRUIT LE PIB AU LIEU DE LE CHERCHER AILLEURS
#   La BRI publie le même encours sous deux unités : en monnaie locale (XDC) et
#   en % du PIB (770). Leur rapport redonne EXACTEMENT le PIB nominal que la BRI
#   a utilisé. Numérateur et dénominateur viennent donc de la même source, avec
#   les mêmes conventions et le même millésime — aucun risque d'assembler un PIB
#   d'un fournisseur avec une dette d'un autre, et le taux de change n'entre
#   jamais dans le calcul (tout est en monnaie locale).
BIS_TC_URL = "https://stats.bis.org/api/v1/data/WS_TC"
BIS_WINDOW_Q = 40          # 40 trimestres = 10 ans
BIS_COUNTRIES = {
    "US": "États-Unis", "FR": "France", "DE": "Allemagne", "IT": "Italie",
    "ES": "Espagne", "JP": "Japon", "GB": "Royaume-Uni", "CN": "Chine",
    "XM": "Zone euro",
}
# Code BRI du secteur emprunteur → clé de sortie.
BIS_BORROWERS = {"C": "total", "G": "public", "P": "prive"}


def _bis_q_to_iso(q):
    """« 1957-Q4 » → « 1957-10-01 » (Plotly veut une date, pas un libellé)."""
    y, t = q.split("-Q")
    return f"{int(y):04d}-{(int(t) - 1) * 3 + 1:02d}-01"


def _bis_q_index(q):
    """« 1957-Q4 » → entier monotone, pour repérer t−40 trimestres sans trou."""
    y, t = q.split("-Q")
    return int(y) * 4 + int(t) - 1


def fetch_bis_tc(borrower, unit):
    """{pays: {trimestre: valeur}} pour un secteur emprunteur et une unité.

    Un seul appel ramène les ~48 économies couvertes : 6 requêtes au total.
    """
    url = f"{BIS_TC_URL}/Q..{borrower}.A.M.{unit}.A/all?format=csv"
    try:
        txt = http_get_text(url, timeout=120, max_retries=3)
    except Exception as e:                                       # noqa: BLE001
        sys.stderr.write(f"[BIS {borrower}/{unit}] {e}\n")
        return None
    out = {}
    for row in csv.DictReader(io.StringIO(txt)):
        c = row.get("BORROWERS_CTY")
        if c not in BIS_COUNTRIES:
            continue
        try:
            out.setdefault(c, {})[row["TIME_PERIOD"]] = float(row["OBS_VALUE"])
        except (TypeError, ValueError, KeyError):
            continue
    return out or None


def _mpd_from_bis(xdc, pct):
    """Séries {trimestre: valeur} d'encours → série MPD {dates, values}."""
    gdp, debt = {}, {}
    for q, lvl in xdc.items():
        share = pct.get(q)
        if not share:                     # 0 ou absent → PIB non reconstructible
            continue
        gdp[_bis_q_index(q)] = lvl / (share / 100.0)
        debt[_bis_q_index(q)] = lvl
    dates, values = [], []
    for k in sorted(gdp):
        prev = k - BIS_WINDOW_Q
        if prev not in gdp:
            continue
        d_debt = debt[k] - debt[prev]
        # Dénominateur nul ou négatif (désendettement net sur 10 ans) : le ratio
        # n'a pas de sens économique, on laisse un trou plutôt qu'une valeur
        # explosive qui écraserait l'échelle du graphe.
        if d_debt <= 0:
            continue
        dates.append(_bis_q_to_iso(f"{k // 4}-Q{k % 4 + 1}"))
        values.append(round((gdp[k] - gdp[prev]) / d_debt, 3))
    if len(values) < 8:
        return None
    return {"dates": dates, "values": values,
            "latest_date": dates[-1], "latest_value": values[-1]}


def fetch_mpd():
    """Bloc `mpd` complet du cache. (payload|None, liste_ok, liste_failed)."""
    raw, ok, failed = {}, [], []
    for b in BIS_BORROWERS:
        for unit in ("XDC", "770"):
            d = fetch_bis_tc(b, unit)
            if d is None:
                failed.append(f"BIS:WS_TC:{b}/{unit}")
            else:
                raw[(b, unit)] = d
                ok.append(f"BIS:WS_TC:{b}/{unit}")

    # Le périmètre TOTAL est le cœur de la section : sans lui, rien à publier.
    if ("C", "XDC") not in raw or ("C", "770") not in raw:
        sys.stderr.write("[BIS] crédit total indisponible — bloc mpd non produit\n")
        return None, ok, failed

    series = {}
    for iso, label in BIS_COUNTRIES.items():
        entry = {"label": label}
        for b, key in BIS_BORROWERS.items():
            xdc = (raw.get((b, "XDC")) or {}).get(iso)
            pct = (raw.get((b, "770")) or {}).get(iso)
            if not xdc or not pct:
                continue
            s = _mpd_from_bis(xdc, pct)
            if s:
                entry[key] = s
        if "total" in entry:
            series[iso] = entry

    if not series:
        return None, ok, failed

    us = series.get("US", {})
    us_tot, us_pub = us.get("total"), us.get("public")
    kpi = {}
    if us_tot:
        vals, dates = us_tot["values"], us_tot["dates"]
        kpi["us_total_now"] = vals[-1]
        kpi["us_total_now_date"] = dates[-1]
        # Sommet historique : la référence honnête pour dire « il en fallait X fois moins ».
        i_max = max(range(len(vals)), key=lambda i: vals[i])
        kpi["us_total_peak"] = vals[i_max]
        kpi["us_total_peak_date"] = dates[i_max]
        kpi["us_total_first"] = vals[0]
        kpi["us_total_first_date"] = dates[0]
    if us_pub:
        kpi["us_public_now"] = us_pub["values"][-1]
    fr = series.get("FR", {})
    if fr.get("total"):
        kpi["fr_total_now"] = fr["total"]["values"][-1]
    if fr.get("public"):
        kpi["fr_public_now"] = fr["public"]["values"][-1]
    cn = series.get("CN", {})
    if cn.get("total"):
        cv = cn["total"]["values"]
        kpi["cn_total_now"] = cv[-1]
        kpi["cn_total_first"] = cv[0]
        kpi["cn_total_first_date"] = cn["total"]["dates"][0]

    # Encours en % du PIB au dernier trimestre (même source) : sert au texte
    # (« la BRI compte 108 % de dette publique française au prix de marché »,
    # comparaison avec la Banque mondiale) — jamais de chiffre recopié à la main.
    pct_gdp = {}
    for iso in BIS_COUNTRIES:
        e = {}
        for b, key in BIS_BORROWERS.items():
            s = (raw.get((b, "770")) or {}).get(iso)
            if s:
                q = max(s, key=_bis_q_index)
                e[key] = round(s[q], 1)
                e["date"] = _bis_q_to_iso(q)
        if e:
            pct_gdp[iso] = e

    return {
        "pct_gdp": pct_gdp,
        "meta": {
            "window_quarters": BIS_WINDOW_Q,
            "window_years": BIS_WINDOW_Q // 4,
            "source": "BRI — Credit to the non-financial sector (WS_TC)",
            "source_url": "https://data.bis.org/topics/TOTAL_CREDIT",
            "api_url": f"{BIS_TC_URL}/Q.US.C.A.M.770.A/all?format=csv",
            "perimetre": {
                "total": "Ménages + entreprises non financières + administrations publiques",
                "public": "Administrations publiques seules",
                "prive": "Ménages + entreprises non financières",
            },
            "gdp_note": "PIB nominal reconstruit = encours en monnaie locale ÷ "
                        "(encours en % du PIB / 100) — même source, même millésime, "
                        "aucun taux de change dans le calcul.",
        },
        "kpi": kpi,
        "series": series,
    }, ok, failed


def fetch_eurostat_fr_public_finance():
    """Eurostat gov_10a_main (annuel, administrations publiques S13, France) :
    intérêts versés (D41PAY), solde (B9), recettes (TR) — en M€ et en % du PIB.
    Alimente le texte (charge d'intérêts française, taux apparent, solde
    primaire) au lieu de chiffres recopiés d'un rapport."""
    base = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
            "gov_10a_main?format=JSON&geo=FR&sector=S13&sinceTimePeriod=2000")
    out = {}
    for item, unit, key in [("D41PAY", "MIO_EUR", "interest_meur"),
                            ("D41PAY", "PC_GDP", "interest_pct_gdp"),
                            ("B9", "PC_GDP", "balance_pct_gdp"),
                            ("B9", "MIO_EUR", "balance_meur"),
                            ("TR", "MIO_EUR", "revenue_meur")]:
        try:
            d = json.loads(http_get_text(f"{base}&na_item={item}&unit={unit}",
                                         timeout=30, accept="application/json"))
        except Exception as e:                                  # noqa: BLE001
            sys.stderr.write(f"[EUROSTAT gov_10a_main {item}/{unit}] {e}\n")
            return None
        idx = d.get("dimension", {}).get("time", {}).get("category", {}).get("index", {})
        vals = d.get("value", {})
        out[key] = {t: vals.get(str(i)) for t, i in idx.items() if vals.get(str(i)) is not None}
    years = sorted(set(out["interest_meur"]) & set(out["balance_pct_gdp"]))
    if not years:
        return None
    res = {"years": [int(y) for y in years]}
    for k in out:
        res[k] = [out[k].get(y) for y in years]
    # Solde primaire = solde total + intérêts (en % du PIB)
    res["primary_balance_pct_gdp"] = [
        round(b + i, 1) if b is not None and i is not None else None
        for b, i in zip(res["balance_pct_gdp"], res["interest_pct_gdp"])]
    res["source"] = "Eurostat · gov_10a_main (France, administrations publiques)"
    res["source_url"] = "https://ec.europa.eu/eurostat/databrowser/view/gov_10a_main/default/table?lang=fr"
    res["updated"] = d.get("updated")
    return res


def fetch_tic_foreign_total():
    """Trésor US · TIC : total des Treasuries détenus par l'étranger (dernier mois)."""
    url = ("https://ticdata.treasury.gov/resource-center/data-chart-center/tic/"
           "Documents/slt_table5.txt")
    try:
        txt = http_get_text(url, timeout=40)
    except Exception as e:                                      # noqa: BLE001
        sys.stderr.write(f"[TIC] {e}\n")
        return None
    header = None
    for ln in txt.splitlines():
        cells = ln.split("\t")
        if cells and cells[0].strip() == "Country":
            header = [c.strip() for c in cells[1:]]
        elif header and cells and cells[0].strip() == "Grand Total":
            try:
                return {"month": header[0], "bn_usd": float(cells[1]),
                        "source_url": url}
            except (ValueError, IndexError):
                return None
    return None


def fetch_wb_private_credit():
    """Banque mondiale FS.AST.PRVT.GD.ZS (crédit au secteur privé, % PIB) — la
    série qu'utilise l'Atlas ; sert à expliquer l'écart Atlas / BRI au lecteur."""
    url = ("https://api.worldbank.org/v2/country/USA;FRA/indicator/FS.AST.PRVT.GD.ZS"
           "?format=json&mrnev=1&per_page=10")
    try:
        d = json.loads(http_get_text(url, timeout=30, accept="application/json"))
    except Exception as e:                                      # noqa: BLE001
        sys.stderr.write(f"[WB credit] {e}\n")
        return None
    out = {}
    for r in (d[1] if len(d) > 1 and d[1] else []):
        if r.get("value") is not None:
            out[r["countryiso3code"]] = {"pct": round(r["value"], 1), "year": int(r["date"])}
    if not out:
        return None
    out["source_url"] = "https://data.worldbank.org/indicator/FS.AST.PRVT.GD.ZS"
    return out


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


def load_prev():
    """Cache du passage précédent : sert de repli source par source."""
    try:
        return json.loads(OUT_JSON.read_text())
    except (OSError, ValueError):
        return {}


def build_payload():
    failed = []
    ok = []
    prev = load_prev()
    prev_meta = prev.get("meta") or {}
    last_success = dict(prev_meta.get("last_success") or {})
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stale = []

    def success(label):
        ok.append(label)
        last_success[label] = now_iso

    def prev_get(*path):
        """Valeur du cache précédent (repli = dernière valeur réellement collectée)."""
        cur = prev
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                return None
            cur = cur[k]
        return cur

    # ─── 1. Debt/GDP series (multi-pays) ──
    debt_gdp_series = {}
    # 1a) FRED pour US (trimestriel BEA), UK (annuel OCDE), JP (annuel OCDE)
    for fred_id, (label, iso, _u) in FRED_DEBT_GDP.items():
        s = fetch_fred_csv(fred_id)
        if s is None:
            failed.append(f"FRED:{fred_id}")
            if prev_get("debt_gdp", iso):
                debt_gdp_series[iso] = prev_get("debt_gdp", iso)
                stale.append(f"debt_gdp.{iso}")
            continue
        debt_gdp_series[iso] = {
            "label": label,
            "source": "FRED",
            "source_id": fred_id,
            "source_url": f"https://fred.stlouisfed.org/series/{fred_id}",
            "freq": "Q" if fred_id == "GFDEGDQ188S" else "A",
            **s,
        }
        success(f"FRED:{fred_id}")

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
            success(f"EUROSTAT:{geo}/PC_GDP")
        else:
            failed.append(f"EUROSTAT:{geo}/PC_GDP")
            if prev_get("debt_gdp", geo):
                debt_gdp_series[geo] = prev_get("debt_gdp", geo)
                stale.append(f"debt_gdp.{geo}")

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
            success(f"EUROSTAT:{geo}/MIO_EUR")
        else:
            failed.append(f"EUROSTAT:{geo}/MIO_EUR")
            if prev_get("debt_levels_eur", geo):
                eurostat_debt_levels[geo] = prev_get("debt_levels_eur", geo)
                stale.append(f"debt_levels_eur.{geo}")

    # ─── 2-4. Taux, charge d'intérêts, croissance (FRED) ──
    def fred_group(catalog, block):
        out = {}
        for fred_id, (label, _u) in catalog.items():
            s = fetch_fred_csv(fred_id)
            if s is None:
                failed.append(f"FRED:{fred_id}")
                if prev_get(block, fred_id):
                    out[fred_id] = prev_get(block, fred_id)
                    stale.append(f"{block}.{fred_id}")
                continue
            out[fred_id] = {
                "label": label,
                "fred_id": fred_id,
                "source_url": f"https://fred.stlouisfed.org/series/{fred_id}",
                **s,
            }
            success(f"FRED:{fred_id}")
        return out

    yields_series = fred_group(FRED_RATES, "yields")
    interest_series = fred_group(FRED_INTEREST, "interest")
    growth_series = fred_group(FRED_GROWTH, "growth")

    # Dette fédérale détenue par le public (hors fonds publics) — % du PIB
    held = fetch_fred_csv("FYGFGDQ188S")
    if held:
        success("FRED:FYGFGDQ188S")
    else:
        failed.append("FRED:FYGFGDQ188S")

    # ─── 4d. IMF WEO General Government Gross Debt (annuel, projections incl.)
    weo_meta = fetch_imf_weo_edition()
    if weo_meta:
        success("IMF:WEO:edition")
    else:
        failed.append("IMF:WEO:edition")
        weo_meta = prev_get("imf_weo_meta")
        if weo_meta:
            stale.append("imf_weo_meta")
    imf_iso = ["JPN", "USA", "GBR", "FRA", "DEU", "ITA", "ESP"]
    imf_weo = fetch_imf_weo_debt_gdp(
        imf_iso, cutoff=(weo_meta or {}).get("last_actual_year")) or {}
    prev_weo = prev_get("imf_weo_debt_gdp") or {}
    for iso in imf_iso:
        if iso in imf_weo:
            success(f"IMF:WEO:{iso}")
        else:
            failed.append(f"IMF:WEO:{iso}")
            if iso in prev_weo:
                imf_weo[iso] = prev_weo[iso]
                stale.append(f"imf_weo_debt_gdp.{iso}")

    # ─── 5. US Treasury debt to the penny ──
    treasury = fetch_us_treasury_debt()
    if treasury:
        success("TREASURY:debt_to_penny")
    else:
        failed.append("TREASURY:debt_to_penny")
        treasury = prev_get("treasury_total_debt")
        if treasury:
            stale.append("treasury_total_debt")

    # ─── 6. France : finances publiques annuelles (Eurostat) ──
    fr_pf = fetch_eurostat_fr_public_finance()
    if fr_pf:
        success("EUROSTAT:gov_10a_main/FR")
        # Taux apparent = intérêts de l'année ÷ encours moyen (fin t−1, fin t)
        lv = (eurostat_debt_levels.get("FR") or {})
        q4 = {d[:4]: v for d, v in zip(lv.get("dates", []), lv.get("values", []))
              if d.endswith("-12-31")}
        rates = []
        for y, i in zip(fr_pf["years"], fr_pf["interest_meur"]):
            a, b = q4.get(str(y - 1)), q4.get(str(y))
            rates.append(round(100 * i / ((a + b) / 2), 2) if (a and b and i) else None)
        fr_pf["apparent_rate_pct"] = rates
    else:
        failed.append("EUROSTAT:gov_10a_main/FR")
        fr_pf = prev_get("fr_public_finance")
        if fr_pf:
            stale.append("fr_public_finance")

    # ─── 6 bis. Détention étrangère de la dette US (TIC) ──
    tic = fetch_tic_foreign_total()
    if tic:
        success("TREASURY:TIC")
    else:
        failed.append("TREASURY:TIC")
        tic = prev_get("tic_foreign")
        if tic:
            stale.append("tic_foreign")

    wb_credit = fetch_wb_private_credit()
    if wb_credit:
        success("WB:FS.AST.PRVT.GD.ZS")
    else:
        failed.append("WB:FS.AST.PRVT.GD.ZS")
        wb_credit = prev_get("wb_private_credit")
        if wb_credit:
            stale.append("wb_private_credit")

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
            kpi["fr_debt_total_bn_eur"] = round(lvl["latest_value"] / 1000)
            kpi["fr_debt_date"]         = lvl["latest_date"]

    for iso, key in (("EA20", "ea"), ("IT", "it"), ("DE", "de"), ("ES", "es")):
        if iso in debt_gdp_series:
            d, v = latest_pair(debt_gdp_series[iso], (None, None))
            kpi[f"{key}_debt_gdp_pct"]  = round(v, 1) if v else None
            kpi[f"{key}_debt_gdp_date"] = d

    # US Debt/GDP
    if "US" in debt_gdp_series:
        d, v = latest_pair(debt_gdp_series["US"], (None, None))
        kpi["us_debt_gdp_pct"]  = round(v, 1) if v else None
        kpi["us_debt_gdp_date"] = d
    if held and held.get("values"):
        kpi["us_debt_held_public_gdp_pct"] = round(held["values"][-1], 1)
        kpi["us_debt_held_public_gdp_date"] = held["dates"][-1]

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
    # même trimestre des deux côtés (l'ancien calcul prenait le dernier point de
    # chaque série, qui pouvaient ne pas coïncider).
    if ip and "FGRECPT" in interest_series:
        rm = dict(zip(interest_series["FGRECPT"]["dates"], interest_series["FGRECPT"]["values"]))
        pairs = [(dt, v) for dt, v in zip(ip["dates"], ip["values"]) if rm.get(dt)]
        if pairs:
            dt, v = pairs[-1]
            kpi["us_interest_pct_receipts"] = round(100 * v / rm[dt], 1)
            kpi["us_interest_pct_receipts_date"] = dt

    # Défense nationale (NIPA, même base comptable que les intérêts)
    df = interest_series.get("FDEFX")
    if df:
        d, v = latest_pair(df, (None, None))
        kpi["us_defense_bn_usd_saar"] = round(v) if v else None
        kpi["us_defense_date"] = d

    # Part de la dette fédérale détenue à l'étranger (TIC ÷ dette du mois)
    if tic and treasury:
        m = {d[:7]: v for d, v in zip(treasury["dates"], treasury["values"])}
        tot = m.get(tic["month"])
        if tot:
            kpi["us_foreign_held_pct"] = round(100 * tic["bn_usd"] * 1e9 / tot, 1)
            kpi["us_foreign_held_month"] = tic["month"]

    # JP Debt/GDP
    if "JP" in debt_gdp_series:
        d, v = latest_pair(debt_gdp_series["JP"], (None, None))
        kpi["jp_debt_gdp_pct"] = round(v, 1) if v else None
        kpi["jp_debt_gdp_date"] = d

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

    # ─── 7 bis. Productivité marginale de la dette (BRI, crédit total) ──
    # Bloc optionnel : s'il tombe, la section du chapitre se met en dégradation
    # propre (message explicite côté page) et le reste du chapitre est intact.
    mpd_block, mpd_ok, mpd_failed = fetch_mpd()
    for lab in mpd_ok:
        success(lab)
    failed.extend(mpd_failed)
    if mpd_block is None and prev_get("mpd"):
        # Garde par source : la BRI est parfois indisponible plusieurs heures.
        # On recopie le bloc du run précédent plutôt que d'effacer la section —
        # les autres sources du chapitre, elles, ont bien répondu.
        mpd_block = prev_get("mpd")
        stale.append("mpd")
        sys.stderr.write("[BIS] indisponible — bloc mpd recopié du cache précédent\n")
    if mpd_block:
        n_c = len(mpd_block["series"])
        print(f"[BIS] mpd : {n_c} économies · "
              f"US total={mpd_block['kpi'].get('us_total_now')} "
              f"public={mpd_block['kpi'].get('us_public_now')}")

    # ─── 8. Assembly ──
    meta = {
        "updated_at":      now_iso,
        "updated_at_unix": int(time.time()),
        "sources_ok":      ok,
        "sources_failed":  failed,
        "stale_blocks":    stale,
        "last_success":    last_success,
        "start_date":      START_DATE,
        "doc_version":     "2.0",
    }
    payload = {
        "meta": meta,
        "kpi":  kpi,
        "debt_gdp":  debt_gdp_series,
        "imf_weo_meta": weo_meta,
        "imf_weo_debt_gdp": imf_weo,
        "debt_levels_eur": eurostat_debt_levels,
        "yields":    yields_series,
        "interest":  interest_series,
        "growth":    growth_series,
        "treasury_total_debt": treasury,
        "fr_public_finance": fr_pf,
        "tic_foreign": tic,
        "wb_private_credit": wb_credit,
        "mpd":       mpd_block,
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
