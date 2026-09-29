#!/usr/bin/env python3
"""Où va l'argent — performance comparée de 13 classes d'actifs (widget Accueil).

Classes & sources :
  Yahoo spark v8 (range 2y daily, 1 requête batch, retry x3, fallback yfinance) :
    dxy   DX-Y.NYB   Dollar index
    spx   ^GSPC      Actions US (S&P 500)
    ndx   ^IXIC      Tech US (Nasdaq Composite)
    eu    VGK        Actions Europe (FTSE Developed Europe, en USD)
    cn    MCHI       Actions Chine (MSCI China, en USD)
    gold  GC=F       Or (futures)
    silver SI=F      Argent (futures)
    oil   BZ=F       Pétrole Brent (cohérent onglet Corrélations)
    copper HG=F      Cuivre (futures)
    bonds TLT        Obligations US 20 ans+ (ETF, en USD)
  CoinGecko /coins/markets top 250 (1 requête) :
    btc   Bitcoin (24h/7d/30d/1y upstream ; YTD via crypto_ytd_cache)
    alts  Agrégat mcap-weighted hors BTC, hors stablecoins, hors wrapped/staked
          pct_w = (Σmcap_now − Σmcap_then) / Σmcap_then, mcap_then = mcap/(1+p/100)
          (YTD : mcap_then = mcap × base_jan1/prix ; base = crypto_ytd_cache.json)
  DefiLlama stablecoincharts/all (1 requête) :
    stables  Masse totale stablecoins (variation de supply = vrais flux entrants)

Matières premières (Brent, or, argent, cuivre) : 24h → 30d GLISSANTS sur le perpétuel
Hyperliquid (bourse HIP-3 « xyz », coté 7 j/7, sans marche au changement d'échéance) —
cf HL_COMMODITIES ; repli = SEUL contrat Yahoo en tête (FUT_ROOTS) ; 90d → 1y sur la
cotation continue Yahoo. Incidents : 27/09 Brent −8,59 % (couture nov./déc.), 29/09
Brent −6,45 % (BZ=F saute d'un contrat à l'autre d'heure en heure).

Horodatages : TOUJOURS en heure de Paris (`asof`, `updated`) + epoch (`asof_ts`,
`updated_ts`). Le collecteur tourne aussi dans le nuage, en UTC : « 08:00 » y valait
10:00 à Paris, et la page en déduisait « PÉRIMÉ 3H » pour une ligne d'une heure.

Fenêtres : 24h · 7d · 14d · 30d · 90d · 180d · ytd · 1y — calculées SERVEUR-side,
réfs incluses dans le cache (auditable : chaque % reconstruit depuis px_last / ref).
Plafond crypto = 1 an (CoinGecko gratuit) ; toutes les fenêtres restent ≤ 1 an.
90d/180d crypto : CoinGecko /markets ne fournit pas ces fenêtres → calculées depuis
la série quotidienne (btc = live px / série ; alts = panier proxy, ±1pt cf courbe).

Sorties (~/Library/Caches/site_crypto_finance/) :
  moneyflow_cache.json + moneyflow_cache.js (window.__MONEYFLOW__)
Résilience : merge-preserve par classe (une classe ratée garde ses valeurs
précédentes, flag stale + asof) ; jamais d'écrasement par du vide ; écriture atomique.
Lancé par launchd scf.moneyflow (30 min) + watchdog_freshness.
Charge légère (3 requêtes HTTP) → cadence 30 min sans risque de rate-limit.

REPLI CRYPTO SANS COINGECKO (2026-07-25, cf. bloc CRYPTO_YF plus bas) : btc & alts
ne dépendent plus d'une source unique. Si CoinGecko renvoie 429 (rafales fréquentes :
IP partagée avec les autres fetchers du site), les deux classes sont recalculées
depuis Yahoo (même endpoint spark que les 11 autres classes, sans clé ni quota).
"""
import json, sys, time, urllib.parse, warnings
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

warnings.filterwarnings("ignore")

PARIS = ZoneInfo("Europe/Paris")

def stamp():
    """Horodatage affiché, en heure de PARIS quelle que soit la machine (le Mac est
    à Paris, le runner du nuage en UTC : sans ce fuseau explicite, les deux
    producteurs écrivaient deux heures différentes pour le même instant)."""
    return datetime.now(PARIS).strftime("%Y-%m-%d %H:%M")

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "moneyflow_cache.json"
CACHE_JS   = CACHE_DIR / "moneyflow_cache.js"
YTD_CACHE  = CACHE_DIR / "crypto_ytd_cache.json"
# Séries quotidiennes pour le mode COURBE (fichier séparé, lazy-load côté client
# → n'alourdit pas l'Accueil par défaut). window.__MONEYFLOW_SERIES__.
SERIES_FILE = CACHE_DIR / "moneyflow_series.json"
SERIES_JS   = CACHE_DIR / "moneyflow_series.js"
SERIES_DAYS = 366                # ~1 an de daily conservé par classe

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
WINDOWS = ["24h", "7d", "14d", "30d", "90d", "180d", "ytd", "1y"]
WIN_DAYS = {"7d": 7, "14d": 14, "30d": 30, "90d": 90, "180d": 180, "1y": 365}
# Fenêtres crypto absentes de CoinGecko /markets → calculées depuis la série daily.
CRYPTO_LONG_DAYS = {"90d": 90, "180d": 180}

# ── Définition des classes (ordre d'affichage par défaut, le JS re-trie) ────
YAHOO_CLASSES = [
    # id, ticker, label, icône, source affichée
    ("dxy",    "DX-Y.NYB", "Dollar · DXY",            "💵", "Yahoo DX-Y.NYB"),
    ("spx",    "^GSPC",    "Actions US · S&P 500",    "🇺🇸", "Yahoo ^GSPC"),
    ("ndx",    "^IXIC",    "Tech US · Nasdaq",        "💻", "Yahoo ^IXIC"),
    ("eu",     "VGK",      "Actions Europe · FTSE Europe", "🇪🇺", "Yahoo VGK (FTSE Developed Europe, USD — marché large ≈ STOXX 600)"),
    ("cn",     "ASHR",     "Actions Chine · CSI 300",  "🇨🇳", "Yahoo ASHR (CSI 300 A-shares, USD)"),
    ("cntech", "KWEB",     "Tech Chine · KWEB",        "🐉", "Yahoo KWEB (KraneShares CSI China Internet, USD — géantes tech/internet chinoises, ≈ Nasdaq CN)"),
    ("gold",   "GC=F",     "Or",                      "🥇", "Yahoo GC=F futures"),
    ("silver", "SI=F",     "Argent",                  "🥈", "Yahoo SI=F futures"),
    ("oil",    "BZ=F",     "Pétrole · Brent",         "🛢️", "Yahoo BZ=F futures"),
    ("copper", "HG=F",     "Cuivre",                  "🟠", "Yahoo HG=F futures"),
    ("bonds",  "TLT",      "Obligations US · 20a+",   "🏛️", "Yahoo TLT"),
]
BTC_META    = ("btc",     "Bitcoin",                        "₿",  "CoinGecko")
ALTS_META   = ("alts",    "Altcoins · hors BTC & stables",  "🌈", "CoinGecko top 250 · pondéré mcap")
STABLE_META = ("stables", "Stablecoins · masse totale",     "🪙", "DefiLlama (supply = vrais flux)")

# ── REPLI CRYPTO 100 % YAHOO (anti-panne CoinGecko) ─────────────────────────
# INCIDENT 2026-07-25 : CoinGecko a renvoyé 429 sur les 3 tentatives → btc & alts
# figés sur les valeurs du run précédent pendant 24 h, pendant que les 12 autres
# classes étaient fraîches (et l'horodatage global du widget affichait « maj 22:29 »,
# donc l'écart était invisible). Cause : source UNIQUE pour ces 2 classes.
# FIX : mêmes classes recalculables depuis Yahoo spark (aucune clé, aucun quota,
# déjà la source des 11 autres classes) :
#   · btc  = BTC-USD, séries daily 2 ans → toutes les fenêtres via series_windows()
#   · alts = indice Σ supply_i × prix_i, supplies FIGÉES au dernier run CoinGecko OK
#            (moneyflow_fallback.json) → panier de 19 alts ≈ 77 % de la mcap alts.
# Tickers validés le 2026-07-25 contre CoinGecko (écart de prix < 0,25 %). Les
# symboles absents de Yahoo (SUI, POL, TON) sont simplement hors panier.
# 20 symboles max par requête spark (au-delà : HTTP 400) → BTC + 19 alts = 1 lot.
YF_BTC = "BTC-USD"
CRYPTO_YF_ALTS = {          # symbole CoinGecko (majuscules) → ticker Yahoo
    "ETH": "ETH-USD",   "BNB": "BNB-USD",   "XRP": "XRP-USD",   "SOL": "SOL-USD",
    "TRX": "TRX-USD",   "HYPE": "HYPE32196-USD", "DOGE": "DOGE-USD", "XMR": "XMR-USD",
    "LINK": "LINK-USD", "ADA": "ADA-USD",   "XLM": "XLM-USD",   "BCH": "BCH-USD",
    "LTC": "LTC-USD",   "HBAR": "HBAR-USD", "AVAX": "AVAX-USD", "SHIB": "SHIB-USD",
    "CRO": "CRO-USD",   "UNI": "UNI7083-USD", "NEAR": "NEAR-USD",
}
FALLBACK_FILE = CACHE_DIR / "moneyflow_fallback.json"   # supplies + couverture (serveur only)
PRICE_SANITY_PCT = 25.0     # écart max toléré Yahoo vs CoinGecko avant de jeter un ticker (cf price_sanity_pct)

