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
# ── LE VOCABULAIRE INTERNATIONAL, ET LES TROIS ÉTIQUETTES QU'ON REFUSE ──
#
# Le lecteur interroge les DEUX taxonomies. Ce n'est donc pas la taxonomie qui
# manquait mais le VOCABULAIRE : sur cinquante grandeurs, six seulement portaient
# un nom international. 297 sociétés et 2 257 exercices n'avaient AUCUN résultat
# avant impôt, AUCUNE trésorerie, sur aucun exercice — Novo Nordisk, Ericsson,
# AB InBev, Nokia, Ferrari, BAT, Ambev, Tenaris, Aegon, VEON, Cenovus, BBVA.
#
# Les étiquettes internationales sont ajoutées EN FIN de liste : le rang départage
# à date égale, donc un nom us-gaap reste prioritaire chez un déposant américain.
#
# ⚠ TROIS ÉTIQUETTES SONT REFUSÉES, ET CE SONT CELLES QUI COMBLENT LE PLUS DE
# CASES. C'est précisément ce qui les rend dangereuses : un recensement par
# fréquence les remonterait en tête, et quelqu'un les ajouterait.
#
#   · `FinanceCosts` comble 100 % des trous de charge d'intérêts contre 40 % pour
#     l'étiquette juste — mais la norme y met aussi les pertes de change et la
#     désactualisation. ASR 2017 : 618 831 000 MXN contre 1 804 000 d'intérêts
#     réels, facteur 343. TEO 2024 : −1 914 786 000 000 ARS, NÉGATIF, contre
#     +173 837 000 000. La couverture d'intérêts qui en sortirait serait négative
#     et parfaitement bien formée.
#   · `WeightedAverageShares` : PAC 2024, BPA 17,0444 × 5,053e11 actions =
#     8,612e12 pour un résultat de 8,612e9. Facteur mille exact, quatre exercices
#     d'affilée.
#   · `Borrowings` dans `lt_debt` : c'est le TOTAL de la dette, pas sa part
#     longue. BSAC 2021, 8 827 Md CLP contre 16 056 pour la somme des échéances.
#     Il a sa place dans la dette totale, jamais dans une composante.
#
# Et `ShareBasedPaymentsExpense` N'EXISTE PAS : la taxonomie écrit
# `ExpenseFromSharebasedPaymentTransactionsWithEmployees`, avec un b MINUSCULE.
# Même piège que le pluriel de `NoncontrollingInterests`. Non ajoutée tant qu'elle
# n'a pas été contrôlée en valeur sur un cas nommé.
#
# Enfin, `...AndGeneralPartnershipUnit...` est refusée : c'est le concept dont les
# deux valeurs se contredisent d'un dépôt à l'autre chez Natural Resource Partners.
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
    # ── DEUX SÉRIES DE CONTRÔLE, POUR UN DÉFAUT QUI DÉCAPITE LES FONCIÈRES ──
    #
    # Le revenu « contrats clients » de la norme 606 est, par construction, un
    # SOUS-ENSEMBLE du chiffre d'affaires total : les loyers relèvent des baux,
    # norme 842, et n'y figurent pas. Pour un bailleur, l'étiquette 606 ne porte
    # donc que des miettes — des honoraires de gestion.
    #
    # Mesuré le 28/08/2026 : AvalonBay passe de 1 856 M$ en 2015 à 5,6 M$ en 2016
    # et n'en bouge plus, parce qu'elle a commencé à déposer l'étiquette 606 cette
    # année-là et que celle-ci prime dans notre ordre. Vingt-cinq sociétés
    # affichent un résultat net supérieur à dix fois leur chiffre d'affaires,
    # et la liste est une liste de foncières : REXR, CTRE, AVB, AIV, UDR, CPT, LXP.
    #
    # On collecte donc les deux séries À PART pour pouvoir arbitrer, plutôt que
    # de réordonner la liste — un simple réordonnancement casserait les BANQUES,
    # dont on veut délibérément le produit NET d'intérêts et non le produit brut.
    "revenue_total": ["Revenues"],
    "revenue_contrats": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                         "RevenueFromContractWithCustomerIncludingAssessedTax"],
    # Les LOYERS, quand la société ne dépose aucun total. Camden Property n'a pas
    # de ligne `Revenues` du tout : son chiffre d'affaires 2025 est la somme de
    # 1 573 M$ de loyers et de 13 M$ de contrats. Les deux ensembles sont
    # DISJOINTS par construction — la norme 842 régit les baux, la 606 les
    # contrats clients, et une recette ne relève que de l'une des deux. Les
    # additionner n'est donc pas une estimation, c'est une addition.
    # ── LE PRODUIT NET BANCAIRE ──
    # Une banque ne vend pas des marchandises : son produit d'exploitation est la
    # marge d'intérêt plus les commissions. Ces deux étiquettes étaient
    # totalement absentes du dictionnaire, et Western Alliance publiait 135,8 M$
    # de chiffre d'affaires pour une banque qui en fait 3 543.
    "produit_interet_net": ["InterestIncomeExpenseNet"],
    "produit_commissions": ["NoninterestIncome"],

    "revenue_baux": ["OperatingLeaseLeaseIncome",
                     "OperatingLeasesIncomeStatementMinimumLeaseRevenue"],
    "cogs": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold",
             "CostOfServices",
        "CostOfSales"],
    "gross_profit": ["GrossProfit"],
    "rd": ["ResearchAndDevelopmentExpense",
           "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
    "sga": ["SellingGeneralAndAdministrativeExpense"],
    "ga": ["GeneralAndAdministrativeExpense"],
    "sm": ["SellingAndMarketingExpense"],
    "opex": ["OperatingExpenses", "CostsAndExpenses", "OperatingCostsAndExpenses"],
    "autres_non_ope": ["OtherNonoperatingIncomeExpense", "NonoperatingIncomeExpense"],
    "operating_income": ["OperatingIncomeLoss",
        "ProfitLossFromOperatingActivities"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
               "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "ProfitLossBeforeTax"],
    "tax": ["IncomeTaxExpenseBenefit",
        "IncomeTaxExpenseContinuingOperations"],
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
    # `ProfitLossAttributableToOwnersOfParent` est la part du groupe balisée
    # DIRECTEMENT par un déposant IFRS — sans soustraction, donc sans risque.
    # Elle vient en second : chez un déposant américain, `NetIncomeLoss` doit
    # rester prioritaire, et le rang de `_annuels` fait gagner l'étiquette la
    # plus spécifique.
    "net_income": ["NetIncomeLoss", "ProfitLossAttributableToOwnersOfParent"],
    "net_income_total": ["ProfitLoss"],
    # ⚠ LE PLURIEL. La taxonomie IFRS écrit `...NoncontrollingInterests`, avec un
    # S. Le dictionnaire ne connaissait que le singulier américain, et sur huit
    # déposants IFRS vérifiés un par un, le singulier collecte ZÉRO date quand le
    # pluriel en collecte trois à onze.
    #
    # Sans lui, `net_income` reste le résultat TOTAL au lieu de la part du
    # groupe : Grupo Aval surévalué de 30,1 %, JBS de 10,2 %, BTG de 311,9 % sur
    # 2023 — et le ROE, la marge nette, la distribution et les croissances du
    # résultat en dérivent toutes.
    "interets_minoritaires_resultat": [
        "NetIncomeLossAttributableToNoncontrollingInterest",
        "ProfitLossAttributableToNoncontrollingInterest",
        "ProfitLossAttributableToNoncontrollingInterests"],
    "net_income_common": ["NetIncomeLossAvailableToCommonStockholdersBasic"],
    "interest_expense": ["InterestExpense", "InterestExpenseDebt",
                         "InterestExpenseNonoperating",
        "InterestExpenseOnBorrowings"],
    "eps_diluted": ["EarningsPerShareDiluted",
        "DilutedEarningsLossPerShare",
        "DilutedEarningsLossPerShareFromContinuingOperations",
        "EarningsPerShareBasicAndDiluted",
        "BasicAndDilutedEarningsLossPerShare",
        "IncomeLossFromContinuingOperationsPerBasicAndDilutedShare",
        "NetIncomeLossNetOfTaxPerOutstandingLimitedPartnershipUnitDiluted"],
    "eps_basic": ["EarningsPerShareBasic",
        "BasicEarningsLossPerShare",
        "BasicEarningsLossPerShareFromContinuingOperations",
        "EarningsPerShareBasicAndDiluted",
        "BasicAndDilutedEarningsLossPerShare",
        "NetIncomeLossPerOutstandingLimitedPartnershipUnitBasicNetOfTax"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
        "WeightedAverageNumberOfSharesOutstandingBasicAndDiluted",
        "WeightedAverageLimitedPartnershipUnitsOutstandingDiluted"],
    "shares_basic": ["WeightedAverageNumberOfSharesOutstandingBasic",
                     "WeightedAverageNumberOfSharesOutstanding",
        "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
        "WeightedAverageNumberOfSharesOutstandingBasicAndDiluted",
        "WeightedAverageLimitedPartnershipUnitsOutstanding"],

    # ── Bilan (instant) ──
    "assets": ["Assets"],
    "assets_current": ["AssetsCurrent",
        "CurrentAssets"],
    "liabilities": ["Liabilities"],
    "liabilities_current": ["LiabilitiesCurrent",
        "CurrentLiabilities"],
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
    # `MinorityInterest` est le nom américain, `NoncontrollingInterests` le nom
    # IFRS — même poste, deux taxonomies. Symétrique de ce qui a déjà été fait
    # pour les capitaux propres quelques lignes plus haut.
    "interets_minoritaires_bilan": ["MinorityInterest", "NoncontrollingInterests"],
    # ── LE TÉMOIN QUI DIT SI `equity` INCLUT DÉJÀ LES MINORITAIRES ──
    # `equity` accepte deux étiquettes : `StockholdersEquity`, qui EXCLUT les
    # minoritaires, et le repli `...IncludingPortionAttributableToNoncontrolling
    # Interest`, qui les INCLUT. La reconstruction du passif soustrait les
    # minoritaires — juste dans le premier cas, faux dans le second, où elle les
    # retire une seconde fois. Mesuré : 22 exercices reconstruits à tort, jusqu'à
    # 8,7 % d'écart.
    #
    # On collecte donc la seule étiquette exclusive, à part. Si elle porte une
    # valeur pour un exercice, c'est elle que l'union a retenue — elle vient en
    # tête de liste et gagne à date égale — et la soustraction est légitime.
    "equity_part_groupe": ["StockholdersEquity"],
    "capitaux_mezzanine": [
        "TemporaryEquityCarryingAmountAttributableToParent",
        "TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests",
        "RedeemableNoncontrollingInterestEquityCarryingAmount",
        "RedeemableNoncontrollingInterestEquityFairValue",
    ],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndCashEquivalents"],
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
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt",
        "LongtermBorrowings"],
    "current_debt": ["LongTermDebtCurrent", "DebtCurrent",
        "ShorttermBorrowings"],
    # Les loyers capitalisés SONT de la dette depuis IFRS 16 / ASC 842, et les
    # omettre sous-estime l'endettement de tout ce qui loue ses locaux ou ses
    # centres de données. Contrôle sur NVIDIA, exercice 2026 : 7 469 + 999 de
    # dette financière + 2 572 + 372 de loyers = 11 412 M$, soit exactement le
    # chiffre publié par le concurrent. Sans les loyers on tombait à 8 468.
    # ── LES BAUX S'ADDITIONNENT, ILS NE SE CHOISISSENT PAS ──
    #
    # Ces quatre étiquettes étaient rangées deux par deux, comme des synonymes.
    # Le lecteur départage alors au RANG : le loyer simple passait, le
    # crédit-bail était jeté. Ce ne sont pas des synonymes mais deux COMPOSANTES
    # du bilan — 21,4 Md$ manquants sur 22 sociétés.
    #
    # La correction d'origine des baux avait été validée sur NVIDIA, qui ne
    # dépose AUCUN crédit-bail : le témoin choisi était celui sur lequel le
    # défaut est invisible.
    # ── LA DETTE DES DÉPOSANTS INTERNATIONAUX ──
    # `Borrowings` est le TOTAL des emprunts, pas une composante : il n'a donc
    # rien à faire dans `lt_debt` (voir le refus en tête du dictionnaire), et
    # toute sa place ici. `LeaseLiabilities` est le total ACTUALISÉ des baux —
    # ne jamais lui préférer `GrossLeaseLiabilities`, qui ne l'est pas et
    # gonflerait la dette de 45 % chez Shell.
    "emprunts_ifrs": ["Borrowings"],
    "baux_ifrs": ["LeaseLiabilities"],

    "bail_simple_lt": ["OperatingLeaseLiabilityNoncurrent"],
    "credit_bail_lt": ["FinanceLeaseLiabilityNoncurrent"],
    "bail_simple_ct": ["OperatingLeaseLiabilityCurrent"],
    "credit_bail_ct": ["FinanceLeaseLiabilityCurrent"],
    "goodwill": ["Goodwill"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit",
        "RetainedEarnings"],
    "inventory": ["InventoryNet",
        "Inventories"],

    # ── Flux de trésorerie (durée) ──
    # Le troisième nom est celui de la taxonomie IFRS. Sans lui, 267 sociétés
    # déjà publiées n'avaient AUCUN flux d'exploitation — donc pas de cash libre,
    # pas de marge de cash libre, pas de distribution sur cash libre, et deux
    # points de Piotroski perdus pour 264 d'entre elles. Le flux existait dans
    # leur dépôt ; nous ne demandions pas le bon mot.
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            "CashFlowsFromUsedInOperatingActivities"],
    # ── L'INVESTISSEMENT, ET CE QU'ON REFUSE D'APPELER AINSI ──
    #
    # Mesuré le 28/08/2026 : 4 454 exercices sur 41 038 n'ont pas d'investissement
    # et donc pas de cash libre. Deux populations très différentes s'y cachent.
    #
    # 292 sociétés n'en ont JAMAIS — banques, assureurs, foncières. Wells Fargo
    # sort dix-neuf exercices sur dix-neuf. Il n'y a rien à réparer : leur métier
    # n'a pas de ligne d'investissement, et en fabriquer une serait pire que la
    # case vide.
    #
    # 436 sociétés en ont avec des TROUS — 1 964 exercices perdus. Recensement sur
    # soixante d'entre elles : trois étiquettes de décaissement, jamais lues,
    # couvrent des années qu'aucune de celles ci-dessus ne couvre. On les ajoute
    # APRÈS les autres : la recouture départage par rang, donc elles ne servent
    # que là où le reste manque.
    #
    # ⚠ CE QU'ON REFUSE, ET POURQUOI. Le recensement remonte aussi, en nombre,
    # `ProceedsFromSaleOfPropertyPlantAndEquipment`, `ProceedsFromSaleOfProductive
    # Assets`, `PropertyPlantAndEquipmentDisposals` et leurs variantes. Ce sont
    # des PRODUITS DE CESSION : de l'argent qui ENTRE. Les prendre pour des
    # investissements inverserait le signe du cash libre sur ces exercices — une
    # société qui vend une usine passerait pour une société qui en construit une.
    # `CapitalExpendituresIncurredButNotYetPaid` est également refusé : c'est un
    # montant engagé et NON payé, donc pas un flux de trésorerie.
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets",
              "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
              "PaymentsToAcquireOtherPropertyPlantAndEquipment",
              "PaymentsForCapitalImprovements",
              # Pétrole et gaz : leur investissement porte un autre nom, et sans
              # lui tout un secteur perd son cash libre.
              "PaymentsToAcquireOilAndGasPropertyAndEquipment",
              "PaymentsToAcquireOilAndGasProperty",
              # Dernier repli : une composante, pas le total. Elle ne sert que
              # sur les exercices où aucune ligne complète n'est déposée — et sur
              # ceux-là, une part de l'investissement vaut mieux que rien, à
              # condition de savoir qu'elle est partielle.
              "PaymentsToAcquireOtherProductiveAssets",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
    # Le décaissement NET d'investissement — achats MOINS cessions. Il vit dans
    # son propre champ et non parmi les précédents, pour une raison de signe : le
    # collecteur prend la valeur absolue de l'investissement, parce que certains
    # déposants l'écrivent en négatif. Sur une grandeur nette, un négatif ne veut
    # pas dire la même chose — il veut dire que la société a vendu plus qu'elle
    # n'a acheté. Mélanger les deux transformerait cette année-là en année
    # d'investissement record. On le collecte donc à part, et on ne s'en sert
    # qu'à défaut, en gardant son signe.
    "capex_net": ["PaymentsForProceedsFromProductiveAssets"],
    "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfOrdinaryDividends",
                       "PaymentsOfDividends",
        "DividendsPaidToEquityHoldersOfParentClassifiedAsFinancingActivities",
        "DividendsPaidClassifiedAsFinancingActivities"],
    # Le dividende PRÉFÉRENTIEL, séparément — et il sert à une chose précise.
    # `PaymentsOfDividends` est un TOTAL : il englobe les actions de préférence.
    # Sans cette ligne, le dividende par action implicite (versé ÷ actions) est
    # gonflé pour toute société qui en verse, et le contrôle qui s'appuie dessus
    # accuse à tort. Mesuré : JPMorgan 2009, qui a réellement coupé de 1,52 $ à
    # 0,20 $, portait 3 422 M$ de dividendes versés dont l'essentiel revenait aux
    # préférentielles du plan TARP. L'implicite sortait à 0,88 $ et faisait passer
    # une vraie coupe pour une erreur de saisie.
    "dividends_paid_preferred": ["PaymentsOfDividendsPreferredStockAndPreferenceStock",
                                 "PaymentsOfDistributionsToAffiliates"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock",
        "PaymentsToAcquireOrRedeemEntitysShares"],
    "dna": ["DepreciationDepletionAndAmortization",
            "DepreciationAmortizationAndAccretionNet",
            "DepreciationAndAmortization", "Depreciation",
        "DepreciationAndAmortisationExpense"],
    # ⚠ LES DEUX PREMIÈRES SONT US-GAAP, ET ELLES SEULES ÉTAIENT CHERCHÉES.
    # Aucun déposant en normes internationales — tous les étrangers cotés aux
    # États-Unis, qui remplissent un 20-F — ne les produit. Couverture mesurée le
    # 29/08/2026 : 44,4 % côté SEC contre 74,7 % côté international.
    #
    # À l'écran, 5 283 fiches affirmaient « X ne verse pas de dividende » et
    # 1 925 d'entre elles versaient : Taiwan Semiconductor 11,07 Md$ en 2024,
    # Shell, Citigroup, Novo Nordisk, Unilever, Danaher, BAT, Petrobras.
    #
    # ⚠ `DividendsRecognisedAsDistributionsToOwnersPerShare` n'est PAS ajoutée :
    # elle est dimensionnée par catégorie d'actions dans la plupart des dépôts,
    # et l'API rend alors une valeur par catégorie. En additionner ou en choisir
    # une donnerait un montant faux ; on préfère la case vide.
    "dps": ["CommonStockDividendsPerShareDeclared",
            "CommonStockDividendsPerShareCashPaid",
            "DividendsPerShareDeclared",
            "DividendsDeclaredPerShare",
            "DividendsPaidPerShare"],
}

