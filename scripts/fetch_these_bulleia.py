#!/usr/bin/env python3
"""Cache du chapitre Thèse · 02 — La bulle IA.

Version 2.0 (audit du 04/10/2026).

En direct (gratuit, sans clé) :
  1. SEC EDGAR companyfacts (XBRL) : investissements (« achats d'immobilisations »
     du tableau de flux de trésorerie) et chiffre d'affaires, TRIMESTRE PAR
     TRIMESTRE, des Sept Magnifiques + Oracle. Les 10-Q donnent des cumuls depuis
     le début de l'exercice : les trimestres sont reconstitués par différence et
     rangés en trimestres CIVILS (milieu de la période). Remplace yfinance, qui
     était limité en débit et servait en silence des constantes de mai 2026.
     User-Agent : variable SCF_CONTACT_UA (secret du dépôt cloud), sinon nom neutre.
  2. Epoch AI (CSV public notable_ai_models.csv) : coût de calcul de
     l'entraînement des grands modèles, estimation homogène en dollars 2023.
  3. Yahoo Finance v8 (^GSPC, RSP) : S&P 500 pondéré vs équipondéré.
  4. FRED (API officielle) : fret ferroviaire intermodal, consommation de biens,
     défauts immobilier commercial, crédit aux entreprises, production industrielle.

Recopié d'éditions publiées (URL et date dans REGISTRES) : électricité des data
centers (AIE), engagements et trajectoire financière d'OpenAI, prix des robots
humanoïdes, scores de référence des modèles de langage.

Repli : une source qui tombe garde la DERNIÈRE valeur réellement collectée (relue
dans le cache précédent) avec la date de son dernier succès ; plus aucune
constante de secours réinjectée en silence.

Sortie : these_bulleia_cache.json + .js (window.__THESE_BULLEIA__).
Lancé par scf.these_bulleia.refresh.
"""
import csv
import io
import json
import math
import os
import shutil
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "these_bulleia_cache.json"
OUT_JS = CACHE_DIR / "these_bulleia_cache.js"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) SiteCryptoFinance-TheseBulleIA/2.0"
SEC_UA = os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research")
DOC_VERSION = "2.0"


