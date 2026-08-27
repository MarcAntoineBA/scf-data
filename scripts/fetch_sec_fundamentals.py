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
    "opex": ["OperatingExpenses", "CostsAndExpenses", "OperatingCostsAndExpenses"],
    "autres_non_ope": ["OtherNonoperatingIncomeExpense", "NonoperatingIncomeExpense"],
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
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfOrdinaryDividends",
                       "PaymentsOfDividends"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "dna": ["DepreciationDepletionAndAmortization",
            "DepreciationAmortizationAndAccretionNet",
            "DepreciationAndAmortization", "Depreciation"],
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


# ─────────────────────────────────────────────────────────────────────────
# Le calcul commun vit dans `fondamentaux_communs` — voir l'en-tête de ce
# module pour la raison. Ce fichier ne garde que ce qui touche à la SEC.
# ─────────────────────────────────────────────────────────────────────────
from fondamentaux_communs import (          # noqa: E402
    _div, _pct, _r,
    _BAREME, _CRITERES_INDUSTRIELS, _noter_critere, note_quantitative,
    _croissance_annuelle, _mediane, _croissances, _predictibilite,
    _serie_sans_baisse_dividende, _serie_hausses_dividende,
    _FACTEURS_USUELS, _facteur_division, _corriger_divisions,
    _piotroski, _altman_z,
    TAUX_SANS_RISQUE, PRIME_DE_RISQUE, _wacc,
)



