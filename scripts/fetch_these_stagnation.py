#!/usr/bin/env python3
"""Cache antifragile pour le chapitre Thèse · La stagnation séculaire française.

Sources auditables :
  1. Eurostat · gov_10q_ggdebt (FR debt %, déjà fetché dans these_dette)
  2. FRED · IRLTLT01FRM156N — France 10Y benchmark yield
  3. FRED · CLVMNACSCAB1GQFR — France GDP volume (chain-linked)
  4. FRED · LRHUTTTTFRM156S — France unemployment rate
  5. FRED · LRHU24TTFRM156S — France youth unemployment rate 15-24 (actifs)
  6. FRED · ECBDFR / ECBMRRFR — taux directeurs BCE (dépôt / refi, live)
  7. FRED · IRLTLT01DEM156N — Bund 10Y → spread OAT-Bund dérivé (FR − DE)
  8. Eurostat · gov_10dd_edpt1 — charge d'intérêt (D41PAY) + déficit (B9) live
  9. Banque de France — défaillances d'entreprises (cumul 12 mois, curated)
 10. Insee + Cour des comptes + AFT hardcoded (charge dette projetée, retraites)

Sortie : these_stagnation_cache.json + .js
Lancé par scf.these_stagnation.refresh.
"""
import csv
import html as html_mod
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
OUT_JSON = CACHE_DIR / "these_stagnation_cache.json"
OUT_JS   = CACHE_DIR / "these_stagnation_cache.js"
UA = "Mozilla/5.0 SiteCryptoFinance-TheseStagnation/1.0"
# banque-france.fr renvoie 403 sur un UA non-navigateur → UA Chrome explicite.
UA_BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ════════════════════════════════════════════════════════════════
# HARDCODED DATASETS
# ════════════════════════════════════════════════════════════════

# Dette publique française trimestrielle (Insee/Eurostat)
# Md€ valeur nominale
FR_DEBT_BN_EUR = [
    (2010, 1632), (2011, 1755), (2012, 1869), (2013, 1953), (2014, 2039),
    (2015, 2102), (2016, 2188), (2017, 2258), (2018, 2315), (2019, 2380),
    (2020, 2648), (2021, 2814), (2022, 2950), (2023, 3104), (2024, 3306),
    (2025, 3484),
]

# Charge des intérêts de la dette publique française (Md€/an, Cour des comptes)
FR_INTEREST_CHARGE_BN = [
    (2010, 47), (2011, 50), (2012, 47), (2013, 45), (2014, 43),
    (2015, 42), (2016, 39), (2017, 38), (2018, 37), (2019, 36),
    (2020, 34), (2021, 32), (2022, 43), (2023, 50), (2024, 55),
    (2025, 58),  # projection AFT
]

# Solde primaire / PIB (% PIB)
FR_PRIMARY_BALANCE = [
    (2010, -5.4), (2011, -2.7), (2012, -2.2), (2013, -1.8), (2014, -1.6),
    (2015, -1.4), (2016, -1.3), (2017, -1.0), (2018, -1.0), (2019, -1.0),
    (2020, -6.7), (2021, -4.2), (2022, -2.5), (2023, -3.3), (2024, -3.5),
    (2025, -3.3),
]

# Sécurité sociale - déficit cumulé ACOSS (Md€)
ACOSS_DEFICIT = [
    # (year, total_debt_bn)
    (2010, 27), (2015, 30), (2018, 26), (2020, 56),
    (2022, 132), (2024, 175), (2025, 195),
]

# Programme de financement AFT (Md€)
AFT_FINANCING = [
    (2019, 200), (2020, 260), (2021, 260), (2022, 260),
    (2023, 270), (2024, 285), (2025, 295), (2026, 305),
]

# Élections / fragmentation politique
FR_POLITICAL_TIMELINE = [
    (2017, "Macron 1 — majorité absolue 308 sièges"),
    (2022, "Macron 2 — majorité relative 245 sièges"),
    (2024, "Dissolution + élections — 3 blocs ~180 sièges chacun"),
    (2025, "Censure Barnier + Bayrou minoritaire + Lecornu"),
]

# Taux moyen apparent de la dette française
FR_AVG_RATE = [
    (2018, 1.8), (2019, 1.6), (2020, 1.4), (2021, 1.4), (2022, 1.5),
    (2023, 1.7), (2024, 1.9), (2025, 2.0), (2026, 2.3), (2027, 2.6),
    (2028, 2.9), (2029, 3.1), (2030, 3.3),  # projection AFT
]

# ════════════════════════════════════════════════════════════════
# COUCHE AIGUË JUIN 2026 — défaillances + décomposition hors-bilan
# ════════════════════════════════════════════════════════════════

# Défaillances d'entreprises France — cumul 12 mois (Banque de France, Stat Info).
# 2020-2021 artificiellement bas (PGE + reports d'échéances Covid) ; rebond
# 2023-2025 jusqu'au niveau record, désormais ETI et grandes entreprises incluses.
# Valeurs arrondies — pas d'API stable, série curée depuis Stat Info BdF.
FR_DEFAILLANCES = [
    (2017, 55000), (2018, 54000), (2019, 52000),
    (2020, 31000), (2021, 28000), (2022, 42000),
    (2023, 56000), (2024, 66400), (2025, 67500),
]

