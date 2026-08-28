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


class DelaiGlobalAtteint(Exception):
    """Le délai est écoulé — on écrit ce qu'on a et on s'arrête.

    Une exception ORDINAIRE, et non SystemExit : celle-ci traverse les
    `except Exception` sans être vue, c'est son rôle. Ici on veut au contraire
    être rattrapé par la boucle de collecte, pour que le code d'écriture
    s'exécute avec ce qui a été construit.

    Mesuré le 28/08/2026 : un run de vingt-cinq minutes avait construit six cent
    dix sociétés sur trois mille huit cent cinquante-six, puis `sys.exit(2)` les
    a toutes jetées. La fusion étant inconditionnelle, écrire six cent dix
    laisse un cache MEILLEUR qu'avant — et le passage suivant continue.
    """


def _global_timeout_handler(signum, frame):
    raise DelaiGlobalAtteint()


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
    # ⚠ LES SOCIÉTÉS CHANGENT D'ÉTIQUETTE, ET LA SÉRIE S'ARRÊTE SANS RIEN DIRE.
    #
    # Mesuré le 28/08/2026 à la source : 154 sociétés avaient un dernier exercice
    # plus ancien dans notre paquet SEC que dans le paquet international, dont 84
    # avec au moins trois ans de retard et 53 valant plus d'un milliard de dollars.
    # La fiche d'Agnico Eagle affichait 640 M$ de chiffre d'affaires pour une
    # société qui en fait 11,9 milliards — les chiffres de 2009, seize ans plus
    # tôt, sans que rien à l'écran ne dise leur âge.
    #
    # La cause n'était ni un filtre ni une taxonomie manquante : c'est que ces
    # sociétés ont CESSÉ d'employer l'étiquette qu'on leur demandait.
    #   · Agnico Eagle : `us-gaap:Revenues` s'arrête en 2009 ; elle publie depuis
    #     sous `ifrs-full:Revenue`, qui va jusqu'à 2025 — dix-huit exercices.
    #   · Morgan Stanley : `Revenues` s'arrête en 2014 ; elle publie depuis sous
    #     `RevenuesNetOfInterestExpense`, jusqu'à 2025. Wells Fargo pareil, en 2019.
    #     Une banque ne présente pas un « chiffre d'affaires » mais un produit net
    #     bancaire, et l'étiquette américaine a suivi.
    #
    # On les ajoute donc toutes. La recouture existante fait le reste : elle prend
    # la première étiquette qui renseigne une date, dans l'ordre de cette liste,
    # et les dates que la première ne couvre pas sont servies par les suivantes.
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet",
                # Les banques et courtiers : produit net, intérêts déduits.
                "RevenuesNetOfInterestExpense",
                # Les déposants passés aux normes internationales.
                "Revenue", "RevenueFromContractsWithCustomers"],
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
    # ⚠ `NetIncomeLoss` désigne DÉJÀ la part du groupe en norme américaine ;
    # `ProfitLoss` désigne le TOTAL, minoritaires compris. Les empiler dans la
    # même liste de replis change la définition du mot selon la société.
    #
    # Mesuré sur Freeport-McMoRan, qui ne dépose PAS `NetIncomeLoss` : le
    # collecteur prenait 4 399 M$ (le total) et le divisait par 17 581 M$ de
    # capitaux propres part du groupe, publiant un ROE de 25,67 % là où le vrai
    # est 10,99 %. Quatre-vingt-douze sociétés divergeaient ainsi de l'autre
    # collecteur, jusqu'à 440 %.
    #
    # La part du groupe se RECONSTRUIT plus bas quand elle manque : total moins
    # minoritaires. Une soustraction, pas une substitution.
    "net_income": ["NetIncomeLoss"],
    "net_income_total": ["ProfitLoss"],
    "interets_minoritaires_resultat": [
        "NetIncomeLossAttributableToNoncontrollingInterest",
        "ProfitLossAttributableToNoncontrollingInterest"],
    "net_income_common": ["NetIncomeLossAvailableToCommonStockholdersBasic"],
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
    # `Equity` et `EquityAttributableToOwnersOfParent` sont les noms des normes
    # internationales. Sans eux, les déposants passés aux IFRS — Agnico Eagle,
    # Vale, Petrobras, Alcon — rendaient des capitaux propres vides, et avec eux
    # le rendement des capitaux, la valeur comptable par action, l'Altman Z et le
    # coût du capital. Un poste manquant en emporte six.
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
               "EquityAttributableToOwnersOfParent", "Equity"],
    # ── Les deux postes qui manquaient au bilan, et qui l'empêchaient de
    #    s'équilibrer ──
    # Mesuré le 28/08/2026 : sur 27 732 bilans américains complets, 19,3 %
    # violaient l'identité « actif = passif + capitaux propres ». Ce n'était pas
    # une donnée fausse, c'était un bilan INCOMPLET — deux lignes réelles
    # n'étaient collectées ni d'un côté ni de l'autre.
    #
    # `equity` désigne la part du GROUPE. Les participations ne donnant pas le
    # contrôle s'ajoutent pour former le total : Apollo Global porte 5,8 Md$ de
    # minoritaires sur 257 Md$ d'actif, et sans eux son bilan manquait de 3,4 %.
    #
    # Les capitaux MEZZANINE — actions privilégiées rachetables, minoritaires
    # rachetables — ne sont ni dette ni capitaux propres permanents : le plan
    # comptable américain leur donne une ligne à part, entre les deux. Red Cat
    # Holdings en portait exactement 1 500 004 $, soit tout l'écart de son bilan.
    "interets_minoritaires_bilan": ["MinorityInterest"],
    "capitaux_mezzanine": [
        "TemporaryEquityCarryingAmountAttributableToParent",
        "TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests",
        "RedeemableNoncontrollingInterestEquityCarryingAmount",
        "RedeemableNoncontrollingInterestEquityFairValue",
    ],
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
            # Deux postes de BILAN, donc pris à une date et non sur une période :
            # les demander comme des flux ne rendrait rien du tout.
            "interets_minoritaires_bilan", "capitaux_mezzanine",
            "goodwill", "retained_earnings", "inventory"}