# ── Exclusions agrégat altcoins ─────────────────────────────────────────────
# Stables connus (complété par la bande de peg ci-dessous)
STABLE_IDS = {
    "tether", "usd-coin", "dai", "ethena-usde", "usds", "first-digital-usd",
    "paypal-usd", "true-usd", "usdd", "frax", "pax-dollar", "gemini-dollar",
    "usd1-wlfi", "usdt0", "tether-eurt", "stasis-eurs", "usual-usd",
    "global-dollar", "agora-dollar", "mountain-protocol-usdm", "resolv-usr",
    "falcon-usd", "ondo-us-dollar-yield", "openeden-opendollar",
    "blackrock-usd-institutional-digital-liquidity-fund",
    # yield-bearing (prix dérive > bande de peg)
    "ethena-staked-usde", "susds", "savings-dai", "staked-frax-ether",
}
# Or tokenisé (ni altcoin, ni stable USD : réplique l'or, fausserait l'agrégat)
GOLD_TOKEN_IDS = {"pax-gold", "tether-gold"}
# Wrapped / staked / bridged = doublons d'exposition (BTC, ETH, SOL…)
WRAP_IDS = {"weth", "wbnb", "tbtc", "solv-btc", "rocket-pool-eth", "clbtc"}
WRAP_PATTERNS = ("wrapped", "staked", "restaked", "bridged", "-peg-", "binance-peg")
WRAP_SYMBOLS = {"weth", "wbnb", "wbtc", "tbtc", "reth", "cbeth", "meth", "oseth", "cbbtc", "lbtc"}

def log(msg):
    sys.stderr.write(f"[MoneyFlow] {msg}\n"); sys.stderr.flush()

def _sig(v, digits=6):
    """Arrondi à `digits` chiffres significatifs (garde les micro-prix vivants)."""
    if v is None or v != v or v == 0:
        return v
    from math import floor, log10
    return round(v, -int(floor(log10(abs(v)))) + (digits - 1))

def wait_for_network(host="query1.finance.yahoo.com", max_wait=120):
    """Anti-course « réveil du Mac » : launchd (StartInterval + RunAtLoad) se rejoue
    DÈS le réveil, AVANT que le Wi-Fi/DNS soit reconnecté. Sans cette garde, chaque
    requête lève NameResolutionError, aucune classe n'est fraîche → le cache précédent
    est conservé et le widget affiche « PÉRIMÉ » jusqu'à ce qu'un run tombe enfin sur
    un réseau vivant (incident 2026-07-21 : moneyflow figé 43h). On attend donc que le
    DNS résolve avant de lancer les fetchs. Si toujours down après max_wait, on continue
    (les retries par-source jouent leur rôle, le prochain tick 30 min réessaiera)."""
    import socket
    waited, delay = 0, 3
    while waited < max_wait:
        try:
            socket.getaddrinfo(host, 443)
            if waited:
                log(f"réseau prêt après {waited}s — fetch lancé sur réseau vivant")
            return True
        except socket.gaierror:
            time.sleep(delay)
            waited += delay
            delay = min(delay * 1.5, 15)
    log(f"WARN réseau/DNS indisponible après {waited}s — fetch tenté quand même (retries par-source)")
    return False

# ── Yahoo ───────────────────────────────────────────────────────────────────
def fetch_yahoo_spark(tickers, rng="2y", interval="1d", min_pts=50):
    """1 requête batch → {ticker: [[ts, close], …]} (pattern fetch_stock_bubble).
    interval='1h' + rng='1mo' → série HORAIRE (mode courbe fenêtres courtes)."""
    out = {}
    sy = urllib.parse.quote(",".join(tickers), safe=",")
    url = f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={sy}&range={rng}&interval={interval}"
    for k in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                for s, obj in r.json().items():
                    if not obj:
                        continue
                    ts = obj.get("timestamp") or []
                    cl = obj.get("close") or []
                    # Arrondi en CHIFFRES SIGNIFICATIFS, pas en décimales : round(x, 4)
                    # écrasait à 0.0 tout actif sous 0,0001 $ (SHIB à 4,9e-06 sortait
                    # du panier de repli, PEPE idem) → prix nul = série morte.
                    pts = [[int(t), _sig(float(c))] for t, c in zip(ts, cl) if c is not None]
                    if len(pts) > min_pts:
                        out[s] = pts
                break
            log(f"spark HTTP {r.status_code} retry {k+1}/3")
        except Exception as e:
            log(f"spark err {e} retry {k+1}/3")
        time.sleep(2 * (k + 1))
    return out

def fetch_yfinance_single(ticker):
    """Fallback par ticker si absent du spark batch."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="2y", interval="1d", auto_adjust=False)
        if hist is None or len(hist) < 50:
            return []
        return [[int(idx.timestamp()), round(float(row["Close"]), 4)]
                for idx, row in hist.iterrows() if row["Close"] == row["Close"]]
    except Exception as e:
        log(f"yfinance {ticker} err: {e}")
        return []

# ── Contrats à terme : fenêtres courtes mesurées sur UN SEUL contrat ─────────
# INCIDENT 2026-09-27 : le widget affichait « Pétrole · Brent −8,59 % sur 24H »
# un dimanche où le Brent avait fait −2,8 %. Cause : BZ=F n'est pas un prix mais
# une COUTURE de contrats. Le vendredi 25/09 Yahoo l'a fait passer du contrat
# novembre (clôture 104,32 $) au contrat décembre (97,44 $) — et la clôture de
# jeudi venait encore de novembre (106,60 $). Le « −8,59 % » comparait deux
# contrats différents : c'était l'écart entre novembre et décembre (le marché
# est en fort déport), pas un mouvement du pétrole. Même défaut le même jour sur
# l'argent (+2,1 % affiché, +1,2 % réel) et le cuivre (+0,7 % affiché, −0,4 %).
#
# RÈGLE (une variation compare toujours deux prix d'échéances aussi proches que
# possible) :
#  · 24H → 30J : on suit le contrat en tête AUJOURD'HUI sur toute la fenêtre.
#    Écart d'échéance = la fenêtre elle-même (≤ 1 mois) ; la couture pèserait
#    plus que le mouvement réel.
#  · 3M → 1AN : prix du contrat en tête à chaque date (convention des graphiques
#    de cours et des médias). Suivre le contrat de décembre sur 6 mois comparerait
#    un contrat à 2 mois d'échéance à lui-même 6 mois plus tôt : Brent +15 % au
#    lieu de −13 % depuis le pic de mars, contredit par tout graphique du Brent.
# Le contrat en tête est IDENTIFIÉ, pas deviné : c'est celui dont la dernière
# clôture est identique à celle de la cotation continue (même barre).
FUT_ROOTS = {           # cotation continue Yahoo → (racine, bourse)
    "BZ=F": ("BZ", "NYM"),
    "GC=F": ("GC", "CMX"),
    "SI=F": ("SI", "CMX"),
    "HG=F": ("HG", "CMX"),
}
CONTRACT_WINDOWS = ("24h", "7d", "14d", "30d")
MONTH_CODES = "FGHJKMNQUVXZ"
MONTHS_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
             "juil.", "août", "sept.", "oct.", "nov.", "déc."]
CONTRACT_MATCH_TOL = 0.001   # 0,1 % : deux échéances voisines s'écartent bien plus
ROLL_FLAG_PT = 0.3           # écart 24H continu vs contrat au-delà duquel on logue

def contract_candidates(root, ex, now=None, n=7):
    """Échéances des `n` prochains mois civils (mois courant inclus) :
    BZZ26.NYM, GCZ26.CMX… Couvre le contrat en tête de chaque racine (Brent :
    m+2/m+3, argent : jusqu'à m+4 en fin d'année)."""
    now = now or datetime.now(timezone.utc)
    out = []
    for k in range(n):
        m0 = now.month - 1 + k
        m, y = m0 % 12, now.year + m0 // 12
        out.append((f"{root}{MONTH_CODES[m]}{y % 100:02d}.{ex}", m, y))
    return out

def match_front_contract(cont_pts, cand_series):
    """Parmi les échéances candidates, celle que la cotation continue suit sur sa
    DERNIÈRE barre : même horodatage, même clôture (tolérance 0,1 %). Renvoie
    None si aucune ne colle ou si deux collent (ambiguïté → on ne tranche pas)."""
    if not cont_pts:
        return None
    lt, lp = cont_pts[-1]
    if not lp:
        return None
    hits = []
    for sym, pts in cand_series.items():
        if not pts or pts[-1][0] != lt or not pts[-1][1]:
            continue
        d = abs(pts[-1][1] / lp - 1)
        if d < CONTRACT_MATCH_TOL:
            hits.append((d, sym))
    if len(hits) != 1:
        return None
    return hits[0][1]

def resolve_front_contracts(cont_series):
    """{ticker continu: {"symbol", "mois", "series"}} pour chaque racine de
    FUT_ROOTS dont le contrat en tête a été identifié. 2 requêtes spark (daily
    2 ans, 14 symboles par lot)."""
    cands, meta = [], {}
    for cont, (root, ex) in FUT_ROOTS.items():
        for sym, m, y in contract_candidates(root, ex):
            cands.append(sym)
            meta[sym] = (cont, f"{MONTHS_FR[m]} {y % 100:02d}")
    got = {}
    for i in range(0, len(cands), 14):
        got.update(fetch_yahoo_spark(cands[i:i + 14], rng="2y", min_pts=5))
    out = {}
    for cont in FUT_ROOTS:
        pool = {s: got.get(s) for s in cands if meta[s][0] == cont}
        sym = match_front_contract(cont_series.get(cont, []), pool)
        if sym:
            out[cont] = {"symbol": sym, "mois": meta[sym][1], "series": got[sym]}
        else:
            log(f"WARN contrat en tête de {cont} NON identifié → fenêtres courtes "
                f"sur la cotation continue (risque de couture de contrats)")
    return out

def apply_contract_windows(pct, refs, contract_pts):
    """Remplace les fenêtres courtes (CONTRACT_WINDOWS) par celles calculées sur
    le seul contrat en tête. Renvoie l'écart 24H (continu − contrat), en points."""
    cpct, crefs = series_windows(contract_pts)
    if not cpct:
        return None
    gap = None
    if pct.get("24h") is not None and cpct.get("24h") is not None:
        gap = round(pct["24h"] - cpct["24h"], 2)
    for w in CONTRACT_WINDOWS:
        pct[w] = cpct.get(w)
        if crefs.get(w) is not None:
            refs[w] = crefs[w]
        else:
            refs.pop(w, None)
    # Prix du contrat : c'est lui qui prolonge la courbe 7J/14J/30J côté page
    # (refs.last reste celui de la continue, qui sert aux fenêtres 3M → 1AN).
    refs["last_court"] = crefs.get("last")
    return gap