def http_get_text(url, timeout=30, max_retries=4, accept="text/csv,*/*", headers=None):
    h = {"User-Agent": UA, "Accept": accept}
    h.update(headers or {})
    last_err = None
    for attempt in range(max_retries):
        try:
            with urlopen(Request(url, headers=h), timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="ignore")
        except HTTPError as e:
            last_err = e
            if (500 <= e.code < 600 or e.code == 429) and attempt < max_retries - 1:
                time.sleep(5 * (2 ** attempt))
                continue
            raise
        except (URLError, ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(5 * (2 ** attempt))
    raise last_err if last_err else RuntimeError("retries exhausted")


# FRED via API officielle (la version CSV graph est cassée depuis ~mai 2026)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fred_helpers import fetch_fred as fetch_fred_csv  # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
# 1. SEC — investissements et chiffre d'affaires trimestriels
# ════════════════════════════════════════════════════════════════════════════

SEC_CIK = {"MSFT": "0000789019", "GOOGL": "0001652044", "AMZN": "0001018724",
           "META": "0001326801", "NVDA": "0001045810", "AAPL": "0000320193",
           "TSLA": "0001318605", "ORCL": "0001341439"}
SEC_NOMS = {"MSFT": "Microsoft", "GOOGL": "Alphabet", "AMZN": "Amazon", "META": "Meta",
            "NVDA": "Nvidia", "AAPL": "Apple", "TSLA": "Tesla", "ORCL": "Oracle"}
HYPERSCALERS = ["MSFT", "GOOGL", "AMZN", "META"]
MAG7 = ["NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "TSLA"]
# Même ligne du tableau de flux, étiquetée sous l'un ou l'autre concept selon les années
CONCEPTS_CAPEX = ["PaymentsToAcquirePropertyPlantAndEquipment",
                  "PaymentsToAcquireProductiveAssets"]
CONCEPTS_CA = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
               "SalesRevenueNet"]
PREMIERE_ANNEE = 2018


def _d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def sec_trimestres(facts):
    """{(début, fin): valeur} des trimestres, à partir des faits XBRL (10-K/10-Q).
    Trimestre publié tel quel (80-100 jours) en priorité ; sinon différence de deux
    cumuls qui partagent le même début d'exercice (T2 = S1 − T1, T4 = an − 9 mois)."""
    fx = {}
    for f in facts:
        if f.get("form") not in ("10-K", "10-Q", "10-K/A", "10-Q/A"):
            continue
        if not f.get("start") or not f.get("end") or f.get("val") is None:
            continue
        k = (f["start"], f["end"])
        if k not in fx or f.get("filed", "") > fx[k][1]:  # le dépôt le plus récent l'emporte
            fx[k] = (float(f["val"]), f.get("filed", ""))
    q = {}
    for (s, e), (v, _) in fx.items():
        if 80 <= (_d(e) - _d(s)).days <= 100:
            q[(s, e)] = v
    par_debut = {}
    for (s, e), (v, _) in fx.items():
        if 80 <= (_d(e) - _d(s)).days <= 380:
            par_debut.setdefault(s, []).append((e, v))
    for s, lst in par_debut.items():
        lst.sort()
        for (e0, v0), (e1, v1) in zip(lst, lst[1:]):
            if 80 <= (_d(e1) - _d(e0)).days <= 100:
                debut = date.fromordinal(_d(e0).toordinal() + 1).isoformat()
                q.setdefault((debut, e1), v1 - v0)
    return q


def trimestre_civil(s, e):
    mid = date.fromordinal((_d(s).toordinal() + _d(e).toordinal()) // 2)
    return f"{mid.year}T{(mid.month - 1) // 3 + 1}"


def _par_trimestre_civil(q):
    out = {}
    for (s, e), v in q.items():
        cq = trimestre_civil(s, e)
        if int(cq[:4]) < PREMIERE_ANNEE:
            continue
        if cq not in out or e > out[cq]["fin"]:
            out[cq] = {"fin": e, "md": round(v / 1e9, 3)}
    return dict(sorted(out.items()))


def _trimestre_suivant(cq):
    a, t = int(cq[:4]), int(cq[-1])
    return f"{a + 1}T1" if t == 4 else f"{a}T{t + 1}"


def _derniers_4(qs, fin):
    """Les 4 trimestres consécutifs se terminant au trimestre civil `fin`."""
    a, t = int(fin[:4]), int(fin[-1])
    ks = []
    for _ in range(4):
        ks.append(f"{a}T{t}")
        t -= 1
        if t == 0:
            a, t = a - 1, 4
    if all(k in qs for k in ks):
        return sum(qs[k]["md"] for k in ks)
    return None


FIN_TRIM_FR = {1: "fin mars", 2: "fin juin", 3: "fin septembre", 4: "fin décembre"}


def fetch_sec():
    capex, ca = {}, {}
    for tk, cik in SEC_CIK.items():
        req = Request(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
                      headers={"User-Agent": SEC_UA, "Accept": "application/json"})
        with urlopen(req, timeout=90) as r:
            g = json.load(r).get("facts", {}).get("us-gaap", {})
        time.sleep(0.3)  # la SEC demande ≤ 10 requêtes/s
        f_cap, f_ca = [], []
        for c in CONCEPTS_CAPEX:
            f_cap += g.get(c, {}).get("units", {}).get("USD", [])
        for c in CONCEPTS_CA:
            f_ca += g.get(c, {}).get("units", {}).get("USD", [])
        capex[tk] = _par_trimestre_civil(sec_trimestres(f_cap))
        ca[tk] = _par_trimestre_civil(sec_trimestres(f_ca))
    if any(len(capex[tk]) < 20 for tk in HYPERSCALERS):
        raise RuntimeError("SEC : historique d'investissements incomplet")
    # Dernier trimestre civil publié par LES QUATRE géants du cloud
    fin = min(list(capex[tk])[-1] for tk in HYPERSCALERS)
    annees = []
    for a in range(PREMIERE_ANNEE, int(fin[:4]) + 1):
        ligne, complet = {"annee": a}, True
        for tk in HYPERSCALERS:
            v = _derniers_4(capex[tk], f"{a}T4")
            if v is None:
                complet = False
                break
            ligne[tk] = round(v, 1)
        if complet:  # jamais d'année partielle présentée comme une année pleine
            ligne["total"] = round(sum(ligne[tk] for tk in HYPERSCALERS), 1)
            annees.append(ligne)
    glissant = {"fin": fin, "libelle": f"12 mois à {FIN_TRIM_FR[int(fin[-1])]} {fin[:4]}"}
    for tk in HYPERSCALERS:
        glissant[tk] = round(_derniers_4(capex[tk], fin), 1)
    glissant["total"] = round(sum(glissant[tk] for tk in HYPERSCALERS), 1)
    # Une ligne par société pour le tableau : 12 derniers mois publiés par CHACUNE
    societes = {}
    for tk in MAG7 + ["ORCL"]:
        if not capex[tk] or not ca[tk]:
            continue
        f_tk = min(list(capex[tk])[-1], list(ca[tk])[-1])
        c12, r12 = _derniers_4(capex[tk], f_tk), _derniers_4(ca[tk], f_tk)
        if c12 is None or r12 is None:
            continue
        societes[tk] = {"nom": SEC_NOMS[tk], "fin": f_tk,
                        "fin_date": capex[tk][f_tk]["fin"],
                        "capex_12m": round(c12, 1), "ca_12m": round(r12, 1),
                        "part_reinvestie": round(c12 / r12 * 100, 1) if r12 else None}
    return {
        "hyperscalers": {"annees": annees, "glissant": glissant, "ordre": HYPERSCALERS,
                         "noms": {tk: SEC_NOMS[tk] for tk in HYPERSCALERS}},
        "societes": societes,
        "trimestres_capex": capex,
        "definition": ("Investissements = « achats d'immobilisations corporelles » du tableau de "
                       "flux de trésorerie (cash décaissé, hors contrats de location-financement), "
                       "reconstitués trimestre par trimestre depuis les 10-Q et 10-K, rangés en "
                       "trimestres civils."),
        "source_url": "https://www.sec.gov/edgar/search/",
        "api": "https://data.sec.gov/api/xbrl/companyfacts/",
    }


# ════════════════════════════════════════════════════════════════════════════
# 2. Epoch AI — coût d'entraînement des grands modèles
# ════════════════════════════════════════════════════════════════════════════

EPOCH_CSV = "https://epoch.ai/data/notable_ai_models.csv"
EPOCH_COL = "Training compute cost (2023 USD)"
# Comparaisons « à performances voisines » citées dans le texte
EPOCH_PAIRES = [("DeepSeek-V3", "Llama 3.1-405B"), ("DeepSeek-V3", "GPT-4 (Mar 2023)")]


def fetch_epoch():
    txt = http_get_text(EPOCH_CSV, timeout=90)
    rows = list(csv.DictReader(io.StringIO(txt)))
    out = []
    for r in rows:
        try:
            c = float(r.get(EPOCH_COL) or "nan")
        except ValueError:
            continue
        if not math.isfinite(c) or c <= 0:
            continue
        d = (r.get("Publication date") or "")[:10]
        if d < "2023-01-01":
            continue
        pays = r.get("Country (of organization)") or ""
        out.append({
            "modele": r.get("Model", "").strip(),
            "organisation": (r.get("Organization") or "").split(",")[0].strip(),
            "date": d,
            "pays": "Chine" if "China" in pays else ("États-Unis" if "United States" in pays else pays.split(",")[0]),
            "cout_musd": round(c / 1e6, 2),
            "confiance": r.get("Confidence") or "",
        })
    out.sort(key=lambda x: x["date"])
    if len(out) < 5:
        raise RuntimeError("Epoch : trop peu de modèles chiffrés")
    return {"modeles": out, "colonne": EPOCH_COL,
            "definition": ("Estimation par Epoch AI du coût du calcul de l'entraînement final "
                           "(matériel amorti + énergie), en dollars 2023 : même méthode pour "
                           "tous les modèles, donc comparable."),
            "source_url": "https://epoch.ai/data/ai-models", "csv": EPOCH_CSV}


# ════════════════════════════════════════════════════════════════════════════
# 3-4. Marchés et économie réelle
# ════════════════════════════════════════════════════════════════════════════

def fetch_yahoo_daily(symbol, period1=1577836800, label=None):
    sym_enc = symbol.replace("=", "%3D").replace("^", "%5E")
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_enc}"
           f"?period1={period1}&period2={int(time.time())}&interval=1d")
    d = json.loads(http_get_text(url, timeout=25, accept="application/json"))
    r = d.get("chart", {}).get("result", [{}])[0]
    ts = r.get("timestamp", []) or []
    close = r.get("indicators", {}).get("quote", [{}])[0].get("close", []) or []
    dates, values = [], []
    for t, c in zip(ts, close):
        if c is None:
            continue
        dates.append(datetime.fromtimestamp(t).strftime("%Y-%m-%d"))
        values.append(round(float(c), 4))
    if len(dates) < 100:
        raise RuntimeError(f"Yahoo {symbol}: {len(dates)} points")
    return {"dates": dates, "values": values, "symbol": symbol, "label": label or symbol}


