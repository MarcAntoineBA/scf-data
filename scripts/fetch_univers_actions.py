#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L'univers des actions cotées, collecté au lieu d'être écrit à la main.

POURQUOI
Le site suivait 800 titres — une liste tenue à la main dans `fetch_tradfi.py`.
Le concurrent en annonce cinquante mille. Tant que l'univers est une liste
écrite par un humain, l'écart ne se comble pas : il faudrait taper cinquante
mille lignes, et les tenir à jour.

Or stockanalysis publie une page de liste par place de cotation, et cette page
est une application SvelteKit : sa charge de données est lisible à l'adresse
`/list/<place>/__data.json`. Une requête rend **cinq cents titres** avec leur
symbole, leur nom, leur capitalisation, leur cours DANS LA DEVISE DE LA PLACE et
leur chiffre d'affaires. Une seconde requête, `?page=2`, rend les suivants.

Quatre-vingts places, quelques pages chacune : l'univers entier se collecte en
quelques minutes, gratuitement, sans clé. Il devient une donnée datée, pas une
opinion figée dans du code.

CE QUE CE FICHIER N'EST PAS
Ce n'est pas une collecte de fondamentaux. Il ne descend dans aucune fiche, ne
lit aucun état financier. Il répond à une seule question — « quels titres
existent, et lesquels pèsent quelque chose ? » — pour que la recherche du site
trouve n'importe quelle société, et pour que les collectes profondes sachent
par où commencer.

LES DOUBLES COTATIONS
NVIDIA apparaît à Londres sous le code 0R1I, à Francfort, à Mexico. Ce sont des
reflets, pas des sociétés. On les garde — un lecteur peut chercher le code
qu'il a sous les yeux — mais on en désigne UNE comme cotation principale, et
c'est celle-là qui compte dans les totaux. La règle est un ordre de priorité
entre places, écrit noir sur blanc plus bas, et non un classement par
capitalisation : les capitalisations sont dans des devises différentes, les
comparer serait comparer des taux de change.

SORTIES
  · univers_actions.json — la liste complète, avec sa date et son compte rendu
  · univers_<CLÉ>.json   — les fragments de recherche, un par première lettre

Pas de jumeau `.js` : la convention du dépôt veut qu'un cache existe aussi en
`window.__X__ = …` pour être chargé par la page. Elle suppose que la page charge
le cache entier. Ici elle ne le fait jamais — elle prend un fragment — et le
jumeau ferait vingt-deux mégaoctets déposés chaque jour pour rien.
  · univers_actions_light.json — les seuls champs dont la recherche a besoin
"""
import signal as _signal
import sys as _sys


def _delai(signum, frame):
    print("[fatal] délai global (20 min) atteint — abandon.", file=_sys.stderr)
    _sys.exit(2)


try:
    _signal.signal(_signal.SIGALRM, _delai)
    _signal.alarm(20 * 60)
except Exception:
    pass

import gzip
import json
import os
import sys
import re
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR = CACHE_DIR

BASE = "https://stockanalysis.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
DEBIT = 0.35            # même politesse que la collecte de fondamentaux
TIMEOUT = 30
RETRIES = 3
PAGES_MAX = 8           # 4 000 titres par place : au-delà, c'est de la poussière
_last = [0.0]


def _get(url):
    for essai in range(RETRIES):
        d = time.time() - _last[0]
        if d < DEBIT:
            time.sleep(DEBIT - d)
        _last[0] = time.time()
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept-Encoding": "gzip",
            "Accept": "application/json,text/plain,*/*",
            "Referer": BASE + "/",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code in (404, 403, 410):
                return None
            if essai == RETRIES - 1:
                return None
            time.sleep(1.2 * (essai + 1))
        except Exception:
            if essai == RETRIES - 1:
                return None
            time.sleep(1.2 * (essai + 1))
    return None


def _resoudre(arr, idx, prof=0):
    """Le format « devalue » de SvelteKit : chaque valeur est un INDICE."""
    if prof > 40:
        return None
    v = arr[idx] if isinstance(idx, int) and 0 <= idx < len(arr) else idx
    if isinstance(v, dict):
        return {k: _resoudre(arr, j, prof + 1) for k, j in v.items()}
    if isinstance(v, list):
        return [_resoudre(arr, j, prof + 1) for j in v]
    return v


def _tableau(charge):
    """Le tableau des titres, où qu'il soit dans la charge décodée."""
    tab = None
    for n in (charge or {}).get("nodes", []):
        if isinstance(n, dict) and n.get("type") == "data" and isinstance(n.get("data"), list):
            tab = n["data"]          # le DERNIER nœud de données est celui de la page
    if tab is None:
        return None
    val = _resoudre(tab, 0)

    def chercher(o, prof=0):
        if isinstance(o, list) and o and isinstance(o[0], dict) and "s" in o[0]:
            return o
        if isinstance(o, dict):
            for v in o.values():
                r = chercher(v, prof + 1)
                if r is not None:
                    return r
        if isinstance(o, list) and prof < 4:
            for v in o[:8]:
                r = chercher(v, prof + 1)
                if r is not None:
                    return r
        return None

    return chercher(val)


