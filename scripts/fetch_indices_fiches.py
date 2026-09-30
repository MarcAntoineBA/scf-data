#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_indices_fiches.py — la composition et le fondamental des dix-sept indices
de l'onglet « Indices » (Analyse fondamentale).

CE QUE ÇA PRODUIT
  · indices_fiches.json / indices_fiches.js (window.__INDICES_FICHES__)
    Une synthèse par indice : valorisation agrégée, concentration, ampleur,
    performance, secteurs. Légère : c'est elle qui sert les comparaisons ENTRE
    indices (« le plus concentré des dix-sept »).
  · indice_<code>.json
    La fiche complète d'UN indice, chargée à l'ouverture : ses membres un par un,
    les contributions à la performance, l'historique de l'indice lui-même et
    l'historique de ses propres mesures, accumulé passage après passage.

D'OÙ VIENT LA COMPOSITION — une source par indice, la meilleure gratuite
  · S&P 500      avoirs du SPY (State Street) : poids EXACTS, quotidiens.
  · Nikkei 225   fichier de poids publié par Nikkei (mensuel). L'indice est
                 pondéré par les PRIX : aucune capitalisation ne redonnerait
                 ses poids — Fast Retailing y pèse plus que Toyota.
  · Ibovespa     portefeuille théorique de la B3 : poids officiels du jour.
  · KOSPI, TAIEX TOUTES les actions ordinaires de la place — c'est la définition
                 de ces deux indices — pondérées par la capitalisation.
  · les douze autres
                 la liste des membres sur Wikipédia, pondérée par la
                 capitalisation FLOTTANTE et plafonnée comme l'indice l'est.
  Les grandeurs de chaque membre viennent du point d'entrée du screener de
  stockanalysis, le même que fetch_marche_actions.py, plus cinq champs que la
  collecte de marché ne demande pas : flottant, record historique, écart au
  record, date du record, écart au plus haut sur un an.

⚠ POURQUOI LE FLOTTANT ET NON LA CAPITALISATION
  Hermès est détenue aux deux tiers par la famille : sa capitalisation totale la
  mettrait à ~9 % du CAC 40, son poids réel est de ~4 %. Les indices modernes
  ne comptent que les actions qui peuvent s'échanger. Le poids calculé ici en
  est une ESTIMATION, et la sortie publie son écart aux poids exacts là où on les
  connaît (S&P 500) : c'est la seule preuve que l'estimation vaut quelque chose.

⚠ UN P/E D'INDICE N'EST PAS LA MOYENNE DES P/E
  C'est la capitalisation totale divisée par les bénéfices totaux, soit l'inverse
  de la moyenne PONDÉRÉE des rendements bénéficiaires (bénéfice / cours). Une
  société en perte y entre avec un rendement négatif : l'écarter (ce que fait une
  moyenne de P/E, qui n'existe pas pour elle) rendrait l'indice moins cher qu'il
  n'est. La médiane des P/E est publiée À CÔTÉ, sous son nom.

⚠ LES CONTRIBUTIONS SE CALCULENT SUR LE POIDS DE DÉPART
  Un titre qui a doublé pèse aujourd'hui deux fois plus qu'au début de la
  période. Sa contribution est poids_de_départ × variation, avec
  poids_de_départ ∝ poids_actuel / (1 + variation). Sans ce retour en arrière,
  les gagnants sont comptés deux fois.
"""
import signal as _signal
import sys as _sys


def _delai(signum, frame):
    print("[fatal] délai global (15 min) atteint — abandon.", file=_sys.stderr)
    _sys.exit(2)


try:
    _signal.signal(_signal.SIGALRM, _delai)
    _signal.alarm(15 * 60)
except Exception:
    pass

import base64
import collections
import csv
import difflib
import glob
import gzip
import io
import json
import math
import os
import re
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone

# Yahoo répond 403 aux requêtes sans empreinte TLS de navigateur depuis les
# machines du nuage (requirements.txt du dépôt) : curl_cffi quand il est là.
try:
    from curl_cffi import requests as _cffi
except Exception:
    _cffi = None

CACHE_DIR = os.path.expanduser("~/Library/Caches/site_crypto_finance")
# Sortie déplaçable : tester sans écrire dans le dossier partagé par Syncthing.
OUT_DIR = os.environ.get("SCF_INDICES_OUT") or CACHE_DIR

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
# Wikipédia demande un agent qui dit qui appelle.
UA_WIKI = ("SiteCryptoFinance-indices/1.0 (%s)"
           % (os.environ.get("SCF_CONTACT_UA") or "https://site-crypto-finance.pages.dev"))

SCREENER = "https://stockanalysis.com/_api/endpoints/screener/data-points"

# ── LES DIX-SEPT INDICES ─────────────────────────────────────────────────────
# `pays` est le code du screener (la place de COTATION, pas le siège) ;
# `plafond` le poids maximal d'un membre selon le règlement de l'indice.
INDICES = [
    dict(code="sp500", nom="S&P 500", ticker="^GSPC", pays="US", source="ssga",
         devise="USD"),
    dict(code="csi300", nom="CSI 300", ticker="000300.SS", pays="CN", source="wiki",
         page="CSI 300 Index", attendus=(280, 310), devise="CNY"),
    dict(code="nifty50", nom="Nifty 50", ticker="^NSEI", pays="IN", source="wiki",
         page="NIFTY 50", attendus=(48, 52), devise="INR"),
    dict(code="nikkei225", nom="Nikkei 225", ticker="^N225", pays="JP", source="nikkei",
         devise="JPY"),
    dict(code="dax40", nom="DAX 40", ticker="^GDAXI", pays="DE", source="wiki",
         page="DAX", attendus=(38, 42), devise="EUR"),
    dict(code="cac40", nom="CAC 40", ticker="^FCHI", pays="FR", source="wiki",
         page="CAC 40", attendus=(38, 42), plafond=15.0, devise="EUR"),
    dict(code="ftse100", nom="FTSE 100", ticker="^FTSE", pays="UK", source="wiki",
         page="FTSE 100 Index", attendus=(95, 105), devise="GBP"),
    dict(code="kospi", nom="KOSPI", ticker="^KS11", pays="KR", source="place",
         place="Korea Stock Exchange", devise="KRW"),
    dict(code="ibov", nom="Bovespa", ticker="^BVSP", pays="BR", source="b3",
         devise="BRL"),
    dict(code="tsx", nom="TSX Composite", ticker="^GSPTSE", pays="CA", source="wiki",
         page="S&P/TSX Composite Index", attendus=(180, 260), devise="CAD"),
    dict(code="asx200", nom="ASX 200", ticker="^AXJO", pays="AU", source="wiki",
         page="S&P/ASX 200", attendus=(190, 205), devise="AUD"),
    dict(code="taiex", nom="TAIEX", ticker="^TWII", pays="TW", source="place",
         place="Taiwan Stock Exchange", devise="TWD"),
    dict(code="hsi", nom="Hang Seng", ticker="^HSI", pays="HK", source="wiki",
         page="Hang Seng Index", attendus=(60, 110), plafond=8.0, devise="HKD"),
    dict(code="ftsemib", nom="FTSE MIB", ticker="FTSEMIB.MI", pays="IT", source="wiki",
         page="FTSE MIB", attendus=(38, 42), plafond=15.0, devise="EUR"),
    dict(code="ibex35", nom="IBEX 35", ticker="^IBEX", pays="ES", source="wiki",
         page="IBEX 35", attendus=(33, 37), devise="EUR"),
    dict(code="smi", nom="SMI", ticker="^SSMI", pays="CH", source="wiki",
         page="Swiss Market Index", attendus=(19, 21), plafond=18.0, devise="CHF"),
    dict(code="ipc", nom="IPC Mexico", ticker="^MXX", pays="MX", source="wiki",
         page="Indice de Precios y Cotizaciones", attendus=(33, 37), plafond=25.0,
         devise="MXN"),
]

# ── LES ETF QUI AIDENT À PONDÉRER ──
# Un ETF américain qui RÉPLIQUE l'indice donne ses 25 premiers poids exacts
# (page « holdings » de stockanalysis, mêmes chemins que le screener).
ETF_REPLIQUE = {"dax40": "dax", "csi300": "ashr", "nifty50": "indy"}
# Un ETF MSCI « standard » (non plafonné 25/50) du même pays compte chaque LIGNE
# cotée pour ce qu'elle est. Il sert à recaler les poids RELATIFS des titres que
# le flottant seul surestime : doubles cotations (Rio Tinto à Londres et Sydney),
# actions A+H chinoises (ICBC à Hong Kong porte la capitalisation de Shanghai).
# Mesuré le 30/09/2026 : hors ces cas, l'estimation tombe à ±5 % de l'ETF.
# ⚠ Jamais un ETF 25/50 (EWI, EWP, EWL, EWT, EWY, EWH, EWW) : son plafond
# fausserait justement les plus gros poids.
ETF_RECALAGE = {"hsi": "mchi", "asx200": "ewa", "ftse100": "ewu", "tsx": "ewc", "cac40": "ewq"}

# Places supplémentaires à interroger : un membre du CAC 40 peut être coté à
# Amsterdam (ArcelorMittal), un membre du FTSE MIB aussi (Stellantis, Ferrari).
PLACES_EN_PLUS = {"FR": ["NL", "BE"], "IT": ["NL"], "DE": ["NL", "FR"]}

# Préfixe de chemin du screener → suffixe du symbole Yahoo, celui que la fiche
# société attend dans l'adresse (#societe=MC.PA).
SUFFIXE = {"epa": ".PA", "ams": ".AS", "ebr": ".BR", "etr": ".DE", "fra": ".F",
           "lon": ".L", "tyo": ".T", "hkg": ".HK", "sha": ".SS", "she": ".SZ",
           "nse": ".NS", "bom": ".BO", "krx": ".KS", "tpe": ".TW", "bvmf": ".SA",
           "tsx": ".TO", "asx": ".AX", "bit": ".MI", "bme": ".MC", "swx": ".SW",
           "bmv": ".MX", "eli": ".LS"}
PREFIXE = {v: k for k, v in SUFFIXE.items()}

CHAMPS = """
name isin exchange country priceCurrency sector industry price marketCap float
floatPercent sharesOut high52 low52 ma50 ma200 rsi beta allTimeHigh
allTimeHighChange allTimeHighDate high52ch low52ch peRatio peForward psRatio
pbRatio evEbitda earningsYield fcfYield dividendYield buybackYield grossMargin
operatingMargin profitMargin roe roic revenueGrowth epsGrowth revenueThisYear
debtEquity debtEbitda ch1m ch3m ch6m chYTD ch1y ch3y ch5y analystRatings
priceTargetChange interestCoverage currentRatio payoutRatio revenue3y marketCapUsd
""".split()


MAX_HISTO = 1000

_last = [0.0]


def log(*a):
    print(*a, file=sys.stderr)


def _get(url, ua=UA, accept="*/*", referer=None, debit=0.35, essais=3, timeout=120, navigateur=False):
    for essai in range(essais):
        d = time.time() - _last[0]
        if d < debit:
            time.sleep(debit - d)
        _last[0] = time.time()
        h = {"User-Agent": ua, "Accept-Encoding": "gzip", "Accept": accept}
        if referer:
            h["Referer"] = referer
        if navigateur and _cffi is not None:
            try:
                r = _cffi.get(url, headers={"Accept": accept}, impersonate="chrome120", timeout=timeout)
                if r.status_code == 200:
                    return r.content
                if r.status_code in (400, 404):
                    return None
                if essai == essais - 1:
                    log("[warn] %s → HTTP %s" % (url[:90], r.status_code))
                    return None
                time.sleep((6 if r.status_code == 429 else 2) * (essai + 1))
            except Exception as e:
                if essai == essais - 1:
                    log("[warn] %s → %s" % (url[:90], e))
                    return None
                time.sleep(2 * (essai + 1))
            continue
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                        timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            if e.code == 429:
                time.sleep(6 * (essai + 1))
                continue
            if essai == essais - 1:
                log("[warn] %s → HTTP %s" % (url[:90], e.code))
                return None
            time.sleep(2 * (essai + 1))
        except Exception as e:
            if essai == essais - 1:
                log("[warn] %s → %s" % (url[:90], e))
                return None
            time.sleep(2 * (essai + 1))
    return None


def estnb(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def rd(x, n=2):
    return round(x, n) if estnb(x) else None


# ── LE SCREENER ──────────────────────────────────────────────────────────────

def screener(pays):
    """{chemin: {champ: valeur}} pour toute une place. None si la source se tait."""
    url = SCREENER + "?type=s&ids=" + "+".join(CHAMPS) + ("" if pays == "US" else "&c=" + pays)
    raw = _get(url, accept="application/json", referer="https://stockanalysis.com/",
               timeout=180)
    if not raw:
        return None
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
        x = d.get("data", d)
        if isinstance(x, dict) and "data" in x:
            x = x["data"]
        return x if isinstance(x, dict) and x else None
    except Exception:
        return None


def symbole_yahoo(chemin):
    if "/" not in chemin:
        return chemin.replace(".", "-")          # BRK.B → BRK-B
    pre, t = chemin.split("/", 1)
    suf = SUFFIXE.get(pre)
    if not suf:
        return None
    if pre == "bmv":
        # Yahoo colle la série au code à Mexico : AMXB.MX, GFNORTEO.MX — et non
        # AMX-B.MX, qui ne répond pas (21 membres de l'IPC sans cours sinon).
        return t.replace(".", "") + suf
    return t.replace(".", "-") + suf


# ── LES SOURCES DE COMPOSITION ───────────────────────────────────────────────
# Chacune rend (membres, meta) ; un membre = {cle, nom, poids_officiel|None}.
# `cle` est le chemin du screener quand on sait le former, sinon un symbole à
# résoudre.

def compo_ssga():
    u = ("https://www.ssga.com/us/en/intermediary/library-content/products/"
         "fund-data/etfs/us/holdings-daily-us-en-spy.xlsx")
    raw = _get(u, timeout=60)
    if not raw or raw[:2] != b"PK":
        return None, None
    z = zipfile.ZipFile(io.BytesIO(raw))
    ss = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
    chaines = [re.sub(r"<[^>]+>", "", m) for m in re.findall(r"<si>(.*?)</si>", ss, re.S)]
    feuille = z.read("xl/worksheets/sheet1.xml").decode("utf-8", "replace")
    lignes = []
    for rx in re.findall(r"<row[^>]*>(.*?)</row>", feuille, re.S):
        cel = {}
        for c in re.finditer(r'<c r="([A-Z]+)\d+"([^>]*?)(?:/>|>(.*?)</c>)', rx, re.S):
            v = re.search(r"<v>(.*?)</v>", c.group(3) or "")
            v = v.group(1) if v else ""
            if 't="s"' in c.group(2) and v != "":
                v = chaines[int(v)]
            cel[c.group(1)] = v.replace("&amp;", "&")
        lignes.append(cel)
    date = None
    membres = []
    entete = False
    for cel in lignes:
        if cel.get("A", "").startswith("Holdings:"):
            m = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", cel.get("B", ""))
            if m:
                date = datetime.strptime(m.group(1), "%d-%b-%Y").strftime("%Y-%m-%d")
        if cel.get("A") == "Name" and cel.get("E") == "Weight":
            entete = True
            continue
        if entete:
            t = (cel.get("B") or "").strip()
            try:
                w = float(cel.get("E") or "")
            except ValueError:
                continue
            # ⚠ Un droit à paiement conditionnel (CVR Hologic, libellé « TPG INC »
            # dans le fichier du 29/09/2026) porte un poids infime : il faisait un
            # 504e « membre ». Le plus petit vrai membre pèse cent fois plus.
            if t and t != "-" and w >= 0.0005 and not re.search(r"\b(CVR|RIGHTS?)\b", cel.get("A") or ""):
                membres.append({"cle": t, "nom": cel.get("A"), "poids_officiel": w})
    return membres, {"source": "Avoirs du SPY (State Street)", "url": "https://www.ssga.com/us/en/intermediary/etfs/spdr-sp-500-etf-trust-spy",
                     "date": date, "poids": "officiels"}


def compo_nikkei():
    u = "https://indexes.nikkei.co.jp/en/nkave/archives/file/nikkei_stock_average_weight_en.csv"
    raw = _get(u, timeout=60)
    if not raw:
        return None, None
    t = raw.decode("latin-1", "replace")
    membres, date = [], None
    for l in csv.reader(io.StringIO(t)):
        if len(l) < 6 or not re.match(r"^\d{4}/\d{2}/\d{2}$", l[0] or ""):
            continue
        date = l[0].replace("/", "-")
        try:
            w = float(l[5].rstrip("%"))
        except ValueError:
            continue
        membres.append({"cle": "tyo/" + l[1].strip(), "nom": l[2].strip().title(),
                        "poids_officiel": w})
    return membres, {"source": "Fichier de poids de Nikkei Inc.", "url": "https://indexes.nikkei.co.jp/en/nkave/index/component?idx=nk225",
                     "date": date, "poids": "officiels (mensuels)"}


def compo_b3():
    p = base64.b64encode(json.dumps({"language": "pt-br", "pageNumber": 1, "pageSize": 150,
                                     "index": "IBOV", "segment": "1"}).encode()).decode()
    raw = _get("https://sistemaswebb3-listados.b3.com.br/indexProxy/indexCall/GetPortfolioDay/" + p,
               accept="application/json", timeout=60)
    if not raw:
        return None, None
    try:
        d = json.loads(raw)
    except Exception:
        return None, None
    membres = []
    for r in d.get("results") or []:
        try:
            w = float(str(r.get("part")).replace(".", "").replace(",", "."))
        except ValueError:
            continue
        membres.append({"cle": "bvmf/" + (r.get("cod") or "").strip(),
                        "nom": (r.get("asset") or "").strip().title(), "poids_officiel": w})
    date = None
    m = re.match(r"(\d{2})/(\d{2})/(\d{2})", ((d.get("header") or {}).get("date") or ""))
    if m:
        date = "20%s-%s-%s" % (m.group(3), m.group(2), m.group(1))
    return membres, {"source": "Portefeuille théorique de la B3", "url": "https://www.b3.com.br/pt_br/market-data-e-indices/indices/indices-amplos/ibovespa.htm",
                     "date": date, "poids": "officiels"}


class _Tables(__import__("html.parser").parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.pile, self.cell, self.row = [], [], None, None

    def handle_starttag(self, tag, a):
        if tag == "table":
            self.pile.append([])
        elif tag == "tr" and self.pile:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = ""
        elif tag == "br" and self.cell is not None:
            self.cell += " "

    def handle_endtag(self, tag):
        if tag == "table" and self.pile:
            self.tables.append(self.pile.pop())
        elif tag == "tr" and self.pile and self.row is not None:
            self.pile[-1].append(self.row)
            self.row = None
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", self.cell).strip())
            self.cell = None

    def handle_data(self, d):
        if self.cell is not None:
            self.cell += d


def _propre(s):
    return re.sub(r"\[[^\]]*\]", "", s or "").strip()


COL_TICKER = ("ticker", "symbol", "code", "ticker symbol", "epic")
COL_NOM = ("company", "name", "company name", "constituent")
COL_POIDS = ("weighting", "index weighting", "weight")
COL_CAPI = ("market capitalisation", "market capitalization", "market cap")


def compo_wiki(ix):
    titre = ix["page"]
    u = ("https://en.wikipedia.org/w/api.php?action=parse&page=%s&prop=text|revid&format=json&redirects=1"
         % urllib.parse.quote(titre))
    raw = _get(u, ua=UA_WIKI, accept="application/json", debit=2.0, essais=5)
    if not raw:
        return None, None
    try:
        d = json.loads(raw)["parse"]
    except Exception:
        return None, None
    p = _Tables()
    p.feed(d["text"]["*"])
    lo, hi = ix["attendus"]
    meilleur = None
    for tb in p.tables:
        if len(tb) < 2:
            continue
        tete = [_propre(c).lower() for c in tb[0]]
        it = next((i for i, c in enumerate(tete) if any(c.startswith(k) for k in COL_TICKER)), None)
        inn = next((i for i, c in enumerate(tete) if any(c.startswith(k) for k in COL_NOM)), None)
        if it is None:
            continue
        n = len(tb) - 1
        if lo <= n <= hi and (meilleur is None or abs(n - (lo + hi) / 2) < abs(len(meilleur[0]) - 1 - (lo + hi) / 2)):
            ip = next((i for i, c in enumerate(tete) if any(c.startswith(k) for k in COL_POIDS)), None)
            ic = next((i for i, c in enumerate(tete) if any(c.startswith(k) for k in COL_CAPI)), None)
            meilleur = (tb, it, inn, ip, ic)
    if not meilleur:
        log("[warn] %s : aucune table de %d à %d membres sur Wikipédia" % (ix["nom"], lo, hi))
        return None, None
    tb, it, inn, ip, ic = meilleur
    membres = []
    for row in tb[1:]:
        if it >= len(row):
            continue
        t = _propre(row[it])
        if not t:
            continue
        nom = _propre(row[inn]) if inn is not None and inn < len(row) else None
        pw = None
        if ip is not None and ip < len(row):
            try:
                pw = float(re.sub(r"[^0-9.]", "", _propre(row[ip])) or "x")
            except ValueError:
                pw = None
        cw = None
        if ic is not None and ic < len(row):
            try:
                cw = float(re.sub(r"[^0-9.]", "", _propre(row[ic]).replace(",", "")) or "x")
            except ValueError:
                cw = None
        membres.append({"cle": t, "nom": nom, "poids_wiki": pw, "capi_wiki": cw,
                        "poids_officiel": None})
    # La date de la dernière modification de la page : c'est elle qui dit si la
    # liste peut avoir manqué une entrée ou une sortie récente.
    date = None
    q = _get("https://en.wikipedia.org/w/api.php?action=query&prop=revisions&rvprop=timestamp&format=json&titles="
             + urllib.parse.quote(titre) + "&redirects=1", ua=UA_WIKI, accept="application/json", debit=2.0)
    try:
        pg = next(iter(json.loads(q)["query"]["pages"].values()))
        date = pg["revisions"][0]["timestamp"][:10]
    except Exception:
        pass
    return membres, {"source": "Liste des membres sur Wikipédia", "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(titre.replace(" ", "_")),
                     "date": date, "poids": "estimés (capitalisation flottante%s)"
                     % (", plafond %g %%" % ix["plafond"] if ix.get("plafond") else "")}


def chemin_wiki(ix, t):
    """Le ticker tel que Wikipédia l'écrit → chemin du screener."""
    t = t.strip().upper().replace("–", "-")
    c = ix["code"]
    if c == "hsi":
        m = re.search(r"(\d+)", t)
        return "hkg/%04d" % int(m.group(1)) if m else None
    if c == "csi300":
        m = re.search(r"(SSE|SZSE)\s*:\s*(\d{6})", t)
        if m:
            return ("sha/" if m.group(1) == "SSE" else "she/") + m.group(2)
        m = re.search(r"(\d{6})", t)
        return ("sha/" if t.startswith("6") else "she/") + m.group(1) if m else None
    if c == "ipc":
        return "bmv/" + re.sub(r"\s+", ".", t.split(":")[-1].strip())
    m = re.match(r"^([A-Z0-9&\-.]+?)(\.[A-Z]{1,2})?$", t.split(":")[-1].strip())
    if not m:
        return None
    base, suf = m.group(1).rstrip("."), (m.group(2) or "")
    defaut = {"cac40": "epa", "dax40": "etr", "ftse100": "lon", "ftsemib": "bit",
              "ibex35": "bme", "smi": "swx", "ipc": "bmv", "asx200": "asx", "tsx": "tsx",
              "nifty50": "nse"}[c]
    if suf and suf in PREFIXE and c not in ("ftse100", "tsx"):
        return PREFIXE[suf] + "/" + base
    if c in ("ftse100", "tsx") and suf:
        # « BT.A », « BBD.B », « REI.UN » : le point fait partie du ticker.
        return defaut + "/" + base + suf
    return defaut + "/" + base


