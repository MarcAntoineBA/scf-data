#!/usr/bin/env python3
"""Fetch live valuation data for 10 Layer-1 blockchains.

Sources (all free, no auth required):
  - CoinGecko /coins/markets for MCap, FDV, price, circulating/total supply
  - DefiLlama /v2/chains for TVL per chain
  - DefiLlama /overview/fees/<chain> for annualized chain fees (total1y)

Writes to ~/Library/Caches/site_crypto_finance/ (non-TCC-protected):
  - l1_valuation_cache.json  (structured)
  - l1_valuation_cache.js    (window.__L1_VALUATION_LIVE__ = {...};)

Run by launchd (scf.l1valuation) every 4h.

Run manually: python3 fetch_l1_valuation.py [--force]
"""
import json
import re
import sys
import time
from pathlib import Path
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

CACHE_DIR  = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_JSON = CACHE_DIR / "l1_valuation_cache.json"
CACHE_JS   = CACHE_DIR / "l1_valuation_cache.js"
HYPE_NUSERS_SNAPSHOTS = CACHE_DIR / "hyperliquid_nusers_snapshots.json"
# Snapshots auto-accumulés des adresses actives chain-level pour les chaînes sans
# CoinMetrics gratuit (SOL/BNB/AVAX) : on persiste la valeur /chains activeUsers24h
# jour après jour pour reconstruire un historique honnête (cf. _accumulate_chain_active_users).
CHAIN_AA_SNAPSHOTS = CACHE_DIR / "chain_active_users_snapshots.json"
CACHE_MAX_HOURS = 4

TOKENS = [
    # token, coingecko_id, defillama_chain, blockchain_name
    ("BTC",    "bitcoin",          "Bitcoin",     "Bitcoin"),
    ("ETH",    "ethereum",         "Ethereum",    "Ethereum"),
    ("SOL",    "solana",           "Solana",      "Solana"),
    ("BNB",    "binancecoin",      "BSC",         "BNB Chain"),
    ("XRP",    "ripple",           "XRPL",        "XRP Ledger"),
    ("ADA",    "cardano",          "Cardano",     "Cardano"),
    ("AVAX",   "avalanche-2",      "Avalanche",   "Avalanche"),
    ("DOT",    "polkadot",         "Polkadot",    "Polkadot"),
    ("NEAR",   "near",             "Near",        "NEAR Protocol"),
    ("SUI",    "sui",              "Sui",         "Sui"),
    ("APT",    "aptos",            "Aptos",       "Aptos"),
    ("TON",    "the-open-network", "TON",         "TON"),
    ("TRX",    "tron",             "Tron",        "TRON"),
    ("HYPE",   "hyperliquid",      "Hyperliquid", "Hyperliquid"),
    ("TAO",    "bittensor",        "Bittensor",   "Bittensor"),
]

# ──────────────────────────────────────────────────────────────
# Staking data — snapshot + on-chain overrides
#
# StakingRewards.com est fully client-rendered depuis ~2026 — la page HTML SSR
# ne contient plus les valeurs spécifiques à l'asset (retourne un payload générique
# de top validators identique pour tous les slugs). Pas d'API gratuite publique.
#
# Stratégie :
#   1) On-chain API pour les chaînes qui en exposent (Solana via RPC).
#   2) Snapshot manuel `STAKING_FALLBACK` daté — à revalider ≥ 1×/trimestre.
#   3) Le scraping SR n'est plus appelé (cf. fetch_stakingrewards_scrape supprimée).
# ──────────────────────────────────────────────────────────────
STAKING_SNAPSHOT_DATE = "2026-04-19"  # Date de la dernière revalidation manuelle

STAKING_FALLBACK = {
    #          staking_apy %, inflation %, source_url
    "BTC":    (None,   1.68,  "bitcoin.org halving schedule"),
    "ETH":    (3.10,   0.55,  "stakingrewards.com/asset/ethereum-2-0"),
    "SOL":    (7.10,   4.60,  "stakingrewards.com/asset/solana"),
    "BNB":    (2.40,  -3.70,  "stakingrewards.com/asset/bnb (net of auto-burn)"),
    "XRP":    (None,   0.00,  "xrpl.org (no native staking, fixed supply)"),
    "ADA":    (2.70,   0.60,  "stakingrewards.com/asset/cardano"),
    "AVAX":   (5.30,   4.90,  "stakingrewards.com/asset/avalanche"),
    "DOT":    (13.80,  7.50,  "stakingrewards.com/asset/polkadot"),
    "NEAR":   (8.50,   4.70,  "stakingrewards.com/asset/near-protocol"),
    "SUI":    (2.80,   4.70,  "stakingrewards.com/asset/sui"),
    "APT":    (6.90,   5.40,  "stakingrewards.com/asset/aptos"),
    "TON":    (3.10,   0.60,  "stakingrewards.com/asset/the-open-network"),
    "TRX":    (4.50,   0.00,  "tronscan.org (delegated resources model)"),
    "HYPE":   (None,  12.00,  "hyperliquid.xyz (AF buyback model)"),
    "TAO":    (16.50,  7.20,  "stakingrewards.com/asset/bittensor"),
}

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) SiteCryptoFinance/1.0"


def http_get_json(url, timeout=20):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_coingecko():
    """Get MCap, FDV, price, volume, 24h change per token in one batched call."""
    ids = ",".join(t[1] for t in TOKENS)
    url = ("https://api.coingecko.com/api/v3/coins/markets"
           f"?vs_currency=usd&ids={ids}"
           "&order=market_cap_desc&per_page=50&page=1"
           "&sparkline=false&price_change_percentage=24h,7d,30d")
    try:
        data = http_get_json(url)
    except (URLError, HTTPError) as e:
        sys.stderr.write(f"[L1] CoinGecko error: {e}\n")
        return {}

    out = {}
    for coin in data:
        cid = coin.get("id")
        out[cid] = {
            "price_usd":    coin.get("current_price"),
            "mcap_usd":     coin.get("market_cap"),
            "fdv_usd":      coin.get("fully_diluted_valuation") or coin.get("market_cap"),
            "circ_supply":  coin.get("circulating_supply"),
            "total_supply": coin.get("total_supply"),
            "ath":          coin.get("ath"),
            "ath_chg_pct":  coin.get("ath_change_percentage"),
            "chg_24h":      coin.get("price_change_percentage_24h"),
            "chg_7d":       coin.get("price_change_percentage_7d_in_currency"),
            "chg_30d":      coin.get("price_change_percentage_30d_in_currency"),
            "volume_24h":   coin.get("total_volume"),
            "image":        coin.get("image"),
        }
    return out


