#!/usr/bin/env python3
"""États financiers des sociétés cotées HORS États-Unis.

POURQUOI CE SECOND COLLECTEUR
Le collecteur SEC couvre 315 sociétés américaines sur les 800 suivies. LVMH,
Toyota, ASML, Nestlé, Reliance, Samsung, Tencent n'y sont pas : elles ne déposent
pas auprès de la SEC. Une fiche société qui marche pour Apple et pas pour L'Oréal
ne vaut rien. Ce collecteur ramène les 444 autres.

LA SOURCE, ET POURQUOI ELLE PLUTÔT QU'UNE AUTRE
`stockanalysis.com` sert ses pages d'états financiers en JSON, gratuitement, sans
clé et sans compte. Mesuré le 2026-08-27 sur l'univers réel :
  · 430 sociétés sur 444 répondent — 96,8 %, toutes places confondues ;
  · 5 exercices par société, uniformément (le site en détient dix, `full_count`
    le dit, mais n'en sert que cinq à un visiteur anonyme) ;
  · 40 à 48 lignes de compte de résultat, 57 à 65 de bilan, 39 à 46 de flux ;
  · 80 requêtes en rafale sans une seule limitation, ~7 minutes pour tout.

yfinance a été mesuré en face : 4 exercices exploitables seulement, dividende par
action irrécupérable (trois sources internes qui se contredisent), R&D absente
pour les groupes japonais et chinois qui la publient pourtant, et trois cas où la
devise des états diffère de celle de la cotation. Écarté comme source principale.

CONTRÔLE DE JUSTESSE, contre une référence indépendante : ASML dépose aussi
auprès de la SEC (formulaire 20-F). Chiffre d'affaires, résultat net, marge
brute, total du bilan et capitaux propres sont IDENTIQUES AU CENTIME sur les cinq
exercices comparés. Une seule divergence, le résultat d'exploitation 2021 à
−3,2 % : c'est une ligne RETRAITÉE par le fournisseur de la source. D'où la règle
ci-dessous.

TROIS SOURCES, ET CHAQUE EXERCICE DIT LAQUELLE (ajouté le 2026-09-05)
La source ci-dessus ne sert que CINQ exercices à un visiteur anonyme, et ce n'est
pas un réglage : treize formes de paramètres, quatre cookies, trois en-têtes et
vingt places de cotation ont été essayés — 20 sur 20 rendent six colonnes, TTM
plus cinq. C'est un péage. L'international restait donc à cinq exercices quand la
SEC en donne jusqu'à trente-quatre, et toutes les médianes « dix ans » de
l'international étaient muettes par construction.

Deux compléments les remplissent, et chacun a sa faiblesse écrite dans la sortie :
  · LE SCANNER TRADINGVIEW sert des séries annuelles gratuites sous les colonnes
    `_fy_h`. Mesuré sur mille sociétés tirées au hasard : 93,7 % répondent,
    profondeur MÉDIANE 18 exercices, 41,1 % en ont vingt. Mais il ne sert que
    DOUZE grandeurs — pas le passif, pas les capitaux propres, pas le flux
    d'exploitation, pas le nombre d'actions — et ses montants sont dans la devise
    de la LIGNE COTÉE, pas dans celle des états. Voir `exercices_tradingview`.
  · NOS PROPRES PAQUETS. La fenêtre de la source principale GLISSE : l'exercice
    2020 qu'elle servait l'an dernier n'existe plus que chez nous. On le relit
    avant de construire, au lieu de l'écraser comme le faisait la fusion de fin
    de passage.

CE QU'IL FAUT SAVOIR AVANT DE LIRE CES CHIFFRES, et qui est écrit dans la sortie
  · LA PROFONDEUR EST INÉGALE, et les exercices anciens sont plus MAIGRES que les
    récents : cinq exercices complets, puis jusqu'à vingt exercices qui n'ont ni
    bilan complet ni tableau de flux. La fiche doit l'assumer plutôt que la
    masquer — `resume.sources_exercices` la compte source par source.
  · LES MONTANTS SONT EN DEVISE NATIVE, jamais convertis. Convertir un bilan de
    2021 au cours d'aujourd'hui produirait un nombre qui n'a jamais existé.
    Les marges, rendements et croissances, eux, ne dépendent pas de la devise —
    c'est l'essentiel de ce que la fiche montre.
  · LA SOURCE N'EST PAS PRIMAIRE. Les données viennent de S&P Global Market
    Intelligence, revendues par le site. Les lignes « dures » (chiffre
    d'affaires, résultat net, actif, capitaux propres) sont les comptes publiés ;
    les lignes « composées » (résultat d'exploitation, EBITDA) sont des
    retraitements du fournisseur. On garde le lien vers la page à chaque fois.
  · LES DIVISIONS D'ACTION SONT DÉJÀ RÉTRO-AJUSTÉES par la source, sur tous les
    exercices. Les bénéfices par action divergent donc des rapports annuels
    publiés à l'époque. C'est cohérent en interne, et c'est signalé.

LE PIÈGE QUI A ÉTÉ TROUVÉ EN CHEMIN, et qui touche le dépôt aujourd'hui :
`fetch_tradfi_hist.py` interroge la route `/financials/` pour l'international.
Cette route n'est PLUS le compte de résultat — c'est devenue une page de synthèse
où `financialData` vaut −1. Le garde-fou de schéma se déclenche donc pour TOUTES
les sociétés internationales, et le script se rabat en silence sur son cache
figé. Le compte de résultat a déménagé sur `/financials/income-statement/`.
"""
import signal as _signal, sys as _sys
def _global_timeout_handler(signum, frame):
    print("[fatal] délai global (25 min) atteint — abandon.", file=_sys.stderr)
    _sys.exit(2)
try:
    _signal.signal(_signal.SIGALRM, _global_timeout_handler)
    _signal.alarm(90 * 60)
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
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from fondamentaux_communs import (
    ecarter_ratios_degeneres,          # noqa: E402
    annee_exercice,
    dedupliquer_exercices,
    _div, _pct, _r,
    note_quantitative,
    _mediane, _mediane_fenetre, _croissances, _predictibilite,
    _serie_sans_baisse_dividende, _serie_hausses_dividende,
    _corriger_divisions, _piotroski, _altman_z, _wacc,
    beta_plausible,
    cours_ancres, cours_a_la_date,
    effacer_l_impossible,
    redresser_dividende_par_action,
    _taux_impot_reel, _taux_pour_nopat, _charge, _corriger_unite_actions,
)

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR = CACHE_DIR
OUT_JSON = CACHE_DIR / "intl_fundamentals_index.json"
OUT_JS = CACHE_DIR / "intl_fundamentals_index.js"
TRACKER_CACHE = CACHE_DIR / "tradfi_cache.json"

BASE = "https://stockanalysis.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
# Aucune limitation constatée à 4 requêtes par seconde sur 80 essais. On reste
# nettement en dessous : la source est gratuite et ne nous doit rien.
DEBIT = 0.35
TIMEOUT = 25
RETRIES = 3
_last = [0.0]

# Les deux leviers de profondeur, débrayables SÉPARÉMENT — pas par confort, mais
# parce qu'ils coûtent des choses différentes et tombent en panne séparément.
# `AVEC_RATIOS` ajoute une cinquième requête PAR SOCIÉTÉ à la source principale
# (+25 % de réseau) ; `AVEC_TV` ajoute une dizaine de requêtes EN TOUT chez un
# second fournisseur. Le jour où l'un des deux se ferme, on coupe celui-là et
# la collecte continue au lieu de s'arrêter en entier.
AVEC_RATIOS = True
AVEC_TV = True


# Combien de fois la source nous a dit « trop vite » ET qu'on a fini par
# abandonner. Un plafond de débit n'est pas une page inexistante : les confondre
# fait passer une collecte bridée pour une collecte finie.
_bridages = [0]


def _get(url, accept_404=True):
    for essai in range(RETRIES):
        d = time.time() - _last[0]
        if d < DEBIT:
            time.sleep(DEBIT - d)
        _last[0] = time.time()
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept-Encoding": "gzip",
            "Accept": "application/json,text/plain,*/*",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code in (404, 403, 410):
                return None            # la page n'existe pas, insister ne sert à rien
            if e.code == 429:
                # Un plafond de débit ne se force pas, il s'attend. Mille deux
                # cents millisecondes n'ont jamais suffi ; on part de cinq
                # secondes et on double, ou on suit l'en-tête quand il est là.
                if essai == RETRIES - 1:
                    _bridages[0] += 1
                    return None
                pause = 5.0 * (2 ** essai)
                try:
                    ra = e.headers.get("Retry-After")
                    if ra:
                        pause = max(pause, float(ra))
                except Exception:
                    pass
                time.sleep(min(pause, 60.0))
                continue
            if essai == RETRIES - 1:
                return None
            time.sleep(1.2 * (essai + 1))
        except Exception:
            if essai == RETRIES - 1:
                return None
            time.sleep(1.2 * (essai + 1))
    return None


# ─────────────────────────────────────────────────────────────────────────
# Correspondance suffixe de cotation → code de place du site
# ─────────────────────────────────────────────────────────────────────────
# Reprise de fetch_tradfi_hist.py, avec TROIS corrections vérifiées une par une
# le 2026-08-27 : « ose » ne désigne pas Oslo (c'est « osl » — Equinor rendait
# zéro période), « set » ne désigne pas Bangkok (c'est « bkk » — PTT idem), et
# Dubaï manquait entièrement.
PLACES = {
    "L": "lon", "HK": "hkg", "PA": "epa", "DE": "etr", "SW": "swx", "MC": "bme",
    "MI": "bit", "AS": "ams", "BR": "ebr", "HE": "hel", "CO": "cph", "OL": "osl",
    "ST": "sto", "TO": "tsx", "V": "tsxv", "T": "tyo", "KS": "krx", "KQ": "krx",
    "TW": "tpe", "TWO": "tpe", "NS": "nse", "BO": "bom", "SI": "sgx", "AX": "asx",
    "SS": "sha", "SZ": "she", "JK": "idx", "JO": "jse", "BK": "bkk", "KL": "klse",
    "SR": "tadawul", "AE": "adx", "DU": "dfm", "MX": "bmv", "SA": "bvmf",
    "PS": "pse", "VI": "vie", "LS": "eli", "IR": "ise", "WA": "wse", "PR": "pse",
    "TA": "tase", "IS": "bist", "CN": "cse", "NE": "neo", "F": "fra",
}


# Les adresses que la règle générale ne peut pas deviner. Ce ne sont pas des
# variantes de format mais des conventions de place, chacune vérifiée en
# appelant la page : Roche rend 370,31 CHF sur 18 analystes sous swx/ROP,
# Maybank 11,77 MYR sur 19 sous klse/MAYBANK. Sans cette table, cinq sociétés
# de l'univers passaient pour absentes de la source alors qu'elle les servait.
EXCEPTIONS_CHEMIN = {
    "ROG.SW":        "quote/swx/ROP",        # Roche, action au porteur
    "BAJAJ-AUTO.NS": "quote/nse/BAJAJ_AUTO", # souligné, pas point
    "EMIRATESNBD.AE": "quote/dfm/EMIRATESNBD",  # cotée à Dubaï, pas à Abu Dhabi
    "GMEXICOB.MX":   "quote/bmv/GMEXICO.B",  # le point sépare la classe d'action
    "1155.KL":       "quote/klse/MAYBANK",   # code alphabétique, pas numérique
}


def chemin_du_titre(symbole):
    """« MC.PA » → « quote/epa/MC ». None si la place est inconnue."""
    exc = EXCEPTIONS_CHEMIN.get((symbole or "").upper())
    if exc:
        return exc
    if "." not in symbole:
        return None
    ticker, suffixe = symbole.rsplit(".", 1)
    place = PLACES.get(suffixe.upper())
    if not place:
        return None
    # Les catégories d'actions nordiques s'écrivent avec un point sur le site
    # (VOLV-B devient VOLV.B) là où Yahoo emploie un tiret.
    ticker = ticker.replace("-", ".")
    return "quote/%s/%s" % (place, ticker)


# ═════════════════════════════════════════════════════════════════════════
# LE SECOND FOURNISSEUR : LES SÉRIES ANNUELLES DU SCANNER TRADINGVIEW
# ═════════════════════════════════════════════════════════════════════════
# POURQUOI IL A FALLU EN CHERCHER UN SECOND, ET POURQUOI ON NE RETOURNE PAS
# NÉGOCIER AVEC LE PREMIER
#
# `stockanalysis.com` sert une fenêtre GLISSANTE de cinq exercices à un visiteur
# anonyme. Ce n'est pas un paramètre oublié : treize formes de paramètres, quatre
# cookies et trois en-têtes ont été essayés, et vingt places de cotation testées
# — 20 sur 20 rendent six colonnes, TTM plus cinq. C'est un péage, pas un
# réglage. Résultat : 19 495 sociétés internationales à cinq exercices, contre
# jusqu'à trente-quatre pour les américaines. Toutes les médianes « dix ans » de
# l'international étaient donc muettes par construction.
#
# CE QUE LE SCANNER DE TRADINGVIEW SERT, MESURÉ LE 2026-09-05
# `POST https://scanner.tradingview.com/global/scan` rend des SÉRIES annuelles
# sous les colonnes suffixées `_fy_h`, gratuitement, sans clé et sans compte.
# Sur mille sociétés internationales tirées au hasard de `univers_actions.json` :
# 93,7 % répondent, profondeur MÉDIANE 18 exercices, 70,0 % en ont au moins dix
# et 41,1 % en ont vingt. Un lot de 6 000 tickers passe en 2,5 s pour 10 Mo ;
# un lot de 2 000 en 0,7 s pour 3,3 Mo.
#
# ⚠ LA LISTE DES CHAMPS EST CLOSE, ET ELLE EST COURTE. Le scanner déclare ses
# colonnes sur `GET /america/metainfo` (3 777 champs). Douze seulement portent le
# suffixe `_fy_h` — les douze ci-dessous — et un nom non déclaré rend `None` SANS
# ERREUR, ce qui fait passer une faute de frappe pour une donnée non publiée.
# Vérifié : `total_liabilities_fy_h`, `total_equity_fy_h`,
# `cash_f_operating_activities_fy_h` et `total_shares_outstanding_fy_h` rendent
# `None` comme `zzz_not_a_field_fy_h`. Le PASSIF, les CAPITAUX PROPRES, le FLUX
# D'EXPLOITATION et le NOMBRE D'ACTIONS n'ont pas d'historique annuel chez ce
# fournisseur : ils existent en instantané (`total_liabilities_fy`,
# `shrhldrs_equity_fy`, `cash_f_operating_activities_fy`) et le suffixe `_fh`
# désigne la PRÉVISION du prochain exercice, pas le passé. Les exercices
# TradingView portent donc un bilan et un tableau de flux INCOMPLETS — le ROE, le
# ROIC et le score de Piotroski restent vides sur eux, et c'est écrit dans la
# sortie plutôt que comblé.
TV_SCAN = "https://scanner.tradingview.com/global/scan"
TV_TIMEOUT = 60
TV_RETRIES = 3
# Mesuré : 6 000 tickers passent (10,1 Mo, 2,5 s). On travaille à 2 000 — trois
# fois moins que le plus gros lot réussi. Ce n'est pas de la timidité : une
# réponse de 10 Mo se garde entière en mémoire, et un lot qui échoue emporte
# tout ce qu'il contient. À 2 000, un échec coûte 0,7 s de reprise.
TV_LOT = 2000

# Les douze colonnes annuelles, et le champ de notre schéma qu'elles alimentent.
# `fiscal_period_fy_h` porte l'ÉTIQUETTE d'exercice, pas une grandeur.
TV_GRANDEURS = {
    "revenue":        "total_revenue_fy_h",
    "gross_profit":   "gross_profit_fy_h",
    "ebitda_publie":  "ebitda_fy_h",
    "net_income":     "net_income_fy_h",
    "eps_basic":      "earnings_per_share_basic_fy_h",
    "eps_diluted":    "earnings_per_share_diluted_fy_h",
    "assets":         "total_assets_fy_h",
    "dette_publiee":  "total_debt_fy_h",
    "fcf":            "free_cash_flow_fy_h",
    "capex":          "capital_expenditures_unchanged_fy_h",
    "dps":            "dps_common_stock_prim_issue_fy_h",
}
# `description` sert de témoin d'identité au journal, `currency` de témoin de
# devise, `fiscal_period_end_fy` de témoin de calendrier. Aucune n'est une
# grandeur : elles ne sont jamais écrites comme telles.
TV_COLONNES = (["fiscal_period_fy_h"] + sorted(set(TV_GRANDEURS.values()))
               + ["currency", "fiscal_period_end_fy", "description"])


# Les places que `fetch_stock_logos_tv.py` ne connaît pas — il ne couvre que les
# quatre zones de la carte (us, cn, eu, in). Chacune a été VÉRIFIÉE en appelant
# le scanner avec le plus gros titre de la place, le 2026-09-05 : le préfixe est
# retenu quand la réponse porte une série `fiscal_period_fy_h` non vide.
#
# ⚠ CE QUI A ÉTÉ ESSAYÉ ET NE MARCHE PAS, pour qu'on ne le réessaie pas :
# `TYO:`/`JPX:` (Tokyo, c'est `TSE:`), `KOSPI:` (c'est `KRX:`), `TPE:` (c'est
# `TWSE:`), `KLSE:`/`MYX:1155` (Kuala Lumpur emploie le code ALPHABÉTIQUE :
# `MYX:MAYBANK`), `BVMF:` (c'est `BMFBOVESPA:`), `PRA:` (Prague, c'est `PSECZ:`).
# Sans correspondance mesurée : Bombay (`.BO`, codes numériques que TradingView
# n'indexe pas), Moscou (`.ME`), Lagos (`.NG`), Aquis (`.AQ`) et les places
# allemandes secondaires (`.SG`, `.MU`, `.DU`, `.HM`). Ces sociétés gardent
# leurs cinq exercices, et la sortie le dit.
TV_PLACES_SUP = {
    "T": "TSE", "KS": "KRX", "KQ": "KRX", "TW": "TWSE", "TWO": "TPEX",
    "SI": "SGX", "AX": "ASX", "TO": "TSX", "V": "TSXV", "JK": "IDX",
    "JO": "JSE", "BK": "SET", "KL": "MYX", "SR": "TADAWUL", "AE": "ADX",
    "MX": "BMV", "SA": "BMFBOVESPA", "PS": "PSE", "IR": "EURONEXT",
    "TA": "TASE", "IS": "BIST", "PR": "PSECZ", "NZ": "NZX", "AT": "ATHEX",
    "QA": "QSE", "CA": "EGX", "NE": "NEO", "CN": "CSE", "KA": "PSX",
    "KW": "KSE", "SN": "BCS", "BA": "BCBA", "BD": "BET", "TL": "OMXTSE",
    "VS": "OMXVSE", "RG": "OMXRSE", "IC": "OMXICE", "VN": "HOSE",
}
_TV_PLACES = [None]