def fetch_sp500_vs_eqw():
    return {"GSPC": fetch_yahoo_daily("^GSPC", label="S&P 500 pondéré par la capitalisation"),
            "RSP": fetch_yahoo_daily("RSP", label="S&P 500 équipondéré (ETF RSP)")}


def _fred(sid, start):
    def f():
        return fetch_fred_csv(sid, start=start)
    return f


# ════════════════════════════════════════════════════════════════════════════
# 5. AIE — électricité des data centers (observatoire « Energy and AI »)
# ════════════════════════════════════════════════════════════════════════════
# Série annuelle 2005 → dernière estimation, publiée sans compte dans le script
# public de l'observatoire (pas de bouton de téléchargement) : somme des quatre
# postes serveurs + autres équipements informatiques + refroidissement + autres
# infrastructures, en TWh. Les projections (2030, 2035) viennent du rapport et
# sont recopiées dans AIE_PROJECTIONS.
AIE_OBS_JS = "https://iea.blob.core.windows.net/scripts/ai-observatory/ai-observatory.js"
AIE_POSTES = ("Servers", "Other IT equipment", "Cooling", "Other infrastructure")
AIE_PROJECTIONS = {
    "edition": "AIE, « Key Questions on Energy and AI », 16 avril 2026",
    "url": "https://www.iea.org/reports/key-questions-on-energy-and-ai",
    "scenario": "scénario de référence",
    "points": [[2030, 945], [2035, 1193]],
    "note": ("Le texte du rapport arrondit 2030 à ~950 TWh ; l'annexe donne 945. "
             "Autres scénarios 2030 : 833 (« Headwinds ») à 1 008 TWh (« Lift-Off »)."),
}