def fetch_defillama_tvl():
    """Return chain → tvl_usd mapping."""
    try:
        data = http_get_json("https://api.llama.fi/v2/chains")
    except (URLError, HTTPError) as e:
        sys.stderr.write(f"[L1] DefiLlama /v2/chains error: {e}\n")
        return {}

    out = {}
    for chain in data:
        name = chain.get("name")
        if name:
            out[name] = chain.get("tvl") or 0.0
            # Also store by chainId-like aliases
            out[name.lower()] = chain.get("tvl") or 0.0

    # Cas spécial Hyperliquid : /v2/chains sépare "Hyperliquid L1" (~$1.4B) du bridge Arbitrum
    # (~$3.2B) qui détient le collateral utilisateurs. Le TVL "économique" du protocole agrège
    # tout via /tvl/hyperliquid (~$5B). C'est la valeur comparable aux autres L1.
    try:
        hyperliquid_tvl = http_get_json("https://api.llama.fi/tvl/hyperliquid")
        if isinstance(hyperliquid_tvl, (int, float)) and hyperliquid_tvl > 0:
            out["Hyperliquid"] = float(hyperliquid_tvl)
            out["hyperliquid"] = float(hyperliquid_tvl)
            sys.stderr.write(f"[L1] HYPE TVL via /tvl/hyperliquid (protocol aggregate): ${hyperliquid_tvl/1e9:.2f}B\n")
    except (URLError, HTTPError) as e:
        sys.stderr.write(f"[L1] Hyperliquid /tvl aggregate error: {e}\n")

    return out


def fetch_defillama_fees(chain_slug, retries=3):
    """Fetch annualized fees for a given chain from DefiLlama.

    Returns fees in USD (annualized from total30d × 12 or total1y if available).
    Falls back to None on failure. Bitcoin has no traditional 'fees' aggregation here,
    so we fall back to miner revenue heuristic later.

    Retries on transient errors (timeout, 5xx, network blips) with exponential
    backoff to avoid flaking the cache to None — DefiLlama's CDN occasionally
    returns 502/timeout and a single shot was clobbering valid cached values.
    """
    url = (f"https://api.llama.fi/overview/fees/{chain_slug}"
           "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true")
    data = None
    for attempt in range(retries):
        try:
            data = http_get_json(url, timeout=20)
            break
        except HTTPError as e:
            if e.code == 404:
                return None
            if attempt < retries - 1:
                sys.stderr.write(f"[L1] DefiLlama fees/{chain_slug} HTTP {e.code} (retry {attempt+1}/{retries})\n")
                time.sleep(2 ** (attempt + 1))
                continue
            sys.stderr.write(f"[L1] DefiLlama fees/{chain_slug} HTTP {e.code} (final)\n")
            return None
        except Exception as e:
            if attempt < retries - 1:
                sys.stderr.write(f"[L1] DefiLlama fees/{chain_slug} {type(e).__name__} (retry {attempt+1}/{retries})\n")
                time.sleep(2 ** (attempt + 1))
                continue
            sys.stderr.write(f"[L1] DefiLlama fees/{chain_slug} {type(e).__name__} (final)\n")
            return None

    if data is None:
        return None

    total_1y = data.get("total1y")
    if total_1y:
        return float(total_1y)
    total_30d = data.get("total30d")
    if total_30d:
        return float(total_30d) * 12.0
    total_7d = data.get("total7d")
    if total_7d:
        return float(total_7d) * 52.0
    return None


# ──────────────────────────────────────────────────────────────
# Active addresses — routage des sources (toutes gratuites, sans clé)
#
# SOURCE PRIMAIRE = CoinMetrics community AdrActCnt (adresses uniques actives
# on-chain, chain-level, 7+ ans d'historique, frais quotidiennement). C'est la
# métrique la plus homogène disponible. Couverture community gratuite : BTC, ETH,
# ADA, TRX, XRP (SOL/BNB/AVAX/ATOM/NEAR/DOT renvoient 400/403/empty = payant).
#
# SOURCE SECONDAIRE = DefiLlama /chains activeUsers24h (valeur courante chain-level,
# même ordre de grandeur que CoinMetrics — cf. BTC 663k DLL ≈ 642k CM, ADA identique)
# pour les chaînes sans CoinMetrics gratuit : SOL, BNB, AVAX. Pas d'historique exposé
# par /chains → on auto-accumule la valeur jour après jour (_accumulate_chain_active_users).
#
# ⚠ NE PAS utiliser /overview/active-users/<chain> : cet endpoint adaptateur agrège
# les users par protocole DeFi et renvoie des comptages incohérents entre chaînes
# (ETH 6k vs /chains 513k ; AVAX 228 vs 707k) — il s'est aussi vidé (empty) pour
# BTC/SOL/ADA/TRX mi-2026, ce qui figeait faussement le KPI. Audit 2026-06-17.
# ──────────────────────────────────────────────────────────────
# tok → asset id CoinMetrics (= source primaire des adresses actives)
COINMETRICS_ASSET_ID = {
    "BTC": "btc", "ETH": "eth", "XRP": "xrp", "ADA": "ada", "TRX": "trx",
}

# DefiLlama expose activeUsers24h par chaîne dans le payload Next.js de /chains.
# On fetch le buildId dynamiquement puis le .json pour obtenir les données live.
DEFILLAMA_CHAIN_NAME = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "BNB": "BSC",
    "ADA": "Cardano",
    "AVAX": "Avalanche",
    "TRX": "Tron",
    "HYPE": "Hyperliquid L1",
}

# Chaînes sans CoinMetrics gratuit : KPI via /chains activeUsers24h + historique
# auto-accumulé. Le nom = clé chaîne dans le payload DLL /chains.
# HYPE inclus : le feed Hyperliquid Foundation CloudFront (daily_unique_users du perp
# DEX) est gelé en amont depuis avril 2026 ; /chains « Hyperliquid L1 » donne les
# adresses actives on-chain L1 en frais (audit 2026-06-17). Métrique différente
# (couche L1 vs traders perp) mais LIVE, cohérente avec les autres chaînes.
DLL_ACCUM_CHAINS = {"SOL": "Solana", "BNB": "BSC", "AVAX": "Avalanche", "HYPE": "Hyperliquid L1"}
_DLL_AA_CACHE = None  # lazy-loaded dict {chain_name: activeUsers24h}