def table_places_tv():
    """Suffixe de cotation → préfixe TradingView, LU dans `fetch_stock_logos_tv.py`.

    ⚠ ON NE RÉÉCRIT PAS LA TABLE, ON LA LIT. `fetch_stock_logos_tv.py` porte déjà
    la correspondance place → préfixe TradingView (`TV_EX`) et s'en sert tous les
    jours pour aller chercher 1 288 logos. Une seconde copie divergerait le jour
    où l'une des deux serait corrigée, et la divergence ne produirait aucune
    erreur : seulement des sociétés muettes d'un côté et pas de l'autre.

    ⚠ ON LA LIT SANS L'IMPORTER. Ce fichier-là importe `curl_cffi` et `Pillow`,
    rasterise un masque au chargement et lit `sys.argv` : l'importer ici ferait
    dépendre une collecte de fondamentaux de la présence d'un rasteriseur
    d'images, et lui ferait interpréter NOS options. On extrait donc l'affectation
    `TV_EX` par l'analyseur syntaxique de Python, sans exécuter une ligne.

    Si l'extraction échoue, on le DIT et on continue sur la seule table des places
    supplémentaires : une table amputée fait perdre des exercices, elle n'en
    invente aucun.
    """
    if _TV_PLACES[0] is not None:
        return _TV_PLACES[0]
    table = dict(TV_PLACES_SUP)
    src = SCRIPT_DIR / "fetch_stock_logos_tv.py"
    lues = 0
    try:
        import ast
        arbre = ast.parse(src.read_text(encoding="utf-8"))
        for noeud in arbre.body:
            if not isinstance(noeud, ast.Assign):
                continue
            if not any(isinstance(c, ast.Name) and c.id == "TV_EX"
                       for c in noeud.targets):
                continue
            for k, v in ast.literal_eval(noeud.value).items():
                # `TV_EX` indexe par suffixe AVEC son point (« .HK ») ; notre
                # univers l'écrit sans (« HK »).
                table[str(k).lstrip(".").upper()] = v
                lues += 1
    except Exception as e:
        print("[warn] TV_EX illisible dans %s (%s) — la table TradingView "
              "tourne amputée de ses places européennes" % (src.name, e),
              file=sys.stderr)
    if not lues:
        print("[warn] aucune place lue dans fetch_stock_logos_tv.py : la "
              "correspondance a-t-elle changé de nom ?", file=sys.stderr)
    _TV_PLACES[0] = table
    return table


def symboles_tv(symbole):
    """« MC.PA » → [« EURONEXT:MC »]. Plusieurs candidats quand l'écriture varie.

    Trois écarts d'écriture, les mêmes que ceux relevés par le collecteur de
    logos : Hong Kong sans zéros de tête (0700 → 700), Londres avec un point
    final (BP.L → « BP. »), et les catégories d'actions écrites avec un point ou
    un tiret bas selon la place (VOLV-B → VOLV_B, GMEXICO.B → GMEXICO_B).

    On rend TOUS les candidats plutôt que de choisir : ils partent dans le même
    lot de deux mille, donc un candidat de plus ne coûte rien, et c'est la
    réponse qui tranche. Deviner coûterait des sociétés muettes.
    """
    symbole = (symbole or "").upper()
    if "." not in symbole:
        return []
    ticker, suffixe = symbole.rsplit(".", 1)
    place = table_places_tv().get(suffixe)
    if not place:
        return []
    # Les codes que la règle générale ne peut pas deviner sont déjà connus : la
    # table d'exceptions de chemin porte le VRAI code de la place (Maybank est
    # « MAYBANK » et non « 1155 »). On le réutilise plutôt que d'en tenir une
    # seconde liste.
    exc = EXCEPTIONS_CHEMIN.get(symbole)
    if exc and "/" in exc:
        ticker = exc.rsplit("/", 1)[1].upper()
    if suffixe == "HK":
        ticker = ticker.lstrip("0") or ticker
    formes = [ticker, ticker.replace("-", "."), ticker.replace("-", "_"),
              ticker.replace("-", ""), ticker.replace(".", "_"),
              ticker.replace(".", "")]
    if suffixe == "L":
        formes.append(ticker + ".")
    out, vus = [], set()
    for f in formes:
        f = f.strip().upper()
        if f and f not in vus:
            vus.add(f)
            out.append("%s:%s" % (place, f))
    return out


# Combien de lots TradingView ont été abandonnés. Même raison que `_bridages`
# côté stockanalysis : un lot perdu n'est pas une société sans historique, et
# les confondre ferait passer une panne de réseau pour un fournisseur avare.
_bridages_tv = [0]


def _tv_lot(tickers):
    """Un lot de symboles → {symbole TradingView: {colonne: valeur}}.

    Rend None si le lot n'a pas pu être obtenu — à distinguer d'un lot vide, qui
    signifie « aucun de ces symboles n'existe chez le fournisseur ».
    """
    corps = json.dumps({"symbols": {"tickers": list(tickers)},
                        "columns": list(TV_COLONNES)}).encode("utf-8")
    for essai in range(TV_RETRIES):
        req = urllib.request.Request(TV_SCAN, data=corps, headers={
            "Content-Type": "application/json", "User-Agent": UA,
            "Accept": "application/json", "Accept-Encoding": "gzip",
            "Origin": "https://www.tradingview.com",
            "Referer": "https://www.tradingview.com/",
        })
        try:
            with urllib.request.urlopen(req, timeout=TV_TIMEOUT) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            d = json.loads(raw)
            return {x["s"]: dict(zip(TV_COLONNES, x["d"]))
                    for x in (d.get("data") or [])
                    if isinstance(x, dict) and x.get("s") and x.get("d")}
        except Exception:
            if essai == TV_RETRIES - 1:
                _bridages_tv[0] += 1
                return None
            time.sleep(2.0 * (essai + 1))
    return None


def series_tv(symboles):
    """Les séries annuelles de tout un univers, en lots. {symbole nôtre: réponse}.

    Une requête PAR SOCIÉTÉ coûterait dix-neuf mille appels ; le scanner en prend
    deux mille d'un coup. Sur l'univers entier cela fait une dizaine de lots,
    quelques secondes et une trentaine de mégaoctets — à comparer aux quatre
    pages par société de la source principale.

    Quand plusieurs écritures d'un même titre répondent, on garde LA PLUS
    PROFONDE : c'est la cotation d'origine, celle qui porte l'historique complet.
    """
    candidats, par_symbole = [], {}
    for sym in symboles:
        c = symboles_tv(sym)
        if c:
            par_symbole[sym] = c
            candidats.extend(c)
    candidats = sorted(set(candidats))
    if not candidats:
        return {}, {"candidats": 0, "lots": 0, "repondus": 0}
    brut, lots = {}, 0
    for i in range(0, len(candidats), TV_LOT):
        r = _tv_lot(candidats[i:i + TV_LOT])
        lots += 1
        if r:
            brut.update(r)
    out = {}
    for sym, cands in par_symbole.items():
        meilleur = None
        for c in cands:
            v = brut.get(c)
            if not v or not v.get("fiscal_period_fy_h"):
                continue
            if meilleur is None or len(v["fiscal_period_fy_h"]) > len(meilleur[1]["fiscal_period_fy_h"]):
                meilleur = (c, v)
        if meilleur:
            v = dict(meilleur[1])
            v["_symbole_tv"] = meilleur[0]
            out[sym] = v
    return out, {"candidats": len(candidats), "lots": lots,
                 "repondus": len(out), "vises": len(par_symbole)}


# Les sous-unités de cotation, et leur unité majeure. TradingView annonce la
# devise de la LIGNE COTÉE (GBX pour Londres, ZAC pour Johannesburg, ILA pour
# Tel-Aviv) mais publie ses fondamentaux dans l'unité MAJEURE.
#
# Vérifié le 2026-09-05, et c'est exactement le facteur cent qui a déjà coûté
# cher à ce dépôt : `LSE:SHEL` (annoncé GBX) rend 195 259 808 527 de chiffre
# d'affaires 2025 quand `NYSE:SHEL` en rend 257 932 349 270 — un rapport de
# 1,3210, soit le taux livre/dollar, pas 132,10. C'est donc des LIVRES.
# Recoupé de même sur `HKEX:5` contre `LSE:HSBA` (rapport 10,49 = dollar de
# Hong Kong par livre) et sur `TASE:TEVA`.
TV_SOUS_UNITES = {"GBX": "GBP", "GBP": "GBP", "ZAC": "ZAR", "ZAR": "ZAR",
                  "ILA": "ILS", "ILS": "ILS"}


def _devise_tv(cur):
    cur = (cur or "").upper() or None
    return TV_SOUS_UNITES.get(cur, cur)


# Au-delà de ce pourcentage, deux fournisseurs ne parlent plus de la même chose.
# Le seuil vient d'une mesure, pas d'un choix : sur les huit sociétés de contrôle,
# chiffre d'affaires, résultat net, actif total, bénéfice par action et dividende
# par action concordent à 0,0 % — la même donnée, au centime. Ce qui dépasse
# cinq pour cent n'est jamais du bruit d'arrondi, c'est une définition qui change
# (le résultat brut de Reliance diverge de 36 % parce que le raffinage y range
# ses achats ailleurs) ou une devise qui n'est pas celle qu'on croit.
TV_ECART_RACCORD = 5.0

# La série de change doit couvrir l'exercice, pas le frôler. Cent relevés, c'est
# un peu moins de la moitié des jours de cotation d'une année : en dessous, la
# moyenne est celle d'un trimestre et pas celle de l'exercice.
TV_MIN_RELEVES_FX = 100


def _taux_moyen(par_jour, debut, fin):
    """Le taux MOYEN sur une fenêtre — la seule base qui reproduise le fournisseur.

    ⚠ MOYEN, ET NON À LA CLÔTURE. TradingView convertit ses fondamentaux au taux
    moyen de l'exercice, comme le font les comptes consolidés pour un compte de
    résultat. Vérifié sur BHP, qui publie en dollars et cote en dollars
    australiens : le chiffre d'affaires TradingView converti au taux MOYEN de
    chaque exercice tombe à +0,16 %, +0,08 %, +0,07 %, +0,10 % et +0,08 % des
    cinq exercices publiés par stockanalysis. Au taux de clôture, l'écart aurait
    atteint plusieurs pour cent — assez pour franchir le seuil de raccord et
    faire refuser une donnée juste.
    """
    if not par_jour:
        return None
    v = [x for j, x in par_jour.items()
         if debut <= j <= fin and isinstance(x, (int, float)) and x > 0]
    if len(v) < TV_MIN_RELEVES_FX:
        return None
    return sum(v) / len(v)


def _fenetre_exercice(fin_iso):
    """(premier jour, dernier jour) de l'exercice qui se clôt à cette date."""
    try:
        f = datetime.fromisoformat(fin_iso)
    except Exception:
        return None, None
    d = f.replace(year=f.year - 1) if f.month != 2 or f.day != 29 else f.replace(year=f.year - 1, day=28)
    return d.strftime("%Y-%m-%d"), fin_iso


def _fin_reconstruite(annee, mois, jour):
    """La clôture d'un exercice TradingView, à partir de la dernière connue.

    TradingView ne date pas ses exercices passés : il ne rend qu'une ÉTIQUETTE
    d'année et la date de clôture du DERNIER. On reporte donc le jour et le mois
    de cette clôture sur les années antérieures. C'est exact pour les 90 % de
    sociétés qui clôturent à date fixe, et faux de quelques jours pour celles qui
    suivent un calendrier de 52/53 semaines. L'exercice porte donc
    `fin_reconstruite`, pour qu'une date approchée ne se lise pas comme une date
    déposée.
    """
    for essai in (jour, 28):
        try:
            return datetime(annee, mois, essai).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def exercices_tradingview(tv, devise_etats, fx, exercices_sa):
    """Les exercices TradingView qui MANQUENT à stockanalysis, et leur dossier.

    Rend `(lignes, diagnostic)`. Les lignes sont au format du fichier, chacune
    portant `source: "tradingview"` — jamais un mélange, jamais une ligne à
    moitié de chaque fournisseur.

    QUATRE GARDES, DANS CET ORDRE, ET AUCUNE N'EST DÉCORATIVE

    1. LE CALENDRIER. L'étiquette d'exercice de TradingView n'est PAS la nôtre :
       il étiquette « 2025 » l'exercice de Toyota clos le 31/03/2026, que nous
       appelons 2026, et « 2026 » celui de BHP clos le 30/06/2026, que nous
       appelons aussi 2026. Le décalage n'est donc pas une règle de calendrier,
       il se MESURE : `fiscal_period_end_fy` donne la clôture du dernier
       exercice, notre propre `annee_exercice` en tire notre étiquette, et la
       différence avec la première étiquette du fournisseur donne le décalage.
       Vérifié sur huit sociétés : +1 pour Toyota et Reliance, 0 pour les six
       autres, et les montants tombent alors à 0,0 % sur toute la fenêtre commune.

    2. LA DEVISE. ⚠ TradingView publie ses fondamentaux dans la devise de la
       LIGNE COTÉE, pas dans celle des états. `HKEX:700` rend Tencent en dollars
       de Hong Kong quand la société publie en yuans ; `ASX:BHP` rend BHP en
       dollars australiens quand elle publie en dollars américains — +47 % à
       +55 % sur toutes les grandeurs, soit exactement le genre de facteur qui
       passe inaperçu et fausse tout. Quand les deux devises diffèrent, on
       convertit au taux MOYEN de chaque exercice, et l'exercice porte la trace
       de sa conversion. Quand la devise manque au cache de change, l'exercice
       est refusé — pas approximé.

    3. L'IDENTITÉ. Le chiffre d'affaires des exercices COMMUNS doit concorder
       avec celui de stockanalysis. C'est la même garde que `_noms_concordent`
       plus haut, et pour la même raison : ce dépôt a déjà publié 118 fiches
       portant les états d'un autre émetteur pour 1 913 Md$ de capitalisation.
       Un symbole TradingView mal deviné donnerait ici un écart massif ; il est
       refusé en bloc. Sans exercice commun, aucune preuve — donc aucun ajout.

    4. LE RACCORD, GRANDEUR PAR GRANDEUR. Deux fournisseurs ne définissent pas
       « résultat brut » ni « EBITDA » de la même façon. Mesuré : l'EBITDA de
       LVMH diverge de 18 %, celui de Nestlé de 7,5 %, le résultat brut de
       Reliance de 36 %. Une courbe qui saute de 36 % à la jonction des deux
       sources ment plus qu'elle n'informe : la grandeur fautive est RETIRÉE des
       exercices TradingView, et la raison est écrite. Vide plutôt que plausible.
    """
    diag = {"symbole_tv": tv.get("_symbole_tv"), "nom_tv": tv.get("description")}
    etiquettes = tv.get("fiscal_period_fy_h") or []
    if not etiquettes:
        diag["refus"] = "aucune série annuelle"
        return [], diag
    diag["profondeur_servie"] = len(etiquettes)

    # ── 1. Le calendrier ──
    horo = tv.get("fiscal_period_end_fy")
    if not isinstance(horo, (int, float)):
        diag["refus"] = "clôture du dernier exercice inconnue"
        return [], diag
    fin_derniere = datetime.fromtimestamp(horo, timezone.utc).strftime("%Y-%m-%d")
    decalage = annee_exercice(fin_derniere) - int(etiquettes[0])
    diag["fin_derniere"] = fin_derniere
    diag["decalage_etiquette"] = decalage
    mois, jour = int(fin_derniere[5:7]), int(fin_derniere[8:10])

    # ── 2. La devise ──
    dev_tv = _devise_tv(tv.get("currency"))
    diag["devise_tv"] = dev_tv
    conversion = None
    if devise_etats and dev_tv and dev_tv != devise_etats:
        if not fx:
            diag["refus"] = ("conversion %s → %s impossible : devise introuvable, "
                             "le cache de change est vide" % (dev_tv, devise_etats))
            return [], diag
        pj_tv = None if dev_tv == "USD" else (fx.get(dev_tv) or None)
        pj_et = None if devise_etats == "USD" else (fx.get(devise_etats) or None)
        if (dev_tv != "USD" and not pj_tv) or (devise_etats != "USD" and not pj_et):
            diag["refus"] = "conversion %s → %s impossible : devise absente du cache de change" % (dev_tv, devise_etats)
            return [], diag
        conversion = (pj_tv, pj_et)
        diag["conversion"] = "%s → %s, au taux moyen de chaque exercice" % (dev_tv, devise_etats)
    elif not devise_etats:
        # Sans devise déclarée par la source principale, rien ne permet de dire
        # si les montants sont comparables. On ne devine pas.
        diag["refus"] = "devise des états inconnue"
        return [], diag

    def facteur(fin_iso):
        """Le multiplicateur qui amène un montant TradingView en devise des états."""
        if conversion is None:
            return 1.0
        d, f = _fenetre_exercice(fin_iso)
        if not d:
            return None
        pj_tv, pj_et = conversion
        t_tv = 1.0 if pj_tv is None else _taux_moyen(pj_tv, d, f)
        t_et = 1.0 if pj_et is None else _taux_moyen(pj_et, d, f)
        if not (t_tv and t_et and t_tv > 0 and t_et > 0):
            return None
        return t_tv / t_et

    # Les lignes TradingView, année par année, converties, AVANT tout tri.
    brut = {}
    sans_taux = 0
    for i, lab in enumerate(etiquettes):
        try:
            an = int(lab) + decalage
        except Exception:
            continue
        fin = _fin_reconstruite(an, mois, jour)
        if not fin:
            continue
        k = facteur(fin)
        if k is None:
            sans_taux += 1
            continue
        ligne = {"annee": an, "fin": fin}
        for champ, colonne in TV_GRANDEURS.items():
            col = tv.get(colonne)
            v = _nombre(col[i]) if (isinstance(col, list) and i < len(col)) else None
            if v is None:
                ligne[champ] = None
                continue
            # Le bénéfice et le dividende PAR ACTION sont des montants comme les
            # autres : ils se convertissent au même taux. Ne pas les convertir
            # ferait un BPA en dollars australiens sous un chiffre d'affaires en
            # dollars américains, dans la même ligne.
            ligne[champ] = v * k
        brut[an] = ligne
    if sans_taux:
        diag["exercices_sans_taux"] = sans_taux
    if not brut:
        diag["refus"] = "aucun exercice convertible"
        return [], diag

    # ── 3 et 4. Le recoupement sur les exercices COMMUNS ──
    sa_par_annee = {e.get("annee"): e for e in (exercices_sa or [])}

    def ecart_median(champ, valeur_sa):
        ecarts = []
        for an, ligne in brut.items():
            a, b = valeur_sa(sa_par_annee.get(an) or {}), ligne.get(champ)
            if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
                continue
            if abs(a) < 1e-9:
                continue
            ecarts.append(100.0 * (b / a - 1.0))
        if not ecarts:
            return None, 0
        ecarts.sort()
        n = len(ecarts)
        med = ecarts[n // 2] if n % 2 else (ecarts[n // 2 - 1] + ecarts[n // 2]) / 2.0
        return med, n

    def _fcf_sa(e):
        o, c = e.get("ocf"), e.get("capex")
        return (o - abs(c)) if isinstance(o, (int, float)) and isinstance(c, (int, float)) else None

    LECTURES = {
        "revenue":       lambda e: e.get("revenue"),
        "net_income":    lambda e: e.get("net_income"),
        "assets":        lambda e: e.get("assets"),
        "eps_basic":     lambda e: e.get("eps_basic"),
        "eps_diluted":   lambda e: e.get("eps_diluted"),
        "dps":           lambda e: e.get("dps"),
        "gross_profit":  lambda e: e.get("gross_profit"),
        "ebitda_publie": lambda e: e.get("ebitda_publie"),
        "dette_publiee": lambda e: e.get("dette_publiee"),
        "capex":         lambda e: (abs(e["capex"]) if isinstance(e.get("capex"), (int, float)) else None),
        "fcf":           _fcf_sa,
    }

    med_ca, n_ca = ecart_median("revenue", LECTURES["revenue"])
    diag["raccord"] = {}
    if med_ca is None:
        diag["refus"] = ("aucun exercice commun avec la source principale : "
                         "l'identité de la société n'est pas prouvée")
        return [], diag
    diag["raccord"]["revenue"] = {"ecart_median_pct": round(med_ca, 2),
                                  "exercices_communs": n_ca, "retenu": True}
    if abs(med_ca) > TV_ECART_RACCORD:
        diag["raccord"]["revenue"]["retenu"] = False
        diag["refus"] = ("chiffre d'affaires en désaccord de %.1f %% sur %d exercice(s) "
                         "commun(s) : symbole ou devise douteux" % (med_ca, n_ca))
        return [], diag

    retenus = {"revenue"}
    for champ, lecture in LECTURES.items():
        if champ == "revenue":
            continue
        med, n = ecart_median(champ, lecture)
        if med is None:
            # Aucun point de comparaison : la source principale ne sert pas cette
            # grandeur pour cette société. On garde, et on le dit.
            diag["raccord"][champ] = {"ecart_median_pct": None,
                                      "exercices_communs": 0, "retenu": True,
                                      "note": "non recoupée"}
            retenus.add(champ)
            continue
        ok = abs(med) <= TV_ECART_RACCORD
        diag["raccord"][champ] = {"ecart_median_pct": round(med, 2),
                                  "exercices_communs": n, "retenu": ok}
        if ok:
            retenus.add(champ)

    # ── Les lignes finales : les années que la source principale n'a PAS ──
    lignes = []
    for an in sorted(brut):
        if an in sa_par_annee:
            continue
        ligne = brut[an]
        garde = {c: v for c, v in ligne.items() if c in ("annee", "fin")}
        vide = True
        for champ in TV_GRANDEURS:
            v = ligne.get(champ) if champ in retenus else None
            garde[champ] = v
            if v is not None:
                vide = False
        if vide:
            continue
        # Un exercice sans chiffre d'affaires NI résultat net n'apporte rien
        # qu'une ligne de plus dans le compteur : il ferait grossir
        # `n_exercices` sans nourrir une seule médiane.
        if garde.get("revenue") is None and garde.get("net_income") is None:
            continue
        garde["source"] = "tradingview"
        garde["fin_reconstruite"] = True
        if conversion is not None:
            garde["devise_convertie"] = "%s → %s" % (dev_tv, devise_etats)
        lignes.append(garde)
    diag["exercices_ajoutes"] = len(lignes)
    diag["grandeurs_ecartees"] = sorted(c for c in LECTURES if c not in retenus)
    return lignes, diag


# Les formes juridiques ne distinguent pas deux sociétés : « Repsol S.A. » et
# « Repsol » sont la même, « Zeta Inc. » et « Sany Heavy Industry Co.,Ltd » ne le
# sont pas. On les retire avant de comparer.
_FORMES = ("corporation", "incorporated", "company", "limited", "holdings",
           "holding", "group", "plc", "ltd", "llc", "inc", "co", "sa", "se",
           "ag", "nv", "bv", "ab", "asa", "oyj", "spa", "pcl", "tbk", "berhad",
           "bhd", "pjsc", "psc", "saog", "sae", "aps", "as", "oy", "kgaa",
           # Les formes écrites en toutes lettres, que l'abréviation masquait :
           # sans elles, « OMV Aktiengesellschaft » et « OMV AG » ne se
           # reconnaissent pas.
           "aktiengesellschaft", "aktiebolag", "nyilvanosan", "mukodo",
           "reszvenytarsasag", "societe", "anonyme", "anonima", "publica",
           "public", "joint", "stock", "jsc", "ojsc", "pjs", "pt", "tbk",
           "the", "and")


def _sans_accents(t):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t)
                   if unicodedata.category(c) != "Mn")


