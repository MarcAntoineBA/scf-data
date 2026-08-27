#!/usr/bin/env python3
"""États financiers complets depuis les dépôts XBRL de la SEC.

POURQUOI CE COLLECTEUR EXISTE
Le site savait comparer des secteurs, pas lire une entreprise. Les ratios qu'il
possédait venaient tous de Yahoo : un instantané, sans historique, sans source
citable, et sans les lignes du compte de résultat. Impossible d'y bâtir une note
de qualité, un compte de résultat en flux, ou seulement une courbe de marges sur
dix ans.

L'API XBRL de la SEC donne tout cela GRATUITEMENT, sans clé, sans quota
mensuel : `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` rend l'intégralité
des faits comptables déposés par une société depuis ~2009. Mesuré sur NVIDIA le
2026-08-27 : 627 concepts, exercices 2010 à 2026.

CE QUE ÇA CHANGE, ET QUI NE S'ACHÈTE PAS
Chaque chiffre porte son numéro de dépôt (`accn`) et sa date. Le lecteur peut
remonter au 10-K. Un concurrent qui revend un agrégateur affiche un nombre ;
ici on affiche un nombre ET d'où il vient. C'est la seule chose qu'on peut faire
mieux sans budget.

LES LIMITES, DITES ICI PLUTÔT QUE DÉCOUVERTES PLUS TARD
  · États-Unis seulement. Un déposant SEC, c'est un 10-K (société américaine) ou
    un 20-F/40-F (émetteur étranger coté aux États-Unis). LVMH n'y est pas.
  · L'XBRL commence vers 2009 pour les grandes capitalisations, plus tard pour
    les petites. « Quarante ans d'historique » n'est pas atteignable par cette
    voie — on affiche ce qu'on a, daté, plutôt qu'un chiffre sans provenance.
  · Les banques, assurances et foncières ne publient pas les mêmes lignes
    (pas de coût des ventes, pas de marge brute). Les champs manquants restent
    vides ; c'est au calcul de la note de savoir qu'il ne peut pas les noter.

CADENCE ET POLITESSE
La SEC autorise 10 requêtes par seconde et exige un en-tête d'identification
nominatif. On reste sous la limite avec le même débit que les trois collecteurs
SEC déjà en place (`fetch_13f.py`, `fetch_sec_inities.py`, `fetch_sec_rachats.py`) :
0,11 s entre deux requêtes. ~350 sociétés ≈ 1 minute.

SORTIES
  · sec_fundamentals_index.{json,js} — un résumé par société (dernier exercice,
    croissances, scores). Léger, chargé par la page.
  · sec_detail_<INITIALE>.json — les séries annuelles complètes, regroupées par
    initiale du ticker, chargées au clic. Le découpage n'est pas un détail : le
    dépôt porte la trace d'un incident où 14,5 Mo chargés d'un coup ont mis 30 s.
"""
# ── Garde-fou de durée, comme les autres collecteurs : un script bloqué sur une
#    I/O réseau ne doit pas monopoliser le créneau du suivant.
import signal as _signal, sys as _sys
def _global_timeout_handler(signum, frame):
    print("[fatal] délai global (25 min) atteint — abandon.", file=_sys.stderr)
    _sys.exit(2)
try:
    _signal.signal(_signal.SIGALRM, _global_timeout_handler)
    _signal.alarm(25 * 60)
except Exception:
    pass

import json
import os
import sys
import time
import gzip
import math
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR = CACHE_DIR

OUT_JSON = CACHE_DIR / "sec_fundamentals_index.json"
OUT_JS = CACHE_DIR / "sec_fundamentals_index.js"
TRACKER_CACHE = CACHE_DIR / "tradfi_cache.json"

# La SEC refuse les requêtes sans identification nominative (403). La variable
# est déjà utilisée par les trois autres collecteurs SEC du dépôt.
UA = os.environ.get("SCF_CONTACT_UA") or "Capital Antifragile marcantoine.bassetti@gmail.com"
DEBIT = 0.11          # s entre deux requêtes — la SEC autorise 10/s, on reste dessous
TIMEOUT = 45
RETRIES = 3

_last_call = [0.0]


def _get(url, accept_404=False):
    """GET avec débit maîtrisé, gzip, et reprise sur 429/503."""
    for essai in range(RETRIES):
        delta = time.time() - _last_call[0]
        if delta < DEBIT:
            time.sleep(DEBIT - delta)
        _last_call[0] = time.time()
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept-Encoding": "gzip",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 404 and accept_404:
                return None
            if e.code in (429, 503) and essai < RETRIES - 1:
                # La SEC ralentit avant de bloquer : on lui laisse le temps.
                time.sleep(2.0 * (essai + 1))
                continue
            if essai == RETRIES - 1:
                raise
        except Exception:
            if essai == RETRIES - 1:
                raise
            time.sleep(1.5 * (essai + 1))
    return None


