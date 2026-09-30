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
import csv
import difflib
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
from datetime import datetime, timezone

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
name exchange country priceCurrency sector industry price marketCap float
floatPercent sharesOut high52 low52 ma50 ma200 rsi beta allTimeHigh
allTimeHighChange allTimeHighDate high52ch low52ch peRatio peForward psRatio
pbRatio evEbitda earningsYield fcfYield dividendYield buybackYield grossMargin
operatingMargin profitMargin roe roic revenueGrowth epsGrowth revenueThisYear
debtEquity debtEbitda ch1m ch3m ch6m chYTD ch1y ch3y ch5y analystRatings
priceTargetChange
""".split()

HORIZONS = [("1m", "ch1m"), ("3m", "ch3m"), ("6m", "ch6m"), ("ytd", "chYTD"),
            ("1a", "ch1y"), ("3a", "ch3y"), ("5a", "ch5y")]
HORIZONS_CONTRIB = ("1m", "3m", "ytd", "1a")

MAX_HISTO = 1000

_last = [0.0]


def log(*a):
    print(*a, file=sys.stderr)


def _get(url, ua=UA, accept="*/*", referer=None, debit=0.35, essais=3, timeout=120):
    for essai in range(essais):
        d = time.time() - _last[0]
        if d < debit:
            time.sleep(debit - d)
        _last[0] = time.time()
        h = {"User-Agent": ua, "Accept-Encoding": "gzip", "Accept": accept}
        if referer:
            h["Referer"] = referer
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
            if t and t != "-" and w > 0:
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


# ── L'INDICE LUI-MÊME : son historique, son record ──────────────────────────

def yahoo_serie(ticker, intervalle, periode):
    u = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s"
         % (urllib.parse.quote(ticker), periode, intervalle))
    raw = _get(u, accept="application/json", timeout=40)
    if not raw:
        return []
    try:
        r = json.loads(raw)["chart"]["result"][0]
        ts = r["timestamp"]
        cl = r["indicators"]["quote"][0]["close"]
        return [(t, c) for t, c in zip(ts, cl) if estnb(c)]
    except Exception:
        return []


def niveau_indice(ticker):
    """Semaines sur dix ans pour le graphe, jours sur dix ans + mois depuis
    l'origine pour le record. Un record se lit sur des CLÔTURES : un plus haut en
    séance n'est pas un niveau où l'indice a terminé une journée."""
    jours = yahoo_serie(ticker, "1d", "10y")
    mois = yahoo_serie(ticker, "1mo", "max")
    if len(jours) < 200:
        return None
    rec_v, rec_t = max((c, t) for t, c in jours)
    debut10 = jours[0][0]
    for t, c in mois:
        if t < debut10 and c > rec_v:
            rec_v, rec_t = c, t
    dernier_t, dernier = jours[-1]
    # Semaines : la dernière clôture de chaque semaine calendaire.
    sem = {}
    for t, c in jours:
        sem[int(t // (7 * 86400))] = (t, c)
    hebdo = [[int(t // 86400), rd(c, 2)] for t, c in sorted(sem.values())]
    hebdo[-1] = [int(dernier_t // 86400), rd(dernier, 2)]
    # Pire repli depuis le record, sur dix ans (jours).
    pic, pire, pire_t = -1e18, 0.0, None
    for t, c in jours:
        pic = max(pic, c)
        dd = c / pic - 1
        if dd < pire:
            pire, pire_t = dd, t
    un_an = [c for t, c in jours if t >= dernier_t - 365 * 86400]
    return {
        "dernier": rd(dernier, 2),
        "date": datetime.fromtimestamp(dernier_t, timezone.utc).strftime("%Y-%m-%d"),
        "record": rd(rec_v, 2),
        "record_date": datetime.fromtimestamp(rec_t, timezone.utc).strftime("%Y-%m-%d"),
        "ecart_record_pct": rd(100 * (dernier / rec_v - 1), 2),
        "plus_haut_1a": rd(max(un_an), 2) if un_an else None,
        "pire_repli_10a_pct": rd(100 * pire, 1),
        "pire_repli_10a_date": (datetime.fromtimestamp(pire_t, timezone.utc).strftime("%Y-%m-%d")
                                if pire_t else None),
        "hebdo": hebdo,
    }


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


def concentration(rows):
    ws = sorted((r["w"] for r in rows), reverse=True)
    tot = sum(ws) or 1.0
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
        def f(r):
            p, m = r["v"].get("price"), r["v"].get(champ)
            return (p > m) if estnb(p) and estnb(m) and m > 0 else None
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


def performance(rows, niveau):
    """Par horizon : indice reconstitué, équipondéré, médiane, participation,
    contributions. Mesuré sur les membres ACTUELS : un titre entré en cours de
    période compte pour toute la période (biais du survivant, dit à l'écran)."""
    out = {}
    for h, champ in HORIZONS:
        pts = [(r, r["v"].get(champ) / 100.0) for r in rows
               if estnb(r["v"].get(champ)) and r["v"][champ] > -99.9]
        if len(pts) < max(5, 0.6 * len(rows)):
            continue
        w0 = {id(r): r["w"] / (1 + x) for r, x in pts}
        tot0 = sum(w0.values())
        R = sum(w0[id(r)] / tot0 * x for r, x in pts)
        xs = [x for _, x in pts]
        ew = sum(xs) / len(xs)
        d = {
            "indice": rd(100 * R, 2), "equipondere": rd(100 * ew, 2),
            "mediane": rd(100 * statistics.median(xs), 2),
            "positifs": rd(100 * sum(1 for x in xs if x > 0) / len(xs), 1),
            "battent": rd(100 * sum(1 for x in xs if x > R) / len(xs), 1),
            "couverture": rd(100 * sum(r["w"] for r, _ in pts) / (sum(r["w"] for r in rows) or 1), 1),
        }
        if h in HORIZONS_CONTRIB:
            c = sorted(((r, 100 * w0[id(r)] / tot0 * x) for r, x in pts), key=lambda t: -t[1])
            tot_c = 100 * R
            # Combien de titres font la moitié du mouvement de l'indice, dans son sens.
            sens = c if tot_c >= 0 else list(reversed(c))
            acc, n50 = 0.0, None
            for i, (_, ci) in enumerate(sens):
                acc += ci
                if tot_c != 0 and acc / tot_c >= 0.5:
                    n50 = i + 1
                    break
            gros = sorted(rows, key=lambda r: -r["w"])[:10]
            ids10 = {id(r) for r in gros}
            c10 = sum(ci for r, ci in c if id(r) in ids10)
            # Quand l'indice a peu bougé (moins d'un point), « la moitié de son
            # mouvement » ne veut plus rien dire : 0,08 % de hausse sur un mois donnait
            # « 1 titre fait la moitié » et « les dix premiers font 1 859 % ».
            petit = abs(tot_c) < 1.0
            d["n_moitie"] = None if petit else n50
            # La mesure qui a toujours un sens : sur l'ensemble des GAINS de la
            # période (les titres en hausse seulement), combien de titres en font la moitié.
            gains = [ci for _, ci in c if ci > 0]
            g_tot = sum(gains)
            acc, ng = 0.0, None
            for i, ci in enumerate(gains):
                acc += ci
                if g_tot > 0 and acc >= 0.5 * g_tot:
                    ng = i + 1
                    break
            d["n_moitie_gains"] = ng
            d["gains_bruts"] = rd(g_tot, 2)
            d["pertes_brutes"] = rd(sum(ci for _, ci in c if ci < 0), 2)
            d["contrib_top10"] = rd(c10, 2)
            d["part_top10"] = (rd(100 * c10 / tot_c, 0)
                               if not petit and c10 * tot_c > 0 else None)
            d["hausses"] = [[r["sym"], r["nom"], rd(ci, 3), rd(100 * x, 1)]
                            for r, ci in c[:12] for x in [r["v"][champ] / 100.0]]
            d["baisses"] = [[r["sym"], r["nom"], rd(ci, 3), rd(100 * x, 1)]
                            for r, ci in list(reversed(c))[:12] for x in [r["v"][champ] / 100.0]]
            for r, ci in c:
                r.setdefault("contrib", {})[h] = rd(ci, 3)
        if h == "1a":
            d["hist"] = seaux([(100 * x, r["w"]) for r, x in pts], BORNES_PERF)
            q = quantiles([100 * x for x in xs], [0.1, 0.25, 0.5, 0.75, 0.9])
            d["quantiles"] = [rd(v, 1) for v in q]
            d["ecart_type"] = rd(100 * statistics.pstdev(xs), 1)
        out[h] = d
    # Écart entre l'indice reconstitué et l'indice réel, sur un an : il mesure
    # ce que la reconstitution ne voit pas (entrées/sorties, dividendes, poids).
    if niveau and niveau.get("hebdo") and "1a" in out:
        hb = niveau["hebdo"]
        fin = hb[-1][0]
        debut = next((c for j, c in reversed(hb) if j <= fin - 365), None)
        if debut:
            reel = 100 * (hb[-1][1] / debut - 1)
            out["1a"]["reel"] = rd(reel, 2)
    return out


def repartition(rows, cle, fx=None):
    g = {}
    for r in rows:
        k = cle(r) or "Non classé"
        e = g.setdefault(k, {"w": 0.0, "n": 0, "ey": 0.0, "wey": 0.0, "mm200": 0, "mm200_n": 0,
                             "c_ytd": 0.0, "c_1a": 0.0})
        e["w"] += r["w"]
        e["n"] += 1
        x = r["v"].get("earningsYield")
        if estnb(x):
            e["ey"] += r["w"] * x
            e["wey"] += r["w"]
        p, m = r["v"].get("price"), r["v"].get("ma200")
        if estnb(p) and estnb(m) and m > 0:
            e["mm200_n"] += 1
            e["mm200"] += 1 if p > m else 0
        e["c_ytd"] += (r.get("contrib") or {}).get("ytd") or 0.0
        e["c_1a"] += (r.get("contrib") or {}).get("1a") or 0.0
    tot = sum(e["w"] for e in g.values()) or 1.0
    out = []
    for k, e in sorted(g.items(), key=lambda t: -t[1]["w"]):
        out.append([k, rd(100 * e["w"] / tot, 2), e["n"],
                    rd(100.0 / (e["ey"] / e["wey"]), 1) if e["wey"] and e["ey"] > 0 else None,
                    rd(100 * e["mm200"] / e["mm200_n"], 0) if e["mm200_n"] else None,
                    rd(e["c_ytd"], 2), rd(e["c_1a"], 2)])
    return out


TAILLES = [(200e9, "géantes (> 200 Md$)"), (50e9, "très grandes (50-200 Md$)"),
           (10e9, "grandes (10-50 Md$)"), (2e9, "moyennes (2-10 Md$)"), (0, "petites (< 2 Md$)")]


# ── LES CHAMPS D'UNE LIGNE DE FICHE ──────────────────────────────────────────
LIGNE = ["sym", "chemin", "nom", "secteur", "industrie", "pays", "poids", "capi_usd",
         "pe", "pe_fwd", "ps", "pb", "rdt_div", "marge_nette", "roe", "crois_ca",
         "ch1m", "chYTD", "ch1y", "ch3y", "ch5y", "ath_ecart", "ath_date", "haut1a_ecart",
         "mm50", "mm200", "rsi", "c_ytd", "c_1a", "potentiel"]


def ligne_fiche(r, fx):
    v = r["v"]
    p = v.get("price")

    def dessus(m):
        m = v.get(m)
        return (1 if p > m else 0) if estnb(p) and estnb(m) and m > 0 else None
    capi = v.get("marketCap")
    return [r["sym"], r["cle"], r["nom"], v.get("sector"), v.get("industry"), v.get("country"),
            rd(100 * r["w"], 4), rd(capi * fx / 1e9, 2) if estnb(capi) and fx else None,
            rd(v.get("peRatio"), 1), rd(v.get("peForward"), 1), rd(v.get("psRatio"), 2),
            rd(v.get("pbRatio"), 2), rd(v.get("dividendYield"), 2), rd(v.get("profitMargin"), 1),
            rd(v.get("roe"), 1), rd(v.get("revenueGrowth"), 1),
            rd(v.get("ch1m"), 1), rd(v.get("chYTD"), 1), rd(v.get("ch1y"), 1),
            rd(v.get("ch3y"), 1), rd(v.get("ch5y"), 1),
            rd(v.get("allTimeHighChange"), 1), v.get("allTimeHighDate"), rd(v.get("high52ch"), 1),
            dessus("ma50"), dessus("ma200"), rd(v.get("rsi"), 0),
            (r.get("contrib") or {}).get("ytd"), (r.get("contrib") or {}).get("1a"),
            rd(v.get("priceTargetChange"), 1)]


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
        if not k and src == "wiki":
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
        for r in rows:
            r["w"] = (r["officiel"] / tot) if estnb(r.get("officiel")) else 0.0
        rows[:] = [r for r in rows if r["w"] > 0]
        return {"libelle": "officiels"}
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


def traiter(ix, places, fx, precedent):
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
    niveau = niveau_indice(ix["ticker"])
    val = valorisation(rows)
    rent = rentabilite(rows)
    conc = concentration(rows)
    amp = ampleur(rows)
    perf = performance(rows, niveau)
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
    secteurs = repartition(rows, lambda r: r["v"].get("sector"))
    pays = repartition(rows, lambda r: r["v"].get("country"))
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
            rd(r["v"].get("chYTD"), 1), rd(r["v"].get("ch1y"), 1),
            rd(r["v"].get("allTimeHighChange"), 1), rd(r["v"].get("peRatio"), 1)] for r in rs[:10]]
    synth = {
        "code": ix["code"], "nom": ix["nom"], "ticker": ix["ticker"], "devise": ix["devise"],
        "n_membres": n_membres, "n_rattaches": len(rows),
        "couverture_membres": rd(100 * len(rows) / n_membres, 1) if n_membres else None,
        "absents": absents[:30],
        "composition": compo, "poids_methode": methode, "poids_detail": methode_info,
        "plafond": ix.get("plafond"),
        "capi_usd_md": rd(capi_tot / 1e9, 0) if capi_tot else None,
        "valorisation": val, "rentabilite": rent, "concentration": {k: v for k, v in conc.items() if k != "courbe"},
        "ampleur": {k: v for k, v in amp.items() if not k.startswith("hist_")},
        "performance": {h: {k: v for k, v in d.items() if k not in ("hausses", "baisses", "hist")}
                        for h, d in perf.items()},
        "niveau": {k: v for k, v in (niveau or {}).items() if k != "hebdo"} or None,
        "secteurs": secteurs, "tailles": tailles, "top10": top,
    }
    if methode == "officiels" and ix["code"] == "sp500":
        synth["controle_estimation"] = ecart_aux_officiels(rows)
    # ── L'HISTORIQUE DES MESURES, accumulé un point par jour ──
    h = dict((precedent or {}).get("histo") or {})
    jour = time.strftime("%Y-%m-%d")
    series = {"mm200": amp.get("mm200"), "mm50": amp.get("mm50"), "rec5": amp.get("record_5"),
              "repli20": amp.get("repli_20"), "top10": conc.get("top10"),
              "neff": conc.get("n_effectif"), "pe": val.get("pe"),
              "ecart_ew_ytd": rd(((perf.get("ytd") or {}).get("indice") or 0) - ((perf.get("ytd") or {}).get("equipondere") or 0), 2)
              if perf.get("ytd") else None}
    d = list(h.get("d") or [])
    if d and d[-1] == jour:
        for k in series:
            if h.get(k):
                h[k] = list(h[k])[:-1]
        d = d[:-1]
    d.append(jour)
    h["d"] = d[-MAX_HISTO:]
    for k, x in series.items():
        prev = list(h.get(k) or [])
        prev = prev + [None] * (len(d) - 1 - len(prev))
        h[k] = (prev + [x])[-MAX_HISTO:]
    fiche = {
        "code": ix["code"], "genere_le": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "synthese": synth,
        "champs": LIGNE, "lignes": [ligne_fiche(r, r.get("fx")) for r in rs],
        "courbe_concentration": conc.get("courbe"),
        "hist_record": amp.get("hist_record"), "hist_haut1a": amp.get("hist_haut1a"),
        "contributions": {k: {"hausses": v.get("hausses"), "baisses": v.get("baisses")}
                          for k, v in perf.items() if v.get("hausses")},
        "hist_perf_1a": (perf.get("1a") or {}).get("hist"),
        "pays": pays,
        "niveau": niveau,
        "histo": h,
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
    ancien = lire_json("indices_fiches.json") or {}
    anciens = {s.get("code"): s for s in (ancien.get("indices") or [])}
    synths, repris = [], []
    for ix in choix:
        precedent = lire_json("indice_%s.json" % ix["code"])
        res = None
        if places.get(ix["pays"]):
            try:
                res = traiter(ix, [places[ix["pays"]]] + [places.get(p) for p in PLACES_EN_PLUS.get(ix["pays"], [])],
                              fx, precedent)
            except Exception as e:
                import traceback
                traceback.print_exc()
                log("[warn] %s : %s" % (ix["nom"], e))
        if res:
            synth, fiche = res
            taille = ecrire("indice_%s.json" % ix["code"], fiche)
            synths.append(synth)
            log("[ok] %-13s %4d/%4d membres  poids %-25s  top10 %5s %%  P/E %5s  mm200 %5s %%  %d Ko"
                % (ix["nom"], synth["n_rattaches"], synth["n_membres"], synth["poids_methode"],
                   synth["concentration"]["top10"], synth["valorisation"]["pe"],
                   synth["ampleur"]["mm200"], taille // 1024))
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
        "methode": [
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