# ── RAPPROCHEMENT PAR LE NOM (quand le ticker de la liste ne tombe pas juste) ──

_FORMES = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|plc|ag|se|sa|nv|spa|s p a|"
                     r"ltd|limited|holdings?|group|the|adr|ord|class [a-z]|sab de cv|bhd|tbk|"
                     r"n v|s a|a s|asa|ab|oyj|kgaa|reit|trust|units?)\b")


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s.replace("&", " and "))
    s = _FORMES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def par_le_nom(nom, lignes, deja):
    if not nom:
        return None
    n = _norm(nom)
    if not n:
        return None
    best, score, second = None, 0.0, 0.0
    for k, v in lignes.items():
        if k in deja:
            continue
        s = difflib.SequenceMatcher(None, n, _norm(v.get("name"))).ratio()
        if s > score:
            best, second, score = k, score, s
        elif s > second:
            second = s
    # Un nom très proche ET nettement meilleur que le suivant : sinon on préfère
    # déclarer le membre absent plutôt que de le confondre avec un homonyme.
    return best if score >= 0.86 and score - second >= 0.04 else None


# ── LES POIDS ────────────────────────────────────────────────────────────────

def ratio_flottant(v):
    fp = v.get("floatPercent")
    if estnb(fp) and 5 <= fp <= 100:
        return fp / 100.0
    fl, so = v.get("float"), v.get("sharesOut")
    if estnb(fl) and estnb(so) and so > 0 and 0.05 <= fl / so <= 1.0:
        return fl / so
    return None


def plafonner(poids, plafond):
    """Plafonnement itératif : l'excédent se répartit au prorata des autres."""
    tot = sum(poids.values()) or 1.0
    if not plafond:
        return {k: x / tot for k, x in poids.items()}
    cap = plafond / 100.0
    w = dict(poids)
    for _ in range(50):
        tot = sum(w.values())
        w = {k: x / tot for k, x in w.items()}
        trop = {k for k, x in w.items() if x > cap + 1e-12}
        if not trop:
            break
        exces = sum(w[k] - cap for k in trop)
        libres = sum(x for k, x in w.items() if k not in trop)
        for k in w:
            w[k] = cap if k in trop else w[k] + exces * w[k] / libres
    return w


# ── L'INDICE LUI-MÊME : ses clôtures, son record, ses performances ──────────
#
# ⚠ LA PERFORMANCE D'UN INDICE EST CELLE QU'IL PUBLIE (30/09/2026). La première
# version affichait en tête « l'indice (reconstitué) » : la somme des variations
# de ses membres ACTUELS, pondérés par leur poids de départ. Elle s'écartait de
# l'indice publié de 0,3 à 9 points sur un an (Bovespa +17,8 % contre +26,9 %,
# DAX +3,4 % contre +6,7 %) : entrées et sorties de la période, poids estimés,
# et surtout les DIVIDENDES que le DAX et l'Ibovespa réinvestissent. Le chiffre
# de tête est désormais toujours l'indice publié ; la reconstitution ne sert
# plus qu'à DÉCOMPOSER ce chiffre titre par titre, et l'écart restant est
# publié à côté, sous son nom.
#
# LES FENÊTRES sont celles du Comparateur (fetch_comparateur.py, FENETRES) :
# mêmes durées, même référence — le dernier cours AU PLUS TARD la date cible.
# Un indice ne peut pas afficher deux performances différentes selon l'onglet.
# « 3a » en plus : le screener publie ch3y pour les membres.
FENETRES = [("1s", 7), ("1m", 30), ("3m", 91), ("6m", 182), ("ytd", "ytd"),
            ("1a", 365), ("2a", 730), ("3a", 1095), ("5a", 1826), ("10a", 3650)]
# Fenêtres couvertes par les cours quotidiens des membres (trois ans tirés) :
# au-delà, l'attribution titre par titre ne serait plus que l'histoire des
# survivants — on publie l'indice, pas sa décomposition.
FEN_MEMBRES = ("1s", "1m", "3m", "6m", "ytd", "1a", "2a")
FEN_CONTRIB = ("1m", "3m", "6m", "ytd", "1a", "2a")
SCREENER_FEN = {"1m": "ch1m", "3m": "ch3m", "6m": "ch6m", "ytd": "chYTD", "1a": "ch1y"}