# Décomposition dette exigible vs engagements hors-bilan — pour corriger
# rigoureusement le récit "France en faillite / actif net négatif".
# La dette Maastricht est exigible ; les engagements de retraite sont des flux
# futurs actualisés, NON exigibles à un instant t (≠ dette au sens comptable).
# Sources : Insee (dette Maastricht) + Compte général de l'État (engagements
# de retraite des fonctionnaires, évaluation actuarielle).
FR_HORS_BILAN = [
    # (label, bn_eur, kind)
    ("Dette Maastricht (exigible)",                              3484, "exigible"),
    ("Engagements de retraite des fonctionnaires (actuariel)",   2300, "hors_bilan"),
]

# ════════════════════════════════════════════════════════════════
# ENRICHISSEMENT MAI 2026 — paradoxes français + souveraineté + atouts
# ════════════════════════════════════════════════════════════════

# Solde naturel France — naissances vs décès (milliers/an)
# Source : Insee Bilan démographique 2025
FR_SOLDE_NATUREL = [
    # (year, naissances_k, deces_k)
    (2000, 775, 540), (2010, 832, 551), (2015, 800, 593),
    (2018, 758, 609), (2020, 736, 668), (2022, 723, 668),
    (2023, 678, 631), (2024, 663, 647), (2025, 645, 651),  # croisement historique
]

# Part de l'électricité bas-carbone française · annuel %
# Source : RTE Bilan électrique
FR_ELEC_LOW_CARBON_PCT = [
    (2015, 91.5), (2018, 91.8), (2020, 92.0), (2021, 91.8),
    (2022, 88.4), (2023, 92.0), (2024, 93.5), (2025, 95.2),  # RTE 2025
]

# Prélèvements obligatoires · % PIB par pays (2024)
# Source : OCDE Revenue Statistics
PRELEVEMENTS_OBLIGATOIRES = [
    # (country, pct_gdp, services_satisfaction_pct)  -- satisfaction Eurobaromètre
    ("France",     45.6, 32),
    ("Danemark",   44.1, 71),
    ("Belgique",   42.4, 48),
    ("Italie",     42.8, 38),
    ("Allemagne",  39.5, 56),
    ("Pays-Bas",   38.9, 64),
    ("UK",         33.6, 42),
    ("Espagne",    37.5, 45),
    ("USA",        27.7, 50),
    ("Suisse",     27.5, 78),
]

# Souveraineté alimentaire France — taux dépendance imports par catégorie (%)
# Source : FranceAgriMer + Min. Agriculture 2024
FR_FOOD_IMPORTS = [
    # (category, pct_imports)
    ("Fruits frais",         60),
    ("Légumes frais",        45),
    ("Poisson & fruits mer", 80),
    ("Engrais azotés",       70),
    ("Aliments bétail (soja)",98),
    ("Volaille (cumul UE)",  43),
    ("Vin (équilibré)",      0),
    ("Céréales (excédentaire)",-10),
]

# Sécheresses France — nombre de communes sous restrictions d'eau (été)
# Source : Propluvia / Ministère Transition Écologique
FR_DROUGHT_COMMUNES = [
    (2015,  6300),
    (2017,  7800),
    (2019,  8200),
    (2020,  9100),
    (2022, 13800),  # canicule historique
    (2023, 12500),
    (2024, 11200),
]

# Effondrement insectes en Europe (Krefeld study + suivis)
# Source : Hallmann et al. 2017 + suivis 2024
EU_INSECT_BIOMASS = [
    # (year, biomass_pct_relatif_1989)
    (1989, 100), (1995, 87), (2000, 73), (2005, 56),
    (2010, 42), (2015, 31), (2020, 24), (2024, 22),
]

# Atouts français · contribution au PIB ou export (Md€/an, 2024)
# Sources : INSEE + FEVAD + GIFAS + COSE + LVMH IR
FR_STRENGTHS = [
    # (sector, value_bn_eur, type, comment)
    ("Luxe (LVMH, Hermès, Kering)",    97, "export", "1er secteur d'exportation FR"),
    ("Aéronautique (Airbus, Safran)",   75, "export", "2e secteur export"),
    ("Tourisme",                        67, "PIB",    "1er pays touristique mondial · 100 M visiteurs"),
    ("Défense (Dassault, Naval, MBDA)", 50, "export", "3e exportateur d'armes mondial"),
    ("Agriculture & agroalim",          81, "export", "premier pays agricole UE"),
    ("Pharmaceutique",                  42, "export", "Sanofi · Servier · Pierre Fabre"),
    ("Cosmétiques (L'Oréal)",           20, "export", "leader mondial"),
    ("Épargne nette ménages",         5500, "stock",  "patrimoine financier net"),
    ("Recherche publique (CNRS, CEA)", 24,  "budget", "3e dépense R&D publique UE"),
]