def _noyau(nom):
    """Le nom d'une société, réduit à ce qui la distingue.

    ⚠ LES MOTS D'UNE SEULE LETTRE SONT JETÉS. « ENA S.p.A. » se découpait en
    « ena s p a » une fois la ponctuation retirée, et ces trois lettres isolées
    faussaient toute comparaison — elles venaient d'une forme juridique, pas du
    nom.
    """
    import re as _re
    t = _sans_accents((nom or "").lower())
    t = _re.sub(r"[^a-z0-9 ]+", " ", t)
    mots = [m for m in t.split() if len(m) > 1 and m not in _FORMES]
    return " ".join(mots)


def _premier(noyau):
    """Le premier mot significatif — celui qui porte l'identité."""
    p = (noyau or "").split()
    return p[0] if p else ""


def _noms_concordent(attendu, rendu):
    """Deux noms désignent-ils la même société ?

    ⚠ C'EST LE SEUL DISCRIMINANT QUI TIENT. La place échoue (les états de Repsol
    vivent à Madrid alors qu'elle est cotée à Francfort), la devise échoue (287
    holdings des îles Caïman publient en yuans, dont Tencent), l'ordre de
    grandeur échoue (Bure Equity a un revenu négatif). Le nom, lui, tranche :
    vérifié à l'aveugle sur vingt cas, vingt verdicts justes.
    """
    import difflib
    a, b = _noyau(attendu), _noyau(rendu)
    if not a and not b:
        # ⚠ UN NOYAU VIDE DES DEUX CÔTÉS ne prouve pas que les sociétés
        # diffèrent : il prouve que la méthode ne s'applique pas. « T&L Co.,
        # Ltd. » se réduit à « t l », deux lettres isolées que la règle jette.
        # On compare alors les noms entiers, ponctuation et casse mises à part.
        na = "".join(c for c in _sans_accents((attendu or "").lower()) if c.isalnum())
        nb = "".join(c for c in _sans_accents((rendu or "").lower()) if c.isalnum())
        return bool(na) and na == nb
    if not a or not b:
        return False
    if a == b:
        return True
    # ⚠ LE PREMIER MOT PORTE L'IDENTITÉ, et c'est lui qui doit concorder.
    # « endesa » n'est pas « ena », « sany » n'est pas « zeta », « swatch » n'est
    # pas « mc ». Mais « omv aktiengesellschaft » EST « omv », et « halyk bank
    # kazakhstan » EST « halyk savings bank kazakhstan » — deux cas qu'une
    # exigence de longueur sur la sous-chaîne détruisait.
    pa, pb = _premier(a), _premier(b)
    if not pa or not pb:
        return False
    if pa != pb and not (pa.startswith(pb) or pb.startswith(pa)) :
        return False
    if len(pa) >= 4 and len(pb) >= 4 and pa[:4] != pb[:4]:
        return False
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.72


# Le même marché sous deux noms. La source n'est pas cohérente avec elle-même :
# Shenzhen s'écrit « she » et « shz », Shanghai « sha » et « shh », Kuala Lumpur
# « kls » et « klse », Singapour « sgx » et « sgxc », et l'AIM londonien répond
# sous « aim » comme sous « lon ». Refuser un chemin sur ce seul motif casserait
# 5 200 collectes parfaitement justes — mesuré.
ALIAS_PLACES = {
    "she": "shz", "shz": "shz", "sha": "shh", "shh": "shh",
    "klse": "kls", "kls": "kls", "sgxc": "sgx", "sgx": "sgx",
    "aim": "lon", "lon": "lon", "tpex": "tpex", "tpe": "tpe",
}


def _meme_place(a, b):
    """Deux codes de place désignent-ils le même marché ?"""
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    return ALIAS_PLACES.get(a, a) == ALIAS_PLACES.get(b, b)


def chercher_chemins_par_nom(nom, exclure=None, maxi=3):
    """Les chemins que la recherche associe à ce NOM, la cotation d'origine d'abord.

    POURQUOI ELLE EXISTE, alors que `chercher_chemin` fait déjà un repli

    `chercher_chemin` interroge d'abord avec le TICKER. Pour une ligne secondaire
    — `etr/RHO`, `bit/1ATCA`, `lon/HHPD` — la recherche par ticker ne rend que
    des reflets de la même place, dont celui qui vient d'échouer. Le repli
    tournait donc à vide.

    Interrogée par le NOM, la même recherche rend la cotation d'origine EN
    PREMIER : `swx/ROP` pour Roche, `bme/CABK` pour CaixaBank, `sto/ATCO.A` pour
    Atlas Copco, `tpe/2317` pour Hon Hai, `hel/NDA.FI` pour Nordea. Cinq fois
    sur six sur l'échantillon relevé.

    ⚠ LA GARDE DE NOM RESTE ENTIÈRE. On relâche la contrainte de place — c'est
    tout l'objet — mais chaque candidat passe par `_noms_concordent`, la fonction
    resserrée après que 118 fiches ont porté les états d'un autre émetteur pour
    1 913 Md$ de capitalisation. Le nom tranche, jamais la place.

    Les lignes de gré à gré sont écartées : elles portent le nom du bon émetteur
    mais la source n'y sert pas d'états, et les essayer ne coûterait que du
    temps.
    """
    if not nom:
        return []
    d = _get(BASE + "/api/search?q=" + urllib.parse.quote(nom))
    if not d:
        return []
    out = []
    for item in (d.get("data") or d.get("results") or []):
        if not isinstance(item, dict):
            continue
        s2 = item.get("s")
        n2 = item.get("n") or item.get("name") or ""
        if not s2 or "/" not in s2:
            continue
        if s2.partition("/")[0].lower() in ("otc", "otcmkts", "pink"):
            continue
        cand = "quote/" + s2
        if exclure and cand == exclure:
            continue
        if not _noms_concordent(nom, n2):
            continue
        if cand not in out:
            out.append(cand)
        if len(out) >= maxi:
            break
    return out


def chercher_chemin(symbole, nom, place_attendue=None):
    """Repli : l'API de recherche du site rend le chemin canonique.

    Elle existe pour les cas où notre table de places se trompe ou ne connaît
    pas la place — ce qui est arrivé trois fois sur trente-trois marchés. Mieux
    vaut demander au site où il range un titre que de le deviner.

    ⚠ UN TICKER N'EST UNIQUE QUE SUR SA PLACE, ET CETTE BOUCLE L'IGNORAIT.
    Elle retenait le PREMIER résultat contenant une barre oblique, quelle que
    soit la bourse. « 6031 » désigne Sany Heavy Industry à Hong Kong ET Zeta Inc.
    à Tokyo ; « MC » désigne Swatch à Zurich, LVMH à Paris et une société à
    Bangkok ; « ASG » désigne Assicurazioni Generali à Francfort et une société
    vietnamienne à Hô-Chi-Minh-Ville.

    Mesuré sur les paquets publiés : 118 sociétés portaient les états d'un autre
    émetteur, pour 1 913 Md$ de capitalisation cumulée — Fastned avec les comptes
    d'une société de Jakarta (facteur 29 598), Generali avec ceux d'une société
    de Hô-Chi-Minh (facteur 40). Avec une note sur 20, un ROIC, un coût du
    capital et un Piotroski calculés dessus.

    ⚠ ET SI AUCUN RÉSULTAT N'EST SUR LA BONNE PLACE, ON REND None. La fiche reste
    vide, ce qui est la doctrine de ce fichier : « une source dont le format bouge
    doit se signaler bruyamment, pas livrer des trous qu'on prendrait pour des
    lignes non publiées ». Une fiche vide se voit ; une fiche juste-mais-d'une-
    autre-société, non.
    """
    for terme in (symbole.split(".")[0], nom):
        if not terme:
            continue
        d = _get(BASE + "/api/search?q=" + urllib.parse.quote(terme))
        if not d:
            continue
        # ⚠ ON GARDE LE NOM. La réponse rend un COUPLE — `s` le chemin, `n` le
        # nom de l'émetteur — et l'ancienne boucle jetait `n`. C'est pourtant lui
        # qui tranche : la place échoue (les états de Repsol vivent à Madrid
        # alors qu'elle cote à Francfort), la devise échoue, l'ordre de grandeur
        # échoue. Le nom, non.
        candidats = []
        for item in (d.get("data") or d.get("results") or []):
            if not isinstance(item, dict):
                continue
            s2 = item.get("s")
            if s2 and "/" in s2:
                candidats.append((s2, item.get("n") or item.get("name") or ""))
        if not candidats:
            continue
        if not nom:
            # Sans nom attendu on ne peut rien trancher : mieux vaut ne rien
            # rendre qu'adopter un homonyme. C'est la doctrine du fichier — une
            # fiche vide se voit, une fiche d'une autre société non.
            return None
        # Le nom d'abord ; à nom concordant, la place et le ticker départagent.
        cible = (symbole.split(".")[0] or "").replace("-", ".").lower()
        bons = [(s2, n2) for s2, n2 in candidats if _noms_concordent(nom, n2)]
        if not bons:
            continue
        for s2, _n2 in bons:
            pl, _, tk = s2.partition("/")
            if place_attendue and _meme_place(pl, place_attendue) and tk.lower() == cible:
                return "quote/" + s2
        for s2, _n2 in bons:
            if place_attendue and _meme_place(s2.partition("/")[0], place_attendue):
                return "quote/" + s2
        # Aucun sur la place attendue, mais le nom concorde : c'est le cas
        # LÉGITIME de Repsol, OMV, South32, Covestro — leurs états vivent
        # ailleurs que leur cotation. On accepte.
        return "quote/" + bons[0][0]
    return None


# ─────────────────────────────────────────────────────────────────────────
# Décodage du format « devalue »
# ─────────────────────────────────────────────────────────────────────────
# Le site est une application SvelteKit : sa charge JSON est une table de
# POINTEURS. Chaque valeur est un ENTIER qui désigne une case du tableau plat.
# Il n'y a aucun nom de champ dans le transport — d'où la fragilité, et d'où le
# garde-fou de schéma plus bas : une refonte de route a déjà suffi à faire
# passer `financialData` de « dict complet » à « −1 », en silence.
def _resoudre(arr, idx, prof=0):
    if prof > 40:
        return None
    v = arr[idx] if isinstance(idx, int) and 0 <= idx < len(arr) else idx
    if isinstance(v, dict):
        return {k: _resoudre(arr, j, prof + 1) for k, j in v.items()}
    if isinstance(v, list):
        return [_resoudre(arr, j, prof + 1) for j in v]
    return v


def _page(chemin, cle="financialData"):
    d = _get(BASE + "/" + chemin + "/__data.json")
    if not d:
        return None
    for n in d.get("nodes", []):
        if not (isinstance(n, dict) and isinstance(n.get("data"), list)):
            continue
        arr = n["data"]
        if not arr or not isinstance(arr[0], dict) or cle not in arr[0]:
            continue
        bloc = _resoudre(arr, arr[0][cle])
        if isinstance(bloc, dict) and bloc:
            return bloc
    return None


def etats(chemin):
    """Les trois états annuels, en colonnes, plus le contexte.

    Rend None si la structure attendue a changé — jamais un dict à moitié
    rempli. Une source dont le format bouge doit se signaler bruyamment, pas
    livrer des trous qu'on prendrait pour des lignes non publiées.
    """
    res = _page(chemin + "/financials/income-statement")
    if not isinstance(res, dict) or "datekey" not in res:
        return None
    bil = _page(chemin + "/financials/balance-sheet") or {}
    flx = _page(chemin + "/financials/cash-flow-statement") or {}
    # La DEVISE et la fréquence de publication ne sont pas sur les pages
    # d'états : elles vivent dans le bloc `details` de la page de synthèse.
    # C'est une requête de plus, et elle n'est pas optionnelle — sans elle on
    # comparerait des yens à des euros dans le même tableau.
    ctx = _page(chemin + "/financials", "details") or {}
    if not ctx.get("currency"):
        ctx = dict(ctx or {}, **(_page(chemin + "/financials/income-statement", "details") or {}))
    # ── LA CINQUIÈME PAGE : LES RATIOS, QUI PORTENT LA CAPITALISATION PASSÉE ──
    #
    # `/financials/ratios/` n'avait jamais été demandée. Elle sert pourtant,
    # dans le même format et sur les mêmes cinq exercices, la CAPITALISATION et
    # la VALEUR D'ENTREPRISE de chaque clôture — la vraie, celle du fournisseur,
    # pas une reconstruction par ancre de cours.
    #
    # Mesuré avant : 55,2 % des exercices n'avaient aucune capitalisation et
    # 26,0 % en portaient une reconstruite. Vérifié sur 25 sociétés : 25 servent
    # le bloc. Coût : une requête de plus par société, soit +25 % de réseau.
    rat = _page(chemin + "/financials/ratios") if AVEC_RATIOS else None
    return {"contexte": ctx, "resultat": res, "bilan": bil, "flux": flx,
            "ratios": rat if isinstance(rat, dict) else {}}