# Indices de RENTABILITÉ (dividendes réinvestis). Leurs membres sont alors lus
# en cours AJUSTÉS des dividendes : c'est la seule façon que la somme des
# contributions retombe sur l'indice publié.
RENTABILITE = {
    "dax40": "Le DAX est un indice de rentabilité : les dividendes y sont réinvestis.",
    "ibov": "L'Ibovespa est un indice de rentabilité : les dividendes y sont réinvestis.",
}

# ⚠ PAS DE « POIDS ÉGAUX » RECONSTITUÉ (30/09/2026). Les membres d'AUJOURD'HUI
# rééquilibrés à poids égaux donnaient pour le S&P 500 +13,2 % sur un an et
# +28,0 % sur deux, contre +10,0 % et +16,4 % pour l'indice officiel S&P 500
# Equal Weight (^SPXEW) — alors que la semaine et le mois tombaient au dixième
# près. C'est le biais du survivant : les titres entrés en cours de route, choisis
# parce qu'ils avaient monté, comptent depuis le début ; ceux qui sont sortis
# n'y sont plus. À poids égaux, chacun pèse autant qu'Apple : l'erreur est
# énorme. On ne publie donc que l'équipondéré OFFICIEL, là où il existe. Pour
# les autres, on décrit « les membres d'aujourd'hui » (titre médian, part qui
# bat l'indice) — une phrase exacte telle qu'elle est écrite.
EGAL_OFFICIEL = {"sp500": ("^SPXEW", "S&P 500 Equal Weight")}

SPARK = "https://query1.finance.yahoo.com/v8/finance/spark"
LOT = 20                 # au-delà de 20 symboles, le spark répond 400
SAUT_MAX = 0.60          # |variation quotidienne| au-delà : erreur de cours, pas un marché
PIC_FACTEUR = 5.0        # un cours > 5× (ou < 1/5) la médiane de ses 10 voisins : retiré
TROU_JOURS = 30          # une performance ne franchit pas une suspension plus longue
REPORT_MAX = 10          # jours calendaires pendant lesquels un dernier cours est reporté
JOURS_SERIES = 520       # ~2 ans de séances pour les séries publiées
PART_SEANCE_MIN = 0.25   # sans cours publié de l'indice : une séance = 25 % du poids cote


def nettoyer(serie):
    """Retire les cours ISOLÉS aberrants — règle de fetch_comparateur.py : un
    point plus de 5 fois au-dessus ou au-dessous de la médiane de ses 5 voisins
    de chaque côté. Un vrai changement de niveau persiste et reste."""
    if len(serie) < 11:
        return serie, 0
    lp = [math.log(c) for _, c in serie]
    garde, n = [], 0
    for i, pt in enumerate(serie):
        vois = lp[max(0, i - 5):i] + lp[i + 1:i + 6]
        if abs(lp[i] - statistics.median(vois)) > math.log(PIC_FACTEUR):
            n += 1
            continue
        garde.append(pt)
    return garde, n


def en_dates_locales(pts):
    """Règle de fetch_comparateur.py. Yahoo horodate une barre quotidienne à
    l'OUVERTURE de la séance, en UTC — Sydney ouvre à 23 h UTC la veille, la date
    UTC est fausse d'un jour. On ramène l'ouverture la plus fréquente vers 9 h 30
    locales, ce qui donne le décalage, donc la date de séance."""
    heures = collections.Counter(round(((t % 86400) / 3600.0) * 4) / 4 for t, _ in pts)
    h = heures.most_common(1)[0][0]
    decalage = ((9.5 - h + 12) % 24) - 12
    jours = collections.OrderedDict()
    for t, c in pts:
        jours[datetime.fromtimestamp(t + decalage * 3600, timezone.utc).date().isoformat()] = c
    return list(jours.items())


def yahoo_jours(ticker, periode="max", ajuste=False):
    """Clôtures quotidiennes [(date de séance ISO, clôture)], nettoyées. La date
    est ramenée à l'heure de la place par meta.gmtoffset."""
    # ⚠ `range=max` est ramené par Yahoo à des barres MENSUELLES (mesuré le
    # 30/09/2026 : 440 points sur le CAC 40). Les bornes explicites gardent le pas
    # quotidien : 9 439 séances depuis 1990.
    plage = ("period1=0&period2=%d" % int(time.time())) if periode == "max" else ("range=" + periode)
    u = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?%s&interval=1d%s"
         % (urllib.parse.quote(ticker, safe=""), plage, "&events=div,split" if ajuste else ""))
    raw = _get(u, accept="application/json", timeout=60, navigateur=True)
    if not raw:
        return []
    try:
        r = json.loads(raw)["chart"]["result"][0]
        off = int((r.get("meta") or {}).get("gmtoffset") or 0)
        ts = r["timestamp"]
        cl = (r["indicators"]["adjclose"][0]["adjclose"] if ajuste
              else r["indicators"]["quote"][0]["close"])
    except Exception:
        return []
    jours = collections.OrderedDict()
    for t, c in zip(ts, cl):
        if estnb(c) and c > 0:
            jours[datetime.fromtimestamp(t + off, timezone.utc).date().isoformat()] = float(c)
    out = list(jours.items())
    # ⚠ La séance EN COURS n'est pas une clôture (règle de fetch_comparateur_indices.py) :
    # deux passages sur quatre tombent pendant la séance de New York, et le S&P
    # affichait « +14,4 % sur un an » à 22 h pour +15,0 % à la clôture de la veille.
    per = ((r.get("meta") or {}).get("currentTradingPeriod") or {}).get("regular") or {}
    if out and per.get("start") and per.get("end") and time.time() < per["end"]:
        seance = datetime.fromtimestamp(per["start"] + off, timezone.utc).date().isoformat()
        if out[-1][0] == seance:
            out.pop()
    return nettoyer(out)[0]


def spark_jours(symboles, periode="3y"):
    """{symbole: [(date de séance, clôture)]} par lots de 20. Rend (cours, lots en échec)."""
    out, echecs = {}, 0
    for i in range(0, len(symboles), LOT):
        lot = symboles[i:i + LOT]
        u = (SPARK + "?symbols=" + ",".join(urllib.parse.quote(s, safe="") for s in lot)
             + "&range=" + periode + "&interval=1d")
        raw = _get(u, accept="application/json", timeout=40, debit=0.15, navigateur=True)
        try:
            d = json.loads(raw) if raw else None
        except Exception:
            d = None
        if not isinstance(d, dict):
            echecs += 1
            continue
        for s, o in d.items():
            if not o or not o.get("timestamp"):
                continue
            pts = [(int(t), float(c)) for t, c in zip(o["timestamp"], o.get("close") or [])
                   if estnb(c) and c > 0]
            if len(pts) >= 5:
                out[s] = nettoyer(en_dates_locales(pts))[0]
    return out, echecs


def date_cible(fin, j):
    """La date de référence d'une fenêtre (celle du Comparateur)."""
    if j == "ytd":
        return date(fin.year, 1, 1) - timedelta(days=1)
    return fin - timedelta(days=j)


def dernier_au_plus_tard(dates, cible_iso):
    """Rang du dernier élément ≤ cible dans une liste de dates triées, ou None."""
    lo, hi = 0, len(dates)
    while lo < hi:
        m = (lo + hi) // 2
        if dates[m] <= cible_iso:
            lo = m + 1
        else:
            hi = m
    return lo - 1 if lo > 0 else None


def perfs_indice(serie):
    """{fenêtre: (performance %, date de référence)} lues sur les clôtures mêmes."""
    dates = [d for d, _ in serie]
    fin = date.fromisoformat(dates[-1])
    v = serie[-1][1]
    out = {}
    for f, j in FENETRES:
        c = date_cible(fin, j).isoformat()
        k = dernier_au_plus_tard(dates, c)
        # La série doit COUVRIR la date cible : une référence plus de dix jours
        # avant elle serait un autre point de départ.
        if k is None or (date.fromisoformat(c) - date.fromisoformat(dates[k])).days > REPORT_MAX:
            out[f] = (None, None)
        else:
            out[f] = (rd(100 * (v / serie[k][1] - 1), 2), dates[k])
    return out


def csindex_jours(code_indice="000300"):
    """Clôtures OFFICIELLES de China Securities Index. Yahoo ne rend qu'un point
    par jour pour le CSI 300 (000300.SS) ; cette source-ci rend 6 004 séances
    depuis 2002, contrôlées le 30/09/2026 contre EastMoney et Sina sur 5 040
    séances : écart maximal 0,01 point."""
    fin = time.strftime("%Y%m%d")
    u = ("https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode=%s&startDate=20020101&endDate=%s"
         % (code_indice, fin))
    raw = _get(u, accept="application/json", timeout=60, referer="https://www.csindex.com.cn/")
    try:
        d = json.loads(raw)
        jours = []
        for x in d.get("data") or []:
            t, c = str(x.get("tradeDate") or ""), x.get("close")
            if len(t) == 8 and estnb(c) and c > 0:
                jours.append(("%s-%s-%s" % (t[:4], t[4:6], t[6:]), float(c)))
        jours.sort()
        return nettoyer(jours)[0]
    except Exception:
        return []


SOURCE_NIVEAU = {"csi300": csindex_jours}


def niveau_indice(ticker, code=None):
    """Toute la vie cotée en clôtures quotidiennes : le record se lit sur des
    CLÔTURES de séance (un plus haut en séance n'est pas un niveau où l'indice a
    terminé une journée), et plus sur des fins de mois avant dix ans — une fin de
    mois n'est pas le plus haut du mois."""
    jours = SOURCE_NIVEAU[code]() if code in SOURCE_NIVEAU else yahoo_jours(ticker, "max")
    if len(jours) < 200:
        return None
    dernier_d, dernier = jours[-1]
    rec_v, rec_d = max((c, d) for d, c in jours)
    fin = date.fromisoformat(dernier_d)
    dix = (fin - timedelta(days=3652)).isoformat()
    j10 = [(d, c) for d, c in jours if d >= dix]
    # Semaines : la dernière clôture de chaque semaine ISO, sur dix ans.
    sem = collections.OrderedDict()
    for d, c in j10:
        y, w, _ = date.fromisoformat(d).isocalendar()
        sem[(y, w)] = (d, c)
    epoch = date(1970, 1, 1)
    hebdo = [[(date.fromisoformat(d) - epoch).days, rd(c, 2)] for d, c in sem.values()]
    pic, pire, pire_d = -1e18, 0.0, None
    for d, c in j10:
        pic = max(pic, c)
        if c / pic - 1 < pire:
            pire, pire_d = c / pic - 1, d
    un_an = [c for d, c in jours if d >= (fin - timedelta(days=365)).isoformat()]
    pf = perfs_indice(jours)
    return {
        "dernier": rd(dernier, 2), "date": dernier_d,
        "depuis": jours[0][0],
        "record": rd(rec_v, 2), "record_date": rec_d,
        "ecart_record_pct": rd(100 * (dernier / rec_v - 1), 2),
        "plus_haut_1a": rd(max(un_an), 2) if un_an else None,
        "pire_repli_10a_pct": rd(100 * pire, 1), "pire_repli_10a_date": pire_d,
        "perf": {f: v for f, (v, _) in pf.items()},
        "refs": {f: r for f, (_, r) in pf.items()},
        "hebdo": hebdo,
        # Les séances des trois dernières années : le calendrier des séries.
        "_jours": [(d, c) for d, c in jours if d >= (fin - timedelta(days=1100)).isoformat()],
    }


# ── LES MEMBRES, SÉANCE PAR SÉANCE ───────────────────────────────────────────

def cours_aligne(serie, cal):
    """Le cours d'un titre à chaque séance du calendrier : son dernier cours au
    plus tard ce jour-là, reporté au plus REPORT_MAX jours (au-delà : None — un
    titre suspendu ne doit pas figer l'indice)."""
    out, j, n = [], 0, len(serie)
    for d in cal:
        while j < n and serie[j][0] <= d:
            j += 1
        if j == 0:
            out.append(None)
            continue
        dd, c = serie[j - 1]
        out.append(c if (date.fromisoformat(d) - date.fromisoformat(dd)).days <= REPORT_MAX else None)
    return out


def drapeaux_moyennes(serie):
    """{date: (au-dessus MM50, au-dessus MM200, à 2 % du plus haut 52 semaines)}
    sur les séances PROPRES du titre (pas sur le calendrier de l'indice)."""
    out, cl = {}, [c for _, c in serie]
    s50 = s200 = 0.0
    for i, (d, c) in enumerate(serie):
        s50 += c
        s200 += c
        if i >= 50:
            s50 -= cl[i - 50]
        if i >= 200:
            s200 -= cl[i - 200]
        m50 = s50 / 50 if i >= 49 else None
        m200 = s200 / 200 if i >= 199 else None
        haut = max(cl[max(0, i - 251):i + 1]) if i >= 251 else None
        out[d] = (None if m50 is None else c > m50, None if m200 is None else c > m200,
                  None if haut is None else c >= 0.98 * haut)
    return out


def calendrier_membres(series, poids):
    """Sans clôtures publiées de l'indice (CSI 300) : une séance est un jour où
    au moins 25 % du poids a coté — la règle du Comparateur."""
    compte = collections.Counter()
    for k, s in series.items():
        for d, _ in s:
            compte[d] += poids.get(k, 0.0)
    tot = sum(poids.values()) or 1.0
    return sorted(d for d, w in compte.items() if w >= PART_SEANCE_MIN * tot)