# Un fait de DURÉE (chiffre d'affaires) porte un début et une fin ; un fait
# d'INSTANT (trésorerie) n'a qu'une fin. Les confondre revient à comparer un flux
# annuel à un solde ponctuel.
INSTANTS = {"assets", "assets_current", "liabilities", "liabilities_current",
            "equity", "cash", "short_term_inv", "lt_debt", "current_debt",
            "bail_simple_lt", "credit_bail_lt", "bail_simple_ct", "credit_bail_ct",
            "emprunts_ifrs", "baux_ifrs",
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
    # ── NE COMPTER QUE LES DÉPÔTS ANNUELS ──
    #
    # Le comptage prenait TOUS les points, y compris ceux d'un 6-K trimestriel.
    # Deux points en dollars déposés dans un rapport trimestriel suffisaient donc
    # à égaler la monnaie locale en couverture — et l'égalité revient au dollar,
    # à raison, puisque c'est la colonne comparable.
    #
    # Puis le lecteur jetait les faits en monnaie locale (devise retenue : le
    # dollar) ET les faits en dollars (un 6-K n'est pas une forme annuelle). La
    # société se vidait entièrement. Deux gardes justes qui se détruisaient.
    #
    # Sans aucun poste annuel, on retombe sur le comptage complet : mieux vaut
    # une décision imparfaite qu'aucune.
    def annuels_seuls(pts):
        return [p for p in pts
                if (p.get("form") or "").split("/")[0] in FORMES_ANNUELLES]

    def compter(annuel):
        couv, pts_par_devise = {}, {}
        for groupe in postes:
            vues_groupe = set()
            for espace in ("us-gaap", "ifrs-full"):
                bloc = facts.get(espace) or {}
                for nom in groupe:
                    for u, pts in ((bloc.get(nom) or {}).get("units") or {}).items():
                        if not (u.isalpha() and len(u) == 3):
                            continue
                        retenus = annuels_seuls(pts) if annuel else pts
                        if not retenus:
                            continue
                        vues_groupe.add(u)
                        pts_par_devise[u] = pts_par_devise.get(u, 0) + len(retenus)
            for u in vues_groupe:
                couv[u] = couv.get(u, 0) + 1
        return couv, pts_par_devise

    # D'abord sur les seuls dépôts annuels. Si aucune devise n'y est couverte —
    # une société qui n'a encore déposé que des trimestriels — on retombe sur le
    # comptage complet : une décision imparfaite vaut mieux qu'aucune.
    couverture, points = compter(True)
    if not couverture:
        couverture, points = compter(False)
    if not couverture:
        return None
    meilleure = max(couverture.values())
    if couverture.get("USD", 0) >= meilleure:
        return "USD"
    # Sinon : la devise qui couvre le plus de postes, départagée par le nombre de
    # points quand deux monnaies en couvrent autant.
    return max(couverture, key=lambda u: (couverture[u], points.get(u, 0)))


def _annuels(facts, concept_noms, instant=False, devise=None, millesimes=False):
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
            # ── LES UNITÉS « PAR ACTION » PORTENT UNE DEVISE, ELLES AUSSI ──
            #
            # Le test ci-dessus reconnaît une devise à `u.isalpha() and len == 3`.
            # L'unité d'un bénéfice par action s'écrit « USD/shares », « TWD/shares » :
            # la barre oblique n'est pas alphabétique, la liste des monétaires
            # ressort vide, et le verrou de devise est SAUTÉ. On tombait alors sur
            # l'unité la plus peuplée, sans regarder la monnaie.
            #
            # Taiwan Semiconductor dépose son BPA dans les deux monnaies. Le
            # dollar taïwanais étant le plus fourni, la fiche aurait publié 44,68
            # pour un cours en dollars américains — facteur 33, P/E divisé
            # d'autant. Mesuré aussi sur BCH (pesos chiliens), ELLO, TLX.
            #
            # On préfère donc explicitement la devise de la société quand elle
            # existe, AVANT de retomber sur la plus peuplée.
            par_action = "%s/shares" % devise if devise else None
            if par_action and par_action in unites:
                cle_unite = par_action
            else:
                # Ni devise à trois lettres, ni « devise/shares » : des actions,
                # des taux, des ratios. La plus peuplée reste le bon choix.
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
                # Cinquième et sixième places : la valeur la plus ANCIENNE pour
                # cette étiquette, et sa date de dépôt. Un exercice déjà déposé
                # ne change jamais de nombre d'actions — SAUF division. Leur
                # rapport est donc le facteur exact, là où le comparer à
                # l'exercice voisin oblige à deviner.
                out[fin] = [float(val), p.get("accn"), depose, rang, float(val), depose]
            elif rang < prec[3]:
                # étiquette plus spécifique pour la même date : elle l'emporte,
                # et l'on repart de zéro pour les millésimes — comparer deux
                # étiquettes différentes ne mesure rien.
                out[fin] = [float(val), p.get("accn"), depose, rang, float(val), depose]
            elif rang == prec[3]:
                if depose >= prec[2]:
                    # même étiquette, dépôt plus récent : c'est le chiffre corrigé
                    prec[0], prec[1], prec[2] = float(val), p.get("accn"), depose
                if depose < prec[5]:
                    prec[4], prec[5] = float(val), depose
    if millesimes:
        return {k: (v[0], v[1], v[2], v[4], v[5]) for k, v in out.items()}
    return {k: (v[0], v[1], v[2]) for k, v in out.items()}


def facteurs_de_division(facts, devise=None):
    """Les facteurs de division LUS dans les millésimes, par date de fin d'exercice.

    Un exercice déjà déposé ne change jamais de nombre d'actions, sauf division.
    Le rapport entre la valeur la plus récemment déposée et la plus ancienne EST
    donc le facteur — sans tolérance à régler, sans exercice voisin à interroger.

    O'Reilly exercice 2023 : 60 998 000 actions dans les dépôts de 2024 et 2025,
    puis 914 976 000 dans celui de 2026. Rapport 15,0001.

    ⚠ TROIS GARDES, dont une SANS LAQUELLE CE REMÈDE DÉTRUIT CSX :

      · `_facteur_division` PLAFONNE. CSX exercice 2008 donne un rapport de
        1000,0000 et 2009 de 2999,85 : ses vieux dépôts expriment les actions en
        MILLIERS, pas en unités. Sans ce plafond, on multiplierait CSX par mille.
      · l'étiquette doit être la MÊME des deux côtés — `_annuels` s'en charge en
        repartant de zéro quand une étiquette plus spécifique l'emporte.
      · la tolérance reste utile : 15,0001 et 3,0013 doivent s'arrondir.

    LE BÉNÉFICE PAR ACTION EN SECOURS. Alphabet n'a aucun nombre d'actions de
    2013 à 2021 — elle les balise par catégorie A/B/C et l'API jette les faits
    dimensionnés. Le BPA, lui, porte l'information : exercice 2020 à 58,61 puis
    2,93, rapport 20,0034. Il varie à l'INVERSE des actions, d'où l'inversion.
    """
    out = {}
    for cle, inverse in (("shares_diluted", False), ("eps_diluted", True)):
        serie = _annuels(facts, CONCEPTS[cle], devise=devise, millesimes=True)
        for fin, v in serie.items():
            if fin in out:
                continue          # le nombre d'actions a la priorité
            recente, _, _, ancienne, _ = v
            if not ancienne or not recente:
                continue
            r = (ancienne / recente) if inverse else (recente / ancienne)
            if r <= 1.0:
                continue
            f = _facteur_division(r)
            if f is not None:
                out[fin] = (f, cle)
    return out


def exercices_confirmes(facts, devise=None):
    """{date_de_fin: date du dépôt le plus récent} pour les exercices JAMAIS retraités.

    ⚠ L'ABSENCE DE MILLÉSIME DIVERGENT EST UNE INFORMATION, ET C'EST L'INVERSE
    DE CE QU'ON EN FAISAIT. `facteurs_de_division` ne retient que les exercices
    dont la valeur a CHANGÉ entre deux dépôts. Ceux dont elle n'a PAS changé
    étaient jetés — alors qu'ils prouvent quelque chose de fort : une division
    d'action retraite TOUJOURS les exercices antérieurs, donc un exercice
    redéposé à l'identique après une date n'a connu aucune division depuis.

    Cas qui l'a fait écrire : VNET Group. L'exercice 2023 vaut 901 143 138
    actions diluées dans les dépôts de 2024, 2025 ET 2026. L'inférence voyait le
    saut à 1 742 346 367 en 2024 et concluait à une division ×2, rebasant treize
    ans d'historique par action. VNET n'a pas divisé : il a émis.

    On rend la date du dépôt le plus récent, parce que la répétition seule ne
    prouve rien — deux dépôts ANTÉRIEURS au saut n'avaient rien à retraiter.
    L'appelant exige un dépôt postérieur.
    """
    out = {}
    for cle in ("shares_diluted", "eps_diluted"):
        serie = _annuels(facts, CONCEPTS[cle], devise=devise, millesimes=True)
        for fin, v in serie.items():
            recente, _, depose_r, ancienne, depose_a = v
            if not recente or not ancienne:
                continue
            if not depose_r or not depose_a or depose_r <= depose_a:
                continue          # une seule vue : elle ne confirme rien
            # Identiques à un cheveu près : les dépôts arrondissent parfois le
            # dernier chiffre d'un nombre d'actions.
            if abs(recente - ancienne) / max(abs(ancienne), 1e-9) > 1e-6:
                continue
            if fin not in out or depose_r > out[fin]:
                out[fin] = depose_r
    return out


def _val(serie, fin):
    v = serie.get(fin)
    return v[0] if v else None


# ─────────────────────────────────────────────────────────────────────────
# Le calcul commun vit dans `fondamentaux_communs` — voir l'en-tête de ce
# module pour la raison. Ce fichier ne garde que ce qui touche à la SEC.
# ─────────────────────────────────────────────────────────────────────────
from fondamentaux_communs import (
    ecarter_ratios_degeneres,          # noqa: E402
    annee_exercice,
    dedupliquer_exercices,
    _div, _pct, _r,
    _BAREME, _CRITERES_INDUSTRIELS, _noter_critere, note_quantitative,
    _croissance_annuelle, _mediane, _mediane_fenetre, _croissances, _predictibilite,
    _serie_sans_baisse_dividende, _serie_hausses_dividende,
    _FACTEURS_USUELS, _facteur_division, _corriger_divisions,
    _piotroski, _altman_z,
    effacer_l_impossible,
    redresser_dividende_par_action,
    TAUX_SANS_RISQUE, PRIME_DE_RISQUE, _wacc,
    beta_plausible,
    cours_ancres, cours_a_la_date,
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
        # ── DEUX GRANDEURS POUR LE BARÈME DE SUBSTITUTION ──
        #
        # Une banque n'a pas d'EBITDA mais elle a des fonds propres et un bilan :
        # c'est de leur rapport que dépend sa solidité. Une foncière a un flux
        # d'exploitation erratique mais un chiffre d'affaires stable. Ces deux
        # ratios remplacent `dette_ebitda_brut` et `capex_ocf` quand le dépôt se
        # tait — voir `_SUBSTITUTS` dans `fondamentaux_communs`.
        #
        # ⚠ NULS PLUTÔT QUE ZÉRO quand un terme manque : un ratio sans
        # dénominateur n'est pas nul, il est inconnu, et la note distingue les deux.
        "fonds_propres_sur_actif": (
            _r(100.0 * d["equity"] / d["assets"], 2)
            if (isinstance(d.get("equity"), (int, float))
                and isinstance(d.get("assets"), (int, float))
                and d["assets"] > 0) else None),
        "capex_sur_ca": (
            _r(100.0 * abs(d["capex"]) / d["revenue"], 2)
            if (isinstance(d.get("capex"), (int, float))
                and isinstance(d.get("revenue"), (int, float))
                and d["revenue"] > 0) else None),
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
# Le pas maximal toléré dans une série de nombres d'actions APRÈS recouture. Un
# saut qui subsiste est un défaut, ou une entrée en bourse : dans les deux cas,
# transporter une capitalisation à travers lui donnerait un faux.
SAUT_ACTIONS_MAX = 5.0


def combler_mcap_par_ancres(exercices, variations, jour_ref):
    """Comble la capitalisation manquante en transportant un RAPPORT, pas un montant.

        mcap(exercice) = mcap_du_dernier × cours(exercice)/cours(aujourd'hui)
                                         × actions(exercice)/actions(dernier)

    le premier rapport valant exactement `1 / (1 + chNy/100)`.

    ⚠ POURQUOI CETTE FONCTION EXISTE ICI ET PAS SEULEMENT DANS LE REJEU. Un
    correctif qui ne vit que dans le rejeu est détruit au passage suivant du
    collecteur, en silence. La règle doit être là où la donnée est fabriquée.

    ⚠ POURQUOI UN RAPPORT ET NON UN MONTANT. Calculer `cours × actions`
    obligerait à connaître la devise des états, l'unité du nombre d'actions et un
    taux de change — trois occasions de se tromper — et produirait un montant qui
    ne se raccorde pas à celui du dernier exercice, d'où des sauts de ×3 000 sur
    le graphique. Un rapport n'a ni monnaie ni unité, et vaut 1 au dernier
    exercice.

    ⚠ ON NE COMBLE QUE LES VIDES : les titres à vraie série de cours gardent la
    leur, exercice par exercice.

    ⚠ QUATRE ANCRES, PAS UNE SÉRIE, et six mois de tolérance. On n'interpole pas
    entre deux ancres : une capitalisation devinée nourrirait un jugement de
    création de valeur, et un jugement bâti sur une interpolation n'en est pas un.
    L'écart retenu est inscrit sous `mcap_ecart_jours`.

    Rend le nombre d'exercices comblés.
    """
    if not variations or not jour_ref or not exercices:
        return 0
    try:
        ref_date = datetime.fromisoformat(jour_ref)
    except Exception:
        return 0

    serie = [e.get("shares_diluted") for e in exercices]
    serie = [x for x in serie if isinstance(x, (int, float)) and x > 0]
    for k in range(1, len(serie)):
        if max(serie[k] / serie[k - 1], serie[k - 1] / serie[k]) > SAUT_ACTIONS_MAX:
            return 0

    ref = None
    for e in reversed(exercices):
        if (e.get("mcap_estime") or 0) > 0 and e.get("shares_diluted"):
            ref = e
            break
    if ref is None:
        return 0
    sh_ref = ref["shares_diluted"]

    n = 0
    for e in exercices:
        if e is ref or (e.get("mcap_estime") or 0) > 0:
            continue
        sh, fin_e = e.get("shares_diluted"), e.get("fin")
        if not sh or sh <= 0 or not fin_e:
            continue
        try:
            jours = (ref_date - datetime.fromisoformat(fin_e)).days
        except Exception:
            continue
        rapport, ecart = cours_a_la_date(variations, jours)
        if rapport is None or rapport <= 0:
            continue
        e["mcap_estime"] = round(ref["mcap_estime"] * rapport * sh / sh_ref)
        e["mcap_source"] = "ancre"
        e["mcap_ecart_jours"] = ecart
        n += 1
    return n


def construire(facts, mcap_usd=None, beta=None, cours=None,
               variations=None, jour_marche=None):
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
    # `net_income_total` est l'étiquette IFRS `ProfitLoss` : elle était lue,
    # rangée, et absente de l'union. Une société qui ne balise QUE le résultat
    # total — un déposant IFRS qui ne ventile pas la part du groupe — n'avait
    # donc aucun exercice et disparaissait du parc. BBVA passe de 0 à 11
    # exercices par ce seul ajout, BSBR 11, VIV 11, NXE 10, BTG 10, TLK 8.
    axe = sorted(set(series["revenue"])
                 | set(series["net_income"])
                 | set(series["net_income_total"])
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

        # ── Le bailleur dont le chiffre d'affaires ne retient que les miettes ──
        # On ne remplace QUE si les trois conditions sont réunies : la valeur
        # retenue vient bien de l'étiquette « contrats clients », un total existe
        # pour la même année, et ce total la dépasse de moitié. Hors de ce cas
        # précis on ne touche à rien — surtout pas aux banques, dont le produit
        # net d'intérêts est plus petit que leur produit brut et doit le rester.
        _baux = e.get("revenue_baux")
        if (e.get("revenue") is not None
                and e.get("revenue_contrats") is not None
                and e["revenue"] == e["revenue_contrats"]):
            if (e.get("revenue_total") is not None
                    and e["revenue_total"] > e["revenue"] * 1.5):
                e["revenue"] = e["revenue_total"]
                e["revenue_total_utilise"] = True
            elif _baux is not None and _baux > e["revenue"]:
                # Aucun total déposé : on additionne les deux ensembles disjoints.
                e["revenue"] = _baux + e["revenue"]
                e["revenue_total_utilise"] = "loyers + contrats"
        elif (e.get("revenue") is None and _baux is not None
                and isinstance(e.get("assets"), (int, float)) and e["assets"] > 0
                and _baux >= 0.02 * e["assets"]):
            # ── LES LOYERS SEULS, ET SEULEMENT POUR UN VRAI BAILLEUR ──
            # Un bailleur qui ne dépose ni total ni contrats : ses loyers SONT
            # son chiffre d'affaires. Mais une BANQUE dépose aussi des revenus
            # de crédit-bail, et ce n'est pas son activité. Sans garde, cette
            # ligne attribuait 80 M$ de recettes à Fifth Third Bancorp, qui en
            # fait huit milliards et demi — un faux chiffre est pire qu'un vide,
            # parce que le vide se voit et que le faux se lit.
            #
            # Le discriminant est le rapport des loyers à l'actif, et la mesure
            # du 28/08/2026 le rend évident : sur 237 exercices concernés, 131
            # tombent SOUS 0,5 % — vingt-neuf banques, dont certaines à zéro
            # loyer — et 90 se tiennent entre 5 % et 15 %, ce qui est exactement
            # le rendement locatif d'un parc immobilier. Le seuil de 2 % passe
            # dans le creux entre les deux populations, pas au milieu de l'une.
            e["revenue"] = _baux
            e["revenue_total_utilise"] = "loyers seuls"

        # ── LE PRODUIT NET BANCAIRE, TROISIÈME BRANCHE ──
        #
        # Même mécanisme que les bailleurs, autre métier. Quand le chiffre
        # d'affaires se réduit aux commissions — c'est-à-dire quand il ÉGALE le
        # chiffre d'affaires « contrats » — et que la société dépose les deux
        # composantes du produit bancaire, on prend leur somme.
        #
        # ⚠ LA PREMIÈRE CONDITION NE SUFFIT PAS, ET DE LOIN. L'égalité est vraie
        # pour 2 168 sociétés, dont NVIDIA, Apple, Microsoft, Amazon, Walmart,
        # Exxon et Johnson & Johnson. C'est la présence SIMULTANÉE des deux
        # étiquettes qui fait garde-fou — mesuré avant d'écrire cette branche, sur
        # 62 sociétés tirées de ces 2 168 : deux seulement les déposent, et ce
        # sont deux banques. Zéro non-financière.
        # ⚠ LA CONDITION SUR L'EXISTENCE DU CHIFFRE D'AFFAIRES A ÉTÉ RETIRÉE.
        # Elle exigeait qu'un chiffre d'affaires existe DÉJÀ pour le corriger :
        # la branche ne pouvait donc que réécrire, jamais créer. Or 148 sociétés
        # américaines n'en ont AUCUN sur AUCUN exercice tout en déposant les deux
        # composantes du produit bancaire — Truist, banque du S&P 500, dix-neuf
        # exercices, marge d'intérêt et commissions dormant dans son paquet.
        #
        # La garde qui compte n'a jamais été cette condition, c'est la présence
        # SIMULTANÉE des deux étiquettes : mesuré avant d'écrire la branche, deux
        # sociétés sur soixante-deux les déposent, et ce sont deux banques.
        _pin, _pcom = e.get("produit_interet_net"), e.get("produit_commissions")
        _deja = (e.get("revenue") is not None
                 and e.get("revenue_contrats") is not None
                 and e["revenue"] == e["revenue_contrats"])
        if (_pin is not None and _pcom is not None
                and (_deja or e.get("revenue") is None)):
            e["revenue"] = _pin + _pcom
            e["revenue_total_utilise"] = "produit net bancaire"

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
            # ⚠ NE SOUSTRAIRE LES MINORITAIRES QUE SI `equity` NE LES CONTIENT PAS.
            # Le dictionnaire autorise un repli sur l'étiquette « including
            # portion attributable to noncontrolling interest », qui les inclut
            # déjà : les retirer une seconde fois creuserait le passif. Mesuré :
            # 22 exercices reconstruits à tort, jusqu'à 8,7 % d'écart.
            # Le témoin `equity_part_groupe` porte la seule étiquette exclusive ;
            # s'il a une valeur, c'est elle que l'union a retenue.
            minoritaires = ((e.get("interets_minoritaires_bilan") or 0.0)
                            if e.get("equity_part_groupe") is not None else 0.0)
            e["liabilities"] = (e["assets"] - e["equity"] - minoritaires
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
        # À défaut, le décaissement NET — et son signe est porteur : négatif
        # signifie que la société a vendu plus d'actifs qu'elle n'en a acheté.
        # On ne prend donc PAS sa valeur absolue, et on marque l'exercice : un
        # cash libre calculé sur un investissement net n'est pas le même chiffre
        # que sur un investissement brut, et la fiche doit pouvoir le dire.
        elif e.get("capex_net") is not None:
            e["capex"] = e["capex_net"]
            e["capex_net_utilise"] = True
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
        # ── LES BAUX : SOMME EXPLICITE DES DEUX NATURES ──
        # `None + None` reste `None` : une société qui ne dépose aucun bail n'a
        # pas zéro bail, elle n'en a pas. C'est la différence entre « mesuré à
        # zéro » et « non mesuré », et elle se propage jusqu'à la dette totale.
        for _long, _a, _b in (("lease_lt", "bail_simple_lt", "credit_bail_lt"),
                              ("lease_ct", "bail_simple_ct", "credit_bail_ct")):
            _x, _y = e.get(_a), e.get(_b)
            e[_long] = None if (_x is None and _y is None) else ((_x or 0) + (_y or 0))

        # ── UNE DETTE FAITE DE LOYERS SEULS N'EST PAS UNE DETTE ──
        #
        # Le repli international ne peut pas s'armer sur « dette_totale est
        # nulle » : dès qu'un déposant porte un bail, le total cesse d'être nul —
        # il vaut LES LOYERS SEULS — et le repli ne s'arme jamais. Shell
        # publierait 28 933 M$ au lieu de 104 576, sans erreur et avec l'air
        # complet. On regarde donc si UNE LIGNE D'EMPRUNT est servie.
        # LE TOTAL PRIME SUR LES COMPOSANTES quand la société le dépose.
        # `Borrowings` est le total des emprunts ; la somme des deux échéances
        # l'oublie partiellement — chez Shell, `ShorttermBorrowings` (506) ne
        # compte pas la part à moins d'un an des emprunts longs (9 128), et la
        # reconstruction par composantes lui volait 8,6 Md$ EN SILENCE. Mais
        # `Borrowings` est parfois absent ou périmé (Chunghwa Telecom, ChipMOS,
        # Sify) : à cette date-là, la somme des composantes reprend la main.
        _emp = e.get("emprunts_ifrs")
        if not _emp:
            # Osisko porte un `Borrowings` de ZÉRO : le `not` l'attrape, et la
            # somme des composantes prend le relais. `None` quand aucune
            # composante n'existe — pas zéro : « pas d'emprunt déclaré » et
            # « emprunt nul » ne se distinguent qu'ici.
            _lt, _ct = e.get("lt_debt"), e.get("current_debt")
            _emp = ((_lt or 0) + (_ct or 0)) if (_lt is not None or _ct is not None) else None

        # Les baux, du total international ou de la somme des deux natures.
        _bx = e.get("baux_ifrs")
        if _bx is None and (e.get("lease_lt") is not None or e.get("lease_ct") is not None):
            _bx = (e.get("lease_lt") or 0) + (e.get("lease_ct") or 0)

        # ⚠ UNE DETTE FAITE DE LOYERS SEULS EST UNE DETTE — quand la société
        # n'a pas d'emprunt. J'avais écrit l'inverse, et la mesure l'a dit :
        # 1 023 sociétés perdaient leur dette, dont Five Below (2 032 M$ de baux,
        # aucun emprunt), Landstar, Kennametal, Weis Markets. Par cascade, 4 384
        # exercices perdaient leur ROIC.
        #
        # Ce qui était vrai dans la règle : Shell ne doit pas publier 28 933 M$.
        # Ce qui était faux : la cause n'était pas la nature de sa dette, c'était
        # que ses EMPRUNTS n'étaient pas lus. Depuis que `Borrowings` est au
        # dictionnaire, elle rend 104 576 sans qu'aucun refus soit nécessaire.
        # Corriger la conséquence au lieu de la cause coûtait mille sociétés.
        dette_totale = (None if (_emp is None and _bx is None)
                        else ((_emp or 0) + (_bx or 0)))

        # ⚠ AUCUNE SOCIÉTÉ NE DOIT PLUS QUE CE QU'ELLE POSSÈDE. Grupo
        # Aeroportuario del Sureste 2017 déclare 7 149 177 000 000 pesos
        # d'emprunts longs pour 56 614 103 000 d'actif — 126 fois. La SEC porte
        # deux points pour cette date, l'un juste et l'autre avec trois zéros de
        # trop, et la règle « le dépôt le plus récent gagne » choisit le faux.
        # Une dette qui dépasse l'actif ne se corrige pas : elle s'efface.
        if (dette_totale is not None and e.get("assets")
                and dette_totale > 10 * abs(e["assets"])):
            e["_dette_effacee"] = dette_totale
            dette_totale = None
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

    # ── UNE ANNÉE VIDE N'EST PAS UN EXERCICE ──
    # Contrepartie de l'ossature en union : elle admet une année dès qu'un
    # résultat net y est déposé. C'est ce qui a rendu à Investar et à Goldman
    # Sachs les années où le bilan était là sans le chiffre d'affaires — un vrai
    # gain. Mais elle admet aussi les tableaux de « pertes depuis la création »
    # que déposent les sociétés en phase de développement, où une année n'est
    # qu'une ligne d'annexe. Mesuré après la collecte du 28/08/2026 : 8 exercices
    # sur 43 513, chez 6 sociétés, ne portaient NI bilan, NI flux, NI revenus,
    # NI résultat par action. Peu — mais ces années-là gonflent le compteur
    # d'exercices de la fiche, qui promet alors une profondeur inexistante.
    ANCRES_EXERCICE = ("revenue", "assets", "ocf", "equity", "liabilities",
                       "eps_diluted")
    exercices = [e for e in exercices
                 if any(isinstance(e.get(k), (int, float)) for k in ANCRES_EXERCICE)]
    if not exercices:
        return None

    # Puis ce qui est logiquement impossible — un résultat brut au-dessus du
    # chiffre d'affaires, un poste au-dessus de son propre total. Avant tout
    # calcul : une marge tirée d'un couple impossible est fausse, et elle n'a
    # plus l'air de rien une fois arrondie à deux décimales.
    impossibles = effacer_l_impossible(exercices)

    # L'unité du nombre d'actions ensuite : McDonald's portait 716,4 actions là
    # où il en faut 716,4 millions. Ce facteur traverse la capitalisation, la
    # valeur d'entreprise, le coût du capital et toutes les grandeurs par
    # action — le corriger après serait le corriger nulle part.
    unites_actions = _corriger_unite_actions(exercices)

    # Le dividende par action ENSUITE, et avant la recouture des divisions : il se
    # confronte au montant total verse divise par le nombre d actions, donc les
    # deux doivent etre sur la meme base — brute, telle que deposee.
    dps_redresses = redresser_dividende_par_action(exercices)

    # Les divisions d'action enfin : tout ce qui suit se calcule « par action »
    # et serait faux sur une série non recousue.
    # Les facteurs LUS dans les millesimes, quand les depots en portent. Ils
    # REMPLACENT l inference : le rapport entre deux depots d un meme exercice est
    # exact, celui entre deux exercices voisins vaut « facteur x derive de
    # l annee » — et c est cette derive qui faisait rater O Reilly d un dixieme
    # de point de tolerance, CSX d un rachat, Tesla d une dilution.
    divisions = _corriger_divisions(
        exercices,
        facteurs_lus={fin: f for fin, (f, _src) in facteurs_de_division(facts, devise).items()},
        # ⚠ L'ABSENCE DE MILLÉSIME DIVERGENT EST UNE INFORMATION, ET C'EST
        # L'INVERSE DE CE QU'ON EN FAISAIT. Un exercice redéposé À L'IDENTIQUE
        # après une date n'a connu aucune division depuis — une division retraite
        # TOUJOURS les exercices antérieurs. Sans cette table, l'inférence lisait
        # chez VNET Group une division ×2 en 2024 et rebasait treize ans
        # d'historique par action, alors que l'exercice 2023 vaut 901 143 138
        # actions diluées dans les dépôts de 2024, 2025 ET 2026.
        confirmes=exercices_confirmes(facts, devise))

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

    # ── LA CAPITALISATION HISTORIQUE QUE LA SÉRIE DE COURS NE COUVRE PAS ──
    #
    # Seuls les huit cents titres du tracker ont une vraie série de cours. Pour
    # tous les autres, la boucle ci-dessus n'a posé une capitalisation que sur le
    # DERNIER exercice — celle d'aujourd'hui, faute de mieux — et le coût du
    # capital n'avait donc qu'une barre au bord droit d'un graphique de vingt.
    #
    # Les fichiers de marché portent la variation du cours sur un, trois, cinq et
    # dix ans : quatre ancres, dont on déduit le rapport entre le cours d'alors
    # et celui d'aujourd'hui. Un rapport suffit, puisqu'on TRANSPORTE la
    # capitalisation du dernier exercice au lieu de recalculer un montant.
    #
    # ⚠ ET ON REPASSE `_wacc` SUR CE QU'ON VIENT DE COMBLER : le coût du capital
    # se calcule dans la boucle ci-dessus, donc avant que ces capitalisations
    # n'existent. Sans ce second passage, l'exercice gagnerait sa capitalisation
    # et pas le coût du capital qui en dépend — le contraire du but.
    if combler_mcap_par_ancres(exercices, variations, jour_marche):
        for e in exercices:
            if e.get("mcap_source") != "ancre":
                continue
            e["wacc"] = _wacc(e.get("mcap_estime"), e.get("dette_totale"),
                              e.get("interest_expense"), e.get("_taux_nopat"), beta)
            e["roic_moins_wacc"] = (
                round(e["roic"] - e["wacc"], 2)
                if (e.get("roic") is not None and e.get("wacc") is not None)
                else None)

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
    # AVANT la note, pas apres : un ratio qu'on s'apprete a declarer non
    # mesurable ne doit pas d'abord rapporter un point. Un ROIC de 804 436 %
    # rapportait le point plein sur les trois criteres de rentabilite.
    ecarter_ratios_degeneres(resume)
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
        # Ici on EFFACE, et la fiche a le droit de l'annoncer tel quel — à la
        # différence de la filière internationale, qui convertit au taux daté de
        # la clôture. Le front lit ce champ plutôt que de deviner.
        resume["montants_marche"] = "ecartes"
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
        # ── LES VARIATIONS DE COURS, POUR LA CAPITALISATION HISTORIQUE ──
        #
        # ch1y, ch3y, ch5y, ch10y donnent le rapport entre le cours d'alors et
        # celui d'aujourd'hui — quatre ancres qui font passer le coût du capital
        # d'une barre à trois pour la plupart des sociétés. Elles sont dans ce
        # fichier depuis toujours et personne ne les lisait.
        #
        # ⚠ `genere_le` ET NON `updated` : les fichiers de marché datent leur
        # écriture sous ce nom-là. Chercher `updated` rend None sans lever
        # d'erreur, et la reconstruction se tait entièrement.
        i_ch = {n: ch.index(n) for n in ("ch1y", "ch3y", "ch5y", "ch10y") if n in ch}
        jour_m = (d.get("genere_le") or d.get("updated") or "")[:10] or None
        for sym, v in (d.get("societes") or {}).items():
            # Un ticker à suffixe n'est pas américain ; un chemin non plus.
            if "." in sym or "/" in sym or sym not in principales:
                continue
            var = cours_ancres(
                1.0,
                *[v[i_ch[n]] if (n in i_ch and i_ch[n] < len(v)) else None
                  for n in ("ch1y", "ch3y", "ch5y", "ch10y")])
            lignes.append((v[i_capi] or 0, sym, v[i_nom],
                           v[i_ind] if i_ind is not None else None,
                           var or None, jour_m))
    lignes.sort(reverse=True)
    if tranche:
        i, n = tranche
        lignes = [x for x in lignes if int(_initiale(x[1])) % n == i]

    out = {}
    for capi, sym, nom, ind, var, jour_m in lignes:
        out[sym] = {"nom": nom, "mcap": capi, "secteur_suivi": ind,
                    "cours_cotation": cotations.get(sym),
                    # Les quatre ancres de cours et la date du fichier qui les
                    # porte : sans elles, `combler_mcap_par_ancres` se tait.
                    "variations": var, "jour_marche": jour_m}

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
            print("[info] bêta connu pour %d sociétés (cache sectoriel)" % n_beta)
        except Exception as e:
            print("[warn] bêtas illisibles : %s" % e, file=sys.stderr)
    combler_beta_marche(out)
    return out


def combler_variations_marche(cible):
    """Range les quatre ancres de cours et la date du fichier dans chaque `meta`.

    Jumelle de `combler_beta_marche` : même parcours, même source, autre colonne.
    Elle sert l'univers du tracker, que `univers_marche` ne construit pas.
    """
    import glob as _g
    n = 0
    try:
        for pth in sorted(_g.glob(str(CACHE_DIR / "marche_[0-9]*.json"))):
            try:
                with open(pth, encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception:
                continue
            ch = d.get("champs") or []
            i_ch = {x: ch.index(x) for x in ("ch1y", "ch3y", "ch5y", "ch10y")
                    if x in ch}
            if not i_ch:
                continue
            jour_m = (d.get("genere_le") or d.get("updated") or "")[:10] or None
            for sym, v in (d.get("societes") or {}).items():
                e = cible.get(sym)
                if e is None or e.get("variations"):
                    continue
                var = cours_ancres(
                    1.0,
                    *[v[i_ch[x]] if (x in i_ch and i_ch[x] < len(v)) else None
                      for x in ("ch1y", "ch3y", "ch5y", "ch10y")])
                if var:
                    e["variations"] = var
                    e["jour_marche"] = jour_m
                    n += 1
    except Exception as exc:
        print("[warn] variations de marché illisibles : %s" % exc, file=sys.stderr)
    if n:
        print("[info] ancres de cours pour %d société(s)" % n)
    return n


def combler_beta_marche(cible):
    """Complète le bêta manquant depuis les fichiers de marché. Ne l'écrase JAMAIS.

    Le cache sectoriel `tradfi_fundamentals_cache.json` ne porte que ~820
    titres : au-delà, pas de bêta, donc pas de coût des fonds propres, donc pas
    de WACC — et la tuile « rendement du capital contre son coût » reste éteinte
    alors que c'est la seule qui dise si la croissance crée de la valeur.

    Les fichiers `marche_NN.json` portent le bêta de 71,9 % des 37 574
    cotations, et ce collecteur les ouvre déjà. C'est exactement le défaut
    corrigé côté international, où le bêta dormait à l'index 19.

    ⚠ On ne comble QUE les vides. Le cache sectoriel est la source historique :
    changer une valeur déjà servie serait un effet de bord non demandé, et un
    bêta qui bouge fait bouger un jugement de création de valeur.

    ⚠ Un bêta n'est pas un montant : aucune conversion de devise ici. Le bêta
    est un rapport de variations, il est sans unité — c'est justement pour ça
    qu'il traverse les places sans être touché.
    """
    import glob as _g
    n = 0
    try:
        for pth in sorted(_g.glob(str(CACHE_DIR / "marche_[0-9]*.json"))):
            try:
                with open(pth, encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception:
                continue
            ch = d.get("champs") or []
            if "beta" not in ch:
                continue
            i_b = ch.index("beta")
            for sym, v in (d.get("societes") or {}).items():
                e = cible.get(sym)
                if e is None or e.get("beta") is not None:
                    continue
                # `beta_plausible` refuse le zéro exact et tout ce qui sort de
                # ±8 : un bêta de 1 877 donnerait un coût des fonds propres de
                # 9 400 %, et la tuile afficherait une destruction de valeur
                # inventée. La règle est dans `fondamentaux_communs`, avec le
                # relevé qui a fixé la bande.
                b = beta_plausible(v[i_b] if i_b < len(v) else None)
                if b is not None:
                    e["beta"] = b
                    n += 1
    except Exception as e:
        print("[warn] bêtas de marché illisibles : %s" % e, file=sys.stderr)
    if n:
        print("[info] bêta comblé pour %d société(s) depuis les fichiers de marché" % n)
    return n


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
            print("[info] bêta connu pour %d sociétés (cache sectoriel)" % n_beta)
        except Exception as e:
            print("[warn] bêtas illisibles : %s" % e, file=sys.stderr)
    combler_beta_marche(univers)
    # Les ancres de cours, pour que l'univers du tracker en bénéficie aussi :
    # ses huit cents titres ont une vraie série de cours, mais pas tous, et un
    # champ vide vaut mieux qu'une branche qui ne s'exécute jamais.
    combler_variations_marche(univers)
    return univers


# ── LES SOCIÉTÉS RECOIFFÉES D'UNE HOLDING NEUVE ───────────────────────────
#
# Quand une société se coiffe d'une holding, la SEC enregistre une entité neuve
# et fait pointer le ticker vers elle. On récolte alors l'histoire de la
# holding — deux ou trois ans — pendant que des décennies de dépôts restent
# sous l'ancien CIK. Aucune erreur : la collecte réussit et la fiche paraît
# n'avoir aucun passé.
#
# Une table NOMMÉE, pas une règle : une règle automatique qui se tromperait
# rattacherait une société à l'histoire d'une AUTRE, et produirait des états
# financiers parfaitement formés et entièrement faux. Chaque entrée est prouvée
# avant d'être écrite — ordre de grandeur du chiffre d'affaires, et concordance
# sur les exercices que les deux entités couvrent.
#
#   XOM  34088   « EXXON MOBIL CORP » — 19 exercices 2007→2025, CA 302 à 350 Md$.
#                Le CIK pointé par le ticker (2115436) rend ZÉRO exercice.
#   BLK  1364742 EDGAR le nomme « BlackRock Finance, Inc. » et liste « BlackRock,
#                Inc. » parmi ses anciens noms — le nom que porte maintenant le
#                CIK 2012383, anciennement « BlackRock Funding, Inc. ». Les deux
#                se sont échangé le nom. Et sur 2022 et 2023, que les deux
#                couvrent, elles annoncent le même CA : 18 Md$.
CIK_HISTORIQUE = {
    "XOM": "0000034088",
    "BLK": "0001364742",
}


def fusionner_faits(vieux, neuf):
    """Empile les faits de deux entités ; on ne choisit rien ici.

    L'ancien d'abord, le neuf ensuite. C'est `dedupliquer_exercices` qui tranche
    ensuite, avec la règle qu'elle applique déjà partout : la version la plus
    GARNIE gagne, puis la plus récemment déposée. Choisir ici reviendrait à
    inventer une seconde règle, qui divergerait un jour de la première.
    """
    out = {}
    for source in (vieux, neuf):
        for taxo, concepts in (source or {}).items():
            d1 = out.setdefault(taxo, {})
            for concept, corps in concepts.items():
                d2 = d1.setdefault(concept, {"units": {}})
                for k, v in corps.items():
                    if k != "units":
                        d2.setdefault(k, v)
                for unite, lignes in (corps.get("units") or {}).items():
                    d2["units"].setdefault(unite, []).extend(lignes)
    return out


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


def _index_precedent():
    """Les sociétés de l'index déjà publié, {symbole: métadonnées}.

    Sert à réinjecter dans l'univers ce qui a été publié une fois : une société
    qui sort de la source ne doit pas pour autant cesser d'être vérifiée.
    """
    if not OUT_JSON.exists():
        return {}
    try:
        with OUT_JSON.open(encoding="utf-8") as fh:
            return json.load(fh).get("societes") or {}
    except Exception:
        return {}


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
        # ── UNE SOCIÉTÉ DÉJÀ PUBLIÉE RESTE RAFRAÎCHISSABLE ──
        # L'univers vient d'une source extérieure ; une société peut en sortir
        # sans que nous le sachions. Son paquet, lui, reste publié : la fusion le
        # reporte d'une collecte à l'autre, indéfiniment. La fiche s'affiche, les
        # chiffres ont l'air normaux, et ils vieillissent — la panne muette dans
        # sa forme la plus difficile à voir.
        #
        # Mesuré le 28/08/2026 : deux sociétés dans ce cas, mais lesquelles —
        # Berkshire Hathaway (1 078 Md$) et AvalonBay (26 Md$). AvalonBay avait
        # en prime un chiffre d'affaires faux, et aucune collecte ne pouvait plus
        # le corriger.
        #
        # On réinjecte donc tout symbole déjà présent dans l'index précédent. Le
        # coût est nul — deux sociétés aujourd'hui — et le principe tient pour
        # toujours : ce que nous publions, nous continuons de le vérifier.
        anciens = _index_precedent()
        rendus = 0
        for sym, meta in anciens.items():
            if sym in univers or "." in sym or "/" in sym:
                continue
            if opts["tranche"]:
                i, n = opts["tranche"]
                if int(_initiale(sym)) % n != i:
                    continue
            univers[sym] = {"nom": meta.get("nom"), "mcap": meta.get("mcap"),
                            "secteur_suivi": meta.get("secteur"),
                            "cours_cotation": None}
            rendus += 1
        if rendus:
            print("[info] %d société(s) déjà publiée(s) réinjectée(s) — "
                  "elles avaient quitté l'univers de la source" % rendus)

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
    # Un compteur ne se répare pas : on garde les NOMS. Quatre chemins, qui ne
    # se soignent pas pareil — et « construction refusée » est le seul où c'est
    # notre code qui dit non à une donnée existante.
    perdus = {"sans_cik": [], "reseau": [], "faits_vides": [], "construction": []}
    recoiffees = []
    interrompu = False
    for i, (sym, meta) in enumerate(sorted(univers.items()), 1):
        if interrompu:
            break
        cik = cik_par_ticker.get(sym.upper())
        if not cik:
            sans_cik += 1
            perdus["sans_cik"].append(sym)
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
            perdus["reseau"].append("%s (%s)" % (sym, str(e)[:60]))
            continue
        faits = (facts_doc or {}).get("facts")

        # ── L'HISTOIRE SOUS L'ANCIEN CIK ──
        # Pour les sociétés recoiffées d'une holding neuve, on va chercher les
        # faits de l'entité d'origine et on les empile avant ceux de l'entité
        # actuelle. Un échec ici n'est pas fatal : on publie ce qu'on a plutôt
        # que rien, mais on le DIT — un rattrapage qui échoue en silence
        # laisserait croire que la société n'a pas de passé.
        vieux_cik = CIK_HISTORIQUE.get(sym.upper())
        if vieux_cik:
            try:
                doc2 = _get("https://data.sec.gov/api/xbrl/companyfacts/"
                            f"CIK{vieux_cik}.json", accept_404=True)
                if doc2 and doc2.get("facts"):
                    faits = fusionner_faits(doc2["facts"], faits or {})
                    recoiffees.append(sym)
                else:
                    print(f"[warn] {sym} : CIK historique {vieux_cik} sans faits",
                          file=sys.stderr)
            except DelaiGlobalAtteint:
                raise
            except Exception as e:
                print(f"[warn] {sym} : CIK historique illisible : {e}", file=sys.stderr)

        if not faits:
            # La SEC a répondu, mais sans données XBRL. À distinguer d'un refus
            # de NOTRE code : celui-là se répare, celui-ci non.
            echecs += 1
            perdus["faits_vides"].append(sym)
            continue
        try:
            bati = construire(faits, meta.get("mcap"),
                              beta=meta.get("beta"), cours=cours.get(sym),
                              variations=meta.get("variations"),
                              jour_marche=meta.get("jour_marche"))
        except Exception as e:
            print(f"[warn] {sym} : construction impossible : {e}", file=sys.stderr)
            echecs += 1
            perdus["construction"].append("%s (exception: %s)" % (sym, str(e)[:60]))
            continue
        if not bati:
            # LE CHEMIN QUI COMPTE : les faits sont la, et c'est NOTRE code qui
            # les refuse. Le plus frequent, le plus reparable — c'est par ici que
            # passait BBVA, faute d'une etiquette IFRS dans l'ossature.
            echecs += 1
            perdus["construction"].append(sym)
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
    if recoiffees:
        print("[ok] histoire rattachée depuis le CIK d'origine pour %d société(s) : %s"
              % (len(recoiffees), ", ".join(sorted(recoiffees))))

    # ── LE DÉTECTEUR ──
    # `CIK_HISTORIQUE` est figée et ne verra pas la réorganisation suivante. Le
    # défaut ne produit aucune erreur : la collecte réussit et la fiche paraît
    # seulement pauvre. On NOMME donc les grosses capitalisations presque vides.
    # La plupart auront une bonne raison — scission récente, classe d'actions
    # secondaire, émetteur étranger déposant un 20-F. Deux ou trois n'en auront
    # pas : ce sont celles-là qu'on cherche, et un humain tranchera.
    maigres = []
    for sym, meta in univers.items():
        m = meta.get("mcap") or 0
        if m < 30e9 or sym.upper() in CIK_HISTORIQUE:
            continue
        bloc = index.get(sym) or {}
        n = bloc.get("n_exercices")
        if n is None:
            n = 0
        if n < 6:
            maigres.append((m, sym, n))
    if maigres:
        maigres.sort(reverse=True)
        print("[?] %d société(s) de plus de 30 Md$ rendues avec moins de six "
              "exercices — à regarder si l'une d'elles a été recoiffée d'une "
              "holding neuve :" % len(maigres))
        for m, sym, n in maigres[:12]:
            print("      %-7s %6.0f Md$  %d exercice(s)" % (sym, m / 1e9, n))
        if len(maigres) > 12:
            print("      … et %d autre(s)" % (len(maigres) - 12))

    # ── LES PERDUS, DANS UN FICHIER À PART ──
    #
    # PAS dans `exhaustivite`. `_fusionner_sec` recoud l'index et les paquets,
    # mais reconstruit `exhaustivite` à partir des seuls compteurs du passage
    # COURANT. C'est déjà visible dans l'index publié, qui annonce « univers 2,
    # construites 2, échecs 0 » alors que 3 463 sociétés sont publiées — trace
    # d'une passe à deux tickers.
    #
    # Y ajouter les listes ferait dire à une passe partielle « aucun échec »,
    # ce qui est pire que se taire. Le fichier séparé porte donc la taille de
    # l'univers auquel il se rapporte, et dit s'il vient d'une passe partielle.
    try:
        with (OUT_DIR / "sec_echecs.json").open("w", encoding="utf-8") as fh:
            json.dump({
                "genere_le": horodatage,
                "univers": len(univers),
                "partielle": bool(opts.get("tickers") or opts.get("tranche")),
                "note": ("Diagnostic, hors manifeste. Quatre chemins qui ne "
                         "se soignent pas pareil : « sans_cik » est souvent "
                         "légitime (hors marché réglementé, ADR non parrainé), "
                         "« reseau » se retente, « faits_vides » veut dire que "
                         "la SEC répond sans données XBRL — et « construction » "
                         "est le seul où les faits EXISTENT et où notre propre "
                         "code les refuse. C'est celui-là qu'il faut regarder."),
                "compte": {k: len(v) for k, v in perdus.items()},
                "perdus": perdus,
            }, fh, ensure_ascii=False, indent=1)
        print("[ok] echecs nommes dans sec_echecs.json : %s"
              % ", ".join("%s %d" % (k, len(v)) for k, v in sorted(perdus.items()) if v))
    except OSError as e:
        print("[warn] sec_echecs.json non ecrit : %s" % e, file=sys.stderr)

    print(f"[ok] index : {OUT_JSON.stat().st_size // 1024} Ko")
    if poids:
        print(f"[ok] {len(poids)} paquet(s) de détail — "
              f"plus gros {max(poids) // 1024} Ko, total {sum(poids) // 1024} Ko")
    return 0


if __name__ == "__main__":
    sys.exit(main())