# Cinq scénarios — probabilités estimées par cohorte d'analystes
# Source : synthèse interne (Cour des comptes + IFRAP + Asterès + OFCE)
SCENARIOS_FR = [
    # (scenario, probability_pct, horizon, deceleration, comment)
    ("Déclin contrôlé · réforme volontaire",  15, "2026-2030",
     "Croissance ~1 %, dette stabilisée 115 %, services maintenus",
     "Suppose maturité politique + maîtrise dépenses"),
    ("Déclin mou · status quo prolongé",       45, "2026-2032",
     "Croissance 0,5-1 %, dette 130 %, services dégradés",
     "Scénario tendanciel — extrapolation 2020-2025"),
    ("Crise dette · austérité forcée",         20, "2027-2029",
     "Spread OAT-Bund > 200 pb, plan FMI/BCE, coupes 5 % PIB",
     "Déclencheur externe (taux, choc Italie, défaite UE)"),
    ("Fragmentation sociale · paralysie",      15, "2026-2031",
     "Explosion violences, ingouvernabilité, alternance autoritaire",
     "Climat post-dissolution 2024 si aggravé"),
    ("Redressement par souveraineté productive", 5, "2027-2035",
     "Reindustrialisation nucléaire + IA souveraine + atouts mobilisés",
     "Suppose consensus politique long terme + acceptation sobriété"),
]

# Décès du débat public — promesses incompatibles (matrice)
# Source : synthèse sondages CEVIPOF + Ipsos 2024
FR_INCOMPATIBLE_DEMANDS = [
    # (demand, pct_d_accord)
    ("Moins d'impôts",          78),
    ("Plus de services publics", 81),
    ("Moins de dette",           67),
    ("Plus de pouvoir d'achat",  88),
    ("Plus de sécurité",         84),
    ("Plus d'écologie",          64),
    ("Plus de souveraineté",     71),
    ("Moins de contraintes",     72),
]


def http_get_text(url, timeout=20, max_retries=5, accept="text/csv,*/*", ua=UA):
    req = Request(url, headers={"User-Agent": ua, "Accept": accept})
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


# Fallbacks France · FRED archives mensuelles
FR_10Y_FALLBACK = [
    ("2000-01-01", 5.4), ("2003-06-01", 4.1), ("2005-12-01", 3.4),
    ("2007-06-01", 4.4), ("2008-12-01", 3.6), ("2010-06-01", 3.0),
    ("2011-11-01", 3.5), ("2012-12-01", 2.0), ("2015-04-01", 0.45),
    ("2016-08-01", 0.13), ("2018-12-01", 0.71), ("2019-08-01",-0.40),
    ("2020-12-01",-0.34), ("2021-12-01", 0.16), ("2022-06-01", 2.20),
    ("2022-12-01", 3.10), ("2023-10-01", 3.50), ("2024-06-01", 3.20),
    ("2024-12-01", 3.10), ("2025-06-01", 3.30), ("2026-05-01", 3.45),
]
FR_UNEMP_FALLBACK = [
    ("2000-01-01", 9.6), ("2003-12-01", 9.0), ("2007-12-01", 7.7),
    ("2009-06-01", 9.5), ("2013-12-01",10.3), ("2016-12-01", 9.9),
    ("2019-12-01", 8.1), ("2021-06-01", 8.0), ("2023-06-01", 7.3),
    ("2024-12-01", 7.7), ("2025-12-01", 7.9), ("2026-04-01", 7.9),
]
FR_YOUTH_FALLBACK = [
    ("2000-01-01", 19.5), ("2003-12-01", 21.4), ("2007-12-01", 19.5),
    ("2009-12-01", 24.5), ("2013-06-01", 25.3), ("2015-12-01", 24.5),
    ("2017-12-01", 21.4), ("2019-12-01", 19.6), ("2021-06-01", 20.4),
    ("2023-06-01", 17.5), ("2024-12-01", 19.7), ("2025-12-01", 21.5),
    ("2026-04-01", 21.5),
]
# BCE — la trajectoire réelle est une DÉTENTE depuis 2024 (pas une hausse) :
# dépôt 4,00 % (sept-2023) → 2,00 % (2025) ; refi 4,50 % → 2,15 %.
ECB_DEPO_FALLBACK = [
    ("2022-07-27", 0.00), ("2023-09-20", 4.00), ("2024-06-12", 3.75),
    ("2024-10-23", 3.25), ("2025-01-30", 2.75), ("2025-06-11", 2.00),
    ("2026-06-16", 2.00),
]
ECB_REFI_FALLBACK = [
    ("2022-07-27", 0.50), ("2023-09-20", 4.50), ("2024-06-12", 4.25),
    ("2024-10-23", 3.40), ("2025-06-11", 2.15), ("2026-06-16", 2.15),
]
DE_10Y_FALLBACK = [
    ("2000-01-01", 5.3), ("2008-12-01", 3.0), ("2012-12-01", 1.3),
    ("2016-08-01", -0.1), ("2020-12-01", -0.6), ("2022-12-01", 2.5),
    ("2024-06-01", 2.5), ("2025-06-01", 2.6), ("2026-05-01", 3.05),
]