def series_indice(rows, cours, niveau, egal=None):
    """Les séries quotidiennes publiées, sur ~2 ans de séances :
      · officiel   — clôtures publiées de l'indice ;
      · reconstitue — les membres ACTUELS pondérés par la capitalisation (poids
                      d'aujourd'hui ramenés en arrière par les cours) ;
      · egal_officiel — l'indice équipondéré OFFICIEL, s'il existe (EGAL_OFFICIEL) ;
      · mm50/mm200/haut52 — part des titres au-dessus de leur moyenne 50/200
                     séances, ou à 2 % de leur plus haut d'un an, en nombre et
                     en poids ;
      · top10      — poids des dix premiers.
    Composition d'AUJOURD'HUI tout du long : un titre entré en cours de route
    compte depuis le début. Dit à l'écran ; écart mesuré contre l'officiel."""
    avec = [r for r in rows if r["sym"] in cours and cours[r["sym"]]]
    if len(avec) < max(5, 0.6 * len(rows)):
        return None
    if niveau and niveau.get("_jours"):
        cal = [d for d, _ in niveau["_jours"]]
        off = dict(niveau["_jours"])
    else:
        cal = calendrier_membres({r["sym"]: cours[r["sym"]] for r in avec},
                                 {r["sym"]: r["w"] for r in avec})
        off = {}
    cal = cal[-(JOURS_SERIES + 1):]
    if len(cal) < 60:
        return None
    n = len(cal)
    px = {id(r): cours_aligne(cours[r["sym"]], cal) for r in avec}
    fin = {id(r): px[id(r)][-1] for r in avec}
    avec = [r for r in avec if fin[id(r)]]
    # Les drapeaux de moyennes, reportés comme les cours : un titre sans séance
    # ce jour-là (jour férié local) garde son dernier état connu.
    fl = {id(r): cours_aligne(list(drapeaux_moyennes(cours[r["sym"]]).items()), cal) for r in avec}
    reconst = [100.0]
    mm = {"mm50": [], "mm200": [], "haut52": []}
    mmp = {"mm50": [], "mm200": [], "haut52": []}
    top10, couv = [], []
    wT = {id(r): r["w"] for r in avec}
    for t in range(n):
        # poids du jour : poids d'aujourd'hui × (cours du jour / cours d'aujourd'hui)
        w = {i: wT[i] * px[i][t] / fin[i] for i in wT if px[i][t]}
        tw = sum(w.values())
        couv.append(rd(100 * sum(wT[i] for i in w) / (sum(wT.values()) or 1), 1))
        ws = sorted(w.values(), reverse=True)
        top10.append(rd(100 * sum(ws[:10]) / tw, 2) if tw else None)
        if t:
            num = den = 0.0
            for i, wi in wprec.items():
                a, b = px[i][t - 1], px[i][t]
                if a and b and abs(b / a - 1) < SAUT_MAX:
                    num += wi * b / a
                    den += wi
            reconst.append(reconst[-1] * (num / den) if den else reconst[-1])
        wprec = w
        for cle, pos in (("mm50", 0), ("mm200", 1), ("haut52", 2)):
            nb_ok = nb_oui = 0
            w_ok = w_oui = 0.0
            for r in avec:
                f = fl[id(r)][t]
                if not px[id(r)][t] or f is None or f[pos] is None:
                    continue
                nb_ok += 1
                w_ok += w.get(id(r), 0.0)
                if f[pos]:
                    nb_oui += 1
                    w_oui += w.get(id(r), 0.0)
            # Sous la moitié des titres mesurés, la part ne dit plus rien.
            ok = nb_ok >= max(5, 0.5 * len(avec))
            mm[cle].append(rd(100 * nb_oui / nb_ok, 1) if ok else None)
            mmp[cle].append(rd(100 * w_oui / w_ok, 1) if ok and w_ok else None)
    eg = dict((egal or {}).get("_jours") or [])
    for r in avec:
        f = fl[id(r)][-1]
        if f:
            r["mm50_drap"], r["mm200_drap"], r["haut52_drap"] = f
    return {
        "d": cal,
        "officiel": [rd(off.get(d), 2) for d in cal] if off else None,
        "egal_officiel": [rd(eg.get(d), 2) for d in cal] if eg else None,
        "reconstitue": [rd(x, 3) for x in reconst],

        "mm50": mm["mm50"], "mm50_poids": mmp["mm50"],
        "mm200": mm["mm200"], "mm200_poids": mmp["mm200"],
        "haut52": mm["haut52"], "haut52_poids": mmp["haut52"],
        "top10": top10, "couverture": couv,
        "n_titres": len(avec),
    }


def perfs_membres(rows, cours, niveau, cal_fin=None):
    """Pose r['p'] = {fenêtre: variation} sur les dates de référence de l'INDICE :
    dernier cours au plus tard la séance de référence de l'indice, pas plus de
    REPORT_MAX jours avant, et pas au travers d'une suspension de TROU_JOURS."""
    refs = (niveau or {}).get("refs") or {}
    if not refs and cal_fin:
        fin = date.fromisoformat(cal_fin)
        refs = {f: date_cible(fin, j).isoformat() for f, j in FENETRES}
    fin_d = (niveau or {}).get("date") or cal_fin
    for r in rows:
        r["p"] = {}
        s = cours.get(r["sym"])
        if not s or not fin_d:
            continue
        dates = [d for d, _ in s]
        kf = dernier_au_plus_tard(dates, fin_d)
        if kf is None or (date.fromisoformat(fin_d) - date.fromisoformat(dates[kf])).days > REPORT_MAX:
            continue
        for f in FEN_MEMBRES:
            ref = refs.get(f)
            if not ref:
                continue
            k = dernier_au_plus_tard(dates, ref)
            if k is None or (date.fromisoformat(ref) - date.fromisoformat(dates[k])).days > REPORT_MAX:
                continue
            trou = any((date.fromisoformat(b) - date.fromisoformat(a)).days > TROU_JOURS
                       for a, b in zip(dates[k:kf], dates[k + 1:kf + 1]))
            if trou:
                continue
            r["p"][f] = 100 * (s[kf][1] / s[k][1] - 1)


# ── AGRÉGATS ─────────────────────────────────────────────────────────────────

def somme_ponderee(rows, f, pos_seulement=False):
    """(Σ w·f, Σ w) sur les lignes où f est connu."""
    s, ws = 0.0, 0.0
    for r in rows:
        x = f(r)
        if not estnb(x) or (pos_seulement and x <= 0):
            continue
        s += r["w"] * x
        ws += r["w"]
    return s, ws


def inv(x):
    return 1.0 / x if estnb(x) and x != 0 else None


def mediane_ponderee(rows, f):
    pts = sorted((f(r), r["w"]) for r in rows if estnb(f(r)))
    if not pts:
        return None
    tot = sum(w for _, w in pts)
    acc = 0.0
    for x, w in pts:
        acc += w
        if acc >= tot / 2:
            return x
    return pts[-1][0]


def quantiles(xs, qs):
    xs = sorted(xs)
    if not xs:
        return [None for _ in qs]
    out = []
    for q in qs:
        i = (len(xs) - 1) * q
        a, b = int(math.floor(i)), int(math.ceil(i))
        out.append(xs[a] + (xs[b] - xs[a]) * (i - a))
    return out


def valorisation(rows):
    """Les multiples de l'indice, tous par rendements pondérés (voir en-tête)."""
    ey, wey = somme_ponderee(rows, lambda r: r["v"].get("earningsYield"))
    sy, wsy = somme_ponderee(rows, lambda r: inv(r["v"].get("psRatio")), pos_seulement=True)
    by, wby = somme_ponderee(rows, lambda r: inv(r["v"].get("pbRatio")), pos_seulement=True)
    # Le cash-flow libre et l'EBITDA d'une banque ou d'une foncière ne mesurent
    # rien de comparable : Itaú « rendait » 1 % de FCF. Hors financières, et dit.
    non_fin = [r for r in rows if (r["v"].get("sector") or "") not in ("Financials", "Real Estate")]
    fy, wfy = somme_ponderee(non_fin, lambda r: r["v"].get("fcfYield"))
    fwd, wfwd = somme_ponderee(rows, lambda r: inv(r["v"].get("peForward")), pos_seulement=True)
    eb, web = somme_ponderee(non_fin, lambda r: inv(r["v"].get("evEbitda")), pos_seulement=True)
    tot = sum(r["w"] for r in rows) or 1.0
    # Le dividende absent est un dividende NUL : Alphabet a longtemps été sans
    # champ, et renormaliser sur les seuls payeurs gonflerait le rendement.
    dy = sum(r["w"] * (r["v"].get("dividendYield") or 0.0) for r in rows) / tot
    bb, wbb = somme_ponderee(rows, lambda r: r["v"].get("buybackYield"))
    pe = 100.0 / (ey / wey) if wey and ey > 0 else None
    pes = [r["v"]["peRatio"] for r in rows if estnb(r["v"].get("peRatio")) and r["v"]["peRatio"] > 0]
    pertes = sum(r["w"] for r in rows if estnb(r["v"].get("earningsYield")) and r["v"]["earningsYield"] < 0)
    # ⚠ UNE PERTE GÉANTE FAIT LE P/E (30/09/2026) : Stellantis, 1,1 % du FTSE MIB,
    # perdait 1,7 fois sa capitalisation (rendement −172 %) et effaçait à elle
    # seule 31 % des bénéfices de l'indice — P/E 24,6 au lieu de 16,2 hors pertes.
    # Le P/E agrégé reste la mesure (celle de S&P), mais on publie à côté celui des
    # sociétés bénéficiaires (convention de Bloomberg), la part des bénéfices
    # effacée par les pertes et le plus gros perdant.
    gains = [(r, r["v"]["earningsYield"]) for r in rows if estnb(r["v"].get("earningsYield")) and r["v"]["earningsYield"] > 0]
    g_ey = sum(r["w"] * e for r, e in gains)
    g_w = sum(r["w"] for r, _ in gains)
    p_ey = sum(r["w"] * r["v"]["earningsYield"] for r in rows
               if estnb(r["v"].get("earningsYield")) and r["v"]["earningsYield"] < 0)
    perdant = min((r for r in rows if estnb(r["v"].get("earningsYield"))),
                  key=lambda r: r["w"] * r["v"]["earningsYield"], default=None)
    return {
        "pe": rd(pe, 1),
        "pe_couv": rd(100 * wey / tot, 0),
        "pe_mediane": rd(statistics.median(pes), 1) if pes else None,
        "pe_fwd": rd(wfwd / fwd, 1) if fwd > 0 else None,
        "pe_fwd_couv": rd(100 * wfwd / tot, 0),
        "ps": rd(wsy / sy, 2) if sy > 0 else None,
        "pb": rd(wby / by, 2) if by > 0 else None,
        "pfcf": rd(100.0 / (fy / wfy), 1) if wfy and fy > 0 else None,
        "ev_ebitda": rd(web / eb, 1) if eb > 0 else None,
        "rdt_benef": rd(ey / wey, 2) if wey else None,
        "rdt_fcf": rd(fy / wfy, 2) if wfy else None,
        "rdt_div": rd(dy, 2),
        "rdt_rachat": rd(bb / wbb, 2) if wbb else None,
        "rdt_actionnaire": rd(dy + (bb / wbb if wbb else 0.0), 2),
        "poids_en_perte": rd(100 * pertes / tot, 1),
        "pe_hors_pertes": rd(100.0 / (g_ey / g_w), 1) if g_w and g_ey > 0 else None,
        "pertes_part_benef": rd(-100 * p_ey / g_ey, 1) if g_ey > 0 and p_ey < 0 else 0.0,
        "plus_gros_perdant": ([perdant["nom"], rd(100 * perdant["w"], 2), rd(perdant["v"]["earningsYield"], 1)]
                              if perdant and perdant["v"]["earningsYield"] < 0 else None),
    }


def rentabilite(rows):
    """Marges et rendements de l'ENSEMBLE, pas moyennes de ratios : la marge
    nette de l'indice est Σ bénéfices / Σ chiffres d'affaires."""
    def couple(f_num, f_den):
        num = den = 0.0
        for r in rows:
            a, b = f_num(r), f_den(r)
            if estnb(a) and estnb(b) and b > 0:
                num += r["w"] * a
                den += r["w"] * b
        return num / den if den else None
    sy = lambda r: inv(r["v"].get("psRatio")) if estnb(r["v"].get("psRatio")) and r["v"]["psRatio"] > 0 else None
    by = lambda r: inv(r["v"].get("pbRatio")) if estnb(r["v"].get("pbRatio")) and r["v"]["pbRatio"] > 0 else None

    def pond_ca(champ, borne=None):
        num = den = 0.0
        for r in rows:
            s, x = sy(r), r["v"].get(champ)
            if estnb(x) and borne:
                x = max(-borne, min(borne, x))
            if estnb(s) and estnb(x):
                num += r["w"] * s * x
                den += r["w"] * s
        return num / den if den else None
    ey = lambda r: (r["v"].get("earningsYield") / 100.0) if estnb(r["v"].get("earningsYield")) else None
    non_fin = [r for r in rows if (r["v"].get("sector") or "") not in ("Financials", "Real Estate")]
    return {
        "marge_brute": rd(pond_ca("grossMargin"), 1),
        "marge_op": rd(pond_ca("operatingMargin"), 1),
        "marge_nette": rd(100 * couple(ey, sy), 1) if couple(ey, sy) is not None else None,
        "roe": rd(100 * couple(ey, by), 1) if couple(ey, by) is not None else None,
        "roic_med": rd(mediane_ponderee(rows, lambda r: r["v"].get("roic")), 1),
        "crois_ca": rd(pond_ca("revenueGrowth", 100.0), 1),
        # Une prévision de +132 % (Kioxia) pèse autant qu'un chiffre d'affaires :
        # la médiane pondérée résiste, la moyenne non.
        "crois_ca_prev": rd(mediane_ponderee(rows, lambda r: r["v"].get("revenueThisYear")), 1),
        "crois_bpa_med": rd(mediane_ponderee(rows, lambda r: r["v"].get("epsGrowth")), 1),
        "dette_ebitda_med": rd(mediane_ponderee(non_fin, lambda r: r["v"].get("debtEbitda")), 2),
        "dette_fp_med": rd(mediane_ponderee(non_fin, lambda r: r["v"].get("debtEquity")), 2),
        "beta": rd((lambda s: s[0] / s[1] if s[1] else None)(somme_ponderee(rows, lambda r: r["v"].get("beta"))), 2),
        "potentiel_analystes": rd(mediane_ponderee(
            [r for r in rows if estnb(r["v"].get("priceTargetChange")) and abs(r["v"]["priceTargetChange"]) <= 200],
            lambda r: r["v"].get("priceTargetChange")), 1),
        "part_achat": rd(100 * sum(r["w"] for r in rows if (r["v"].get("analystRatings") or "") in ("Buy", "Strong Buy"))
                         / (sum(r["w"] for r in rows if r["v"].get("analystRatings")) or 1), 0),
    }