# ─────────────────────────────────────────────────────────────────────────
# Correspondance des libellés de la source vers les champs du schéma
# ─────────────────────────────────────────────────────────────────────────
# Une seule règle, et elle est stricte : on n'accepte un repli que s'il désigne
# EXACTEMENT le même concept. Le repli « dette » vers « dette + loyers » ou
# « écarts d'acquisition » vers « écarts + incorporels » change la définition du
# mot : il doit produire un vide, pas une valeur. C'est ce que le dépôt s'est
# déjà fait mordre ailleurs — deux nombres justes qui en produisent un faux.
CHAMPS = {
    # compte de résultat
    # ── LES QUATRE FORMES DE COMPTE DE RÉSULTAT ─────────────────────────
    #
    # La source ne sert pas le même compte de résultat à tout le monde : elle a
    # une forme industrielle, une forme assurance (« Ins »), une forme
    # immobilière (« RE ») et une forme financement (« Fin »). On n'en lisait
    # qu'une, et 1 484 sociétés sortaient sans aucun chiffre d'affaires —
    # Allianz, Ping An, Zurich, AXA, AIA, Tokio Marine, Munich Re, Generali,
    # Vonovia, Goodman Group, Link REIT, Bajaj Finance.
    #
    # Vérifié avant d'être écrit : produits − charges = résultat d'exploitation
    # à 0,0 % chez Allianz, Munich Re, Generali, AXA, Goodman, Vonovia et Link
    # REIT. Et les montants tombent juste : Munich Re 62,3 Md€, Generali 57,8,
    # Goodman 4,06 Md A$.
    #
    # ⚠ EN QUEUE, et c'est ce qui protège les 18 000 autres : le résolveur prend
    # le PREMIER nom qui rend une valeur, donc un ajout en fin de liste ne peut
    # que combler un vide. Et ces clefs n'existent que dans leur schéma —
    # contrôlé sur Nestlé, LVMH et Erste Group : zéro clef « Ins », « RE » ou
    # « Fin » chez elles.
    "revenue":           ("resultat", ["revenue", "operatingRevenue",
                                       "revenueIns", "revenueRE", "revenueFin"]),
    "cogs":              ("resultat", ["cor"]),
    "gross_profit":      ("resultat", ["gp"]),
    "rd":                ("resultat", ["rnd"]),
    "sga":               ("resultat", ["sgna", "sgaIns", "sgnaRE"]),
    "opex":              ("resultat", ["opex", "totalOpexIns",
                                       "totalOperatingExpensesRE",
                                       "totalOperatingExpensesFin"]),
    "operating_income":  ("resultat", ["opinc", "opincIns",
                                       "operatingIncomeRE"]),
    "pretax":            ("resultat", ["pretax", "ebtExcl"]),
    "tax":               ("resultat", ["taxexp"]),
    # `netinccmn` est le résultat PART DU COMMUN, `netinc` le total. Les
    # empiler donnait un champ juste par accident de l'ordre — il faut que ce
    # soit juste par intention, et que le total reste lisible sous son nom.
    #
    # Ce que cet ordre préserve : `net_income` s'accorde avec `equity`, qui
    # prend `totalCommonEquity` en premier. Numérateur et dénominateur parlent
    # du même monde. Le collecteur SEC, lui, tombait sur le total quand une
    # société ne déposait pas la part du groupe — ROE de Freeport publié à
    # 25,67 % là où le vrai est 10,99 %.
    "net_income":        ("resultat", ["netinccmn"]),
    "net_income_total":  ("resultat", ["netinc"]),
    "interest_expense":  ("resultat", ["interestExpense",
                                       "interestExpenseRE", "intExpFin"]),
    "eps_diluted":       ("resultat", ["epsdil"]),
    "eps_basic":         ("resultat", ["epsBasic"]),
    "shares_diluted":    ("resultat", ["sharesDiluted"]),
    "shares_basic":      ("resultat", ["sharesBasic"]),
    "dps":               ("resultat", ["dps"]),
    "ebitda_publie":     ("resultat", ["ebitda"]),
    # bilan
    "assets":              ("bilan", ["assets"]),
    "assets_current":      ("bilan", ["assetsc"]),
    # ── LE TÉMOIN DE LA PART DU GROUPE ──
    # Volontairement SANS repli : c'est justement l'absence de repli qui en fait
    # un témoin. S'il a une valeur, `equity` a été pris sur la part du groupe et
    # les minoritaires peuvent être retranchés ; sinon, ils y sont déjà.
    "equity_part_groupe": ("bilan", ["totalCommonEquity"]),
    "liabilities":       ("bilan", ["liabilities", "totalLiabilitiesIns",
                                    "totalLiabilitiesRE"]),
    "liabilities_current": ("bilan", ["currentLiabilities"]),
    "equity":              ("bilan", ["totalCommonEquity", "equity"]),
    # Les participations ne donnant pas le contrôle. `totalCommonEquity` désigne
    # la part du GROUPE ; sans cette ligne, le bilan ne s'équilibre pas — mesuré
    # le 28/08/2026 : 33,3 % des 61 682 bilans internationaux violaient
    # « actif = passif + capitaux propres », et c'était l'absence de ce poste,
    # pas une donnée fausse. LVMH en porte 1,6 Md€.
    "interets_minoritaires_bilan": ("bilan", ["minorityInterestBS"]),
    "cash":                ("bilan", ["cashneq"]),
    "short_term_inv":      ("bilan", ["investmentsc"]),
    # ── LA DETTE : LE TOTAL DE LA SOURCE, ET CINQ FAMILLES DE DÉTAIL ──
    #
    # `debt` est le « Total Debt » que la source calcule elle-même. Il PRIME sur
    # toute recomposition, et pas seulement parce qu'il est plus sûr : Cupid
    # Limited montre que la source DÉPLACE un même montant entre `debtc` et
    # `currentPortDebt` d'un instantané à l'autre — 29 332 000 comptés dans l'un
    # ou dans l'autre selon la date de lecture. Les additionner comme deux postes
    # distincts doublerait ce montant. Le total, lui, ne bouge pas.
    "dette_publiee":       ("bilan", ["debt"]),

    # Le DÉTAIL, en cinq familles. Elles vivent dans le même bloc, à la même
    # requête, et ne coûtent rien. Sans elles, l'identité entre notre somme et le
    # total de la source ne tient que sur 41 % des exercices ; avec elles, 99,6 %.
    #
    # Iberdrola, Enel et National Grid emploient le schéma « services aux
    # collectivités » ; Hana, Royal Bank of Canada, BNP et Absa le schéma
    # « banque » ; Emperador et Carindale celui de l'immobilier ; Toyota se
    # referme au million près par sa division de financement.
    "lt_debt":             ("bilan", ["debtnc", "longTermDebtUti",
                                      "longTermDebtBank", "longTermDebtRE",
                                      "finDivDebtLT"]),
    "current_debt":        ("bilan", ["debtc", "shortTermDebtUti",
                                      "shortTermDebtBank", "finDivDebtCurrent"]),
    "current_port_debt":   ("bilan", ["currentPortDebt",
                                      "currentPortLongTermDebtUti",
                                      "currentPortDebtBank",
                                      "currentPortLongTermDebtRE"]),
    # `trustPref` et `fhlbDebt` sont propres aux banques américaines cotées à
    # l'étranger. Sixième famille, trouvée en cherchant pourquoi Equity
    # Bancshares gardait un écart CONSTANT de 22 à 24 M$ sur cinq exercices.
    "dette_bancaire_autre": ("bilan", ["trustPref", "fhlbDebt"]),
    "lease_lt":            ("bilan", ["capitalLeases"]),
    "lease_ct":            ("bilan", ["currentCapLeases"]),
    "goodwill":            ("bilan", ["goodwill"]),
    "retained_earnings":   ("bilan", ["retearn"]),
    "inventory":           ("bilan", ["inventory"]),
    # flux de trésorerie
    "ocf":            ("flux", ["ncfo"]),
    "capex":          ("flux", ["capex", "capexIns"]),
    "sbc":            ("flux", ["sbcomp"]),
    "dividends_paid": ("flux", ["commonDividendCF"]),
    "buybacks":          ("flux", ["commonRepurchased",
                                   "commonRepurchasedIns"]),
    # ── CE QUE LA SOCIÉTÉ REPREND AU MARCHÉ ──
    # Sans ce champ, le retour à l'actionnaire n'additionne que ce qu'on rend.
    # Xcel Energy affichait 1,282 Md$ de retour pour 3,349 Md$ d'émissions la
    # même année : son retour réel est NÉGATIF. Il est dans le même bloc de flux,
    # à la même requête, et il n'était pas demandé.
    "emissions_actions": ("flux", ["commonIssued", "commonIssuedIns"]),
    "dna":            ("flux", ["totalDepAmorCF"]),
}


