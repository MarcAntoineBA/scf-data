#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetcher — Volatilite historique realisee, multi-actifs (~70 actifs, 5 classes).

CE QUE CA MESURE
----------------
La volatilite REALISEE close-to-close, annualisee, exactement la definition
retenue par Coinglass et par la litterature :

    r_t   = ln(P_t / P_{t-1})                     (rendement logarithmique)
    sigma = ecart-type(r) sur la fenetre W        (ddof=1, ecart-type d'echantillon)
    HV    = sigma * sqrt(N)                       (N = observations par an)

N vaut 365 pour le crypto (cote 24/7) et 252 pour tout ce qui est cote en
bourse. La fenetre W est exprimee en JOURS CALENDAIRES (7 / 30 / 90 / 180 / 365)
et convertie en nombre d'observations : round(W * N / 365). Une fenetre "30 j"
vaut donc 30 observations en crypto et 21 seances en TradFi — c'est la meme
duree de marche, ce qui rend les deux directement comparables.

HISTORIQUE MAXIMAL — c'est le point du cahier des charges
---------------------------------------------------------
Chaque actif va chercher la source qui remonte le PLUS LOIN, pas la plus
commode :
  · BTC 2010-07 et 10 autres cryptos via Coinmetrics (Yahoo ne demarre qu'en
    2014-09 pour BTC, 2017-11 pour ETH/XRP/DOGE — trois ans a sept ans perdus) ;
  · S&P 500 depuis 1927-12 et une dizaine d'actions depuis 1962-01 via Yahoo
    avec period1 NEGATIF (period1=0 tronque tout a 1970, epoch Unix oblige) ;
  · WTI 1986-01, Brent 1987-05, gaz Henry Hub 1997-01, USD/JPY 1971-01,
    EUR/USD 1999-01 via FRED (Yahoo ne les a qu'a partir de 2000 / 1996 / 2003).

RACCORD DE SOURCES : ON RACCORDE LES RENDEMENTS, PAS LES NIVEAUX
-----------------------------------------------------------------
Le WTI profond vient de FRED (spot Cushing) et sa queue vivante de Yahoo
(future CL=F) : deux definitions proches mais pas identiques. Coller les deux
NIVEAUX fabriquerait un rendement fantome au point de jonction, qui se
propagerait dans toutes les fenetres pendant un an. On calcule donc les
rendements DANS chaque segment, puis on concatene les rendements en JETANT
celui qui enjambe la jonction. Le raccord ne cree aucun mouvement.

SORTIE — deux fichiers, parce qu'ils n'ont pas le meme usage
-------------------------------------------------------------
  volatility_cache.js    (leger, charge sur toutes les visites)
      window.__VOLATILITY__ = {assets:[...], generated_at, ...}
      Metadonnees, valeurs courantes des 5 fenetres, percentiles, regime,
      bornes de zones, etincelle. Alimente la carte de la grille et le
      classement, sans rien telecharger d'autre.

  volatility_returns.js  (charge A LA DEMANDE, a l'ouverture du panneau)
      window.__VOLATILITY_RET__ = {<cle>: {t0, dd, rs, r:[...]}}
      La serie de RENDEMENTS quotidiens, au jour le jour, sans decimation.

POURQUOI EXPEDIER LES RENDEMENTS ET NON LES COURBES DE VOLATILITE
------------------------------------------------------------------
Premiere version : cinq courbes de volatilite precalculees par actif. Mesure
faite sur cinq actifs : 734 Ko — soit environ 10 Mo pour l'univers entier, et
il fallait deja decimer pour en arriver la. Or les cinq fenetres derivent de la
MEME serie de rendements. En expediant cette serie unique et en calculant les
fenetres dans le navigateur (quelques dizaines de milliers d'operations pour
l'actif affiche, instantane), on divise le poids par cinq ET on supprime toute
decimation : la courbe est tracee au jour le jour, sur la profondeur complete.
Les rendements sont stockes en entiers (unite 1e-5, ou 1e-6 pour le change ou
les mouvements quotidiens sont cent fois plus petits) et les dates en ecarts
successifs — un chiffre par jour dans l'immense majorite des cas.

L'ERREUR D'ARRONDI EST BORNEE ET NEGLIGEABLE : a l'unite 1e-5, le bruit ajoute
a un rendement vaut au pire 5e-6, soit un ecart-type de 2,9e-6, soit 0,006 point
de volatilite annualisee. Face a des volatilites de 10 a 100 %, c'est invisible.

CE QUI EST CALCULE ICI PLUTOT QUE DANS LE NAVIGATEUR
-----------------------------------------------------
Les percentiles, les bornes de zones et les valeurs courantes sont calcules ici,
sur la serie quotidienne complete, et voyagent dans le cache leger. Le
classement et la carte de la grille fonctionnent donc sans telecharger un seul
rendement.
"""
import json
import math
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from curl_cffi import requests as cr
except ImportError:                                       # pragma: no cover
    cr = None

try:
    from _fred_helpers import fetch_fred
except ImportError:                                       # pragma: no cover
    fetch_fred = None

# TCC : launchd ne peut pas ecrire sur ~/Desktop. On ecrit dans Library/Caches,
# snapshot_site.sh recopie vers le depot. Cf project_treasury_launchd_tcc_piege.
_CACHE_DIR = pathlib.Path.home() / "Library" / "Caches" / "site_crypto_finance"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_DESKTOP = pathlib.Path.home() / "Desktop" / "Site_Crypto_Finance"

OUT_LIGHT = "volatility_cache.js"
OUT_RET = "volatility_returns.js"
OUT_DIRS = [_CACHE_DIR] + ([_DESKTOP] if _DESKTOP.is_dir() else [])

WINDOWS = [7, 30, 90, 180, 365]
PRIMARY_W = 30                      # fenetre de reference (carte, classement)
SPARK_PTS = 180                     # etincelle de la carte / du classement
MIN_OBS = 400                       # un actif sans 400 seances n'entre pas
YAHOO_EPOCH = -2208988800           # 1900-01-01 : deverrouille l'avant-1970

# Bornes de zones, en percentiles de la PROPRE histoire de l'actif. Un seuil
# absolu n'a aucun sens ici : 20 % de vol est un calme plat pour le BTC et une
# tempete pour le S&P 500. Les percentiles rendent les regimes comparables
# d'une classe d'actifs a l'autre, et les valeurs absolues correspondantes sont
# affichees a l'ecran pour que le lecteur garde les deux lectures.
ZONE_PCTS = [15, 40, 70, 90]
ZONE_NAMES = ["Torpeur", "Calme", "Regime normal", "Agite", "Panique"]
ZONE_TONES = ["pos", "pos", "eq", "warn", "neg"]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) CapitalAntifragile/1.0"
CM_URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"


def log(m):
    sys.stderr.write("[vol] %s\n" % m)
    sys.stderr.flush()


# ══════════════════════════════════════════════════════════════════════════
#  UNIVERS
# ══════════════════════════════════════════════════════════════════════════
# Chaque actif : cle, nom affiche, classe, sources ordonnees du plus profond au
# plus recent. Une source = (type, identifiant). Types : "cm" (Coinmetrics),
# "yf" (Yahoo), "fred". Le premier segment donne la profondeur, les suivants
# prolongent jusqu'a aujourd'hui (raccord sur les rendements, cf en-tete).
#   logo : ("crypto", cle de _crySvgB64) | ("stock", ticker de stock_logos/)
#          | ("mono", texte du monogramme)
CRYPTO_ANN, TRADFI_ANN = 365, 252

UNIVERSE = [
    # ── Crypto (24/7, annualisation sqrt(365)) ──────────────────────────────
    dict(k="BTC",   name="Bitcoin",        cls="crypto", srcs=[("cm", "btc")],  logo=("crypto", "bitcoin")),
    dict(k="ETH",   name="Ethereum",       cls="crypto", srcs=[("cm", "eth")],  logo=("crypto", "ethereum")),
    dict(k="XRP",   name="XRP",            cls="crypto", srcs=[("cm", "xrp")],  logo=("mono", "XRP")),
    dict(k="LTC",   name="Litecoin",       cls="crypto", srcs=[("cm", "ltc")],  logo=("mono", "LTC")),
    dict(k="DOGE",  name="Dogecoin",       cls="crypto", srcs=[("cm", "doge")], logo=("crypto", "dogecoin")),
    dict(k="XMR",   name="Monero",         cls="crypto", srcs=[("cm", "xmr")],  logo=("crypto", "monero")),
    dict(k="XLM",   name="Stellar",        cls="crypto", srcs=[("cm", "xlm")],  logo=("mono", "XLM")),
    dict(k="BNB",   name="BNB",            cls="crypto", srcs=[("cm", "bnb")],  logo=("crypto", "bnb")),
    dict(k="ADA",   name="Cardano",        cls="crypto", srcs=[("cm", "ada")],  logo=("mono", "ADA")),
    dict(k="LINK",  name="Chainlink",      cls="crypto", srcs=[("cm", "link")], logo=("crypto", "chainlink")),
    dict(k="BCH",   name="Bitcoin Cash",   cls="crypto", srcs=[("cm", "bch")],  logo=("mono", "BCH")),
    dict(k="ETC",   name="Ethereum Classic", cls="crypto", srcs=[("cm", "etc")], logo=("mono", "ETC")),
    dict(k="TRX",   name="TRON",           cls="crypto", srcs=[("cm", "trx")],  logo=("crypto", "tron")),
    dict(k="DOT",   name="Polkadot",       cls="crypto", srcs=[("cm", "dot")],  logo=("mono", "DOT")),
    dict(k="UNI",   name="Uniswap",        cls="crypto", srcs=[("cm", "uni")],  logo=("crypto", "uniswap")),
    dict(k="AAVE",  name="Aave",           cls="crypto", srcs=[("cm", "aave")], logo=("crypto", "aave")),
    dict(k="SOL",   name="Solana",         cls="crypto", srcs=[("yf", "SOL-USD")],       logo=("crypto", "solana")),
    dict(k="AVAX",  name="Avalanche",      cls="crypto", srcs=[("yf", "AVAX-USD")],      logo=("crypto", "avalanche")),
    dict(k="HBAR",  name="Hedera",         cls="crypto", srcs=[("yf", "HBAR-USD")],      logo=("mono", "HBAR")),
    dict(k="ALGO",  name="Algorand",       cls="crypto", srcs=[("cm", "algo")], logo=("mono", "ALGO")),
    dict(k="ZEC",   name="Zcash",          cls="crypto", srcs=[("cm", "zec")],  logo=("mono", "ZEC")),
    # SHIB et PEPE sont ABSENTS a dessein : Yahoo ne rend que deux chiffres
    # significatifs sur les prix micro, 64 % et 72 % de leurs rendements
    # quotidiens sont exactement nuls, et Coinmetrics community ne les sert pas.
    # Une volatilite de memecoin calculee la-dessus serait un chiffre creux ;
    # DOGE (Coinmetrics, prix a resolution normale) tient le role de la categorie.
    dict(k="NEAR",  name="NEAR Protocol",  cls="crypto", srcs=[("yf", "NEAR-USD")],      logo=("crypto", "near")),
    # PIEGE VERIFIE 2026-08-10 — sur Yahoo, le ticker « evident » n'est pas la
    # bonne piece. `SUI-USD` est Salmonation, `TON-USD` est TON Token (0,005 $) :
    # ni Sui ni Toncoin. Le bon ticker porte le suffixe numerique CoinMarketCap.
    # Toncoin (TON11419-USD) n'a que 56 jours d'historique chez Yahoo et n'est pas
    # servi par Coinmetrics community : il est EXCLU plutot que d'etre affiche sur
    # deux mois. Toute addition de crypto doit passer par la verification du
    # `shortName` renvoye par Yahoo — le symbole seul ne prouve rien.
    dict(k="SUI",   name="Sui",            cls="crypto", srcs=[("yf", "SUI20947-USD")],  logo=("crypto", "sui")),
    dict(k="OP",    name="Optimism",       cls="crypto", srcs=[("yf", "OP-USD")],        logo=("crypto", "optimism")),
    dict(k="APT",   name="Aptos",          cls="crypto", srcs=[("yf", "APT21794-USD")],  logo=("crypto", "aptos")),
    dict(k="ARB",   name="Arbitrum",       cls="crypto", srcs=[("yf", "ARB11841-USD")],  logo=("crypto", "arbitrum")),

    # ── Actions (annualisation sqrt(252)) ───────────────────────────────────
    dict(k="NVDA",  name="Nvidia",         cls="action", srcs=[("yf", "NVDA")],  logo=("stock", "NVDA")),
    dict(k="AAPL",  name="Apple",          cls="action", srcs=[("yf", "AAPL")],  logo=("stock", "AAPL")),
    dict(k="MSFT",  name="Microsoft",      cls="action", srcs=[("yf", "MSFT")],  logo=("stock", "MSFT")),
    dict(k="GOOGL", name="Alphabet",       cls="action", srcs=[("yf", "GOOGL")], logo=("stock", "GOOGL")),
    dict(k="AMZN",  name="Amazon",         cls="action", srcs=[("yf", "AMZN")],  logo=("stock", "AMZN")),
    dict(k="META",  name="Meta",           cls="action", srcs=[("yf", "META")],  logo=("stock", "META")),
    dict(k="TSLA",  name="Tesla",          cls="action", srcs=[("yf", "TSLA")],  logo=("stock", "TSLA")),
    dict(k="AVGO",  name="Broadcom",       cls="action", srcs=[("yf", "AVGO")],  logo=("stock", "AVGO")),
    dict(k="ORCL",  name="Oracle",         cls="action", srcs=[("yf", "ORCL")],  logo=("stock", "ORCL")),
    dict(k="AMD",   name="AMD",            cls="action", srcs=[("yf", "AMD")],   logo=("stock", "AMD")),
    dict(k="MSTR",  name="Strategy (MicroStrategy)", cls="action", srcs=[("yf", "MSTR")], logo=("crypto", "microstrategy")),
    dict(k="COIN",  name="Coinbase",       cls="action", srcs=[("yf", "COIN")],  logo=("crypto", "coinbase")),
    dict(k="HOOD",  name="Robinhood",      cls="action", srcs=[("yf", "HOOD")],  logo=("crypto", "robinhood")),
    dict(k="PLTR",  name="Palantir",       cls="action", srcs=[("yf", "PLTR")],  logo=("stock", "PLTR")),
    dict(k="SMCI",  name="Super Micro",    cls="action", srcs=[("yf", "SMCI")],  logo=("mono", "SMCI")),
    dict(k="JPM",   name="JPMorgan",       cls="action", srcs=[("yf", "JPM")],   logo=("stock", "JPM")),
    dict(k="XOM",   name="ExxonMobil",     cls="action", srcs=[("yf", "XOM")],   logo=("stock", "XOM")),
    dict(k="KO",    name="Coca-Cola",      cls="action", srcs=[("yf", "KO")],    logo=("stock", "KO")),
    dict(k="IBM",   name="IBM",            cls="action", srcs=[("yf", "IBM")],   logo=("stock", "IBM")),
    dict(k="BRK-B", name="Berkshire Hathaway", cls="action", srcs=[("yf", "BRK-B")], logo=("stock", "BRK-B")),

    # ── Indices & ETF ───────────────────────────────────────────────────────
    dict(k="SPX",   name="S&P 500",        cls="indice", srcs=[("yf", "^GSPC")], logo=("mono", "SPX")),
    dict(k="NDX",   name="Nasdaq Composite", cls="indice", srcs=[("yf", "^IXIC")], logo=("mono", "NDX")),
    dict(k="RUT",   name="Russell 2000",   cls="indice", srcs=[("yf", "^RUT")],  logo=("mono", "RUT")),
    dict(k="VIX",   name="VIX (vol du S&P)", cls="indice", srcs=[("yf", "^VIX")], logo=("mono", "VIX")),
    dict(k="N225",  name="Nikkei 225",     cls="indice", srcs=[("yf", "^N225")], logo=("mono", "N225")),
    dict(k="HSI",   name="Hang Seng",      cls="indice", srcs=[("yf", "^HSI")],  logo=("mono", "HSI")),
    dict(k="SPY",   name="SPY (ETF S&P 500)", cls="etf", srcs=[("yf", "SPY")],   logo=("mono", "SPY")),
    dict(k="QQQ",   name="QQQ (ETF Nasdaq 100)", cls="etf", srcs=[("yf", "QQQ")], logo=("mono", "QQQ")),
    dict(k="IWM",   name="IWM (ETF small caps)", cls="etf", srcs=[("yf", "IWM")], logo=("mono", "IWM")),
    dict(k="TLT",   name="TLT (Treasuries 20 ans+)", cls="etf", srcs=[("yf", "TLT")], logo=("mono", "TLT")),
    dict(k="HYG",   name="HYG (high yield)", cls="etf", srcs=[("yf", "HYG")],    logo=("mono", "HYG")),
    dict(k="EEM",   name="EEM (marches emergents)", cls="etf", srcs=[("yf", "EEM")], logo=("mono", "EEM")),

    # ── Matieres premieres ──────────────────────────────────────────────────
    dict(k="GOLD",  name="Or",             cls="commo", srcs=[("yf", "GC=F")], logo=("mono", "AU")),
    dict(k="SILVER", name="Argent",        cls="commo", srcs=[("yf", "SI=F")], logo=("mono", "AG")),
    dict(k="COPPER", name="Cuivre",        cls="commo", srcs=[("yf", "HG=F")], logo=("mono", "CU")),
    dict(k="WTI",   name="Petrole WTI",    cls="commo", srcs=[("fred", "DCOILWTICO"), ("yf", "CL=F")], logo=("mono", "WTI")),
    dict(k="BRENT", name="Petrole Brent",  cls="commo", srcs=[("fred", "DCOILBRENTEU"), ("yf", "BZ=F")], logo=("mono", "BRT")),
    dict(k="NATGAS", name="Gaz naturel (Henry Hub)", cls="commo", srcs=[("fred", "DHHNGSP"), ("yf", "NG=F")], logo=("mono", "GAZ")),
    dict(k="WHEAT", name="Ble",            cls="commo", srcs=[("yf", "ZW=F")], logo=("mono", "BLE")),
    dict(k="URA",   name="Uranium (ETF URA)", cls="commo", srcs=[("yf", "URA")], logo=("mono", "U")),

    # ── Devises & taux ──────────────────────────────────────────────────────
    dict(k="DXY",   name="Dollar index",   cls="fx", srcs=[("yf", "DX-Y.NYB")], logo=("mono", "DXY")),
    dict(k="EURUSD", name="EUR / USD",     cls="fx", srcs=[("fred", "DEXUSEU"), ("yf", "EURUSD=X")], logo=("mono", "EUR")),
    dict(k="USDJPY", name="USD / JPY",     cls="fx", srcs=[("fred", "DEXJPUS"), ("yf", "USDJPY=X")], logo=("mono", "JPY")),
    dict(k="USDCNY", name="USD / CNY",     cls="fx", srcs=[("fred", "DEXCHUS"), ("yf", "USDCNY=X")], logo=("mono", "CNY")),
    dict(k="US10Y", name="Taux US 10 ans", cls="fx", srcs=[("yf", "^TNX")], logo=("mono", "10Y")),
]

CLASS_LABEL = {"crypto": "Crypto", "action": "Actions", "indice": "Indices",
               "etf": "ETF", "commo": "Matieres premieres", "fx": "Devises & taux"}
CLASS_ORDER = ["crypto", "action", "indice", "etf", "commo", "fx"]

# Unite du NIVEAU, pour la courbe de cours affichee en fond du graphe. Elle ne
# se deduit pas de la classe d'actif : le Nikkei cote en points et non en
# dollars, le ble cote en CENTS par boisseau chez Yahoo (~550, pas 5,50 $), le
# cuivre en dollars par livre, le gaz en dollars par MMBtu, et le « cours » du
# 10 ans est un rendement en pourcent. Ecrire « $ » partout serait plus simple
# et faux six fois.
PX_UNIT_CLS = {"crypto": "$", "action": "$", "etf": "$", "indice": "pts",
               "commo": "$", "fx": ""}
PX_UNIT = {
    "GOLD": "$/oz", "SILVER": "$/oz", "COPPER": "$/livre",
    "WTI": "$/baril", "BRENT": "$/baril", "NATGAS": "$/MMBtu",
    "WHEAT": "cents/boisseau",
    "DXY": "pts", "US10Y": "%",
}

# Lecture particuliere : ces series ne sont pas des prix d'actifs. La formule est
# identique (variations relatives), le SENS ne l'est pas — on le dit a l'ecran
# plutot que de laisser croire a une volatilite de prix.
CAVEAT = {
    "VIX":   "Volatilite DE la volatilite : variations relatives de l'indice VIX lui-meme.",
    "US10Y": "Volatilite du RENDEMENT : variations relatives du taux 10 ans, pas du prix de l'obligation.",
    "DXY":   "Panier de devises face au dollar — une vol de change, structurellement bien plus basse qu'une vol d'action.",
    "EURUSD": "Vol de change : structurellement la plus basse de l'univers.",
    "USDJPY": "Vol de change.",
    "USDCNY": "Vol de change d'une monnaie a flottement administre : les regimes refletent la politique de la PBoC autant que le marche.",
}


# ══════════════════════════════════════════════════════════════════════════
#  RECUPERATION DES SERIES DE PRIX
# ══════════════════════════════════════════════════════════════════════════
_SESSION = None


def _yf_session():
    global _SESSION
    if _SESSION is None:
        if cr is None:
            raise RuntimeError("curl_cffi absent : Yahoo repond 429 sans empreinte TLS "
                               "(cf project_yahoo_curl_cffi_required)")
        _SESSION = cr.Session(impersonate="chrome120")
    return _SESSION


def fetch_yahoo(ticker, tries=4):
    """Serie quotidienne complete Yahoo. period1 NEGATIF pour l'avant-1970."""
    last = None
    for a in range(tries):
        try:
            r = _yf_session().get(
                YF_URL.format(urllib.parse.quote(ticker)),
                params={"period1": YAHOO_EPOCH, "period2": int(time.time()),
                        "interval": "1d", "includeAdjustedClose": "true"},
                timeout=45)
            if r.status_code != 200:
                raise RuntimeError("HTTP %s" % r.status_code)
            res = r.json()["chart"]["result"][0]
            ts = res["timestamp"]
            quote = res["indicators"]["quote"][0]
            closes = quote.get("close") or []
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
            # Les actions se decoupent et detachent des dividendes : sur le cours
            # brut, un split 4:1 est un rendement de -75 % qui gonflerait la vol
            # d'un facteur enorme pendant toute la fenetre. On prend donc le cours
            # AJUSTE quand Yahoo le fournit.
            vals = adj if adj and any(v is not None for v in adj) else closes
            out = {}
            for t, v in zip(ts, vals):
                if v is None or v <= 0:
                    continue
                out[datetime.fromtimestamp(t, timezone.utc).date().isoformat()] = float(v)
            if len(out) < 30:
                raise RuntimeError("serie trop courte (%d)" % len(out))
            return out
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(1.5 * (a + 1))
    raise RuntimeError("Yahoo %s : %s" % (ticker, last))


def fetch_coinmetrics(asset, tries=3):
    """Serie PriceUSD quotidienne complete, paginee."""
    last = None
    for a in range(tries):
        try:
            out, params = {}, {
                "assets": asset, "metrics": "PriceUSD", "frequency": "1d",
                "page_size": 10000, "start_time": "2009-01-01",
            }
            url = CM_URL + "?" + urllib.parse.urlencode(params)
            for _ in range(40):                      # garde-fou pagination
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    js = json.loads(resp.read().decode("utf-8"))
                for row in js.get("data", []):
                    v = row.get("PriceUSD")
                    if v is None:
                        continue
                    fv = float(v)
                    if fv > 0:
                        out[row["time"][:10]] = fv
                nxt = js.get("next_page_url")
                if not nxt:
                    break
                url = nxt
            if len(out) < 30:
                raise RuntimeError("serie trop courte (%d)" % len(out))
            return out
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(2 * (a + 1))
    raise RuntimeError("Coinmetrics %s : %s" % (asset, last))


def fetch_fred_series(series_id):
    if fetch_fred is None:
        raise RuntimeError("_fred_helpers indisponible")
    obs = fetch_fred(series_id)
    if not obs or not obs.get("dates"):
        raise RuntimeError("FRED %s : reponse vide" % series_id)
    out = {}
    for d, v in zip(obs["dates"], obs["values"]):
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv > 0:
            out[d] = fv
    if len(out) < 30:
        raise RuntimeError("FRED %s : serie trop courte" % series_id)
    return out


SRC_LABEL = {
    "cm": ("Coinmetrics (community API, PriceUSD)", "https://coinmetrics.io/community-network-data/"),
    "yf": ("Yahoo Finance (cours ajuste des splits et dividendes)", "https://finance.yahoo.com/quote/{}"),
    "fred": ("FRED — Federal Reserve Bank of St. Louis", "https://fred.stlouisfed.org/series/{}"),
}


def fetch_segment(kind, ident):
    if kind == "cm":
        return fetch_coinmetrics(ident)
    if kind == "yf":
        return fetch_yahoo(ident)
    if kind == "fred":
        return fetch_fred_series(ident)
    raise ValueError("source inconnue : %s" % kind)


# ══════════════════════════════════════════════════════════════════════════
#  RENDEMENTS, RACCORD, VOLATILITE
# ══════════════════════════════════════════════════════════════════════════
MAX_GAP = 10          # jours calendaires enjambes par un rendement, au maximum
MAX_TICK = 0.005      # pas de cotation tolere, en fraction du prix

# Seuil de RUPTURE DE SERIE. Premier reglage a x4 : il a coupe 2 562 points de
# DOGE (le x4,6 du 28/01/2021, la ruee WSB — parfaitement reel) et 6 790 points
# de gaz Henry Hub (le x4,4 du 16/01/2024, tempete Heather — reel aussi). Un x4
# quotidien EXISTE sur ces marches. Un x20, non : le seul cas rencontre dans
# l'univers est OP-USD, x2001, qui est une erreur de source caracterisee. Le
# seuil est donc place la ou aucun marche reel ne va.
MAX_JUMP = math.log(20)


def price_grid_ok(prices):
    """La serie a-t-elle assez de resolution pour qu'un rendement quotidien
    veuille dire quelque chose ?

    PIEGE MESURE (2026-08-10) : Yahoo stocke les cours en flottant simple et ne
    rend que DEUX chiffres significatifs pour les pieces a prix micro. PEPE cote
    3,0e-6 : le pas de cotation vaut 0,1e-6, soit 3,3 % du prix. Resultat, 72 %
    des rendements quotidiens sont EXACTEMENT nuls (64 % pour SHIB) et la
    volatilite calculee dessus tombe a 11,8 % — pour un memecoin. Ce n'est pas
    une volatilite basse, c'est une absence de donnee, et rien a l'ecran ne
    l'aurait signale.

    LE CRITERE EST LE PAS DE COTATION, PAS LES JOURS IMMOBILES. Premiere version :
    « plus de 25 % de rendements nuls → rejet ». Elle a rejete USD/CNY, dont un
    tiers des seances ne bouge pas — non par manque de resolution (FRED donne
    quatre decimales sur un cours de 7, soit 0,0014 %) mais parce que la PBoC
    administre le change. La donnee y est bonne et la volatilite tres basse est
    la reponse juste. Seul le pas rapporte au prix distingue une grille trop
    grossiere d'un marche reellement immobile. La part de jours inchanges reste
    calculee et remonte dans les metadonnees, pour etre affichee, pas pour juger.

    Renvoie (True, part de jours inchanges) ou (False, motif)."""
    vals = [prices[d] for d in sorted(prices)]
    diffs = sorted(abs(vals[i] - vals[i - 1]) for i in range(1, len(vals)))
    nz = [x for x in diffs if x > 0]
    if not nz:
        return False, "serie constante"
    med_price = sorted(vals)[len(vals) // 2]
    tick = nz[max(0, len(nz) // 100)]          # 1er centile des ecarts non nuls
    if med_price > 0 and tick / med_price > MAX_TICK:
        return False, ("pas de cotation trop grossier : %.3g pour un prix median de "
                       "%.3g (%.1f %% du prix)" % (tick, med_price, 100 * tick / med_price))
    return True, sum(1 for x in diffs if x == 0) / float(len(diffs))


def trim_broken_prefix(prices, label=""):
    """Coupe tout ce qui precede la derniere rupture impossible.

    PIEGE MESURE (2026-08-10) : Yahoo sert pour OP-USD un cours fige a 0,000425 $
    jusqu'au 06/10/2022, puis 0,85 $ le lendemain — un facteur 2000 en un jour.
    Le prefixe est faux, le reste est bon. Un x4 quotidien n'existe sur aucun des
    actifs de cet univers (le pire jour du BTC, Mt.Gox 2013, vaut -66 %) : on
    considere donc qu'un tel saut marque une rupture de serie et on ne garde que
    ce qui suit. Couper le prefixe plutot que rejeter l'actif preserve la partie
    exploitable ; l'operation est journalisee, jamais silencieuse."""
    ds = sorted(prices)
    cut = None
    for i in range(1, len(ds)):
        p0, p1 = prices[ds[i - 1]], prices[ds[i]]
        if p0 > 0 and p1 > 0 and abs(math.log(p1 / p0)) > MAX_JUMP:
            cut = i
    if cut is None:
        return prices
    log("  ~ %s : rupture x%.0f le %s → %d points anterieurs ecartes"
        % (label, math.exp(abs(math.log(prices[ds[cut]] / prices[ds[cut - 1]]))),
           ds[cut], cut))
    return {d: prices[d] for d in ds[cut:]}


def returns_from_prices(prices):
    """[(date, r)] a partir de {date: prix}. Rendements logarithmiques.

    Un rendement qui enjambe plus de MAX_GAP jours est ECARTE. Ce n'est pas un
    rendement quotidien : c'est le rattrapage d'un trou dans la serie (source
    interrompue, place fermee longtemps). Le garder reviendrait a injecter le
    mouvement de trois mois dans une seule observation, ce qui ferait exploser
    toutes les fenetres qui la contiennent pendant un an."""
    ds = sorted(prices)
    out = []
    for i in range(1, len(ds)):
        p0, p1 = prices[ds[i - 1]], prices[ds[i]]
        if p0 <= 0 or p1 <= 0:
            continue
        if to_epoch_day(ds[i]) - to_epoch_day(ds[i - 1]) > MAX_GAP:
            continue
        out.append((ds[i], math.log(p1 / p0)))
    return out


def splice_returns(segments):
    """Concatene des segments ordonnes du plus profond au plus recent.

    Le point cle : on raccorde des RENDEMENTS, jamais des NIVEAUX. Le WTI profond
    vient de FRED (spot Cushing), sa queue vivante de Yahoo (future CL=F) — deux
    definitions proches mais pas egales. Coller les niveaux fabriquerait a la
    jonction un rendement qui ne correspond a aucun mouvement de marche, et ce
    faux mouvement resterait dans la fenetre 365 jours pendant un an entier.
    Chaque rendement etant calcule DANS un seul segment, aucun n'enjambe la
    couture, et il n'y a donc rien a jeter au raccord : le segment suivant
    reprend simplement a la premiere date que le precedent ne couvrait pas."""
    all_ret, cut = [], None
    for prices in segments:
        rets = returns_from_prices(prices)
        all_ret.extend(rets if cut is None else [(d, r) for d, r in rets if d > cut])
        if rets:
            cut = rets[-1][0] if cut is None else max(cut, rets[-1][0])
    all_ret.sort(key=lambda x: x[0])
    dedup, seen = [], set()
    for d, r in all_ret:
        if d in seen:
            continue
        seen.add(d)
        dedup.append((d, r))
    return dedup


def rolling_vol(rets, n_obs, ann_sqrt):
    """Ecart-type glissant (ddof=1) annualise, en POURCENTS. Somme et somme des
    carres glissantes : O(N) au lieu de O(N*W), ce qui compte quand ^GSPC pese
    24 000 points et qu'on calcule cinq fenetres pour soixante-dix actifs."""
    n = len(rets)
    out = [None] * n
    if n_obs < 2 or n < n_obs:
        return out
    s = sum(r for _, r in rets[:n_obs])
    s2 = sum(r * r for _, r in rets[:n_obs])
    for i in range(n_obs - 1, n):
        if i >= n_obs:
            add = rets[i][1]
            drop = rets[i - n_obs][1]
            s += add - drop
            s2 += add * add - drop * drop
        var = (s2 - s * s / n_obs) / (n_obs - 1)
        out[i] = math.sqrt(var) * ann_sqrt * 100.0 if var > 0 else 0.0
    return out


def percentile(sorted_vals, p):
    """Percentile par interpolation lineaire sur une liste DEJA triee."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * p / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def rank_percentile(sorted_vals, x):
    """Rang de x dans la distribution, en percentile (recherche dichotomique)."""
    if not sorted_vals or x is None:
        return None
    lo, hi = 0, len(sorted_vals)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_vals[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return round(100.0 * lo / len(sorted_vals), 1)


EPOCH = date(1970, 1, 1)
B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def to_epoch_day(iso):
    y, m, d = (int(x) for x in iso.split("-"))
    return (date(y, m, d) - EPOCH).days


def encode_deltas(deltas):
    """Ecarts entre observations successives, un caractere par ecart.

    Un ecart vaut 1 en semaine et 3 apres un week-end : en base 36, cela tient
    sur un seul caractere pour tout ce qui va jusqu'a 35 jours, contre 2 a 4
    caracteres en JSON avec les virgules. Les rares trous plus longs (fermeture
    de place, serie interrompue) sont echappes entre tildes, ce qui garde le
    codage exact — un ecart tronque decalerait toutes les dates suivantes."""
    out = []
    for d in deltas:
        out.append(B36[d] if 1 <= d <= 35 else "~%d~" % d)
    return "".join(out)


# ══════════════════════════════════════════════════════════════════════════
#  CONSTRUCTION
# ══════════════════════════════════════════════════════════════════════════
def build_asset(spec):
    ann = CRYPTO_ANN if spec["cls"] == "crypto" else TRADFI_ANN
    ann_sqrt = math.sqrt(ann)

    segments, used = [], []
    for kind, ident in spec["srcs"]:
        try:
            seg = fetch_segment(kind, ident)
        except Exception as e:                       # noqa: BLE001
            log("  ! %s : segment %s/%s echoue — %s" % (spec["k"], kind, ident, e))
            continue
        seg = trim_broken_prefix(seg, "%s/%s" % (spec["k"], ident))
        ok, info = price_grid_ok(seg)
        if not ok:
            raise RuntimeError("resolution de cotation insuffisante (%s/%s) — %s"
                               % (kind, ident, info))
        segments.append(seg)
        lbl, tmpl = SRC_LABEL[kind]
        used.append({"kind": kind, "id": ident, "label": lbl,
                     "url": tmpl.format(urllib.parse.quote(ident)) if "{}" in tmpl else tmpl,
                     "start": min(seg), "end": max(seg), "n": len(seg),
                     "flat": round(100 * info, 1)})
    if not segments:
        raise RuntimeError("aucune source disponible")

    rets = splice_returns(segments)
    if len(rets) < MIN_OBS:
        raise RuntimeError("historique insuffisant (%d rendements)" % len(rets))

    cur, pct, zones, dist_n, n_win = {}, {}, {}, {}, {}
    prim_vals, prim_series = None, None
    for w in WINDOWS:
        n_obs = max(2, int(round(w * ann / 365.0)))
        n_win[w] = n_obs
        vals = rolling_vol(rets, n_obs, ann_sqrt)
        clean = sorted(v for v in vals if v is not None)
        dist_n[w] = len(clean)
        if not clean:
            continue
        last = next((v for v in reversed(vals) if v is not None), None)
        cur[w] = round(last, 2) if last is not None else None
        pct[w] = rank_percentile(clean, last)
        zones[w] = [round(percentile(clean, p), 2) for p in ZONE_PCTS]
        if w == PRIMARY_W:
            prim_series = [(rets[i][0], v) for i, v in enumerate(vals) if v is not None]
            prim_vals = [v for _, v in prim_series]

    if PRIMARY_W not in cur or not prim_vals:
        raise RuntimeError("fenetre de reference indisponible")

    # Variation de la fenetre de reference sur les 30 dernieres observations
    chg = round(prim_vals[-1] - prim_vals[-31], 1) if len(prim_vals) > 31 else None
    spark = [round(v, 1) for v in prim_vals[-SPARK_PTS:]]

    # Reperes d'audit affiches dans le panneau. Ils existent pour que le lecteur
    # puisse reconnaitre lui-meme une aberration de source : une vol a 108 % est
    # soit un vrai krach de la valeur (IBM, -29,0 % le 14/07/2026, verifie), soit
    # une operation sur titre non ajustee. Montrer la plus forte variation
    # quotidienne et sa date, c'est donner de quoi trancher sans nous croire.
    dworst, rworst = max(rets, key=lambda x: abs(x[1]))
    dpeak, vpeak = max(prim_series, key=lambda x: x[1])
    dlow, vlow = min(prim_series, key=lambda x: x[1])

    zn = zones[PRIMARY_W]
    v0 = cur[PRIMARY_W]
    zi = sum(1 for t in zn if v0 >= t)

    # ── Ancre de niveau : UN nombre, pas une deuxieme serie ────────────────
    # Le panneau affiche le cours en fond de la courbe de volatilite. Le cache
    # ne transporte pourtant aucun prix : le navigateur reconstruit le niveau
    # en remontant depuis le dernier connu, P(i-1) = P(i) * exp(-r(i)). Une
    # deuxieme serie doublerait le poids du fichier lourd (2,8 Mo) ET pourrait
    # diverger de la premiere ; un seul flottant ne le peut pas. L'erreur de
    # reconstruction est bornee par l'arrondi des rendements : a l'unite 1e-5,
    # elle vaut 3e-6 par pas, soit 0,05 % cumule sur les 24 766 seances du
    # S&P 500 — sous l'epaisseur du trait.
    #
    # Ce cours est RETROPOLE, au sens d'un contrat continu de futures : la ou
    # deux sources se raccordent (WTI = FRED spot jusqu'en 2000 puis CL=F),
    # l'ecart de NIVEAU entre elles n'est pas reinjecte puisqu'on ne raccorde
    # que des rendements — le passe est donc exprime au niveau de la source
    # vivante. On MESURE cet ecart ici pour que le panneau le dise, plutot que
    # de laisser croire que le WTI de 1986 est celui qu'affichait l'ecran ce
    # jour-la. Les rendements ecartes (trou > MAX_GAP, seance a prix negatif)
    # entrent dans le meme ecart, ce qui est exactement le but : un seul
    # chiffre resume tout ce qui separe la reconstruction du cours d'epoque.
    px_last = None
    for seg in reversed(segments):
        if rets[-1][0] in seg:
            px_last = seg[rets[-1][0]]
            break
    px_drift = None
    if px_last:
        back = px_last * math.exp(-sum(r for _, r in rets[1:]))
        real0 = next((seg[rets[0][0]] for seg in segments if rets[0][0] in seg), None)
        if real0:
            px_drift = round(100.0 * (back / real0 - 1.0), 1)

    # D'OU vient l'ecart, quand il y en a un. Deux causes possibles, et il ne
    # faut pas les confondre a l'ecran : le raccord de sources, et les
    # rendements ECARTES parce qu'ils enjambaient plus de MAX_GAP jours. La
    # mesure a valu la peine — l'ecart de 16,6 % du S&P 500 ne vient PAS d'un
    # raccord (il n'a qu'une source) mais d'un seul rendement ecarte : la
    # fermeture du NYSE du 3 au 15 mars 1933, le bank holiday de Roosevelt, a
    # la reouverture duquel l'indice a fait le plus fort bond de son histoire.
    # De meme le Nikkei (Golden Week de 2019, dix jours feries d'affilee) et
    # Avalanche (71 jours absents de Yahoo en 2020).
    skipped = []
    for seg in segments:
        sds = sorted(seg)
        for i in range(1, len(sds)):
            gap = to_epoch_day(sds[i]) - to_epoch_day(sds[i - 1])
            if gap > MAX_GAP:
                skipped.append((sds[i - 1], sds[i], gap))
    big = max(skipped, key=lambda x: x[2]) if skipped else None

    meta_px = {"last": float("%.8g" % px_last) if px_last else None,
               "d": rets[-1][0],
               "unit": PX_UNIT.get(spec["k"], PX_UNIT_CLS.get(spec["cls"], "")),
               "drift": px_drift,
               "spliced": len(segments) > 1,
               "ngap": len(skipped),
               "gap": ({"a": big[0], "b": big[1], "n": big[2]} if big else None)}

    meta = {
        "k": spec["k"], "name": spec["name"], "cls": spec["cls"],
        "logo": {"type": spec["logo"][0], "key": spec["logo"][1]},
        "ann": ann, "n_win": n_win,
        "start": rets[0][0], "end": rets[-1][0], "n_obs": len(rets) + 1,
        "sources": used,
        "cur": cur, "pct": pct, "zones": zones, "dist_n": dist_n,
        "chg30": chg, "spark": spark,
        "regime": ZONE_NAMES[zi], "tone": ZONE_TONES[zi], "zi": zi,
        "caveat": CAVEAT.get(spec["k"]),
        "px": meta_px,
        "worst": {"d": dworst, "r": round(rworst * 100, 2)},
        "peak": {"d": dpeak, "v": round(vpeak, 1)},
        "low": {"d": dlow, "v": round(vlow, 1)},
    }

    # ── Serie de rendements, compactee ─────────────────────────────────────
    # rs : unite du pas entier. 1e-6 pour le change, dont les variations
    # quotidiennes sont deux ordres de grandeur plus petites que celles d'une
    # action — a 1e-5 l'arrondi y pesserait 1 % du signal.
    rs = 1000000 if spec["cls"] == "fx" else 100000
    days = [to_epoch_day(d) for d, _ in rets]
    deltas = [days[i] - days[i - 1] for i in range(1, len(days))]
    ret_pack = {
        "t0": days[0], "rs": rs,
        "dd": encode_deltas(deltas),
        "r": [int(round(r * rs)) for _, r in rets],
    }
    return meta, ret_pack


def js_dump(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def write_out(name, content):
    for d in OUT_DIRS:
        p = d / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, p)                            # ecriture atomique
        log("ecrit %s (%.1f Ko)" % (p, len(content) / 1024.0))


def main():
    only = None
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))

    t0 = time.time()
    metas, rets_out, failed = [], {}, []
    todo = [s for s in UNIVERSE if only is None or s["k"] in only]
    for i, spec in enumerate(todo, 1):
        try:
            m, h = build_asset(spec)
            metas.append(m)
            rets_out[spec["k"]] = h
            log("%2d/%d %-7s %s → %s  %5d obs  vol%dj=%.1f%%  p%s  %s"
                % (i, len(todo), spec["k"], m["start"], m["end"], m["n_obs"],
                   PRIMARY_W, m["cur"][PRIMARY_W], m["pct"][PRIMARY_W], m["regime"]))
        except Exception as e:                        # noqa: BLE001
            failed.append({"k": spec["k"], "err": str(e)[:160]})
            log("%2d/%d %-7s ECHEC — %s" % (i, len(todo), spec["k"], str(e)[:120]))
        time.sleep(0.15)

    if not metas:
        log("ABANDON : aucun actif construit, caches precedents conserves")
        return 1

    order = {c: i for i, c in enumerate(CLASS_ORDER)}
    metas.sort(key=lambda m: (order.get(m["cls"], 9), -(m["cur"].get(PRIMARY_W) or 0)))

    light = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "windows": WINDOWS, "primary": PRIMARY_W,
        "zone_pcts": ZONE_PCTS, "zone_names": ZONE_NAMES, "zone_tones": ZONE_TONES,
        "class_label": CLASS_LABEL, "class_order": CLASS_ORDER,
        "n_assets": len(metas), "failed": failed,
        "assets": metas,
        "methodology": ("Volatilite realisee close-to-close : ecart-type (ddof=1) des rendements "
                        "logarithmiques quotidiens sur la fenetre, annualise par sqrt(365) pour le "
                        "crypto et sqrt(252) pour les actifs cotes en bourse. Les fenetres sont "
                        "exprimees en jours calendaires et converties en observations. Percentiles, "
                        "zones et valeurs courantes calcules sur l'historique quotidien COMPLET, "
                        "sans decimation : le navigateur recoit la serie de rendements et recalcule "
                        "les fenetres au jour le jour."),
    }
    write_out(OUT_LIGHT, "window.__VOLATILITY__=" + js_dump(light) + ";\n")
    write_out(OUT_RET, "window.__VOLATILITY_RET__=" + js_dump(rets_out) + ";\n")
    log("%d actifs, %d echecs, %.0f s" % (len(metas), len(failed), time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