def concentration(rows, total=None):
    """`total` : dénominateur imposé (1.0 quand les poids sont des parts de
    l'indice ENTIER, membres introuvables compris)."""
    ws = sorted((r["w"] for r in rows), reverse=True)
    tot = total or sum(ws) or 1.0
    ws = [w / tot for w in ws]
    cum, acc = [], 0.0
    for w in ws:
        acc += w
        cum.append(acc)

    def n_pour(p):
        return next((i + 1 for i, c in enumerate(cum) if c >= p), len(cum))
    hhi = sum(w * w for w in ws)
    rs = sorted(rows, key=lambda r: -r["w"])
    top10, reste = rs[:10], rs[10:]

    def part(rr, f):
        a = sum(r["w"] * f(r) for r in rr if estnb(f(r)))
        b = sum(r["w"] * f(r) for r in rows if estnb(f(r)))
        return 100 * a / b if b > 0 else None
    ey = lambda r: r["v"].get("earningsYield")
    sy = lambda r: inv(r["v"].get("psRatio")) if estnb(r["v"].get("psRatio")) and r["v"]["psRatio"] > 0 else None

    def pe_de(rr):
        s, w = somme_ponderee(rr, ey)
        return rd(100.0 / (s / w), 1) if w and s > 0 else None
    # La courbe cumulée, échantillonnée : les 50 premiers un par un, puis par pas.
    pts = []
    for i in range(len(cum)):
        if i < 50 or i % max(1, len(cum) // 60) == 0 or i == len(cum) - 1:
            pts.append([i + 1, rd(100 * cum[i], 2)])
    return {
        "top1": rd(100 * sum(ws[:1]), 2), "top5": rd(100 * sum(ws[:5]), 2),
        "top10": rd(100 * sum(ws[:10]), 2), "top20": rd(100 * sum(ws[:20]), 2),
        "hhi": rd(10000 * hhi, 0), "n_effectif": rd(1.0 / hhi, 1) if hhi else None,
        "n_50": n_pour(0.5), "n_80": n_pour(0.8), "n": len(ws),
        "part_benef_top10": rd(part(top10, ey), 1),
        "part_ca_top10": rd(part(top10, sy), 1),
        "pe_top10": pe_de(top10), "pe_reste": pe_de(reste),
        "courbe": pts,
    }


def seaux(xs_w, bornes):
    """Histogramme en nombre ET en poids ; bornes décroissantes [(bas, lib)]."""
    out = [[lib, 0, 0.0] for _, lib in bornes]
    tot_n = tot_w = 0
    for x, w in xs_w:
        if not estnb(x):
            continue
        tot_n += 1
        tot_w += w
        for i, (bas, _) in enumerate(bornes):
            if x >= bas:
                out[i][1] += 1
                out[i][2] += w
                break
    return [[lib, n, rd(100 * n / tot_n, 1) if tot_n else None, rd(100 * w / tot_w, 1) if tot_w else None]
            for lib, n, w in out]


BORNES_RECORD = [(-2, "au record (0 à −2 %)"), (-5, "−2 à −5 %"), (-10, "−5 à −10 %"),
                 (-20, "−10 à −20 %"), (-30, "−20 à −30 %"), (-50, "−30 à −50 %"),
                 (-1e9, "plus de −50 %")]
BORNES_PERF = [(100, "plus de +100 %"), (50, "+50 à +100 %"), (30, "+30 à +50 %"),
               (10, "+10 à +30 %"), (0, "0 à +10 %"), (-10, "0 à −10 %"),
               (-30, "−10 à −30 %"), (-50, "−30 à −50 %"), (-1e9, "plus de −50 %")]


def ampleur(rows):
    tot = sum(r["w"] for r in rows) or 1.0
    n = len(rows)

    def part(cond):
        ok = [r for r in rows if cond(r) is not None]
        if not ok:
            return None, None
        vrais = [r for r in ok if cond(r)]
        return (rd(100 * len(vrais) / len(ok), 1),
                rd(100 * sum(r["w"] for r in vrais) / (sum(r["w"] for r in ok) or 1), 1))

    def au_dessus(champ):
        # Les drapeaux posés par series_indice (cours quotidiens, les mêmes que
        # la courbe d'ampleur), à défaut ceux du screener (voir traiter).
        cle = {"ma50": "mm50_drap", "ma200": "mm200_drap"}[champ]

        def f(r):
            return r.get(cle)
        return f

    def seuil(champ, s, sens=1):
        def f(r):
            x = r["v"].get(champ)
            return ((x >= s) if sens > 0 else (x <= s)) if estnb(x) else None
        return f
    ath = [r["v"].get("allTimeHighChange") for r in rows if estnb(r["v"].get("allTimeHighChange"))]
    out = {}
    for cle, cond in (("mm50", au_dessus("ma50")), ("mm200", au_dessus("ma200")),
                      ("record_2", seuil("allTimeHighChange", -2)),
                      ("record_5", seuil("allTimeHighChange", -5)),
                      ("record_10", seuil("allTimeHighChange", -10)),
                      ("repli_20", seuil("allTimeHighChange", -20, -1)),
                      ("repli_50", seuil("allTimeHighChange", -50, -1)),
                      ("haut1a_2", seuil("high52ch", -2)),
                      ("haut1a_10", seuil("high52ch", -10)),
                      ("bas1a_5", seuil("low52ch", 5, -1)),
                      ("rsi_70", seuil("rsi", 70)), ("rsi_30", seuil("rsi", 30, -1))):
        nb, pw = part(cond)
        out[cle] = nb
        out[cle + "_poids"] = pw
    # Records de l'année : la date du record est dans les douze derniers mois.
    lim = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 365 * 86400))
    rec_an = [r for r in rows if (r["v"].get("allTimeHighDate") or "") >= lim]
    out["records_12m"] = rd(100 * len(rec_an) / n, 1) if n else None
    out["records_12m_poids"] = rd(100 * sum(r["w"] for r in rec_an) / tot, 1)
    out["ecart_record_median"] = rd(statistics.median(ath), 1) if ath else None
    out["ecart_record_pondere"] = rd(sum(r["w"] * r["v"]["allTimeHighChange"] for r in rows
                                         if estnb(r["v"].get("allTimeHighChange")))
                                     / (sum(r["w"] for r in rows if estnb(r["v"].get("allTimeHighChange"))) or 1), 1)
    out["hist_record"] = seaux([(r["v"].get("allTimeHighChange"), r["w"]) for r in rows], BORNES_RECORD)
    out["hist_haut1a"] = seaux([(r["v"].get("high52ch"), r["w"]) for r in rows], BORNES_RECORD)
    return out


def performance(rows, niveau, series, rentab=False, egal=None):
    """Par fenêtre : l'indice PUBLIÉ d'abord ; puis sa décomposition titre par
    titre sur les MÊMES séances (contribution = poids de départ × variation),
    l'écart entre la somme des contributions et l'indice publié sous son nom,
    les poids égaux lus sur la série quotidienne, le titre médian, la
    participation. Au-delà de deux ans, les variations des membres sont celles
    du screener et ne portent que sur les membres ACTUELS (dit à l'écran)."""
    off = (niveau or {}).get("perf") or {}
    refs = (niveau or {}).get("refs") or {}
    tot_w = sum(r["w"] for r in rows) or 1.0
    out = {}
    for h, _ in FENETRES:
        d = {"officiel": off.get(h)}
        if h in FEN_MEMBRES:
            pts = [(r, r["p"][h] / 100.0) for r in rows
                   if h in (r.get("p") or {}) and r["p"][h] > -99.9]
            d["source"] = "cours"
            # Cours quotidiens des membres indisponibles (Yahoo muet depuis la
            # machine de collecte) : les variations du screener, aux fenêtres
            # APPROCHÉES (un mois calendaire au lieu de 30 jours), dit dans
            # `source` — plutôt qu'aucune décomposition.
            champ = SCREENER_FEN.get(h)
            if champ and sum(r["w"] for r, _ in pts) < 0.6 * tot_w:
                pts = [(r, r["v"].get(champ) / 100.0) for r in rows
                       if estnb(r["v"].get(champ)) and r["v"][champ] > -99.9]
                for r, x in pts:
                    r.setdefault("p", {})[h] = 100 * x
                d["source"] = "screener (fenêtres approchées)"
        elif h in ("3a", "5a"):
            champ = {"3a": "ch3y", "5a": "ch5y"}[h]
            pts = [(r, r["v"].get(champ) / 100.0) for r in rows
                   if estnb(r["v"].get(champ)) and r["v"][champ] > -99.9]
            d["source"] = "screener (membres actuels)"
        else:
            pts = []
        if h in ("3a", "5a") and rentab:
            # Variations de COURS des membres face à un indice qui réinvestit les
            # dividendes : rien de comparable. On publie l'indice seul.
            pts = []
        couv = sum(r["w"] for r, _ in pts) / tot_w
        if len(pts) < max(5, 0.6 * len(rows)) or couv < 0.6:
            if estnb(d["officiel"]):
                out[h] = d
            continue
        w0 = {id(r): r["w"] / (1 + x) for r, x in pts}
        tot0 = sum(w0.values())
        R = sum(w0[id(r)] / tot0 * x for r, x in pts)
        xs = [x for _, x in pts]
        # « Font mieux que l'indice » : que l'indice PUBLIÉ quand on l'a.
        ref = d["officiel"] / 100.0 if estnb(d["officiel"]) else R
        longue = h not in FEN_MEMBRES
        d.update({
            # Au-delà de deux ans, les membres actuels ne recomposent plus l'indice
            # d'alors : pas de reconstitution, seulement ce que font ses membres.
            "reconstitue": None if longue else rd(100 * R, 2),
            "ecart": None if longue or not estnb(d["officiel"]) else rd(d["officiel"] - 100 * R, 2),
            "membres_actuels": longue,
            "mediane": rd(100 * statistics.median(xs), 2),
            "moyenne": rd(100 * sum(xs) / len(xs), 2),
            "positifs": rd(100 * sum(1 for x in xs if x > 0) / len(xs), 1),
            "battent": rd(100 * sum(1 for x in xs if x > ref) / len(xs), 1),
            "couverture": rd(100 * couv, 1), "n": len(xs),
        })
        if h in FEN_CONTRIB:
            c = sorted(((r, 100 * w0[id(r)] / tot0 * x) for r, x in pts), key=lambda t: -t[1])
            tot_c = 100 * R
            gros = {id(r) for r in sorted(rows, key=lambda r: -r["w"])[:10]}
            c10 = sum(ci for r, ci in c if id(r) in gros)
            # Quand l'indice a peu bougé (moins d'un point), « la moitié de son
            # mouvement » ne veut plus rien dire.
            petit = abs(tot_c) < 1.0
            gains = [ci for _, ci in c if ci > 0]
            g_tot, acc, ng = sum(gains), 0.0, None
            for i, ci in enumerate(gains):
                acc += ci
                if g_tot > 0 and acc >= 0.5 * g_tot:
                    ng = i + 1
                    break
            d["n_moitie_gains"] = ng
            d["gains_bruts"] = rd(g_tot, 2)
            d["pertes_brutes"] = rd(sum(ci for _, ci in c if ci < 0), 2)
            d["contrib_top10"] = rd(c10, 2)
            d["part_top10"] = rd(100 * c10 / tot_c, 0) if not petit and c10 * tot_c > 0 else None
            d["hausses"] = [[r["sym"], r["nom"], rd(ci, 3), rd(r["p"].get(h), 1)] for r, ci in c[:12]]
            d["baisses"] = [[r["sym"], r["nom"], rd(ci, 3), rd(r["p"].get(h), 1)]
                            for r, ci in list(reversed(c))[:12]]
            for r, ci in c:
                r.setdefault("contrib", {})[h] = rd(ci, 3)
        if h == "1a":
            d["hist"] = seaux([(100 * x, r["w"]) for r, x in pts], BORNES_PERF)
            q = quantiles([100 * x for x in xs], [0.1, 0.25, 0.5, 0.75, 0.9])
            d["quantiles"] = [rd(v, 1) for v in q]
            d["ecart_type"] = rd(100 * statistics.pstdev(xs), 1)
        out[h] = d
    # Les poids égaux : l'indice équipondéré OFFICIEL seulement (EGAL_OFFICIEL).
    for h, v in ((egal or {}).get("perf") or {}).items():
        if h in out:
            out[h]["equipondere"] = v
        elif estnb(v):
            out[h] = {"equipondere": v}
    return out


def repartition(rows, cle):
    """Les groupes d'un indice (secteurs, métiers, pays) : poids, titres, P/E du
    groupe, participation, et pour chaque fenêtre la variation du groupe
    (portefeuille pondéré au DÉPART) et sa contribution en points d'indice."""
    g = collections.OrderedDict()
    for r in sorted(rows, key=lambda r: -r["w"]):
        k = cle(r) or "Non classé"
        e = g.setdefault(k, {"w": 0.0, "n": 0, "ey": 0.0, "wey": 0.0, "mm200": 0, "mm200_n": 0,
                             "c": collections.Counter(), "p": {}, "top": []})
        e["w"] += r["w"]
        e["n"] += 1
        if len(e["top"]) < 3:
            e["top"].append(r["sym"])
        x = r["v"].get("earningsYield")
        if estnb(x):
            e["ey"] += r["w"] * x
            e["wey"] += r["w"]
        m = r.get("mm200_drap")
        if m is not None:
            e["mm200_n"] += 1
            e["mm200"] += 1 if m else 0
        for h, ci in (r.get("contrib") or {}).items():
            if estnb(ci):
                e["c"][h] += ci
        for h, x in (r.get("p") or {}).items():
            a = e["p"].setdefault(h, [0.0, 0.0, 0.0])      # Σ w_fin, Σ w_départ, Σ w_fin (couverts)
            if estnb(x) and x > -99.9:
                a[0] += r["w"]
                a[1] += r["w"] / (1 + x / 100.0)
    tot = sum(e["w"] for e in g.values()) or 1.0
    out = []
    for k, e in sorted(g.items(), key=lambda t: -t[1]["w"]):
        perf = {}
        for h, (wf, wd, _) in e["p"].items():
            # Pas de variation de groupe sur moins de 60 % de son poids.
            if wd > 0 and wf >= 0.6 * e["w"]:
                perf[h] = rd(100 * (wf / wd - 1), 2)
        out.append({
            "nom": k, "poids": rd(100 * e["w"] / tot, 2), "n": e["n"],
            "pe": rd(100.0 / (e["ey"] / e["wey"]), 1) if e["wey"] and e["ey"] > 0 else None,
            "mm200": rd(100 * e["mm200"] / e["mm200_n"], 0) if e["mm200_n"] else None,
            "perf": perf, "contrib": {h: rd(v, 3) for h, v in e["c"].items()},
            "top": e["top"],
        })
    return out