def charger_cours():
    """{symbole: [(horodatage_s, cours), …]} — l'historique déjà collecté.

    Sert à reconstituer la capitalisation de chaque exercice passé, seule façon
    d'avoir un WACC historique plutôt qu'un seul point. Le fichier pèse une
    dizaine de mégaoctets ; il est lu une fois, côté serveur, jamais servi au
    navigateur.
    """
    for nom in ("tradfi_history_cache.json", "tradfi_histories_cache.json"):
        f = CACHE_DIR / nom
        if not f.exists():
            continue
        try:
            with f.open(encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception as e:
            print("[warn] %s illisible : %s" % (nom, e), file=sys.stderr)
            continue
        h = d.get("histories") if isinstance(d, dict) else None
        if isinstance(h, dict):
            return h
        if isinstance(d, dict) and d and isinstance(next(iter(d.values())), list):
            return d
    print("[warn] aucun historique de cours — le WACC restera vide", file=sys.stderr)
    return {}


def _dernier_cours(serie):
    """Le dernier cours de la série, en devise de COTATION, avec sa date.

    La fiche affiche un cours converti en dollars ; les états d'une société
    européenne sont en euros. Un prix juste calculé sur un BPA en euros puis
    comparé à un cours en dollars ne mesure pas une décote, il mesure un taux
    de change. D'où ce point de comparaison, dans la devise des états — quand
    les deux devises coïncident, ce que le collecteur vérifie par ailleurs.
    """
    if not serie:
        return None, None
    px = quand = None
    for p in serie:
        try:
            t, c = p[0], p[1]
        except Exception:
            continue
        if c is None:
            continue
        if t > 1e11:
            t = t / 1000.0
        if quand is None or t > quand:
            quand, px = t, c
    if px is None:
        return None, None
    return px, datetime.fromtimestamp(quand, timezone.utc).strftime("%Y-%m-%d")


def _cours_au(serie, fin_iso):
    """Le cours de clôture le plus proche d'une date d'arrêté, à 45 jours près.

    Au-delà de quarante-cinq jours, on rend None : un cours vieux d'un trimestre
    associé à un bilan produirait une capitalisation, donc un WACC, qui ne
    correspond à aucun moment réel.
    """
    if not serie:
        return None
    try:
        cible = datetime.fromisoformat(fin_iso).replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None
    best, ecart = None, None
    for p in serie:
        try:
            t, c = p[0], p[1]
        except Exception:
            continue
        if t > 1e11:          # certaines séries sont en millisecondes
            t = t / 1000.0
        e = abs(t - cible)
        if ecart is None or e < ecart:
            ecart, best = e, c
    if ecart is None or ecart > 45 * 86400:
        return None
    return best


# ─────────────────────────────────────────────────────────────────────────
# Construction de la série annuelle d'une société
# ─────────────────────────────────────────────────────────────────────────
def construire(facts, mcap_usd=None, beta=None, cours=None):
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

        # ── RECONSTRUIRE LE RÉSULTAT D'EXPLOITATION QUAND IL N'EST PAS DÉPOSÉ ──
        # Beaucoup de sociétés cessent d'étiqueter `OperatingIncomeLoss` : leur
        # compte de résultat publié n'a tout simplement pas de ligne « résultat
        # d'exploitation ». Mesuré sur Johnson & Johnson le 2026-08-27 : la
        # balise s'arrête en 2014, et son absence coûtait SIX critères de la note
        # sur vingt — la marge d'exploitation, les trois ROIC, la dette sur
        # EBITDA et la distribution — par simple cascade. Une société de qualité
        # notée 7/20 pour cause de balise manquante, c'est une note fausse.
        #
        # Trois reconstructions, de la plus sûre à la plus indirecte. On note
        # laquelle a servi : un chiffre reconstruit doit pouvoir être distingué
        # d'un chiffre déposé.
        if e["operating_income"] is None:
            if e["gross_profit"] is not None and e["opex"] is not None:
                e["operating_income"] = e["gross_profit"] - e["opex"]
                e["_ope_source"] = "brut moins charges d’exploitation"
            elif e["gross_profit"] is not None and (e["rd"] is not None or e["sga"] is not None):
                e["operating_income"] = e["gross_profit"] - (e["rd"] or 0) - (e["sga"] or 0)
                e["_ope_source"] = "brut moins R&D et frais généraux"
            elif e["pretax"] is not None and e["autres_non_ope"] is not None:
                # Le résultat avant impôt comprend le non-opérationnel : on le
                # retire pour revenir à l'exploitation.
                e["operating_income"] = e["pretax"] - e["autres_non_ope"]
                e["_ope_source"] = "avant impôt moins non opérationnel"
        else:
            e["_ope_source"] = "déposé"


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

        # WACC de l'exercice : capitalisation reconstituée au cours de clôture,
        # dette du bilan, intérêts et impôt réellement payés cette année-là.
        mc = None
        if cours and e.get("shares_diluted"):
            px = _cours_au(cours, e["fin"])
            if px:
                mc = px * e["shares_diluted"]
        if mc is None and i == len(exercices) - 1:
            mc = mcap_usd          # dernier exercice : la capitalisation du jour
        e["mcap_estime"] = round(mc) if mc else None
        e["wacc"] = _wacc(mc, e.get("dette_totale"), e.get("interest_expense"),
                          e.get("taux_impot"), beta)
        # L'écart entre ce que le capital rapporte et ce qu'il coûte : la seule
        # question qui décide si la croissance crée ou détruit de la valeur.
        e["roic_moins_wacc"] = (round(e["roic"] - e["wacc"], 2)
                                if (e.get("roic") is not None and e.get("wacc") is not None) else None)

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

    # ── Les entrées du barème, calculables sur une série TRONQUÉE ────────────
    # Extraites dans leur propre fonction pour une raison précise : elles servent
    # deux fois. Une fois sur la série complète, pour la note d'aujourd'hui ; et
    # une fois par exercice passé, pour la note TELLE QU'ELLE ÉTAIT cette
    # année-là. Le concurrent n'affiche qu'un instantané ; avec dix-neuf
    # exercices en main, « 16/20 en 2019, 11/20 aujourd'hui » est une phrase
    # qu'on peut écrire et lui non.
    def entrees_bareme(exs):
        if not exs:
            return {}
        d = exs[-1]
        pa = lambda cle: [(e["annee"], e.get(cle)) for e in exs]
        m = lambda cle, n: _mediane([e.get(cle) for e in exs[-n:]])
        return {
            "roic_1a": d.get("roic"), "roic_5a": m("roic", 5), "roic_10a": m("roic", 10),
            "marge_brute": d.get("marge_brute"),
            "marge_ope": d.get("marge_ope"),
            "marge_nette": d.get("marge_nette"),
            "capex_ocf": d.get("capex_ocf"),
            "predictibilite": _predictibilite(pa("revenue")),
            "annees_hausse_dividende": _serie_hausses_dividende(pa("dps")),
            "annees_sans_baisse_dividende": _serie_sans_baisse_dividende(pa("dps")),
            "dette_ebitda_brut": d.get("dette_ebitda_brut"),
            "payout_benefices": d.get("payout_benefices"),
            "verse_dividende": bool(d.get("dps") or d.get("dividends_paid")),
            "croissances": {
                "ca":  _croissances(pa("ca_par_action")),
                "fcf": _croissances(pa("fcf_par_action")),
                "div": _croissances(pa("dps")),
            },
        }

    # La note de chaque exercice, en ne connaissant que ce qu'on savait alors.
    # On s'arrête aux dix derniers : au-delà, la série est trop courte pour que
    # les médianes à cinq et dix ans veuillent dire quelque chose, et une note
    # calculée sur trois exercices se compare mal à une note calculée sur dix.
    note_historique = []
    for i in range(len(exercices)):
        if i < 4:
            continue
        n = note_quantitative(entrees_bareme(exercices[:i + 1]))
        note_historique.append({
            "annee": exercices[i]["annee"],
            "note": n["note"],
            "note_ramenee": n["note_ramenee"],
            "criteres_notables": n["criteres_notables"],
        })
    note_historique = note_historique[-12:]

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

        "wacc_1a": dernier.get("wacc"), "wacc_5a": med("wacc", 5), "wacc_10a": med("wacc", 10),
        "roic_moins_wacc": dernier.get("roic_moins_wacc"),
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
        "annees_sans_baisse_dividende": _serie_sans_baisse_dividende(par_annee("dps")),

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

    # La note se calcule EN DERNIER : elle lit le résumé qu'on vient de bâtir.
    resume["note_q"] = note_quantitative(resume)
    resume["note_historique"] = note_historique

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

    # Le bêta vient du cache des fondamentaux, déjà collecté par ailleurs. Sans
    # lui, pas de coût des fonds propres, donc pas de WACC — et on préfère un
    # champ vide à un bêta supposé, qui servirait ensuite à juger si la société
    # crée de la valeur.
    f = CACHE_DIR / "tradfi_fundamentals_cache.json"
    if f.exists():
        try:
            with f.open(encoding="utf-8") as fh:
                tf = json.load(fh)
            n_beta = 0
            for sec in tf.get("sectors", []):
                for st in sec.get("stocks", []):
                    sym = st.get("symbol")
                    if sym in univers and st.get("beta") is not None:
                        univers[sym]["beta"] = st["beta"]
                        n_beta += 1
            print("[info] bêta connu pour %d sociétés" % n_beta)
        except Exception as e:
            print("[warn] bêtas illisibles : %s" % e, file=sys.stderr)
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

    cours = charger_cours()
    if cours:
        print("[info] historique de cours : %d titres" % len(cours))

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
            bati = construire(facts_doc["facts"], meta.get("mcap"),
                              beta=meta.get("beta"), cours=cours.get(sym))
        except Exception as e:
            print(f"[warn] {sym} : construction impossible : {e}", file=sys.stderr)
            echecs += 1
            continue
        if not bati:
            echecs += 1
            continue

        bati["resume"]["cours_natif"], bati["resume"]["cours_natif_le"] = \
            _dernier_cours(cours.get(sym))
        bati["resume"]["devise"] = "USD"

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
        # Le détail des vingt critères et l'historique de la note restent dans le
        # paquet de détail, que la fiche charge de toute façon à l'ouverture.
        # Les laisser ici coûtait 3 Ko par société, soit 2,7 Mo d'index pour une
        # information lue ailleurs. L'index ne garde que de quoi TRIER et FILTRER.
        r.pop("note_historique", None)
        if isinstance(r.get("note_q"), dict):
            r["note_q"] = {k: v for k, v in r["note_q"].items()
                            if k not in ("details", "criteres_muets", "criteres_nuls_par_nature")}

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