def fetch_aie():
    import re
    txt = http_get_text(AIE_OBS_JS, timeout=90, accept="application/javascript,*/*")
    objs = re.findall(r'\{\s*year:\s*"?(\d{4})(e?)"?,\s*type:\s*"([^"]+)",\s*value:\s*([-\d.eE]+)\s*\}', txt)
    tot, estim = {}, set()
    for an, e, typ, val in objs:
        if typ in AIE_POSTES:
            tot.setdefault(int(an), {})[typ] = float(val)
            if e:
                estim.add(int(an))
    annees = sorted(a for a, d in tot.items() if len(d) == len(AIE_POSTES))
    if len(annees) < 10:
        raise RuntimeError(f"AIE : {len(annees)} années complètes")
    obs = [[a, round(sum(tot[a].values()), 1)] for a in annees if a not in estim]
    est = [[a, round(sum(tot[a].values()), 1)] for a in annees if a in estim]
    if not (300 < obs[-1][1] < 3000):
        raise RuntimeError(f"AIE : valeur hors bornes {obs[-1]}")
    return {"observe": [p for p in obs if p[0] >= 2015], "estimation": est,
            "projection": AIE_PROJECTIONS["points"], "edition": AIE_PROJECTIONS["edition"],
            "edition_url": AIE_PROJECTIONS["url"], "scenario": AIE_PROJECTIONS["scenario"],
            "note": AIE_PROJECTIONS["note"],
            "source_url": "https://www.iea.org/data-and-statistics/data-tools/energy-and-ai-observatory",
            "api": AIE_OBS_JS}