TAILLES = [(200e9, "géantes (> 200 Md$)"), (50e9, "très grandes (50-200 Md$)"),
           (10e9, "grandes (10-50 Md$)"), (2e9, "moyennes (2-10 Md$)"), (0, "petites (< 2 Md$)")]


# ── CHANGES (seulement pour afficher des milliards de dollars) ───────────────

def taux_usd():
    out = {"USD": 1.0}
    raw = _get("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml", timeout=30)
    if raw:
        par_euro = {m.group(1): float(m.group(2)) for m in
                    re.finditer(r"currency=.([A-Z]{3}).\s+rate=.([0-9.]+).", raw.decode("utf-8", "replace"))}
        if par_euro.get("USD"):
            out["EUR"] = par_euro["USD"]
            for dev, t in par_euro.items():
                if t > 0:
                    out[dev] = par_euro["USD"] / t
    if "TWD" not in out:      # la BCE ne cote pas le dollar de Taïwan
        raw = _get("https://open.er-api.com/v6/latest/USD", accept="application/json", timeout=30)
        try:
            for dev, t in json.loads(raw)["rates"].items():
                if t and dev not in out:
                    out[dev] = 1.0 / t
        except Exception:
            pass
    return out


# ── LE TRAITEMENT D'UN INDICE ────────────────────────────────────────────────