FORMES_ANNUELLES = ("10-K", "20-F", "40-F")


# Les devises rencontrées pendant la construction d'UNE société. Remise à zéro
# par `construire`, lue à la fin pour écrire `resume["devise"]`.
#
# Un accumulateur de module plutôt qu'une valeur de retour : `_annuels` est
# appelée une fois par concept — quarante fois par société — et lui faire rendre
# un couple obligerait à modifier quarante points d'appel pour une information
# qui ne varie pas d'un concept à l'autre.
#
# ⚠ Le collecteur est SÉQUENTIEL (un débit de 0,11 s entre requêtes, pas de fils
# d'exécution). Un accumulateur partagé serait dangereux dans un pool de threads ;
# ici il ne l'est pas, et le collecteur international, lui, ne s'en sert pas.
_DEVISES_VUES = set()


def devise_du_deposant(facts):
    """La devise dans laquelle CE déposant publie ses montants.

    Décidée UNE fois pour toute la société, puis imposée à chaque concept.

    Sans cette décision commune, chaque concept choisissait son unité dans son
    coin : LG Display rendait un chiffre d'affaires en wons et des capitaux
    propres en dollars, dans le même tableau, sans que rien ne le dise. Deux
    nombres justes qui, mis côte à côte, en font un faux — c'est la forme
    d'erreur que ce dépôt a déjà payée plusieurs fois.

    Le dollar l'emporte dès qu'il apparaît sur un poste principal : les déposants
    étrangers publient leurs deux colonnes dans le même document, et c'est celle
    en dollars qui se compare au reste de l'univers.
    """
    # On choisit sur la COUVERTURE DES POSTES, pas sur la simple présence du
    # dollar. Une première version prenait le dollar dès qu'il apparaissait
    # quelque part : LG Display, qui ne le publie que sur un poste, se retrouvait
    # amputée de onze exercices sur douze — la règle censée corriger un défaut en
    # créait un pire. Cinq autres sociétés y perdaient leurs capitaux propres.
    #
    # La bonne question n'est pas « le dollar existe-t-il ? » mais « le dollar
    # porte-t-il AUTANT DE POSTES que la monnaie locale ? ». S'il les porte tous,
    # c'est la colonne comparable et on la prend. Sinon la société ne publie
    # qu'en local, et il faut le dire plutôt que de l'amputer.
    postes = (("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
               "Revenue", "RevenuesNetOfInterestExpense"),
              ("Assets",),
              ("StockholdersEquity", "Equity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
              ("NetIncomeLoss", "ProfitLoss"))
    couverture, points = {}, {}
    for groupe in postes:
        vues_groupe = set()
        for espace in ("us-gaap", "ifrs-full"):
            bloc = facts.get(espace) or {}
            for nom in groupe:
                for u, pts in ((bloc.get(nom) or {}).get("units") or {}).items():
                    if u.isalpha() and len(u) == 3:
                        vues_groupe.add(u)
                        points[u] = points.get(u, 0) + len(pts)
        for u in vues_groupe:
            couverture[u] = couverture.get(u, 0) + 1
    if not couverture:
        return None
    meilleure = max(couverture.values())
    if couverture.get("USD", 0) >= meilleure:
        return "USD"
    # Sinon : la devise qui couvre le plus de postes, départagée par le nombre de
    # points quand deux monnaies en couvrent autant.
    return max(couverture, key=lambda u: (couverture[u], points.get(u, 0)))


def _annuels(facts, concept_noms, instant=False, devise=None):
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
    # ── DEUX TAXONOMIES, PAS UNE ──
    #
    # On ne lisait que `us-gaap`. Or un déposant étranger — formulaire 20-F ou
    # 40-F — publie sous `ifrs-full` dès qu'il passe aux normes internationales,
    # ce que beaucoup ont fait entre 2010 et 2013. Sa série s'arrêtait donc net à
    # la date du basculement, sans erreur, sans case vide : juste un dernier
    # exercice qui cesse de bouger.
    #
    # Mesuré le 28/08/2026 : 154 sociétés avaient un dernier exercice PLUS ANCIEN
    # dans le paquet SEC que dans le paquet international, dont 84 avec au moins
    # trois ans de retard, et 53 valant plus d'un milliard de dollars — 1 568 Md$
    # de capitalisation cumulée. Agnico Eagle s'arrêtait en 2009 : la fiche
    # affichait 640 M$ de chiffre d'affaires pour une société qui en fait 11,9 Md$.
    # Même cas pour Petrobras, Vale, Itaú, Bradesco, Barrick, Alcon, LG Display.
    #
    # `us-gaap` reste PRIORITAIRE : quand les deux taxonomies portent le même
    # concept, c'est la version américaine qui fait foi pour un déposant
    # américain — mais on ne CHOISIT pas entre les deux : on les RECOUD, exactement
    # comme on recoud les étiquettes.
    #
    # Une première version écrivait `us.get(nom) or ifrs.get(nom)`. C'était le même
    # défaut d'un cran plus loin : Agnico Eagle porte `Assets` dans les DEUX
    # taxonomies — jusqu'à 2009 sous `us-gaap`, jusqu'à 2025 sous `ifrs-full` — et
    # le `or` retenait la première, donc la périmée. L'actif restait vide de 2010
    # à 2025 pour six sociétés, et avec lui le rendement de l'actif, le ratio
    # d'écarts d'acquisition et l'équilibre du bilan.
    us = facts.get("us-gaap") or {}
    ifrs = facts.get("ifrs-full") or {}
    out = {}          # fin -> (valeur, accn, depose, rang_du_concept)
    # Chaque (taxonomie, nom) est une source distincte. L'ordre place `us-gaap`
    # avant `ifrs-full` à rang égal : pour une date que les deux couvrent, la
    # version américaine fait foi chez un déposant américain.
    sources = []
    for rang, nom in enumerate(concept_noms):
        if nom in us:
            sources.append((rang * 2, us[nom]))
        if nom in ifrs:
            sources.append((rang * 2 + 1, ifrs[nom]))
    for rang, bloc in sources:
        unites = bloc.get("units") or {}
        # ── LE CHOIX DE L'UNITÉ, ET POURQUOI IL NE PEUT PAS ÊTRE « LA PLUS PEUPLÉE » ──
        #
        # Cette ligne prenait l'unité la plus peuplée, en supposant que les
        # autres seraient « des doublons en devise étrangère ». C'est l'inverse
        # pour un déposant ÉTRANGER : sa devise de publication est l'unité la
        # plus fournie, et le dollar la minoritaire.
        #
        # Mesuré le 28/08/2026 à la source (companyfacts de Nebius, CIK 1513845,
        # même dépôt 20-F) : Revenues {RUB: 39 valeurs, USD: 19}. Le max() prenait
        # donc les roubles — 800 125 000 000 — et la fiche affichait 800 milliards
        # de dollars de chiffre d'affaires pour une société qui en fait neuf. Huit
        # sociétés confirmées dans le même cas : Nebius, VinFast (dongs), JD.com,
        # NIO, JinkoSolar, HUYA, Gaotu, ECARX.
        #
        # On prend donc le DOLLAR quand il existe. Ces déposants publient leurs
        # deux colonnes dans le même document : le jumeau en dollars est là, à
        # côté, et c'est celui qui se compare au reste de l'univers.
        #
        # ⚠ On ne se contente pas de préférer : on RETIENT la devise choisie, pour
        # que l'appelant sache ce qu'il manipule. Elle était écrite « USD » en
        # dur plus bas, ce qui rendait l'anomalie invisible en aval.
        if not unites:
            continue
        monetaires = [u for u in unites if u.isalpha() and len(u) == 3]
        if monetaires:
            # La devise de la société, décidée une fois pour toutes. Si ce
            # concept ne la porte pas, on le LAISSE VIDE plutôt que de prendre
            # une autre monnaie : une case vide se voit, un mélange non.
            if devise and devise in unites:
                cle_unite = devise
            elif devise:
                continue
            else:
                cle_unite = max(monetaires, key=lambda u: len(unites[u]))
            _DEVISES_VUES.add(cle_unite)
        else:
            # Ni devise à trois lettres : des actions, des taux, des unités par
            # action. La plus peuplée reste le bon choix.
            cle_unite = max(unites, key=lambda u: len(unites[u]))
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
    annee_exercice,
    dedupliquer_exercices,
    _div, _pct, _r,
    _BAREME, _CRITERES_INDUSTRIELS, _noter_critere, note_quantitative,
    _croissance_annuelle, _mediane, _mediane_fenetre, _croissances, _predictibilite,
    _serie_sans_baisse_dividende, _serie_hausses_dividende,
    _FACTEURS_USUELS, _facteur_division, _corriger_divisions,
    _piotroski, _altman_z,
    effacer_l_impossible,
    TAUX_SANS_RISQUE, PRIME_DE_RISQUE, _wacc,
    _taux_impot_reel, _taux_pour_nopat, _charge, _corriger_unite_actions,
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


# ── Les entrées du barème, calculables sur une série TRONQUÉE ────────────
# Elles servent DEUX fois : une fois sur la série complète, pour la note
# d'aujourd'hui ; et une fois par exercice passé, pour la note TELLE QU'ELLE
# ÉTAIT cette année-là. Le concurrent n'affiche qu'un instantané ; avec vingt et
# un exercices en main, « 16/20 en 2011, 11/20 aujourd'hui » est une phrase qu'on
# peut écrire et lui non.
#
# Au NIVEAU MODULE et non plus imbriquée dans `construire` : elle ne dépend que
# d'auxiliaires de module, et la sortir permet de RECALCULER l'historique de la
# note depuis les exercices déjà en cache, sans réinterroger la SEC. Sans cela,
# lever le plafond de douze aurait imposé une collecte complète pour une valeur
# qu'on détenait déjà.
def entrees_bareme(exs):
    if not exs:
        return {}
    d = exs[-1]
    pa = lambda cle: [(e["annee"], e.get(cle)) for e in exs]
    m = lambda cle, n: _mediane_fenetre([e.get(cle) for e in exs[-n:]], n)
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


def historique_note(exercices):
    """La note de chaque exercice, en ne connaissant que ce qu'on savait alors.

    Aucun plafond : une société à vingt et un exercices produit dix-sept notes,
    et les dix-sept sont rendues. Chacune porte le nombre d'exercices dont son
    calcul disposait, pour que la fiche puisse signaler les plus courtes au lieu
    de les faire passer pour les autres.
    """
    out = []
    for i in range(len(exercices)):
        if i < 4:
            continue
        n = note_quantitative(entrees_bareme(exercices[:i + 1]))
        out.append({
            "annee": exercices[i]["annee"],
            "note": n["note"],
            "note_ramenee": n["note_ramenee"],
            "criteres_notables": n["criteres_notables"],
            "n_exercices_connus": i + 1,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────
# Construction de la série annuelle d'une société
# ─────────────────────────────────────────────────────────────────────────
def construire(facts, mcap_usd=None, beta=None, cours=None):
    _DEVISES_VUES.clear()
    # La devise se décide AVANT de lire quoi que ce soit, et s'impose ensuite à
    # tous les postes monétaires : c'est la seule façon de garantir qu'un tableau
    # ne mêle pas deux monnaies.
    devise = devise_du_deposant(facts)
    series = {}
    for cle, noms in CONCEPTS.items():
        series[cle] = _annuels(facts, noms, instant=(cle in INSTANTS), devise=devise)

    # ── L'OSSATURE EST UNE UNION, PAS UNE SEULE LIGNE ──
    #
    # Elle était faite du seul chiffre d'affaires, avec repli sur le résultat net
    # quand il manquait ENTIÈREMENT. Mesuré le 28/08/2026, c'est faux dès qu'une
    # ligne APPARAÎT en cours de route — et c'est silencieux. Investar Holding ne
    # publie son produit net bancaire qu'à partir de 2019 : ses exercices 2012 à
    # 2018, dont le résultat net, l'actif et les capitaux propres sont pourtant
    # tous déposés, étaient jetés. Quatorze exercices tombaient à sept. Vingt-
    # quatre sociétés dans ce cas, dont Goldman Sachs (19 → 15) et NetEase.
    #
    # L'ossature est donc l'union des dates d'arrêté des trois flux principaux.
    # Un exercice sans chiffre d'affaires vaut mieux qu'un exercice absent : la
    # case vide se voit, l'année manquante non.
    axe = sorted(set(series["revenue"])
                 | set(series["net_income"])
                 | set(series["ocf"]))
    if not axe:
        return None

    exercices = []
    for fin in axe:
        e = {"fin": fin, "annee": annee_exercice(fin)}
        for cle in CONCEPTS:
            e[cle] = _val(series[cle], fin)

        # Reconstructions quand la ligne n'est pas déposée telle quelle.
        if e["gross_profit"] is None and e["revenue"] is not None and e["cogs"] is not None:
            e["gross_profit"] = e["revenue"] - e["cogs"]
        if e["sga"] is None and (e["ga"] is not None or e["sm"] is not None):
            e["sga"] = (e["ga"] or 0) + (e["sm"] or 0)
            # ── La part du groupe, reconstruite quand elle n'est pas déposée ──
        # Freeport ne dépose pas `NetIncomeLoss` : il publie le total puis les
        # minoritaires séparément. Prendre le total reviendrait à attribuer aux
        # actionnaires un bénéfice qui ne leur revient pas — et c'est ce que
        # faisait le repli muet vers `ProfitLoss`. Mesuré : ROE publié 25,67 %
        # là où le vrai est 10,99 %.
        if e.get("net_income") is None:
            tot = e.get("net_income_total")
            mino = e.get("interets_minoritaires_resultat")
            if tot is not None:
                e["net_income"] = tot - (mino or 0.0)
                e["net_income_reconstruit"] = True

        if e["pretax"] is None and e["net_income"] is not None and e["tax"] is not None:
            e["pretax"] = e["net_income"] + e["tax"]

        # ── LE TOTAL DES DETTES SE DÉDUIT, IL NE SE DEVINE PAS ──
        # Beaucoup de sociétés n'étiquettent jamais `Liabilities` : leur bilan
        # publié détaille les postes sans en donner le total. Mesuré le
        # 28/08/2026 : 453 sociétés américaines sur 3 195 — 14,2 % — sortaient
        # sans total des dettes, dont 437 dont l'actif ET les capitaux propres
        # étaient pourtant déposés.
        #
        # Le coût était silencieux et lourd : le terme « capitalisation sur
        # dettes » du Z d'Altman disparaissait, et le Z était quand même publié,
        # amputé d'un cinquième de sa formule, puis comparé aux seuils d'Altman
        # comme s'il était complet. Illumina s'affichait à 1,28 — « zone de
        # détresse » — pour un Z réel autour de 2,7.
        #
        # Ce n'est pas une estimation : actif = dettes + capitaux propres +
        # intérêts minoritaires + capitaux mezzanine est une identité comptable.
        # On la retourne, et on marque le chiffre comme reconstruit — un montant
        # déduit doit rester distinguable d'un montant déposé.
        if (e.get("liabilities") is None and e.get("assets") is not None
                and e.get("equity") is not None):
            e["liabilities"] = (e["assets"] - e["equity"]
                                - (e.get("interets_minoritaires_bilan") or 0.0)
                                - (e.get("capitaux_mezzanine") or 0.0))
            e["liabilities_reconstruit"] = True

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
        # DEUX taux, et c'est le fond du correctif. Celui qu'on AFFICHE est le
        # taux réellement payé, sans borne et vide quand il n'a pas de sens.
        # Celui qui CALCULE le résultat après impôt est borné, faute de quoi un
        # crédit d'impôt exceptionnel produirait un résultat après impôt
        # supérieur au résultat avant impôt. Les confondre revenait à publier
        # 21 % pour des sociétés qui ne l'ont jamais payé — un cinquième de
        # l'univers américain, un septième de l'international.
        e["taux_impot"] = _taux_impot_reel(e["tax"], e["pretax"])
        taux, borne = _taux_pour_nopat(e["tax"], e["pretax"])
        e["_taux_nopat"] = round(taux * 100, 1)
        e["_taux_nopat_borne"] = borne
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

    # Une seule entrée par année, AVANT tout calcul. Une société qui change de
    # date de clôture publie une période de transition qui chevauche l'exercice
    # suivant ; deux entrées de même millésime feraient mentir toutes les
    # fenêtres glissantes qui suivent. Voir `dedupliquer_exercices`.
    exercices = dedupliquer_exercices(exercices)

    # Deux redressements AVANT tout calcul, dans cet ordre.
    #
    # Le signe des charges d'intérêts d'abord : les deux sources ne s'accordent
    # pas, et une garde du type `interest_expense > 0` vide alors la couverture
    # des intérêts pour quatre cinquièmes d'un univers sans rien signaler.
    for _e in exercices:
        _e["interest_expense"] = _charge(_e.get("interest_expense"))

    # D'abord ce qui est logiquement impossible — un résultat brut au-dessus du
    # chiffre d'affaires, un poste au-dessus de son propre total. Avant tout
    # calcul : une marge tirée d'un couple impossible est fausse, et elle n'a
    # plus l'air de rien une fois arrondie à deux décimales.
    impossibles = effacer_l_impossible(exercices)

    # L'unité du nombre d'actions ensuite : McDonald's portait 716,4 actions là
    # où il en faut 716,4 millions. Ce facteur traverse la capitalisation, la
    # valeur d'entreprise, le coût du capital et toutes les grandeurs par
    # action — le corriger après serait le corriger nulle part.
    unites_actions = _corriger_unite_actions(exercices)

    # Les divisions d'action enfin : tout ce qui suit se calcule « par action »
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
                          e.get("_taux_nopat"), beta)
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
        return _mediane_fenetre([e.get(cle) for e in exercices[-n:]], n)

    # La note de chaque exercice, en ne connaissant que ce qu'on savait alors.
    #
    # LE PLAFOND À DOUZE A ÉTÉ RETIRÉ le 28/08/2026. Il jetait jusqu'à cinq
    # notes déjà calculées : une société à vingt et un exercices en produit
    # dix-sept, et n'en gardait que douze. Or la note dans le temps est
    # précisément ce que le concurrent ne montre pas — il n'affiche qu'un
    # instantané — et la tronquer nous privait de notre seul avantage sur ce
    # point.
    #
    # La justification d'origine reste vraie sur le fond : une note calculée sur
    # cinq exercices ne se compare pas à une note calculée sur quinze, parce que
    # les médianes à cinq et dix ans y sont bornées par la longueur de la série.
    # Mais la réponse juste n'est pas de CACHER ces notes : c'est de dire sur
    # combien d'exercices chacune repose, et de laisser la fiche le montrer.
    # D'où `n_exercices_connus`, qui accompagne désormais chaque point.
    note_historique = historique_note(exercices)

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
        "unites_actions_corrigees": unites_actions,
    }

    # La note se calcule EN DERNIER : elle lit le résumé qu'on vient de bâtir.
    resume["note_q"] = note_quantitative(resume)
    resume["note_historique"] = note_historique

    # ── LA DEVISE RÉELLEMENT LUE, et non « USD » par principe ──
    # Un déposant étranger publie ses montants dans sa monnaie ET en dollars, dans
    # le même document. On préfère le dollar, mais quand il manque, il faut le
    # DIRE : Nebius publiait 800 125 000 000 roubles, que la fiche présentait comme
    # 800 milliards de dollars — plus qu'Apple et Microsoft réunis.
    devises = {d for d in _DEVISES_VUES if d and d != "USD"}
    if "USD" in _DEVISES_VUES or not devises:
        resume["devise"] = "USD"
    else:
        # Une seule devise étrangère : c'est celle des états. Plusieurs : on ne
        # tranche pas, on nomme la mieux fournie et on signale l'ambiguïté.
        resume["devise"] = sorted(devises)[0]
        resume["devise_deduite"] = True
        if len(devises) > 1:
            resume["devises_multiples"] = sorted(devises)

        # ── ÉTATS EN MONNAIE LOCALE, COTATION EN DOLLARS : ON NE MULTIPLIE PAS ──
        #
        # La capitalisation et le cours viennent de la cotation américaine, donc
        # en dollars ; les états, eux, sont en wons, en pesos ou en réaux. Tout
        # ratio qui croise les deux est faux d'un facteur mille — c'est
        # exactement le défaut qui avait mis Toyota à un P/E de 0,1, et il a déjà
        # coûté une soirée à ce dépôt.
        #
        # On pourrait convertir : le cache de change existe. On ne le fait PAS,
        # et c'est délibéré — mesuré le 28/08/2026, sa dernière cotation date du
        # 28 avril, quatre mois plus tôt, et il ne couvre pas le peso colombien.
        # Une conversion à un taux périmé serait une fausse précision, plus
        # dangereuse qu'une case vide parce qu'elle a l'air d'un chiffre.
        #
        # On efface donc les grandeurs de marché de ces exercices et on déclare
        # la divergence. La fiche affiche alors « états en KRW mais cotation en
        # USD » — le mécanisme existe déjà côté international — et le lecteur
        # sait pourquoi les multiples manquent au lieu de lire des multiples faux.
        resume["devise_cotation"] = "USD"
        resume["devises_alignees"] = False
        for e in exercices:
            e["mcap_estime"] = None
            e["wacc"] = None
            e["ecart_roic_wacc"] = None
        resume["cours_natif"] = None
        resume["cours_natif_le"] = None

    return {"exercices": exercices, "resume": resume}


# ─────────────────────────────────────────────────────────────────────────
# Univers et correspondance ticker → CIK
# ─────────────────────────────────────────────────────────────────────────
def univers_marche(tranche=None):
    """Les cotations américaines PRINCIPALES de la collecte de marché.

    Dans cette nomenclature, un ticker sans suffixe de place est américain
    (« NVDA » contre « MC.PA »). On ne garde que les cotations principales :
    sans ce filtre, Apple apparaîtrait cinq fois, et surtout les cotations
    secondaires portent des capitalisations dans la monnaie de leur place —
    mesuré, 231 lignes affichaient des montants impossibles.

    `tranche` vaut (i, n) : une société sur n, celles dont le PAQUET modulo n
    vaut i.

    ⚠ SUR LE PAQUET, ET NON SUR LE RANG DE CAPITALISATION.
    La découpe se faisait sur le rang, pour que chaque tranche porte un
    échantillon de toutes les tailles. L'intention était bonne, le coût invisible :
    un rang de capitalisation n'a aucun rapport avec l'empreinte qui choisit le
    paquet, donc les cinq cent cinquante sociétés d'une tranche se répartissaient
    sur les CINQ CENT DOUZE paquets. Les cinq cent douze fichiers changeaient donc
    tous les jours.

    Mesuré le 28/08/2026 : dix-sept mégaoctets compressés par jour ajoutés à
    l'historique d'un dépôt git — qui n'en retire jamais rien. Le dépôt pesait
    271 Mo ; il franchissait le gigaoctet en six semaines et les cinq en neuf mois.

    En découpant sur le paquet, la tranche du jour ne touche que soixante-treize
    fichiers au lieu de cinq cent douze : deux mégaoctets et demi par jour, sept
    fois moins. L'échantillon de tailles est préservé — l'empreinte est une
    fonction de hachage du symbole, donc sans corrélation avec la capitalisation ;
    on troque une stratification exacte contre un tirage aléatoire, ce qui ne
    change rien à l'usage et divise le coût par sept.
    """
    import glob as _glob
    f = CACHE_DIR / "univers_actions.json"
    if not f.exists():
        print("[fatal] univers_actions.json absent : impossible de savoir quelle "
              "cotation est la principale.", file=sys.stderr)
        return {}
    with f.open(encoding="utf-8") as fh:
        u = json.load(fh)
    # Les cotations principales, ET LEUR COURS.
    #
    # Le cours ne venait que de `tradfi_history_cache`, qui ne couvre que les
    # quelque huit cents titres du tracker. Mesuré le 28/08/2026 après la
    # collecte complète : sur 3 195 sociétés américaines, **315 seulement**
    # portaient un cours — 9,9 %. Sans lui, pas de P/E, pas de rendement, pas de
    # prix juste : l'onglet Valorisation reste éteint pour neuf sociétés sur dix.
    #
    # `univers_actions.json` porte la cotation de 46 992 titres principaux. Le
    # cas américain est le plus simple qui soit : la société cote en dollars et
    # publie en dollars, donc aucune conversion — contrairement à
    # l'international, où il a fallu écrire `_cotation_vers_etats`.
    #
    # On garde tout de même la devise, et on REFUSE un cours qui ne serait pas
    # en dollars : un certificat étranger coté ailleurs se glisserait sinon dans
    # l'univers, et son cours rencontrerait des états en dollars.
    principales = set()
    cotations = {}
    for t in u.get("titres", []):
        sym = t.get("yahoo") or t.get("sa")
        if sym and t.get("principal"):
            principales.add(sym)
            px = t.get("cours")
            if isinstance(px, (int, float)) and px > 0 \
               and (t.get("devise") or "").upper() == "USD":
                cotations[sym] = px

    lignes = []
    for pth in _glob.glob(str(CACHE_DIR / "marche_[0-9]*.json")):
        try:
            with open(pth, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        ch = d.get("champs") or []
        try:
            i_nom, i_capi = ch.index("name"), ch.index("marketCapUsd")
        except ValueError:
            continue
        i_ind = ch.index("industry") if "industry" in ch else None
        for sym, v in (d.get("societes") or {}).items():
            # Un ticker à suffixe n'est pas américain ; un chemin non plus.
            if "." in sym or "/" in sym or sym not in principales:
                continue
            lignes.append((v[i_capi] or 0, sym, v[i_nom],
                           v[i_ind] if i_ind is not None else None))
    lignes.sort(reverse=True)
    if tranche:
        i, n = tranche
        lignes = [x for x in lignes if int(_initiale(x[1])) % n == i]

    out = {}
    for capi, sym, nom, ind in lignes:
        out[sym] = {"nom": nom, "mcap": capi, "secteur_suivi": ind,
                    "cours_cotation": cotations.get(sym)}

    # Le bêta, comme pour l'univers suivi : sans lui, pas de coût des fonds
    # propres, donc pas de WACC — et on préfère un champ vide à un bêta supposé,
    # qui servirait ensuite à juger si la société crée de la valeur.
    fb = CACHE_DIR / "tradfi_fundamentals_cache.json"
    if fb.exists():
        try:
            with fb.open(encoding="utf-8") as fh:
                tf = json.load(fh)
            n_beta = 0
            for sec in tf.get("sectors", []):
                for st in sec.get("stocks", []):
                    sym = st.get("symbol")
                    if sym in out and st.get("beta") is not None:
                        out[sym]["beta"] = st["beta"]
                        n_beta += 1
            print("[info] bêta connu pour %d sociétés" % n_beta)
        except Exception as e:
            print("[warn] bêtas illisibles : %s" % e, file=sys.stderr)
    return out


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


# Cinq cent douze paquets, comme le jeu international. Le nombre n'est pas
# arbitraire : c'est celui qui donnait des paquets réguliers là-bas, et les deux
# collectes servent la même fiche.
PAQUETS_SEC = 512


def _initiale(sym):
    """Le paquet où ranger une société : une EMPREINTE, pas une initiale.

    Vingt-six lettres suffisaient pour trois cent quinze sociétés — un
    mégaoctet au plus. L'univers passe à trois mille sept cent quatre-vingt-dix,
    et le plus gros paquet par lettre atteindrait quatorze mégaoctets pour
    afficher UNE société.

    Le jeu international a déjà payé cette leçon : un préfixe d'une lettre
    mettait 3,9 Mo dans « A », deux caractères en laissaient 3 dans « 60 »,
    parce que tous les codes de Shanghai commencent par là.

    ⚠ La fiche connaît la MÊME empreinte, dans `paquetDe()`. Elle est primitive
    exprès : une empreinte savante qui divergerait entre Python et JavaScript
    produirait des fiches vides sans le moindre message d'erreur.
    """
    t = (sym or "?").upper()
    h = 0
    for c in t:
        h = (h * 31 + ord(c)) % 4294967296
    return "%03d" % (h % PAQUETS_SEC)


def _options(argv):
    """--tickers NVDA,AAPL    n'en traiter que ceux-là (mise au point, contrôle)
       --limit 20             s'arrêter après N sociétés
       --sortie <dossier>     écrire ailleurs que dans le cache partagé
       --source marche        prendre l'univers dans la collecte de marché
                              (3 790 sociétés avec un CIK) au lieu du tracker
                              des trente-neuf narratifs (315)
       --tranche auto         un septième de l'univers par jour, découpé sur le
                              RANG de capitalisation pour que chaque tranche
                              porte un échantillon de toutes les tailles

    `--sortie` existe pour une raison précise : le cache est synchronisé en
    continu avec l'autre machine. Un essai lancé sur le PC écrirait chez elle.
    """
    o = {"tickers": None, "limit": None, "sortie": None,
         "source": "suivi", "tranche": None}
    for i, a in enumerate(argv):
        if a == "--source" and i + 1 < len(argv):
            o["source"] = argv[i + 1]
        elif a == "--tranche" and i + 1 < len(argv):
            v = argv[i + 1]
            if v == "auto":
                # Le jour de la semaine : lundi 0, dimanche 6. L'univers entier
                # est parcouru en sept jours, sans registre à tenir.
                o["tranche"] = (datetime.now(timezone.utc).weekday(), 7)
            else:
                a2, b2 = v.split("/")
                o["tranche"] = (int(a2), int(b2))
        if a == "--tickers" and i + 1 < len(argv):
            o["tickers"] = {t.strip().upper() for t in argv[i + 1].split(",") if t.strip()}
        elif a == "--limit" and i + 1 < len(argv):
            o["limit"] = int(argv[i + 1])
        elif a == "--sortie" and i + 1 < len(argv):
            o["sortie"] = Path(argv[i + 1]).expanduser()
    return o


def _fusionner_sec(index, paquets):
    """Ajoute ce qu'on vient de collecter à ce qui existe déjà.

    INCONDITIONNEL, et pas par précaution excessive : le collecteur
    international s'est fait mordre exactement ici — un passage sur quatre cents
    sociétés avait effacé les quatre cent trente-cinq paquets des passages
    précédents. Un collecteur qui écrase est dangereux même quand personne ne lui
    a demandé de découper son univers.

    Ce qu'on vient de collecter PRIME sur ce qui existait : c'est plus récent.
    """
    import glob as _glob

    repris_i = repris_p = 0
    if OUT_JSON.exists():
        try:
            with OUT_JSON.open(encoding="utf-8") as fh:
                ancien = json.load(fh)
            for sym, v in (ancien.get("societes") or {}).items():
                if sym not in index:
                    index[sym] = v
                    repris_i += 1
        except Exception as e:
            print("[warn] index précédent illisible, non fusionné : %s" % e,
                  file=sys.stderr)

    # On RE-RANGE selon la règle courante au lieu de croire le nom du fichier :
    # c'est ce qui fait migrer les anciens paquets par lettre vers les nouveaux
    # paquets par empreinte, sans traitement séparé.
    anciens = []
    for chemin in _glob.glob(str(OUT_DIR / "sec_detail_*.json")):
        cle = Path(chemin).stem.split("_")[-1]
        if not (len(cle) == 3 and cle.isdigit()):
            anciens.append(chemin)
        try:
            with open(chemin, encoding="utf-8") as fh:
                vieux = (json.load(fh) or {}).get("societes") or {}
        except Exception:
            continue
        for sym, v in vieux.items():
            cible = paquets.setdefault(_initiale(sym), {})
            if sym not in cible:
                cible[sym] = v
                repris_p += 1
    if anciens:
        # Les laisser servirait des données périmées à qui les demanderait.
        for chemin in anciens:
            try:
                Path(chemin).unlink()
            except Exception:
                pass
        print("[ok] migration : %d ancien(s) paquet(s) par lettre retiré(s)"
              % len(anciens))

    if repris_i or repris_p:
        print("[ok] fusion : %d sociétés reprises de l'index précédent, "
              "%d des paquets" % (repris_i, repris_p))
    return index, paquets


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

    if opts["source"] == "marche":
        univers = univers_marche(opts["tranche"])
        if not univers:
            # Un univers vide n'est pas un résultat, c'est une panne. Sans ce
            # refus, le collecteur parcourrait zéro société, n'écrirait rien, et
            # sortirait en SUCCÈS. Ce dépôt connaît déjà ce silence.
            print("[fatal] univers de marché demandé mais vide : "
                  "univers_actions.json ou marche_NN.json manquent.",
                  file=sys.stderr)
            return 1
        quoi = "collecte de marché, cotations américaines principales"
        if opts["tranche"]:
            quoi += " — tranche %d sur %d" % (opts["tranche"][0] + 1,
                                              opts["tranche"][1])
        print("[info] %s" % quoi)
    else:
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
    # La date des cotations de l'univers, pour dater le cours de repli. Une
    # cotation sans date se confondrait avec une cotation du jour.
    jour_univers = None
    try:
        with (CACHE_DIR / "univers_actions.json").open(encoding="utf-8") as _fh:
            jour_univers = str((json.load(_fh) or {}).get("updated") or "")[:10] or None
    except Exception:
        pass
    if cours:
        print("[info] historique de cours : %d titres" % len(cours))

    index = {}
    paquets = {}
    ok = sans_cik = echecs = 0
    interrompu = False
    for i, (sym, meta) in enumerate(sorted(univers.items()), 1):
        if interrompu:
            break
        cik = cik_par_ticker.get(sym.upper())
        if not cik:
            sans_cik += 1
            continue
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        try:
            facts_doc = _get(url, accept_404=True)
        except DelaiGlobalAtteint:
            print("[!] délai global atteint après %d sociétés — on écrit ce qui "
                  "a été construit et on s'arrête. La fusion étant "
                  "inconditionnelle, le passage suivant continuera." % ok,
                  file=sys.stderr)
            interrompu = True
            break
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

        # ⚠ On ne repose PAS de cours quand `construire` a refusé les grandeurs
        # de marché : il l'a fait parce que les états ne sont pas en dollars, et
        # la cotation américaine, elle, l'est. Remettre le cours ici referait
        # précisément le croisement de monnaies qu'on vient d'écarter.
        if bati["resume"].get("devises_alignees") is False:
            bati["resume"]["cours_source"] = None
        else:
            bati["resume"]["cours_natif"], bati["resume"]["cours_natif_le"] = \
                _dernier_cours(cours.get(sym))
            # Repli sur la cotation de l'univers. La série du tracker ne couvre
            # que ses huit cents titres ; sans ce repli, 90 % des sociétés
            # américaines n'avaient aucun cours — donc ni P/E, ni rendement, ni
            # prix juste. Ici la conversion est inutile : la cotation retenue est
            # en dollars, et les états d'un déposant SEC le sont aussi.
            bati["resume"]["cours_source"] = \
                "tracker" if bati["resume"]["cours_natif"] is not None else None
            if bati["resume"]["cours_natif"] is None and meta.get("cours_cotation"):
                bati["resume"]["cours_natif"] = meta["cours_cotation"]
                bati["resume"]["cours_natif_le"] = jour_univers
                bati["resume"]["cours_source"] = "univers"
        # ⚠ La devise est posée par `construire`, qui SAIT laquelle il a lue. Elle
        # était écrite « USD » en dur ici, ce qui écrasait la vérité et rendait
        # invisible le cas des déposants étrangers publiant en roubles, en dongs
        # ou en yuans. On ne la touche plus.
        bati["resume"].setdefault("devise", "USD")

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

    # Ce qui vient d'être collecté s'AJOUTE à ce qui existe.
    index, paquets = _fusionner_sec(index, paquets)

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

    if interrompu:
        print("[!] COLLECTE INCOMPLÈTE : arrêtée par le délai global. Ce qui suit "
              "décrit ce qui a été écrit, pas l'univers visé.", file=sys.stderr)
    print(f"[ok] {ok} sociétés — {sans_cik} sans CIK, {echecs} échecs — "
          f"{round(time.time() - t0, 1)} s")
    print(f"[ok] index : {OUT_JSON.stat().st_size // 1024} Ko")
    if poids:
        print(f"[ok] {len(poids)} paquet(s) de détail — "
              f"plus gros {max(poids) // 1024} Ko, total {sum(poids) // 1024} Ko")
    return 0


if __name__ == "__main__":
    sys.exit(main())