# ════════════════════════════════════════════════════════════════════════════
# REGISTRES recopiés de sources publiées (vérifiés le 04/10/2026)
# ════════════════════════════════════════════════════════════════════════════

OPENAI_ENGAGEMENTS = {
    "etat_au": "2026-10-04",
    "contrats": [
        {"partenaire": "Oracle (Stargate)", "objet": "capacité de calcul sur 5 ans", "md": 300,
         "date_fr": "septembre 2025", "statut": "officiel",
         "detail": "« exceeds $300 billion between the two companies over the next five years »",
         "url": "https://openai.com/index/five-new-stargate-sites/"},
        {"partenaire": "Microsoft (Azure)", "objet": "services cloud supplémentaires", "md": 250,
         "date_fr": "octobre 2025", "statut": "officiel",
         "url": "https://blogs.microsoft.com/blog/2025/10/28/the-next-chapter-of-the-microsoft-openai-partnership/"},
        {"partenaire": "Amazon (AWS)", "objet": "cloud et puces Trainium, 38 + 100 Md$", "md": 138,
         "date_fr": "novembre 2025 et février 2026", "statut": "officiel",
         "url": "https://openai.com/index/amazon-partnership/"},
        {"partenaire": "CoreWeave", "objet": "cloud spécialisé IA (11,9 + 4 + 6,5 Md$)", "md": 22.4,
         "date_fr": "mars à septembre 2025", "statut": "officiel",
         "url": "https://www.coreweave.com/news/coreweave-expands-agreement-with-openai-by-up-to-6-5b"},
        {"partenaire": "Cerebras", "objet": "750 MW de calcul d'inférence", "md": 20,
         "date_fr": "avril 2026", "statut": "officiel",
         "detail": "« more than $20 billion » (prospectus d'introduction en bourse de Cerebras)",
         "url": "https://www.sec.gov/Archives/edgar/data/2021728/000162828026025762/cerebras-sx1april2026.htm"},
    ],
    "sans_montant": [
        {"partenaire": "AMD", "objet": "6 GW de puces, bons de souscription jusqu'à 160 M d'actions",
         "detail": "« tens of billions of dollars in revenue for AMD »",
         "url": "https://www.sec.gov/Archives/edgar/data/2488/000119312525230895/d28189dex991.htm"},
        {"partenaire": "Broadcom", "objet": "10 GW de puces sur mesure", "detail": "montant non publié",
         "url": "https://openai.com/index/openai-and-broadcom-announce-strategic-collaboration/"},
        {"partenaire": "Nvidia", "objet": "lettre d'intention de 10 GW et « jusqu'à 100 Md$ » jamais signée ; "
         "remplacée par 30 Md$ investis dans OpenAI (février 2026) et une garantie de baux jusqu'à 105 Md$ "
         "pour le campus PORTS-Pike (août 2026)",
         "url": "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000069/nvda-20260817.htm"},
    ],
    "total_annonce": {"md": 1400, "gw": 30, "date_fr": "novembre 2025",
                      "qui": "Sam Altman", "detail": "engagements sur 8 ans",
                      "url": "https://techcrunch.com/2025/11/06/sam-altman-says-openai-has-20b-arr-and-about-1-4-trillion-in-data-center-commitments/"},
}