def lire_json(nom):
    for d in (OUT_DIR, CACHE_DIR):
        f = os.path.join(d, nom)
        if os.path.exists(f):
            try:
                with open(f, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                pass
    return None


def composer(ix, places, precedent):
    """Membres rattachés au screener + leurs poids. Rend (rows, meta, absents)."""
    src = ix["source"]
    membres, meta = None, None
    try:
        if src == "ssga":
            membres, meta = compo_ssga()
        elif src == "nikkei":
            membres, meta = compo_nikkei()
        elif src == "b3":
            membres, meta = compo_b3()
        elif src == "wiki":
            membres, meta = compo_wiki(ix)
    except Exception as e:
        log("[warn] %s : composition en échec (%s)" % (ix["nom"], e))
        membres = None
    lignes = {}
    for pl in places:
        lignes.update(pl or {})
    ix["_exact"] = False
    mx, meta_x = composition_exacte(ix, lignes)
    if mx:
        membres, meta, src = mx, meta_x, "exact"
        ix["_exact"] = True
        # Le total du FICHIER, membres introuvables compris (les fonds cotés du
        # FTSE 100 : 1,4 %) : on ne redistribue pas leur poids sur les autres.
        ix["_poids_total"] = sum(m["poids_officiel"] for m in mx)
    if src == "place":
        membres = [{"cle": k, "nom": v.get("name"), "poids_officiel": None}
                   for k, v in lignes.items()
                   if v.get("exchange") == ix["place"] and re.match(r"^[a-z]+/\d+$", k)]
        meta = {"source": "Toutes les actions ordinaires cotées (%s)" % ix["place"],
                "url": None, "date": time.strftime("%Y-%m-%d"),
                "poids": "estimés (capitalisation totale, comme l'indice)"}
    # ── UNE LISTE QUI NE REPOND PAS N'EST PAS UNE LISTE VIDE ──
    # On reprend la dernière composition connue, et la fiche le dit.
    reprise = None
    if not membres and precedent and precedent.get("membres"):
        membres = precedent["membres"]
        meta = dict(precedent.get("composition") or {})
        reprise = (precedent.get("genere_le") or "")[:10]
        log("[warn] %s : composition reprise du passage du %s" % (ix["nom"], reprise))
    if not membres:
        return None, None, None, None
    rows, absents, deja = [], [], set()
    for m in membres:
        cle = m["cle"]
        if src == "wiki":
            cle = chemin_wiki(ix, cle) or cle
        if src == "ssga":
            cle = cle.replace("/", ".")
        k = cle if cle in lignes and cle not in deja else None
        if not k and src in ("wiki", "exact"):
            # « BT.A » vs « BT-A », « RR. » vs « RR » : on essaie les graphies voisines.
            alts = [cle.replace(".", "-"), cle.replace("-", "."), cle.rstrip(".")]
            if ix["code"] == "ipc" and "." not in cle.split("/")[-1]:
                # La série colle au code dans la liste (AMXB, KOFUBL) et en est
                # séparée par un point à la Bourse de Mexico (AMX.B, KOF.UBL).
                b = cle.split("/")[-1]
                alts += ["bmv/%s.%s" % (b[:-n], b[-n:]) for n in (1, 2, 3) if len(b) > n + 1]
            for alt in alts:
                if alt in lignes and alt not in deja:
                    k = alt
                    break
        if not k:
            # Par le nom : d'abord sur la place attendue, puis sur toutes celles
            # qu'on a interrogées (un membre du CAC 40 coté à Amsterdam).
            pre = cle.split("/")[0] if "/" in cle else None
            if pre:
                k = par_le_nom(m.get("nom"), {a: b for a, b in lignes.items()
                                              if a.split("/")[0] == pre}, deja)
            if not k:
                k = par_le_nom(m.get("nom"), lignes, deja)
        if not k:
            absents.append(m.get("nom") or m["cle"])
            continue
        deja.add(k)
        rows.append({"cle": k, "v": lignes[k], "nom": lignes[k].get("name") or m.get("nom"),
                     "sym": symbole_yahoo(k) or k, "officiel": m.get("poids_officiel"),
                     "wiki": m.get("poids_wiki"), "capi_wiki": m.get("capi_wiki")})
    meta = dict(meta or {})
    if reprise:
        meta["reprise_du"] = reprise
    return rows, meta, absents, membres


def bases(rows, capi_seule=False):
    """{id(ligne): capitalisation retenue}, flottante sauf demande contraire.

    ⚠ Deux lignes d'une même société (Alphabet A et C) portent chacune la
    capitalisation de TOUTE la société : on la partage au lieu de la compter deux
    fois. Le flottant manquant ou aberrant (Berkshire B à 0,06 %, qui est celui des
    actions A) prend la médiane de l'indice."""
    ratios = {id(r): ratio_flottant(r["v"]) for r in rows}
    connus = [x for x in ratios.values() if x]
    med = statistics.median(connus) if connus else 1.0
    par_nom = {}
    for r in rows:
        c = r["v"].get("marketCap")
        if estnb(c) and c > 0:
            par_nom.setdefault(_norm(r["nom"])[:14] or r["cle"], []).append(r)
    # La part de la société réellement cotée sur la place, quand la liste la
    # donne (ASX 200) : Newmont y pèse 13,5 Md A$ de titres, la société entière
    # 176. Le rapport des deux capitalisations, à la date de la liste, est un
    # rapport de NOMBRES D'ACTIONS : il ne dérive pas avec les cours.
    tot_wiki = sum(r.get("capi_wiki") or 0 for r in rows)
    tot_scr = sum(r["v"].get("marketCap") or 0 for r in rows if r.get("capi_wiki"))
    echelle = (tot_wiki / tot_scr) if tot_wiki and tot_scr else None
    out = {}
    for grp in par_nom.values():
        for r in grp:
            c = r["v"]["marketCap"]
            # Même nom ET même capitalisation à 5 % près (GOOGL 4,30 T$, GOOG 4,15 T$).
            sosies = [x for x in grp if abs(x["v"]["marketCap"] / c - 1) < 0.05]
            part = 1.0
            if echelle and r.get("capi_wiki"):
                q = r["capi_wiki"] / (c * echelle)
                # Sous 0,5 seulement : un cours qui a gagné 30 % depuis la date de la
                # liste fait descendre le rapport à 0,77 sans que rien ne soit partiel.
                if q < 0.5:
                    part = max(q, 0.01)
                    r["cotation_partielle"] = rd(q, 3)
            out[id(r)] = c / len(sosies) * part * (1.0 if capi_seule else (ratios[id(r)] or med))
    return out


def etf_top(etf):
    """Les 25 premières lignes d'un ETF américain : {chemin: poids %}, date."""
    raw = _get("https://stockanalysis.com/etf/%s/holdings/__data.json" % etf,
               accept="application/json", referer="https://stockanalysis.com/", timeout=40)
    if not raw:
        return None, None
    try:
        d = json.loads(raw)
    except Exception:
        return None, None
    for n in d.get("nodes") or []:
        if not n or n.get("type") != "data":
            continue
        arr = n.get("data") or []

        def res(i, memo={}):
            if not isinstance(i, int) or i < 0 or i >= len(arr):
                return None
            v = arr[i]
            if isinstance(v, dict):
                return {k: res(j) for k, j in v.items()}
            if isinstance(v, list):
                return [res(j) for j in v]
            return v
        tete = arr[0] if arr and isinstance(arr[0], dict) else {}
        if "holdings" not in tete:
            continue
        h = res(tete["holdings"]) or []
        out = {}
        for x in h:
            try:
                out[(x.get("s") or "").lstrip("!$")] = float(str(x.get("as")).rstrip("%"))
            except (TypeError, ValueError):
                pass
        date = res(tete["date"]) if "date" in tete else None
        try:
            date = datetime.strptime(date, "%b %d, %Y").strftime("%Y-%m-%d")
        except Exception:
            pass
        return (out or None), date
    return None, None


# ── LES POIDS EXACTS : le fichier du fonds qui réplique l'indice ────────────
# ⚠ AUDIT DU 30/09/2026 contre les fonds répliquants : l'estimation par le
# flottant se trompait de 0,07 à 2,55 points en moyenne sur le top 10 (DAX :
# Airbus à 0,96 % pour 6,44 % réels — non rapproché dans l'ETF de recalage puis
# plafonné au 25e ; IPC : FEMSA à 14,2 % pour 7,3 %), et la liste Wikipédia
# était PÉRIMÉE presque partout (CAC : Edenred et Teleperformance sorties,
# Eiffage et Euronext absentes ; DAX : Porsche SE au lieu d'Hochtief ; FTSE 100 :
# Balfour Beatty, sorti en 2009 ; IPC : 34 membres au lieu de 35).
# Quand un fichier exact existe, il fait donc la COMPOSITION et les POIDS ;
# Wikipédia n'est plus qu'un repli.
LECTEURS_EXACTS, FONDS_EXACTS = {}, {
    "cac40": "Amundi CAC 40 UCITS ETF", "ibex35": "Amundi IBEX 35 UCITS ETF",
    "dax40": "iShares Core DAX UCITS ETF (DE)", "ftse100": "iShares Core FTSE 100 UCITS ETF",
    "ftsemib": "iShares FTSE MIB UCITS ETF", "smi": "iShares SMI ETF (CH)",
    "tsx": "iShares Core S&P/TSX Capped Composite (XIC)", "ipc": "iShares NAFTRAC",
    "nikkei225": "Nikkei Inc. (facteurs d'ajustement officiels × cours)", "kospi": "liste KIND, capitalisation des actions ordinaires",
    "taiex": "TAIFEX (poids officiels)", "hsi": "Tracker Fund of Hong Kong (2800)",
    "csi300": "iShares Core CSI 300 (2846)", "nifty50": "NSE Indices (poids officiels)", "asx200": "SPDR S&P/ASX 200 (STW)",
}
for _mod in ("poids_exacts_europe", "poids_exacts_asie", "poids_exacts_ameriques"):
    try:
        _m = __import__(_mod)
        LECTEURS_EXACTS.update(getattr(_m, "LECTEURS", None) or getattr(_m, "INDICES", {}) or {})
    except Exception:
        pass
PLACE_MAISON = {"FR": "epa", "DE": "etr", "UK": "lon", "IT": "bit", "ES": "bme", "CH": "swx",
                "CA": "tsx", "MX": "bmv", "AU": "asx", "HK": "hkg", "IN": "nse", "JP": "tyo",
                "KR": "krx", "TW": "tpe"}


def composition_exacte(ix, lignes):
    """(membres, meta) depuis le fichier exact, ou (None, None). Chaque membre est
    retrouvé dans le screener par son ISIN (cotation de la place de l'indice de
    préférence : Airbus a la même ISIN à Paris et à Francfort)."""
    lecteur = LECTEURS_EXACTS.get(ix["code"])
    if not lecteur:
        return None, None
    try:
        date_, xs, url = lecteur()
    except Exception as e:
        log("[warn] %s : fichier exact indisponible (%s) — liste de repli" % (ix["nom"], e))
        return None, None
    xs = [x for x in (xs or []) if estnb(x.get("poids")) and x["poids"] > 0]
    if len(xs) < 10:
        return None, None
    maison = PLACE_MAISON.get(ix["pays"])
    par_isin = {}
    for k, v in lignes.items():
        if v.get("isin"):
            par_isin.setdefault(v["isin"], []).append(k)
    def par_code(code):
        """Le code de place → chemin du screener (Tokyo 6857 → tyo/6857 ;
        Shanghai/Shenzhen selon le premier chiffre ; M&M → nse/M_M)."""
        code = str(code or "").strip()
        if not code:
            return None
        pre = maison
        if ix["code"] == "csi300":
            pre = "sha" if code[:1] in ("6", "9") else "she"
        for v in (code, code.replace("&", "_"), code.replace("-", "_"), code.replace(".", "_"),
                  code.replace("-", "."), code.replace(".", "-"), code.lstrip("0").zfill(4)):
            if pre and "%s/%s" % (pre, v) in lignes:
                return "%s/%s" % (pre, v)
        return None
    membres = []
    for x in xs:
        cands = sorted(par_isin.get(x.get("isin") or "", []),
                       key=lambda k: (0 if maison and k.startswith(maison + "/") else 1, len(k)))
        k = cands[0] if cands else par_code(x.get("code_place") or x.get("ticker"))
        membres.append({"cle": k or (x.get("ticker") or x.get("code_place") or x.get("isin") or ""),
                        "nom": x.get("nom"), "poids_officiel": x["poids"], "isin": x.get("isin")})
    return membres, {"source": "Avoirs du fonds %s, qui réplique physiquement l'indice"
                               % FONDS_EXACTS.get(ix["code"], "répliquant"),
                     "url": url, "date": date_, "poids": "exacts (fonds répliquant, au %s)"
                     % ("/".join(reversed(str(date_)[:10].split("-"))) if re.match(r"^\d{4}-\d{2}-\d{2}", str(date_)) else date_),
                     "exact": True}


def ponderer(ix, rows):
    """Pose r['w'] (somme 1) et rend un dict qui décrit la méthode.

    Quatre étapes, chacune pour un défaut mesuré :
      1. capitalisation flottante (totale pour KOSPI/TAIEX), part cotée sur la place ;
      2. recalage des poids RELATIFS sur un ETF MSCI standard du même pays ;
      3. plafond de l'indice ;
      4. les 25 premiers poids exacts d'un ETF qui réplique l'indice, le reste
         ramené à ce qui reste et plafonné au 25e (un titre hors du top 25 ne
         peut pas peser plus que le 25e).
    """
    officiels = [r for r in rows if estnb(r.get("officiel"))]
    if officiels and len(officiels) >= 0.9 * len(rows):
        tot = sum(r["officiel"] for r in officiels)
        if ix.get("_exact") and estnb(ix.get("_poids_total")) and ix["_poids_total"] >= tot:
            tot = ix["_poids_total"]
        for r in rows:
            r["w"] = (r["officiel"] / tot) if estnb(r.get("officiel")) else 0.0
        # Un membre de la liste absent du fichier exact : le fonds (ou le
        # fournisseur) ne le tient plus — la liste Wikipédia est en retard.
        rows[:] = [r for r in rows if r["w"] > 0]
        return {"libelle": "exacts" if ix.get("_exact") else "officiels"}
    capi_seule = ix["source"] == "place"
    base = bases(rows, capi_seule)
    tot = sum(base.values()) or 1.0
    w = {k: x / tot for k, x in base.items()}
    info = {"libelle": "capitalisation totale" if capi_seule else "capitalisation flottante"}
    partiels = [r["nom"] for r in rows if r.get("cotation_partielle")]
    if partiels:
        info["cotation_partielle"] = partiels[:12]
    par_chemin = {r["cle"]: r for r in rows}

    # 2. recalage sur un ETF MSCI standard
    proxy = ETF_RECALAGE.get(ix["code"])
    if proxy:
        top, date = etf_top(proxy)
        communs = [(par_chemin[k], p) for k, p in (top or {}).items() if k in par_chemin and p > 0]
        if len(communs) >= 8:
            k_med = statistics.median(w[id(r)] * 100 / p for r, p in communs if id(r) in w)
            recales = []
            for r, p in communs:
                avant = w.get(id(r), 0.0)
                w[id(r)] = k_med * p / 100.0
                if avant and abs(w[id(r)] / avant - 1) > 0.15:
                    recales.append(r["nom"])
            tot = sum(w.values())
            w = {k: x / tot for k, x in w.items()}
            info["recalage"] = {"etf": proxy.upper(), "date": date, "titres": len(communs),
                                "corriges": recales[:12]}
    # 3. plafond
    w = plafonner(w, ix.get("plafond"))
    # 4. les 25 premiers poids exacts
    etf = ETF_REPLIQUE.get(ix["code"])
    if etf:
        top, date = etf_top(etf)
        exacts = {id(par_chemin[k]): p / 100.0 for k, p in (top or {}).items() if k in par_chemin and p > 0}
        if len(exacts) >= 15:
            # Contrôle publié : l'estimation seule contre les poids exacts.
            e = [abs(100 * w.get(i, 0) - 100 * p) for i, p in sorted(exacts.items(), key=lambda t: -t[1])[:10]]
            info["controle"] = {"etf": etf.upper(), "ecart_moyen_top10_pts": rd(sum(e) / len(e), 2)}
            reste = max(0.0, 1.0 - sum(exacts.values()))
            plancher = min(exacts.values())
            # ⚠ Un poids exact de l'ETF NON RAPPROCHÉ (Airbus : coté à Paris chez
            # nous, à Francfort dans l'ETF) est un gros membre qui tombe dans « le
            # reste » : le plafonner au 25e l'a mis à 0,96 % pour 6,45 % réels
            # (30/09/2026). Plafond seulement si tout le haut a été rapproché.
            non_rapproches = [k for k, p in (top or {}).items() if p > 0 and k not in par_chemin]
            if non_rapproches:
                plancher = float("inf")
                info["non_rapproches"] = non_rapproches[:10]
            autres = {i: x for i, x in w.items() if i not in exacts}
            ta = sum(autres.values()) or 1.0
            autres = {i: x / ta * reste for i, x in autres.items()}
            # plafonné au 25e poids exact, l'excédent redistribué entre les autres
            for _ in range(50):
                trop = [i for i, x in autres.items() if x > plancher + 1e-12]
                if not trop:
                    break
                exces = sum(autres[i] - plancher for i in trop)
                libres = sum(x for i, x in autres.items() if i not in trop) or 1.0
                autres = {i: (plancher if i in trop else x + exces * x / libres) for i, x in autres.items()}
            w = dict(autres)
            w.update(exacts)
            tot = sum(w.values())
            w = {k: x / tot for k, x in w.items()}
            info["exacts"] = {"etf": etf.upper(), "date": date, "titres": len(exacts),
                              "part": rd(100 * sum(exacts.values()), 1)}
    for r in rows:
        r["w"] = w.get(id(r), 0.0)
    rows[:] = [r for r in rows if r["w"] > 0]
    return info


def ecart_aux_officiels(rows):
    """Sur un indice aux poids officiels, recalcule les poids ESTIMÉS et mesure
    l'écart : la seule preuve publiable que l'estimation vaut quelque chose."""
    base = bases(rows)
    tot = sum(base.values()) or 1.0
    est = {k: x / tot for k, x in base.items()}
    rs = sorted(rows, key=lambda r: -r["w"])
    e10 = [abs(100 * est.get(id(r), 0) - 100 * r["w"]) for r in rs[:10]]
    top10_est = 100 * sum(sorted(est.values(), reverse=True)[:10])
    return {"ecart_moyen_top10_pts": rd(sum(e10) / len(e10), 2) if e10 else None,
            "top10_estime": rd(top10_est, 1),
            "top10_officiel": rd(100 * sum(r["w"] for r in rs[:10]), 1)}


# ── LA NOTE /20 — celle des secteurs, sur les membres de l'indice ───────────
# Même barème, mêmes seuils ABSOLUS, mêmes bornes de plausibilité, même lecture
# de la MÉDIANE des titres : la fonction est celle de fetch_secteurs_mondiaux.py,
# importée et non recopiée — deux copies divergeraient, et un indice noté 12
# ne se comparerait plus à un secteur noté 12. Les valeurs sont celles des
# fragments de la collecte de marché (fetch_marche_actions.py), comme pour les
# secteurs : un membre du CAC 40 a la même marge sur sa fiche, dans son secteur
# et dans son indice.

# Les champs du screener qui portent un autre nom dans la collecte de marché
# (fetch_marche_actions.py, RENOMS) — le barème des secteurs lit ceux-là.
RENOMS_MARCHE = {"revenue3y": "croissance_ca_3a_pct", "revenueGrowth": "croissance_ca_pct",
                 "epsGrowth": "croissance_bpa_pct"}


def charger_marche():
    """Rien à charger : les valeurs viennent du screener de l'indice lui-même.
    (La première idée — relire les fragments `marche_NN.json` — ne couvrait que
    55 % du TAIEX et 27 % de l'IPC : ces places n'y sont presque pas.)"""
    return {"source": "screener"}


def note_indice(code, rows, marche):
    """(groupe agrégé avec `note_fondamentale`, couverture en poids %)."""
    try:
        import fetch_secteurs_mondiaux as fsm
    except Exception as e:
        log("[warn] note : fetch_secteurs_mondiaux introuvable (%s)" % e)
        return None, None
    champs = sorted(set(fsm.GRANDEURS) | {"price", "ma50", "ma200"})
    ix = {c: i for i, c in enumerate(champs)}
    inverse = {v: k for k, v in RENOMS_MARCHE.items()}
    membres = []
    for r in rows:
        v = [r["v"].get(inverse.get(c, c)) for c in champs]
        membres.append((r["sym"], v, r["nom"], r["v"].get("marketCapUsd") or r.get("_usd") or 0.0, 1))
    g = fsm.agreger(membres, ix, code)
    fsm.poser_note_fondamentale(g)
    return g, 100.0


# ── LES CHAMPS D'UNE LIGNE DE FICHE ──────────────────────────────────────────
# p* : variation du titre sur les séances de référence de l'INDICE (cours
# quotidiens) ; p3a/p5a : screener. c* : contribution en points d'indice.
# chYTD/ch1y/ch1m/c_ytd/c_1a : noms de la première version, gardés le temps que
# la page qui les lit soit remplacée.
LIGNE = ["sym", "chemin", "nom", "secteur", "industrie", "pays", "poids", "capi_usd",
         "pe", "pe_fwd", "ps", "pb", "rdt_div", "marge_nette", "roe", "crois_ca",
         "p1s", "p1m", "p3m", "p6m", "pytd", "p1a", "p2a", "p3a", "p5a",
         "ath_ecart", "ath_date", "haut1a_ecart", "mm50", "mm200", "rsi",
         "c1m", "c3m", "c6m", "cytd", "c1a", "c2a", "potentiel",
         "ch1m", "chYTD", "ch1y", "ch3y", "ch5y", "c_ytd", "c_1a"]


def ligne_fiche(r, fx):
    v, p, c = r["v"], r.get("p") or {}, r.get("contrib") or {}
    capi = v.get("marketCap")

    def drap(x):
        return None if x is None else (1 if x else 0)
    return [r["sym"], r["cle"], r["nom"], v.get("sector"), v.get("industry"), v.get("country"),
            rd(100 * r["w"], 4), rd(capi * fx / 1e9, 2) if estnb(capi) and fx else None,
            rd(v.get("peRatio"), 1), rd(v.get("peForward"), 1), rd(v.get("psRatio"), 2),
            rd(v.get("pbRatio"), 2), rd(v.get("dividendYield"), 2), rd(v.get("profitMargin"), 1),
            rd(v.get("roe"), 1), rd(v.get("revenueGrowth"), 1),
            rd(p.get("1s"), 2), rd(p.get("1m"), 2), rd(p.get("3m"), 2), rd(p.get("6m"), 2),
            rd(p.get("ytd"), 2), rd(p.get("1a"), 2), rd(p.get("2a"), 2),
            rd(v.get("ch3y"), 1), rd(v.get("ch5y"), 1),
            rd(v.get("allTimeHighChange"), 1), v.get("allTimeHighDate"), rd(v.get("high52ch"), 1),
            drap(r.get("mm50_drap")), drap(r.get("mm200_drap")), rd(v.get("rsi"), 0),
            c.get("1m"), c.get("3m"), c.get("6m"), c.get("ytd"), c.get("1a"), c.get("2a"),
            rd(v.get("priceTargetChange"), 1),
            rd(p.get("1m", v.get("ch1m")), 1), rd(p.get("ytd", v.get("chYTD")), 1),
            rd(p.get("1a", v.get("ch1y")), 1), rd(v.get("ch3y"), 1), rd(v.get("ch5y"), 1),
            c.get("ytd"), c.get("1a")]


def cours_des_membres(ix, rows, memo):
    """{symbole Yahoo: [(séance, clôture)]} sur trois ans. Indice de rentabilité :
    cours AJUSTÉS des dividendes, titre par titre (le spark n'en donne pas)."""
    syms = [r["sym"] for r in rows if r.get("sym")]
    ajuste = ix["code"] in RENTABILITE
    cle = "adj" if ajuste else "brut"
    manque = [s for s in syms if (cle, s) not in memo]
    echecs = 0
    if ajuste:
        for s in manque:
            memo[(cle, s)] = yahoo_jours(s, "3y", ajuste=True)
    elif manque:
        got, echecs = spark_jours(manque, "3y")
        for s in manque:
            memo[(cle, s)] = got.get(s) or []
    out = {s: memo[(cle, s)] for s in syms if memo.get((cle, s))}
    return out, echecs, ajuste


# La concentration EXACTE, mois par mois, lue dans les avoirs des fonds qui
# répliquent l'indice (construire_histo_indices.py) : chargée une fois par passage.
HISTO_MENSUEL = {}


def histo_mensuel(ix, precedent, conc, rows, methode):
    """La concentration EXACTE, mois par mois. Amorcée par les avoirs mensuels
    passés du fonds répliquant (indices_histo.json), puis relue dans la fiche
    précédente et prolongée à chaque passage : le point du mois courant est
    réécrit avec les poids du jour, et devient celui de fin de mois. Seulement
    avec des poids exacts ou officiels — jamais d'estimation dans cette série."""
    hm = (precedent or {}).get("histo_mensuel")
    base = HISTO_MENSUEL.get(ix["code"])
    if base and (not hm or len(base.get("mois") or []) > len(hm.get("mois") or [])):
        hm = base
    hm = {k: (list(v) if isinstance(v, list) else v) for k, v in (hm or {}).items()}
    if methode not in ("exacts", "officiels"):
        return hm or None
    for k in ("mois", "n", "top1", "top5", "top10", "neff", "hhi", "premier"):
        hm.setdefault(k, [])
    mois = time.strftime("%Y-%m")
    if hm["mois"] and hm["mois"][-1] == mois:
        for k in ("mois", "n", "top1", "top5", "top10", "neff", "hhi", "premier"):
            if hm[k]:
                hm[k].pop()
    premier = max(rows, key=lambda r: r["w"]) if rows else None
    for k, v in (("mois", mois), ("n", conc.get("n")), ("top1", conc.get("top1")), ("top5", conc.get("top5")),
                 ("top10", conc.get("top10")), ("neff", conc.get("n_effectif")), ("hhi", conc.get("hhi")),
                 ("premier", premier["nom"] if premier else None)):
        hm[k].append(v)
    hm.setdefault("source", "Poids %s relevés à chaque passage de la collecte." % methode)
    hm["pas"] = "mensuel"
    return hm


def traiter(ix, places, fx, precedent, memo_cours, marche):
    rows, compo, absents, membres = composer(ix, places, precedent)
    if not rows:
        return None
    methode_info = ponderer(ix, rows)
    methode = methode_info["libelle"]

    n_membres = len(membres)
    # ── GARDE : une composition mal rattachée ne se publie pas ──
    # Moins de 85 % des membres retrouvés, c'est une table Wikipédia qui a changé
    # de forme ou un screener qui a changé ses chemins — pas l'indice.
    if len(rows) < 0.85 * n_membres:
        log("[warn] %s : %d membres rattachés sur %d — sous le seuil, on garde le précédent"
            % (ix["nom"], len(rows), n_membres))
        return None
    niveau = niveau_indice(ix["ticker"], ix["code"])
    egal = niveau_indice(EGAL_OFFICIEL[ix["code"]][0]) if ix["code"] in EGAL_OFFICIEL else None
    cours, lots_ko, ajuste = cours_des_membres(ix, rows, memo_cours)
    series = series_indice(rows, cours, niveau, egal)
    perfs_membres(rows, cours, niveau, cal_fin=series["d"][-1] if series else None)
    # Les drapeaux de moyennes : ceux des cours quotidiens (les mêmes que la
    # courbe d'ampleur), à défaut ceux du screener.
    for r in rows:
        if "mm200_drap" not in r:
            p_, m50, m200 = r["v"].get("price"), r["v"].get("ma50"), r["v"].get("ma200")
            r["mm50_drap"] = (p_ > m50) if estnb(p_) and estnb(m50) and m50 > 0 else None
            r["mm200_drap"] = (p_ > m200) if estnb(p_) and estnb(m200) and m200 > 0 else None
    val = valorisation(rows)
    rent = rentabilite(rows)
    conc = concentration(rows, 1.0 if ix.get("_exact") else None)
    amp = ampleur(rows)
    perf = performance(rows, niveau, series, rentab=ix["code"] in RENTABILITE, egal=egal)
    # La capitalisation est dans la devise PRINCIPALE du cours : Londres cote en
    # pence mais publie sa capitalisation en livres (Shell : 3 568,5 GBX,
    # 206 Md£) — vérifié le 30/09/2026.
    for r in rows:
        dev = r["v"].get("priceCurrency") or ix["devise"]
        dev = {"GBX": "GBP", "GBp": "GBP", "ZAC": "ZAR", "ILA": "ILS"}.get(dev, dev)
        c = r["v"].get("marketCap")
        r["fx"] = fx.get(dev)
        r["_usd"] = c * r["fx"] if estnb(c) and r["fx"] else None
    capi_tot = sum(r["_usd"] for r in rows if estnb(r["_usd"]))
    note, couv_note = note_indice(ix["code"], rows, marche)
    groupes = {"secteurs": repartition(rows, lambda r: r["v"].get("sector")),
               "industries": repartition(rows, lambda r: r["v"].get("industry")),
               "pays": repartition(rows, lambda r: r["v"].get("country"))}
    # Format de la première version (tableaux), gardé pour la page en place.
    secteurs = [[x["nom"], x["poids"], x["n"], x["pe"], x["mm200"],
                 x["contrib"].get("ytd"), x["contrib"].get("1a")] for x in groupes["secteurs"]]
    pays = [[x["nom"], x["poids"], x["n"], x["pe"], x["mm200"],
             x["contrib"].get("ytd"), x["contrib"].get("1a")] for x in groupes["pays"]]
    tt = {}
    for r in rows:
        u = r.get("_usd")
        lib = next((l for b, l in TAILLES if estnb(u) and u >= b), None)
        if lib:
            e = tt.setdefault(lib, [0.0, 0])
            e[0] += r["w"]
            e[1] += 1
    tailles = [[lib, rd(100 * tt[lib][0], 1), tt[lib][1]] for _, lib in TAILLES if lib in tt]
    rs = sorted(rows, key=lambda r: -r["w"])
    top = [[r["sym"], r["nom"], r["v"].get("sector"), rd(100 * r["w"], 2),
            rd((r.get("p") or {}).get("ytd", r["v"].get("chYTD")), 1),
            rd((r.get("p") or {}).get("1a", r["v"].get("ch1y")), 1),
            rd(r["v"].get("allTimeHighChange"), 1), rd(r["v"].get("peRatio"), 1)] for r in rs[:10]]
    niv = {k: v for k, v in (niveau or {}).items() if k not in ("hebdo", "_jours")} or None
    perf_synth = {h: dict({k: v for k, v in d.items() if k not in ("hausses", "baisses", "hist")},
                          indice=d.get("reconstitue"))
                  for h, d in perf.items()}
    medianes = None
    if note:
        medianes = {k[:-7]: note[k] for k in note if k.endswith("_median")}
    synth = {
        "code": ix["code"], "nom": ix["nom"], "ticker": ix["ticker"], "devise": ix["devise"],
        "rentabilite_phrase": RENTABILITE.get(ix["code"]),
        "egal_officiel": ({"ticker": EGAL_OFFICIEL[ix["code"]][0], "nom": EGAL_OFFICIEL[ix["code"]][1]}
                          if egal else None),
        "type": "rentabilité" if ix["code"] in RENTABILITE else "prix",
        "n_membres": n_membres, "n_rattaches": len(rows),
        "couverture_membres": rd(100 * len(rows) / n_membres, 1) if n_membres else None,
        "absents": absents[:30],
        "composition": compo, "poids_methode": methode, "poids_detail": methode_info,
        "plafond": ix.get("plafond"),
        "capi_usd_md": rd(capi_tot / 1e9, 0) if capi_tot else None,
        "valorisation": val, "rentabilite": rent,
        "concentration": {k: v for k, v in conc.items() if k != "courbe"},
        "ampleur": {k: v for k, v in amp.items() if not k.startswith("hist_")},
        "performance": perf_synth,
        "niveau": niv,
        "secteurs": secteurs, "tailles": tailles, "top10": top,
        "note_fondamentale": (note or {}).get("note_fondamentale"),
        "note_couverture": couv_note,
        "medianes": medianes,
        "cours_membres": {"titres": len(cours), "sur": len(rows), "ajustes_dividendes": ajuste,
                          "lots_en_echec": lots_ko},
    }
    if methode == "officiels" and ix["code"] == "sp500":
        synth["controle_estimation"] = ecart_aux_officiels(rows)
    # ── L'HISTORIQUE DES MESURES, accumulé un point par jour ──
    h = dict((precedent or {}).get("histo") or {})
    jour = time.strftime("%Y-%m-%d")
    p_ytd = perf.get("ytd") or {}
    series_h = {"mm200": amp.get("mm200"), "mm50": amp.get("mm50"), "rec5": amp.get("record_5"),
                "repli20": amp.get("repli_20"), "top10": conc.get("top10"), "top1": conc.get("top1"),
                "neff": conc.get("n_effectif"), "pe": val.get("pe"), "pe_fwd": val.get("pe_fwd"),
                "rdt_div": val.get("rdt_div"), "niveau": (niveau or {}).get("dernier"),
                "ecart_ew_ytd": (rd(p_ytd["officiel"] - p_ytd["equipondere"], 2)
                                 if estnb(p_ytd.get("officiel")) and estnb(p_ytd.get("equipondere")) else None)}
    d = list(h.get("d") or [])
    if d and d[-1] == jour:
        for k in list(h):
            if k != "d" and h.get(k):
                h[k] = list(h[k])[:-1]
        d = d[:-1]
    d.append(jour)
    h["d"] = d[-MAX_HISTO:]
    for k, x in series_h.items():
        prev = list(h.get(k) or [])
        prev = prev + [None] * (len(d) - 1 - len(prev))
        h[k] = (prev + [x])[-MAX_HISTO:]
    fiche = {
        "code": ix["code"], "version": 2,
        "genere_le": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "synthese": synth,
        "champs": LIGNE, "lignes": [ligne_fiche(r, r.get("fx")) for r in rs],
        "courbe_concentration": conc.get("courbe"),
        "hist_record": amp.get("hist_record"), "hist_haut1a": amp.get("hist_haut1a"),
        "contributions": {k: {"hausses": v.get("hausses"), "baisses": v.get("baisses")}
                          for k, v in perf.items() if v.get("hausses")},
        "hist_perf_1a": (perf.get("1a") or {}).get("hist"),
        "groupes": groupes,
        "pays": pays,
        "series": series,
        "note": note and {k: note[k] for k in note if k.endswith("_median") or k.endswith("_n")},
        "niveau": dict({k: v for k, v in (niveau or {}).items() if k != "_jours"}) if niveau else None,
        "histo": h,
        "histo_mensuel": histo_mensuel(ix, precedent, conc, rows, methode),
        "composition": compo,
        "membres": [{"cle": m["cle"], "nom": m.get("nom"), "poids_officiel": m.get("poids_officiel")}
                    for m in membres],
    }
    return synth, fiche


def ecrire(nom, obj, js_global=None):
    os.makedirs(OUT_DIR, exist_ok=True)
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    tmp = os.path.join(OUT_DIR, nom + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(s)
    os.replace(tmp, os.path.join(OUT_DIR, nom))
    if js_global:
        nj = nom.replace(".json", ".js")
        tmp = os.path.join(OUT_DIR, nj + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("window.%s=%s;\n" % (js_global, s))
        os.replace(tmp, os.path.join(OUT_DIR, nj))
    return len(s)


def main():
    t0 = time.time()
    seuls = [a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--seul=")]
    choix = [ix for ix in INDICES if not seuls or ix["code"] in seuls[0].split(",")]
    fx = taux_usd()
    log("[info] %d devises" % len(fx))
    pays = sorted({ix["pays"] for ix in choix} | {p for ix in choix for p in PLACES_EN_PLUS.get(ix["pays"], [])})
    places = {}
    for p in pays:
        places[p] = screener(p)
        log("[info] screener %s : %s lignes" % (p, len(places[p]) if places[p] else "MUET"))
    marche = charger_marche()
    HISTO_MENSUEL.update(((lire_json("indices_histo.json") or {}).get("indices")) or {})
    log("[info] concentration exacte mensuelle : %s" % (", ".join(sorted(HISTO_MENSUEL)) or "aucune"))
    memo_cours = {}
    ancien = lire_json("indices_fiches.json") or {}
    anciens = {s.get("code"): s for s in (ancien.get("indices") or [])}
    synths, repris = [], []
    for ix in choix:
        precedent = lire_json("indice_%s.json" % ix["code"])
        res = None
        if places.get(ix["pays"]):
            try:
                res = traiter(ix, [places[ix["pays"]]] + [places.get(p) for p in PLACES_EN_PLUS.get(ix["pays"], [])],
                              fx, precedent, memo_cours, marche)
            except Exception as e:
                import traceback
                traceback.print_exc()
                log("[warn] %s : %s" % (ix["nom"], e))
        if res:
            synth, fiche = res
            taille = ecrire("indice_%s.json" % ix["code"], fiche)
            synths.append(synth)
            p1 = (synth["performance"].get("1a") or {})
            log("[ok] %-13s %4d/%4d membres  cours %4d  top10 %5s %%  P/E %5s  mm200 %5s %%  1 an publié %6s / reconstitué %6s  note %s  %d Ko"
                % (ix["nom"], synth["n_rattaches"], synth["n_membres"], synth["cours_membres"]["titres"],
                   synth["concentration"]["top10"], synth["valorisation"]["pe"],
                   synth["ampleur"]["mm200"], p1.get("officiel"), p1.get("reconstitue"),
                   (synth.get("note_fondamentale") or {}).get("note"), taille // 1024))
        elif ix["code"] in anciens:
            # Un indice qui échoue garde sa dernière synthèse, datée : jamais un trou.
            s = dict(anciens[ix["code"]])
            s["reprise_du"] = s.get("reprise_du") or (ancien.get("genere_le") or "")[:10]
            synths.append(s)
            repris.append(ix["nom"])
        else:
            repris.append(ix["nom"] + " (absent)")
    if seuls and anciens:
        faits = {s["code"] for s in synths}
        synths += [s for c, s in anciens.items() if c not in faits]
    if len([s for s in synths if not s.get("reprise_du")]) < (1 if seuls else 8):
        log("[fatal] trop peu d'indices calculés — rien n'est écrit")
        return 2
    ordre = {ix["code"]: i for i, ix in enumerate(INDICES)}
    synths.sort(key=lambda s: ordre.get(s["code"], 99))
    out = {
        "genere_le": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "duree_s": round(time.time() - t0, 1),
        "repris": repris,
        "version": 2,
        "methode": [
            "Performance : celle de l'indice PUBLIÉ (clôtures quotidiennes), sur les fenêtres du "
            "Comparateur ; sa décomposition titre par titre porte sur les mêmes séances, et l'écart "
            "restant est publié sous son nom.",
            "Composition : avoirs du SPY pour le S&P 500, fichier de Nikkei pour le Nikkei 225, "
            "portefeuille théorique de la B3 pour l'Ibovespa, toutes les actions de la place pour "
            "le KOSPI et le TAIEX, liste Wikipédia pour les douze autres.",
            "Poids : officiels quand la source les donne, sinon capitalisation flottante plafonnée "
            "comme l'indice (capitalisation totale pour le KOSPI et le TAIEX).",
            "Multiples : capitalisation totale / grandeur totale, soit l'inverse de la moyenne "
            "pondérée des rendements (bénéfice/cours) ; une perte compte en négatif.",
            "Contributions : poids de départ × variation, poids de départ = poids actuel / (1 + variation).",
        ],
        "indices": synths,
    }
    ecrire("indices_fiches.json", out, js_global="__INDICES_FICHES__")
    log("[ok] %d indices en %.0f s — repris : %s" % (len(synths), time.time() - t0, ", ".join(repris) or "aucun"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