# Eurostat EDP (gov_10dd_edpt1) — déficit (B9) + charge d'intérêt (D41PAY), annuel
EUROSTAT_EDP = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/"
                "1.0/data/gov_10dd_edpt1")


def fetch_eurostat_series(na_item, unit, sector="S13", geo="FR"):
    """Série annuelle Eurostat gov_10dd_edpt1 (déficit/intérêts EDP, France).

    Comme toutes les dimensions sauf le temps sont épinglées à une seule
    valeur, l'index plat de `value` == l'index temporel.
    Retourne {'years':[int], 'values':[float], 'source_url':...} ou None.
    """
    from urllib.parse import urlencode
    url = EUROSTAT_EDP + "?" + urlencode({
        "format": "JSON", "geo": geo, "na_item": na_item,
        "sector": sector, "unit": unit,
    })
    try:
        txt = http_get_text(url, timeout=30, accept="application/json")
        d = json.loads(txt)
    except Exception as e:
        sys.stderr.write(f"[Eurostat {na_item}/{unit}] {e}\n")
        return None
    idx = (d.get("dimension", {}).get("time", {})
            .get("category", {}).get("index", {}))
    vals = d.get("value", {})
    if not idx or not vals:
        return None
    rows = []
    for year, i in idx.items():
        v = vals.get(str(i))
        if v is None:
            continue
        try:
            rows.append((int(year), float(v)))
        except (ValueError, TypeError):
            continue
    rows.sort()
    if not rows:
        return None
    return {
        "years": [y for y, _ in rows],
        "values": [v for _, v in rows],
        "source_url": ("https://ec.europa.eu/eurostat/databrowser/view/"
                       "gov_10dd_edpt1"),
    }


# ════════════════════════════════════════════════════════════════
# BANQUE DE FRANCE · Stat Info défaillances (LIVE, mensuel)
# ════════════════════════════════════════════════════════════════
# La page mensuelle publie (a) la série complète cumul-12-mois depuis déc-1991
# dans le JS Highcharts du « graphique 1 », (b) le tableau A = ventilation par
# secteur, (c) le tableau B = ventilation par taille (dont ETI-GE). Aucune API
# ouverte n'expose ces séries (Webstat exige un compte développeur), la page
# HTML est donc la source live auditable. URL déterministe par mois de réf.
BDF_STATINFO = ("https://www.banque-france.fr/fr/statistiques/entreprises/"
                "defaillances-dentreprises-{ym}")
BDF_MONTHS = {
    "janv": 1, "févr": 2, "fevr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6,
    "juil": 7, "août": 8, "aout": 8, "sept": 9, "oct": 10, "nov": 11,
    "déc": 12, "dec": 12,
}


def _bdf_cat_to_iso(cat):
    """'déc. 1991' → '1991-12-01'."""
    c = cat.replace("\xa0", " ").strip().lower()
    m = re.match(r"([a-zéûàôA-Z]+)\.?\s+(\d{4})", c)
    if not m:
        return None
    mon = BDF_MONTHS.get(m.group(1).rstrip("."))
    return f"{int(m.group(2)):04d}-{mon:02d}-01" if mon else None


def _bdf_num(s):
    """'70 077' | '4,7%' | '-1,9%' → float (None si non numérique)."""
    if s is None:
        return None
    t = (s.replace("\xa0", " ").replace(" ", "")
          .replace("%", "").replace(" ", "").replace(",", ".").strip())
    if not t or t in ("-", "—", "n.d.", "nd", "ns"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _bdf_cells(row_html):
    cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row_html, re.S)
    return [html_mod.unescape(re.sub(r"<[^>]+>", " ", c))
            .replace("\xa0", " ").replace("\t", " ").strip() for c in cells]


def _bdf_breakdown(table_html):
    """Tableau A (secteur) ou B (taille) → lignes exploitables.

    Colonnes : label | moy 2010-2019 | M-1 | M-1 a/a | M-1 vs moy |
               M an-1 | M | M a/a | M vs moy   → on retient moy, M, a/a, vs moy.
    """
    out = []
    for row in re.findall(r"<tr.*?</tr>", table_html, re.S):
        c = _bdf_cells(row)
        if len(c) < 9:
            continue
        label = re.sub(r"\s+", " ", c[0]).strip()
        moy, latest = _bdf_num(c[1]), _bdf_num(c[6])
        if not label or moy is None or latest is None:
            continue
        out.append({"label": label, "moy_2010_2019": moy, "latest": latest,
                    "yoy_pct": _bdf_num(c[7]), "vs_moy_pct": _bdf_num(c[8])})
    return out