# ── Matières premières : 24H → 30J sur Hyperliquid (source PRINCIPALE) ───────
# INCIDENT 2026-09-29 (MA : « le pétrole affiche −6,45 % aujourd'hui, c'est faux ») :
# le collecteur du nuage tournait encore la version d'avant FUT_ROOTS. Mais le
# diagnostic a montré que FUT_ROOTS lui-même reposait sur du sable : BZ=F ne coud
# pas deux contrats UNE fois par mois, il SAUTE de l'un à l'autre D'HEURE EN HEURE
# (relevé 21→28/09 : lun. 8h = déc. 97,61 $ ; 14h = nov. 100,20 $ ; 19h = déc.
# 96,17 $ ; mar. 8h = nov. 98,98 $…). Le contrat « identifié » par sa clôture
# change donc d'un passage à l'autre, et avec lui le 7J/30J affiché (nov. et déc.
# s'écartaient de 7 $).
#
# Hyperliquid (bourse HIP-3 « xyz ») cote un perpétuel par matière première,
# 7 j/7, qui suit le contrat le plus traité et glisse vers le suivant sur ~5
# séances — la méthode des indices de matières premières (S&P GSCI) : AUCUNE
# marche. Mesuré le 29/09 sur 75 j : xyz:BRENTOIL = contrat nov. à ±0,1 % jusqu'au
# 04/09, bascule progressive du 08 au 14/09 (−0,9 %, −1,8 %, −2,8 %, −3,8 %,
# −4,3 % vs nov.), puis contrat déc. à ±0,03 %. Aucune bougie horaire n'ouvre à
# plus de 1,5 % de la clôture précédente depuis mars (4 800 bougies × 4 marchés).
# Or et argent y suivent le comptant (≈ 0,9 % sous l'échéance déc., le portage).
# Volumes 24 h au 29/09 : Brent 247 M$, or 103 M$, argent 204 M$, cuivre 6 M$.
#
# Les fenêtres sont GLISSANTES (24 h = maintenant vs il y a 24 h, pas « depuis la
# clôture d'hier ») et calculées sur les bougies horaires, toutes de la même façon.
# `prevDayPx` de l'API n'est PAS le prix d'il y a 24 h (99,458 contre 100,73 à
# l'heure dite, le 29/09) : on ne s'en sert pas.
# Repli, si Hyperliquid ne répond pas ou paraît faux : le contrat Yahoo (FUT_ROOTS).
HL_INFO = "https://api.hyperliquid.xyz/info"
HL_DEX = "xyz"
HL_COMMODITIES = {      # cotation continue Yahoo → perpétuel Hyperliquid
    "BZ=F": "xyz:BRENTOIL",
    "GC=F": "xyz:GOLD",
    "SI=F": "xyz:SILVER",
    "HG=F": "xyz:COPPER",
}
HL_HOURLY_DAYS = 33          # couvre la borne 30J avec 3 jours de marge
HL_STEP = 900                # bougies de 15 min : la référence tombe à ≤ 15 min de la
                             # borne. En horaire elle pouvait tomber 59 min à côté, et le
                             # 22/09 le Brent a perdu 2,4 % entre 08:00 et 09:00 UTC :
                             # le 7J du 29/09 à 08:21 variait de 3 points selon l'heure prise.
HL_MAX_AGE_S = 3 * 3600      # marché coté 24/7 : une bougie plus vieille = source figée
HL_YAHOO_TOL = 0.12          # |HL / Yahoo − 1| au-delà → ce n'est pas le même actif
SHORT_DAYS = {"24h": 1, "7d": 7, "14d": 14, "30d": 30}
ROLLING_MAX_GAP_S = 6 * 3600 # réf. à plus de 6 h de la borne → fenêtre non mesurée

def bar_close(pts, step, now=None):
    """Recale une série intrajournalière Yahoo sur l'instant de son PRIX. Yahoo date
    chaque bougie par son OUVERTURE (la bougie horaire « 08:00 » porte le prix de
    09:00), et ajoute en queue un point live daté de la dernière transaction
    (08:24:31). La bougie EN COURS (08:00, ouverture ≤ maintenant < clôture) porte
    une valeur provisoire qui retarde : on l'écarte, le point live la remplace.
    Mesuré le 29/09 : sans recalage, la référence « il y a 7 jours » du BTC tombait
    39 min après la borne, pendant une hausse de 1 % — 7J −2,42 % au lieu de −1,43."""
    now = int(now or time.time())
    pts = pts or []
    live = pts[-1] if pts and pts[-1][0] % step else None
    limit = live[0] if live else now
    out = [[t + step, v] for t, v in pts if not t % step and t + step <= limit]
    if live:
        out.append([live[0], live[1]])
    return out