OPENAI_FINANCES = {
    "titre": None,  # calculé côté page
    "unite": "Md$ de trésorerie (négatif = argent brûlé)",
    "points": [
        {"annee": 2025, "md": -8, "type": "réalisé"},
        {"annee": 2026, "md": -25, "type": "projeté"},
        {"annee": 2027, "md": -57, "type": "projeté"},
        {"annee": 2028, "md": -85, "type": "projeté"},
        {"annee": 2029, "md": -51, "type": "projeté"},
        {"annee": 2030, "md": 39, "type": "projeté"},
    ],
    "source": "The Information, février 2026 (repris par The Decoder) ; 2025 : comptes audités ayant fuité, recoupés par le FT",
    "url": "https://the-decoder.com/openai-adds-111-billion-to-its-cash-burn-forecast-as-ai-costs-spiral-beyond-projections/",
    "derniere_estimation": {"md": 278, "periode": "2026-2030", "date_fr": "septembre 2026",
                            "source": "Financial Times",
                            "url": "https://www.thestar.com.my/tech/tech-news/2026/09/19/openai-forecasts-cash-burn-near-280-billion-by-2030-ft-reports"},
    "depense_calcul_2030": {"md": 665, "source": "The Information, février 2026"},
    "comptes_2025": {"revenus": 13.07, "perte_exploitation": 20.9, "perte_nette": 38.5,
                     "dont_reevaluations": 41.6,
                     "url": "https://finance.yahoo.com/markets/stocks/articles/openai-2025-financials-leaked-38-121508294.html"},
    "valorisation": {"md": 852, "date_fr": "31 mars 2026", "url": "https://openai.com/index/accelerating-the-next-phase-ai/"},
}

REVENUS_IA = {
    "depenses_entreprises": {
        "libelle": "Dépenses des entreprises en IA générative (Menlo Ventures)",
        "points": [[2023, 1.7], [2024, 11.5], [2025, 37]],
        "url": "https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/",
        "note": "Rapport 2025 de Menlo Ventures, qui révise ses estimations 2023 et 2024.",
    },
    "lignes": [
        {"quoi": "Dépenses des entreprises en IA générative", "valeur": "37 Md$", "periode": "2025",
         "source": "Menlo Ventures", "url": "https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/"},
        {"quoi": "Chiffre d'affaires d'OpenAI", "valeur": "3,7 Md$ puis 13,1 Md$", "periode": "2024 puis 2025",
         "source": "comptes ayant fuité, recoupés par le FT", "url": "https://www.cnbc.com/2026/03/31/openai-funding-round-ipo.html"},
        {"quoi": "Rythme annualisé d'OpenAI", "valeur": "≈ 70 Md$", "periode": "fin septembre 2026",
         "source": "Axios (presse)", "url": "https://www.axios.com/2026/09/29/scoop-openais-annual-recurring-revenue-nears-70b"},
        {"quoi": "Rythme annualisé d'Anthropic", "valeur": "> 47 Md$ (officiel) ; > 100 Md$ (presse)", "periode": "mai 2026 ; mi-septembre 2026",
         "source": "Anthropic ; New York Times", "url": "https://www.anthropic.com/news/series-h"},
        {"quoi": "Dépenses mondiales en IA générative, matériel compris", "valeur": "644 Md$ (80 % de matériel)", "periode": "2025 (prévision)",
         "source": "Gartner", "url": "https://www.gartner.com/en/newsroom/press-releases/2025-03-31-gartner-forecasts-worldwide-genai-spending-to-reach-644-billion-in-2025"},
    ],
}

HUMANOIDES = [
    # (modèle, pays, prix $ ou None, année, statut, source, url, pays_fr)
    ("Unitree R1 Air", "CN", 4900, 2026, "prix public", "Unitree", "https://www.unitree.com/R1", "Chine"),
    ("Unitree R1", "CN", 5900, 2025, "prix public", "Unitree", "https://www.unitree.com/R1", "Chine"),
    ("Unitree G1", "CN", 13500, 2026, "prix public", "Unitree", "https://www.unitree.com/g1", "Chine"),
    ("1X NEO", "US", 20000, 2025, "prix public", "1X Technologies", "https://www.1x.tech/order", "États-Unis / Norvège"),
    ("Unitree H2", "CN", 29900, 2025, "prix public", "Unitree", "https://www.unitree.com/H2", "Chine"),
    ("Unitree H1", "CN", None, 2023, "prix indicatif (90 000 $), « nous contacter »", "Unitree", "https://www.unitree.com/h1", "Chine"),
    ("AgiBot Lingxi X2", "CN", None, 2026, "prix public en yuans : 155 000 ¥", "AgiBot", "https://store.agibot.com.cn/", "Chine"),
    ("Tesla Optimus", "US", None, 2026, "pas en vente ; objectif « 20 000 à 30 000 $ à long terme » (E. Musk)", "Tesla", "https://www.tesla.com/optimus", "États-Unis"),
    ("Figure 03", "US", None, 2025, "pas de prix public", "Figure AI", "https://www.figure.ai/", "États-Unis"),
    ("Boston Dynamics Atlas", "US", None, 2025, "pas de prix public (« Contact Sales »)", "Boston Dynamics", "https://bostondynamics.com/atlas/", "États-Unis"),
    ("Apptronik Apollo", "US", None, 2025, "pas de prix public", "Apptronik", "https://apptronik.com/", "États-Unis"),
    ("UBTech Walker S2", "CN", None, 2025, "pas de prix public (contrats industriels)", "UBTech", "https://www.ubtrobot.com/", "Chine"),
]