def _bdf_parse(page):
    i = page.find("cumul du nombre des d")
    if i < 0:
        raise ValueError("série 'graphique 1' introuvable")
    blk = page[max(0, i - 6000): i + 40000]
    m = re.search(r"data:\s*\[([0-9.,\s]+)\]", blk)
    mc = re.search(r"categories\s*:\s*\[(.*?)\]", blk, re.S)
    if not m or not mc:
        raise ValueError("data[]/categories[] introuvables")
    values = [float(x) for x in m.group(1).split(",") if x.strip()]
    cats = [a or b for a, b in re.findall(r"'([^']*)'|\"([^\"]*)\"", mc.group(1))]
    pairs = [(d, v) for d, v in
             zip((_bdf_cat_to_iso(c) for c in cats), values) if d and v is not None]
    if len(pairs) < 100:
        raise ValueError(f"série anormalement courte ({len(pairs)} points)")
    tables = re.findall(r"<table.*?</table>", page, re.S)
    return {
        "dates":  [d for d, _ in pairs],
        "values": [v for _, v in pairs],
        "by_sector": _bdf_breakdown(tables[0]) if len(tables) > 0 else [],
        "by_size":   _bdf_breakdown(tables[1]) if len(tables) > 1 else [],
    }


def fetch_bdf_defaillances(max_lookback=8):
    """Remonte les mois jusqu'à trouver la dernière parution Stat Info publiée."""
    today = datetime.now(timezone.utc).date()
    y, mo = today.year, today.month
    for _ in range(max_lookback):
        mo -= 1
        if mo == 0:
            mo, y = 12, y - 1
        ym = f"{y:04d}-{mo:02d}"
        url = BDF_STATINFO.format(ym=ym)
        try:
            page = http_get_text(url, timeout=30, max_retries=2,
                                 accept="text/html,application/xhtml+xml",
                                 ua=UA_BROWSER)
        except Exception:
            continue
        if "cumul du nombre des d" not in page:
            continue  # parution pas encore en ligne pour ce mois
        try:
            d = _bdf_parse(page)
        except Exception as e:
            sys.stderr.write(f"[BdF {ym}] parse: {e}\n")
            continue
        d["source_url"] = url
        d["ref_period"] = ym
        return d
    return None


# ════════════════════════════════════════════════════════════════
# EUROSTAT · lecture générique d'un cube JSON-stat
# ════════════════════════════════════════════════════════════════
EUROSTAT_BASE = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/"
                 "1.0/data/")


def fetch_eurostat_cube(dataset, free_dims, **params):
    """Cube Eurostat → {tuple(valeurs des free_dims): {période: valeur}}.

    Décode l'index plat JSON-stat (ordre `id`, tailles `size`) au lieu de
    supposer une seule cellule libre — indispensable dès qu'on demande
    plusieurs pays ou plusieurs secteurs en un appel.
    """
    from urllib.parse import urlencode
    url = EUROSTAT_BASE + dataset + "?" + urlencode(
        dict(format="JSON", lang="EN", **params), doseq=True)
    try:
        d = json.loads(http_get_text(url, timeout=45, accept="application/json"))
    except Exception as e:
        sys.stderr.write(f"[Eurostat {dataset}] {e}\n")
        return None
    try:
        order, sizes, dims = d["id"], d["size"], d["dimension"]
        decoders = []
        for dim in order:
            idx = dims[dim]["category"]["index"]
            inv = {v: k for k, v in idx.items()}
            decoders.append([inv[i] for i in range(len(inv))])
        out = {}
        for flat, val in d["value"].items():
            if val is None:
                continue
            f, coords = int(flat), []
            for s in reversed(sizes):
                coords.append(f % s)
                f //= s
            coords.reverse()
            labels = {order[i]: decoders[i][coords[i]] for i in range(len(order))}
            out.setdefault(tuple(labels[x] for x in free_dims), {})[labels["time"]] = val
        return out or None
    except (KeyError, IndexError, ValueError, TypeError) as e:
        sys.stderr.write(f"[Eurostat {dataset}] décodage: {e}\n")
        return None


def _cube_to_series(cube, key):
    """Une cellule du cube → {'x': [périodes triées], 'y': [valeurs]}."""
    s = (cube or {}).get(key)
    if not s:
        return None
    xs = sorted(s)
    return {"x": xs, "y": [s[x] for x in xs]}