# ─────────────────────────────────────────────────────────────────────────
# Les places
# ─────────────────────────────────────────────────────────────────────────
# Relevé sur `/list/` le 2026-08-27 : cent quatre-vingt-huit pages de liste, dont
# la plupart sont thématiques (« dividend-kings », « ai-stocks », des ETF, des
# fonds). On ne garde que les VRAIES places de cotation, et on inscrit à côté de
# chacune sa devise et son suffixe Yahoo — les deux informations que la page ne
# donne pas et sans lesquelles le titre n'est raccordable à rien.
#
# `suffixe` vide = cotation américaine, sans suffixe (le site nomme AAPL, pas
# AAPL.US). `None` = aucun suffixe Yahoo connu ; le titre est collecté et
# cherchable, mais aucune autre collecte ne saura le retrouver.
PLACES = [
    # id de liste,                    code SA,   devise, suffixe Yahoo, libellé
    ("tokyo-stock-exchange",          "tyo",     "JPY",  "T",    "Tokyo"),
    ("hong-kong-stock-exchange",      "hkg",     "HKD",  "HK",   "Hong Kong"),
    ("nasdaq-stocks",                 "",        "USD",  "",     "NASDAQ"),
    ("nyse-stocks",                   "",        "USD",  "",     "NYSE"),
    ("nyseamerican-stocks",           "",        "USD",  "",     "NYSE American"),
    ("shanghai-stock-exchange",       "sha",     "CNY",  "SS",   "Shanghai"),
    ("shenzhen-stock-exchange",       "she",     "CNY",  "SZ",   "Shenzhen"),
    ("london-stock-exchange",         "lon",     "GBP",  "L",    "Londres"),
    ("euronext-paris",                "epa",     "EUR",  "PA",   "Euronext Paris"),
    ("euronext-amsterdam",            "ams",     "EUR",  "AS",   "Euronext Amsterdam"),
    ("euronext-brussels",             "ebr",     "EUR",  "BR",   "Euronext Bruxelles"),
    ("euronext-lisbon",               "eli",     "EUR",  "LS",   "Euronext Lisbonne"),
    ("euronext-dublin",               "ise",     "EUR",  "IR",   "Euronext Dublin"),
    ("deutsche-boerse-xetra",         "etr",     "EUR",  "DE",   "XETRA"),
    ("six-swiss-exchange",            "swx",     "CHF",  "SW",   "SIX Suisse"),
    ("borsa-italiana",                "bit",     "EUR",  "MI",   "Borsa Italiana"),
    ("madrid-stock-exchange",         "bme",     "EUR",  "MC",   "Madrid"),
    ("vienna-stock-exchange",         "vie",     "EUR",  "VI",   "Vienne"),
    ("nasdaq-stockholm",              "sto",     "SEK",  "ST",   "Stockholm"),
    ("copenhagen-stock-exchange",     "cph",     "DKK",  "CO",   "Copenhague"),
    ("oslo-bors",                     "osl",     "NOK",  "OL",   "Oslo"),
    ("nasdaq-helsinki",               "hel",     "EUR",  "HE",   "Helsinki"),
    ("nasdaq-iceland",                "ice",     "ISK",  "IC",   "Reykjavik"),
    ("toronto-stock-exchange",        "tsx",     "CAD",  "TO",   "Toronto"),
    ("tsx-venture-exchange",          "tsxv",    "CAD",  "V",    "TSX Venture"),
    ("australian-securities-exchange", "asx",    "AUD",  "AX",   "Australie"),
    ("new-zealand-stock-exchange",    "nzx",     "NZD",  "NZ",   "Nouvelle-Zélande"),
    ("korea-stock-exchange",          "krx",     "KRW",  "KS",   "Séoul KOSPI"),
    ("kosdaq-korea",                  "krx",     "KRW",  "KQ",   "KOSDAQ"),
    ("taiwan-stock-exchange",         "tpe",     "TWD",  "TW",   "Taïwan"),
    ("nse-india",                     "nse",     "INR",  "NS",   "NSE Inde"),
    ("bse-india",                     "bom",     "INR",  "BO",   "BSE Inde"),
    ("singapore-exchange",            "sgx",     "SGD",  "SI",   "Singapour"),
    ("bursa-malaysia",                "klse",    "MYR",  "KL",   "Malaisie"),
    ("stock-exchange-of-thailand",    "bkk",     "THB",  "BK",   "Thaïlande"),
    ("indonesia-stock-exchange",      "idx",     "IDR",  "JK",   "Indonésie"),
    ("philippine-stock-exchange",     "pse",     "PHP",  "PS",   "Philippines"),
    ("ho-chi-minh-stock-exchange",    "hose",    "VND",  None,   "Hô-Chi-Minh"),
    ("saudi-stock-exchange",          "tadawul", "SAR",  "SR",   "Tadawul"),
    ("abu-dhabi-securities-exchange", "adx",     "AED",  "AE",   "Abu Dhabi"),
    ("dubai-financial-market",        "dfm",     "AED",  "AE",   "Dubaï"),
    ("qatar-stock-exchange",          "qse",     "QAR",  "QA",   "Qatar"),
    ("tel-aviv-stock-exchange",       "tase",    "ILS",  "TA",   "Tel-Aviv"),
    ("borsa-istanbul",                "bist",    "TRY",  "IS",   "Istanbul"),
    ("johannesburg-stock-exchange",   "jse",     "ZAR",  "JO",   "Johannesburg"),
    ("brazil-stock-exchange",         "bvmf",    "BRL",  "SA",   "B3 Brésil"),
    ("mexican-stock-exchange",        "bmv",     "MXN",  "MX",   "Mexique"),
    ("santiago-stock-exchange",       "bcs",     "CLP",  "SN",   "Santiago"),
    ("buenos-aires-stock-exchange",   "bcba",    "ARS",  "BA",   "Buenos Aires"),
    ("colombia-stock-exchange",       "bvc",     "COP",  "CL",   "Colombie"),
    ("lima-stock-exchange",           "bvl",     "PEN",  None,   "Lima"),
    ("warsaw-stock-exchange",         "wse",     "PLN",  "WA",   "Varsovie"),
    ("prague-stock-exchange",         "pse-cz",  "CZK",  "PR",   "Prague"),
    ("budapest-stock-exchange",       "bse-hu",  "HUF",  "BD",   "Budapest"),
    ("athens-stock-exchange",         "athex",   "EUR",  "AT",   "Athènes"),
    ("egyptian-stock-exchange",       "egx",     "EGP",  "CA",   "Le Caire"),
    ("nigerian-stock-exchange",       "ngx",     "NGN",  None,   "Nigeria"),
    ("pakistan-stock-exchange",       "psx",     "PKR",  "KA",   "Pakistan"),
    ("colombo-stock-exchange",        "cse-lk",  "LKR",  None,   "Colombo"),
    ("dhaka-stock-exchange",          "dse",     "BDT",  None,   "Dhaka"),
    ("kuwait-stock-exchange",         "kse",     "KWD",  "KW",   "Koweït"),
    ("muscat-securities-market",      "msx",     "OMR",  None,   "Mascate"),
    ("bahrain-stock-exchange",        "bhb",     "BHD",  None,   "Bahreïn"),
    ("amman-stock-exchange",          "ase-jo",  "JOD",  None,   "Amman"),
    ("casablanca-stock-exchange",     "cse-ma",  "MAD",  None,   "Casablanca"),
    ("tunis-stock-exchange",          "bvmt",    "TND",  None,   "Tunis"),
    ("nairobi-stock-exchange",        "nse-ke",  "KES",  None,   "Nairobi"),
    ("luxembourg-stock-exchange",     "lux",     "EUR",  None,   "Luxembourg"),
    ("malta-stock-exchange",          "mse-mt",  "EUR",  None,   "Malte"),
    ("cyprus-stock-exchange",         "cse-cy",  "EUR",  None,   "Chypre"),
    ("zagreb-stock-exchange",         "zse",     "EUR",  None,   "Zagreb"),
    ("ljubljana-stock-exchange",      "ljse",    "EUR",  None,   "Ljubljana"),
    ("bucharest-stock-exchange",      "bvb",     "RON",  None,   "Bucarest"),
    ("bulgarian-stock-exchange",      "bse-bg",  "BGN",  None,   "Sofia"),
    ("belgrade-stock-exchange",       "belex",   "RSD",  None,   "Belgrade"),
    ("nasdaq-riga",                   "rig",     "EUR",  "RG",   "Riga"),
    ("nasdaq-tallinn",                "tal",     "EUR",  "TL",   "Tallinn"),
    ("nasdaq-vilnius",                "vln",     "EUR",  "VS",   "Vilnius"),
    # Places de RÉFLEXION : on y trouve surtout des doubles cotations. Elles sont
    # en fin de liste, ce qui suffit à en faire des cotations secondaires (voir
    # la règle de priorité ci-dessous).
    ("london-stock-exchange-aim",     "lon",     "GBP",  "L",    "Londres AIM"),
    ("aquis-exchange",                "aqse",    "GBP",  "AQ",   "Aquis"),
    ("frankfurt-stock-exchange",      "fra",     "EUR",  "F",    "Francfort"),
    ("stuttgart-stock-exchange",      "stu",     "EUR",  "SG",   "Stuttgart"),
    ("munich-stock-exchange",         "mun",     "EUR",  "MU",   "Munich"),
    ("dusseldorf-stock-exchange",     "dus",     "EUR",  "DU",   "Düsseldorf"),
    ("hamburg-stock-exchange",        "ham",     "EUR",  "HM",   "Hambourg"),
    ("nagoya-stock-exchange",         "nse-jp",  "JPY",  "NG",   "Nagoya"),
    ("fukuoka-stock-exchange",        "fse-jp",  "JPY",  None,   "Fukuoka"),
    ("sapporo-stock-exchange",        "sse-jp",  "JPY",  None,   "Sapporo"),
    ("taipei-exchange",               "tpex",    "TWD",  "TWO",  "Taipei OTC"),
    ("canadian-securities-exchange",  "cse",     "CAD",  "CN",   "CSE Canada"),
    ("cboe-canada",                   "neo",     "CAD",  "NE",   "Cboe Canada"),
    ("nordic-growth-market",          "ngm",     "SEK",  "NG",   "NGM"),
    ("spotlight-stock-market",        "spot",    "SEK",  None,   "Spotlight"),
    ("moscow-stock-exchange",         "moex",    "RUB",  "ME",   "Moscou"),
    ("kazakhstan-stock-exchange",     "kase",    "KZT",  None,   "Kazakhstan"),
    ("otc-stocks",                    "otc",     "USD",  "",     "OTC américain"),
]