# Scores publiés par les laboratoires (génération 2024 - début 2025). None = non publié
# ou incertain. Coût : Epoch AI quand il existe (jointure sur EPOCH_NOMS).
LLM_BENCHMARKS = [
    ("DeepSeek-V3", 88.5, 82.6, 90.2, "CN"),
    ("DeepSeek-R1", 90.8, None, 97.3, "CN"),
    ("Qwen 2.5 72B", 86.1, None, 83.1, "CN"),
    ("Llama 3.1 405B", 88.6, 89.0, 73.8, "US"),
    ("Claude 3.5 Sonnet", 88.7, 92.0, 78.3, "US"),
    ("GPT-4o", 88.7, 90.2, 76.6, "US"),
    ("Gemini 1.5 Pro", 85.9, 84.1, None, "US"),
]
EPOCH_NOMS = {"DeepSeek-V3": "DeepSeek-V3", "DeepSeek-R1": "DeepSeek-R1",
              "Llama 3.1 405B": "Llama 3.1-405B", "Claude 3.5 Sonnet": "Claude 3.5 Sonnet"}

REGISTRES = {
    "openai_engagements": OPENAI_ENGAGEMENTS,
    "openai_finances": OPENAI_FINANCES,
    "revenus_ia": REVENUS_IA,
    "humanoid_robots": [
        {"label": m, "country": c, "price_usd": p, "year": a, "statut_prix": st,
         "source": so, "url": u, "pays_fr": pf}
        for m, c, p, a, st, so, u, pf in HUMANOIDES],
}


# ════════════════════════════════════════════════════════════════════════════
# ASSEMBLAGE
# ════════════════════════════════════════════════════════════════════════════

LIVE = [
    ("sec", fetch_sec),
    ("epoch", fetch_epoch),
    ("aie_datacenters", fetch_aie),
    ("sp500_vs_eqw", fetch_sp500_vs_eqw),
    ("freight_index", _fred("RAILFRTINTERMODAL", "2015-01-01")),
    ("pce_goods", _fred("DGDSRX1", "2015-01-01")),
    ("cre_delinquency", _fred("DRCRELEXFACBS", "2010-01-01")),
    ("busloans", _fred("BUSLOANS", "2015-01-01")),
    ("indpro", _fred("INDPRO", "1919-01-01")),
]


def charger_precedent():
    try:
        return json.loads(OUT_JSON.read_text())
    except Exception:
        return {}