def build_payload():
    ok, failed = [], []

    fr_10y = fetch_fred_csv("IRLTLT01FRM156N", start="2000-01-01")
    if fr_10y: ok.append("FRED:IRLTLT01FRM156N")
    else:
        failed.append("FRED:IRLTLT01FRM156N")
        fr_10y = {"dates":  [d for d, v in FR_10Y_FALLBACK],
                  "values": [v for d, v in FR_10Y_FALLBACK],
                  "source_url": "fallback hardcoded · FRED IRLTLT01FRM156N archives",
                  "stale": True}

    fr_gdp = fetch_fred_csv("CLVMNACSCAB1GQFR", start="2000-01-01")
    if fr_gdp: ok.append("FRED:CLVMNACSCAB1GQFR")
    else: failed.append("FRED:CLVMNACSCAB1GQFR")

    fr_unemp = fetch_fred_csv("LRHUTTTTFRM156S", start="2000-01-01")
    if fr_unemp: ok.append("FRED:LRHUTTTTFRM156S")
    else:
        failed.append("FRED:LRHUTTTTFRM156S")
        fr_unemp = {"dates":  [d for d, v in FR_UNEMP_FALLBACK],
                    "values": [v for d, v in FR_UNEMP_FALLBACK],
                    "source_url": "fallback hardcoded · FRED LRHUTTTTFRM156S archives",
                    "stale": True}

    # LRHU24TTFRM156S = taux de chômage 15-24 (OCDE, % de la population active 15-24).
    # L'ancienne série LRHUADTTFRM156S était le RATIO chômeurs 15-24 / population
    # totale 15-24 (~6 %), pas le taux de chômage des actifs — caption fausse.
    fr_youth = fetch_fred_csv("LRHU24TTFRM156S", start="2000-01-01")
    if fr_youth: ok.append("FRED:LRHU24TTFRM156S")
    else:
        failed.append("FRED:LRHU24TTFRM156S")
        fr_youth = {"dates":  [d for d, v in FR_YOUTH_FALLBACK],
                    "values": [v for d, v in FR_YOUTH_FALLBACK],
                    "source_url": "fallback hardcoded · FRED LRHU24TTFRM156S archives",
                    "stale": True}

    # ── BCE · taux directeurs (live FRED) ──────────────────────────
    ecb_depo = fetch_fred_csv("ECBDFR", start="2000-01-01")
    if ecb_depo: ok.append("FRED:ECBDFR")
    else:
        failed.append("FRED:ECBDFR")
        ecb_depo = {"dates": [d for d, v in ECB_DEPO_FALLBACK],
                    "values": [v for d, v in ECB_DEPO_FALLBACK],
                    "source_url": "fallback hardcoded · FRED ECBDFR", "stale": True}

    ecb_refi = fetch_fred_csv("ECBMRRFR", start="2000-01-01")
    if ecb_refi: ok.append("FRED:ECBMRRFR")
    else:
        failed.append("FRED:ECBMRRFR")
        ecb_refi = {"dates": [d for d, v in ECB_REFI_FALLBACK],
                    "values": [v for d, v in ECB_REFI_FALLBACK],
                    "source_url": "fallback hardcoded · FRED ECBMRRFR", "stale": True}

    # ── Bund 10Y + spread OAT-Bund (dérivé FR − DE, en points de base) ──
    de_10y = fetch_fred_csv("IRLTLT01DEM156N", start="2000-01-01")
    if de_10y: ok.append("FRED:IRLTLT01DEM156N")
    else:
        failed.append("FRED:IRLTLT01DEM156N")
        de_10y = {"dates": [d for d, v in DE_10Y_FALLBACK],
                  "values": [v for d, v in DE_10Y_FALLBACK],
                  "source_url": "fallback hardcoded · FRED IRLTLT01DEM156N", "stale": True}

    de_map = dict(zip(de_10y["dates"], de_10y["values"]))
    spr_dates, spr_vals = [], []
    for dt, fv in zip(fr_10y["dates"], fr_10y["values"]):
        dv = de_map.get(dt)
        if dv is None:
            continue
        spr_dates.append(dt)
        spr_vals.append(round((fv - dv) * 100, 1))  # pp → bps
    oat_bund_spread = {"dates": spr_dates, "values": spr_vals,
                       "source_url": "FRED IRLTLT01FRM156N − IRLTLT01DEM156N"}

    # ── Charge d'intérêt + déficit LIVE (Eurostat EDP, annuel) ──────
    eu_interest = fetch_eurostat_series("D41PAY", "MIO_EUR")
    if eu_interest: ok.append("Eurostat:D41PAY")
    else: failed.append("Eurostat:D41PAY")
    eu_interest_pct = fetch_eurostat_series("D41PAY", "PC_GDP")
    eu_deficit = fetch_eurostat_series("B9", "PC_GDP")
    if eu_deficit: ok.append("Eurostat:B9")
    else: failed.append("Eurostat:B9")

    fr_interest_live = None
    if eu_interest:
        fr_interest_live = {
            "years": eu_interest["years"],
            "bn_eur": [round(v / 1000.0, 1) for v in eu_interest["values"]],
            "pct_gdp": (eu_interest_pct["values"] if eu_interest_pct else None),
            "source_url": eu_interest["source_url"],
        }
    fr_deficit_gdp = None
    if eu_deficit:
        fr_deficit_gdp = {"years": eu_deficit["years"], "values": eu_deficit["values"],
                          "source_url": eu_deficit["source_url"]}

    # ── Dette brute Maastricht LIVE (Eurostat EDP, GD) ─────────────
    eu_debt = fetch_eurostat_series("GD", "MIO_EUR")
    eu_debt_pct = fetch_eurostat_series("GD", "PC_GDP")
    if eu_debt: ok.append("Eurostat:GD")
    else: failed.append("Eurostat:GD")

    fr_debt_live = None
    if eu_debt:
        fr_debt_live = {
            "years": eu_debt["years"],
            "bn_eur": [round(v / 1000.0, 1) for v in eu_debt["values"]],
            "pct_gdp": (eu_debt_pct["values"] if eu_debt_pct else None),
            "source_url": eu_debt["source_url"],
        }

    # ── Solde primaire LIVE = capacité de financement B9 + intérêts D41PAY ──
    # (le solde primaire neutralise la charge d'intérêt : B9 est déjà net
    #  d'intérêts, on les rajoute pour isoler l'effort budgétaire hors dette.)
    fr_primary_live = None
    if eu_deficit and eu_interest_pct:
        int_by_year = dict(zip(eu_interest_pct["years"], eu_interest_pct["values"]))
        yrs, vals = [], []
        for y, v in zip(eu_deficit["years"], eu_deficit["values"]):
            iv = int_by_year.get(y)
            if iv is None:
                continue
            yrs.append(y)
            vals.append(round(v + iv, 2))
        if yrs:
            fr_primary_live = {"years": yrs, "values": vals,
                               "source_url": eu_deficit["source_url"]}

    # ── Taux apparent LIVE = intérêts payés (t) / dette brute (t−1) ────
    fr_avg_rate_live = None
    if eu_interest and eu_debt:
        debt_by_year = dict(zip(eu_debt["years"], eu_debt["values"]))
        yrs, vals = [], []
        for y, paid in zip(eu_interest["years"], eu_interest["values"]):
            stock = debt_by_year.get(y - 1)
            if not stock:
                continue
            yrs.append(y)
            vals.append(round(100.0 * paid / stock, 2))
        if yrs:
            fr_avg_rate_live = {"years": yrs, "values": vals,
                                "source_url": eu_interest["source_url"]}

    # ══ TISSU PRODUCTIF ═══════════════════════════════════════════
    # ── Défaillances d'entreprises LIVE (Banque de France Stat Info) ──
    defaillances_live = fetch_bdf_defaillances()
    if defaillances_live: ok.append("BdF:StatInfo-defaillances")
    else: failed.append("BdF:StatInfo-defaillances")

    # ── Part de la VA manufacturière dans la VA totale (Eurostat annuel) ──
    GEO_COMP = ["FR", "DE", "IT", "EU27_2020"]
    va_cube = fetch_eurostat_cube("nama_10_a10", ["geo"], geo=GEO_COMP,
                                  nace_r2="C", na_item="B1G", unit="PC_TOT")
    fr_va_manuf_share = None
    if va_cube:
        ok.append("Eurostat:nama_10_a10")
        fr_va_manuf_share = {
            "series": {g: _cube_to_series(va_cube, (g,)) for g in GEO_COMP},
            "source_url": ("https://ec.europa.eu/eurostat/databrowser/view/"
                           "nama_10_a10"),
        }
    else:
        failed.append("Eurostat:nama_10_a10")

    # ── Production manufacturière mensuelle, indice 2021=100 (CVS-CJO) ──
    GEO_PROD = ["FR", "DE", "IT", "ES"]
    prod_cube = fetch_eurostat_cube("sts_inpr_m", ["geo"], geo=GEO_PROD,
                                    nace_r2="C", s_adj="SCA", unit="I21")
    fr_prod_indus = None
    if prod_cube:
        ok.append("Eurostat:sts_inpr_m")
        fr_prod_indus = {
            "series": {g: _cube_to_series(prod_cube, (g,)) for g in GEO_PROD},
            "source_url": ("https://ec.europa.eu/eurostat/databrowser/view/"
                           "sts_inpr_m"),
        }
    else:
        failed.append("Eurostat:sts_inpr_m")

    # ── Emploi manufacturier France, milliers de personnes (trimestriel) ──
    emp_cube = fetch_eurostat_cube("lfsq_egan2", ["geo"], geo="FR", nace_r2="C",
                                   sex="T", age="Y15-74", unit="THS_PER")
    fr_emploi_manuf = None
    if emp_cube:
        ok.append("Eurostat:lfsq_egan2")
        s = _cube_to_series(emp_cube, ("FR",))
        if s:
            fr_emploi_manuf = dict(s, source_url=(
                "https://ec.europa.eu/eurostat/databrowser/view/lfsq_egan2"))
    else:
        failed.append("Eurostat:lfsq_egan2")

    # ── Balance commerciale par grand produit SITC (Md€, monde) ────
    SITC = {
        "TOTAL":   "Total",
        "SITC3":   "Énergie",
        "SITC6_8": "Biens manufacturés",
        "SITC7":   "Machines et matériel de transport",
        "SITC5":   "Chimie",
        "SITC0_1": "Agroalimentaire",
    }
    trade_cube = fetch_eurostat_cube("ext_lt_intertrd", ["sitc06"], geo="FR",
                                     indic_et="MIO_BAL_VAL", partner="WORLD")
    fr_trade_sitc = None
    if trade_cube:
        ok.append("Eurostat:ext_lt_intertrd")
        series = {}
        for code, label in SITC.items():
            s = _cube_to_series(trade_cube, (code,))
            if s:
                series[code] = {"label": label, "x": s["x"],
                                "y": [round(v / 1000.0, 1) for v in s["y"]]}
        if series:
            fr_trade_sitc = {"series": series, "source_url": (
                "https://ec.europa.eu/eurostat/databrowser/view/ext_lt_intertrd")}
    else:
        failed.append("Eurostat:ext_lt_intertrd")

    meta = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_at_unix": int(time.time()),
        "sources_ok": ok, "sources_failed": failed,
        "doc_version": "1.0",
    }
    payload = {
        "meta": meta,
        "fr_10y": fr_10y,
        "fr_gdp": fr_gdp,
        "fr_unemployment": fr_unemp,
        "fr_youth_unemployment": fr_youth,
        "fr_debt_bn": [{"year": y, "bn_eur": v} for y, v in FR_DEBT_BN_EUR],
        "fr_interest_charge": [{"year": y, "bn_eur": v} for y, v in FR_INTEREST_CHARGE_BN],
        "fr_primary_balance": [{"year": y, "pct_gdp": v} for y, v in FR_PRIMARY_BALANCE],
        "acoss_deficit": [{"year": y, "bn_eur": v} for y, v in ACOSS_DEFICIT],
        "aft_financing": [{"year": y, "bn_eur": v} for y, v in AFT_FINANCING],
        "political_timeline": [{"year": y, "event": e} for y, e in FR_POLITICAL_TIMELINE],
        "fr_avg_rate": [{"year": y, "pct": v} for y, v in FR_AVG_RATE],
        # ── Couche aiguë juin 2026 ──
        "ecb_deposit": ecb_depo,
        "ecb_refi": ecb_refi,
        "de_10y": de_10y,
        "oat_bund_spread": oat_bund_spread,
        "fr_interest_live": fr_interest_live,
        "fr_deficit_gdp": fr_deficit_gdp,
        "fr_defaillances": [{"year": y, "count": c} for y, c in FR_DEFAILLANCES],
        # ── Tissu productif · tout live ──
        "fr_defaillances_live": defaillances_live,
        "fr_va_manuf_share": fr_va_manuf_share,
        "fr_prod_indus": fr_prod_indus,
        "fr_emploi_manuf": fr_emploi_manuf,
        "fr_trade_sitc": fr_trade_sitc,
        # ── Séries budgétaires passées en live (Eurostat EDP) ──
        "fr_debt_live": fr_debt_live,
        "fr_primary_balance_live": fr_primary_live,
        "fr_avg_rate_live": fr_avg_rate_live,
        "fr_hors_bilan": [{"label": l, "bn_eur": v, "kind": k}
                          for l, v, k in FR_HORS_BILAN],
        # ── Enrichissement mai 2026 ──
        "fr_solde_naturel": [{"year": y, "naissances_k": n, "deces_k": d}
                              for y, n, d in FR_SOLDE_NATUREL],
        "fr_elec_low_carbon": [{"year": y, "pct": p} for y, p in FR_ELEC_LOW_CARBON_PCT],
        "prelevements_obligatoires": [{"country": c, "pct_gdp": p, "satisfaction": s}
                                       for c, p, s in PRELEVEMENTS_OBLIGATOIRES],
        "fr_food_imports": [{"category": c, "pct": p} for c, p in FR_FOOD_IMPORTS],
        "fr_drought_communes": [{"year": y, "communes": c} for y, c in FR_DROUGHT_COMMUNES],
        "eu_insect_biomass": [{"year": y, "pct_1989": p} for y, p in EU_INSECT_BIOMASS],
        "fr_strengths": [{"sector": s, "value_bn": v, "type": t, "comment": c}
                          for s, v, t, c in FR_STRENGTHS],
        "scenarios_fr": [{"name": n, "proba_pct": p, "horizon": h, "decel": d, "comment": c}
                          for n, p, h, d, c in SCENARIOS_FR],
        "incompatible_demands": [{"demand": d, "pct_agree": p}
                                  for d, p in FR_INCOMPATIBLE_DEMANDS],
    }
    return payload, len(ok), len(failed)


def write_outputs(payload):
    OUT_JSON.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    js = (
        f"/* these_stagnation_cache.js — generated {payload['meta']['updated_at']} */\n"
        f"window.__THESE_STAGNATION__ = "
        f"{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)};\n"
    )
    OUT_JS.write_text(js)
    site_dir = Path.home() / "Desktop" / "Site_Crypto_Finance"
    if site_dir.exists():
        for name in ("these_stagnation_cache.json", "these_stagnation_cache.js"):
            link = site_dir / name
            target = CACHE_DIR / name
            try:
                if link.is_symlink() or link.exists(): link.unlink()
                link.symlink_to(target)
            except OSError:
                shutil.copy2(target, link)


def main():
    t0 = time.time()
    payload, n_ok, n_fail = build_payload()
    write_outputs(payload)
    dt = time.time() - t0
    sys.stdout.write(f"[these_stagnation] OK · {n_ok} sources, {n_fail} failed · {dt:.1f}s\n")


if __name__ == "__main__":
    main()