# ─────────────────────────────────────────────────────────────────────────
# Concepts US-GAAP, avec leurs synonymes
# ─────────────────────────────────────────────────────────────────────────
# Une même réalité comptable porte plusieurs étiquettes selon l'année et le
# rédacteur du dépôt : le chiffre d'affaires est tantôt `Revenues`, tantôt
# `RevenueFromContractWithCustomerExcludingAssessedTax` (depuis ASC 606, 2018),
# tantôt `SalesRevenueNet` (avant). Une seule étiquette laisserait des trous de
# plusieurs années au milieu d'une série — le genre de trou qu'on prend pour une
# chute d'activité. L'ordre compte : le premier trouvé gagne, on met donc en tête
# la formulation la plus spécifique.
CONCEPTS = {
    # ── Compte de résultat (durée) ──
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet"],
    "cogs": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold",
             "CostOfServices"],
    "gross_profit": ["GrossProfit"],
    "rd": ["ResearchAndDevelopmentExpense",
           "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
    "sga": ["SellingGeneralAndAdministrativeExpense"],
    "ga": ["GeneralAndAdministrativeExpense"],
    "sm": ["SellingAndMarketingExpense"],
    "opex": ["OperatingExpenses", "CostsAndExpenses"],
    "operating_income": ["OperatingIncomeLoss"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
               "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "tax": ["IncomeTaxExpenseBenefit"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "interest_expense": ["InterestExpense", "InterestExpenseDebt",
                         "InterestExpenseNonoperating"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "eps_basic": ["EarningsPerShareBasic"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "shares_basic": ["WeightedAverageNumberOfSharesOutstandingBasic",
                     "WeightedAverageNumberOfSharesOutstanding"],

    # ── Bilan (instant) ──
    "assets": ["Assets"],
    "assets_current": ["AssetsCurrent"],
    "liabilities": ["Liabilities"],
    "liabilities_current": ["LiabilitiesCurrent"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    # Placements à court terme. Beaucoup d'étiquettes pour une même réalité :
    # NVIDIA a utilisé `MarketableSecurities` jusqu'en 2024, puis
    # `MarketableSecuritiesCurrent`, puis la ventilation par échéance. On prend
    # la PART À MOINS D'UN AN seulement — les participations stratégiques et les
    # titres à plus d'un an ne sont pas de la trésorerie, quoi qu'en dise le
    # calcul de dette nette d'un concurrent.
    "short_term_inv": ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                       "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                       "AvailableForSaleSecuritiesCurrent",
                       "AvailableForSaleSecuritiesDebtMaturitiesWithinOneYearFairValue",
                       "OtherShortTermInvestments"],
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "current_debt": ["LongTermDebtCurrent", "DebtCurrent"],
    # Les loyers capitalisés SONT de la dette depuis IFRS 16 / ASC 842, et les
    # omettre sous-estime l'endettement de tout ce qui loue ses locaux ou ses
    # centres de données. Contrôle sur NVIDIA, exercice 2026 : 7 469 + 999 de
    # dette financière + 2 572 + 372 de loyers = 11 412 M$, soit exactement le
    # chiffre publié par le concurrent. Sans les loyers on tombait à 8 468.
    "lease_lt": ["OperatingLeaseLiabilityNoncurrent", "FinanceLeaseLiabilityNoncurrent"],
    "lease_ct": ["OperatingLeaseLiabilityCurrent", "FinanceLeaseLiabilityCurrent"],
    "goodwill": ["Goodwill"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
    "inventory": ["InventoryNet"],

    # ── Flux de trésorerie (durée) ──
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets",
              "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
              "PaymentsToAcquireOtherPropertyPlantAndEquipment",
              "PaymentsForCapitalImprovements"],
    "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "dna": ["DepreciationDepletionAndAmortization",
            "DepreciationAmortizationAndAccretionNet", "Depreciation"],
    "dps": ["CommonStockDividendsPerShareDeclared",
            "CommonStockDividendsPerShareCashPaid"],
}

# Un fait de DURÉE (chiffre d'affaires) porte un début et une fin ; un fait
# d'INSTANT (trésorerie) n'a qu'une fin. Les confondre revient à comparer un flux
# annuel à un solde ponctuel.
INSTANTS = {"assets", "assets_current", "liabilities", "liabilities_current",
            "equity", "cash", "short_term_inv", "lt_debt", "current_debt",
            "lease_lt", "lease_ct",
            "goodwill", "retained_earnings", "inventory"}

FORMES_ANNUELLES = ("10-K", "20-F", "40-F")


def _annuels(facts, concept_noms, instant=False):
    """{date_de_fin: (valeur, accn, date_de_depot)} pour les exercices publiés.

    ⚠ RECOUDRE LES ÉTIQUETTES, NE PAS EN CHOISIR UNE.
    La version naïve prenait le premier concept de la liste qui contenait des
    données. Mesuré sur NVIDIA le 2026-08-27, c'est faux et c'est SILENCIEUX :
    `RevenueFromContractWithCustomerExcludingAssessedTax` n'y couvre que 2019 à
    2022 (la société a adopté puis abandonné cette étiquette), tandis que
    `Revenues` couvre 2010 à 2026. La série rendue s'arrêtait donc quatre ans
    trop tôt — sans erreur, sans trou visible, juste un dernier exercice
    plausible et périmé, et toutes les marges, croissances et scores calculés
    dessus.
    Une société change d'étiquette au fil des normes comptables et des rédacteurs
    de ses dépôts. La série est donc l'UNION de toutes les étiquettes, date par
    date, et l'ordre de la liste ne sert plus qu'à départager deux étiquettes qui
    couvrent la MÊME date — la plus spécifique gagne.

    Deux précautions conservées :
      · une société qui corrige un exercice le redépose ; deux valeurs cohabitent
        alors pour la même date et la même étiquette. On garde la PLUS RÉCEMMENT
        DÉPOSÉE — le chiffre que la société tient aujourd'hui pour vrai.
      · un fait de durée n'est retenu que si sa fenêtre fait un an (340 à 400
        jours). Sans ce filtre, les cumuls de neuf mois entrent dans la série
        annuelle et y dessinent de fausses chutes de 25 %.
    """
    us = facts.get("us-gaap") or {}
    out = {}          # fin -> (valeur, accn, depose, rang_du_concept)
    for rang, nom in enumerate(concept_noms):
        bloc = us.get(nom)
        if not bloc:
            continue
        unites = bloc.get("units") or {}
        # L'unité la plus peuplée (USD, USD/shares, shares…). Les autres sont des
        # doublons en devise étrangère ou des unités marginales.
        cle_unite = max(unites, key=lambda u: len(unites[u]), default=None)
        if not cle_unite:
            continue
        for p in unites[cle_unite]:
            forme = (p.get("form") or "").split("/")[0]   # « 10-K/A » compte comme « 10-K »
            if forme not in FORMES_ANNUELLES:
                continue
            if not instant:
                if p.get("fp") != "FY":
                    continue
                d0, d1 = p.get("start"), p.get("end")
                if not d0 or not d1:
                    continue
                try:
                    jours = (datetime.fromisoformat(d1) - datetime.fromisoformat(d0)).days
                except Exception:
                    continue
                if not (340 <= jours <= 400):
                    continue
            fin, val = p.get("end"), p.get("val")
            if fin is None or val is None:
                continue
            depose = p.get("filed") or ""
            prec = out.get(fin)
            if prec is None:
                out[fin] = (float(val), p.get("accn"), depose, rang)
            elif rang < prec[3]:
                # étiquette plus spécifique pour la même date : elle l'emporte
                out[fin] = (float(val), p.get("accn"), depose, rang)
            elif rang == prec[3] and depose >= prec[2]:
                # même étiquette, dépôt plus récent : c'est le chiffre corrigé
                out[fin] = (float(val), p.get("accn"), depose, rang)
    return {k: (v[0], v[1], v[2]) for k, v in out.items()}


def _val(serie, fin):
    v = serie.get(fin)
    return v[0] if v else None


def _div(a, b):
    if a is None or b in (None, 0):
        return None
    try:
        r = a / b
        return r if math.isfinite(r) else None
    except Exception:
        return None


def _pct(a, b):
    r = _div(a, b)
    return round(r * 100, 2) if r is not None else None


def _r(v, n=2):
    return round(v, n) if isinstance(v, (int, float)) and math.isfinite(v) else None


# ─────────────────────────────────────────────────────────────────────────
# Croissances, prédictibilité, scores
# ─────────────────────────────────────────────────────────────────────────
def _croissance_annuelle(series):
    """[(annee, valeur)] triés → liste des variations en % d'une année sur l'autre.

    Une variation calculée sur une base NÉGATIVE n'a pas de sens : passer de
    −10 à −5 n'est pas « +50 % » et passer de −5 à +5 n'est pas « +200 % ».
    Ces cas rendent None plutôt qu'un nombre spectaculaire et faux.
    """
    out = []
    for i in range(1, len(series)):
        prev, cur = series[i - 1][1], series[i][1]
        if prev is None or cur is None or prev <= 0:
            out.append(None)
        else:
            out.append(round(100 * (cur - prev) / prev, 2))
    return out


def _mediane(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    n = len(v)
    return round(v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2, 2)


def _croissances(series):
    """{'1a':…, '5a':…, '10a':…} — dernière variation, puis MÉDIANES.

    Médiane et non moyenne : c'est le choix du barème qu'on veut reproduire, et
    c'est le bon choix — une année de rebond après un creux (+300 %) tirerait
    une moyenne décennale vers le haut et ferait passer une société cyclique
    pour une société en croissance.
    """
    g = _croissance_annuelle(series)
    return {
        "1a": g[-1] if g else None,
        "5a": _mediane(g[-5:]) if len(g) >= 3 else None,
        "10a": _mediane(g[-10:]) if len(g) >= 5 else None,
        "n": len(g),
    }


def _predictibilite(series):
    """0-100 : à quel point le chiffre d'affaires suit une exponentielle propre.

    C'est le coefficient de détermination d'une régression linéaire sur le
    LOGARITHME du chiffre d'affaires. Le log parce qu'une entreprise saine croît
    en pourcentage, pas en montant : sans lui, une société qui double tous les
    trois ans serait jugée « imprévisible » parce que sa courbe s'incurve.
    100 = série parfaitement régulière. Une valeur négative ou nulle interrompt
    la série (on ne prend pas le log d'un chiffre d'affaires négatif).
    """
    pts = [(i, v) for i, (_, v) in enumerate(series) if v is not None and v > 0]
    if len(pts) < 5:
        return None
    xs = [p[0] for p in pts]
    ys = [math.log(p[1]) for p in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    r2 = (sxy * sxy) / (sxx * syy)
    return round(max(0.0, min(1.0, r2)) * 100, 1)


def _serie_hausses_dividende(dps_par_annee):
    """Années consécutives de hausse du dividende par action, en partant de la fin.

    On s'arrête à la première année sans hausse. Une société qui a augmenté
    trente ans puis gelé une fois est à 0, pas à 30 : c'est la définition, et
    c'est ce qui fait la valeur du critère.
    """
    vals = [v for _, v in dps_par_annee if v is not None]
    if len(vals) < 2:
        return 0
    streak = 0
    for i in range(len(vals) - 1, 0, -1):
        if vals[i] > vals[i - 1]:
            streak += 1
        else:
            break
    return streak


# Facteurs de division d'action réellement pratiqués. Une société divise par un
# nombre simple ; personne ne divise par 3,7.
_FACTEURS_USUELS = [1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 50]


def _facteur_division(r):
    """Le facteur de division si `r` y ressemble, sinon None.

    6 % de tolérance : entre deux exercices, le nombre d'actions bouge aussi par
    rachats et attributions. Au-delà, ce n'est plus une division déguisée.
    """
    for f in _FACTEURS_USUELS:
        if abs(r - f) / f < 0.06:
            return f
        if abs(r * f - 1.0) < 0.06:      # regroupement (division inversée)
            return 1.0 / f
    return None


def _corriger_divisions(exercices):
    """Recoud les séries PAR ACTION que les divisions d'action coupent en deux.

    LE PIÈGE, ET IL EST SILENCIEUX. La SEC conserve les dépôts TELS QU'ILS ONT
    ÉTÉ FAITS. Le 10-K de 2019 de NVIDIA dit « dividende 0,61 $ par action » ;
    après deux divisions (4 pour 1 en 2021, 10 pour 1 en 2024), le même exercice
    vaut 0,015 $ dans les dépôts récents. Les deux chiffres sont exacts. La série
    qui les enchaîne ne l'est pas : elle affiche une chute de 97 % là où rien ne
    s'est passé, et toute croissance « par action » calculée dessus est fausse.
    Mesuré ici avant correction : croissance médiane du BPA sur 5 ans à 122 %
    contre 95 % chez le concurrent — l'écart était entièrement l'artefact.

    LA SIGNATURE D'UNE DIVISION, et de rien d'autre : le nombre d'actions est
    multiplié par un facteur proche d'un nombre simple, ET le résultat net ne
    suit PAS. Un rachat massif ne fait pas fois dix ; une augmentation de capital
    qui ferait fois dix ferait aussi bondir le résultat. On exige les deux
    conditions, sinon on ne touche à rien — mieux vaut une série non corrigée
    qu'une série corrigée à tort.

    On normalise sur le PLUS RÉCENT : c'est le nombre d'actions d'aujourd'hui,
    celui que le lecteur voit dans son courtier.
    """
    n = len(exercices)
    if n < 3:
        return []
    evenements = []
    cumul = 1.0
    facteurs = [1.0] * n          # facteur à appliquer au NOMBRE D'ACTIONS de l'année
    for i in range(n - 1, 0, -1):
        a, b = exercices[i - 1], exercices[i]
        sa, sb = a.get("shares_diluted"), b.get("shares_diluted")
        if sa and sb and sa > 0:
            f = _facteur_division(sb / sa)
            if f is not None:
                na, nb = a.get("net_income"), b.get("net_income")
                confirme = True
                if na and nb and na > 0:
                    # Si le résultat net a été multiplié par le même facteur,
                    # c'est une vraie croissance et non une division.
                    if abs((nb / na) - (sb / sa)) / max(sb / sa, 1e-9) < 0.20:
                        confirme = False
                if confirme:
                    cumul *= f
                    evenements.append({
                        "entre": a["annee"], "et": b["annee"],
                        "facteur": round(f, 4),
                        "actions_avant": sa, "actions_apres": sb,
                    })
        facteurs[i - 1] = cumul

    if cumul == 1.0:
        return []

    for i, e in enumerate(exercices):
        f = facteurs[i]
        if f == 1.0:
            continue
        for cle in ("shares_diluted", "shares_basic"):
            if e.get(cle) is not None:
                e[cle] = e[cle] * f
        for cle in ("eps_diluted", "eps_basic", "dps"):
            if e.get(cle) is not None:
                e[cle] = e[cle] / f
        e["_facteur_division"] = round(f, 4)
    return list(reversed(evenements))


def _piotroski(cur, prev):
    """F-Score de Piotroski, 0 à 9. None si le bilan n'a pas de quoi le calculer.

    Neuf tests binaires en trois familles : la société gagne-t-elle de l'argent,
    s'endette-t-elle moins, travaille-t-elle mieux. Le détail est rendu en même
    temps que le total — un 7/9 ne dit rien, un 7/9 dont les deux points perdus
    sont la dilution dit quelque chose.
    """
    if not cur or not prev:
        return None, {}
    d = {}
    roa_c = _div(cur.get("net_income"), cur.get("assets"))
    roa_p = _div(prev.get("net_income"), prev.get("assets"))
    ocf = cur.get("ocf")
    d["roa_positif"] = 1 if (roa_c is not None and roa_c > 0) else 0
    d["cash_positif"] = 1 if (ocf is not None and ocf > 0) else 0
    d["roa_en_hausse"] = 1 if (roa_c is not None and roa_p is not None and roa_c > roa_p) else 0
    d["qualite_du_resultat"] = 1 if (ocf is not None and cur.get("net_income") is not None
                                     and ocf > cur["net_income"]) else 0
    lev_c = _div(cur.get("lt_debt"), cur.get("assets"))
    lev_p = _div(prev.get("lt_debt"), prev.get("assets"))
    d["dette_en_baisse"] = 1 if (lev_c is not None and lev_p is not None and lev_c < lev_p) else 0
    cr_c = _div(cur.get("assets_current"), cur.get("liabilities_current"))
    cr_p = _div(prev.get("assets_current"), prev.get("liabilities_current"))
    d["liquidite_en_hausse"] = 1 if (cr_c is not None and cr_p is not None and cr_c > cr_p) else 0
    sh_c, sh_p = cur.get("shares_diluted"), prev.get("shares_diluted")
    d["pas_de_dilution"] = 1 if (sh_c is not None and sh_p is not None and sh_c <= sh_p * 1.005) else 0
    gm_c = _div(cur.get("gross_profit"), cur.get("revenue"))
    gm_p = _div(prev.get("gross_profit"), prev.get("revenue"))
    d["marge_en_hausse"] = 1 if (gm_c is not None and gm_p is not None and gm_c > gm_p) else 0
    at_c = _div(cur.get("revenue"), cur.get("assets"))
    at_p = _div(prev.get("revenue"), prev.get("assets"))
    d["rotation_en_hausse"] = 1 if (at_c is not None and at_p is not None and at_c > at_p) else 0
    return sum(d.values()), d


def _altman_z(cur, mcap_usd):
    """Z-Score d'Altman, version sociétés cotées.

    Z = 1,2·FR/A + 1,4·RÉ/A + 3,3·EBIT/A + 0,6·CAPI/DETTES + 1,0·CA/A
    Au-dessus de 2,99 : zone sûre. En dessous de 1,81 : zone de détresse.
    ⚠ Le modèle a été calibré sur des industriels des années 1960. Une société
    de logiciels sans actifs corporels et sans dette y sort des valeurs
    astronomiques qui ne veulent rien dire — c'est pourquoi on rend AUSSI le
    détail, et pourquoi la fiche devra le cadrer plutôt que l'afficher nu.
    """
    A = cur.get("assets")
    if not A or A <= 0 or not mcap_usd:
        return None, {}
    fr = None
    if cur.get("assets_current") is not None and cur.get("liabilities_current") is not None:
        fr = cur["assets_current"] - cur["liabilities_current"]
    dettes = cur.get("liabilities")
    ebit = cur.get("operating_income")
    parts = {
        "fonds_de_roulement": _r(1.2 * fr / A, 3) if fr is not None else None,
        "reserves": _r(1.4 * cur["retained_earnings"] / A, 3) if cur.get("retained_earnings") is not None else None,
        "resultat_exploitation": _r(3.3 * ebit / A, 3) if ebit is not None else None,
        "capitalisation_sur_dettes": _r(0.6 * mcap_usd / dettes, 3) if dettes else None,
        "rotation": _r(1.0 * cur["revenue"] / A, 3) if cur.get("revenue") is not None else None,
    }
    presentes = [v for v in parts.values() if v is not None]
    if len(presentes) < 4:
        return None, parts
    return _r(sum(presentes), 2), parts


# ─────────────────────────────────────────────────────────────────────────
# Construction de la série annuelle d'une société
# ─────────────────────────────────────────────────────────────────────────
def construire(facts, mcap_usd=None):
    series = {}
    for cle, noms in CONCEPTS.items():
        series[cle] = _annuels(facts, noms, instant=(cle in INSTANTS))

    # Les dates d'arrêté du chiffre d'affaires font l'ossature. Si une société
    # n'a pas de chiffre d'affaires publié (rare, mais les holdings financières
    # en font), on retombe sur le résultat net.
    axe = sorted(series["revenue"].keys()) or sorted(series["net_income"].keys())
    if not axe:
        return None

    exercices = []
    for fin in axe:
        e = {"fin": fin, "annee": int(fin[:4])}
        for cle in CONCEPTS:
            e[cle] = _val(series[cle], fin)

        # Reconstructions quand la ligne n'est pas déposée telle quelle.
        if e["gross_profit"] is None and e["revenue"] is not None and e["cogs"] is not None:
            e["gross_profit"] = e["revenue"] - e["cogs"]
        if e["sga"] is None and (e["ga"] is not None or e["sm"] is not None):
            e["sga"] = (e["ga"] or 0) + (e["sm"] or 0)
        if e["pretax"] is None and e["net_income"] is not None and e["tax"] is not None:
            e["pretax"] = e["net_income"] + e["tax"]

        # Le capex est déposé en montant POSITIF de décaissement. On le garde
        # positif : « investissements de l'année », pas « flux négatif ».
        if e["capex"] is not None:
            e["capex"] = abs(e["capex"])
        for k in ("dividends_paid", "buybacks"):
            if e[k] is not None:
                e[k] = abs(e[k])

        e["fcf"] = (e["ocf"] - e["capex"]) if (e["ocf"] is not None and e["capex"] is not None) else None
        e["ebitda"] = (e["operating_income"] + e["dna"]) if (e["operating_income"] is not None and e["dna"] is not None) else None

        # Trois grandeurs distinctes, nommées séparément parce qu'on les affiche
        # séparément : la trésorerie STRICTE (ce que montre un graphe « Cash »),
        # les liquidités (trésorerie + placements à moins d'un an, ce qui se
        # déduit vraiment de la dette), et la dette TOTALE loyers compris.
        e["tresorerie"] = e["cash"]
        liquidites = ((e["cash"] or 0) + (e["short_term_inv"] or 0)) if e["cash"] is not None else None
        dette_totale = None
        if any(e[k] is not None for k in ("lt_debt", "current_debt", "lease_lt", "lease_ct")):
            dette_totale = ((e["lt_debt"] or 0) + (e["current_debt"] or 0)
                            + (e["lease_lt"] or 0) + (e["lease_ct"] or 0))
        e["liquidites"] = liquidites
        e["tresorerie_totale"] = liquidites          # nom conservé pour compatibilité
        e["dette_totale"] = dette_totale
        e["dette_nette"] = (dette_totale - liquidites) if (dette_totale is not None and liquidites is not None) else None

        # ── Ratios de l'exercice ──
        e["marge_brute"] = _pct(e["gross_profit"], e["revenue"])
        e["marge_ope"] = _pct(e["operating_income"], e["revenue"])
        e["marge_nette"] = _pct(e["net_income"], e["revenue"])
        e["marge_fcf"] = _pct(e["fcf"], e["revenue"])
        # Le taux d'impôt retenu est celui RÉELLEMENT payé cette année-là, pas un
        # taux légal théorique : c'est ce qui distingue une société qui optimise
        # d'une société qui subit. Borné à [0 ; 50 %] pour qu'un crédit d'impôt
        # exceptionnel ne fabrique pas un résultat après impôt supérieur au brut.
        taux = _div(e["tax"], e["pretax"])
        if taux is None or not (0 <= taux <= 0.5):
            taux = 0.21
        e["taux_impot"] = round(taux * 100, 1)
        e["nopat"] = e["operating_income"] * (1 - taux) if e["operating_income"] is not None else None

        # Bases de capital, à la CLÔTURE. Les rendements sont calculés plus bas
        # sur la MOYENNE de deux clôtures — voir le commentaire du second passage.
        e["_capital_investi"] = (e["equity"] + dette_totale - (liquidites or 0)) \
            if (e["equity"] is not None and dette_totale is not None) else None
        e["_capitaux_employes"] = (e["assets"] - e["liabilities_current"]) \
            if (e["assets"] is not None and e["liabilities_current"] is not None) else None

        e["capex_ca"] = _pct(e["capex"], e["revenue"])
        e["capex_ocf"] = _pct(e["capex"], e["ocf"]) if (e["ocf"] and e["ocf"] > 0) else None
        e["rd_ocf"] = _pct(e["rd"], e["ocf"]) if (e["ocf"] and e["ocf"] > 0) else None
        e["sbc_fcf"] = _pct(e["sbc"], e["fcf"]) if (e["fcf"] and e["fcf"] > 0) else None
        e["dette_ebitda"] = _r(_div(e["dette_nette"], e["ebitda"]), 2) if (e["ebitda"] and e["ebitda"] > 0) else None
        e["dette_ebitda_brut"] = _r(_div(dette_totale, e["ebitda"]), 2) if (e["ebitda"] and e["ebitda"] > 0) else None
        e["couverture_interets"] = _r(_div(e["operating_income"], e["interest_expense"]), 1) if (e["interest_expense"] and e["interest_expense"] > 0) else None
        e["goodwill_actifs"] = _pct(e["goodwill"], e["assets"])
        e["payout_benefices"] = _pct(e["dividends_paid"], e["net_income"]) if (e["net_income"] and e["net_income"] > 0) else None
        e["payout_fcf"] = _pct(e["dividends_paid"], e["fcf"]) if (e["fcf"] and e["fcf"] > 0) else None
        e["retour_actionnaire"] = ((e["dividends_paid"] or 0) + (e["buybacks"] or 0)) if (e["dividends_paid"] is not None or e["buybacks"] is not None) else None

        # ⚠ Les grandeurs PAR ACTION ne sont PAS calculées ici : le nombre
        # d'actions n'est pas encore recousu des divisions. Voir plus bas.

        # Provenance : le dépôt d'où sort le chiffre d'affaires de l'année.
        src = series["revenue"].get(fin) or series["net_income"].get(fin)
        e["accn"] = src[1] if src else None
        e["depose_le"] = src[2] if src else None
        exercices.append(e)

    # Les divisions d'action d'abord : tout ce qui suit se calcule « par action »
    # et serait faux sur une série non recousue.
    divisions = _corriger_divisions(exercices)

    # ── SECOND PASSAGE : les rendements, sur CAPITAUX MOYENS ────────────────
    # Un rendement rapporte un FLUX de l'année (le résultat) à un STOCK (les
    # capitaux). Prendre le stock de clôture fait diviser le résultat d'une année
    # entière par un capital que la société n'a possédé que le dernier jour —
    # et sous-estime d'autant tout ce qui grossit vite.
    # Mesuré sur NVIDIA, exercice 2026 : capitaux propres 79,3 Md$ à l'ouverture,
    # 157,3 Md$ à la clôture. Sur la clôture, ROE = 76,3 %. Sur la moyenne des
    # deux, ROE = 101,5 % — soit exactement le chiffre publié par le concurrent,
    # et la convention des manuels.
    # Le premier exercice de la série n'a pas d'ouverture : il retombe sur la
    # clôture, faute de mieux, et c'est signalé par `_base_capital`.
    def _moy(cle, i):
        cur = exercices[i].get(cle)
        if cur is None:
            return None, "aucune"
        if i == 0:
            return cur, "cloture"
        prev = exercices[i - 1].get(cle)
        if prev is None:
            return cur, "cloture"
        return (cur + prev) / 2.0, "moyenne"

    for i, e in enumerate(exercices):
        # Par action, MAINTENANT que le nombre d'actions est recousu. C'est la
        # seule base honnête pour une croissance : une société qui double son
        # chiffre d'affaires en doublant son nombre d'actions n'a rien créé pour
        # l'actionnaire. C'est aussi la base du barème qu'on veut reproduire.
        sh = e.get("shares_diluted")
        if sh and sh > 0:
            e["ca_par_action"] = _r(_div(e["revenue"], sh), 4)
            e["fcf_par_action"] = _r(_div(e["fcf"], sh), 4)
            e["ocf_par_action"] = _r(_div(e["ocf"], sh), 4)
        else:
            e["ca_par_action"] = e["fcf_par_action"] = e["ocf_par_action"] = None

        cp_moy, base = _moy("equity", i)
        e["_base_capital"] = base
        e["roe"] = _pct(e["net_income"], cp_moy) if (cp_moy and cp_moy > 0) else None
        act_moy, _ = _moy("assets", i)
        e["roa"] = _pct(e["net_income"], act_moy) if (act_moy and act_moy > 0) else None
        ci_moy, _ = _moy("_capital_investi", i)
        e["roic"] = _pct(e["nopat"], ci_moy) if (ci_moy and ci_moy > 0) else None
        ce_moy, _ = _moy("_capitaux_employes", i)
        e["roce"] = _pct(e["operating_income"], ce_moy) if (ce_moy and ce_moy > 0) else None

    # ROIIC — rendement du capital NOUVELLEMENT investi. Il répond à la question
    # que le ROIC élude : la croissance récente crée-t-elle autant de valeur que
    # l'existant ? Variation du résultat d'exploitation après impôt sur variation
    # du capital investi. Ici les VARIATIONS sont l'objet même du calcul : on
    # prend donc les clôtures, pas les moyennes.
    for i in range(1, len(exercices)):
        a, b = exercices[i - 1], exercices[i]
        d_nopat = (b["nopat"] - a["nopat"]) if (a.get("nopat") is not None and b.get("nopat") is not None) else None
        ci_a, ci_b = a.get("_capital_investi"), b.get("_capital_investi")
        d_ci = (ci_b - ci_a) if (ci_a is not None and ci_b is not None) else None
        b["roiic"] = _pct(d_nopat, d_ci) if (d_ci and abs(d_ci) > 0) else None
    if exercices:
        exercices[0]["roiic"] = None

    # ── Piotroski et Altman sur le dernier exercice ──
    piotroski = piotroski_detail = None
    altman = altman_detail = None
    if len(exercices) >= 2:
        piotroski, piotroski_detail = _piotroski(exercices[-1], exercices[-2])
    if exercices:
        altman, altman_detail = _altman_z(exercices[-1], mcap_usd)

    # ── Croissances par action ──
    def par_annee(cle):
        return [(e["annee"], e.get(cle)) for e in exercices]

    croissances = {
        "ca": _croissances(par_annee("ca_par_action")),
        "eps": _croissances(par_annee("eps_diluted")),
        "fcf": _croissances(par_annee("fcf_par_action")),
        "ocf": _croissances(par_annee("ocf_par_action")),
        "div": _croissances(par_annee("dps")),
    }

    def med(cle, n):
        return _mediane([e.get(cle) for e in exercices[-n:]])

    dernier = exercices[-1]
    resume = {
        "n_exercices": len(exercices),
        "premier": exercices[0]["annee"],
        "dernier": dernier["annee"],
        "fin_exercice": dernier["fin"],
        "accn": dernier["accn"],
        "depose_le": dernier["depose_le"],

        "roic_1a": dernier.get("roic"), "roic_5a": med("roic", 5), "roic_10a": med("roic", 10),
        "roce_1a": dernier.get("roce"), "roce_5a": med("roce", 5), "roce_10a": med("roce", 10),
        "roe_1a": dernier.get("roe"), "roe_5a": med("roe", 5), "roe_10a": med("roe", 10),
        "roiic_1a": dernier.get("roiic"), "roiic_5a": med("roiic", 5), "roiic_10a": med("roiic", 10),

        "marge_brute": dernier.get("marge_brute"),
        "marge_ope": dernier.get("marge_ope"),
        "marge_nette": dernier.get("marge_nette"),
        "marge_fcf": dernier.get("marge_fcf"),
        "capex_ca": dernier.get("capex_ca"),
        "capex_ocf": dernier.get("capex_ocf"),
        "rd_ocf": dernier.get("rd_ocf"),
        "sbc_fcf": dernier.get("sbc_fcf"),

        "croissances": croissances,
        "predictibilite": _predictibilite(par_annee("revenue")),
        "annees_hausse_dividende": _serie_hausses_dividende(par_annee("dps")),

        "dette_ebitda": dernier.get("dette_ebitda"),
        "dette_ebitda_brut": dernier.get("dette_ebitda_brut"),
        "couverture_interets": dernier.get("couverture_interets"),
        "goodwill_actifs": dernier.get("goodwill_actifs"),
        "payout_benefices": dernier.get("payout_benefices"),
        "payout_benefices_10a": med("payout_benefices", 10),
        "payout_fcf": dernier.get("payout_fcf"),
        "piotroski": piotroski,
        "piotroski_detail": piotroski_detail,
        "altman_z": altman,
        "altman_detail": altman_detail,

        "verse_dividende": bool(dernier.get("dps") or dernier.get("dividends_paid")),
        # Les divisions détectées sont RENDUES, pas seulement appliquées : une
        # correction muette est une correction qu'on ne peut pas contester.
        "divisions_action": divisions,
    }
    return {"exercices": exercices, "resume": resume}


# ─────────────────────────────────────────────────────────────────────────
# Univers et correspondance ticker → CIK
# ─────────────────────────────────────────────────────────────────────────
def charger_univers():
    """Les symboles suivis par le tracker qui ont une chance d'être déposants SEC.

    Un ticker sans suffixe de place est américain dans cette nomenclature
    (« NVDA » contre « MC.PA »). Les ADR y figurent aussi : leur émetteur dépose
    un 20-F, qui entre dans le même XBRL.
    """
    if not TRACKER_CACHE.exists():
        print(f"[fatal] {TRACKER_CACHE} absent — lancer fetch_tradfi.py d'abord", file=sys.stderr)
        return {}
    with TRACKER_CACHE.open(encoding="utf-8") as f:
        tc = json.load(f)
    univers = {}
    for n in tc.get("narratives", []):
        for t in n.get("tokens", []):
            s = t.get("symbol")
            if s and "." not in s and s not in univers:
                univers[s] = {
                    "nom": t.get("name"),
                    "mcap": t.get("mcap"),
                    "secteur_suivi": n.get("narrative"),
                }
    return univers


def charger_cik():
    d = _get("https://www.sec.gov/files/company_tickers.json")
    out = {}
    for v in (d or {}).values():
        t = (v.get("ticker") or "").upper()
        if t:
            out[t] = str(v.get("cik_str")).zfill(10)
    return out


def _initiale(sym):
    """Le paquet où ranger une société. Un ticker commençant par un chiffre va
    dans « 0 » : trente-six paquets suffisent, et le nom de fichier reste sûr
    pour le filtre de la fonction qui les sert."""
    c = (sym or "?")[0].upper()
    return c if "A" <= c <= "Z" else "0"


def _options(argv):
    """--tickers NVDA,AAPL    n'en traiter que ceux-là (mise au point, contrôle)
       --limit 20             s'arrêter après N sociétés
       --sortie <dossier>     écrire ailleurs que dans le cache partagé

    `--sortie` existe pour une raison précise : le cache est synchronisé en
    continu avec l'autre machine. Un essai lancé sur le PC écrirait chez elle.
    """
    o = {"tickers": None, "limit": None, "sortie": None}
    for i, a in enumerate(argv):
        if a == "--tickers" and i + 1 < len(argv):
            o["tickers"] = {t.strip().upper() for t in argv[i + 1].split(",") if t.strip()}
        elif a == "--limit" and i + 1 < len(argv):
            o["limit"] = int(argv[i + 1])
        elif a == "--sortie" and i + 1 < len(argv):
            o["sortie"] = Path(argv[i + 1]).expanduser()
    return o


def main():
    global OUT_JSON, OUT_JS, OUT_DIR
    t0 = time.time()
    opts = _options(sys.argv[1:])
    if opts["sortie"]:
        opts["sortie"].mkdir(parents=True, exist_ok=True)
        OUT_JSON = opts["sortie"] / "sec_fundamentals_index.json"
        OUT_JS = opts["sortie"] / "sec_fundamentals_index.js"
        OUT_DIR = opts["sortie"]
        print(f"[info] sortie détournée vers {opts['sortie']}")

    univers = charger_univers()
    if not univers:
        return 1
    if opts["tickers"]:
        univers = {k: v for k, v in univers.items() if k.upper() in opts["tickers"]}
        print(f"[info] restreint à {len(univers)} symbole(s) demandé(s)")
    print(f"[info] univers : {len(univers)} symboles sans suffixe de place")

    cik_par_ticker = charger_cik()
    print(f"[info] correspondance SEC : {len(cik_par_ticker)} tickers")

    index = {}
    paquets = {}
    ok = sans_cik = echecs = 0
    for i, (sym, meta) in enumerate(sorted(univers.items()), 1):
        cik = cik_par_ticker.get(sym.upper())
        if not cik:
            sans_cik += 1
            continue
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        try:
            facts_doc = _get(url, accept_404=True)
        except Exception as e:
            print(f"[warn] {sym} : {e}", file=sys.stderr)
            echecs += 1
            continue
        if not facts_doc or not facts_doc.get("facts"):
            echecs += 1
            continue
        try:
            bati = construire(facts_doc["facts"], meta.get("mcap"))
        except Exception as e:
            print(f"[warn] {sym} : construction impossible : {e}", file=sys.stderr)
            echecs += 1
            continue
        if not bati:
            echecs += 1
            continue

        # Le détail est REGROUPÉ PAR INITIALE, pas écrit un fichier par société.
        # Deux raisons, l'une technique et l'autre humaine :
        #   · la publication du dépôt de collecte marche sur une liste EXACTE de
        #     fichiers ; trois cent cinquante entrées y seraient ingérables ;
        #   · la fonction qui sert les données n'accepte pas de sous-dossier —
        #     son filtre de sécurité refuse tout nom contenant une barre oblique.
        # Trente-six paquets d'environ 400 Ko, chargés au clic. La localité est
        # bonne : ouvrir NVDA charge aussi NFLX, NKE et NOW, les clics suivants
        # probables. Le tout reste très en dessous des caches déjà servis par le
        # site (743 Ko pour le seul tracker).
        detail = {
            "symbole": sym,
            "cik": cik,
            "nom_sec": facts_doc.get("entityName"),
            "nom": meta.get("nom"),
            "source": "SEC EDGAR — API XBRL companyfacts (10-K / 20-F / 40-F)",
            "exercices": bati["exercices"],
            "resume": bati["resume"],
        }
        paquets.setdefault(_initiale(sym), {})[sym] = detail

        r = dict(bati["resume"])
        r.pop("piotroski_detail", None)
        r.pop("altman_detail", None)
        r["cik"] = cik
        r["nom_sec"] = facts_doc.get("entityName")
        index[sym] = r
        ok += 1
        if i % 50 == 0:
            print(f"[info] {i}/{len(univers)} — {ok} société(s) construites")
        if opts["limit"] and ok >= opts["limit"]:
            print(f"[info] arrêt demandé après {ok} sociétés")
            break

    charge = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": "SEC EDGAR — API XBRL companyfacts, données telles que déposées",
        "duree_s": round(time.time() - t0, 1),
        "exhaustivite": {
            "univers_sans_suffixe": len(univers),
            "avec_cik": len(univers) - sans_cik,
            "construites": ok,
            "sans_cik": sans_cik,
            "echecs": echecs,
        },
        "limites": [
            "Déposants SEC uniquement : sociétés américaines (10-K) et émetteurs étrangers cotés aux États-Unis (20-F/40-F).",
            "L'XBRL commence vers 2009 ; les exercices antérieurs n'existent pas dans cette source.",
            "Banques, assurances et foncières ne publient pas les mêmes lignes : marge brute et %CAPEX y sont souvent vides.",
        ],
        "paquets": sorted(paquets.keys()),
        "societes": index,
    }
    with OUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(charge, f, ensure_ascii=False, indent=1)
    with OUT_JS.open("w", encoding="utf-8") as f:
        f.write("window.__SEC_FUNDA__ = " + json.dumps(charge, ensure_ascii=False, separators=(",", ":")) + ";\n")

    horodatage = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    poids = []
    for lettre, contenu in sorted(paquets.items()):
        chemin = OUT_DIR / f"sec_detail_{lettre}.json"
        with chemin.open("w", encoding="utf-8") as f:
            json.dump({"genere_le": horodatage, "societes": contenu},
                      f, ensure_ascii=False, separators=(",", ":"))
        poids.append(chemin.stat().st_size)

    print(f"[ok] {ok} sociétés — {sans_cik} sans CIK, {echecs} échecs — "
          f"{round(time.time() - t0, 1)} s")
    print(f"[ok] index : {OUT_JSON.stat().st_size // 1024} Ko")
    if poids:
        print(f"[ok] {len(poids)} paquet(s) de détail — "
              f"plus gros {max(poids) // 1024} Ko, total {sum(poids) // 1024} Ko")
    return 0


if __name__ == "__main__":
    sys.exit(main())