def _nombre(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except Exception:
        return None


def notes_historiques(exercices):
    """La note de chaque exercice, en ne connaissant que ce qu'on savait alors.

    ⚠ FONCTION NOMMÉE, ET NON UN BLOC AU MILIEU DE `construire()`.
    Elle vivait là, donc elle n'était rejouable QUE par une collecte complète —
    des heures, et sept jours de tranches pour couvrir l'univers. Le jour où le
    barème change, les paquets déjà écrits gardent l'ancien : la même fiche
    affiche alors deux définitions du même critère selon la nationalité de la
    société. C'est arrivé le 28/08/2026 avec le critère du dividende, qui compte
    désormais les années SANS BAISSE et s'appelait encore « années de hausse »
    sur les dix-neuf mille paquets internationaux.

    Sortie ici, elle se rejoue hors ligne sur les paquets existants, sans une
    requête et sans qu'une seconde copie du calcul existe quelque part.
    """
    # La note dans le temps. Cinq exercices seulement : on ne la calcule qu'à
    # partir du troisième, faute de quoi les médianes à cinq ans porteraient sur
    # deux points et ne voudraient rien dire.
    hist = []
    for i in range(2, len(exercices)):
        sous = exercices[:i + 1]
        sd = sous[-1]
        spa = lambda c: [(x["annee"], x.get(c)) for x in sous]
        n = note_quantitative({
            "roic_1a": sd.get("roic"),
            "roic_5a": _mediane_fenetre([x.get("roic") for x in sous[-5:]], 5),
            "roic_10a": _mediane_fenetre([x.get("roic") for x in sous[-10:]], 10),
            "marge_brute": sd.get("marge_brute"), "marge_ope": sd.get("marge_ope"),
            "marge_nette": sd.get("marge_nette"), "capex_ocf": sd.get("capex_ocf"),
            "predictibilite": _predictibilite(spa("revenue")),
            "annees_hausse_dividende": _serie_hausses_dividende(spa("dps")),
            # Le critère NOTÉ est celui sans baisse ; sans cette ligne il serait
            # muet sur chaque point de l'historique, et la note du passé
            # deviendrait incomparable à celle d'aujourd'hui — un graphique qui
            # monte parce qu'un critère s'est mis à compter, pas parce que la
            # société s'est améliorée.
            "annees_sans_baisse_dividende": _serie_sans_baisse_dividende(spa("dps")),
            "dette_ebitda_brut": sd.get("dette_ebitda_brut"),
            "payout_benefices": sd.get("payout_benefices"),
            "verse_dividende": bool(sd.get("dps") or sd.get("dividends_paid")),
            "croissances": {"ca": _croissances(spa("ca_par_action")),
                            "fcf": _croissances(spa("fcf_par_action")),
                            "div": _croissances(spa("dps"))},
        })
        hist.append({"annee": sd["annee"], "note": n["note"],
                     "note_ramenee": n["note_ramenee"],
                     "criteres_notables": n["criteres_notables"],
                     # Le nombre d'exercices dont ce calcul disposait. Même champ
                     # que côté SEC, et pour la même raison : ici il vaut trois à
                     # cinq, donc TOUTES ces notes reposent sur une série courte
                     # où les médianes à cinq et dix ans sont bornées par la
                     # longueur disponible. La fiche les affiche en demi-teinte
                     # plutôt que de les faire passer pour des notes de même
                     # solidité que celles d'un déposant de vingt ans.
                     "n_exercices_connus": len(sous)})
    return hist


# Le pas maximal toléré dans une série de nombres d'actions. La source
# internationale RÉTRO-AJUSTE son historique : un saut n'y a aucune raison
# d'exister, c'est un défaut du paquet (Enel ×153, Sino Green ×111). Mesuré : à
# ×5 on écarte 385 sociétés sur 19 430 pour 320 reconstructions perdues sur
# 34 267, moins de 1 %.
SAUT_ACTIONS_MAX = 5.0


def combler_mcap_par_ancres(exercices, variations, jour_ref):
    """Comble la capitalisation manquante en transportant un RAPPORT, pas un montant.

        mcap(exercice) = mcap_du_dernier × cours(exercice)/cours(aujourd'hui)
                                         × actions(exercice)/actions(dernier)

    le premier rapport valant exactement `1 / (1 + chNy/100)`, lu dans les
    colonnes ch1y, ch3y, ch5y et ch10y des fichiers de marché.

    ⚠ POURQUOI CETTE FONCTION EXISTE ICI ET PAS SEULEMENT DANS LE REJEU. Un
    correctif qui ne vit que dans le rejeu est détruit au passage suivant du
    collecteur, en silence — et l'univers international se parcourant par
    tranches, la destruction s'étalerait sur une semaine, société par société.

    ⚠ POURQUOI UN RAPPORT ET NON UN MONTANT. Calculer `cours × actions`
    obligerait à croire la devise déclarée des états — 141 sociétés en portent
    une impossible, des chinoises cotées à Hong Kong « publiant en yens » —,
    l'unité du nombre d'actions, et un taux de change. Le premier essai a produit
    278 sauts de plus de ×20 (Mrugesh ×49 867, LATAM ×48 538). Un rapport de deux
    cours n'a pas de monnaie ; un rapport de deux nombres d'actions se moque de
    l'unité ; et au dernier exercice le rapport vaut 1, donc la courbe se
    raccorde exactement.

    ⚠ ON NE COMBLE QUE LES VIDES, et l'on n'interpole PAS entre deux ancres : les
    années intermédiaires restent vides. L'écart à l'ancre est inscrit sous
    `mcap_ecart_jours`, pour qu'une approximation ne se lise pas comme une mesure.

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


def mcap_par_cloture(ratios, resultat):
    """{clôture: (capitalisation, valeur d'entreprise, base)} — en DEVISE DES ÉTATS.

    La page des ratios sert `marketcap` et `ev` pour chacun des cinq exercices.
    C'est la capitalisation VRAIE de chaque clôture, là où le collecteur ne
    savait jusqu'ici que la reconstruire par ancre de cours.

    ⚠ ELLE N'EST PAS DANS LA DEVISE DES ÉTATS. Mesuré le 2026-09-05 : Toyota
    cote et publie en yens, sa capitalisation publiée est cohérente à 0,05 %
    près ; Shell publie en dollars et cote à Londres, la sienne est en LIVRES
    (−24,6 %) ; BHP publie en dollars et cote à Sydney, la sienne est en dollars
    AUSTRALIENS (+44,6 %) ; Tencent publie en yuans et cote à Hong Kong, +11 %.
    Mélangée aux montants des états, elle ferait un P/E faux d'un tiers.

    LE TÉMOIN, ET IL VIENT DU FOURNISSEUR LUI-MÊME. La même page sert `ps`, le
    rapport cours/ventes, et `evrevenue`. Le fournisseur les calcule, LUI, dans
    une devise cohérente : `ps × chiffre d'affaires` redonne exactement
    `marketcap` quand cotation et états coïncident (Toyota : 0,00 %, 0,01 %,
    0,05 %, 0,03 %), et donne la capitalisation EN DEVISE DES ÉTATS quand elles
    diffèrent (Shell : 210,8 Md$ contre 156,7 Md£ publiés, soit le taux de
    l'année). On ne convertit donc rien nous-mêmes : on lit le produit que le
    fournisseur a déjà aligné.

    Quand `ps` manque, aucun témoin n'existe et l'on ne publie rien : une case
    vide se voit, une capitalisation fausse d'un facteur de change non.
    """
    dk = (ratios or {}).get("datekey") or []
    if not dk:
        return {}
    mc = ratios.get("marketcap") or []
    ev = ratios.get("ev") or []
    ps = ratios.get("ps") or []
    rev_par_date = {}
    dres = (resultat or {}).get("datekey") or []
    rrev = (resultat or {}).get("revenue") or []
    for i, d in enumerate(dres):
        if isinstance(d, str) and i < len(rrev):
            rev_par_date[d] = _nombre(rrev[i])
    out = {}
    for i, d in enumerate(dk):
        if not isinstance(d, str) or d.upper() == "TTM" or len(d) < 10:
            continue
        brut_mc = _nombre(mc[i]) if i < len(mc) else None
        brut_ev = _nombre(ev[i]) if i < len(ev) else None
        p = _nombre(ps[i]) if i < len(ps) else None
        r = rev_par_date.get(d)
        if not brut_mc or brut_mc <= 0:
            continue
        if not (p and r and p > 0 and r > 0):
            continue
        implique = p * r
        # Le rapport entre les deux EST le taux de change de l'exercice, tel que
        # le fournisseur l'a appliqué. À un pour cent près, il n'y a pas eu de
        # conversion : les deux devises coïncident.
        k = implique / brut_mc
        if 0.99 <= k <= 1.01:
            out[d] = (round(brut_mc), round(brut_ev) if brut_ev else None,
                      "publiée (stockanalysis)")
        else:
            out[d] = (round(implique),
                      round(brut_ev * k) if brut_ev else None,
                      "publiée (stockanalysis), ramenée en devise des états "
                      "par le rapport cours/ventes du même exercice")
    return out


def construire(brut, mcap_usd=None, beta=None, cours=None, fx_dev=None,
               devise=None, variations=None, jour_marche=None,
               tv=None, fx=None, archive=None):
    res = brut["resultat"]
    dates = res.get("datekey") or []
    # La ligne « TTM » n'est pas un exercice : c'est un cumul glissant. La
    # mélanger aux exercices ferait un point de plus qui n'a pas de clôture.
    idx = [i for i, d in enumerate(dates)
           if isinstance(d, str) and d.upper() != "TTM" and len(d) >= 10]
    if not idx:
        return None
    # La source rend du plus récent au plus ancien ; on remet dans l'ordre.
    idx.sort(key=lambda i: dates[i])

    exercices = []
    for i in idx:
        e = {"fin": dates[i], "annee": annee_exercice(dates[i])}
        for cle, (etat, noms) in CHAMPS.items():
            src = brut.get(etat) or {}
            val = None
            for n in noms:
                col = src.get(n)
                if isinstance(col, list) and i < len(col):
                    val = _nombre(col[i])
                    if val is not None:
                        break
            e[cle] = val
        # Les décaissements sont rendus en négatif par la source ; le schéma les
        # veut en positif — « investissements de l'année », pas « flux négatif ».
        for k in ("capex", "dividends_paid", "buybacks"):
            if e.get(k) is not None:
                e[k] = abs(e[k])
        e["accn"] = None
        e["depose_le"] = None
        exercices.append(e)

    # Une seule entrée par année, AVANT tout calcul — même raison et même
    # fonction que dans le collecteur SEC. Ici la source ne porte pas de numéro
    # de dépôt : c'est la clôture la plus tardive qui l'emporte, donc l'exercice
    # complet plutôt que la période de transition.
    exercices = dedupliquer_exercices(exercices)

    # ── CHAQUE EXERCICE DIT D'OÙ IL VIENT ──
    # Trois fournisseurs cohabitent désormais dans la même série : la source
    # principale, le scanner TradingView et nos propres passages archivés. Deux
    # d'entre eux ne définissent pas « résultat brut » de la même façon. Sans ce
    # champ, la fiche affiche une courbe continue là où il y a une couture, et
    # le lecteur n'a aucun moyen de le savoir.
    for _e in exercices:
        _e["source"] = "stockanalysis"

    # ── LA CAPITALISATION ET LA VALEUR D'ENTREPRISE PUBLIÉES, PAR EXERCICE ──
    _mcaps = mcap_par_cloture(brut.get("ratios") or {}, res)
    for _e in exercices:
        _p = _mcaps.get(_e.get("fin"))
        if _p:
            _e["mcap_publie"], _e["ev_publie"], _e["_mcap_base"] = _p

    provenance = {"stockanalysis": len(exercices)}
    diag_tv = None

    # ── LES EXERCICES QUE LE SECOND FOURNISSEUR AJOUTE ──
    # Ils COMPLÈTENT, ils ne remplacent pas : seules les années absentes de la
    # source principale entrent, et elles entrent entières, jamais mélangées à
    # une ligne existante.
    if tv:
        lignes_tv, diag_tv = exercices_tradingview(tv, devise, fx, exercices)
        if lignes_tv:
            exercices = dedupliquer_exercices(exercices + lignes_tv)
            provenance["tradingview"] = len(lignes_tv)

    # ── NOS PROPRES PASSAGES : CE QUE LA FENÊTRE GLISSANTE A LAISSÉ TOMBER ──
    #
    # La source principale ne sert que les cinq derniers exercices. L'exercice
    # 2020 qu'elle servait l'an dernier n'existe plus nulle part — sauf dans le
    # paquet que NOUS avons écrit. Sans cette reprise, chaque passage détruisait
    # un exercice par an, définitivement. Avec elle, la profondeur s'accroît
    # toute seule d'un exercice par an, gratuitement et pour toujours.
    #
    # ⚠ ON NE REPREND QUE LES ANNÉES QUE PERSONNE NE SERT PLUS. Une année encore
    # servie vient du fournisseur, pas de l'archive : c'est la version fraîche
    # qui gagne, retraitements compris.
    if archive:
        connues = {e.get("annee") for e in exercices}
        repris = []
        for a in (archive.get("exercices") or []):
            an = a.get("annee")
            if an is None or an in connues:
                continue
            ligne = dict(a)
            ligne["archive"] = True
            ligne.setdefault("source", "stockanalysis")
            repris.append(ligne)
            connues.add(an)
        if repris:
            exercices = dedupliquer_exercices(exercices + repris)
            provenance["archive"] = len(repris)
            # Le dossier de raccord du passage qui a écrit ces lignes, quand le
            # passage d'aujourd'hui n'en a pas produit de plus frais.
            if diag_tv is None and archive.get("tradingview"):
                diag_tv = dict(archive["tradingview"])
                diag_tv["repris_de_l_archive"] = True

    # Les lignes venues d'ailleurs que du compte de résultat principal n'ont pas
    # toutes les colonnes du schéma. Le calcul qui suit les lit par leur nom :
    # une clef absente lèverait une exception au milieu d'une série par ailleurs
    # bonne. On complète à None — ce qui est absent reste absent.
    for _e in exercices:
        for _c in CHAMPS:
            _e.setdefault(_c, None)
        _e.setdefault("accn", None)
        _e.setdefault("depose_le", None)

    # Le signe des charges d'intérêts : 351 valeurs négatives sur 352 de ce
    # côté-ci contre 1 sur 223 côté SEC. Sans ce redressement, la garde
    # `interest_expense > 0` de la couverture des intérêts vide ce ratio pour
    # 80,7 % de l'univers international, LVMH compris.
    #
    # ⚠ IL DOIT PASSER AVANT LES RATIOS, et il passait après. Le commentaire
    # ci-dessus annonçait donc exactement le contraire de ce qui se produisait :
    # mesuré le 28/08/2026, la couverture des intérêts était renseignée sur
    # 8 fiches internationales sur 15 887 où elle est calculable — 0,05 %, contre
    # 99,2 % côté SEC. Six lignes plus haut, et le ratio existe.
    for _e in exercices:
        _e["interest_expense"] = _charge(_e.get("interest_expense"))

    # Ce qui est logiquement impossible s'efface AVANT tout calcul : un résultat
    # brut au-dessus du chiffre d'affaires, un poste au-dessus de son propre
    # total. Une marge tirée d'un couple impossible reste fausse, mais elle
    # n'a plus l'air de rien une fois arrondie à deux décimales.
    impossibles = effacer_l_impossible(exercices)

    # ── Reconstructions et ratios, à l'identique du collecteur SEC ──
    for e in exercices:
        if e["gross_profit"] is None and e["revenue"] is not None and e["cogs"] is not None:
            e["gross_profit"] = e["revenue"] - e["cogs"]
        if e["pretax"] is None and e["net_income"] is not None and e["tax"] is not None:
            e["pretax"] = e["net_income"] + e["tax"]

        # ── LE TOTAL DES DETTES SE DÉDUIT, IL NE SE DEVINE PAS ──
        # Même correctif que côté américain, et même mesure : 1 772 sociétés
        # internationales sur 19 430 sortaient sans total des dettes, dont 1 757
        # dont l'actif et les capitaux propres étaient pourtant là. Sans ce
        # total, le Z d'Altman perd son quatrième terme — et il était publié
        # quand même, amputé, puis lu comme un verdict.
        # Actif = dettes + capitaux propres + intérêts minoritaires + mezzanine
        # est une identité comptable : on la retourne, et on marque le résultat.
        if (e.get("liabilities") is None and e.get("assets") is not None
                and e.get("equity") is not None):
            # ⚠ NE SOUSTRAIRE LES MINORITAIRES QUE SI `equity` NE LES CONTIENT PAS.
            #
            # `equity` est pris sur `totalCommonEquity` — la part du GROUPE — et
            # à défaut sur `equity`, qui inclut les minoritaires. Les retrancher
            # dans le second cas les compte deux fois et creuse le passif.
            #
            # C'est la garde qui existe côté américain, et qui manquait ici. Là-bas
            # elle était présente mais son témoin ne pouvait jamais exister ; ici
            # elle était absente. Les deux erreurs se ressemblent peu et donnent
            # le même résultat : un passif déduit faux.
            minoritaires = ((e.get("interets_minoritaires_bilan") or 0.0)
                            if e.get("equity_part_groupe") is not None else 0.0)
            e["liabilities"] = (e["assets"] - e["equity"] - minoritaires
                                - (e.get("capitaux_mezzanine") or 0.0))
            e["liabilities_reconstruit"] = True

        if e["operating_income"] is None:
            if e["gross_profit"] is not None and e["opex"] is not None:
                e["operating_income"] = e["gross_profit"] - e["opex"]
                e["_ope_source"] = "brut moins charges d’exploitation"
            elif e["gross_profit"] is not None and (e["rd"] is not None or e["sga"] is not None):
                e["operating_income"] = e["gross_profit"] - (e["rd"] or 0) - (e["sga"] or 0)
                e["_ope_source"] = "brut moins R&D et frais généraux"
        else:
            e["_ope_source"] = "publié (retraité par le fournisseur)"

        # ⚠ ON NE RECALCULE QUE CE QU'ON PEUT CALCULER. Le second fournisseur
        # sert un flux de trésorerie disponible SANS servir le flux
        # d'exploitation : la soustraction rend None sur ses exercices, et
        # l'écraser détruirait la seule valeur qu'on ait. La règle est la même
        # que partout ailleurs ici — on ne remplace jamais un chiffre par un
        # vide.
        _fcf = (e["ocf"] - e["capex"]) if (e["ocf"] is not None and e["capex"] is not None) else None
        if _fcf is not None or e.get("fcf") is None:
            e["fcf"] = _fcf
        e["ebitda"] = (e["operating_income"] + e["dna"]) \
            if (e["operating_income"] is not None and e["dna"] is not None) else e.get("ebitda_publie")

        e["tresorerie"] = e["cash"]
        liq = ((e["cash"] or 0) + (e["short_term_inv"] or 0)) if e["cash"] is not None else None
        # ── LE TOTAL PUBLIÉ PAR LA SOURCE PRIME ──
        # Il est servi sur 96,1 % des exercices, et il est le seul à ne pas
        # bouger quand la source déplace un montant d'un poste à l'autre entre
        # deux instantanés. On ne recompose que faute de lui.
        dette = e.get("dette_publiee")
        if dette is None:
            _postes = ("lt_debt", "current_debt", "current_port_debt",
                       "dette_bancaire_autre", "lease_lt", "lease_ct")
            if any(e.get(k) is not None for k in _postes):
                dette = sum((e.get(k) or 0) for k in _postes)
            e["_dette_source"] = "recomposée"
        else:
            e["_dette_source"] = "totale, publiée par la source"
        e["liquidites"] = liq
        e["tresorerie_totale"] = liq
        e["dette_totale"] = dette
        e["dette_nette"] = (dette - liq) if (dette is not None and liq is not None) else None

        e["marge_brute"] = _pct(e["gross_profit"], e["revenue"])
        e["marge_ope"] = _pct(e["operating_income"], e["revenue"])
        # Faute de part du commun, le total vaut mieux que rien — mais on le
        # DIT, au lieu de le laisser passer pour ce qu'il n'est pas.
        if e.get("net_income") is None and e.get("net_income_total") is not None:
            e["net_income"] = e["net_income_total"]
            e["net_income_est_total"] = True

        e["marge_nette"] = _pct(e["net_income"], e["revenue"])
        e["marge_fcf"] = _pct(e["fcf"], e["revenue"])

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
        e["_capital_investi"] = (e["equity"] + dette - (liq or 0)) \
            if (e["equity"] is not None and dette is not None) else None
        e["_capitaux_employes"] = (e["assets"] - e["liabilities_current"]) \
            if (e["assets"] is not None and e["liabilities_current"] is not None) else None

        e["capex_ca"] = _pct(e["capex"], e["revenue"])
        e["capex_ocf"] = _pct(e["capex"], e["ocf"]) if (e["ocf"] and e["ocf"] > 0) else None
        e["rd_ocf"] = _pct(e["rd"], e["ocf"]) if (e["ocf"] and e["ocf"] > 0) else None
        e["sbc_fcf"] = _pct(e["sbc"], e["fcf"]) if (e["fcf"] and e["fcf"] > 0) else None
        e["dette_ebitda"] = _r(_div(e["dette_nette"], e["ebitda"]), 2) if (e["ebitda"] and e["ebitda"] > 0) else None
        e["dette_ebitda_brut"] = _r(_div(dette, e["ebitda"]), 2) if (e["ebitda"] and e["ebitda"] > 0) else None
        e["couverture_interets"] = _r(_div(e["operating_income"], e["interest_expense"]), 1) \
            if (e["interest_expense"] and e["interest_expense"] > 0) else None
        e["goodwill_actifs"] = _pct(e["goodwill"], e["assets"])
        e["payout_benefices"] = _pct(e["dividends_paid"], e["net_income"]) \
            if (e["net_income"] and e["net_income"] > 0) else None
        e["payout_fcf"] = _pct(e["dividends_paid"], e["fcf"]) if (e["fcf"] and e["fcf"] > 0) else None
        # ── LE RETOUR NET, ET LE BRUT À CÔTÉ ──
        # Une société qui rachète pour un milliard et en émet pour trois ne rend
        # rien : elle prend. Mesuré sur des cas nommés — MercadoLibre, Guardant
        # Health, CleanSpark et Xcel Energy ont tous un retour réellement NÉGATIF
        # que la version brute affichait positif ; BizLink était surestimée 2,16
        # fois, United Therapeutics 1,41.
        #
        # On garde les deux : leur ÉCART est lui-même une information — une
        # société qui rachète d'une main ce qu'elle émet de l'autre finance sa
        # rémunération en actions.
        _sert = (e["dividends_paid"] is not None or e["buybacks"] is not None
                 or e.get("emissions_actions") is not None)
        _brut = ((e["dividends_paid"] or 0) + (e["buybacks"] or 0)) if _sert else None
        e["retour_actionnaire_brut"] = _brut
        e["retour_actionnaire"] = ((_brut - (e.get("emissions_actions") or 0))
                                   if _brut is not None else None)

    unites_actions = _corriger_unite_actions(exercices, cours=cours)

    # Le dividende par action ENSUITE, et avant la recouture des divisions : il se
    # confronte au montant total verse divise par le nombre d actions, donc les
    # deux doivent etre sur la meme base — brute, telle que deposee.
    dps_redresses = redresser_dividende_par_action(exercices)

    # ── LA RECOUTURE DÉTECTE, MAIS NE CORRIGE PLUS ──
    #
    # Le commentaire d'origine disait : « la source rétro-ajuste déjà les
    # divisions, la recouture ne trouvera rien ». La première moitié est vraie.
    # La seconde était une SUPPOSITION, et elle était fausse : mesuré le
    # 28/08/2026, la recouture trouve 1 423 sauts sur 1 187 sociétés et les
    # corrige tous. Elle ne rattrape rien — elle fabrique.
    #
    # La preuve est dans le cache lui-même : O'Reilly y porte 1 044 165 000
    # actions en 2021, soit 69 611 000 × 15. La division de 2025 est DÉJÀ
    # appliquée à l'exercice 2021. Idem Alphabet, Amazon, Tesla, NVIDIA.
    #
    # Ritchie Bros affichait ainsi 167 110 245 actions en 2021 au lieu de
    # 111 406 830, et IONQ en avait MOINS en 2022 qu'en 2021 — alors qu'elle n'a
    # jamais racheté une action.
    #
    # On garde la DÉTECTION, sur une copie, et on la journalise. Le jour où la
    # source changerait de politique, le journal le dirait ; un appel supprimé,
    # lui, ne dit jamais rien. C'était l'intention d'origine : il lui manquait de
    # ne pas appliquer.
    _sauts = _corriger_divisions([dict(e) for e in exercices])
    if _sauts:
        print("[info] %d saut(s) d'actions detecte(s) et NON corrige(s) — la "
              "source internationale est deja retro-ajustee" % len(_sauts),
              file=sys.stderr)
    divisions = []

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
        sh = e.get("shares_diluted")
        if sh and sh > 0:
            e["ca_par_action"] = _r(_div(e["revenue"], sh), 4)
            e["fcf_par_action"] = _r(_div(e["fcf"], sh), 4)
            e["ocf_par_action"] = _r(_div(e["ocf"], sh), 4)
        else:
            e["ca_par_action"] = e["fcf_par_action"] = e["ocf_par_action"] = None
        cp, base = _moy("equity", i)
        e["_base_capital"] = base
        # ── LE ROE SE CALCULE SUR LES CAPITAUX PART DU GROUPE ──
        # `net_income` est le résultat part du groupe — `net_income_total` existe
        # à côté, preuve que la distinction est faite. Mais `equity` contient les
        # intérêts minoritaires quand la source ne sert pas `totalCommonEquity` :
        # on divisait un numérateur net de minoritaires par un dénominateur qui
        # les contient, et le ROE sortait mécaniquement sous-estimé.
        #
        # Mesuré le 30/08/2026 sur cinq paquets : 47 des 134 exercices portant
        # des minoritaires étaient concernés, soit 35 %. 3994.T publiait 3,91 %
        # au lieu de 4,99 % — ses minoritaires pèsent 27 % du bilan.
        #
        # ⚠ On ne retranche QUE si `equity` les contient. C'est la garde écrite
        # plus haut pour la reconstruction du passif : quand `equity_part_groupe`
        # existe, `equity` EST déjà la part du groupe, et retrancher une seconde
        # fois compterait les minoritaires deux fois. Le dépôt a déjà payé cette
        # erreur ; on réutilise sa garde au lieu d'en écrire une qui divergerait.
        #
        # ⚠ Le ROIC et le ROCE ne sont pas touchés : ils rapportent un résultat
        # d'exploitation, AVANT répartition, à un capital qui doit rester total.
        # ⚠ `cp` est une MOYENNE de deux exercices : on retranche donc une
        # moyenne de minoritaires, pas la valeur d'une seule année. Mélanger un
        # solde de clôture et une moyenne creuserait un écart artificiel de la
        # taille de la variation annuelle des minoritaires.
        _mi, _ = _moy("interets_minoritaires_bilan", i)
        if e.get("equity_part_groupe") is not None or not _mi:
            _mi = 0.0
        cp_groupe = (cp - _mi) if (cp and _mi) else cp
        e["roe"] = _pct(e["net_income"], cp_groupe) if (cp_groupe and cp_groupe > 0) else None
        act, _ = _moy("assets", i)
        e["roa"] = _pct(e["net_income"], act) if (act and act > 0) else None
        ci, _ = _moy("_capital_investi", i)
        e["roic"] = _pct(e["nopat"], ci) if (ci and ci > 0) else None
        ce, _ = _moy("_capitaux_employes", i)
        e["roce"] = _pct(e["operating_income"], ce) if (ce and ce > 0) else None

        # Coût du capital : la capitalisation historique se reconstitue au cours
        # de clôture, comme côté SEC. Elle est en devise de COTATION, la dette en
        # devise des ÉTATS — on ne les mélange que si les deux coïncident.
        mc = None
        # ── LA VALEUR PUBLIÉE PASSE AVANT TOUTE RECONSTRUCTION ──
        # Elle vient de la page des ratios, elle est datée de la clôture, et elle
        # est déjà ramenée en devise des états. Une reconstruction par cours et
        # nombre d'actions ne fait au mieux que la retrouver.
        if e.get("mcap_publie"):
            mc = e["mcap_publie"]
            e["mcap_source"] = e.get("_mcap_base") or "publiée (stockanalysis)"
        if mc is None and cours and e.get("shares_diluted"):
            px = _en_devise_etats(_cours_au(cours, e["fin"]), e["fin"], fx_dev, devise)
            if px:
                mc = px * e["shares_diluted"]
                e["mcap_source"] = "reconstruite (cours de clôture × actions)"
        if mc is None and i == len(exercices) - 1:
            mc = _en_devise_etats(mcap_usd, None, fx_dev, devise)
            if mc:
                e["mcap_source"] = "capitalisation du jour, ramenée en devise des états"
        e["mcap_estime"] = round(mc) if mc else None
        # Le témoin de provenance a servi ; `mcap_source` le dit désormais, et
        # deux champs qui disent la même chose finissent par se contredire.
        e.pop("_mcap_base", None)
        e["wacc"] = _wacc(mc, e.get("dette_totale"), e.get("interest_expense"),
                          e.get("_taux_nopat"), beta)
        e["roic_moins_wacc"] = (round(e["roic"] - e["wacc"], 2)
                                if (e.get("roic") is not None and e.get("wacc") is not None) else None)

    # ── LA CAPITALISATION HISTORIQUE QUE LA SÉRIE DE COURS NE COUVRE PAS ──
    #
    # La boucle ci-dessus n'a posé une capitalisation que là où une vraie série
    # de cours existe — huit cents titres. Pour les 19 000 autres, une seule
    # barre au bord droit d'un graphique. Les quatre ancres des fichiers de
    # marché comblent le reste, en transportant un rapport.
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

    for i in range(1, len(exercices)):
        a, b = exercices[i - 1], exercices[i]
        dn = (b["nopat"] - a["nopat"]) if (a.get("nopat") is not None and b.get("nopat") is not None) else None
        ca_, cb = a.get("_capital_investi"), b.get("_capital_investi")
        dci = (cb - ca_) if (ca_ is not None and cb is not None) else None
        b["roiic"] = _pct(dn, dci) if (dci and abs(dci) > 0) else None
    if exercices:
        exercices[0]["roiic"] = None

    resume = construire_resume(exercices, divisions, unites_actions)
    if diag_tv:
        resume["tradingview"] = diag_tv
    return {"exercices": exercices, "resume": resume,
            "provenance": provenance}


def construire_resume(exercices, divisions=None, unites_actions=None):
    """Le bloc `resume` d'une société, à partir de ses seuls exercices.

    SORTIE DE `construire()` POUR POUVOIR ÊTRE REJOUÉE HORS LIGNE.

    Les paquets portent tous l'horodatage de leur écriture, pas celui du code qui
    les a produits. Mesuré le 28/08/2026 : les 512 paquets internationaux
    dataient de 03:55 et le correctif de `_mediane_fenetre` de 16:16 — douze
    heures d'écart, et 14 666 « médianes sur cinq ans » calculées sur une seule
    variation continuaient d'être publiées, portant 2 654,5 points de barème
    indus sur 2 567 sociétés.

    Recollecter n'était pas la réponse : l'univers fait 19 495 sociétés et la
    source ne se parcourt qu'en plusieurs jours, alors que TOUT ce dont ce calcul
    a besoin est déjà écrit dans les paquets. Cette fonction existe pour qu'un
    correctif de calcul se propage en quelques minutes de processeur, sans une
    requête — comme `notes_historiques` avant elle.
    """
    piotroski = piotroski_detail = altman = altman_detail = None
    if len(exercices) >= 2:
        piotroski, piotroski_detail = _piotroski(exercices[-1], exercices[-2])
    if exercices:
        # Le score d'Altman met en rapport la capitalisation et le passif : les
        # deux doivent être dans la même devise, ce qui est désormais le cas.
        altman, altman_detail = _altman_z(exercices[-1], exercices[-1].get("mcap_estime"))

    def pa(cle):
        return [(e["annee"], e.get(cle)) for e in exercices]

    def med(cle, n):
        # Une fenêtre de n ans exige n-2 points : sans quoi la médiane
        # « dix ans » du jeu international serait celle de cinq.
        return _mediane_fenetre([e.get(cle) for e in exercices[-n:]], n)

    d = exercices[-1]
    # ── QUI A FOURNI QUOI, COMPTÉ SUR LES EXERCICES EUX-MÊMES ──
    # Calculé ici, et non transmis depuis la collecte, pour la même raison que
    # tout le reste de cette fonction : le jour où l'on rejoue les paquets hors
    # ligne, ce compte doit se refaire sans une requête. Un paquet dont les
    # exercices ne portent pas de `source` est un paquet d'avant la couture —
    # il compte sous `inconnue`, ce qui se voit, plutôt que d'être attribué au
    # hasard à la source principale.
    sources = {}
    for e in exercices:
        s = e.get("source") or "inconnue"
        if e.get("archive"):
            s = s + " (archive)"
        sources[s] = sources.get(s, 0) + 1
    resume = {
        "n_exercices": len(exercices),
        "sources_exercices": sources,
        "premier": exercices[0]["annee"], "dernier": d["annee"],
        "fin_exercice": d["fin"], "accn": None, "depose_le": None,
        "roic_1a": d.get("roic"), "roic_5a": med("roic", 5), "roic_10a": med("roic", 10),
        "roce_1a": d.get("roce"), "roce_5a": med("roce", 5), "roce_10a": med("roce", 10),
        "roe_1a": d.get("roe"), "roe_5a": med("roe", 5), "roe_10a": med("roe", 10),
        "roiic_1a": d.get("roiic"), "roiic_5a": med("roiic", 5), "roiic_10a": med("roiic", 10),
        "wacc_1a": d.get("wacc"), "wacc_5a": med("wacc", 5), "wacc_10a": med("wacc", 10),
        "roic_moins_wacc": d.get("roic_moins_wacc"),
        "marge_brute": d.get("marge_brute"), "marge_ope": d.get("marge_ope"),
        "marge_nette": d.get("marge_nette"), "marge_fcf": d.get("marge_fcf"),
        "capex_ca": d.get("capex_ca"), "capex_ocf": d.get("capex_ocf"),
        "rd_ocf": d.get("rd_ocf"), "sbc_fcf": d.get("sbc_fcf"),
        "croissances": {
            "ca": _croissances(pa("ca_par_action")),
            "eps": _croissances(pa("eps_diluted")),
            "fcf": _croissances(pa("fcf_par_action")),
            "ocf": _croissances(pa("ocf_par_action")),
            "div": _croissances(pa("dps")),
        },
        "predictibilite": _predictibilite(pa("revenue")),
        "annees_hausse_dividende": _serie_hausses_dividende(pa("dps")),
        "annees_sans_baisse_dividende": _serie_sans_baisse_dividende(pa("dps")),
        "dette_ebitda": d.get("dette_ebitda"), "dette_ebitda_brut": d.get("dette_ebitda_brut"),
        "couverture_interets": d.get("couverture_interets"),
        "goodwill_actifs": d.get("goodwill_actifs"),
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
        "payout_benefices_10a": med("payout_benefices", 10),
        "payout_fcf": d.get("payout_fcf"),
        "piotroski": piotroski, "piotroski_detail": piotroski_detail,
        "altman_z": altman, "altman_detail": altman_detail,
        "verse_dividende": bool(d.get("dps") or d.get("dividends_paid")),
        "divisions_action": divisions,
        "unites_actions_corrigees": unites_actions,
    }
    # AVANT la note, pas après : un ratio qu'on s'apprête à déclarer non
    # mesurable ne doit pas d'abord rapporter un point.
    ecarter_ratios_degeneres(resume)
    resume["note_q"] = note_quantitative(resume)
    resume["note_historique"] = notes_historiques(exercices)
    return resume


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


def _en_devise_etats(px_usd, date_iso, fx_dev, devise):
    """Un montant en dollars, ramené à la devise des états, au taux de sa date.

    Retourne None si le taux est inconnu : mieux vaut une case vide qu'un
    multiple faux d'un facteur cent cinquante.
    """
    if px_usd is None:
        return None
    if not devise or devise == "USD":
        return px_usd
    t = _taux(fx_dev, date_iso)
    return (px_usd / t) if (t and t > 0) else None


def _cotation_vers_etats(px, dev_cot, dev_etats, fx):
    """Un cours en devise de COTATION, ramené à la devise des ÉTATS.

    Distincte de `_en_devise_etats`, qui part de dollars : la cotation de
    `univers_actions.json` est en devise locale — 45,12 pour ERAMET, en euros.
    Lui appliquer la conversion « depuis le dollar » diviserait un euro par le
    taux de l'euro et donnerait un cours faux d'environ 8 %, assez petit pour
    passer inaperçu et assez gros pour fausser tous les multiples.

    Le passage se fait par le dollar, qui est le pivot du cache de change :
    `fx[DEVISE][jour]` vaut ce qu'UNE unité de cette devise vaut en dollars.

    Rend None si un taux manque — une case vide se voit, un multiple faux non.
    """
    if px is None or not dev_cot:
        return None
    if not dev_etats or dev_cot == dev_etats:
        return px
    t_cot = 1.0 if dev_cot == "USD" else _taux(fx.get(dev_cot), None)
    t_eta = 1.0 if dev_etats == "USD" else _taux(fx.get(dev_etats), None)
    if not (t_cot and t_eta and t_cot > 0 and t_eta > 0):
        return None
    return px * t_cot / t_eta


def _cours_au(serie, fin_iso):
    if not serie:
        return None
    try:
        cible = datetime.fromisoformat(fin_iso).replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None
    best = ecart = None
    for p in serie:
        try:
            t, c = p[0], p[1]
        except Exception:
            continue
        if t > 1e11:
            t = t / 1000.0
        d = abs(t - cible)
        if ecart is None or d < ecart:
            ecart, best = d, c
    return None if (ecart is None or ecart > 45 * 86400) else best


def charger_fx():
    """{DEVISE: {AAAA-MM-JJ: valeur d'UNE unité en dollars}} — déjà dans le cache."""
    # ── LES DEUX CACHES, FUSIONNÉS — PAS LE PREMIER QUI RÉPOND ──
    #
    # L'ancienne boucle rendait le premier fichier non vide. Or
    # `fx_rates_cache.json` s'arrête au 2026-04-28 et `tradfi_fx_cache.json` va
    # jusqu'au 2026-08-29 : le second n'était jamais atteint, et toute
    # conversion de l'exercice le plus récent se faisait à un taux vieux de
    # quatre mois. Mesuré : KRW 7,19 % de dérive, BRL 3,83 %, SEK 3,67 %,
    # médiane 0,98 % — invisible, donc installé depuis quatre mois.
    #
    # ⚠ FUSION ET NON CHOIX : le fichier frais couvre le rand sud-africain que
    # le périmé n'a pas, et le périmé couvre le peso philippin que le frais n'a
    # pas. Prendre l'un OU l'autre perd une devise dans les deux sens.
    #
    # Jour par jour, le plus récent fichier gagnant sur les jours communs : une
    # série fraîche mais courte ne doit pas effacer trente ans d'historique.
    fusion = {}
    for nom in ("fx_rates_cache.json", "tradfi_fx_cache.json"):
        f = CACHE_DIR / nom
        if not f.exists():
            continue
        try:
            with f.open(encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        for dev, par_jour in d.items():
            if isinstance(par_jour, dict) and par_jour:
                fusion.setdefault(dev, {}).update(par_jour)
    return fusion


def _taux(par_jour, date_iso):
    """Le taux à cette date, ou le dernier connu AVANT elle.

    Jamais un taux postérieur : on ne convertit pas le passé avec le change
    d'aujourd'hui. Si la date demandée précède toute la série, on prend le plus
    ancien taux connu — la seule approximation acceptable ici, et elle ne
    concerne que des exercices antérieurs à 2003.
    """
    if not par_jour:
        return None
    if date_iso is None:
        d = max(par_jour)
        return par_jour.get(d)
    t = par_jour.get(date_iso)
    if t:
        return t
    avant = [d for d in par_jour if d <= date_iso]
    if avant:
        return par_jour[max(avant)]
    return par_jour[min(par_jour)]


def charger_cours():
    for nom in ("tradfi_history_cache.json", "tradfi_histories_cache.json"):
        f = CACHE_DIR / nom
        if not f.exists():
            continue
        try:
            with f.open(encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        h = d.get("histories") if isinstance(d, dict) else None
        if isinstance(h, dict):
            return h
        if isinstance(d, dict) and d and isinstance(next(iter(d.values())), list):
            return d
    return {}


def charger_univers():
    """Les titres SUIVIS qui portent un suffixe de place, donc non américains."""
    if not TRACKER_CACHE.exists():
        print("[fatal] %s absent" % TRACKER_CACHE, file=sys.stderr)
        return {}
    with TRACKER_CACHE.open(encoding="utf-8") as f:
        tc = json.load(f)
    u = {}
    for n in tc.get("narratives", []):
        for t in n.get("tokens", []):
            s = t.get("symbol")
            if s and "." in s and s not in u:
                u[s] = {"nom": t.get("name"), "mcap": t.get("mcap"),
                        "exchange": t.get("exchange"), "region": t.get("region"),
                        "secteur_suivi": n.get("narrative")}
    f2 = CACHE_DIR / "tradfi_fundamentals_cache.json"
    if f2.exists():
        try:
            with f2.open(encoding="utf-8") as fh:
                tf = json.load(fh)
            for sec in tf.get("sectors", []):
                for st in sec.get("stocks", []):
                    sym = st.get("symbol")
                    if sym in u:
                        if st.get("beta") is not None:
                            u[sym]["beta"] = st["beta"]
                        if st.get("currency"):
                            u[sym]["devise_cotation"] = st["currency"]
        except Exception:
            pass
    return u


def univers_marche(tranche=None, plafond=None):
    """L'univers de la collecte de marché, trié par capitalisation en dollars.

    On y prend le symbole, le nom et le chemin chez la source — ce dernier est
    déjà résolu par le collecteur d'univers, donc pas une seule requête de
    recherche à refaire ici.

    `tranche` vaut (i, n) : on ne garde qu'une société sur n, celles dont le
    PAQUET modulo n vaut i.

    ⚠ SUR LE PAQUET, ET NON SUR LE RANG DE CAPITALISATION.
    Le découpage se faisait sur le rang, pour que chaque tranche contienne un
    échantillon de toutes les tailles — sinon la tranche du lundi ne verrait que
    des mégacapitalisations et celle du dimanche que des microcaps. L'intention
    était juste ; son coût, invisible.

    Le rang de capitalisation n'a aucun rapport avec l'empreinte qui choisit le
    paquet. Les sociétés d'une tranche se répartissaient donc sur les CINQ CENT
    DOUZE paquets, et les cinq cent douze fichiers changeaient chaque jour. Or
    ces fichiers sont versionnés par git, qui ajoute chaque version à son
    historique et n'en retire jamais aucune. Mesuré le 28/08/2026 sur le jeu
    américain, de même forme : dix-sept mégaoctets compressés par jour, un dépôt
    à 271 Mo qui franchissait le gigaoctet en six semaines.

    En découpant sur le paquet, la tranche du jour n'en touche que soixante-treize.
    L'échantillon de tailles est préservé : l'empreinte est un hachage du symbole,
    sans corrélation avec la capitalisation — vérifié, les sept tranches ont la
    même capitalisation médiane à 15 % près. On troque une stratification exacte
    contre un tirage aléatoire, et on divise le coût par sept.
    """
    f = CACHE_DIR / "univers_actions.json"
    if not f.exists():
        return {}
    with f.open(encoding="utf-8") as fh:
        u = json.load(fh)
    # Le chemin de la source, par symbole — ET LA COTATION.
    #
    # Le cours vivait jusqu'ici dans `tradfi_history_cache`, qui ne couvre que
    # les quelque huit cents titres du tracker. Sur les 11 635 sociétés de la
    # collecte de marché, 93,6 % n'avaient donc ni cours ni capitalisation :
    # ni P/E, ni P/B, ni rendement, ni prix juste — la moitié de la fiche
    # éteinte, sans que rien ne le signale. Mesuré le 28/08/2026 sur ERAMET,
    # dont le cours (45,12 €) était pourtant à deux clés de là, dans
    # `univers_actions.json`.
    #
    # Le cours y est en devise de COTATION, pas en dollars. C'est une bonne
    # nouvelle — c'est ce qu'il faut pour la fiche — mais cela impose de le
    # convertir autrement que la série du tracker, qui est en dollars. D'où
    # `devise_cotation`, transporté avec lui : sans elle, on referait le bug
    # Toyota à P/E 0,1, où un cours en yens rencontrait des états en dollars.
    chemins, cotations = {}, {}
    for t in u.get("titres", []):
        sym = t.get("yahoo") or t.get("sa")
        if sym and t.get("principal"):
            chemins[sym] = t.get("sa")
            px = t.get("cours")
            if isinstance(px, (int, float)) and px > 0:
                cotations[sym] = (px, (t.get("devise") or "").upper() or None)

    import glob as _glob
    lignes = []
    # Le cours vu par la collecte de marché, par symbole. Sert uniquement à
    # démasquer les cours rangés en sous-unité dans l'univers.
    cours_ref = {}
    for p in _glob.glob(str(CACHE_DIR / "marche_[0-9][0-9].json")):
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        ch = d.get("champs") or []
        try:
            i_nom = ch.index("name")
            i_capi = ch.index("marketCapUsd")
        except ValueError:
            continue
        # Le bêta est la vingtième des 97 colonnes de ce fichier, renseigné sur
        # 27 017 des 37 574 cotations. Sans lui, `_wacc` rend None — et il
        # rendait None sur 19 482 sociétés sur 19 495, faute d'être lu.
        #
        # Hors du `try` ci-dessus, volontairement : un fichier qui n'aurait pas
        # la colonne ne doit pas être écarté en entier, il porte quand même son
        # nom et sa capitalisation.
        i_beta = ch.index("beta") if "beta" in ch else None
        # ── DE QUOI DÉMASQUER UN COURS EN SOUS-UNITÉ ──
        # `capitalisation ÷ nombre d'actions` EST le cours, dans la devise de la
        # place. C'est le seul témoin qui ne dépende ni d'une liste de places ni
        # d'une convention de nommage.
        i_mc = ch.index("marketCap") if "marketCap" in ch else None
        i_sh = ch.index("sharesOut") if "sharesOut" in ch else None
        # ── LES VARIATIONS DE COURS, POUR LA CAPITALISATION HISTORIQUE ──
        #
        # ch1y, ch3y, ch5y, ch10y donnent le rapport entre le cours d'alors et
        # celui d'aujourd'hui. Quatre ancres qui font passer le coût du capital
        # d'une barre à trois pour 12 769 sociétés. Elles sont dans ce fichier
        # depuis toujours et personne ne les lisait.
        #
        # ⚠ `genere_le` ET NON `updated` : c'est sous ce nom que ces fichiers
        # datent leur écriture. Chercher `updated` rend None sans lever d'erreur
        # et fait taire la reconstruction entière.
        i_ch = {n: ch.index(n) for n in ("ch1y", "ch3y", "ch5y", "ch10y")
                if n in ch}
        jour_m = (d.get("genere_le") or d.get("updated") or "")[:10] or None
        for sym, v in (d.get("societes") or {}).items():
            if sym not in chemins:
                continue
            # ⚠ PASSÉ PAR LA BANDE. Sans elle, Elcid Investments entre avec un
            # bêta de −20 833 et Compass Gas e Energia avec 95,39 : `_wacc` en
            # tire des coûts du capital à quatre chiffres, et la tuile « ROIC
            # contre WACC » affiche une destruction de valeur inventée. La règle
            # et le relevé qui a fixé ±8 sont dans `fondamentaux_communs`.
            b = beta_plausible(
                v[i_beta] if (i_beta is not None and i_beta < len(v)) else None)
            var = cours_ancres(
                1.0,
                *[v[i_ch[n]] if (n in i_ch and i_ch[n] < len(v)) else None
                  for n in ("ch1y", "ch3y", "ch5y", "ch10y")])
            # Le cours de référence, quand les deux colonnes sont là.
            _mc = (v[i_mc] if (i_mc is not None and i_mc < len(v)) else None)
            _sh = (v[i_sh] if (i_sh is not None and i_sh < len(v)) else None)
            if (isinstance(_mc, (int, float)) and isinstance(_sh, (int, float))
                    and _mc and _sh):
                cours_ref[sym] = _mc / _sh
            lignes.append((v[i_capi] or 0, sym, v[i_nom], b, var or None, jour_m))
    lignes.sort(reverse=True)
    if plafond:
        lignes = lignes[:plafond]
    if tranche:
        i, n = tranche
        lignes = [x for x in lignes if int(_initiale(x[1])) % n == i]
    # Les places qui cotent en SOUS-UNITÉ par convention de bourse. Ce n'est pas
    # une déduction sur les chiffres, c'est une règle du marché : Johannesburg
    # cote en centimes de rand, Londres en pence, Tel-Aviv en agorot.
    #
    # Le seuil dit à partir de quand l'unité principale n'a plus de sens
    # boursier. Une action à 1 000 £ existe (quelques-unes à Londres), une action
    # à 5 000 £ non — c'est un cours en pence.
    PLACES_SOUS_UNITE = {
        "ZAR": ("centimes de rand", 1500.0),
        "GBP": ("pence", 1000.0),
        "ILS": ("agorot", 1500.0),
    }

    def _cours_sous_unite(px, ref, devise=None):
        """Le cours est-il en pence, agorot ou centimes plutôt qu'en unité ?

        Rend le cours corrigé, et un drapeau disant si la correction a eu lieu.

        DEUX CHEMINS, dans cet ordre :

        1. LA RÉFÉRENCE — `mcap / sharesOut`. On n'accepte QUE le facteur cent,
           et seulement s'il est net : entre 50 et 200. En dessous, c'est un
           décalage de date de cotation — les deux fichiers ne sont pas écrits à
           la même minute — et corriger remplacerait un cours frais par un cours
           d'hier.

        2. LA PLACE, quand la référence manque. Sans ce repli, le cours restait
           en centimes SANS QUE RIEN NE LE SIGNALE. Mesuré le 30/08/2026 sur cinq
           paquets : 5 fiches sur 12 en sous-unité, soit 42 % — FSR.JO 9 613 pour
           96 R, SHP.JO 30 700 pour 307 R, STJ.L 1 176 pour 11,77 £, DANE.TA
           42 500 pour 425 ₪. Sur la même place, Standard Bank était corrigée
           (elle avait une référence) et FirstRand non : deux fiches justes, trois
           fausses d'un facteur cent, dans le même tableau.

        ⚠ La référence garde la priorité : elle MESURE la société, alors que la
        place ne fait qu'appliquer une convention. Quand les deux existent, la
        mesure gagne.
        """
        if not (isinstance(px, (int, float)) and px > 0):
            return px, False
        # 1. La mesure, quand elle existe.
        if isinstance(ref, (int, float)) and ref > 0:
            r = px / ref
            return (px / 100.0, True) if 50.0 <= r <= 200.0 else (px, False)
        # 2. La convention de place, à défaut.
        conv = PLACES_SOUS_UNITE.get((devise or "").upper())
        if conv and px > conv[1]:
            return px / 100.0, True
        return px, False

    n_sous_unite = 0
    out = {}
    for capi, sym, nom, beta, var, jour_m in lignes:
        px, dev = cotations.get(sym, (None, None))
        # ── LE COURS EN SOUS-UNITÉ ──
        #
        # `univers_actions.json` range le cours de Londres, de Tel-Aviv et de
        # Johannesburg en pence, agorot et centimes, sous l'étiquette de la
        # devise majeure. Vérifié : Reckitt Benckiser à 5 116 GBP pour 51,32
        # réels, AstraZeneca à 12 114 pour 119,70, NICE à 31 410 ILS pour 310,50.
        # Tous leurs multiples étaient faux d'un facteur cent.
        #
        # On ne se fie pas à une liste de places : une première mesure par place
        # donnait Londres à 1,00, sa médiane étant diluée par les cotations
        # secondaires étrangères. C'est l'invariant qui tranche, société par
        # société.
        px, _corr = _cours_sous_unite(px, cours_ref.get(sym), dev)
        if _corr:
            n_sous_unite += 1
        out[sym] = {
            "nom": nom, "capi_usd": capi, "chemin_sa": chemins.get(sym),
            # `mcap` et non `capi_usd` seul : la boucle principale lit
            # `meta.get("mcap")`, et lisait donc None sur tout l'univers de
            # marché. La capitalisation était calculée, rangée, puis jamais
            # relue — un écart d'un seul nom de clé, invisible parce qu'il ne
            # produit aucune erreur, seulement des cases vides.
            "mcap": capi or None,
            "cours_cotation": px,
            "devise_cotation": dev,
            # La boucle principale lit déjà `meta.get("beta")` : il ne manquait
            # que de le mettre là. Sans lui, `_wacc` rend None, et avec lui
            # `roic_moins_wacc` — « le rendement du capital dépasse-t-il son
            # coût ? » — cesse d'être vide sur tout l'univers mondial.
            "beta": beta,
            # Les quatre ancres de cours et la date du fichier qui les porte :
            # sans elles, `combler_mcap_par_ancres` se tait.
            "variations": var,
            "jour_marche": jour_m,
        }
    return out


def charger_archive(symboles):
    """Les exercices que NOS passages précédents ont écrits, pour ces symboles.

    POURQUOI CETTE FONCTION EXISTE, ET CE QUE SON ABSENCE COÛTAIT

    La source principale sert une fenêtre GLISSANTE de cinq exercices. L'exercice
    2020 qu'elle servait l'an dernier n'existe plus nulle part — sauf dans le
    paquet que nous avons écrit à l'époque. Or la fusion de fin de passage
    REMPLAÇAIT la société entière : `garde.update(contenu)` écrasait l'ancienne
    entrée par la nouvelle, exercices compris. Chaque passage détruisait donc un
    exercice par an, définitivement, sans qu'aucun voyant ne s'allume : le
    fichier restait valide, la société restait présente, elle avait simplement
    perdu son année la plus ancienne.

    En la relisant AVANT de construire, l'exercice tombé de la fenêtre rentre
    dans le calcul comme les autres : médianes, croissances, prédictibilité et
    note historique le voient. La profondeur s'accroît alors d'un exercice par
    an, gratuitement et pour toujours.

    On ne lit QUE les paquets concernés : l'univers entier pèse cent cinquante
    mégaoctets, la tranche du jour une vingtaine.
    """
    besoins = {}
    for s in symboles:
        besoins.setdefault(_initiale(s), set()).add(s)
    out = {}
    for lettre, syms in besoins.items():
        chemin = OUT_DIR / ("intl_detail_%s.json" % lettre)
        if not chemin.exists():
            continue
        try:
            with chemin.open(encoding="utf-8") as fh:
                d = json.load(fh) or {}
        except Exception as e:
            print("[warn] archive %s illisible : %s" % (chemin.name, e), file=sys.stderr)
            continue
        soc = d.get("societes") or {}
        for s in syms:
            fiche = soc.get(s) or {}
            ex = fiche.get("exercices")
            if isinstance(ex, list) and ex:
                # ⚠ ON REPREND AUSSI LE DOSSIER DE RACCORD. Les exercices repris
                # portent bien leur source, mais la PREUVE qu'ils se raccordaient
                # — l'écart mesuré grandeur par grandeur — vivait dans le résumé
                # du passage qui les a écrits. Sans elle, un passage où le second
                # fournisseur n'a pas répondu garderait ses lignes et perdrait
                # leur justification : des chiffres sans dossier, ce que ce dépôt
                # refuse ailleurs.
                out[s] = {"exercices": ex,
                          "tradingview": (fiche.get("resume") or {}).get("tradingview")}
        # On relâche le paquet tout de suite : garder les 512 en mémoire ferait
        # cent cinquante mégaoctets pour quelques milliers de lignes utiles.
        del d, soc
    return out


def fusionner_societe(ancienne, nouvelle):
    """Deux versions d'une même société → une seule, par ANNÉE d'exercice.

    ⚠ CE N'EST PLUS UN REMPLACEMENT. L'ancienne version prenait la société
    entière du passage le plus récent et jetait la précédente. Une société que
    la collecte du jour n'a vue qu'à demi — quatre exercices au lieu de dix
    parce que la source a bridé, ou parce que le second fournisseur n'a pas
    répondu — perdait donc six exercices pour de bon.

    On fusionne maintenant les EXERCICES : la version fraîche gagne à année
    égale (c'est elle qui porte les retraitements), et les années qu'elle ne
    contient pas sont reprises de l'ancienne, marquées `archive`.

    Le reste de la fiche — nom, résumé, note — vient de la version fraîche : ces
    champs-là décrivent le dernier état connu, pas une accumulation.
    """
    ex_new = (nouvelle or {}).get("exercices") or []
    ex_old = (ancienne or {}).get("exercices") or []
    if not ex_old:
        return nouvelle, 0
    connues = {e.get("annee") for e in ex_new if isinstance(e, dict)}
    repris = []
    for e in ex_old:
        if not isinstance(e, dict) or e.get("annee") in connues:
            continue
        ligne = dict(e)
        ligne["archive"] = True
        ligne.setdefault("source", "stockanalysis")
        repris.append(ligne)
        connues.add(e.get("annee"))
    if not repris:
        return nouvelle, 0
    fusion = dict(nouvelle)
    fusion["exercices"] = sorted(ex_new + repris,
                                 key=lambda x: (x.get("annee") is None, x.get("annee")))
    # ⚠ LE RÉSUMÉ SE REFAIT. Sans ça, `n_exercices`, les médianes à dix ans et
    # la note décriraient la version amputée pendant que le tableau, lui,
    # afficherait les exercices repris. `construire_resume` existe justement
    # pour se rejouer hors ligne, sans une requête.
    try:
        anc = (ancienne or {}).get("resume") or {}
        fusion["resume"] = dict((nouvelle.get("resume") or {}),
                                **construire_resume(
                                    fusion["exercices"],
                                    (nouvelle.get("resume") or {}).get("divisions_action")
                                    or anc.get("divisions_action"),
                                    (nouvelle.get("resume") or {}).get("unites_actions_corrigees")
                                    or anc.get("unites_actions_corrigees")))
    except Exception as e:
        print("[warn] résumé non recalculé après fusion : %s" % e, file=sys.stderr)
    # Et le dossier de raccord suit ses lignes : sans lui, les exercices repris
    # gardent leur source mais perdent la preuve qu'ils s'y raccordaient.
    anc_tv = ((ancienne or {}).get("resume") or {}).get("tradingview")
    if anc_tv and not (fusion.get("resume") or {}).get("tradingview"):
        fusion["resume"] = dict(fusion.get("resume") or {},
                                tradingview=dict(anc_tv, repris_de_l_archive=True))
    return fusion, len(repris)


def fusionner_paquets(paquets):
    """Ajoute la tranche du jour aux paquets déjà écrits.

    Sans cette fusion, chaque passage effacerait les six autres tranches : le
    collecteur écrit un fichier par initiale, et une tranche n'en contient qu'un
    septième. On relit donc l'existant, on fusionne les sociétés qu'on vient de
    collecter, on garde les autres.

    Rend `(paquets, sociétés reprises telles quelles, exercices sauvés d'une
    société recollectée)`.
    """
    import glob as _glob
    fusionnes, repris, exercices_sauves = {}, 0, 0
    # Tous les paquets existants, pas seulement ceux que la tranche touche :
    # sinon un fichier qu'aucune société du jour ne concerne serait absent de
    # la sortie et resterait figé au dernier passage qui l'a écrit.
    for p in _glob.glob(str(OUT_DIR / "intl_detail_*.json")):
        lettre = Path(p).stem.replace("intl_detail_", "")
        paquets.setdefault(lettre, {})
    for lettre, contenu in paquets.items():
        chemin = OUT_DIR / ("intl_detail_%s.json" % lettre)
        ancien = {}
        if chemin.exists():
            try:
                with chemin.open(encoding="utf-8") as fh:
                    ancien = (json.load(fh) or {}).get("societes") or {}
            except Exception:
                ancien = {}
        garde = {k: v for k, v in ancien.items() if k not in contenu}
        repris += len(garde)
        # ⚠ ET POUR CELLES QUE LE PASSAGE A REVUES, ON FUSIONNE PAR ANNÉE.
        # `charger_archive` a déjà fait ce travail en amont pour l'immense
        # majorité — ce filet rattrape les cas où il n'a pas pu : sortie
        # détournée, paquet écrit entre-temps, société changée de paquet.
        for sym, fiche in contenu.items():
            av = ancien.get(sym)
            if av:
                fiche, n = fusionner_societe(av, fiche)
                exercices_sauves += n
            garde[sym] = fiche
        fusionnes[lettre] = garde
    # Les paquets qu'aucune société de la tranche ne touche restent tels quels.
    return fusionnes, repris, exercices_sauves


PAQUETS_INTL = 512


def _initiale(sym):
    """Le paquet où ranger une société : une EMPREINTE, pas un préfixe.

    Découper sur les premiers caractères suit la langue et non la donnée. Une
    lettre mettait 3,9 Mo dans le paquet « A » ; deux caractères en laissaient
    3 dans « 60 », parce que tous les codes de Shanghai commencent par là. Un
    troisième déplacerait le problème sans le résoudre.

    Modulo cinq cent douze, les paquets sont réguliers quelle que soit la place :
    cent dix kilo-octets à quatre mille sociétés, trois cent cinquante à douze
    mille.

    ⚠ La fiche connaît la MÊME empreinte, dans `paquetDe()`. Elle est primitive
    exprès : une empreinte savante qui divergerait entre Python et JavaScript
    produirait des fiches vides sans le moindre message d'erreur.
    """
    t = (sym or "?").upper()
    h = 0
    for c in t:
        h = (h * 31 + ord(c)) % 4294967296
    return "%03d" % (h % PAQUETS_INTL)


def _options(argv):
    o = {"tickers": None, "limit": None, "sortie": None, "source": "suivi",
         "tranche": None, "parallele": 1, "plafond": None,
         # Les deux leviers de profondeur se coupent SÉPARÉMENT : ils ne
         # dépendent pas du même fournisseur et ne tombent pas ensemble.
         "sans_tv": False, "sans_ratios": False,
         # Le repli sur nos propres passages ne se coupe que pour un contrôle :
         # le couper en production détruit un exercice par an.
         "sans_archive": False}
    for i, a in enumerate(argv):
        if a == "--sans-tv":
            o["sans_tv"] = True
        elif a == "--sans-ratios":
            o["sans_ratios"] = True
        elif a == "--sans-archive":
            o["sans_archive"] = True
        elif a == "--tickers" and i + 1 < len(argv):
            o["tickers"] = {t.strip().upper() for t in argv[i + 1].split(",") if t.strip()}
        elif a == "--limit" and i + 1 < len(argv):
            o["limit"] = int(argv[i + 1])
        elif a == "--sortie" and i + 1 < len(argv):
            o["sortie"] = Path(argv[i + 1]).expanduser()
        elif a == "--source" and i + 1 < len(argv):
            o["source"] = argv[i + 1]
        elif a == "--parallele" and i + 1 < len(argv):
            o["parallele"] = max(1, min(12, int(argv[i + 1])))
        elif a == "--plafond" and i + 1 < len(argv):
            o["plafond"] = int(argv[i + 1])
        elif a == "--tranche" and i + 1 < len(argv):
            v = argv[i + 1]
            if v == "auto":
                # Le jour de la semaine : lundi 0, dimanche 6. L'univers entier
                # est donc parcouru en sept jours, sans registre à tenir.
                o["tranche"] = (datetime.now(timezone.utc).weekday(), 7)
            else:
                a2, b2 = v.split("/")
                o["tranche"] = (int(a2), int(b2))
    return o


def precharger(univers, parallele):
    """Va chercher les états de tout le monde, en parallèle, puis rend un dict.

    Le réseau est le seul goulot : construire les exercices prend quelques
    millisecondes, télécharger quatre pages en prend presque une seconde. On
    parallélise donc la seule descente, et la construction reste séquentielle —
    elle touche des états partagés et ne gagnerait rien à être concurrente.

    Le débit de politesse global est levé pendant cette phase : ce sont les huit
    fils en vol qui bornent la cadence, à une trentaine de requêtes par seconde.
    """
    import concurrent.futures as _cf
    global DEBIT
    ancien_debit = DEBIT
    if parallele > 1:
        DEBIT = 0.0

    def un(item):
        sym, meta = item
        chemin = meta.get("chemin_sa")
        if chemin and not chemin.startswith("quote/") and "/" in chemin:
            chemin = "quote/" + chemin
        elif chemin and "/" not in chemin:
            chemin = "stocks/" + chemin
        if not chemin:
            chemin = chemin_du_titre(sym)
        brut = etats(chemin) if chemin else None
        trouve_par_recherche = False
        if brut is None:
            # ⚠ LA PLACE ATTENDUE VIENT DU CHEMIN DÉJÀ CONSTRUIT, pas d'une
            # nouvelle déduction : `chemin` a été bâti par `chemin_du_titre` ou
            # fourni par `meta["chemin_sa"]`, tous deux vérifiés en amont.
            # Sans elle, le repli adoptait le premier ticker homonyme trouvé sur
            # n'importe quelle bourse du monde — 118 fiches portaient ainsi les
            # états d'un autre émetteur.
            _p = (chemin or "").split("/")
            place_att = _p[1] if len(_p) > 2 else None
            autre = chercher_chemin(sym, meta.get("nom"), place_att)
            if autre and autre != chemin:
                brut = etats(autre)
                if brut is not None:
                    chemin = autre
                    trouve_par_recherche = True
            if brut is None:
                # ── SECONDE PISTE : PAR LE NOM, SANS CONTRAINTE DE PLACE ──
                #
                # La première passe interroge la recherche avec le TICKER, qui ne
                # rend que des reflets de la même place — dont celui qui vient
                # d'échouer. Mesuré : 102 sociétés de plus de 20 Md$ restaient
                # sans aucun état pour cette seule raison, 4 763 Md$ — Roche,
                # CaixaBank, Atlas Copco, Hon Hai, Nordea, SingTel.
                #
                # Interrogée par le NOM, la recherche rend la cotation d'origine
                # en premier. On essaie jusqu'à trois candidats ; le premier qui
                # rend des états gagne.
                #
                # ⚠ Chaque candidat a déjà passé `_noms_concordent`. On relâche
                # la place, jamais le nom.
                for cand in chercher_chemins_par_nom(meta.get("nom"),
                                                     exclure=chemin):
                    b2 = etats(cand)
                    if b2 is not None:
                        brut, chemin = b2, cand
                        trouve_par_recherche = True
                        break
        return sym, brut, chemin, trouve_par_recherche

    out = {}
    items = sorted(univers.items())
    try:
        with _cf.ThreadPoolExecutor(max_workers=parallele) as ex:
            for k, r in enumerate(ex.map(un, items), 1):
                out[r[0]] = r[1:]
                if k % 500 == 0:
                    print("[info] %d/%d descendues" % (k, len(items)))
    finally:
        DEBIT = ancien_debit
    return out


def main():
    global OUT_JSON, OUT_JS, OUT_DIR, AVEC_RATIOS, AVEC_TV
    t0 = time.time()
    opts = _options(sys.argv[1:])
    AVEC_RATIOS = not opts["sans_ratios"]
    AVEC_TV = not opts["sans_tv"]
    if opts["sortie"]:
        opts["sortie"].mkdir(parents=True, exist_ok=True)
        OUT_DIR = opts["sortie"]
        OUT_JSON = OUT_DIR / "intl_fundamentals_index.json"
        OUT_JS = OUT_DIR / "intl_fundamentals_index.js"
        print("[info] sortie détournée vers %s" % OUT_DIR)

    if opts["source"] == "marche":
        univers = univers_marche(opts["tranche"], opts["plafond"])
        # Un univers VIDE n'est pas un resultat, c'est une panne. Sans ce
        # refus, le collecteur parcourait zero societe, n'ecrivait rien et
        # sortait en SUCCES — et l'univers profond aurait cesse de vivre
        # sans qu'aucun voyant ne s'allume. Ce depot a deja paye ce genre
        # de silence : dix-sept caches figes seize jours derriere un bilan
        # a 26/26 OK.
        if not univers:
            raise SystemExit(
                "[fatal] univers de marche demande mais vide : "
                "univers_actions.json ou marche_NN.json manquent dans le cache. "
                "Le collecteur de marche a-t-il tourne avant celui-ci ?")
        quoi = "collecte de marché"
        if opts["tranche"]:
            quoi += " — tranche %d sur %d" % (opts["tranche"][0] + 1, opts["tranche"][1])
    else:
        univers = charger_univers()
        quoi = "univers suivi, non américain"
    if opts["tickers"]:
        univers = {k: v for k, v in univers.items() if k.upper() in opts["tickers"]}
    if opts["limit"]:
        univers = dict(sorted(univers.items())[: opts["limit"]])
    if not univers:
        return 1
    print("[info] %s : %d titres" % (quoi, len(univers)))
    cours = charger_cours()
    fx = charger_fx()
    # La date des cotations de l'univers. Elle date le cours de repli : sans
    # elle, un cours d'il y a six jours se confondrait avec un cours du jour, et
    # la conversion de change se ferait au mauvais taux.
    jour_univers = None
    try:
        with (CACHE_DIR / "univers_actions.json").open(encoding="utf-8") as fh:
            jour_univers = str((json.load(fh) or {}).get("updated") or "")[:10] or None
    except Exception:
        pass

    # Le réseau d'abord, tout entier, en parallèle. La construction ensuite,
    # séquentielle : elle ne gagnerait rien à être concurrente et touche des
    # états partagés.
    precharges = precharger(univers, opts["parallele"])
    print("[info] descente finie en %.1f s" % (time.time() - t0))

    # ── LE SECOND FOURNISSEUR, EN LOTS, UNE SEULE FOIS POUR TOUT L'UNIVERS ──
    # Dix requêtes pour dix-neuf mille sociétés, contre quatre par société chez
    # la source principale. Le coût est négligeable ; ce qu'il rapporte est une
    # profondeur médiane de dix-huit exercices au lieu de cinq.
    tv_series, tv_bilan = {}, {}
    if AVEC_TV:
        t_tv = time.time()
        tv_series, tv_bilan = series_tv(sorted(univers))
        print("[info] TradingView : %d/%d sociétés servies en %d lot(s), %.1f s"
              % (tv_bilan.get("repondus", 0), tv_bilan.get("vises", 0),
                 tv_bilan.get("lots", 0), time.time() - t_tv))

    # ── NOS PROPRES PASSAGES, RELUS AVANT DE CONSTRUIRE ──
    # La fenêtre de la source principale glisse : l'exercice le plus ancien
    # qu'elle servait l'an dernier n'existe plus que dans nos paquets.
    archives = {}
    if not opts["sans_archive"]:
        archives = charger_archive(sorted(univers))
        print("[info] archive : %d société(s) déjà écrites relues" % len(archives))

    index, paquets = {}, {}
    ok = sans_place = echecs = 0
    par_recherche = 0
    # Ce que les deux leviers ont réellement rapporté, compté sur les lignes
    # écrites et non sur les intentions.
    ajouts = {"tradingview": 0, "archive": 0, "societes_tv": 0,
              "refus_tv": {}, "mcap_publiee": 0}
    for i, (sym, meta) in enumerate(sorted(univers.items()), 1):
        brut, chemin, trouve = precharges.get(sym, (None, None, False))
        if trouve:
            par_recherche += 1
        if brut is None:
            if chemin is None:
                sans_place += 1
            else:
                echecs += 1
            continue

        ctx = brut.get("contexte") or {}
        devise_etats = (ctx.get("currency") or "").upper() or None
        devise_cot = (meta.get("devise_cotation") or "").upper() or None
        # Information, non plus garde-fou. Elle l'a été : tant que la
        # capitalisation restait en dollars, tout rapprochement avec un montant
        # d'état publié dans une autre devise était faux, et on l'interdisait —
        # au prix de quatre-vingts sociétés privées de coût du capital. Depuis que
        # le cours est ramené à la devise des états au taux de SA date, la
        # divergence n'empêche plus rien ; elle reste affichée parce qu'elle dit
        # au lecteur que deux devises sont en jeu (Shell cote en pence à Londres
        # et publie en dollars).
        brut["_devises_alignees"] = bool(devise_etats and devise_cot and devise_etats == devise_cot)

        try:
            bati = construire(brut, meta.get("mcap"), meta.get("beta"), cours.get(sym),
                              fx_dev=fx.get(devise_etats), devise=devise_etats,
                              variations=meta.get("variations"),
                              jour_marche=meta.get("jour_marche"),
                              tv=tv_series.get(sym), fx=fx,
                              archive=archives.get(sym))
        except Exception as e:
            print("[warn] %s : %s" % (sym, e), file=sys.stderr)
            echecs += 1
            continue
        if not bati:
            echecs += 1
            continue

        prov = bati.get("provenance") or {}
        ajouts["tradingview"] += prov.get("tradingview", 0)
        ajouts["archive"] += prov.get("archive", 0)
        if prov.get("tradingview"):
            ajouts["societes_tv"] += 1
        _d = (bati.get("resume") or {}).get("tradingview") or {}
        if _d.get("refus"):
            # Le motif du refus, pas seulement son compte : « symbole douteux »
            # et « devise absente du cache » n'appellent pas la même correction.
            _m = _d["refus"].split(" :")[0]
            ajouts["refus_tv"][_m] = ajouts["refus_tv"].get(_m, 0) + 1
        ajouts["mcap_publiee"] += sum(
            1 for e in bati["exercices"] if e.get("mcap_publie"))

        r = bati["resume"]
        r["devise"] = devise_etats
        r["cours_natif"], r["cours_natif_le"] = _dernier_cours(cours.get(sym))
        r["cours_natif"] = _en_devise_etats(r["cours_natif"], r["cours_natif_le"],
                                           fx.get(devise_etats), devise_etats)
        # Repli sur la cotation de l'univers. La série du tracker ne couvre que
        # ses quelque huit cents titres ; l'univers en porte 46 992 sur 47 269
        # cotations principales, soit 99,4 %. Sans ce repli, 93,6 % des sociétés
        # de la collecte de marché n'avaient aucun cours — et donc ni P/E, ni
        # P/B, ni rendement, ni prix juste.
        r["cours_source"] = "tracker" if r["cours_natif"] is not None else None
        if r["cours_natif"] is None:
            r["cours_natif"] = _cotation_vers_etats(
                meta.get("cours_cotation"), devise_cot, devise_etats, fx)
            if r["cours_natif"] is not None:
                r["cours_natif_le"] = jour_univers
                r["cours_source"] = "univers"
        # ⚠ UNE FICHE TROUVÉE PAR RECHERCHE DOIT SE DÉCLARER. Le drapeau
        # existait déjà mais n'alimentait qu'un compteur de fin de passage : rien
        # dans le paquet ne distinguait une fiche bâtie sur le chemin attendu
        # d'une fiche bâtie sur un chemin deviné. C'est ce silence qui a rendu
        # 161 substitutions invisibles pendant des semaines.
        if brut.get("_trouve_par_recherche"):
            r["chemin_par_recherche"] = True
        r["devise_cotation"] = devise_cot
        r["devises_alignees"] = brut["_devises_alignees"]
        # ⚠ CE QUI A RÉELLEMENT ÉTÉ FAIT DES GRANDEURS DE MARCHÉ.
        #
        # La fiche annonçait « capitalisation, cours et coût du capital laissés
        # vides plutôt que convertis » dès que les devises divergent. C'est vrai
        # de la filière SEC, qui efface. C'est FAUX ici : `_en_devise_etats`
        # convertit au taux DATÉ de la clôture, et rend None si le taux est
        # inconnu. Mesuré le 29/08/2026 : 1 731 sociétés marquées « non
        # alignées », dont 1 454 affichent bel et bien une capitalisation
        # (Galderma 50,5 Md$, Yara 10,1 Md$, Temenos 6,9 Md$).
        #
        # On ne retire pas la garde — elle est juste — on dit ce qu'elle a fait.
        if not brut["_devises_alignees"]:
            r["montants_marche"] = "convertis"
        r["frequence_publication"] = ctx.get("reportingFrequency")
        r["source_url"] = BASE + "/" + chemin + "/financials/"
        # Une tranche par jour veut dire qu'une donnée peut avoir six jours.
        # Ce n'est pas un défaut — un état financier change une fois par
        # trimestre — mais il faut que ça se VOIE, sinon une ligne vieille de
        # trois semaines se confond avec une fraîche.
        r["collecte_le"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r["source"] = "stockanalysis.com (données S&P Global Market Intelligence)"
        # ⚠ « source » AU SINGULIER NE SUFFIT PLUS. Un exercice de 2009 ne vient
        # pas de la même maison qu'un exercice de 2025 : la fiche doit pouvoir
        # nommer chacune. Le détail par exercice est dans `exercices[].source` ;
        # cette table dit ce que chaque clef signifie, pour que la fiche n'ait
        # pas à le deviner ni à le coder en dur de son côté.
        r["sources"] = {
            "stockanalysis": {
                "libelle": "stockanalysis.com (données S&P Global Market Intelligence)",
                "url": r["source_url"]},
        }
        if (bati.get("provenance") or {}).get("tradingview"):
            r["sources"]["tradingview"] = {
                "libelle": "scanner TradingView — séries annuelles `_fy_h`",
                "url": "https://www.tradingview.com/symbols/%s/"
                       % ((tv_series.get(sym) or {}).get("_symbole_tv") or "").replace(":", "-")}

        paquets.setdefault(_initiale(sym), {})[sym] = {
            "symbole": sym, "nom": meta.get("nom"),
            "exchange": meta.get("exchange"), "region": meta.get("region"),
            "source": r["source"], "source_url": r["source_url"],
            "exercices": bati["exercices"], "resume": r,
        }
        allege = dict(r)
        allege.pop("piotroski_detail", None)
        allege.pop("altman_detail", None)
        # Le détail des vingt critères et l'historique de la note restent dans le
        # paquet de détail, que la fiche charge de toute façon à l'ouverture.
        # Les laisser ici coûtait 3 Ko par société, soit 2,7 Mo d'index pour une
        # information lue ailleurs. L'index ne garde que de quoi TRIER et FILTRER.
        allege.pop("note_historique", None)
        if isinstance(allege.get("note_q"), dict):
            allege["note_q"] = {k: v for k, v in allege["note_q"].items()
                            if k not in ("details", "criteres_muets", "criteres_nuls_par_nature")}

        index[sym] = allege
        ok += 1
        if i % 50 == 0:
            print("[info] %d/%d — %d construites" % (i, len(univers), ok))
        if opts["limit"] and ok >= opts["limit"]:
            break

    charge = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": "stockanalysis.com — états financiers publiés, données S&P Global",
        "duree_s": round(time.time() - t0, 1),
        "exhaustivite": {"univers": len(univers), "construites": ok,
                         "place_inconnue": sans_place, "echecs": echecs,
                         "retrouves_par_recherche": par_recherche,
                         "tradingview": dict(tv_bilan,
                                             societes_completees=ajouts["societes_tv"],
                                             exercices_ajoutes=ajouts["tradingview"],
                                             refus=ajouts["refus_tv"]),
                         "archive_exercices_repris": ajouts["archive"],
                         "mcap_publiee_exercices": ajouts["mcap_publiee"]},
        "limites": [
            "Profondeur INÉGALE : cinq exercices complets par la source principale, "
            "jusqu'à vingt de plus par le scanner TradingView, et les exercices "
            "sauvés de nos propres passages. Chaque exercice porte sa source.",
            "Les exercices TradingView n'ont NI passif, NI capitaux propres, NI flux "
            "d'exploitation, NI nombre d'actions : ce fournisseur ne sert pas ces "
            "quatre séries. ROE, ROIC, Piotroski et grandeurs par action restent "
            "donc vides sur eux — vides, et non comblés.",
            "Une grandeur TradingView n'est retenue que si elle concorde à 5 % près "
            "avec la source principale sur les exercices communs. L'EBITDA et le "
            "résultat brut sont fréquemment écartés à ce titre : deux fournisseurs "
            "ne les définissent pas pareil.",
            "Montants en devise NATIVE, jamais convertis : un bilan de 2021 converti au cours d'aujourd'hui n'a jamais existé.",
            "Source non primaire (S&P Global revendu) : les lignes composées — résultat d'exploitation, EBITDA — sont des retraitements du fournisseur.",
            "Divisions d'action déjà rétro-ajustées par la source : les bénéfices par action diffèrent des rapports publiés à l'époque.",
        ],
        "paquets": sorted(paquets.keys()),
        "societes": index,
    }
    if OUT_JSON.exists():
        # Même raison que pour les paquets : l'index porte tout l'univers, la
        # tranche n'en connaît qu'un septième.
        try:
            with OUT_JSON.open(encoding="utf-8") as fh:
                anciens = (json.load(fh) or {}).get("societes") or {}
            for k, v in anciens.items():
                charge["societes"].setdefault(k, v)
            print("[ok] index fusionné : %d sociétés au total" % len(charge["societes"]))
        except Exception as e:
            print("[warn] fusion de l'index impossible : %s" % e, file=sys.stderr)
    with OUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(charge, f, ensure_ascii=False, indent=1)
    with OUT_JS.open("w", encoding="utf-8") as f:
        f.write("window.__INTL_FUNDA__ = " + json.dumps(charge, ensure_ascii=False,
                                                        separators=(",", ":")) + ";\n")
    horo = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Une tranche ne contient qu'un septième de l'univers : sans fusion, chaque
    # passage effacerait les six autres jours.
    # INCONDITIONNELLE. Un passage ne connaît jamais tout l'univers — ni la
    # tranche du jour, ni le sous-ensemble suivi — et le collecteur écrit un
    # fichier par initiale. Sans fusion, chaque passage efface tous les autres.
    paquets, repris, sauves = fusionner_paquets(paquets)
    if repris:
        print("[ok] fusion : %d sociétés reprises des passages précédents" % repris)
    if sauves:
        print("[ok] fusion : %d exercice(s) sauvé(s) de la fenêtre glissante "
              "au filet de fin de passage" % sauves)
    poids = []
    for lettre, contenu in sorted(paquets.items()):
        c = OUT_DIR / ("intl_detail_%s.json" % lettre)
        with c.open("w", encoding="utf-8") as f:
            json.dump({"genere_le": horo, "societes": contenu}, f,
                      ensure_ascii=False, separators=(",", ":"))
        poids.append(c.stat().st_size)

    print("[ok] %d sociétés — %d place inconnue, %d échecs, %d retrouvées par recherche — %.1f s"
          % (ok, sans_place, echecs, par_recherche, time.time() - t0))
    print("[ok] profondeur : +%d exercice(s) TradingView sur %d société(s), "
          "+%d exercice(s) repris de nos passages, %d capitalisation(s) publiée(s)"
          % (ajouts["tradingview"], ajouts["societes_tv"], ajouts["archive"],
             ajouts["mcap_publiee"]))
    if ajouts["refus_tv"]:
        # ⚠ ON NOMME LES REFUS. Un second fournisseur qui se tait ressemble trait
        # pour trait à un second fournisseur qu'on a mal appelé : sans ce
        # décompte par motif, le jour où le scanner changerait de préfixe de
        # place, la profondeur retomberait à cinq sans un mot.
        for motif, n in sorted(ajouts["refus_tv"].items(), key=lambda x: -x[1]):
            print("[info] TradingView refusé %5d fois — %s" % (n, motif))
    # Un second fournisseur muet EN ENTIER n'est pas une source avare, c'est une
    # panne. Il ne doit pas passer pour un choix.
    if AVEC_TV and tv_bilan.get("vises") and not tv_bilan.get("repondus"):
        print("[!] le scanner TradingView n'a rien servi sur %d société(s) visées : "
              "préfixes de place ou route changés ?" % tv_bilan["vises"],
              file=sys.stderr)
    if _bridages_tv[0]:
        print("[!] %d lot(s) TradingView abandonné(s) après %d essais — la "
              "profondeur du passage est incomplète" % (_bridages_tv[0], TV_RETRIES),
              file=sys.stderr)
    print("[ok] index %d Ko · %d paquets, plus gros %d Ko, total %d Ko"
          % (OUT_JSON.stat().st_size // 1024, len(poids),
             (max(poids) // 1024) if poids else 0, (sum(poids) // 1024) if poids else 0))

    # ── Une collecte bridée ne se fait pas passer pour une collecte finie ──
    # `_get` rendait None sur un 429 comme sur une page inexistante, et la
    # société était comptée « sans états déposés ». Un après-midi de bridage
    # aurait produit une collecte vide, en SUCCÈS, et réécrit l'index avec ce
    # qu'on n'avait pas pu récupérer.
    if _bridages[0]:
        part = 100.0 * _bridages[0] / max(1, len(univers))
        print("[!] %d requête(s) abandonnée(s) sur plafond de débit (%.1f %% de "
              "l'univers visé)" % (_bridages[0], part))
        if part > 5.0:
            raise SystemExit(
                "[fatal] la source a bridé plus de cinq pour cent des requêtes : "
                "la collecte est incomplète et ne doit pas passer pour finie. "
                "Relancer plus tard, ou augmenter DEBIT.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
