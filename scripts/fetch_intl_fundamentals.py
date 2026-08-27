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

CE QU'IL FAUT SAVOIR AVANT DE LIRE CES CHIFFRES, et qui est écrit dans la sortie
  · CINQ EXERCICES contre dix-neuf pour la SEC. L'asymétrie est réelle, la fiche
    doit l'assumer plutôt que la masquer.
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

from fondamentaux_communs import (          # noqa: E402
    _div, _pct, _r,
    note_quantitative,
    _mediane, _mediane_fenetre, _croissances, _predictibilite,
    _serie_sans_baisse_dividende, _serie_hausses_dividende,
    _corriger_divisions, _piotroski, _altman_z, _wacc,
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


def chercher_chemin(symbole, nom):
    """Repli : l'API de recherche du site rend le chemin canonique.

    Elle existe pour les cas où notre table de places se trompe ou ne connaît
    pas la place — ce qui est arrivé trois fois sur trente-trois marchés. Mieux
    vaut demander au site où il range un titre que de le deviner.
    """
    for terme in (symbole.split(".")[0], nom):
        if not terme:
            continue
        d = _get(BASE + "/api/search?q=" + urllib.parse.quote(terme))
        if not d:
            continue
        for item in (d.get("data") or d.get("results") or []):
            s = item.get("s") if isinstance(item, dict) else None
            if s and "/" in s:
                return "quote/" + s
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
    return {"contexte": ctx, "resultat": res, "bilan": bil, "flux": flx}


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
    "revenue":           ("resultat", ["revenue", "operatingRevenue"]),
    "cogs":              ("resultat", ["cor"]),
    "gross_profit":      ("resultat", ["gp"]),
    "rd":                ("resultat", ["rnd"]),
    "sga":               ("resultat", ["sgna"]),
    "opex":              ("resultat", ["opex"]),
    "operating_income":  ("resultat", ["opinc"]),
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
    "interest_expense":  ("resultat", ["interestExpense"]),
    "eps_diluted":       ("resultat", ["epsdil"]),
    "eps_basic":         ("resultat", ["epsBasic"]),
    "shares_diluted":    ("resultat", ["sharesDiluted"]),
    "shares_basic":      ("resultat", ["sharesBasic"]),
    "dps":               ("resultat", ["dps"]),
    "ebitda_publie":     ("resultat", ["ebitda"]),
    # bilan
    "assets":              ("bilan", ["assets"]),
    "assets_current":      ("bilan", ["assetsc"]),
    "liabilities":         ("bilan", ["liabilities"]),
    "liabilities_current": ("bilan", ["currentLiabilities"]),
    "equity":              ("bilan", ["totalCommonEquity", "equity"]),
    "cash":                ("bilan", ["cashneq"]),
    "short_term_inv":      ("bilan", ["investmentsc"]),
    "lt_debt":             ("bilan", ["debtnc"]),
    "current_debt":        ("bilan", ["debtc"]),
    "lease_lt":            ("bilan", ["capitalLeases"]),
    "lease_ct":            ("bilan", ["currentCapLeases"]),
    "goodwill":            ("bilan", ["goodwill"]),
    "retained_earnings":   ("bilan", ["retearn"]),
    "inventory":           ("bilan", ["inventory"]),
    # flux de trésorerie
    "ocf":            ("flux", ["ncfo"]),
    "capex":          ("flux", ["capex"]),
    "sbc":            ("flux", ["sbcomp"]),
    "dividends_paid": ("flux", ["commonDividendCF"]),
    "buybacks":       ("flux", ["commonRepurchased"]),
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


def construire(brut, mcap_usd=None, beta=None, cours=None, fx_dev=None, devise=None):
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
        e = {"fin": dates[i], "annee": int(dates[i][:4])}
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

    # ── Reconstructions et ratios, à l'identique du collecteur SEC ──
    for e in exercices:
        if e["gross_profit"] is None and e["revenue"] is not None and e["cogs"] is not None:
            e["gross_profit"] = e["revenue"] - e["cogs"]
        if e["pretax"] is None and e["net_income"] is not None and e["tax"] is not None:
            e["pretax"] = e["net_income"] + e["tax"]
        if e["operating_income"] is None:
            if e["gross_profit"] is not None and e["opex"] is not None:
                e["operating_income"] = e["gross_profit"] - e["opex"]
                e["_ope_source"] = "brut moins charges d’exploitation"
            elif e["gross_profit"] is not None and (e["rd"] is not None or e["sga"] is not None):
                e["operating_income"] = e["gross_profit"] - (e["rd"] or 0) - (e["sga"] or 0)
                e["_ope_source"] = "brut moins R&D et frais généraux"
        else:
            e["_ope_source"] = "publié (retraité par le fournisseur)"

        e["fcf"] = (e["ocf"] - e["capex"]) if (e["ocf"] is not None and e["capex"] is not None) else None
        e["ebitda"] = (e["operating_income"] + e["dna"]) \
            if (e["operating_income"] is not None and e["dna"] is not None) else e.get("ebitda_publie")

        e["tresorerie"] = e["cash"]
        liq = ((e["cash"] or 0) + (e["short_term_inv"] or 0)) if e["cash"] is not None else None
        dette = None
        if any(e.get(k) is not None for k in ("lt_debt", "current_debt", "lease_lt", "lease_ct")):
            dette = ((e["lt_debt"] or 0) + (e["current_debt"] or 0)
                     + (e["lease_lt"] or 0) + (e["lease_ct"] or 0))
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
        e["retour_actionnaire"] = ((e["dividends_paid"] or 0) + (e["buybacks"] or 0)) \
            if (e["dividends_paid"] is not None or e["buybacks"] is not None) else None

    # Le signe des charges d'intérêts : 351 valeurs négatives sur 352 de ce
    # côté-ci contre 1 sur 223 côté SEC. Sans ce redressement, la garde
    # `interest_expense > 0` de la couverture des intérêts vide ce ratio pour
    # 80,7 % de l'univers international, LVMH compris.
    for _e in exercices:
        _e["interest_expense"] = _charge(_e.get("interest_expense"))

    unites_actions = _corriger_unite_actions(exercices)

    # La source rétro-ajuste déjà les divisions d'action sur tous les exercices.
    # On lance quand même la recouture : elle ne trouvera rien (aucun saut), et
    # si un jour la source change de politique, elle rattrapera. Le résultat est
    # rendu, vide ou non, pour que la fiche puisse le dire.
    divisions = _corriger_divisions(exercices)

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
        e["roe"] = _pct(e["net_income"], cp) if (cp and cp > 0) else None
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
        if cours and e.get("shares_diluted"):
            px = _en_devise_etats(_cours_au(cours, e["fin"]), e["fin"], fx_dev, devise)
            if px:
                mc = px * e["shares_diluted"]
        if mc is None and i == len(exercices) - 1:
            mc = _en_devise_etats(mcap_usd, None, fx_dev, devise)
        e["mcap_estime"] = round(mc) if mc else None
        e["wacc"] = _wacc(mc, e.get("dette_totale"), e.get("interest_expense"),
                          e.get("_taux_nopat"), beta)
        e["roic_moins_wacc"] = (round(e["roic"] - e["wacc"], 2)
                                if (e.get("roic") is not None and e.get("wacc") is not None) else None)

    for i in range(1, len(exercices)):
        a, b = exercices[i - 1], exercices[i]
        dn = (b["nopat"] - a["nopat"]) if (a.get("nopat") is not None and b.get("nopat") is not None) else None
        ca_, cb = a.get("_capital_investi"), b.get("_capital_investi")
        dci = (cb - ca_) if (ca_ is not None and cb is not None) else None
        b["roiic"] = _pct(dn, dci) if (dci and abs(dci) > 0) else None
    if exercices:
        exercices[0]["roiic"] = None

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
    resume = {
        "n_exercices": len(exercices),
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
        "payout_benefices_10a": med("payout_benefices", 10),
        "payout_fcf": d.get("payout_fcf"),
        "piotroski": piotroski, "piotroski_detail": piotroski_detail,
        "altman_z": altman, "altman_detail": altman_detail,
        "verse_dividende": bool(d.get("dps") or d.get("dividends_paid")),
        "divisions_action": divisions,
        "unites_actions_corrigees": unites_actions,
    }
    resume["note_q"] = note_quantitative(resume)

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
            "dette_ebitda_brut": sd.get("dette_ebitda_brut"),
            "payout_benefices": sd.get("payout_benefices"),
            "verse_dividende": bool(sd.get("dps") or sd.get("dividends_paid")),
            "croissances": {"ca": _croissances(spa("ca_par_action")),
                            "fcf": _croissances(spa("fcf_par_action")),
                            "div": _croissances(spa("dps"))},
        })
        hist.append({"annee": sd["annee"], "note": n["note"],
                     "note_ramenee": n["note_ramenee"],
                     "criteres_notables": n["criteres_notables"]})
    resume["note_historique"] = hist
    return {"exercices": exercices, "resume": resume}


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
    for nom in ("fx_rates_cache.json", "tradfi_fx_cache.json"):
        f = CACHE_DIR / nom
        if not f.exists():
            continue
        try:
            with f.open(encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict) and d:
                return d
        except Exception:
            continue
    return {}


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
    rang modulo n vaut i. Le découpage se fait sur le RANG et non sur une
    empreinte, pour que chaque tranche contienne un échantillon de toutes les
    tailles — sinon la tranche du lundi ne verrait que des mégacapitalisations
    et celle du dimanche que des microcaps, et une panne un jour donné aurait
    des conséquences très différentes selon le jour.
    """
    f = CACHE_DIR / "univers_actions.json"
    if not f.exists():
        return {}
    with f.open(encoding="utf-8") as fh:
        u = json.load(fh)
    # Le chemin de la source, par symbole.
    chemins = {}
    for t in u.get("titres", []):
        sym = t.get("yahoo") or t.get("sa")
        if sym and t.get("principal"):
            chemins[sym] = t.get("sa")

    import glob as _glob
    lignes = []
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
        for sym, v in (d.get("societes") or {}).items():
            if sym not in chemins:
                continue
            lignes.append((v[i_capi] or 0, sym, v[i_nom]))
    lignes.sort(reverse=True)
    if plafond:
        lignes = lignes[:plafond]
    if tranche:
        i, n = tranche
        lignes = [x for k, x in enumerate(lignes) if k % n == i]
    out = {}
    for capi, sym, nom in lignes:
        out[sym] = {"nom": nom, "capi_usd": capi, "chemin_sa": chemins.get(sym)}
    return out


def fusionner_paquets(paquets):
    """Ajoute la tranche du jour aux paquets déjà écrits.

    Sans cette fusion, chaque passage effacerait les six autres tranches : le
    collecteur écrit un fichier par initiale, et une tranche n'en contient qu'un
    septième. On relit donc l'existant, on remplace les sociétés qu'on vient de
    collecter, on garde les autres.
    """
    import glob as _glob
    fusionnes, repris = {}, 0
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
        garde.update(contenu)
        fusionnes[lettre] = garde
    # Les paquets qu'aucune société de la tranche ne touche restent tels quels.
    return fusionnes, repris


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
         "tranche": None, "parallele": 1, "plafond": None}
    for i, a in enumerate(argv):
        if a == "--tickers" and i + 1 < len(argv):
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
            autre = chercher_chemin(sym, meta.get("nom"))
            if autre and autre != chemin:
                brut = etats(autre)
                if brut is not None:
                    chemin = autre
                    trouve_par_recherche = True
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
    global OUT_JSON, OUT_JS, OUT_DIR
    t0 = time.time()
    opts = _options(sys.argv[1:])
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

    # Le réseau d'abord, tout entier, en parallèle. La construction ensuite,
    # séquentielle : elle ne gagnerait rien à être concurrente et touche des
    # états partagés.
    precharges = precharger(univers, opts["parallele"])
    print("[info] descente finie en %.1f s" % (time.time() - t0))

    index, paquets = {}, {}
    ok = sans_place = echecs = 0
    par_recherche = 0
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
                              fx_dev=fx.get(devise_etats), devise=devise_etats)
        except Exception as e:
            print("[warn] %s : %s" % (sym, e), file=sys.stderr)
            echecs += 1
            continue
        if not bati:
            echecs += 1
            continue

        r = bati["resume"]
        r["devise"] = devise_etats
        r["cours_natif"], r["cours_natif_le"] = _dernier_cours(cours.get(sym))
        r["cours_natif"] = _en_devise_etats(r["cours_natif"], r["cours_natif_le"],
                                           fx.get(devise_etats), devise_etats)
        r["devise_cotation"] = devise_cot
        r["devises_alignees"] = brut["_devises_alignees"]
        r["frequence_publication"] = ctx.get("reportingFrequency")
        r["source_url"] = BASE + "/" + chemin + "/financials/"
        # Une tranche par jour veut dire qu'une donnée peut avoir six jours.
        # Ce n'est pas un défaut — un état financier change une fois par
        # trimestre — mais il faut que ça se VOIE, sinon une ligne vieille de
        # trois semaines se confond avec une fraîche.
        r["collecte_le"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r["source"] = "stockanalysis.com (données S&P Global Market Intelligence)"

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
                         "retrouves_par_recherche": par_recherche},
        "limites": [
            "Cinq exercices par société — la source n'en sert pas davantage à un visiteur anonyme.",
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
    paquets, repris = fusionner_paquets(paquets)
    if repris:
        print("[ok] fusion : %d sociétés reprises des passages précédents" % repris)
    poids = []
    for lettre, contenu in sorted(paquets.items()):
        c = OUT_DIR / ("intl_detail_%s.json" % lettre)
        with c.open("w", encoding="utf-8") as f:
            json.dump({"genere_le": horo, "societes": contenu}, f,
                      ensure_ascii=False, separators=(",", ":"))
        poids.append(c.stat().st_size)

    print("[ok] %d sociétés — %d place inconnue, %d échecs, %d retrouvées par recherche — %.1f s"
          % (ok, sans_place, echecs, par_recherche, time.time() - t0))
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