def _fetch_defillama_active_users():
    """Fetch & parse DefiLlama's /chains page Next.js payload to extract
    activeUsers24h per chain. Returns {chain_name: int} or {} on failure.

    Cached in module-level `_DLL_AA_CACHE` pour ne faire qu'un seul appel par run.
    """
    global _DLL_AA_CACHE
    if _DLL_AA_CACHE is not None:
        return _DLL_AA_CACHE

    # Cloudflare challenge the JSON endpoint with minimal UA. Il faut envoyer
    # un set complet de headers type Safari + Referer DLL pour que le challenge soit
    # bypassé (déjà testé, fonctionne sans nécessiter de cookie).
    _DLL_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://defillama.com/chains",
    }
    try:
        # Step 1: get buildId from the HTML page (Safari UA + text/html Accept)
        html_headers = {**_DLL_HEADERS, "Accept": "text/html"}
        req = Request("https://defillama.com/chains", headers=html_headers)
        with urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="ignore")
        import re as _re
        m = _re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
        if not m:
            sys.stderr.write("[L1] DefiLlama: buildId not found\n")
            _DLL_AA_CACHE = {}
            return _DLL_AA_CACHE
        build_id = m.group(1)

        # Step 2: fetch the structured JSON with the SAME headers
        url = f"https://defillama.com/_next/data/{build_id}/chains.json"
        req = Request(url, headers=_DLL_HEADERS)
        with urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        chains = data.get("pageProps", {}).get("chains", [])
        _DLL_AA_CACHE = {c.get("name"): c.get("activeUsers24h")
                         for c in chains if c.get("activeUsers24h") is not None}
        sys.stderr.write(f"[L1] DefiLlama activeUsers: {len(_DLL_AA_CACHE)} chains loaded\n")
        return _DLL_AA_CACHE
    except Exception as e:
        sys.stderr.write(f"[L1] DefiLlama chains payload: {type(e).__name__}\n")
        _DLL_AA_CACHE = {}
        return _DLL_AA_CACHE


def _fetch_dll_aa(tok):
    """Return activeUsers24h from DLL for a given token, or None."""
    chain_name = DEFILLAMA_CHAIN_NAME.get(tok)
    if not chain_name:
        return None
    d = _fetch_defillama_active_users()
    return d.get(chain_name)

# ────────────────────────────────────────────────────────────────
# Snapshots manuels pour les L1 sans source publique gratuite.
# Source : transparency reports + Discord announcements + agrégateurs
# community (ASXN, Dune dashboards). Revalidation trimestrielle recommandée
# — les chiffres dérivent avec l'adoption.
# ────────────────────────────────────────────────────────────────
ACTIVE_ADDR_SNAPSHOT_DATE = "2026-04-24"
# Snapshots retirés — seules les sources LIVE sont affichées désormais.
# Les chaînes sans API publique gratuite (DOT/SUI/APT/TON/TAO) restent en N/A.
ACTIVE_ADDR_MANUAL_SNAPSHOT = {}

def _fetch_cm_aa(aid):
    """CoinMetrics Community AdrActCnt → 7d avg. Returns None on failure."""
    from datetime import datetime, timedelta
    d_end = datetime.now().strftime("%Y-%m-%d")
    d_start = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
    url = (f"https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
           f"?assets={aid}&metrics=AdrActCnt&frequency=1d"
           f"&start_time={d_start}&end_time={d_end}&page_size=10")
    try:
        data = http_get_json(url, timeout=10)
    except Exception as e:
        sys.stderr.write(f"[L1] CoinMetrics AdrActCnt/{aid}: {type(e).__name__}\n")
        return None
    vals = [float(p["AdrActCnt"]) for p in data.get("data", []) if p.get("AdrActCnt")]
    return round(sum(vals)/len(vals)) if vals else None


def _fetch_near_active_accounts():
    """NEAR via Nearblocks public API. Returns 7-day avg of active_accounts."""
    try:
        data = http_get_json("https://api.nearblocks.io/v1/charts?type=active_accounts", timeout=10)
    except Exception as e:
        sys.stderr.write(f"[L1] Nearblocks charts: {type(e).__name__}\n")
        return None
    charts = data.get("charts", [])
    if not charts:
        return None
    recent = charts[-7:]
    vals = [int(e["active_accounts"]) for e in recent if e.get("active_accounts")]
    return round(sum(vals)/len(vals)) if vals else None


def _fetch_dll_active_users_history(chain_name, days=3000):
    """⚠ DÉPRÉCIÉ (audit 2026-06-17) — NE PLUS BRANCHER. Endpoint adaptateur DeFi
    /overview/active-users : comptages incohérents entre chaînes (ETH 6k vs /chains
    513k ; AVAX 228 vs 707k) et vidé pour BTC/SOL/ADA/TRX. Conservé pour référence.
    Les adresses actives passent désormais par CoinMetrics + /chains activeUsers24h.

    Fetch daily active users history from DefiLlama for a chain.
    Returns list of [timestamp_ms, value] over last `days` days, or None.

    Profondeur : DLL expose jusqu'à 8+ ans pour BTC/ETH, 5+ ans pour SOL/BNB/ADA/AVAX,
    qq mois seulement pour TRX (backfill récent). days=3000 ≈ 8.2 ans, donc on prend
    tout ce qui existe.
    """
    url = f"https://api.llama.fi/overview/active-users/{chain_name}?dataType=dailyActiveUsers"
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15",
            "Referer": "https://defillama.com/",
        })
        with urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
        chart = data.get("totalDataChart") or []
        chart = chart[-days:]
        if not chart:
            return None

        # Filtre anti-backfill : coupe les points <1% du max (artefacts de rollout
        # du tracking quand DLL n'avait pas encore la métrique).
        values = [v for _, v in chart if v is not None]
        if not values:
            return None
        threshold = max(values) * 0.01
        first_valid = 0
        for i, (_, v) in enumerate(chart):
            if v is not None and v >= threshold:
                first_valid = i
                break
        chart = chart[first_valid:]
        return [[int(t) * 1000, v] for t, v in chart]
    except Exception as e:
        sys.stderr.write(f"[L1] DLL active-users history {chain_name}: {type(e).__name__}\n")
        return None


# CloudFront feed alimentant la stats page de la Hyperliquid Foundation
# (utilisé aussi par le homepage du site dans index.Rmd).
HL_DAU_URL = "https://d2v1fiwobg9w6.cloudfront.net/daily_unique_users"


_CM_HIST_CACHE = {}  # {asset: [[ts_ms, val], ...]} — mémoïse l'historique CoinMetrics par run