# Les places qui n'hébergent pratiquement QUE des reflets. Relevé le
# 2026-08-27 sur les premières pages de chacune : Buenos Aires ouvre sur NVIDIA,
# Apple et Alphabet (des CEDEAR), Francfort sur NVIDIA deux fois, l'OTC
# américain sur Samsung et Tencent, Vienne sur NVIDIA et Apple. Aucune de ces
# quatre listes ne commence par une société du pays.
REFLETS = {
    "frankfurt-stock-exchange", "stuttgart-stock-exchange", "munich-stock-exchange",
    "dusseldorf-stock-exchange", "hamburg-stock-exchange", "otc-stocks",
    "buenos-aires-stock-exchange", "vienna-stock-exchange", "aquis-exchange",
    "cboe-canada", "nordic-growth-market", "spotlight-stock-market",
    # Le Luxembourg ne cote presque que des certificats globaux : SK Hynix y
    # figure sous HYNSE, Samsung sous SMSDI.
    "luxembourg-stock-exchange",
}

# Deux capitalisations qui se tiennent à moins de ce seuil sont réputées égales.
# Mesuré : une même société vue à New York et à Zurich diffère de 2 à 3 %, par
# décalage entre la date du taux de change et l'instant du calcul. Départager
# sur cet écart-là revient à tirer à pile ou face.
TOLERANCE_CAPI = 0.10


