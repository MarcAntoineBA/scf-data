#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_tech_debt.py — « Le coût de la dette des géants tech »
Onglet Indicateur (categorie Macro), section `tech-debt-section`.

QUESTION POSEE : ces societes empruntent massivement pour financer l'IA. Combien
leur coute cet argent, quelle part de leur exploitation il absorbe, et que se
passe-t-il si ce cout augmente ?

SOURCE UNIQUE ET AUDITABLE : SEC EDGAR XBRL `companyfacts` (comptes deposes et
audites, 10-K / 10-Q). Aucune estimation, aucun consensus, aucun fournisseur
proprietaire. Chaque nombre affiche est retracable jusqu'a un depot SEC.
Cote marche : FRED (courbe corporate haute qualite, Moody's Aaa/Baa, ICE BofA).

Sortie : tech_debt_cache.js  ->  window.__TECH_DEBT__   (+ .json)

──────────────────────────────────────────────────────────────────────────────
LES QUATRE PIEGES XBRL, ET COMMENT ILS SONT TRAITES
──────────────────────────────────────────────────────────────────────────────
1. `InterestExpense` a ete DEPRECIE par la FASB (taxonomie 2024) au profit de
   `InterestExpenseNonoperating`. Microsoft, Alphabet, Amazon, Nvidia, Tesla…
   ont bascule courant 2024-2025. Concatener naivement les deux donne soit une
   serie qui s'arrete en 2024, soit un double comptage sur l'annee de bascule.
   -> Echelle de priorite PAR PERIODE : pour un intervalle donne on retient UN
      SEUL tag, le plus complet disponible. Jamais de somme entre tags.

2. Les faits XBRL sont publies en CUMUL D'EXERCICE (3, 6, 9 et 12 mois melanges).
   Prendre les faits « 3 mois » suffit pour Oracle, pas pour Alphabet qui publie
   surtout du cumul. -> Moteur de DECUMULATION : Q_n = cumul_n - cumul_(n-1),
   applique par passes successives jusqu'a epuisement.

3. Les LOCATIONS-FINANCEMENT sont de la dette (datacenters). Microsoft paie
   2,5 Md$/an d'interets dessus — plus que sur ses obligations — et ces interets
   SONT dans la charge d'interets publiee. Les exclure du denominateur donnait un
   taux moyen absurde de 6,96 % pour Microsoft. -> `FinanceLeaseLiability` (ou
   Current + Noncurrent) integre a la dette, et signale a l'ecran.

4. Une partie des interets n'apparait PAS au compte de resultat : elle est
   INCORPOREE AU COUT DES IMMOBILISATIONS en construction (datacenters). C'est
   du cout de la dette bien reel, simplement differe. -> `InterestCostsCapitalized`
   collecte quand il est tague ; pour Apple, deduit par difference
   (`InterestCostsIncurred` - charge publiee).

AUTRES POINTS DE RIGUEUR
- EBIT : `OperatingIncomeLoss` quand il existe. IBM ne le publie pas -> repli sur
  (resultat avant impots + charge d'interets), signale par un drapeau `ebit_proxy`.
- Exercices decales (Microsoft juin, Apple septembre, Oracle mai, Nvidia janvier,
  Broadcom novembre) : chaque trimestre fiscal est rattache au TRIMESTRE CALENDAIRE
  qui contient sa date de cloture. L'agregat somme, a chaque date, le dernier
  glissant 12 mois connu de chaque societe (evite toute saisonnalite).
- Tous les ratios sont calcules sur 12 MOIS GLISSANTS, jamais sur un trimestre
  isole, et uniquement quand les 4 trimestres sont reellement disponibles.
- Aucune notation d'agence n'est affirmee : le site ne dispose d'aucune source
  auditable pour les notations. La comparaison se fait contre les COURBES DE
  MARCHE observables (haute qualite / Baa / high yield).
"""
import json
import os
import re
import shutil
import sys
import time
import gzip
import datetime as dt
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _fred_helpers import fetch_fred
except ImportError:                                   # execution hors dossier site
    sys.path.insert(0, os.path.expanduser("~/Desktop/Site_Crypto_Finance"))
    from _fred_helpers import fetch_fred
import bonds                                          # echeancier, tranches, emissions 424B

UA = os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research")
CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
REPO_DIR = Path(os.path.expanduser("~/Desktop/Site_Crypto_Finance"))
HERE = Path(__file__).resolve().parent
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RAW = CACHE_DIR / "tech_debt_raw"
RAW.mkdir(exist_ok=True)
CACHE_MAX_HOURS = 12
RAW_MAX_HOURS = 20          # companyfacts : gros fichiers, deposes au plus 1x/trimestre

OUT_NAME = "tech_debt_cache"

# ─────────────────────────────────────────────────────────────────────────────
# UNIVERS — 12 societes. Choix utilisateur : les 7 du coeur (GAFAM + Nvidia +
# Oracle) + les satellites de l'infrastructure IA (Broadcom, CoreWeave) + la tech
# endettee sans le moteur IA (IBM, Intel) + Tesla.
# `grp` sert au filtrage a l'ecran. `color` = palette du site.
# ─────────────────────────────────────────────────────────────────────────────
COMPANIES = [
    {"t": "MSFT",  "cik": 789019,  "name": "Microsoft",  "grp": "core",  "color": "#4a9eff", "fy": "juin"},
    {"t": "GOOGL", "cik": 1652044, "name": "Alphabet",   "grp": "core",  "color": "#f5c451", "fy": "déc."},
    {"t": "AMZN",  "cik": 1018724, "name": "Amazon",     "grp": "core",  "color": "#ff9f43", "fy": "déc."},
    {"t": "META",  "cik": 1326801, "name": "Meta",       "grp": "core",  "color": "#7ecff4", "fy": "déc."},
    {"t": "AAPL",  "cik": 320193,  "name": "Apple",      "grp": "core",  "color": "#dbe4f3", "fy": "sept."},
    {"t": "NVDA",  "cik": 1045810, "name": "Nvidia",     "grp": "core",  "color": "#26de81", "fy": "janv."},
    {"t": "ORCL",  "cik": 1341439, "name": "Oracle",     "grp": "core",  "color": "#ff6b5b", "fy": "mai"},
    {"t": "AVGO",  "cik": 1730168, "name": "Broadcom",   "grp": "infra", "color": "#c084fc", "fy": "nov."},
    {"t": "CRWV",  "cik": 1769628, "name": "CoreWeave",  "grp": "infra", "color": "#fb7185", "fy": "déc."},
    # SpaceX : introduite au Nasdaq (SPCX) en juin 2026 — prospectus 424B4 du 12/06,
    # premier 10-Q le 04/08/2026. Elle publie donc des comptes deposes, au meme titre
    # que les autres. Historique court par construction : les garde-fous du collecteur
    # le gerent seuls (aucun taux moyen tant que la fenetre de 12 mois ne contient pas
    # 4 trimestres de bilan) — ne rien assouplir pour elle.
    {"t": "SPCX",  "cik": 1181412, "name": "SpaceX",     "grp": "infra", "color": "#a3e635", "fy": "déc."},
    {"t": "TSLA",  "cik": 1318605, "name": "Tesla",      "grp": "other", "color": "#e879a6", "fy": "déc."},
    {"t": "IBM",   "cik": 51143,   "name": "IBM",        "grp": "other", "color": "#94a3b8", "fy": "déc."},
    {"t": "INTC",  "cik": 50863,   "name": "Intel",      "grp": "other", "color": "#38bdf8", "fy": "déc."},
]

# ─────────────────────────────────────────────────────────────────────────────
# ECHELLES DE TAGS — ordre = priorite decroissante. Pour une periode donnee on
# retient le PREMIER tag disponible, jamais la somme (cf piege n°1).
# ─────────────────────────────────────────────────────────────────────────────
T_INTEREST = ["InterestExpenseNonoperating", "InterestExpense",
              "InterestExpenseDebt", "InterestExpenseLongTermDebt",
              "InterestExpenseDebtExcludingAmortization"]
T_INT_CAP = ["InterestCostsCapitalized"]
T_INT_INCURRED = ["InterestCostsIncurred"]
T_INT_PAID = ["InterestPaidNet", "InterestPaid"]
T_EBIT = ["OperatingIncomeLoss"]
T_PRETAX = ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"]
T_REV = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
         "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"]
T_OCF = ["NetCashProvidedByUsedInOperatingActivities",
         "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
T_CAPEX = ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"]

# ─────────────────────────────────────────────────────────────────────────────
# PIEGE n°5 — LES TAGS DE DETTE NE SONT PAS UNIVERSELS, ET ILS SE CHEVAUCHENT.
# Trois erreurs distinctes ont ete observees et corrigees ici :
#   · Oracle ne publie NI `LongTermDebtNoncurrent` NI `LongTermDebtCurrent` (il
#     utilise `LongTermNotesAndLoans` / `NotesPayableCurrent`) -> sa dette tombait
#     a zero hors cloture annuelle et le taux moyen calcule atteignait 386 %.
#   · IBM publie `ShortTermBorrowings` (5,78 Md$) ET
#     `LongTermDebtAndCapitalLeaseObligationsCurrent` (5,77 Md$) : c'est LA MEME
#     dette. Les additionner gonflait sa dette de 7 Md$.
#   · Tesla ne publie plus que `LongTermDebt`, qui est un TOTAL incluant la part
#     courante -> y ajouter `DebtCurrent` comptait la part courante deux fois.
# D'ou trois familles disjointes, jamais melangees :
T_DEBT_LT = ["LongTermDebtNoncurrent", "LongTermNotesAndLoans", "LongTermNotesPayable"]
# LARGE = toute la dette a moins d'un an, papier commercial COMPRIS
T_DEBT_ST_BROAD = ["ShortTermBorrowings", "DebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"]
# ETROIT = seulement la part courante de la dette a terme, papier commercial EXCLU
T_DEBT_ST_NARROW = ["LongTermDebtCurrent", "NotesPayableCurrent", "NotesAndLoansPayableCurrent"]
T_DEBT_CP = ["CommercialPaper", "OtherShortTermBorrowings"]
# TOTAL = dette entiere en un seul poste (dernier recours, exclut tout le reste)
T_DEBT_TOT = ["DebtLongtermAndShorttermCombinedAmount", "LongTermDebt",
              "LongTermDebtAndCapitalLeaseObligations"]
T_LEASE_TOT = ["FinanceLeaseLiability"]
T_LEASE_CUR = ["FinanceLeaseLiabilityCurrent"]
T_LEASE_NC = ["FinanceLeaseLiabilityNoncurrent"]
# Repli decouvert par le garde-fou tresorerie : Intel a CESSE de taguer
# `CashAndCashEquivalentsAtCarryingValue` en 2024 (aucun fait cette annee-la) et
# publie `CashCashEquivalents...RestrictedCash...` a la place. Sans ce repli, sa
# tresorerie tombait a ZERO sur quatre trimestres. Le repli comprend la tresorerie
# soumise a restriction — legerement plus large, mais infiniment plus juste que zero.
T_CASH = ["CashAndCashEquivalentsAtCarryingValue",
          "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]
T_INV_CUR = ["ShortTermInvestments", "MarketableSecuritiesCurrent",
             "AvailableForSaleSecuritiesDebtSecuritiesCurrent"]
# PIEGE n°7 — « TRESORERIE » NE DOIT CONTENIR QUE DES TITRES NEGOCIABLES.
# `OtherLongTermInvestments` / `LongTermInvestments` designent les PARTICIPATIONS
# NON COTEES : chez Alphabet 131,5 Md$ de parts dans des societes privees, chez
# Microsoft sa participation dans OpenAI. Ce sont des investissements strategiques,
# pas des liquidites : on ne rembourse pas une obligation avec une part de societe
# non cotee. Les inclure portait la « tresorerie » d'Alphabet a 374 Md$ au lieu de
# ~242, et gonflait d'autant la position nette du groupe.
# Definition retenue : LIQUIDITES + TITRES NEGOCIABLES, rien d'autre.
T_INV_NC = ["MarketableSecuritiesNoncurrent", "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent"]


def log(*a):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)


def http_json(url, timeout=90, retry=3):
    for i in range(retry):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
            with urlopen(req, timeout=timeout) as r:
                data = r.read()
                if r.info().get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return json.loads(data)
        except Exception as e:
            if i == retry - 1:
                log("  GET fail", url[:80], repr(e)[:90])
            time.sleep(1.5 * (i + 1))
    return None


def companyfacts(cik):
    """companyfacts avec cache disque (fichiers lourds, mis a jour au plus 1x/trimestre)."""
    f = RAW / f"cf_{cik}.json.gz"
    if f.exists() and (time.time() - f.stat().st_mtime) / 3600 < RAW_MAX_HOURS:
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    d = http_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
    if d:
        try:
            with gzip.open(f, "wt", encoding="utf-8") as fh:
                json.dump(d, fh)
        except Exception:
            pass
    elif f.exists():                                    # repli : dernier cache valide
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────
def _usd(gaap, tag):
    """Faits USD d'un tag, restreints aux depots 10-K / 10-Q (exclut 8-K, S-1…)."""
    node = gaap.get(tag)
    if not node:
        return []
    return [x for x in node.get("units", {}).get("USD", [])
            if x.get("form") in ("10-K", "10-Q", "10-K/A", "10-Q/A") and x.get("val") is not None]


def instants(gaap, tags, with_tag=False):
    """Postes de BILAN : date -> valeur. Priorite de tag, puis depot le plus recent."""
    out = {}
    for prio, tag in enumerate(tags):
        for x in _usd(gaap, tag):
            if "start" in x:
                continue
            k = x["end"]
            cur = out.get(k)
            # on garde la meilleure priorite ; a priorite egale, le depot le plus recent
            if cur is None or prio < cur[1] or (prio == cur[1] and x["filed"] > cur[2]):
                out[k] = (float(x["val"]), prio, x["filed"], tag)
    if with_tag:
        return {k: (v[0], v[3]) for k, v in out.items()}
    return {k: v[0] for k, v in out.items()}


def durations(gaap, tags, cost=False):
    """Postes de FLUX : (start,end) -> valeur, avec priorite de tag.

    PIEGE n°6 — CONVENTION DE SIGNE INSTABLE, y compris chez un meme emetteur.
    Oracle a depose sa charge d'interets 2010 en NEGATIF (-754 M$) dans son 10-K
    de 2010, puis a re-depose la MEME periode en POSITIF (+754 M$) dans celui de
    2011. La regle « le depot le plus recent gagne » retient donc le positif pour
    l'exercice complet, mais les trimestres intermediaires, deposes une seule fois,
    restent negatifs. La decumulation soustrayait alors un cumul positif d'un cumul
    negatif et fabriquait un taux de 14,9 % pour Oracle en 2010.
    13 faits chez Oracle et 1 chez Tesla sont concernes ; les dix autres societes
    sont homogenes. `cost=True` normalise en valeur absolue : une charge d'interets
    est un cout par definition, son signe ne porte aucune information.
    """
    out = {}
    for prio, tag in enumerate(tags):
        for x in _usd(gaap, tag):
            if "start" not in x:
                continue
            k = (x["start"], x["end"])
            cur = out.get(k)
            if cur is None or prio < cur[1] or (prio == cur[1] and x["filed"] > cur[2]):
                v = float(x["val"])
                out[k] = (abs(v) if cost else v, prio, x["filed"])
    return {k: v[0] for k, v in out.items()}


def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def quarterize(per):
    """DECUMULATION (piege n°2).

    Entree : {(start,end): val} melangeant cumuls 3/6/9/12 mois.
    Sortie : {end_de_trimestre: val_du_trimestre_seul}.

    Methode : on part des periodes deja trimestrielles (75-105 j), puis par passes
    successives on soustrait d'un cumul la periode qui partage son debut, ce qui
    revele la periode complementaire. Repete jusqu'a ce que plus rien ne bouge.
    Un cumul de 12 mois d'ou l'on a retire 9 mois donne bien le 4e trimestre.
    """
    known = dict(per)                                     # (start,end) -> val
    quarters = {}
    for (s, e), v in known.items():
        if 75 <= _days(s, e) <= 105:
            quarters[e] = v
    for _ in range(6):                                    # convergence en 3-4 passes
        added = False
        by_start = {}
        for (s, e), v in known.items():
            by_start.setdefault(s, []).append((e, v))
        for (s, e), v in list(known.items()):
            n = _days(s, e)
            if n <= 105:
                continue
            for (e2, v2) in by_start.get(s, []):
                if e2 >= e:
                    continue
                rest = (e2, e)                            # complement : e2 -> e
                if rest in known:
                    continue
                gap = _days(e2, e)
                if gap < 20 or gap > 400:
                    continue
                known[rest] = v - v2
                added = True
                if 75 <= gap <= 105:
                    quarters.setdefault(e, v - v2)
        if not added:
            break
    return quarters


def annualize(per):
    """{(start,end):val} -> {end: val} pour les seules periodes de ~12 mois."""
    out = {}
    for (s, e), v in per.items():
        if 340 <= _days(s, e) <= 400:
            out[e] = v
    return out


def nearest(d, key, tol=45):
    """Valeur de bilan a la date `key`, en tolerant un decalage de quelques jours
    (les dates de cloture bougent d'un depot a l'autre : 52/53 semaines)."""
    if key in d:
        return d[key]
    best, bd = None, tol + 1
    for k, v in d.items():
        try:
            gap = abs(_days(min(k, key), max(k, key)))
        except ValueError:
            continue
        if gap < bd:
            best, bd = v, gap
    return best if bd <= tol else None


# ─────────────────────────────────────────────────────────────────────────────
# CONSTRUCTION PAR SOCIETE
# ─────────────────────────────────────────────────────────────────────────────
def build_company(c):
    d = companyfacts(c["cik"])
    if not d:
        log(f"  {c['t']}: companyfacts indisponible")
        return None
    gaap = d.get("facts", {}).get("us-gaap", {})
    if not gaap:
        return None

    ie_per = durations(gaap, T_INTEREST, cost=True)
    ie_q = quarterize(ie_per)
    ie_a = annualize(ie_per)

    ebit_per = durations(gaap, T_EBIT)
    ebit_q, ebit_a = quarterize(ebit_per), annualize(ebit_per)
    ebit_proxy = False
    if len(ebit_a) < 3:                                   # IBM : pas d'OperatingIncomeLoss
        pre_per = durations(gaap, T_PRETAX)
        pq, pa = quarterize(pre_per), annualize(pre_per)
        if len(pa) >= 3:
            ebit_proxy = True
            ebit_q = {k: v + ie_q.get(k, 0) for k, v in pq.items()}
            ebit_a = {k: v + ie_a.get(k, 0) for k, v in pa.items()}

    rev_per = durations(gaap, T_REV)
    rev_q, rev_a = quarterize(rev_per), annualize(rev_per)
    ocf_per = durations(gaap, T_OCF)
    ocf_q, ocf_a = quarterize(ocf_per), annualize(ocf_per)
    cap_per = durations(gaap, T_CAPEX)
    cap_q, cap_a = quarterize(cap_per), annualize(cap_per)

    # interets capitalises (piege n°4)
    icap_per = durations(gaap, T_INT_CAP, cost=True)
    icap_q, icap_a = quarterize(icap_per), annualize(icap_per)
    inc_per = durations(gaap, T_INT_INCURRED, cost=True)             # Apple : total encouru
    if inc_per:
        inc_q, inc_a = quarterize(inc_per), annualize(inc_per)
        for k, v in inc_q.items():
            if k not in icap_q and k in ie_q:
                icap_q[k] = max(0.0, v - ie_q[k])
        for k, v in inc_a.items():
            if k not in icap_a and k in ie_a:
                icap_a[k] = max(0.0, v - ie_a[k])
    ipaid_a = annualize(durations(gaap, T_INT_PAID, cost=True))

    # bilan
    b_lt = instants(gaap, T_DEBT_LT)
    b_broad = instants(gaap, T_DEBT_ST_BROAD)
    b_narrow = instants(gaap, T_DEBT_ST_NARROW)
    b_cp = instants(gaap, T_DEBT_CP)
    b_tot = instants(gaap, T_DEBT_TOT)
    l_tot = instants(gaap, T_LEASE_TOT)
    l_cur = instants(gaap, T_LEASE_CUR)
    l_nc = instants(gaap, T_LEASE_NC)
    cash = instants(gaap, T_CASH)
    inv_c = instants(gaap, T_INV_CUR)
    inv_n = instants(gaap, T_INV_NC)

    dates = sorted(set(list(ie_q) + list(ie_a) + list(b_lt) + list(b_broad) +
                       list(b_narrow) + list(b_tot) + list(cash)))
    # REPORT DES POSTES DE BILAN. Un poste absent d'un trimestre n'est pas nul : c'est
    # un stock, il persiste. Sans ce report, la dette d'Oracle tombait a zero entre
    # deux cloturesuelles et le taux moyen explosait a 386 %.
    prev = {}

    def stock(d, e, key, tol=45, maxage=400):
        v = nearest(d, e, tol)
        if v is not None:
            prev[key] = (e, v)
            return v
        p = prev.get(key)
        if p and 0 <= _days(p[0], e) <= maxage:
            return p[1]
        return None

    recs = []
    for e in dates:
        if e < "2009-01-01":
            continue
        # ── Dette financiere hors locations (piege n°5).
        # `ShortTermBorrowings` designe le papier commercial chez Microsoft mais la
        # TOTALITE de la dette courante chez IBM. Impossible de trancher par le nom :
        # on desambiguise par la VALEUR. Deux postes egaux a 2 % pres designent la
        # meme dette et ne sont comptes qu'une fois.
        # Les postes courts ne sont reportes que d'un trimestre (120 j) : une dette
        # a moins d'un an publiee il y a trois ans n'existe plus.
        lt = stock(b_lt, e, "lt")
        if lt is None:
            # la societe ne publie qu'un total (cas Tesla), part courante comprise
            tot = stock(b_tot, e, "tot")
            gross_bond = tot if tot is not None else 0
        else:
            narrow = stock(b_narrow, e, "narrow", maxage=120)
            st = stock(b_broad, e, "broad", maxage=120)
            # Le papier commercial n'est JAMAIS reporte : c'est un encours a moins de
            # 9 mois, souvent absent d'un trimestre parce qu'il vaut zero. Le reporter
            # le faisait compter en double avec la dette courte de Microsoft (25,7 au
            # lieu des 23,7 Md$ publies au 30/09/2014).
            cp = stock(b_cp, e, "cp", maxage=0)

            def same(a, b):
                return a is not None and b is not None and abs(a - b) <= 0.02 * max(abs(a), abs(b), 1)

            gross_bond = lt + (narrow or 0)
            if st is not None and not same(st, narrow):
                gross_bond += st                            # dette courte distincte
            if cp is not None and not same(cp, st) and not same(cp, narrow):
                gross_bond += cp                            # papier commercial distinct
        lease = stock(l_tot, e, "lease")
        if lease is None:
            lc, ln = stock(l_cur, e, "lc"), stock(l_nc, e, "ln")
            lease = (lc or 0) + (ln or 0) if (lc is not None or ln is not None) else None
        ca = stock(cash, e, "cash")
        iv = (stock(inv_c, e, "invc") or 0) + (stock(inv_n, e, "invn") or 0)
        if gross_bond <= 0 and not lease and ca is None:
            continue
        rec = {
            "d": e,
            "bond": round(gross_bond),
            "lease": round(lease) if lease else 0,
            "cash": round(ca) if ca is not None else None,
            "inv": round(iv) if iv else 0,
        }
        for name, qd, ad in (("ie", ie_q, ie_a), ("ebit", ebit_q, ebit_a), ("rev", rev_q, rev_a),
                             ("ocf", ocf_q, ocf_a), ("capex", cap_q, cap_a), ("icap", icap_q, icap_a)):
            if e in qd:
                rec[name] = round(qd[e])
            if e in ad:
                rec[name + "_a"] = round(ad[e])
        if e in ipaid_a:
            rec["ipaid_a"] = round(ipaid_a[e])
        recs.append(rec)

    recs.sort(key=lambda r: r["d"])
    if not recs:
        return None

    # ── 12 mois glissants. Deux regimes :
    #    - trimestriel disponible  -> somme des 4 derniers trimestres consecutifs
    #    - annuel seulement (Meta) -> valeur annuelle publiee, reportee jusqu'au
    #      prochain exercice (aucune interpolation inventee)
    def roll(field):
        out = {}
        vals = [(r["d"], r.get(field)) for r in recs]
        for i, (d0, _) in enumerate(vals):
            if i < 3:
                continue
            win = vals[i - 3:i + 1]
            if all(v is not None for _, v in win):
                span = _days(win[0][0], win[-1][0])
                if 240 <= span <= 300:                     # 4 trimestres consecutifs
                    out[d0] = sum(v for _, v in win)
        for r in recs:                                     # priorite a l'annuel publie
            if r.get(field + "_a") is not None:
                out[r["d"]] = r[field + "_a"]
        return out

    ttm = {f: roll(f) for f in ("ie", "ebit", "rev", "ocf", "capex", "icap")}

    # ── REPORT DU DERNIER 12 MOIS PUBLIE (piege n°2 bis)
    # Meta ne publie sa charge d'interets qu'en ANNUEL : ses trimestres 2026 n'ont
    # donc aucun glissant. Le dernier exercice publie reste la meilleure information
    # disponible -> on le reporte, mais JAMAIS au-dela de 15 mois, et on marque le
    # point comme reporte (`ieC`) pour que l'ecran puisse le dire.
    carried = set()
    for field in ("ie", "icap"):
        src = ttm[field]
        if not src:
            continue
        last_d = last_v = None
        for r in recs:
            e = r["d"]
            if e in src:
                last_d, last_v = e, src[e]
            elif last_d and _days(last_d, e) <= 460:
                src[e] = last_v
                if field == "ie":
                    carried.add(e)

    # Dette moyenne sur la MEME fenetre de 12 mois que les interets. Prendre la dette
    # de cloture sous-estimerait gravement le taux d'un emprunteur dont la dette double
    # dans l'annee (Oracle : 96 -> 137 Md$ sur l'exercice 2026 ; CoreWeave : x3).
    debt_at = {r["d"]: r["bond"] + r["lease"] for r in recs}
    dlist = [(r["d"], debt_at[r["d"]]) for r in recs]

    def avg_debt_ttm(e):
        """Dette moyenne des 12 derniers mois — ou rien.

        La fenetre doit contenir QUATRE trimestres couvrant au moins 300 jours.
        Au tout debut d'une serie elle n'en contient qu'un ou deux, et la moyenne
        porte alors sur une dette en pleine constitution : CoreWeave affichait
        21,8 % en decembre 2024 (dette moyennee sur 1,6 Md$ alors qu'elle en devait
        deja 8), Oracle 12,3 % sur son premier point. Ces valeurs sont des artefacts
        de demarrage, pas des taux.
        """
        # Une dette a ZERO signifie « non publiee », pas « inexistante » : CoreWeave
        # n'a deposse aucun bilan avant son introduction, ses quatre trimestres 2024
        # portent donc 0. Les moyenner faisait tomber sa base a 1,6 Md$ alors qu'elle
        # devait deja 8 Md$, et affichait un taux de 21,8 %. Ces points sont exclus.
        win = [v for d0, v in dlist if d0 <= e and 0 <= _days(d0, e) <= 400 and v > 0]
        span = [d0 for d0, v in dlist if d0 <= e and 0 <= _days(d0, e) <= 400 and v > 0]
        if len(win) < 4 or not span or _days(span[0], e) < 300:
            return None
        return sum(win) / len(win)

    ser = []
    for r in recs:
        e = r["d"]
        debt = r["bond"] + r["lease"]
        t_ie = ttm["ie"].get(e)
        t_ebit = ttm["ebit"].get(e)
        t_rev = ttm["rev"].get(e)
        t_ocf = ttm["ocf"].get(e)
        # taux moyen paye : interets 12 mois / dette moyenne sur la periode
        avg_debt = avg_debt_ttm(e)
        # PLANCHER DE BASE. Diviser une charge d'interets par quelques centaines de
        # millions de dette ne produit pas un taux mais du bruit : Amazon affichait
        # 26 % en 2010 (0,03 Md$ d'interets sur 0,13 Md$ de dette declaree) et Meta
        # 18,7 % en 2014 sur une dette qui arrondissait a zero. En dessous d'un
        # milliard de dette moyenne, aucun taux n'est publie.
        MIN_BASE = 1e9
        rate = (round(100 * t_ie / avg_debt, 3)
                if (t_ie and avg_debt and avg_debt >= MIN_BASE) else None)
        # GARDE-FOU ANTI-REGRESSION : un taux moyen hors de [0 %, 30 %] ne peut pas
        # etre reel pour un emetteur de cette taille — c'est le symptome d'un poste de
        # bilan manquant (cf piege n°5, Oracle a 386 %). On prefere ne rien afficher
        # plutot qu'afficher un nombre faux.
        if rate is not None and not (0 <= rate <= 30):
            rate = None
        cov = round(t_ebit / t_ie, 2) if (t_ie and t_ie > 0 and t_ebit is not None) else None
        sh_ebit = round(100 * t_ie / t_ebit, 3) if (t_ie and t_ebit and t_ebit > 0) else None
        costs = (t_rev - t_ebit) if (t_rev is not None and t_ebit is not None) else None
        sh_cost = round(100 * t_ie / costs, 3) if (t_ie and costs and costs > 0) else None
        sh_ocf = round(100 * t_ie / t_ocf, 3) if (t_ie and t_ocf and t_ocf > 0) else None
        net = (r["cash"] or 0) + r["inv"] - debt
        # `cash` reste le TOTAL (liquidites + placements) pour ne rien casser en
        # aval ; `liq` et `inv` en donnent la decomposition. « Tresorerie » est un
        # mot trompeur : chez Apple les liquidites au sens strict ne font qu'un
        # cinquieme du total, le reste est un portefeuille obligataire.
        o = {"d": e, "debt": debt, "bond": r["bond"], "lease": r["lease"],
             "cash": (r["cash"] or 0) + r["inv"], "liq": r["cash"] or 0,
             "inv": r["inv"], "net": round(net)}
        if t_ie is not None:
            o["ie"] = round(t_ie)
        if t_ebit is not None:
            o["ebit"] = round(t_ebit)
        if t_ocf is not None:
            o["ocf"] = round(t_ocf)
        if ttm["capex"].get(e) is not None:
            o["capex"] = round(ttm["capex"][e])
        if ttm["icap"].get(e) is not None:
            o["icap"] = round(ttm["icap"][e])
        if rate is not None:
            o["rate"] = rate
        if cov is not None:
            o["cov"] = cov
        if sh_ebit is not None:
            o["shEbit"] = sh_ebit
        if sh_cost is not None:
            o["shCost"] = sh_cost
        if sh_ocf is not None:
            o["shOcf"] = sh_ocf
        if e in carried:
            o["ieC"] = 1                                   # 12 mois reporté, non recalculé
        ser.append(o)

    ser = [s for s in ser if s["debt"] > 0 or s.get("ie")]
    if not ser:
        return None

    # Date du dernier chiffre d'intérêts REELLEMENT publié (hors report) : sert à
    # signaler à l'écran les sociétés qui ont cessé de publier (cas Apple, dernier
    # exercice 2023) plutôt qu'à laisser croire à une donnée à jour.
    ie_real = [s["d"] for s in ser if s.get("ie") is not None and not s.get("ieC")]
    ie_last = ie_real[-1] if ie_real else None
    stale = bool(ie_last) and _days(ie_last, ser[-1]["d"]) > 400
    # HISTORIQUE TROP COURT (cas d'une introduction en bourse recente) : aucun
    # glissant 12 mois n'existe encore. SpaceX, cotee en juin 2026, ne publie que
    # des semestres — annualiser reviendrait a inventer une donnee. On signale
    # l'absence plutot que de laisser des tirets muets dans le tableau.
    young = not ie_real and len(ser) < 6

    audit = self_audit(ser, ie_a, instants(gaap, ["DebtLongtermAndShorttermCombinedAmount"]), c["t"])

    # ── COUCHES AJOUTEES (precision + fraicheur)
    # 1. MUR DE MATURITES, depuis les faits XBRL structures (aucun parsing).
    #    Montants NOMINAUX (par) : ils different de la valeur comptable au bilan,
    #    qui est nette des decotes et frais d'emission non amortis.
    lad = bonds.ladder(gaap)
    # 2. DETAIL PAR TRANCHE, parse dans la note de dette du 10-K puis VALIDE contre
    #    le bilan. Rejete en bloc s'il ne retombe pas dessus -> absent pour la
    #    plupart des emetteurs, qui mettent tous leur note en page differemment.
    last_bond = ser[-1]["bond"] if ser else None
    try:
        trn = bonds.tranches(c["cik"], xbrl_debt=last_bond)
    except Exception as e:
        log(f"    tranches {c['t']} : {repr(e)[:60]}")
        trn = None
    # 3. EMISSIONS RECENTES : prospectus 424B, deposes quelques jours apres
    #    l'emission -> comble le trou entre deux publications trimestrielles.
    try:
        iss = bonds.recent_issues(c["cik"], limit=6, since_days=760)
    except Exception:
        iss = []
    # 4. COMPOSITION DE LA TRESORERIE (note « Financial Instruments » du 10-K).
    #    VALIDATION : contre les PLACEMENTS a la date du 10-K, pas contre le dernier
    #    bilan connu. Comparer une composition de septembre 2025 au bilan de juin
    #    2026 produisait un ecart de 29 % qui n'etait pas une erreur de lecture mais
    #    de reference. Et la table ne liste que des TITRES : y opposer un total qui
    #    comprend les liquidites gonflait encore l'ecart.
    trez = None
    try:
        if ser:
            ref = None
            for p in ser:
                if p.get("inv"):
                    if ref is None or abs(_days(p["d"], ser[-1]["d"])) >= 0:
                        ref = p
            # placements a la date du dernier 10-K (approchee par la serie)
            tenk = bonds._latest_10k(c["cik"])
            if tenk:
                near = min((p for p in ser if p.get("inv")),
                           key=lambda p: abs(_days(min(p["d"], tenk["report"]),
                                                   max(p["d"], tenk["report"]))), default=None)
                if near:
                    trez = bonds.treasury(c["cik"], total_known=near["inv"])
    except Exception as e:
        log(f"    trésorerie {c['t']} : {repr(e)[:60]}")

    return {**{k: c[k] for k in ("t", "name", "grp", "color", "fy")},
            "cik": c["cik"], "ebit_proxy": ebit_proxy, "ie_last": ie_last, "ie_stale": stale,
            "young": young, "first": ser[0]["d"],
            "entity": d.get("entityName", c["name"]), "s": ser, "audit": audit,
            "ladder": lad, "tranches": trn, "issues": iss, "treasury": trez}


def self_audit(ser, ie_annual, debt_published, ticker):
    """AUDIT INTEGRE — tourne a CHAQUE rafraichissement, pas une seule fois.

    Chaque bug corrige dans ce fichier (taux Oracle a 386 %, dette IBM gonflee de
    7 Md$, papier commercial Microsoft compte double) avait produit un nombre faux
    plausible a l'oeil. Un controle qui ne tourne qu'une fois ne protege de rien :
    la prochaine societe qui change de tag reintroduirait le meme genre d'erreur en
    silence. Ces trois controles confrontent donc le resultat calcule a des
    references INDEPENDANTES publiees par l'emetteur lui-meme :

      A. a chaque cloture d'exercice, la charge d'interets doit egaler le fait
         annuel depose (0,5 % de tolerance) ;
      B. a chaque date ou l'emetteur publie un poste de dette CONSOLIDE, la dette
         calculee doit l'egaler (3 % de tolerance : ce poste est parfois le montant
         nominal, quand le bilan porte la valeur comptable apres decote) ;
      C. coherence interne : taux dans [0 %, 30 %], dette positive, ratio fini.

    Les ecarts sont journalises et comptes ; ils n'interrompent pas la collecte,
    mais ils remontent dans `meta.audit` et s'affichent sur la page.
    """
    n = bad = 0
    for p in ser:
        d0 = p["d"]
        if d0 in ie_annual and p.get("ie") is not None and not p.get("ieC"):
            n += 1
            ref = ie_annual[d0]
            if abs(p["ie"] - ref) > max(1e6, 0.005 * abs(ref)):
                bad += 1
                log(f"    ! {ticker} {d0} intérêts {p['ie']/1e6:.0f} ≠ déposé {ref/1e6:.0f} M$")
        if d0 in debt_published:
            n += 1
            ref = debt_published[d0]
            if not any(abs(v - ref) <= max(1e8, 0.03 * ref) for v in (p["bond"], p["debt"])):
                bad += 1
                log(f"    ! {ticker} {d0} dette {p['bond']/1e9:.2f}/{p['debt']/1e9:.2f} ≠ publiée {ref/1e9:.2f} Md$")
        n += 1
        # `ie < 0` attrape le piege n°6 (convention de signe) s'il resurgissait :
        # une charge d'interets negative n'existe pas.
        # GARDE-FOU TRESORERIE : liquidites NULLES alors que la societe detient des
        # placements. Aucune de ces societes n'a jamais eu zero de tresorerie — c'est
        # toujours le symptome d'un tag abandonne. C'est ce test qui a revele qu'Intel
        # avait cesse de taguer `CashAndCashEquivalentsAtCarryingValue` en 2024.
        # NB : un test par RATIO (placements >> liquidites) a ete essaye puis retire :
        # Microsoft detenait reellement 122 Md$ de titres pour 11 Md$ de liquidites en
        # 2018, un profil legitime qui declenchait 33 fausses alertes.
        bad_cash = (p.get("liq") or 0) <= 0 and (p.get("inv") or 0) > 0
        if p["debt"] < 0 or (p.get("ie") is not None and p["ie"] < 0) \
           or (p.get("rate") is not None and not 0 <= p["rate"] <= 30) \
           or (p.get("cov") is not None and abs(p["cov"]) > 1e5) or bad_cash:
            bad += 1
            log(f"    ! {ticker} {d0} incohérence ie={p.get('ie')} rate={p.get('rate')} "
                f"cov={p.get('cov')}" + (" · liquidités nulles" if bad_cash else ""))
    return {"n": n, "bad": bad}


# ─────────────────────────────────────────────────────────────────────────────
# MARCHE OBLIGATAIRE (FRED)
# ─────────────────────────────────────────────────────────────────────────────
def thin(obs, step):
    if not obs:
        return None
    ds, vs = obs["dates"], obs["values"]
    idx = list(range(0, len(ds), step))
    if idx[-1] != len(ds) - 1:
        idx.append(len(ds) - 1)
    return {"d": [ds[i] for i in idx], "v": [round(vs[i], 3) for i in idx]}


def build_market():
    """Courbes de cout de l'argent pour un emprunteur corporate.

    PIEGE FRED (avril 2026) : les series ICE BofA par notation ont ete TRONQUEES
    a 3 ans d'historique. Elles restent les plus precises pour le present, mais
    l'historique long vient de Moody's (Aaa/Baa, quotidien depuis 1983/1986) et de
    la courbe HQM du Tresor americain (haute qualite = AAA/AA/A, mensuelle 1984+).
    """
    m = {}
    hq = fetch_fred("HQMCB10YR", start="1984-01-01")
    if hq:
        m["hq10"] = {"d": hq["dates"], "v": [round(v, 3) for v in hq["values"]],
                     "id": "HQMCB10YR", "lbl": "Corporate haute qualité 10 ans"}
    for key, sid, lbl in (("aaa", "DAAA", "Moody's Aaa"), ("baa", "DBAA", "Moody's Baa"),
                          ("ust10", "DGS10", "Trésor US 10 ans")):
        o = fetch_fred(sid, start="1983-01-01")
        if o:
            t = thin(o, 5)                                # quotidien -> ~hebdomadaire
            m[key] = {**t, "id": sid, "lbl": lbl}
    ice = {}
    for key, sid, lbl in (("aa", "BAMLC0A2CAAEY", "ICE BofA AA"), ("a", "BAMLC0A3CAEY", "ICE BofA A"),
                          ("bbb", "BAMLC0A4CBBBEY", "ICE BofA BBB"), ("hy", "BAMLH0A0HYM2EY", "ICE BofA High Yield")):
        o = fetch_fred(sid)
        if o:
            ice[key] = {**thin(o, 2), "id": sid, "lbl": lbl}
    if ice:
        m["ice"] = ice
        m["ice_note"] = ("FRED a tronqué les séries ICE BofA à 3 ans d'historique en avril 2026 "
                         "— l'historique long vient de Moody's et de la courbe HQM.")
    return m


# ─────────────────────────────────────────────────────────────────────────────
# AGREGAT — a chaque date, somme du dernier 12 mois glissant connu de chaque societe
# ─────────────────────────────────────────────────────────────────────────────
def build_aggregate(cos):
    """Agregat = a chaque date, somme du dernier 12 mois glissant CONNU de chaque
    societe. Une societe qui a cesse de publier (Apple depuis l'exercice 2023) sort
    de l'agregat des interets au bout de 15 mois, mais reste dans l'agregat de dette
    et de tresorerie, qui eux restent publies. `nIe` dit toujours combien de societes
    composent le ratio, pour qu'une rupture de perimetre soit visible a l'ecran."""
    # GRILLE DE TRIMESTRES CALENDAIRES. Sommer aux dates propres de chaque societe
    # produirait des marches d'escalier artificielles : a la cloture d'Oracle (mai)
    # une seule societe se met a jour tandis que les onze autres restent figees.
    # On evalue donc a fin mars / juin / septembre / decembre, chaque societe
    # apportant son dernier exercice glissant connu a cette date.
    allp = sorted({p["d"] for c in cos for p in c["s"]})
    y0 = int(allp[0][:4])
    today = dt.date.today().isoformat()
    dates = []
    for y in range(y0, int(allp[-1][:4]) + 1):
        for md in ("-03-31", "-06-30", "-09-30", "-12-31"):
            g = f"{y}{md}"
            if allp[0] <= g <= min(allp[-1], today):
                dates.append(g)
    order = {c["t"]: [(p["d"], p) for p in c["s"]] for c in cos}
    agg = []
    for d0 in dates:
        debt = cash = ie = ebit = ocf = capex = icap = 0.0
        n = n_ie = 0
        for c in cos:
            pts = [p for dd, p in order[c["t"]] if dd <= d0]
            if not pts:
                continue
            last = pts[-1]
            if _days(last["d"], d0) <= 460:                # bilan encore d'actualite
                debt += last["debt"]
                cash += last["cash"]
                n += 1
            # pour les flux : dernier point PORTANT la donnee, meme s'il est anterieur
            fl = [p for p in pts if p.get("ie") is not None and p.get("ebit") is not None]
            if fl and _days(fl[-1]["d"], d0) <= 460:
                p = fl[-1]
                ie += p["ie"]
                ebit += p["ebit"]
                ocf += p.get("ocf") or 0
                capex += p.get("capex") or 0
                icap += p.get("icap") or 0
                n_ie += 1
        if n < 3:
            continue
        row = {"d": d0, "debt": round(debt), "cash": round(cash), "net": round(cash - debt), "n": n}
        if n_ie >= 3 and ie > 0:
            row.update({"ie": round(ie), "ebit": round(ebit), "nIe": n_ie,
                        "cov": round(ebit / ie, 2)})
            if ebit > 0:
                row["shEbit"] = round(100 * ie / ebit, 3)
            if ocf:
                row["ocf"] = round(ocf)
            if capex:
                row["capex"] = round(capex)
            if icap:
                row["icap"] = round(icap)
        agg.append(row)
    # Taux moyen de l'ensemble : interets 12 mois / dette MOYENNE des 12 mois,
    # exactement la definition retenue societe par societe. La calculer sur la dette
    # de cloture aurait donne 2,63 % contre 3,0 % reels : le groupe a emprunte 180 Md$
    # dans l'annee, et la dette de fin d'annee n'a pas porte d'interets toute l'annee.
    for i, r in enumerate(agg):
        win = [x["debt"] for x in agg[max(0, i - 3):i + 1] if x["debt"] > 0]
        if r.get("ie") and len(win) >= 4:
            r["rate"] = round(100 * r["ie"] / (sum(win) / len(win)), 3)
    return agg


def build():
    cos = []
    for c in COMPANIES:
        r = build_company(c)
        if r:
            last = r["s"][-1]
            log(f"  {c['t']:6s} {len(r['s']):3d} pts | dette {last['debt']/1e9:7.1f} Md$ | "
                f"intérêts {(last.get('ie') or 0)/1e9:5.2f} Md$ | taux {last.get('rate') or float('nan'):5.2f}% | "
                f"couv {last.get('cov') or float('nan'):7.1f}x | part EBIT {last.get('shEbit') or float('nan'):6.2f}%")
            cos.append(r)
        time.sleep(0.2)
    if len(cos) < 6:
        log("trop peu de sociétés récupérées — abort")
        return None
    # Mur de maturites AGREGE : somme, annee par annee, des echeanciers publies.
    # `n` dit sur combien de societes il porte — une couverture partielle doit se
    # voir a l'ecran plutot que se deviner.
    lad_keys = ("y1", "y2", "y3", "y4", "y5", "after")
    lad_tot = {k: 0.0 for k in lad_keys}
    lad_n = 0
    for c in cos:
        L = c.get("ladder")
        if not L:
            continue
        lad_n += 1
        for k in lad_keys:
            lad_tot[k] += L.get(k, 0) or 0
    ladder_agg = ({**{k: round(v) for k, v in lad_tot.items()},
                   "total": round(sum(lad_tot.values())), "n": lad_n,
                   "of": len(cos)} if lad_n >= 3 else None)
    n_tr = sum(1 for c in cos if c.get("tranches"))
    n_is = sum(1 for c in cos if c.get("issues"))
    log(f"couches ajoutées : échéancier {lad_n}/{len(cos)} · tranches validées {n_tr}/{len(cos)} · émissions 424B {n_is}/{len(cos)}")

    agg = build_aggregate(cos)
    groups = {"all": agg}
    for g in ("core", "infra", "other"):
        sub = [c for c in cos if c["grp"] == g]
        if len(sub) >= 2:
            groups[g] = build_aggregate(sub)
    mkt = build_market()
    cur = agg[-1] if agg else {}
    an = sum(c["audit"]["n"] for c in cos)
    ab = sum(c["audit"]["bad"] for c in cos)
    log(f"AUDIT INTÉGRÉ : {an} contrôles, {ab} écart(s)")
    audit_txt = (f"{an} contrôles, aucun écart." if ab == 0
                 else f"{an} contrôles, {ab} écart(s) — voir les journaux du collecteur.")
    return {
        "meta": {
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "source": "SEC EDGAR XBRL companyfacts (10-K / 10-Q) + FRED",
            "universe": len(cos),
            "audit": audit_txt,
            "audit_n": an, "audit_bad": ab,
            "note": "Tous les ratios sont calculés sur 12 mois glissants à partir des comptes déposés.",
        },
        "current": cur,
        "companies": cos,
        "aggregate": agg,
        "groups": groups,
        "ladder": ladder_agg,
        "market": mkt,
    }


def write(p):
    js = (f"/* {OUT_NAME}.js — {p['meta']['updated_at']} — SEC EDGAR XBRL + FRED */\n"
          f"window.__TECH_DEBT__={json.dumps(p, separators=(',', ':'), ensure_ascii=False)};\n")
    targets = []
    for d in (CACHE_DIR, REPO_DIR, HERE):
        if d not in targets and d.exists():
            targets.append(d)
    for d in targets:
        try:
            (d / f"{OUT_NAME}.js").write_text(js, encoding="utf-8")
            (d / f"{OUT_NAME}.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass          # Desktop inaccessible sous launchd (TCC) -> snapshot_site.sh sync
    try:
        mf = REPO_DIR / "_cache_files_synced.txt"
        if mf.exists():
            have = mf.read_text().splitlines()
            for w in (f"{OUT_NAME}.js", f"{OUT_NAME}.json"):
                if w not in have:
                    have.append(w)
            mf.write_text("\n".join(have) + "\n")
    except OSError:
        pass
    log(f"écrit ({len(js)/1024:.0f} KB) dans {len(targets)} dossier(s)")


def main():
    tgt = CACHE_DIR / f"{OUT_NAME}.json"
    if tgt.exists() and "--force" not in sys.argv:
        age = (time.time() - tgt.stat().st_mtime) / 3600
        if age < CACHE_MAX_HOURS:
            log(f"frais ({age:.1f}h) — skip")
            return
    p = build()
    if p:
        write(p)
        log("Terminé.")


if __name__ == "__main__":
    main()