def _fetch_coinmetrics_active_addresses(asset, start="2017-01-01"):
    """CoinMetrics community API → daily active addresses pour un asset.
    Returns list[[ts_ms, value]] ou None.

    Source primaire des adresses actives pour BTC/ETH/ADA/TRX/XRP : `AdrActCnt` =
    nombre d'adresses uniques ayant été actives on-chain dans la journée, métrique
    chain-level homogène avec 7+ ans d'historique, rafraîchie quotidiennement.
    Mémoïsé par run (appelé pour le KPI ET l'historique de chaque chaîne).
    """
    if asset in _CM_HIST_CACHE:
        return _CM_HIST_CACHE[asset]
    url = (f"https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
           f"?assets={asset}&metrics=AdrActCnt&frequency=1d"
           f"&start_time={start}&page_size=10000")
    try:
        from datetime import datetime as _dt, timezone as _tz
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh)",
            "Accept": "application/json",
        })
        with urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
        rows = data.get("data") or []
        out = []
        for row in rows:
            t = row.get("time")
            v = row.get("AdrActCnt")
            if not t or v is None:
                continue
            try:
                dt = _dt.fromisoformat(t.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
                ts_ms = int(dt.timestamp() * 1000)
                out.append([ts_ms, int(float(v))])
            except Exception:
                continue
        result = out or None
        if result is not None:
            _CM_HIST_CACHE[asset] = result
        return result
    except Exception as e:
        sys.stderr.write(f"[L1] CoinMetrics {asset} AdrActCnt: {type(e).__name__}\n")
        return None


def _accumulate_chain_active_users(chain_name, current_val, seed_hist=None):
    """Historique auto-accumulé des adresses actives pour les chaînes sans CoinMetrics
    gratuit (SOL/BNB/AVAX). DefiLlama /chains n'expose que la valeur courante
    (activeUsers24h), pas de série temporelle — on persiste donc un point par jour
    dans un sidecar et on reconstruit l'historique au fil de l'eau.

    Returns list[[ts_ms, val]] triée, ou None.

    Amorçage : si le sidecar est vide pour cette chaîne et qu'on dispose d'un
    historique précédent (seed_hist, ex. l'ancien historique SOL du cache), on
    l'utilise comme socle — MAIS uniquement si son échelle correspond à la valeur
    /chains actuelle (médiane récente dans [0.2×, 5×] de current_val). Ce garde-fou
    écarte automatiquement les vieilles séries à la mauvaise échelle (BNB/AVAX
    provenaient de l'adaptateur DeFi, ~25–3000× trop bas) tout en conservant SOL
    (déjà chain-level, ~1.7M cohérent avec /chains 1.87M). Règle simple et auto-correctrice.
    """
    from datetime import datetime as _dt, timezone as _tz
    today = _dt.now(_tz.utc).date().isoformat()
    today_ts = int(_dt.fromisoformat(today).replace(tzinfo=_tz.utc).timestamp() * 1000)

    # Charger le sidecar
    doc = {}
    if CHAIN_AA_SNAPSHOTS.exists():
        try:
            with CHAIN_AA_SNAPSHOTS.open("r") as f:
                doc = json.load(f) or {}
        except Exception as e:
            sys.stderr.write(f"[L1] chain AA snapshots read: {type(e).__name__}\n")
            doc = {}
    if not isinstance(doc, dict):
        doc = {}

    series = doc.get(chain_name) or []

    # Amorçage one-shot si série vide et seed à la bonne échelle
    if not series and seed_hist and current_val:
        recent = [v for _, v in seed_hist[-5:] if v]
        if recent:
            med = sorted(recent)[len(recent) // 2]
            if med and 0.2 * current_val <= med <= 5 * current_val:
                series = [list(p) for p in seed_hist]
                sys.stderr.write(f"[L1] {chain_name} AA history seeded from previous cache "
                                 f"({len(series)} pts, scale OK vs /chains {current_val})\n")
            else:
                sys.stderr.write(f"[L1] {chain_name} AA seed discarded (median {med} vs /chains "
                                 f"{current_val}, scale mismatch) — starting fresh\n")

    # Append/replace le point du jour
    if current_val is not None:
        series = [p for p in series if p[0] != today_ts]
        series.append([today_ts, int(current_val)])
        series.sort(key=lambda p: p[0])
        doc[chain_name] = series
        try:
            with CHAIN_AA_SNAPSHOTS.open("w") as f:
                json.dump(doc, f)
        except Exception as e:
            sys.stderr.write(f"[L1] chain AA snapshots write: {type(e).__name__}\n")

    return series or None


def _fetch_hyperliquid_dau_history():
    """Hyperliquid Foundation CloudFront feed → daily unique users history.
    Returns list of [timestamp_ms, value] depuis 2023-06-13 (launch HYPE),
    ou None.

    Schéma renvoyé par l'API : {chart_data: [{time, daily_unique_users}, ...]}.
    Le feed peut traîner d'un mois selon la fréquence de pousse du pipeline asxn.
    """
    try:
        req = Request(HL_DAU_URL, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15",
            "Accept": "application/json",
        })
        with urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
        cd = data.get("chart_data") or []
        if not cd:
            return None
        from datetime import datetime as _dt, timezone as _tz
        out = []
        for p in cd:
            t = p.get("time")
            v = p.get("daily_unique_users")
            if not t or v is None:
                continue
            try:
                # Le feed renvoie "YYYY-MM-DDT00:00:00" sans timezone → forcer UTC
                # (sinon .timestamp() interprète en heure locale = décalage 1-2h selon DST).
                dt = _dt.fromisoformat(t.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
                ts_ms = int(dt.timestamp() * 1000)
            except Exception:
                continue
            out.append([ts_ms, int(v)])
        return out or None
    except Exception as e:
        sys.stderr.write(f"[L1] Hyperliquid DAU history: {type(e).__name__}\n")
        return None


def _fetch_nearblocks_history(days=90):
    """Return list of [timestamp_ms, active_accounts] for NEAR."""
    try:
        data = http_get_json("https://api.nearblocks.io/v1/charts?type=active_accounts", timeout=15)
    except Exception as e:
        sys.stderr.write(f"[L1] Nearblocks history: {type(e).__name__}\n")
        return None
    from datetime import datetime
    charts = data.get("charts", [])[-days:]
    out = []
    for e in charts:
        if not e.get("active_accounts"): continue
        try:
            ts = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            out.append([int(ts.timestamp() * 1000), int(e["active_accounts"])])
        except Exception:
            continue
    return out or None


def _fetch_cm_history(asset_id, days=90):
    """CoinMetrics AdrActCnt daily time series. Returns [[ts_ms, value], ...]."""
    from datetime import datetime, timedelta
    d_end = datetime.now().strftime("%Y-%m-%d")
    d_start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    url = (f"https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
           f"?assets={asset_id}&metrics=AdrActCnt&frequency=1d"
           f"&start_time={d_start}&end_time={d_end}&page_size=200")
    try:
        data = http_get_json(url, timeout=15)
    except Exception as e:
        sys.stderr.write(f"[L1] CoinMetrics history {asset_id}: {type(e).__name__}\n")
        return None
    out = []
    for p in data.get("data", []):
        v = p.get("AdrActCnt")
        t = p.get("time")
        if not v or not t: continue
        try:
            from datetime import datetime as _dt
            ts = _dt.fromisoformat(t.replace("Z", "+00:00"))
            out.append([int(ts.timestamp() * 1000), int(float(v))])
        except Exception:
            continue
    return out or None


def _fetch_price_history(cg_id, days=90):
    """Price history via DefiLlama coins.llama.fi/chart (free, pas de rate limit).
    Retourne [[ts_ms, price_usd], ...] ou None.
    """
    from datetime import datetime as _dt, timedelta as _td
    start_ts = int((_dt.now() - _td(days=days+1)).timestamp())
    url = f"https://coins.llama.fi/chart/coingecko:{cg_id}?start={start_ts}&span={days}&period=1d"
    try:
        data = http_get_json(url, timeout=15)
    except Exception as e:
        sys.stderr.write(f"[L1] DLL price history {cg_id}: {type(e).__name__}\n")
        return None
    coins = data.get("coins", {})
    if not coins:
        return None
    series = list(coins.values())[0].get("prices") or []
    return [[int(p["timestamp"]) * 1000, round(float(p["price"]), 6)] for p in series] or None


def _accumulate_hyperliquid_nusers():
    """Capture le nUsers cumulatif live depuis api.hyperliquid.xyz et persist
    en sidecar (un snapshot par jour, le plus récent gagne).
    Returns la liste à jour [{date, nUsers}, ...] triée par date.

    Pourquoi : le pipeline asxn (CloudFront) gele plusieurs semaines à la fois.
    En accumulant nUsers (cumulatif, fresh, source officielle Hyperliquid Foundation)
    on dérive ensuite une série "new accounts/day" complémentaire au DAU CloudFront.
    """
    from datetime import datetime as _dt, timezone as _tz
    today = _dt.now(_tz.utc).date().isoformat()

    # Lire l'existant
    snapshots = []
    if HYPE_NUSERS_SNAPSHOTS.exists():
        try:
            with HYPE_NUSERS_SNAPSHOTS.open("r") as f:
                doc = json.load(f)
                snapshots = doc.get("snapshots", []) if isinstance(doc, dict) else []
        except Exception as e:
            sys.stderr.write(f"[L1] HYPE nUsers snapshots read: {type(e).__name__}\n")

    # Capter le live
    live_nusers = _fetch_hyperliquid_global_stats()
    if live_nusers is not None:
        # Update or insert today's entry
        existing = next((s for s in snapshots if s.get("date") == today), None)
        if existing:
            existing["nUsers"] = live_nusers
            existing["captured_at"] = _dt.now(_tz.utc).isoformat()
        else:
            snapshots.append({
                "date": today,
                "nUsers": live_nusers,
                "captured_at": _dt.now(_tz.utc).isoformat(),
            })
        snapshots.sort(key=lambda s: s.get("date", ""))
        try:
            with HYPE_NUSERS_SNAPSHOTS.open("w") as f:
                json.dump({"snapshots": snapshots}, f, indent=2)
        except Exception as e:
            sys.stderr.write(f"[L1] HYPE nUsers snapshots write: {type(e).__name__}\n")

    return snapshots


def _build_hyperliquid_new_users_series(cloudfront_dau_history):
    """Construit la série "new accounts/day" pour HYPE en combinant :
      1. cumulative_new_users CloudFront jusqu'au freeze (frozen, mais riche)
      2. interpolation linéaire sur le gap (CloudFront freeze → premier snapshot local)
      3. snapshots nUsers locaux (frais, accumulés à chaque refresh)

    Returns list[[ts_ms, daily_new_users]] ou None.

    Pourquoi cette approche : daily_new_users est différent du DAU mais c'est
    une métrique légitime d'adoption. La série CloudFront cumulative_new_users
    est gelée comme le DAU, mais le live nUsers de l'info API est frais —
    on dérive donc daily_new_users = nUsers(j) − nUsers(j-1) à partir des snapshots.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta

    # 1. Récupérer la série historique cumulative_new_users CloudFront
    try:
        req = Request("https://d2v1fiwobg9w6.cloudfront.net/cumulative_new_users",
                      headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urlopen(req, timeout=20) as r:
            cf_data = json.loads(r.read().decode())
        cf_chart = cf_data.get("chart_data", []) or []
    except Exception as e:
        sys.stderr.write(f"[L1] HYPE cumulative_new_users fetch: {type(e).__name__}\n")
        return None

    # Construire un dict {date: cumulative_new_users}
    cumulative_by_date = {}
    for p in cf_chart:
        t = p.get("time")
        v = p.get("cumulative_new_users")
        if not t or v is None:
            continue
        try:
            dt = _dt.fromisoformat(t.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            cumulative_by_date[dt.date().isoformat()] = int(v)
        except Exception:
            continue

    if not cumulative_by_date:
        return None

    # 2. Append les snapshots locaux nUsers
    snapshots = []
    if HYPE_NUSERS_SNAPSHOTS.exists():
        try:
            with HYPE_NUSERS_SNAPSHOTS.open("r") as f:
                doc = json.load(f)
                snapshots = doc.get("snapshots", []) if isinstance(doc, dict) else []
        except Exception:
            pass

    for s in snapshots:
        d = s.get("date")
        n = s.get("nUsers")
        if d and n is not None:
            # Local snapshots écrasent CloudFront seulement si plus récent
            cf_last = max(cumulative_by_date.keys())
            if d > cf_last:
                cumulative_by_date[d] = int(n)

    # 3. Interpoler le gap entre CloudFront freeze et premier snapshot local
    sorted_dates = sorted(cumulative_by_date.keys())
    if len(sorted_dates) < 2:
        return None

    # Identifier les gaps > 1j (typiquement le gap CloudFront → snapshots locaux)
    filled = dict(cumulative_by_date)
    for i in range(len(sorted_dates) - 1):
        d1 = _dt.fromisoformat(sorted_dates[i]).date()
        d2 = _dt.fromisoformat(sorted_dates[i+1]).date()
        days_between = (d2 - d1).days
        if days_between > 1:
            v1 = cumulative_by_date[sorted_dates[i]]
            v2 = cumulative_by_date[sorted_dates[i+1]]
            slope = (v2 - v1) / days_between
            for k in range(1, days_between):
                interp_d = (d1 + timedelta(days=k)).isoformat()
                if interp_d not in filled:
                    filled[interp_d] = int(v1 + slope * k)

    # 4. Dériver daily_new_users series
    sorted_filled = sorted(filled.keys())
    out = []
    for i in range(1, len(sorted_filled)):
        d = sorted_filled[i]
        prev = sorted_filled[i-1]
        delta = filled[d] - filled[prev]
        if delta < 0:
            continue  # safety, shouldn't happen with cumulative
        try:
            ts_ms = int(_dt.fromisoformat(d).replace(tzinfo=_tz.utc).timestamp() * 1000)
            out.append([ts_ms, int(delta)])
        except Exception:
            continue
    return out or None


def fetch_active_addresses_history(tok, cg_id, seed_hist=None):
    """Return time series of daily active addresses for a token.

    Sources (cf. routage en tête de fichier) :
      - BTC/ETH/ADA/TRX/XRP via CoinMetrics AdrActCnt (chain-level, 7+ ans, frais).
      - SOL/BNB/AVAX/HYPE via DefiLlama /chains activeUsers24h auto-accumulé (pas de
        CoinMetrics gratuit ; /chains ne donne que la valeur courante → on persiste
        un point/jour). seed_hist amorce SOL depuis l'ancien historique du cache.
    """
    aid = COINMETRICS_ASSET_ID.get(tok)
    if aid:
        return _fetch_coinmetrics_active_addresses(aid)
    chain_name = DLL_ACCUM_CHAINS.get(tok)
    if chain_name:
        # HYPE bascule d'une métrique (perp-DEX CloudFront, gelé) à une autre (L1
        # on-chain) : ne pas amorcer depuis l'ancien historique (échelle/définition
        # différentes) → repart à zéro comme BNB/AVAX, réémergera après 30j.
        seed = None if tok == "HYPE" else seed_hist
        return _accumulate_chain_active_users(chain_name, _fetch_dll_aa(tok), seed)
    return None


def _fetch_hyperliquid_global_stats():
    """Hyperliquid via api.hyperliquid.xyz/info POST {type:'globalStats'}.

    Source directe utilisée par la homepage hyperfoundation.org — gratuite,
    sans clé. Renvoie :
      {totalVolume: $T cumulé, dailyVolume: $ jour, nUsers: total unique users ever}

    Attention sémantique : `nUsers` est le nombre CUMULÉ d'utilisateurs uniques
    depuis le lancement — pas un DAU (Daily Active Users). Différent des autres
    chaînes du framework qui affichent leur DAU moyen 7j. À interpréter comme
    une mesure d'adoption totale historique, pas d'activité quotidienne.
    """
    import json as _json
    try:
        from urllib.request import Request as _Req, urlopen as _open
        req = _Req("https://api.hyperliquid.xyz/info",
                   data=_json.dumps({"type": "globalStats"}).encode(),
                   headers={"Content-Type": "application/json",
                            "Accept": "application/json",
                            "User-Agent": UA},
                   method="POST")
        with _open(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode())
        return int(data.get("nUsers")) if data.get("nUsers") is not None else None
    except Exception as e:
        sys.stderr.write(f"[L1] Hyperliquid globalStats: {e}\n")
        return None


def fetch_active_addresses(tok):
    """Return (value, source_label) for active addresses or (None, "unavailable").

    Sources (cf. routage en tête de fichier) :
      - CoinMetrics AdrActCnt pour BTC/ETH/ADA/TRX/XRP (dernière valeur de la série)
      - DefiLlama /chains activeUsers24h pour SOL/BNB/AVAX/HYPE.

    Le runtime recalcule ensuite la moyenne 7j à partir de l'historique pour homogénéité.
    """
    aid = COINMETRICS_ASSET_ID.get(tok)
    if aid:
        hist = _fetch_coinmetrics_active_addresses(aid)
        if hist:
            return hist[-1][1], "CoinMetrics community AdrActCnt (live)"
        return None, "unavailable"
    if tok not in DLL_ACCUM_CHAINS:
        return None, "unavailable"
    v = _fetch_dll_aa(tok)
    if v is not None:
        return v, "DefiLlama /chains activeUsers24h (live, unique users last 24h)"
    return None, "unavailable"


# BTC fees = annualized miner fees (not Lightning). Approximate from blockchain.info daily fees × 365.
def fetch_btc_fees():
    try:
        # 30-day rolling average of miner fees (BTC), converted to USD
        data = http_get_json("https://api.blockchain.info/charts/transaction-fees-usd"
                             "?timespan=30days&format=json&sampled=false")
        values = data.get("values", [])
        if not values:
            return None
        avg_daily_usd = sum(v.get("y", 0) for v in values) / len(values)
        return avg_daily_usd * 365.0
    except Exception as e:
        sys.stderr.write(f"[L1] BTC fees fallback error: {e}\n")
        return None


def fetch_dot_fees():
    """Try DefiLlama first. If it fails (Polkadot returns HTTP 500 on /overview/fees),
    return None rather than a stale hardcoded snapshot — honnêteté épistémique.
    DOT sera alors absent du chart P/S et N/A dans la scorecard.
    """
    v = fetch_defillama_fees("Polkadot")
    if v is None:
        sys.stderr.write("[L1] DOT fees: DefiLlama unavailable, marking as N/A\n")
    return v


# ──────────────────────────────────────────────────────────────────
# Staking rates
# StakingRewards.com scraping retiré (fully client-rendered depuis ~2026, la page
# HTML SSR retourne le même payload générique quelle que soit l'asset requesté).
# Audit 2026-04-23. Source canonique = snapshot manuel STAKING_FALLBACK + on-chain
# pour Solana (getInflationRate). Revalidation trimestrielle du snapshot recommandée.
# ──────────────────────────────────────────────────────────────────


def fetch_solana_onchain_inflation():
    """Solana inflation rate via public RPC. Returns (staker_apr, inflation_rate) %.

    getInflationRate renvoie `total` = inflation annuelle totale et `validator` =
    part versée aux validators (~95% de `total`). Le vrai APR d'un staker = reward
    validator × (1 / staking_ratio) car les récompenses sont réparties entre les SOL
    stakés (pas la totalité du supply). On récupère aussi `getInflationReward` /
    `getEpochInfo` serait trop lourd. Approximation : staker_apr = validator / 0.65
    (≈ 65% du supply est staké, chiffre typique 2024-2026).
    """
    try:
        req_body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "getInflationRate"
        }).encode("utf-8")
        req = Request("https://api.mainnet-beta.solana.com",
                      data=req_body,
                      headers={"Content-Type": "application/json", "User-Agent": UA})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        total = data.get("result", {}).get("total")
        validator = data.get("result", {}).get("validator")
        if total is not None and validator is not None:
            STAKING_RATIO = 0.65  # ~65% of SOL supply is staked (stable 2024-2026)
            staker_apr = (validator * 100.0) / STAKING_RATIO
            return (staker_apr, total * 100.0)
    except Exception as e:
        sys.stderr.write(f"[STK] Solana RPC: {e}\n")
    return None, None


def fetch_ethereum_staking_apy_beaconcha():
    """beaconcha.in exposes a free apr endpoint."""
    try:
        data = http_get_json("https://beaconcha.in/api/v1/epoch/latest", timeout=10)
        # APR is in data['data']['rewardsexported'] — imprecise, use fallback
        # beaconcha.in rate-limits aggressively; skip for now
        return None, None
    except Exception:
        return None, None


def resolve_staking(tok):
    """Return (apy, inflation, source_label).

    Priorité :
    1) On-chain API pour Solana (getInflationRate → validator inflation / staking_ratio).
    2) Snapshot manuel daté (STAKING_FALLBACK, revu STAKING_SNAPSHOT_DATE).
    """
    # 1) On-chain fallbacks for specific chains
    if tok == "SOL":
        apy, infl = fetch_solana_onchain_inflation()
        if apy is not None and infl is not None:
            return apy, infl, "api.mainnet-beta.solana.com getInflationRate [on-chain live]"

    # 2) Manual snapshot (cf. STAKING_SNAPSHOT_DATE)
    fb = STAKING_FALLBACK.get(tok)
    if fb:
        return fb[0], fb[1], f"{fb[2]} [snapshot {STAKING_SNAPSHOT_DATE}]"
    return None, None, "unavailable"


def _cache_age_hours():
    """Âge de la donnée, lu DANS le cache — et seulement à défaut sur le fichier.

    POURQUOI PAS LA DATE DU FICHIER (panne du 2026-08-04, vue le 2026-08-20)
    Depuis que la collecte tourne sur un serveur neuf à chaque passage, le cache
    précédent y est RESTITUÉ avant l'exécution — ce qui est juste, c'est la base de
    fusion du collecteur. Mais il arrive avec la date de sa copie. La garde
    « moins de 4 h » se refermait donc à chaque fois : ce collecteur est sorti en
    succès sans écrire pendant seize jours, et le site a servi un P/S figé au 04/08
    sous une étiquette « à jour ». La date écrite DANS le fichier, elle, voyage avec
    lui : c'est celle que le visiteur voit, donc celle qui doit décider.
    """
    if not CACHE_JSON.exists():
        return None
    try:
        with open(CACHE_JSON, "rb") as f:
            tete = f.read(65536).decode("utf-8", "replace")
    except OSError:
        tete = ""
    m = re.search(r'"updated_unix"\s*:\s*(\d{9,13})', tete)
    if m:
        return (time.time() - float(m.group(1))) / 3600
    m = re.search(r'"updated_iso"\s*:\s*"([^"]{10,40})"', tete)
    if m:
        try:
            return (time.time() - datetime.fromisoformat(m.group(1)).timestamp()) / 3600
        except ValueError:
            pass
    # Aucune date lisible (cache d'avant ce correctif) : la date du fichier reste
    # un repli honnête sur la machine d'origine, où le fichier n'est jamais déplacé.
    try:
        return (time.time() - CACHE_JSON.stat().st_mtime) / 3600
    except OSError:
        return None


def is_fresh():
    age_hours = _cache_age_hours()
    return age_hours is not None and age_hours < CACHE_MAX_HOURS


PRESERVE_FIELDS = ("fees_m", "tvl_b", "active_addresses_7d_avg",
                   "active_addresses_history")
PRESERVE_MAX_HOURS = 48  # cap on how stale a preserved value can be


def _load_previous_cache():
    """Load previous cache JSON if recent enough to use for last-known-good
    fallback when a feed returns None this run. Returns {} on miss/too-stale.
    """
    if not CACHE_JSON.exists():
        return {}
    age_hours = _cache_age_hours()
    if age_hours is None or age_hours > PRESERVE_MAX_HOURS:
        sys.stderr.write(f"[L1] Previous cache too stale ({age_hours:.1f}h > {PRESERVE_MAX_HOURS}h), not used for fallback\n")
        return {}
    try:
        with open(CACHE_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"[L1] Could not load previous cache for fallback: {e}\n")
        return {}


def main():
    force = "--force" in sys.argv
    if is_fresh() and not force:
        sys.stderr.write("[L1] Cache is fresh (<4h). Use --force to refresh anyway.\n")
        return

    sys.stderr.write("[L1] Fetching live valuation data...\n")

    prev_cache = _load_previous_cache()
    prev_tokens = prev_cache.get("tokens", {}) if isinstance(prev_cache, dict) else {}

    cg_data = fetch_coingecko()
    tvl_data = fetch_defillama_tvl()

    tokens_out = {}
    for tok, cg_id, dl_chain, name in TOKENS:
        cg = cg_data.get(cg_id, {})
        tvl_usd = tvl_data.get(dl_chain) or tvl_data.get(dl_chain.lower()) or 0.0

        if tok == "BTC":
            fees_usd = fetch_btc_fees()
        elif tok == "DOT":
            fees_usd = fetch_dot_fees()
        else:
            fees_usd = fetch_defillama_fees(dl_chain)

        mcap_usd = cg.get("mcap_usd")
        fdv_usd  = cg.get("fdv_usd")
        price    = cg.get("price_usd")

        apy, infl, stk_src = resolve_staking(tok)
        real_yield = (apy - infl) if (apy is not None and infl is not None) else None
        active_addr, addr_src = fetch_active_addresses(tok)
        # seed_hist = ancien historique (pour amorcer l'auto-accumulation SOL/BNB/AVAX)
        seed_hist = (prev_tokens.get(tok) or {}).get("active_addresses_history")
        addr_hist = fetch_active_addresses_history(tok, cg_id, seed_hist) if active_addr else None
        price_hist = _fetch_price_history(cg_id) if active_addr else None

        # Unifier la métrique affichée : moyenne 7j calculée depuis l'historique
        # pour toutes les sources (DLL + Hyperliquid CloudFront). Rend les 8 chaînes
        # réellement comparables (DAU 7j moyen).
        if addr_hist and len(addr_hist) >= 3:
            last7 = [p[1] for p in addr_hist[-7:] if p[1]]
            if last7:
                active_addr = round(sum(last7) / len(last7))

        # Sanity check: warn si l'historique d'une source est gelé > 7j (typiquement
        # asxn / Hyperliquid Foundation CloudFront qui freeze plusieurs semaines).
        # Le warning est visible dans les logs launchd et sur la page (voir le bandeau
        # ambre rendu dans framework_7_addr_html du Rmd).
        if addr_hist and len(addr_hist) > 0:
            last_ts_ms = addr_hist[-1][0]
            now_ms = int(time.time() * 1000)
            stale_days = (now_ms - last_ts_ms) // 86_400_000
            if stale_days > 7:
                sys.stderr.write(
                    f"[L1] WARN {tok} active-addresses feed stale: last point "
                    f"{stale_days}d old (source: {addr_src})\n"
                )

        # FDV dilution % = how much of total supply is circulating
        fdv_dilution_pct = None
        if cg.get("total_supply") and cg.get("circ_supply"):
            fdv_dilution_pct = round(100.0 * cg["circ_supply"] / cg["total_supply"], 1)

        entry = {
            "token":        tok,
            "name":         name,
            "coingecko_id": cg_id,
            "defillama":    dl_chain,
            "logo":         cg.get("image"),
            "price_usd":    price,
            "mcap_b":       round(mcap_usd / 1e9, 3) if mcap_usd else None,
            "fdv_b":        round(fdv_usd / 1e9, 3)  if fdv_usd  else None,
            "fees_m":       round(fees_usd / 1e6, 1) if fees_usd else None,
            "tvl_b":        round(tvl_usd / 1e9, 3),
            "volume_24h_b": round(cg.get("volume_24h", 0) / 1e9, 3) if cg.get("volume_24h") else None,
            "chg_24h":      cg.get("chg_24h"),
            "chg_7d":       cg.get("chg_7d"),
            "chg_30d":      cg.get("chg_30d"),
            "ath":          cg.get("ath"),
            "ath_chg_pct":  cg.get("ath_chg_pct"),
            "circ_supply":  cg.get("circ_supply"),
            "total_supply": cg.get("total_supply"),
            "fdv_dilution_pct": fdv_dilution_pct,
            "staking_apy":  apy,
            "inflation":    infl,
            "real_yield":   round(real_yield, 2) if real_yield is not None else None,
            "staking_source": stk_src,
            "active_addresses_7d_avg": active_addr,
            "active_addresses_source": addr_src,
            "active_addresses_history": addr_hist,
            "price_history": price_hist,
        }

        # Derived ratios
        if entry["mcap_b"] and entry["fees_m"]:
            entry["ps_ratio"] = round(entry["mcap_b"] * 1000 / entry["fees_m"], 1)
        else:
            entry["ps_ratio"] = None

        if entry["fdv_b"] and entry["fees_m"]:
            entry["ps_fdv"] = round(entry["fdv_b"] * 1000 / entry["fees_m"], 1)
        else:
            entry["ps_fdv"] = None

        # P/TVL sur FDV (et non MCap) pour pénaliser la dilution future : un token
        # dont 76 % du supply est encore à émettre (HYPE) doit être évalué sur sa valeur
        # pleinement diluée, pas son supply circulant. Les tokens fully-diluted (ETH/TRX/BNB)
        # restent inchangés (FDV == MCap).
        if entry["tvl_b"] and entry["fdv_b"] and entry["tvl_b"] > 0:
            entry["ptvl_ratio"] = round(entry["fdv_b"] / entry["tvl_b"], 2)
        else:
            entry["ptvl_ratio"] = None

        # NVT approximation = MCap / (annual on-chain transfer value)
        # Proxy: volume_24h × 365 (CEX + DEX). Imperfect but comparable across chains.
        if entry["mcap_b"] and entry["volume_24h_b"]:
            annual_vol = entry["volume_24h_b"] * 365
            entry["nvt_ratio"] = round(entry["mcap_b"] / annual_vol, 2) if annual_vol > 0 else None
        else:
            entry["nvt_ratio"] = None

        # Last-known-good fallback : si un feed a flaké et renvoyé None ce run mais
        # qu'on a une valeur valide dans le cache précédent (<48h), on la conserve
        # pour éviter le "N/A inattendu" visible sur la page Valorisation L1.
        prev_entry = prev_tokens.get(tok) or {}
        for field in PRESERVE_FIELDS:
            if entry.get(field) is None and prev_entry.get(field) is not None:
                entry[field] = prev_entry[field]
                sys.stderr.write(f"[L1] {tok}.{field} preserved from previous cache (current fetch returned None)\n")

        # Recompute derived ratios if fees_m was restored from cache
        if entry.get("fees_m") and entry.get("mcap_b") and entry.get("ps_ratio") is None:
            entry["ps_ratio"] = round(entry["mcap_b"] * 1000 / entry["fees_m"], 1)
        if entry.get("fees_m") and entry.get("fdv_b") and entry.get("ps_fdv") is None:
            entry["ps_fdv"] = round(entry["fdv_b"] * 1000 / entry["fees_m"], 1)

        tokens_out[tok] = entry
        # gentle rate limit for DefiLlama
        time.sleep(0.3)


    # Les dates passent en TÊTE du cache, et une epoch les rejoint.
    # Deux mécanismes ne lisent que le DÉBUT d'un cache : la garde de fraîcheur
    # ci-dessus, et l'index qui dit au site laquelle de ses deux copies est la plus
    # fraîche. Reléguées derrière `tokens` (1,2 Mo), ces dates étaient hors de leur
    # portée : le site datait ce fichier au passage du collecteur — donc « frais » —
    # pendant que son contenu était gelé depuis seize jours. L'epoch, elle, ne se
    # lit pas de deux façons selon le fuseau de la machine.
    maintenant = datetime.now()
    payload = {
        "updated":  maintenant.strftime("%d/%m/%Y %H:%M"),
        "updated_iso": maintenant.isoformat(),
        "updated_unix": int(time.time()),
        "universe": [t[0] for t in TOKENS],
        "staking_snapshot_date": STAKING_SNAPSHOT_DATE,
        "sources": {
            "mcap_fdv_price_vol": "CoinGecko /coins/markets (free tier)",
            "tvl":            "DefiLlama /v2/chains",
            "fees":           "DefiLlama /overview/fees/<chain> (total1y or total30d × 12)",
            "btc_fees":       "blockchain.info /charts/transaction-fees-usd (30d avg × 365)",
            "staking_apy_inflation": f"Snapshot manuel {STAKING_SNAPSHOT_DATE} (StakingRewards.com + docs officielles) · SOL via getInflationRate on-chain",
            "active_addresses": "CoinMetrics community AdrActCnt (BTC/ETH/ADA/TRX/XRP — adresses actives chain-level, 7+ ans, frais) + DefiLlama /chains activeUsers24h (SOL/BNB/AVAX/HYPE — valeur courante chain-level, historique auto-accumulé ; HYPE = couche L1 on-chain, le feed perp-DEX Hyperliquid Foundation étant gelé depuis avril 2026). DOT/NEAR/SUI/APT/TON/TAO exclus (pas de feed DAA public comparable).",
        },
        "audit_urls": {
            "coingecko":     "https://www.coingecko.com/en/coins/",
            "defillama":     "https://defillama.com/chains",
            "defillama_fees": "https://defillama.com/fees/chains",
            "stakingrewards": "https://www.stakingrewards.com/",
        },
        "tokens":   tokens_out,
    }

    with open(CACHE_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    sys.stderr.write(f"[L1] Wrote {CACHE_JSON}\n")

    js_payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    CACHE_JS.write_text(f"window.__L1_VALUATION_LIVE__={js_payload};", encoding="utf-8")
    sys.stderr.write(f"[L1] Wrote {CACHE_JS}\n")

    n_complete = sum(1 for v in tokens_out.values() if v.get("mcap_b") and v.get("fees_m"))
    sys.stderr.write(f"[L1] Done: {n_complete}/{len(TOKENS)} tokens with MCap + fees\n")


if __name__ == "__main__":
    main()