# Les formes de codes qui trahissent un certificat plutôt qu'une action.
# Chacune suit une convention de place, pas une intuition :
#   · Londres, compartiment international : un zéro et trois caractères (0R1I).
#   · Brésil, B3 : un BDR finit par 34 ou 35 ; une action ordinaire finit par
#     3, 4 ou 11.
#   · Thaïlande : un DR accole deux chiffres au nom de la société d'origine
#     (TENCENT06, MIDEA80) ; une action thaïlandaise n'a pas de chiffres.
#   · Mexique, marché international SIC : le code de l'action étrangère suivi
#     d'un N ou d'un astérisque.
_IOB_LONDRES = re.compile(r"^0[A-Z0-9]{3}$")
_BDR_BRESIL = re.compile(r"^[A-Z0-9]{4}3[45]$")
_DR_THAI = re.compile(r"^[A-Z]{3,}\d{2}$")
_SIC_MEXIQUE = re.compile(r"^[A-Z0-9]{2,}[N*]$")


def _est_reflet(t):
    """Une cotation qui reflète une société cotée ailleurs."""
    if t["place_id"] in REFLETS:
        return True
    c = (t["ticker"] or "").upper()
    p = t["place_id"]
    if p.startswith("london-stock-exchange") and _IOB_LONDRES.match(c):
        return True
    if p == "brazil-stock-exchange" and _BDR_BRESIL.match(c):
        return True
    if p == "stock-exchange-of-thailand" and _DR_THAI.match(c):
        return True
    if p == "mexican-stock-exchange" and _SIC_MEXIQUE.match(c):
        return True
    return False


