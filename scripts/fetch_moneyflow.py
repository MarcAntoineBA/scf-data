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

import requests

warnings.filterwarnings("ignore")

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
PRICE_SANITY_PCT = 5.0      # écart max toléré Yahoo vs CoinGecko avant de jeter un ticker

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
    log(f"Yahoo crypto : daily {len(daily)}/{len(tk)} · horaire {len(hourly)}/{len(tk)}")
    return daily, hourly

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
    payload = {"ts": int(time.time()), "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "supplies": sup, "n": len(sup), "n_alts_total": len(alts),
               "cover_pct": round(basket_mc / total_mc * 100, 1)}
    tmp = FALLBACK_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(FALLBACK_FILE)

def load_fallback_supplies():
    try:
        return json.loads(FALLBACK_FILE.read_text())
    except Exception:
        return {}

def build_yf_alt_index(series_by_ticker, supplies, px_cg=None):
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
        if ref_px and abs(pts[-1][1] / ref_px - 1) * 100 > PRICE_SANITY_PCT:
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

def build_series(yahoo_series, spts, alts, coins, yf_crypto_d=None, yf_crypto_h=None):
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
    sup = (load_fallback_supplies().get("supplies") or {})
    idx_d = build_yf_alt_index(yf_crypto_d, sup)
    idx_h = build_yf_alt_index(yf_crypto_h, sup)
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
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "updated_ts": now_ts,
        "crypto_ts": crypto_ts,
        "days": SERIES_DAYS,
        "resolution": "horaire ~30j + quotidien ~1an (stables : quotidien)",
        "classes": out,
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
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M")
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
    for cid, ticker, label, icon, src in YAHOO_CLASSES:
        pct, refs = series_windows(series.get(ticker, []))
        entry = {"id": cid, "label": label, "icon": icon, "src": src, "asof": now_iso}
        if pct:
            entry["pct"], entry["refs"] = pct, refs
        classes.append(entry)

    # 2) CoinGecko : BTC + altcoins (+ filet Yahoo, fetché à chaque run)
    yf_crypto_d, yf_crypto_h = fetch_crypto_yahoo()
    coins = fetch_coingecko()
    ytd_bases, ytd_year = {}, None
    try:
        yc = json.loads(YTD_CACHE.read_text())
        if int(yc.get("year", 0)) == datetime.now(timezone.utc).year:
            ytd_bases, ytd_year = yc.get("base", {}), yc.get("year")
    except Exception as e:
        log(f"crypto_ytd_cache illisible ({e}) → YTD crypto indisponible")

    btc_entry = {"id": BTC_META[0], "label": BTC_META[1], "icon": BTC_META[2],
                 "src": BTC_META[3], "asof": now_iso}
    alts_entry = {"id": ALTS_META[0], "label": ALTS_META[1], "icon": ALTS_META[2],
                  "src": ALTS_META[3], "asof": now_iso}
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
            btc_entry["pct"], btc_entry["refs"] = bpct, brefs
            btc_entry["src"] = "Repli Yahoo BTC-USD (CoinGecko indisponible) — clôtures quotidiennes"
            btc_entry["fallback"] = "yahoo"
            log("repli BTC → Yahoo BTC-USD : OK")
        else:
            log("repli BTC → Yahoo BTC-USD : ÉCHEC (série absente)")
        aidx = build_yf_alt_index(yf_crypto_d, fb.get("supplies") or {})
        apct, arefs = series_windows(aidx)
        if apct:
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
                "src": STABLE_META[3], "asof": now_iso}
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
        "sources": "Yahoo Finance · CoinGecko · DefiLlama — % en USD, réfs incluses (refs)",
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
        build_series(series, spts, alts, coins, yf_crypto_d, yf_crypto_h)
    except Exception as e:
        log(f"build_series échec (courbe désactivée ce run) : {e}")

if __name__ == "__main__":
    main()