def _hl_post(body, timeout=20):
    for k in range(3):
        try:
            r = requests.post(HL_INFO, json=body, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            log(f"hyperliquid HTTP {r.status_code} retry {k+1}/3")
        except Exception as e:
            log(f"hyperliquid err {e} retry {k+1}/3")
        time.sleep(3 * (k + 1))
    return None

def fetch_hl_commodities(cont_series, now=None):
    """{ticker continu Yahoo: {"coin", "last", "last_ts", "fine", "hourly"}} pour
    chaque matière première dont le perpétuel Hyperliquid répond, est frais et cote
    le MÊME actif que Yahoo (garde HL_YAHOO_TOL). 1 + 4 requêtes (~3 200 bougies de
    15 min chacune, ~450 Ko, poids ~300 sur un plafond de 1 200/min).
    `fine` = [[ts de CLÔTURE de la bougie, clôture], …] au pas de 15 min (références
    des barres) ; `hourly` = la même, une bougie par heure (courbe, fichier léger)."""
    meta = _hl_post({"type": "metaAndAssetCtxs", "dex": HL_DEX})
    if not meta or len(meta) != 2:
        log("WARN Hyperliquid indisponible → matières premières sur le contrat Yahoo")
        return {}
    ctx = {u.get("name"): c for u, c in zip(meta[0].get("universe") or [], meta[1] or [])}
    now = int(now or time.time())
    out = {}
    for yt, coin in HL_COMMODITIES.items():
        c = ctx.get(coin)
        try:
            mark = float((c or {}).get("markPx") or 0)
        except (TypeError, ValueError):
            mark = 0.0
        if mark <= 0:
            log(f"WARN {coin} absent ou sans prix sur Hyperliquid → repli Yahoo")
            continue
        candles = _hl_post({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "15m",
            "startTime": (now - HL_HOURLY_DAYS * 86400) * 1000, "endTime": now * 1000}})
        pts = []
        for k in candles or []:
            try:
                # Bougie datée à sa CLÔTURE (prix connu à cet instant) ; celle en cours
                # l'est à « maintenant » (sa clôture provisoire = dernière transaction).
                pts.append([min(int(k["t"]) // 1000 + HL_STEP, now), _sig(float(k["c"]))])
            except (KeyError, TypeError, ValueError):
                continue
        pts.sort(key=lambda p: p[0])
        if not pts or now - pts[-1][0] > HL_MAX_AGE_S:
            log(f"WARN {coin} : bougies absentes ou figées → repli Yahoo")
            continue
        cont = cont_series.get(yt) or []
        ylast = cont[-1][1] if cont else None
        if ylast and abs(mark / ylast - 1) > HL_YAHOO_TOL:
            log(f"WARN {coin} {mark} vs Yahoo {yt} {ylast} : écart > {HL_YAHOO_TOL:.0%} → repli Yahoo")
            continue
        hourly = [p for p in pts[:-1] if p[0] % 3600 == 0] + pts[-1:]
        out[yt] = {"coin": coin, "last": _sig(mark), "last_ts": now, "fine": pts, "hourly": hourly}
    log(f"Hyperliquid : {len(out)}/{len(HL_COMMODITIES)} matières premières")
    return out

def rolling_windows(points, last_px, last_ts, windows=CONTRACT_WINDOWS,
                    max_gap=ROLLING_MAX_GAP_S):
    """% sur fenêtres GLISSANTES depuis une série infra-journalière [[ts, v], …] :
    réf = dernier point à ts ≤ last_ts − N jours, À CONDITION qu'il soit à moins de
    `max_gap` de la borne (sinon la série a un trou et la fenêtre serait fausse →
    None, jamais une valeur approchée en silence). Renvoie (pct, refs)."""
    pct, refs = {}, {}
    if not points or not last_px:
        return pct, refs
    for w in windows:
        target = last_ts - SHORT_DAYS[w] * 86400
        older = [p for p in points if p[0] <= target]
        if not older or target - older[-1][0] > max_gap or not older[-1][1]:
            pct[w] = None
            continue
        ref = older[-1][1]
        pct[w] = round((last_px / ref - 1) * 100, 2)
        refs[w] = ref
    return pct, refs

def apply_hl_windows(pct, refs, h):
    """Remplace 24H → 30J par les fenêtres glissantes Hyperliquid. Renvoie l'écart
    24H (Yahoo continu − Hyperliquid) en points, ou False si le 24H manque (la
    classe garde alors le repli Yahoo)."""
    hp, hr = rolling_windows(h["fine"], h["last"], h["last_ts"])
    if hp.get("24h") is None:
        return False
    gap = round(pct["24h"] - hp["24h"], 2) if pct.get("24h") is not None else None
    for w in CONTRACT_WINDOWS:
        pct[w] = hp.get(w)
        if hr.get(w) is not None:
            refs[w] = hr[w]
        else:
            refs.pop(w, None)
    refs["last_court"] = h["last"]
    refs["last_court_ts"] = h["last_ts"]
    return gap

def apply_rolling(pct, refs, hourly):
    """Repli crypto (CoinGecko indisponible) : 24H → 30J GLISSANTS depuis la série
    horaire Yahoo, au lieu des clôtures quotidiennes. INCIDENT 2026-09-29 : la barre
    quotidienne Yahoo d'une crypto démarre à 00:00 UTC, donc « 24H » = variation
    DEPUIS MINUIT UTC (8 h de marché à 10:00 à Paris), et « 7J » couvrait 7 j + la
    journée entamée. Une fenêtre que l'horaire ne couvre pas garde la valeur
    quotidienne. Renvoie le nombre de fenêtres remplacées."""
    if not hourly:
        return 0
    last_ts, last_px = hourly[-1]
    rp, rr = rolling_windows(hourly, last_px, last_ts)
    n = 0
    for w, v in rp.items():
        if v is None:
            continue
        pct[w], refs[w] = v, rr[w]
        n += 1
    if n:
        refs["last_court"], refs["last_court_ts"] = last_px, last_ts
    return n

def series_windows(points):
    """% par fenêtre depuis une série daily [[ts, close], …] + réfs pour audit.
    Réf = dernier point à ts ≤ cible (dernier jour de cotation avant la borne)."""
    if not points or len(points) < 5:
        return None, None
    last_ts, last_px = points[-1]
    if not last_px:
        return None, None
    pct, refs = {}, {"last": last_px, "last_ts": last_ts}
    prev_px = points[-2][1]
    pct["24h"] = round((last_px / prev_px - 1) * 100, 2) if prev_px else None
    refs["24h"] = prev_px
    for w, days in WIN_DAYS.items():
        target = last_ts - days * 86400
        older = [p for p in points if p[0] <= target]
        if older and older[-1][1]:
            ref = older[-1][1]
            pct[w] = round((last_px / ref - 1) * 100, 2)
            refs[w] = ref
        else:
            pct[w] = None
    year = datetime.now(timezone.utc).year
    jan1 = datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()
    older = [p for p in points if p[0] < jan1]
    if older and older[-1][1]:
        ref = older[-1][1]
        pct["ytd"] = round((last_px / ref - 1) * 100, 2)
        refs["ytd"] = ref
    else:
        pct["ytd"] = None
    return pct, refs

def window_from_series(points, days, last_px=None):
    """% sur `days` jours depuis une série daily [[ts, v], …] + réf (audit).
    Réf = dernier point à ts ≤ (dernier_ts − days). `last_px` (prix live) prime
    sur le dernier point de la série comme numérateur (cohérent avec les autres
    fenêtres crypto qui utilisent le prix live du marché) ; sinon on prend la
    série. Sert aux fenêtres crypto absentes de CoinGecko /markets (90d/180d)."""
    if not points or len(points) < 3:
        return None, None
    last_ts, series_last = points[-1]
    num = last_px if last_px else series_last
    if not num:
        return None, None
    target = last_ts - days * 86400
    older = [p for p in points if p[0] <= target]
    if older and older[-1][1]:
        ref = older[-1][1]
        return round((num / ref - 1) * 100, 2), ref
    return None, None

# ── CoinGecko (BTC + agrégat altcoins) ──────────────────────────────────────
def fetch_coingecko():
    """Top 250 CoinGecko. Backoff LONG sur 429 (10s → 30s → 60s + Retry-After) :
    le tier gratuit throttle par rafales, l'ancien backoff 5/10/15 s repartait dans
    la même fenêtre de blocage et les 3 essais tombaient ensemble (incident du
    2026-07-25). Si ça échoue quand même, le repli Yahoo prend le relais (CRYPTO_YF)."""
    url = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
           "&order=market_cap_desc&per_page=250&page=1&sparkline=false"
           "&price_change_percentage=24h%2C7d%2C14d%2C30d%2C1y")
    waits = [10, 30, 60]
    for k in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.json()
            # 403 = adresse IP BLOQUÉE par CoinGecko (page « Request blocked » de
            # CloudFront, constatée sur le Mac le 29/09), pas un débit : réessayer
            # coûtait ~2 min par passage, dans le nuage aussi, où le temps d'un
            # collecteur est plafonné. Le repli Yahoo prend la main tout de suite.
            if r.status_code == 403:
                log("coingecko HTTP 403 (IP bloquée) → repli Yahoo sans attendre")
                return []
            wait = waits[k]
            if r.status_code == 429:
                try:
                    wait = max(wait, min(90, int(r.headers.get("Retry-After") or 0)))
                except ValueError:
                    pass
            log(f"coingecko HTTP {r.status_code} retry {k+1}/3 (attente {wait}s)")
        except Exception as e:
            wait = waits[k]
            log(f"coingecko err {e} retry {k+1}/3 (attente {wait}s)")
        time.sleep(wait)
    return []

# ── Repli Yahoo : BTC + indice altcoins (aucune clé, aucun quota) ───────────
def fetch_crypto_yahoo():
    """1 lot spark daily 2 ans + 1 lot horaire 1 mois pour BTC + les 19 alts du
    panier de repli. Fetché à CHAQUE run (et pas seulement en cas de panne CG) pour
    deux raisons : (1) les séries BTC de la courbe en viennent désormais, ce qui
    supprime 2 appels CoinGecko par run — moins de charge = moins de 429 ;
    (2) un chemin de repli jamais exercé pourrit en silence."""
    tk = [YF_BTC] + list(CRYPTO_YF_ALTS.values())
    daily = fetch_yahoo_spark(tk, rng="2y", interval="1d", min_pts=100)
    hourly = fetch_yahoo_spark(tk, rng="1mo", interval="1h", min_pts=20)
    # 15 min sur 5 jours : la référence du 24H à ≤ 15 min de la borne (cf bar_close).
    fine = fetch_yahoo_spark(tk, rng="5d", interval="15m", min_pts=50)
    hourly = {t: bar_close(p, 3600) for t, p in hourly.items()}
    fine = {t: bar_close(p, 900) for t, p in fine.items()}
    log(f"Yahoo crypto : daily {len(daily)}/{len(tk)} · horaire {len(hourly)}/{len(tk)}"
        f" · 15 min {len(fine)}/{len(tk)}")
    return daily, hourly, fine

def save_fallback_supplies(alts, coins_ok):
    """Fige les supplies (mcap/prix) des alts du panier à chaque run CoinGecko OK.
    Elles pondèrent l'indice de repli quand CoinGecko est down : sans elles, un
    panier de prix ne dirait rien de l'argent qui circule (une hausse de SHIB
    pèserait autant qu'une hausse d'ETH). Stocke aussi la couverture réelle
    (mcap panier / mcap alts) pour l'afficher dans la source du widget."""
    if not coins_ok or not alts:
        return
    sup, basket_mc = {}, 0.0
    for c in alts:
        sym = (c.get("symbol") or "").upper()
        tk = CRYPTO_YF_ALTS.get(sym)
        px, mc = c.get("current_price"), c.get("market_cap")
        if not tk or not px or not mc:
            continue
        sup[tk] = {"supply": mc / px, "px_cg": px}
        basket_mc += mc
    total_mc = sum(c.get("market_cap") or 0 for c in alts)
    if not sup or total_mc <= 0:
        return
    payload = {"ts": int(time.time()), "updated": stamp(),
               "supplies": sup, "n": len(sup), "n_alts_total": len(alts),
               "cover_pct": round(basket_mc / total_mc * 100, 1)}
    tmp = FALLBACK_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(FALLBACK_FILE)

# Base INTÉGRÉE (relevé CoinGecko du 2026-09-28 23:50, Paris) — sert quand
# moneyflow_fallback.json manque. INCIDENT 2026-09-29 : le collecteur du nuage
# démarre sur une machine vierge où ce fichier n'existe pas (il n'est pas publié) ;
# un passage où CoinGecko refusait ne pouvait donc pas reconstruire les altcoins
# et la ligne restait figée (« PÉRIMÉ »). Les masses en circulation bougent de
# quelques % par an : une base de quelques semaines pondère correctement le panier.
BUILTIN_FALLBACK = {
    "ts": 1790632217, "updated": "2026-09-28 23:50 (base intégrée)", "n": 19,
    "n_alts_total": 210, "cover_pct": 78.4, "builtin": True,
    "supplies": {
        "ETH-USD": {"supply": 122087282, "px_cg": 2677.75},
        "BNB-USD": {"supply": 133159422, "px_cg": 761.72},
        "XRP-USD": {"supply": 62814838423, "px_cg": 1.49},
        "SOL-USD": {"supply": 587867366, "px_cg": 117.97},
        "TRX-USD": {"supply": 94972812762, "px_cg": 0.335642},
        "HYPE32196-USD": {"supply": 222449702, "px_cg": 86.92},
        "DOGE-USD": {"supply": 156107514031, "px_cg": 0.093329},
        "LINK-USD": {"supply": 748061480, "px_cg": 15.15},
        "XMR-USD": {"supply": 18810693, "px_cg": 536.73},
        "ADA-USD": {"supply": 37535247044, "px_cg": 0.244033},
        "XLM-USD": {"supply": 35008431691, "px_cg": 0.224876},
        "BCH-USD": {"supply": 20095978, "px_cg": 306.92},
        "NEAR-USD": {"supply": 1307529750, "px_cg": 4.71},
        "UNI7083-USD": {"supply": 620227885, "px_cg": 8.69},
        "LTC-USD": {"supply": 77655171, "px_cg": 69.0},
        "HBAR-USD": {"supply": 43831591728, "px_cg": 0.121757},
        "AVAX-USD": {"supply": 469781726, "px_cg": 10.4},
        "CRO-USD": {"supply": 49749999508, "px_cg": 0.069142},
        "SHIB-USD": {"supply": 589088367021277, "px_cg": 5.64e-06},
    },
}

def load_fallback_supplies():
    try:
        d = json.loads(FALLBACK_FILE.read_text())
        if d.get("supplies"):
            return d
    except Exception:
        pass
    return BUILTIN_FALLBACK

def price_sanity_pct(fb):
    """Écart toléré prix Yahoo ↔ prix CoinGecko du relevé des masses : 25 % pour un
    relevé de moins d'un jour, +10 pt par jour d'âge, plafond 60 %. La garde ne vise
    que les MAUVAIS ACTIFS (collision de symbole = facteur 2 à 1 000). À 5 %, elle
    jetait AVAX le 29/09 pour +9,5 % en une nuit — un vrai mouvement, pas une erreur."""
    age_j = max(0.0, (time.time() - (fb.get("ts") or 0)) / 86400)
    return min(60.0, PRICE_SANITY_PCT + 10.0 * int(age_j))

def build_yf_alt_index(series_by_ticker, supplies, px_cg=None, tol=PRICE_SANITY_PCT):
    """Indice altcoins de repli : Σ supply_i × prix_i(t), forward-fill par coin sur
    un calendrier commun (même méthode que build_alt_index, mais prix Yahoo × supply
    figée au lieu des mcap historiques CoinGecko). Biais assumé : l'émission nette
    depuis le dernier run CoinGecko n'est pas captée (< 1 pt sur 1 an) — c'est un
    repli, il est étiqueté comme tel dans la source affichée.
    Garde-fou : un ticker dont le dernier prix Yahoo s'écarte de > 5 % du prix
    CoinGecko connu est JETÉ (mauvais mapping symbole → mauvais actif)."""
    cols = []
    for tk, meta in (supplies or {}).items():
        pts = series_by_ticker.get(tk) or []
        sup = (meta or {}).get("supply")
        if not pts or not sup or len(pts) < 5:
            continue
        ref_px = (meta or {}).get("px_cg")
        if ref_px and abs(pts[-1][1] / ref_px - 1) * 100 > tol:
            log(f"repli : {tk} écarté (prix Yahoo {pts[-1][1]} vs CG {ref_px})")
            continue
        cols.append((sup, pts))
    if len(cols) < 5:                     # panier trop maigre → pas d'indice
        return []
    all_ts = sorted({t for _, pts in cols for t, _ in pts})
    ptrs, last = [0] * len(cols), [None] * len(cols)
    idx = []
    for ts in all_ts:
        s, seen = 0.0, 0
        for i, (sup, pts) in enumerate(cols):
            while ptrs[i] < len(pts) and pts[ptrs[i]][0] <= ts:
                last[i] = pts[ptrs[i]][1]; ptrs[i] += 1
            if last[i] is not None:
                s += last[i] * sup; seen += 1
        if s > 0 and seen >= len(cols) - 2:     # évite les marches de début de série
            idx.append([ts, round(s)])
    return idx

def is_excluded_alt(c):
    """(exclu?, raison) — règles simples & explicites, listées dans le cache."""
    cid = (c.get("id") or "").lower()
    name = (c.get("name") or "").lower()
    sym = (c.get("symbol") or "").lower()
    if cid in STABLE_IDS:
        return True, "stable"
    if cid in GOLD_TOKEN_IDS:
        return True, "gold-token"
    if cid in WRAP_IDS or sym in WRAP_SYMBOLS:
        return True, "wrapped"
    if any(p in cid or p in name for p in WRAP_PATTERNS):
        return True, "wrapped"
    px = c.get("current_price")
    ch24 = c.get("price_change_percentage_24h_in_currency")
    ch30 = c.get("price_change_percentage_30d_in_currency")
    if (px is not None and 0.95 < px < 1.05
            and (ch24 is None or abs(ch24) < 1.5)
            and (ch30 is None or abs(ch30) < 4)):
        return True, "peg-band"
    return False, ""

def agg_alt_pct(coins, field):
    """Agrégat mcap-weighted : reconstruit la mcap passée coin par coin."""
    now_sum, then_sum, n = 0.0, 0.0, 0
    for c in coins:
        mc = c.get("market_cap")
        p = c.get(field)
        if not mc or p is None or p <= -100:
            continue
        now_sum += mc
        then_sum += mc / (1 + p / 100.0)
        n += 1
    if then_sum <= 0:
        return None, 0
    return round((now_sum / then_sum - 1) * 100, 2), n

def agg_alt_ytd(coins, bases):
    now_sum, then_sum, n = 0.0, 0.0, 0
    for c in coins:
        mc, px = c.get("market_cap"), c.get("current_price")
        base = bases.get(c.get("id"))
        if not mc or not px or not base or base <= 0:
            continue
        now_sum += mc
        then_sum += mc * base / px
        n += 1
    if then_sum <= 0:
        return None, 0
    return round((now_sum / then_sum - 1) * 100, 2), n

# ── DefiLlama stablecoins ───────────────────────────────────────────────────
def drop_partial_tail(pts, tol=0.10, max_drop=3):
    """SAFEGUARD (bug 2026-07-27) : jeter le(s) dernier(s) point(s) PARTIEL(S).

    DefiLlama construit le point du jour en cours au fil de son indexation : tôt
    le matin UTC il ne contient qu'une fraction des émetteurs/chaînes. Mesuré au
    run de 04:42 (02:42 UTC) : dernier point = 122,4 Md$ au lieu de ~306 Md$ →
    TOUTES les fenêtres du widget affichaient ≈ −60 % (« l'argent fuit les
    stablecoins ») et la courbe plongeait d'une falaise. Le même point relu 2 h
    plus tard valait 306,3 Md$ : ce n'était pas un flux, c'était un agrégat en
    cours d'écriture.

    Garde-fou : la masse stable totale ne bouge JAMAIS de 10 % en un jour (record
    historique ≈ 3 %, effondrement UST inclus). Tout point de queue qui s'écarte
    de plus de `tol` du précédent est donc incomplet → supprimé (max 3, pour ne
    pas éroder la série si un vrai choc arrivait). NE PAS remplacer par « ignorer
    le point du jour » : on perdrait la fraîcheur les 22 h où il est complet."""
    dropped = 0
    while len(pts) >= 2 and dropped < max_drop:
        prev = pts[-2][1]
        if prev > 0 and abs(pts[-1][1] / prev - 1) > tol:
            log(f"llama point partiel jeté : {pts[-1][0]} = {pts[-1][1]/1e9:.1f} Md$ "
                f"vs {prev/1e9:.1f} Md$ la veille")
            pts.pop()
            dropped += 1
        else:
            break
    return pts

def fetch_stables_series():
    url = "https://stablecoins.llama.fi/stablecoincharts/all"
    for k in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                pts = []
                for p in r.json():
                    ts = int(p.get("date", 0))
                    mc = float((p.get("totalCirculatingUSD") or {}).get("peggedUSD") or 0)
                    if ts and mc > 0:
                        pts.append([ts, mc])
                pts.sort(key=lambda x: x[0])
                return drop_partial_tail(pts)
            log(f"llama HTTP {r.status_code} retry {k+1}/3")
        except Exception as e:
            log(f"llama err {e} retry {k+1}/3")
        time.sleep(4 * (k + 1))
    return []

# ── Séries pour le mode courbe ──────────────────────────────────────────────
def fetch_cg_market_chart(coin_id, days=365, field="prices"):
    """Historique CoinGecko. [[ts_s, v], …]. On OMET `interval` → granularité AUTO
    du tier gratuit : days>90 → quotidien, days 2-90 → HORAIRE (interval explicite
    = payant). Sert UNIQUEMENT à la série BTC de la courbe : elle doit venir de la
    même source que le % des barres, sinon la même ligne affiche deux chiffres
    différents dans la même tuile (mesuré : 6,96 % en Yahoo vs 8,34 % en CoinGecko
    sur 30 j — écart de référence entre sources, pas une erreur, mais incohérent
    à l'écran). 2 appels/run ; c'étaient les 25 appels du panier alts qui
    provoquaient les 429, pas ceux-ci."""
    url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
           f"?vs_currency=usd&days={days}")
    for k in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                out = []
                for p in (r.json().get(field) or []):
                    ts = int(p[0] / 1000); v = float(p[1])
                    if v > 0:
                        out.append([ts, v])
                return out
            log(f"cg chart {coin_id} HTTP {r.status_code} retry {k+1}/3")
        except Exception as e:
            log(f"cg chart {coin_id} err {e} retry {k+1}/3")
        time.sleep([8, 20, 40][k])
    return []

def _round(v):
    return round(v, 6) if v < 1 else round(v, 4)

def trim_series(pts, days=SERIES_DAYS):
    """Garde ~`days` jours, arrondit pour limiter la taille du cache."""
    if not pts:
        return []
    cutoff = int(time.time()) - days * 86400
    return [[t, _round(v)] for t, v in pts if t >= cutoff]

def merge_daily_hourly(daily, hourly):
    """Série à résolution variable : quotidien pour l'ancien + HORAIRE pour le
    récent (~30j) → beaucoup plus de points sur les fenêtres courtes (7J/30J)
    sans exploser la taille (le long terme reste daily). Le JS échantillonne
    selon la fenêtre. Suppose daily & hourly triés ascendants."""
    if not hourly:
        return daily
    hourly = [[t, _round(v)] for t, v in hourly]
    cut = hourly[0][0]
    return [p for p in daily if p[0] < cut] + hourly

def build_series(yahoo_series, spts, alts, coins, yf_crypto_d=None, yf_crypto_h=None, contracts=None,
                 hl=None):
    """Assemble window.__MONEYFLOW_SERIES__ (14 classes) à résolution VARIABLE :
    horaire sur ~30j récents (fenêtres courtes denses) + quotidien sur ~1 an.
    TOUT vient de Yahoo depuis le 2026-07-25 : les 11 classes macro, BTC (BTC-USD)
    et l'indice alts (Σ supply×prix) sortent de 4 requêtes spark ; stables = DefiLlama.
    Plus AUCUN appel CoinGecko ici (avant : 2 pour BTC à chaque run + 25 pour le
    panier alts toutes les 6 h — ces rafales étaient la cause des 429 qui figeaient
    btc & alts). CoinGecko ne sert plus qu'à l'agrégat exact des barres, 1 appel/run."""
    out = {}
    tickers = [t for _, t, _, _, _ in YAHOO_CLASSES]
    # Yahoo horaire (1 requête batch, ~30j) fusionné avec le daily 1 an
    yahoo_hourly = fetch_yahoo_spark(tickers, rng="1mo", interval="1h", min_pts=20)
    log(f"Yahoo horaire : {len(yahoo_hourly)}/{len(tickers)} tickers")
    for cid, ticker, *_ in YAHOO_CLASSES:
        daily = trim_series(yahoo_series.get(ticker, []))
        out[cid] = merge_daily_hourly(daily, yahoo_hourly.get(ticker, []))
    if spts:
        out["stables"] = trim_series(spts)

    # Séries du contrat en tête (matières premières) pour les courbes 7J/14J/30J :
    # mêmes fenêtres que les barres (CONTRACT_WINDOWS), sinon la courbe 7J du
    # Brent plongeait de 8 % le jour de la couture pendant que la barre disait −1,9 %.
    # ~45 j de quotidien + l'horaire du mois : de quoi couvrir la borne 30J.
    contract_out, contract_meta = {}, {}
    fut = {t: cid for cid, t, *_ in YAHOO_CLASSES if t in FUT_ROOTS}
    # Hyperliquid d'abord (même source que les barres 24H → 30J, cf HL_COMMODITIES) :
    # horaire sur 33 j, 7 j/7. Le contrat Yahoo ne sert qu'aux classes sans HL.
    for t, h in (hl or {}).items():
        if t in fut and h.get("hourly"):
            contract_out[fut[t]] = [[ts, _round(v)] for ts, v in h["hourly"]]
            contract_meta[fut[t]] = {"symbol": h["coin"], "source": "hyperliquid"}
    contracts = {t: k for t, k in (contracts or {}).items()
                 if t in fut and fut[t] not in contract_out}
    if contracts:
        syms = [k["symbol"] for k in contracts.values()]
        ch = fetch_yahoo_spark(syms, rng="1mo", interval="1h", min_pts=20)
        for t, k in contracts.items():
            cid = fut[t]
            contract_out[cid] = merge_daily_hourly(trim_series(k["series"], days=45),
                                                   ch.get(k["symbol"], []))
            contract_meta[cid] = {"symbol": k["symbol"], "mois": k["mois"]}
        log(f"séries contrat en tête : {len(contract_out)}/{len(fut)} "
            f"(horaire {len(ch)}/{len(syms)})")

    prev, prev_crypto_ts, prev_basket = {}, 0, []
    try:
        ps = json.loads(SERIES_FILE.read_text())
        prev = ps.get("classes", {})
        prev_crypto_ts = ps.get("crypto_ts", 0)
        prev_basket = ps.get("alts_meta", {}).get("basket", [])
    except Exception:
        pass

    now_ts = int(time.time())
    yf_crypto_d = yf_crypto_d or {}
    yf_crypto_h = yf_crypto_h or {}
    # BTC : CoinGecko (même source que le % des barres → courbe et barres racontent
    # la même chose), avec REPLI Yahoo BTC-USD si CoinGecko est tombé. Avant, l'échec
    # CoinGecko figeait la série sur celle du run précédent.
    btc_d = btc_h = []
    if coins:
        time.sleep(1.5)                                  # respire entre 2 appels CG
        btc_d = fetch_cg_market_chart("bitcoin", days=365)
        time.sleep(1.5)
        # days=32 et non 30 : la courbe 30J démarre à today−30j PILE. Avec 30 jours
        # d'horaire, ce point tombe juste avant le début de l'horaire → le client
        # retombait sur le point QUOTIDIEN de la veille (jusqu'à 24 h plus tôt) et la
        # courbe affichait +5,3 % là où les barres disaient +8,6 %. 2 jours de marge
        # d'horaire couvrent la borne (granularité horaire conservée : days ≤ 90).
        btc_h = fetch_cg_market_chart("bitcoin", days=32)
    if btc_d or btc_h:
        out["btc"] = merge_daily_hourly(trim_series(btc_d), btc_h)
        log(f"BTC séries (CoinGecko) : daily {len(btc_d)} + horaire {len(btc_h)} pts")
    else:
        yd = trim_series(yf_crypto_d.get(YF_BTC, []))
        yh = yf_crypto_h.get(YF_BTC, [])
        if yd or yh:
            out["btc"] = merge_daily_hourly(yd, yh)
            log(f"BTC séries → REPLI Yahoo ({len(yd)} daily + {len(yh)} horaire)")
        elif prev.get("btc"):
            out["btc"] = prev["btc"]
            log("BTC séries : CoinGecko ET Yahoo vides → série précédente conservée")

    # Panier alts pour la COURBE — Yahoo (2026-07-25, ex-CoinGecko).
    # AVANT : 25 appels /market_chart CoinGecko gatés 6 h. Ces rafales étaient la
    # cause première des 429 (run de 7 min à se faire jeter) et poisonnaient l'appel
    # /markets du run suivant → btc & alts figés. APRÈS : indice Σ supply×prix
    # reconstruit depuis le lot Yahoo déjà téléchargé (0 appel CoinGecko), rafraîchi
    # à CHAQUE run au lieu de toutes les 6 h, et HORAIRE au lieu de quotidien.
    # Perdu : la dérive d'émission (supplies figées, < 1 pt/an). Les % des barres
    # restent l'agrégat exact CoinGecko sur 208 coins — seule la courbe est un proxy.
    fbs = load_fallback_supplies()
    sup, tol = (fbs.get("supplies") or {}), price_sanity_pct(fbs)
    idx_d = build_yf_alt_index(yf_crypto_d, sup, tol=tol)
    idx_h = build_yf_alt_index(yf_crypto_h, sup, tol=tol)
    if idx_d or idx_h:
        out["alts"] = merge_daily_hourly(trim_series(idx_d), idx_h)
        crypto_ts, alts_basket = now_ts, list(sup.keys())
        log(f"indice alts Yahoo : {len(idx_d)} daily + {len(idx_h)} horaire ({len(sup)} coins)")
    else:
        if prev.get("alts"): out["alts"] = prev["alts"]
        crypto_ts, alts_basket = prev_crypto_ts, prev_basket
        log(f"indice alts INDISPONIBLE → série précédente (age {(now_ts - prev_crypto_ts)//3600}h)")

    # Merge-preserve : une classe ratée ce run garde sa série précédente
    for cid, arr in prev.items():
        if cid not in out and arr:
            out[cid] = arr

    if not out:
        return
    payload = {
        "updated": stamp(),
        "updated_ts": now_ts,
        "crypto_ts": crypto_ts,
        "days": SERIES_DAYS,
        "resolution": "horaire ~30j + quotidien ~1an (stables : quotidien)",
        "classes": out,
        # Fenêtres 7J/14J/30J des matières premières : série Hyperliquid (source des
        # barres), sinon le SEUL contrat Yahoo en tête (cf FUT_ROOTS). Absente pour
        # une classe = ni l'une ni l'autre ce run.
        "contract": contract_out,
        "contract_meta": contract_meta,
        "contract_windows": list(CONTRACT_WINDOWS),
        "alts_meta": {"proxy": True, "basket": alts_basket, "n": len(alts_basket),
                      "source": "Yahoo · Σ supply×prix, supplies figées au dernier run CoinGecko",
                      "supplies_du": load_fallback_supplies().get("updated")},
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    tmp = SERIES_FILE.with_suffix(".json.tmp"); tmp.write_text(body); tmp.replace(SERIES_FILE)
    tmp = SERIES_JS.with_suffix(".js.tmp")
    tmp.write_text("window.__MONEYFLOW_SERIES__=" + body + ";\n"); tmp.replace(SERIES_JS)
    log(f"séries écrites — {len(out)} classes ({round(len(body)/1024)} Ko)")

# ── Main ────────────────────────────────────────────────────────────────────
def main():
    # Garde anti-course « réveil du Mac » : attendre que le DNS résolve avant de
    # fetcher, sinon les échecs de résolution figent le cache (cf wait_for_network).
    wait_for_network()
    now_iso, now_ts = stamp(), int(time.time())
    classes, notes = [], {}

    # 1) Yahoo (10 classes)
    tickers = [t for _, t, _, _, _ in YAHOO_CLASSES]
    series = fetch_yahoo_spark(tickers)
    missing = [t for t in tickers if t not in series]
    if missing:
        log(f"spark manquants {missing} → fallback yfinance")
        for t in missing:
            pts = fetch_yfinance_single(t)
            if pts:
                series[t] = pts
    # Contrats à terme : identifier le contrat que suit chaque cotation continue
    # (cf bloc FUT_ROOTS). Un échec ne bloque rien : la classe garde la mesure
    # continue ET porte le drapeau `contract.ok=False`, affiché sur la ligne.
    try:
        contracts = resolve_front_contracts(series)
    except Exception as e:
        contracts = {}
        log(f"WARN identification des contrats en échec ({e})")
    # Matières premières : Hyperliquid en SOURCE PRINCIPALE des fenêtres courtes
    # (cf HL_COMMODITIES) ; le contrat Yahoo ci-dessus devient le repli + contrôle.
    try:
        hl = fetch_hl_commodities(series)
    except Exception as e:
        hl = {}
        log(f"WARN Hyperliquid en échec ({e}) → repli contrat Yahoo")
    for cid, ticker, label, icon, src in YAHOO_CLASSES:
        pct, refs = series_windows(series.get(ticker, []))
        entry = {"id": cid, "label": label, "icon": icon, "src": src,
                 "asof": now_iso, "asof_ts": now_ts}
        if pct:
            entry["pct"], entry["refs"] = pct, refs
            if ticker in FUT_ROOTS:
                k = contracts.get(ticker)
                h = hl.get(ticker)
                gap = apply_hl_windows(pct, refs, h) if h else False
                if gap is not False:
                    entry["contract"] = {"ok": True, "source": "hyperliquid", "symbol": h["coin"],
                                         "fenetres": list(CONTRACT_WINDOWS), "ecart_24h_pt": gap}
                    # Contrôle (audit) : le 24H du contrat Yahoo identifié, s'il l'est.
                    # Pas la même définition (veille de cotation vs 24 h glissantes)
                    # → un écart de quelques dixièmes est normal, pas 5 points.
                    kp = series_windows(k["series"])[0] if k else None
                    if kp:
                        entry["contract"]["controle_yahoo"] = {
                            "symbol": k["symbol"], "mois": k["mois"], "24h": kp.get("24h")}
                    entry["src"] = (f"Hyperliquid {h['coin']} (perpétuel coté 7j/7, suit l'échéance la "
                                    f"plus traitée) — 24H à 30J glissants ; 3M à 1AN : {src}, contrat "
                                    f"en tête à chaque date")
                    if gap is not None and abs(gap) >= ROLL_FLAG_PT:
                        log(f"{cid} : 24H Yahoo continu {pct['24h'] + gap:+.2f} % → Hyperliquid "
                            f"{h['coin']} {pct['24h']:+.2f} %")
                    classes.append(entry)
                    continue
                gap = apply_contract_windows(pct, refs, k["series"]) if k else None
                if k and gap is not None:
                    entry["contract"] = {"ok": True, "source": "yahoo_contrat",
                                         "symbol": k["symbol"], "mois": k["mois"],
                                         "fenetres": list(CONTRACT_WINDOWS), "ecart_24h_pt": gap}
                    entry["src"] = (f"{src} — 24H à 30J sur le seul contrat {k['mois']} "
                                    f"({k['symbol']}) ; 3M à 1AN : contrat en tête à chaque date")
                    if abs(gap) >= ROLL_FLAG_PT:
                        log(f"{cid} : changement de contrat neutralisé sur 24H "
                            f"(continu {pct['24h'] + gap:+.2f} % → contrat {k['symbol']} {pct['24h']:+.2f} %)")
                else:
                    entry["contract"] = {"ok": False, "symbol": None}
        classes.append(entry)

    # 2) CoinGecko : BTC + altcoins (+ filet Yahoo, fetché à chaque run)
    yf_crypto_d, yf_crypto_h, yf_crypto_f = fetch_crypto_yahoo()
    # Série des fenêtres glissantes : horaire (≥ 5 j) prolongée par le 15 min (5 j).
    # (Fusion SANS arrondi : merge_daily_hourly arrondit à 6 décimales, ce qui
    #  ramènerait SHIB, 0,0000056 $, à 0,000006 $ dans l'indice altcoins.)
    def _prolonge(h, f):
        return [p for p in (h or []) if not f or p[0] < f[0][0]] + (f or [])
    yf_crypto_r = {t: _prolonge(yf_crypto_h.get(t), yf_crypto_f.get(t))
                   for t in set(yf_crypto_h) | set(yf_crypto_f)}
    coins = fetch_coingecko()
    ytd_bases, ytd_year = {}, None
    try:
        yc = json.loads(YTD_CACHE.read_text())
        if int(yc.get("year", 0)) == datetime.now(timezone.utc).year:
            ytd_bases, ytd_year = yc.get("base", {}), yc.get("year")
    except Exception as e:
        log(f"crypto_ytd_cache illisible ({e}) → YTD crypto indisponible")

    btc_entry = {"id": BTC_META[0], "label": BTC_META[1], "icon": BTC_META[2],
                 "src": BTC_META[3], "asof": now_iso, "asof_ts": now_ts}
    alts_entry = {"id": ALTS_META[0], "label": ALTS_META[1], "icon": ALTS_META[2],
                  "src": ALTS_META[3], "asof": now_iso, "asof_ts": now_ts}
    alts = []                       # hoisté : utilisé plus bas par build_series même si coins vide
    if coins:
        btc = next((c for c in coins if c.get("id") == "bitcoin"), None)
        if btc:
            bpx = btc.get("current_price")
            bpct = {
                "24h": btc.get("price_change_percentage_24h_in_currency"),
                "7d":  btc.get("price_change_percentage_7d_in_currency"),
                "14d": btc.get("price_change_percentage_14d_in_currency"),
                "30d": btc.get("price_change_percentage_30d_in_currency"),
                "1y":  btc.get("price_change_percentage_1y_in_currency"),
            }
            bbase = ytd_bases.get("bitcoin")
            bpct["ytd"] = (bpx / bbase - 1) * 100 if (bpx and bbase) else None
            btc_entry["pct"] = {k: (round(v, 2) if v is not None else None) for k, v in bpct.items()}
            btc_entry["refs"] = {"last": bpx, "ytd": bbase}
        excluded = {}
        for c in coins:
            if c.get("id") == "bitcoin":
                continue
            exc, why = is_excluded_alt(c)
            if exc:
                excluded.setdefault(why, []).append(c.get("id"))
            else:
                alts.append(c)
        apct, ns = {}, {}
        for w, field in [("24h", "price_change_percentage_24h_in_currency"),
                         ("7d", "price_change_percentage_7d_in_currency"),
                         ("14d", "price_change_percentage_14d_in_currency"),
                         ("30d", "price_change_percentage_30d_in_currency"),
                         ("1y", "price_change_percentage_1y_in_currency")]:
            apct[w], ns[w] = agg_alt_pct(alts, field)
        apct["ytd"], ns["ytd"] = agg_alt_ytd(alts, ytd_bases)
        alts_entry["pct"] = apct
        alts_entry["n"] = len(alts)
        alts_entry["mcap"] = round(sum(c.get("market_cap") or 0 for c in alts))
        notes["alts"] = {
            "univers": "CoinGecko top 250 hors bitcoin", "retenus": len(alts),
            "n_par_fenetre": ns, "exclus": excluded, "ytd_base_year": ytd_year,
            "formule": "pct = Σmcap_now/Σmcap_then − 1 ; mcap_then = mcap/(1+p/100) ; YTD mcap_then = mcap×base/prix",
            "caveat": "biais de survivant : composition = top 250 actuel",
        }
        # Filet pour les prochains runs : supplies figées du panier de repli.
        save_fallback_supplies(alts, True)

    # 2ter) REPLI YAHOO si CoinGecko est tombé (429 / réseau) — sans lui, btc & alts
    # gardaient les valeurs du run précédent (merge-preserve) et pouvaient afficher
    # des chiffres vieux de 24 h à côté de 12 classes fraîches (incident 2026-07-25).
    if not coins:
        fb = load_fallback_supplies()
        bpct, brefs = series_windows(yf_crypto_d.get(YF_BTC, []))
        if bpct:
            nr = apply_rolling(bpct, brefs, yf_crypto_r.get(YF_BTC, []))
            btc_entry["pct"], btc_entry["refs"] = bpct, brefs
            btc_entry["src"] = ("Repli Yahoo BTC-USD (CoinGecko indisponible) — "
                                + ("24H à 30J glissants sur l'horaire, " if nr else "")
                                + "3M à 1AN sur les clôtures quotidiennes")
            btc_entry["fallback"] = "yahoo"
            log("repli BTC → Yahoo BTC-USD : OK")
        else:
            log("repli BTC → Yahoo BTC-USD : ÉCHEC (série absente)")
        aidx = build_yf_alt_index(yf_crypto_d, fb.get("supplies") or {}, tol=price_sanity_pct(fb))
        apct, arefs = series_windows(aidx)
        if apct:
            apply_rolling(apct, arefs, build_yf_alt_index(
                yf_crypto_r, fb.get("supplies") or {}, tol=price_sanity_pct(fb)))
            alts_entry["pct"], alts_entry["refs"] = apct, arefs
            alts_entry["n"] = fb.get("n")
            alts_entry["src"] = ("Repli Yahoo — panier de {n} alts pondéré par les masses "
                                 "du {d} (≈{c}% de la mcap altcoins), CoinGecko indisponible"
                                 ).format(n=fb.get("n"), d=fb.get("updated", "?"), c=fb.get("cover_pct", "?"))
            alts_entry["fallback"] = "yahoo"
            notes["alts_repli"] = {
                "raison": "CoinGecko indisponible ce run (429 ou réseau)",
                "methode": "indice Σ supply_i × prix_i(t) — supplies figées au dernier run CoinGecko OK",
                "panier": list((fb.get("supplies") or {}).keys()),
                "couverture_pct": fb.get("cover_pct"),
                "supplies_du": fb.get("updated"),
                "caveat": "émission nette depuis les supplies non captée (< 1 pt sur 1 an)",
            }
            log(f"repli alts → indice Yahoo {fb.get('n')} coins (~{fb.get('cover_pct')}% mcap) : OK")
        else:
            log("repli alts → indice Yahoo : ÉCHEC (supplies ou séries absentes)")

    # 2bis) Fenêtres crypto 90d/180d : CoinGecko /markets ne les fournit pas → on
    # les calcule depuis la série quotidienne du run PRÉCÉDENT (moneyflow_series.json,
    # rafraîchie chaque run par build_series). Aucun appel CG supplémentaire.
    #  · BTC  = prix live (btc_entry.refs.last) / point série à −90j/−180j
    #  · alts = panier proxy (~25 coins), last série / point −90j/−180j (±1pt, cf courbe)
    try:
        scls = json.loads(SERIES_FILE.read_text()).get("classes", {})
    except Exception as e:
        scls = {}
        log(f"séries indisponibles pour 90d/180d crypto ({e})")
    # (En mode repli Yahoo, series_windows a déjà produit 90d/180d depuis la série
    #  daily 2 ans → on ne les réécrit pas depuis le proxy.)
    btc_last = (btc_entry.get("refs") or {}).get("last")
    for w, dd in CRYPTO_LONG_DAYS.items():
        if btc_entry.get("pct") is not None and not btc_entry.get("fallback"):
            p, ref = window_from_series(scls.get("btc", []), dd, last_px=btc_last)
            btc_entry["pct"][w] = p
            if ref is not None:
                btc_entry.setdefault("refs", {})[w] = round(ref, 2)
        if alts_entry.get("pct") is not None and not alts_entry.get("fallback"):
            p, ref = window_from_series(scls.get("alts", []), dd)   # proxy, pas de live
            alts_entry["pct"][w] = p

    classes.insert(0, btc_entry)   # ordre : btc, alts en tête (le JS trie de toute façon)
    classes.insert(1, alts_entry)

    # 3) Stablecoins (DefiLlama)
    spts = fetch_stables_series()
    st_entry = {"id": STABLE_META[0], "label": STABLE_META[1], "icon": STABLE_META[2],
                "src": STABLE_META[3], "asof": now_iso, "asof_ts": now_ts}
    if spts:
        pct, refs = series_windows(spts)
        if pct:
            st_entry["pct"] = pct
            st_entry["refs"] = {k: (round(v) if isinstance(v, (int, float)) else v) for k, v in refs.items()}
            st_entry["mcap"] = round(spts[-1][1])
    classes.append(st_entry)

    # 4) Merge-preserve : une classe sans pct récupère son entrée précédente
    prev = {}
    try:
        prev = {c["id"]: c for c in json.loads(CACHE_FILE.read_text()).get("classes", [])}
    except Exception:
        pass
    ok_count = 0
    for i, c in enumerate(classes):
        has_data = bool(c.get("pct")) and any(v is not None for v in c.get("pct", {}).values())
        if has_data:
            ok_count += 1
        elif c["id"] in prev and prev[c["id"]].get("pct"):
            old = prev[c["id"]]
            old["stale"] = True                      # asof d'origine conservé
            classes[i] = old
            log(f"classe {c['id']} ratée → valeurs précédentes conservées (asof {old.get('asof')})")
        else:
            log(f"classe {c['id']} SANS DONNÉES (ni actuelles ni précédentes)")
    if ok_count < 5:
        log(f"seulement {ok_count} classes fraîches → cache précédent conservé, pas d'écrasement")
        sys.exit(1)

    payload = {
        "updated": now_iso,
        "updated_ts": int(time.time()),
        "windows": WINDOWS,
        "classes": classes,
        "notes": notes,
        "sources": "Yahoo Finance · Hyperliquid · CoinGecko · DefiLlama — % en USD, réfs incluses (refs)",
        # Audit : pct[w] = refs.last_court / refs[w] − 1 pour 24H → 30J quand
        # last_court existe (Hyperliquid, contrat Yahoo ou horaire crypto), sinon
        # refs.last / refs[w] − 1 ; 3M → 1AN toujours refs.last / refs[w] − 1.
        "audit": "24H-30J : last_court/ref − 1 (si last_court) ; sinon last/ref − 1",
    }
    if hl:
        payload["notes"]["matieres_premieres"] = {
            "source": "Hyperliquid (HIP-3 xyz), bougies horaires, fenêtres glissantes",
            "marches": {t: h["coin"] for t, h in hl.items()},
            "pourquoi": "BZ=F/GC=F… Yahoo sautent d'une échéance à l'autre d'heure en heure ; "
                        "le perpétuel Hyperliquid glisse vers l'échéance suivante sur ~5 séances",
        }
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(CACHE_FILE)
    tmp2 = CACHE_JS.with_suffix(".js.tmp")
    tmp2.write_text("window.__MONEYFLOW__=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n")
    tmp2.replace(CACHE_JS)
    log(f"OK — {ok_count}/{len(classes)} classes fraîches · {CACHE_FILE.name} + .js écrits")

    # 5) Séries daily pour le mode courbe (fichier séparé, lazy-load)
    try:
        build_series(series, spts, alts, coins, yf_crypto_d, yf_crypto_h, contracts, hl)
    except Exception as e:
        log(f"build_series échec (courbe désactivée ce run) : {e}")

if __name__ == "__main__":
    main()