def charger_verifies():
    """Les symboles dont on collecte DÉJÀ les états financiers.

    Sept cent cinquante sociétés dont la cotation d'origine est établie : ce
    sont elles qui servent à la fois d'autorité — leur cotation gagne d'office —
    et de contrôle, puisqu'on mesure sur elles le taux d'erreur de la règle.
    """
    out = set()
    for nom in ("sec_fundamentals_index.json", "intl_fundamentals_index.json"):
        f = CACHE_DIR / nom
        if not f.exists():
            continue
        try:
            with f.open(encoding="utf-8") as fh:
                d = json.load(fh)
            out |= set((d.get("societes") or {}).keys())
        except Exception:
            pass
    return out


def charger_taux():
    """{DEVISE: valeur d'une unité en dollars}, au dernier jour connu.

    Le cache est déjà là, alimenté par la collecte des cours. Vingt-trois
    devises : les autres n'auront pas de capitalisation en dollars, et c'est
    préférable à un chiffre inventé.
    """
    for nom in ("fx_rates_cache.json", "tradfi_fx_cache.json"):
        f = CACHE_DIR / nom
        if not f.exists():
            continue
        try:
            with f.open(encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        out = {"USD": 1.0}
        for dev, par_jour in d.items():
            if isinstance(par_jour, dict) and par_jour:
                out[dev] = par_jour[max(par_jour)]
        return out
    return {"USD": 1.0}


# L'ordre de PLACES est la règle de priorité. Le premier trouvé pour un même nom
# de société est la cotation PRINCIPALE ; les autres sont des reflets. Les
# grandes places de domiciliation sont donc en tête, les places de réflexion
# (Londres AIM, les bourses régionales allemandes, l'OTC américain) en queue.


def _norme(nom):
    """Un nom réduit à ce qui l'identifie, pour rapprocher deux cotations.

    On retire les accents, la ponctuation et les formes juridiques : « NVIDIA
    Corporation » et « NVIDIA Corp » sont la même société ; « Société Européenne »
    à la fin de LVMH n'en distingue aucune.
    """
    s = unicodedata.normalize("NFKD", (nom or "")).encode("ascii", "ignore").decode()
    s = s.lower()
    for mot in (" corporation", " incorporated", " company", " limited", " holdings",
                " holding", " group", " plc", " inc", " corp", " ltd", " llc", " nv",
                " sa", " se", " ag", " spa", " s.p.a", " s.a", " n.v", " a/s", " asa",
                " oyj", " ab", " as", " co", " kgaa", " gmbh", " societe europeenne",
                " public limited", " sarl", " bhd", " tbk", " pjsc", " qsc", " psc"):
        if s.endswith(mot):
            s = s[: -len(mot)]
    return "".join(c for c in s if c.isalnum())


def _cle_fragment(mot):
    """Le fragment où ranger un texte, d'après son premier caractère.

    Une lettre suffit pour un mot. Elle ne suffit PAS pour les codes des places
    asiatiques, tous numériques : Tokyo, Hong Kong, Shanghai, Shenzhen et Séoul
    réunis mettaient dix-huit mille titres dans un seul fragment de 1,6 Mo, que
    le visiteur aurait téléchargé pour en lire un. On prend alors deux
    caractères, ce qui suit d'ailleurs la logique des places : 00xx et 07xx à
    Hong Kong, 72xx à Tokyo, 60xx à Shanghai.
    """
    s = unicodedata.normalize("NFKD", (mot or "?")).encode("ascii", "ignore").decode().upper()
    s = s.lstrip(" .-'\"")
    if not s:
        return None
    c = s[0]
    if "A" <= c <= "Z":
        return c
    if c.isdigit():
        return s[:2] if (len(s) >= 2 and s[1].isdigit()) else "0"
    return None


def _cles_de_recherche(symbole, nom):
    """Les fragments où un titre doit apparaître pour être trouvable.

    Deux : celui de son nom, celui de son symbole. On tape « Toyota » aussi
    souvent que « 7203 », et l'un ne mène pas à l'autre.
    """
    cles = set()
    for m in (nom, symbole):
        c = _cle_fragment(m)
        if c:
            cles.add(c)
    return cles or {"0"}


def collecter_place(liste, code, devise, suffixe, libelle):
    """Toutes les pages d'une place, jusqu'à ce qu'elle se tarisse."""
    titres, page = [], 1
    while page <= PAGES_MAX:
        url = BASE + "/list/" + liste + "/__data.json" + ("" if page == 1 else "?page=%d" % page)
        charge = _get(url)
        lignes = _tableau(charge) if charge else None
        if not lignes:
            break
        for L in lignes:
            s = (L.get("s") or "").strip()
            if not s:
                continue
            # Une page de place peut contenir des fonds : on ne garde que les actions.
            st = (L.get("subtype") or "stock").lower()
            if st not in ("stock", "", "adr", "reit"):
                continue
            ticker = s.split("/")[-1]
            titres.append({
                "sa": s if "/" in s else s,           # le chemin chez la source
                "ticker": ticker,
                "yahoo": (ticker + "." + suffixe) if suffixe else (ticker if suffixe == "" else None),
                "nom": L.get("n"),
                "place": libelle,
                "place_id": liste,
                "devise": devise,
                "capi": L.get("marketCap"),
                "cours": L.get("price"),
                "variation": L.get("change"),
                "ca": L.get("revenue"),
            })
        if len(lignes) < 500:
            break                                     # dernière page
        page += 1
    return titres


def main():
    t0 = time.time()
    tout, par_place, echecs = [], {}, []
    for i, (liste, code, devise, suffixe, libelle) in enumerate(PLACES, 1):
        try:
            t = collecter_place(liste, code, devise, suffixe, libelle)
        except Exception as e:
            print("[warn] %s : %s" % (liste, e), file=sys.stderr)
            t = []
        if not t:
            echecs.append(liste)
        par_place[libelle] = len(t)
        tout.extend(t)
        if i % 15 == 0:
            print("[info] %d/%d places — %d titres" % (i, len(PLACES), len(tout)))

    # ── Capitalisation en dollars, seule grandeur comparable entre places ──
    taux = charger_taux()
    sans_taux = set()
    for t in tout:
        d = t.get("devise")
        r = taux.get(d)
        if r is None:
            sans_taux.add(d)
        t["capi_usd"] = round(t["capi"] * r) if (t.get("capi") and r) else None
        t["reflet"] = _est_reflet(t)

    # ── Qui est la vraie cotation ? ──
    # Ordre de départage, du plus fiable au moins fiable :
    #   1. une cotation qui n'est pas sur une place de reflet passe devant ;
    #   2. entre deux candidates, la plus grosse capitalisation en dollars —
    #      c'est celle qui porte le flottant réel ;
    #   3. à défaut de taux de change, l'ordre de la table PLACES, explicite.
    rang = {}
    for i, (liste, _c, _d, _s, _l) in enumerate(PLACES):
        rang.setdefault(liste, i)

    groupes = {}
    for t in tout:
        cle = _norme(t["nom"])
        t["_cle"] = cle
        (groupes.setdefault(cle, []) if cle else groupes.setdefault(id(t), [])).append(t)

    verifies = charger_verifies()
    for t in tout:
        t["verifie"] = (t.get("yahoo") in verifies) if t.get("yahoo") else False

    def elire(membres, avec_autorite=True):
        """La cotation d'origine d'un groupe de cotations d'une même société.

        L'ordre des critères est celui de leur fiabilité décroissante : ce qu'on
        a vérifié, puis ce qu'on reconnaît comme reflet, puis la taille — mais
        arrondie au seuil de tolérance, pour ne pas trancher sur du bruit — puis
        l'ordre explicite des places.
        """
        if avec_autorite:
            verifs = [t for t in membres if t["verifie"]]
            if verifs:
                membres = verifs
        vrais = [t for t in membres if not t["reflet"]]
        if vrais:
            membres = vrais
        plafond = max((t["capi_usd"] or 0) for t in membres)
        if plafond > 0:
            gros = [t for t in membres if (t["capi_usd"] or 0) >= plafond * (1 - TOLERANCE_CAPI)]
            if gros:
                membres = gros
        return min(membres, key=lambda t: (rang.get(t["place_id"], 999),
                                           -(t["capi_usd"] or 0)))

    principaux = 0
    for cle, membres in groupes.items():
        gagnant = elire(membres)
        for t in membres:
            t["principal"] = (t is gagnant)
            if t is not gagnant:
                t["principal_de"] = gagnant.get("yahoo") or gagnant["sa"]
        principaux += 1
    for t in tout:
        t.pop("_cle", None)

    # ── Le taux d'erreur de la règle, mesuré et publié ──
    # On rejoue la désignation SANS l'autorité des symboles vérifiés, et on
    # compte combien de fois elle retombe sur la bonne cotation. Ce chiffre dit
    # ce que vaut la règle pour les quarante-six mille sociétés sur lesquelles
    # on n'a rien à vérifier.
    justes = faux = 0
    for cle, membres in groupes.items():
        if not any(t["verifie"] for t in membres):
            continue
        aveugle = elire(membres, avec_autorite=False)
        if aveugle["verifie"]:
            justes += 1
        else:
            faux += 1
    exactitude = round(100.0 * justes / (justes + faux), 1) if (justes + faux) else None

    horo = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    avec_capi = sum(1 for t in tout if t.get("capi"))
    charge = {
        "updated": horo,
        "source": "stockanalysis.com — pages de liste par place de cotation",
        "duree_s": round(time.time() - t0, 1),
        "methode": [
            "Une requête par page de liste, cinq cents titres par page, jusqu'à huit pages.",
            "Le cours et la capitalisation sont dans la devise de la PLACE, non convertis.",
            "Une société cotée sur plusieurs places n'a qu'une cotation principale : "
            "celle qui n'est pas sur une place de reflet et qui porte la plus grosse "
            "capitalisation RAMENÉE EN DOLLARS. Un reflet est calculé à partir d'un "
            "cours converti ; la cotation d'origine porte le flottant réel.",
            "Quand la devise manque au cache de taux, le départage retombe sur "
            "l'ordre explicite de la table des places.",
        ],
        "exhaustivite": {
            "places_interrogees": len(PLACES),
            "places_muettes": echecs,
            "titres": len(tout),
            "cotations_principales": principaux,
            "avec_capitalisation": avec_capi,
            "avec_capitalisation_en_dollars": sum(1 for t in tout if t.get("capi_usd")),
            "devises_sans_taux": sorted(x for x in sans_taux if x),
            "symboles_verifies": len(verifies),
            "exactitude_de_la_regle_pct": exactitude,
            "controle": {"justes": justes, "faux": faux},
        },
        "par_place": dict(sorted(par_place.items(), key=lambda kv: -kv[1])),
        "titres": tout,
    }

    (OUT_DIR / "univers_actions.json").write_text(
        json.dumps(charge, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # La version légère est celle que la page charge : le strict nécessaire pour
    # trouver un titre et l'afficher en une ligne.
    # Trois mégaoctets d'index ne se chargent pas dans une page ; on les
    # DÉCOUPE par première lettre du symbole. La recherche ne va chercher que le
    # fragment qui correspond à ce que le visiteur tape, soit une centaine de
    # kilo-octets, une seule fois.
    leger = [[t.get("yahoo") or t["sa"], t["nom"], t["place"], t["devise"],
              t.get("capi"), t.get("cours"), t.get("capi_usd")]
             for t in tout if t.get("principal")]
    leger.sort(key=lambda r: -(r[6] or 0))
    fragments = {}
    for r in leger:
        for cle in _cles_de_recherche(r[0], r[1]):
            fragments.setdefault(cle, []).append(r)
    champs = ["symbole", "nom", "place", "devise", "capitalisation", "cours",
              "capitalisation_usd"]
    poids = []
    for c, rows in sorted(fragments.items()):
        f = OUT_DIR / ("univers_%s.json" % c)
        f.write_text(json.dumps({"updated": horo, "champs": champs, "titres": rows},
                                ensure_ascii=False, separators=(",", ":")),
                     encoding="utf-8")
        poids.append((c, len(rows), f.stat().st_size))
    (OUT_DIR / "univers_actions_light.json").write_text(
        json.dumps({"updated": horo, "champs": champs,
                    "total": len(leger),
                    "fragments": {c: n for c, n, _ in poids},
                    "titres": leger[:2000]}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    print("[ok] %d fragments de recherche — plus gros %d Ko, total %d Ko"
          % (len(poids), max(p[2] for p in poids) // 1024,
             sum(p[2] for p in poids) // 1024))

    print("[ok] %d titres sur %d places — %d cotations principales — %.1f s"
          % (len(tout), len(PLACES) - len(echecs), principaux, time.time() - t0))
    if echecs:
        print("[ok] places muettes : %s" % ", ".join(echecs))
    for f in ("univers_actions.json", "univers_actions_light.json"):
        print("[ok] %s : %d Ko" % (f, (OUT_DIR / f).stat().st_size // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
