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
 Audit 04/10/2026 (sources fraîches, sans clé, repli = passage précédent) :
 11. TradingView · TVC:FR10Y / TVC:DE10Y — taux 10 ans AU JOUR + écart du jour
 12. Insee BDM · dette de Maastricht TRIMESTRIELLE (Md€, % PIB, dont ASSO)
 13. Insee BDM · naissances / décès annuels + 12 mois glissants mensuels
 14. VigiEau (API publique) · part du territoire sous restriction d'eau
 15. OCDE SDMX · recettes fiscales % PIB × satisfaction des services publics
 16. FRED · LRHU24TTDEM156S (chômage 15-24 ans Allemagne) ; Eurostat GD (DE)

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
    (2023, 270), (2024, 285), (2025, 300), (2026, 310),
]

# Élections / fragmentation politique
# (audit 04/10/2026) La censure du gouvernement Barnier date du 4 décembre 2024,
# pas de 2025 ; « 3 blocs ~180 sièges chacun » était faux (≈ 190 / 165 / 140).
FR_POLITICAL_TIMELINE = [
    (2017, "Macron 1 — majorité absolue (308 sièges pour LREM seule)"),
    (2022, "Macron 2 — majorité relative (245 sièges, 289 requis)"),
    (2024, "Dissolution (juin) : trois blocs de 140 à 190 sièges ; "
           "censure du gouvernement Barnier (décembre)"),
    (2025, "Chute du gouvernement Bayrou (vote de confiance, septembre) ; "
           "Lecornu Premier ministre"),
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


# ════════════════════════════════════════════════════════════════
# AUDIT 04/10/2026 — sources fraîches, sans clé
# ════════════════════════════════════════════════════════════════
# Règle de repli : une source qui tombe reprend le bloc du passage PRÉCÉDENT
# (dernière valeur réellement collectée, marquée "reprise": true, son horodatage
# "maj" conservé) — jamais une constante périmée réinjectée en silence.

def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_previous():
    try:
        return json.loads(OUT_JSON.read_text())
    except Exception:
        return {}


def _reprise(prev, key):
    """Bloc du passage précédent, marqué comme repris (ou None)."""
    b = (prev or {}).get(key)
    if isinstance(b, dict) and b:
        b = dict(b)
        b["reprise"] = True
        return b
    return None


# ── Taux à 10 ans au JOUR (TradingView, même source que oblig_notes.py) ──
# L'historique long reste la moyenne MENSUELLE OCDE (FRED IRLTLT01…M156N) ;
# le quotidien la prolonge. Même définition : rendement de l'obligation d'État
# de référence à 10 ans (vérifié : moyenne du mois du quotidien − OCDE ≤ 3 pb
# de mai à août 2026). L'écart France − Allemagne est calculé DANS la même
# source, jour par jour, jamais entre deux sources.
TV_WS = "wss://data.tradingview.com/socket.io/websocket?from=chart%2F"
TV_SOURCE_URL = "https://www.tradingview.com/symbols/TVC-FR10Y/"


def fetch_tv_daily(symbol, n_bars=900, timeout=25):
    """Barres quotidiennes (clôture) d'un symbole TradingView → [(date, close)]."""
    try:
        import websocket  # websocket-client (requirements du cloud)
    except ImportError:
        sys.stderr.write("[TradingView] websocket-client absent\n")
        return None
    import random
    import string
    try:
        ws = websocket.create_connection(
            TV_WS, origin="https://www.tradingview.com",
            header=["User-Agent: " + UA_BROWSER], timeout=timeout)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[TradingView {symbol}] connexion : {e}\n")
        return None

    def send(m, p):
        s = json.dumps({"m": m, "p": p}, separators=(",", ":"))
        ws.send("~m~%d~m~%s" % (len(s), s))

    buf = ""
    try:
        cs = "cs_" + "".join(random.choice(string.ascii_lowercase) for _ in range(12))
        send("set_auth_token", ["unauthorized_user_token"])
        send("chart_create_session", [cs, ""])
        send("resolve_symbol", [cs, "sds_sym_1",
                                "=" + json.dumps({"symbol": symbol, "adjustment": "splits"})])
        send("create_series", [cs, "sds_1", "s1", "sds_sym_1", "1D", n_bars, ""])
        t0 = time.time()
        while time.time() - t0 < 2 * timeout:
            r = ws.recv()
            for hb in re.findall(r"~m~\d+~m~(~h~\d+)", r):
                ws.send("~m~%d~m~%s" % (len(hb), hb))
            buf += r
            if "series_completed" in r or "symbol_error" in r or "critical_error" in r:
                break
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[TradingView {symbol}] lecture : {e}\n")
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    m = re.search(r'"s":\[(.*?)\],"ns"', buf)
    if not m:
        return None
    try:
        bars = json.loads("[" + m.group(1) + "]")
    except ValueError:
        return None
    out = {}
    for b in bars:
        v = b.get("v") or []
        if len(v) < 5 or not isinstance(v[4], (int, float)):
            continue
        d = datetime.fromtimestamp(v[0], timezone.utc).date()
        if d.weekday() >= 5 or not (-2 < v[4] < 20):
            continue
        out[d.isoformat()] = round(float(v[4]), 4)
    rows = sorted(out.items())
    if len(rows) < 150:
        return None
    last = datetime.fromisoformat(rows[-1][0]).date()
    if (datetime.now(timezone.utc).date() - last).days > 10:
        sys.stderr.write(f"[TradingView {symbol}] dernière barre trop vieille : {last}\n")
        return None
    return rows


def _merge_daily(prev_block, rows, keep_days=1100):
    """Fusionne avec le passage précédent (le nouveau l'emporte), fenêtre glissante."""
    d = {}
    if isinstance(prev_block, dict):
        d.update(zip(prev_block.get("dates") or [], prev_block.get("values") or []))
    d.update(rows)
    cut = (datetime.now(timezone.utc).date().toordinal() - keep_days)
    items = sorted((k, v) for k, v in d.items()
                   if datetime.fromisoformat(k).date().toordinal() >= cut)
    return {"dates": [k for k, _ in items], "values": [v for _, v in items]}


def build_daily_yields(prev, fr_m, de_m, ok, failed):
    """→ (fr_10y_daily, de_10y_daily, oat_bund_spread_daily) ou reprises."""
    res = {}
    for code, sym in (("fr", "TVC:FR10Y"), ("de", "TVC:DE10Y")):
        key = code + "_10y_daily"
        rows = fetch_tv_daily(sym)
        if rows:
            ok.append("TradingView:" + sym)
            b = _merge_daily(prev.get(key), rows)
            b.update({"source": "TradingView · " + sym + " (clôture du jour)",
                      "source_url": "https://www.tradingview.com/symbols/" + sym.replace(":", "-") + "/",
                      "maj": _now_iso()})
            res[key] = b
        else:
            failed.append("TradingView:" + sym)
            res[key] = _reprise(prev, key)
    fr, de = res.get("fr_10y_daily"), res.get("de_10y_daily")
    spread = None
    if fr and de:
        dm = dict(zip(de["dates"], de["values"]))
        ds, vs = [], []
        for dt, fv in zip(fr["dates"], fr["values"]):
            dv = dm.get(dt)
            if dv is None:
                continue
            ds.append(dt)
            vs.append(round((fv - dv) * 100, 1))
        if ds:
            spread = {"dates": ds, "values": vs,
                      "source": "TradingView · TVC:FR10Y − TVC:DE10Y, même jour",
                      "source_url": TV_SOURCE_URL,
                      "maj": fr.get("maj"),
                      "reprise": bool(fr.get("reprise") or de.get("reprise"))}
    # Contrôle de jonction : moyenne du mois du quotidien vs moyenne mensuelle OCDE.
    if fr and fr_m and fr_m.get("dates"):
        by = {}
        for dt, v in zip(fr["dates"], fr["values"]):
            by.setdefault(dt[:7], []).append(v)
        om = {d[:7]: v for d, v in zip(fr_m["dates"], fr_m["values"])}
        ecarts = [(m, round(100 * (sum(v) / len(v) - om[m]), 1))
                  for m, v in sorted(by.items()) if m in om and len(v) >= 15]
        if ecarts:
            fr["jonction"] = {"mois": [m for m, _ in ecarts][-12:],
                              "ecart_pb": [e for _, e in ecarts][-12:],
                              "ecart_max_pb": max(abs(e) for _, e in ecarts[-12:])}
    return fr, de, spread


# ── Insee BDM (SDMX, sans clé) ─────────────────────────────────
INSEE_BDM = "https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/"


def fetch_insee(idbanks):
    """{idbank: {"obs": [(période, valeur)], "maj": LAST_UPDATE, "statut": {période: P|A}}}."""
    import math
    try:
        txt = http_get_text(INSEE_BDM + "+".join(idbanks), timeout=60,
                            max_retries=3, accept="application/xml")
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[Insee {idbanks}] {e}\n")
        return None
    out = {}
    for s in re.finditer(r"<Series ([^>]*)>(.*?)</Series>", txt, re.S):
        a = dict(re.findall(r'(\w+)="([^"]*)"', s.group(1)))
        obs, st = [], {}
        for o in re.findall(r"<Obs ([^>]*?)/>", s.group(2)):
            od = dict(re.findall(r'(\w+)="([^"]*)"', o))
            try:
                v = float(od["OBS_VALUE"])
            except (KeyError, ValueError):
                continue
            if math.isnan(v):
                continue
            obs.append((od["TIME_PERIOD"], v))
            st[od["TIME_PERIOD"]] = od.get("OBS_STATUS", "A")
        obs.sort()
        if a.get("IDBANK") and obs:
            out[a["IDBANK"]] = {"obs": obs, "maj": a.get("LAST_UPDATE"), "statut": st}
    return out or None


def build_debt_q(prev, ok, failed):
    """Dette de Maastricht TRIMESTRIELLE (Insee, base 2020) : Md€, % PIB, dont ASSO."""
    ids = {"bn": "010777616", "pct": "010777608", "asso": "010777625"}
    d = fetch_insee(list(ids.values()))
    if not d or ids["bn"] not in d:
        failed.append("Insee:dette-trimestrielle")
        return _reprise(prev, "fr_debt_q")
    ok.append("Insee:dette-trimestrielle")
    bn = d[ids["bn"]]["obs"]
    pct = dict(d.get(ids["pct"], {}).get("obs", []))
    asso = dict(d.get(ids["asso"], {}).get("obs", []))
    return {
        "x": [p for p, _ in bn],
        "bn_eur": [v for _, v in bn],
        "pct_gdp": [pct.get(p) for p, _ in bn],
        "asso_bn_eur": [asso.get(p) for p, _ in bn],
        "insee_maj": d[ids["bn"]]["maj"],
        "maj": _now_iso(),
        "source": "Insee · dette trimestrielle de Maastricht des APU (base 2020)",
        "source_url": "https://www.insee.fr/fr/statistiques/serie/010777616",
    }


def build_demo(prev, ok, failed):
    """Naissances / décès : séries annuelles Insee + 12 mois glissants mensuels."""
    ids = {"nais_a": "001641590", "dec_a": "001641592",
           "nais_m": "001641601", "dec_m": "001641603"}
    d = fetch_insee(list(ids.values()))
    if not d or ids["nais_a"] not in d or ids["dec_a"] not in d:
        failed.append("Insee:naissances-deces")
        return _reprise(prev, "fr_demo_live")
    ok.append("Insee:naissances-deces")
    na, da = dict(d[ids["nais_a"]]["obs"]), dict(d[ids["dec_a"]]["obs"])
    years = sorted(set(na) & set(da))
    prov = [y for y in years
            if d[ids["nais_a"]]["statut"].get(y) == "P" or d[ids["dec_a"]]["statut"].get(y) == "P"]
    out = {
        "years": [int(y) for y in years],
        "naissances": [int(na[y]) for y in years],
        "deces": [int(da[y]) for y in years],
        "provisoire": [int(y) for y in prov],
        "insee_maj": d[ids["nais_a"]]["maj"],
        "maj": _now_iso(),
        "source": "Insee · bilan démographique (France, Mayotte incluse depuis 2014)",
        "source_url": "https://www.insee.fr/fr/statistiques/serie/001641590",
    }
    # 12 mois glissants : seulement si les 12 derniers mois existent pour les deux.
    nm = dict(d.get(ids["nais_m"], {}).get("obs", []))
    dm = dict(d.get(ids["dec_m"], {}).get("obs", []))
    commun = sorted(set(nm) & set(dm))
    if commun:
        fin = commun[-1]
        y, mo = int(fin[:4]), int(fin[5:7])
        mois = []
        for i in range(12):
            mm, yy = mo - i, y
            while mm <= 0:
                mm += 12
                yy -= 1
            mois.append(f"{yy:04d}-{mm:02d}")
        # Un glissement qui finit en décembre = l'année civile déjà publiée.
        if all(m in nm and m in dm for m in mois) and fin[5:7] != "12" \
                and int(fin[:4]) > out["years"][-1]:
            out["glissant"] = {"fin": fin,
                               "naissances": int(sum(nm[m] for m in mois)),
                               "deces": int(sum(dm[m] for m in mois)),
                               "insee_maj": d.get(ids["dec_m"], {}).get("maj"),
                               "source_url": "https://www.insee.fr/fr/statistiques/serie/001641601"}
    return out


# ── Sécheresse : part du territoire sous restriction (VigiEau, API publique) ──
# Remplace l'ancien décompte de « communes sous restriction » (constantes non
# sourçables, 2024 = 11 200 alors que 2024 fut une année humide : pic VigiEau
# à 13 % du territoire). Mesure : eaux superficielles (ESU), niveaux alerte +
# alerte renforcée + crise (la « vigilance » n'impose aucune restriction).
VIGIEAU_AREA = "https://api.vigieau.gouv.fr/api/data/area?dateDebut={d0}&dateFin={d1}"


def build_drought(prev, ok, failed):
    d1 = datetime.now(timezone.utc).date().isoformat()
    url = VIGIEAU_AREA.format(d0="2013-01-01", d1=d1)
    try:
        rows = json.loads(http_get_text(url, timeout=90, max_retries=3,
                                        accept="application/json"))
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[VigiEau] {e}\n")
        rows = None
    if not rows or not isinstance(rows, list):
        failed.append("VigiEau:area")
        return _reprise(prev, "fr_drought_area")

    def niv(x):
        v = x.get("ESU") or {}
        try:
            return sum(float(v.get(k) or 0) for k in ("alerte", "alerte_renforcee", "crise"))
        except (TypeError, ValueError):
            return None

    par = {}
    for x in rows:
        dt, v = x.get("date"), niv(x)
        if not dt or v is None:
            continue
        par.setdefault(dt[:4], []).append((dt, v))
    years, peak, pdate, cover = [], [], [], []
    for y in sorted(par):
        xs = par[y]
        dt, v = max(xs, key=lambda t: t[1])
        ete = sum(1 for d, _ in xs if f"{y}-06-01" <= d <= f"{y}-09-30")
        years.append(int(y))
        peak.append(round(v, 1))
        pdate.append(dt)
        cover.append(ete)
    if not years:
        failed.append("VigiEau:area")
        return _reprise(prev, "fr_drought_area")
    ok.append("VigiEau:area")
    last = max(rows, key=lambda x: x.get("date") or "")
    return {
        "years": years, "peak_pct": peak, "peak_date": pdate,
        "jours_ete": cover,               # jours publiés entre le 1er juin et le 30 sept. (122 = complet)
        "last_date": last.get("date"), "last_pct": round(niv(last) or 0, 1),
        "mesure": "part du territoire (eaux superficielles) en alerte, alerte renforcée ou crise",
        "maj": _now_iso(),
        "source": "VigiEau · ministère de la Transition écologique",
        "source_url": "https://vigieau.gouv.fr/",
        "api": "https://api.vigieau.gouv.fr/api/data/area",
    }


# ── Prélèvements obligatoires (OCDE Revenue Statistics) × satisfaction (OCDE,
#    enquête sur la confiance) : remplace des constantes non sourçables
#    (« 45,6 % », « 32 % de satisfaits » : l'OCDE donne 43,5 % en 2024 et
#    une satisfaction française de 47 à 61 % selon le service). ──
OECD_SDMX = "https://sdmx.oecd.org/public/rest/data/"
PO_PAYS = {"FRA": "France", "DNK": "Danemark", "BEL": "Belgique", "ITA": "Italie",
           "DEU": "Allemagne", "NLD": "Pays-Bas", "GBR": "Royaume-Uni",
           "ESP": "Espagne", "CHE": "Suisse", "SWE": "Suède", "FIN": "Finlande",
           "USA": "États-Unis"}
SAT_MES = {"TRUST_S_AS": "démarches administratives", "CS_ES": "système éducatif",
           "CS_HC": "système de santé"}


def _oecd_csv(path):
    txt = http_get_text(OECD_SDMX + path, timeout=90, max_retries=3,
                        accept="application/vnd.sdmx.data+csv; charset=utf-8")
    return list(csv.DictReader(io.StringIO(txt.lstrip("﻿"))))


def build_prelevements(prev, ok, failed):
    y0 = datetime.now(timezone.utc).year - 6
    try:
        tax = _oecd_csv("OECD.CTP.TPS,DSD_REV_COMP_OECD@DF_RSOECD,/"
                        + "+".join(PO_PAYS) + ".TAX_REV.S13._T._T.PT_B1GQ.A"
                        + f"?startPeriod={y0}&dimensionAtObservation=AllDimensions")
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[OCDE recettes] {e}\n")
        tax = None
    sat, edition = None, None
    for ed in range(datetime.now(timezone.utc).year + 1, 2024, -1):
        try:
            rows = _oecd_csv(f"OECD.GOV.GIP,DSD_GOV_INT@DF_GOV_SPS_{ed},/all"
                             "?dimensionAtObservation=AllDimensions")
        except Exception:  # noqa: BLE001
            continue
        rows = [r for r in rows if r.get("MEASURE") in SAT_MES and r.get("REF_AREA") in PO_PAYS]
        if rows:
            sat, edition = rows, ed
            break
    if not tax or not sat:
        failed.append("OCDE:prelevements-satisfaction")
        return _reprise(prev, "prelevements_live")
    ok.append("OCDE:prelevements-satisfaction")
    t_last = {}
    for r in tax:
        try:
            v = float(r["OBS_VALUE"])
        except (KeyError, ValueError, TypeError):
            continue
        c, y = r["REF_AREA"], r["TIME_PERIOD"]
        if c not in t_last or y > t_last[c][0]:
            t_last[c] = (y, v)
    s_last = {}
    for r in sat:
        try:
            v = float(r["OBS_VALUE"])
        except (KeyError, ValueError, TypeError):
            continue
        k = (r["REF_AREA"], r["MEASURE"])
        if k not in s_last or r["TIME_PERIOD"] > s_last[k][0]:
            s_last[k] = (r["TIME_PERIOD"], v)
    pays = []
    for c, nom in PO_PAYS.items():
        if c not in t_last:
            continue
        detail = {m: round(s_last[(c, m)][1], 1) for m in SAT_MES if (c, m) in s_last}
        pays.append({"code": c, "pays": nom, "annee_po": int(t_last[c][0]),
                     "po_pct_pib": round(t_last[c][1], 1),
                     "satisfaction": (round(sum(detail.values()) / len(detail), 1)
                                      if len(detail) == len(SAT_MES) else None),
                     "detail": detail})
    annees_sat = sorted({v[0] for v in s_last.values()})
    return {
        "pays": pays, "mesures": SAT_MES,
        "annee_satisfaction": annees_sat[-1] if annees_sat else None,
        "edition_satisfaction": edition,
        "maj": _now_iso(),
        "source": "OCDE · Revenue Statistics (recettes fiscales totales, % du PIB) ; "
                  "OCDE · Government at a Glance (enquête sur la confiance)",
        "source_url": "https://data-explorer.oecd.org/",
    }


def build_payload():
    ok, failed = [], []
    prev = load_previous()

    fr_10y = fetch_fred_csv("IRLTLT01FRM156N", start="2000-01-01")
    if fr_10y: ok.append("FRED:IRLTLT01FRM156N")
    elif _reprise(prev, "fr_10y"):
        failed.append("FRED:IRLTLT01FRM156N")
        fr_10y = _reprise(prev, "fr_10y")
    else:
        failed.append("FRED:IRLTLT01FRM156N")
        fr_10y = {"dates":  [d for d, v in FR_10Y_FALLBACK],
                  "values": [v for d, v in FR_10Y_FALLBACK],
                  "source_url": "fallback hardcoded · FRED IRLTLT01FRM156N archives",
                  "stale": True}

    fr_gdp = fetch_fred_csv("CLVMNACSCAB1GQFR", start="2000-01-01")
    if fr_gdp: ok.append("FRED:CLVMNACSCAB1GQFR")
    else:
        failed.append("FRED:CLVMNACSCAB1GQFR")
        fr_gdp = _reprise(prev, "fr_gdp")

    # Chômage des 15-24 ans en Allemagne : la comparaison du texte (« X % contre
    # Y % en Allemagne ») se calcule au lieu d'être écrite en dur.
    de_youth = fetch_fred_csv("LRHU24TTDEM156S", start="2000-01-01")
    if de_youth: ok.append("FRED:LRHU24TTDEM156S")
    else:
        failed.append("FRED:LRHU24TTDEM156S")
        de_youth = _reprise(prev, "de_youth_unemployment")

    fr_unemp = fetch_fred_csv("LRHUTTTTFRM156S", start="2000-01-01")
    if fr_unemp: ok.append("FRED:LRHUTTTTFRM156S")
    elif _reprise(prev, "fr_unemployment"):
        failed.append("FRED:LRHUTTTTFRM156S")
        fr_unemp = _reprise(prev, "fr_unemployment")
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
    elif _reprise(prev, "fr_youth_unemployment"):
        failed.append("FRED:LRHU24TTFRM156S")
        fr_youth = _reprise(prev, "fr_youth_unemployment")
    else:
        failed.append("FRED:LRHU24TTFRM156S")
        fr_youth = {"dates":  [d for d, v in FR_YOUTH_FALLBACK],
                    "values": [v for d, v in FR_YOUTH_FALLBACK],
                    "source_url": "fallback hardcoded · FRED LRHU24TTFRM156S archives",
                    "stale": True}

    # ── BCE · taux directeurs (live FRED) ──────────────────────────
    ecb_depo = fetch_fred_csv("ECBDFR", start="2000-01-01")
    if ecb_depo: ok.append("FRED:ECBDFR")
    elif _reprise(prev, "ecb_deposit"):
        failed.append("FRED:ECBDFR")
        ecb_depo = _reprise(prev, "ecb_deposit")
    else:
        failed.append("FRED:ECBDFR")
        ecb_depo = {"dates": [d for d, v in ECB_DEPO_FALLBACK],
                    "values": [v for d, v in ECB_DEPO_FALLBACK],
                    "source_url": "fallback hardcoded · FRED ECBDFR", "stale": True}

    ecb_refi = fetch_fred_csv("ECBMRRFR", start="2000-01-01")
    if ecb_refi: ok.append("FRED:ECBMRRFR")
    elif _reprise(prev, "ecb_refi"):
        failed.append("FRED:ECBMRRFR")
        ecb_refi = _reprise(prev, "ecb_refi")
    else:
        failed.append("FRED:ECBMRRFR")
        ecb_refi = {"dates": [d for d, v in ECB_REFI_FALLBACK],
                    "values": [v for d, v in ECB_REFI_FALLBACK],
                    "source_url": "fallback hardcoded · FRED ECBMRRFR", "stale": True}

    # ── Bund 10Y + spread OAT-Bund (dérivé FR − DE, en points de base) ──
    de_10y = fetch_fred_csv("IRLTLT01DEM156N", start="2000-01-01")
    if de_10y: ok.append("FRED:IRLTLT01DEM156N")
    elif _reprise(prev, "de_10y"):
        failed.append("FRED:IRLTLT01DEM156N")
        de_10y = _reprise(prev, "de_10y")
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

    fr_interest_live = _reprise(prev, "fr_interest_live")
    if eu_interest:
        fr_interest_live = {
            "years": eu_interest["years"],
            "bn_eur": [round(v / 1000.0, 1) for v in eu_interest["values"]],
            "pct_gdp": (eu_interest_pct["values"] if eu_interest_pct else None),
            "source_url": eu_interest["source_url"],
        }
    fr_deficit_gdp = _reprise(prev, "fr_deficit_gdp")
    if eu_deficit:
        fr_deficit_gdp = {"years": eu_deficit["years"], "values": eu_deficit["values"],
                          "source_url": eu_deficit["source_url"]}

    # ── Dette brute Maastricht LIVE (Eurostat EDP, GD) ─────────────
    eu_debt = fetch_eurostat_series("GD", "MIO_EUR")
    eu_debt_pct = fetch_eurostat_series("GD", "PC_GDP")
    if eu_debt: ok.append("Eurostat:GD")
    else: failed.append("Eurostat:GD")

    # Allemagne (% PIB) : « loin devant l'Allemagne (X %) » se calcule.
    de_debt_pct = fetch_eurostat_series("GD", "PC_GDP", geo="DE")
    if de_debt_pct:
        ok.append("Eurostat:GD-DE")
        de_debt_pct = {"years": de_debt_pct["years"], "values": de_debt_pct["values"],
                       "source_url": de_debt_pct["source_url"]}
    else:
        failed.append("Eurostat:GD-DE")
        de_debt_pct = _reprise(prev, "de_debt_pct")

    fr_debt_live = _reprise(prev, "fr_debt_live")
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
    fr_primary_live = _reprise(prev, "fr_primary_balance_live")
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
    fr_avg_rate_live = _reprise(prev, "fr_avg_rate_live")
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
    if defaillances_live:
        ok.append("BdF:StatInfo-defaillances")
        defaillances_live["maj"] = _now_iso()
    else:
        failed.append("BdF:StatInfo-defaillances")
        defaillances_live = _reprise(prev, "fr_defaillances_live")

    # ── Part de la VA manufacturière dans la VA totale (Eurostat annuel) ──
    GEO_COMP = ["FR", "DE", "IT", "EU27_2020"]
    va_cube = fetch_eurostat_cube("nama_10_a10", ["geo"], geo=GEO_COMP,
                                  nace_r2="C", na_item="B1G", unit="PC_TOT")
    fr_va_manuf_share = _reprise(prev, "fr_va_manuf_share")
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
    fr_prod_indus = _reprise(prev, "fr_prod_indus")
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
    fr_emploi_manuf = _reprise(prev, "fr_emploi_manuf")
    if emp_cube:
        ok.append("Eurostat:lfsq_egan2")
        s = _cube_to_series(emp_cube, ("FR",))
        if s:
            fr_emploi_manuf = dict(s, source_url=(
                "https://ec.europa.eu/eurostat/databrowser/view/lfsq_egan2"))
    else:
        failed.append("Eurostat:lfsq_egan2")

    # ── Dépenses publiques totales, % du PIB (Eurostat gov_10a_main, TE) ──
    # « 57 % du PIB, contre 50 % en Allemagne » : se calcule (audit 04/10/2026).
    dep_cube = fetch_eurostat_cube("gov_10a_main", ["geo"], geo=["FR", "DE", "EU27_2020"],
                                   na_item="TE", sector="S13", unit="PC_GDP")
    public_spending = _reprise(prev, "public_spending")
    if dep_cube:
        ok.append("Eurostat:gov_10a_main")
        public_spending = {
            "series": {g: _cube_to_series(dep_cube, (g,)) for g in ("FR", "DE", "EU27_2020")},
            "source_url": "https://ec.europa.eu/eurostat/databrowser/view/gov_10a_main",
            "maj": _now_iso(),
        }
    else:
        failed.append("Eurostat:gov_10a_main")

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
    fr_trade_sitc = _reprise(prev, "fr_trade_sitc")
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

    # ══ AUDIT 04/10/2026 · sources fraîches ═══════════════════════
    fr_10y_daily, de_10y_daily, spread_daily = build_daily_yields(
        prev, fr_10y, de_10y, ok, failed)
    fr_debt_q = build_debt_q(prev, ok, failed)
    fr_demo_live = build_demo(prev, ok, failed)
    fr_drought_area = build_drought(prev, ok, failed)
    prelevements_live = build_prelevements(prev, ok, failed)

    # Horodatage du dernier SUCCÈS par source (repris du passage précédent
    # pour les sources tombées cette fois-ci).
    sources_maj = dict(((prev.get("meta") or {}).get("sources_maj") or {}))
    for name in ok:
        sources_maj[name] = _now_iso()

    meta = {
        "sources_maj": sources_maj,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_at_unix": int(time.time()),
        "sources_ok": ok, "sources_failed": failed,
        "doc_version": "1.1",
    }
    payload = {
        "meta": meta,
        "fr_10y": fr_10y,
        "fr_gdp": fr_gdp,
        "fr_unemployment": fr_unemp,
        "fr_youth_unemployment": fr_youth,
        "de_youth_unemployment": de_youth,
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
        # ── Audit 04/10/2026 : quotidien, trimestriel, API publiques ──
        "fr_10y_daily": fr_10y_daily,
        "de_10y_daily": de_10y_daily,
        "oat_bund_spread_daily": spread_daily,
        "fr_debt_q": fr_debt_q,
        "de_debt_pct": de_debt_pct,
        "fr_demo_live": fr_demo_live,
        "fr_drought_area": fr_drought_area,
        "prelevements_live": prelevements_live,
        "public_spending": public_spending,
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