def formes_historiques(payload):
    """Clés lues par d'autres consommateurs (DESK) : on garde leur forme, avec des
    valeurs désormais réelles."""
    sec = payload.get("sec") or {}
    tq = sec.get("trimestres_capex") or {}
    cap = {}
    for tk in MAG7:
        qs = tq.get(tk) or {}
        ans = []
        for a in range(PREMIERE_ANNEE, date.today().year + 1):
            v = _derniers_4(qs, f"{a}T4") if qs else None
            if v is not None:
                ans.append([a, round(v, 1)])
        if ans:
            cap[tk] = ans
    payload["capex_mag7"] = cap or None
    soc = sec.get("societes") or {}
    payload["snapshot_mag7"] = {tk: {"name": s["nom"], "revenue_b": s["ca_12m"],
                                     "capex_b": s["capex_12m"], "periode": s["fin"]}
                                for tk, s in soc.items() if tk in MAG7} or None
    aie = payload.get("aie_datacenters") or {}
    if aie.get("observe"):
        payload["iea_datacenters"] = (
            [{"year": a, "twh": v, "type": "observé"} for a, v in aie["observe"]]
            + [{"year": a, "twh": v, "type": "estimation"} for a, v in aie.get("estimation", [])]
            + [{"year": a, "twh": v, "type": "projection"} for a, v in aie.get("projection", [])])
    ep = (payload.get("epoch") or {}).get("modeles") or []
    par_nom = {m["modele"]: m for m in ep}
    payload["llm_benchmarks"] = [
        {"model": m, "mmlu": a, "humaneval": b, "math": c, "country": pays,
         "cout_epoch_musd": (par_nom.get(EPOCH_NOMS.get(m, "")) or {}).get("cout_musd")}
        for m, a, b, c, pays in LLM_BENCHMARKS]
    payload["ai_training_costs"] = [
        {"label": m["modele"], "cost_million_usd": m["cout_musd"],
         "year": int(m["date"][:4]), "source": "Epoch AI (estimation, dollars 2023)"}
        for m in ep] or None


def scrub_non_finis(o):
    if isinstance(o, dict):
        return {k: scrub_non_finis(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [scrub_non_finis(v) for v in o]
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


def build_payload():
    prev = charger_precedent()
    prev_src = ((prev.get("meta") or {}).get("sources") or {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload, sources, ok, failed = {}, {}, [], []
    for cle, fn in LIVE:
        err = None
        try:
            val = fn()
        except Exception as e:  # noqa: BLE001
            val, err = None, f"{type(e).__name__}: {e}"
            sys.stderr.write(f"[{cle}] {err}\n")
        if val:
            payload[cle] = val
            sources[cle] = {"ok": True, "dernier_succes": now}
            ok.append(cle)
        else:
            payload[cle] = prev.get(cle)
            old = prev_src.get(cle) or {}
            sources[cle] = {"ok": False, "dernier_succes": old.get("dernier_succes"),
                            "erreur": (err or "aucune donnée")[:300]}
            failed.append(cle)
    formes_historiques(payload)
    for k, v in REGISTRES.items():
        payload[k] = v
    payload["meta"] = {"updated_at": now, "updated_at_unix": int(time.time()),
                       "sources_ok": ok, "sources_failed": failed, "sources": sources,
                       "doc_version": DOC_VERSION}
    return payload, len(ok), len(failed)


def write_outputs(payload):
    payload = scrub_non_finis(payload)
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    tmp = OUT_JSON.with_suffix(".json.tmp")
    tmp.write_text(body)
    os.replace(tmp, OUT_JSON)
    js = (f"/* these_bulleia_cache.js — generated {payload['meta']['updated_at']} */\n"
          f"window.__THESE_BULLEIA__ = {body};\n")
    tmpj = OUT_JS.with_suffix(".js.tmp")
    tmpj.write_text(js)
    os.replace(tmpj, OUT_JS)
    site_dir = Path.home() / "Desktop" / "Site_Crypto_Finance"
    if site_dir.exists():
        for name in ("these_bulleia_cache.json", "these_bulleia_cache.js"):
            link = site_dir / name
            target = CACHE_DIR / name
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(target)
            except OSError as e:
                sys.stderr.write(f"[SYMLINK {name}] {e}, copie à la place\n")
                shutil.copy2(target, link)


def main():
    t0 = time.time()
    try:
        payload, n_ok, n_fail = build_payload()
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[FATAL] {e}\n")
        sys.exit(2)
    if n_ok < 3 and OUT_JSON.exists():
        sys.stderr.write(f"[GARDE] {n_ok} OK / {n_fail} en échec — cache précédent conservé\n")
        sys.exit(1)
    write_outputs(payload)
    sys.stdout.write(f"[these_bulleia] OK · {n_ok} sources en direct, {n_fail} en repli · "
                     f"{time.time() - t0:.1f}s · cache → {OUT_JSON}\n")


if __name__ == "__main__":
    main()
