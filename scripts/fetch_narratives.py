#!/usr/bin/env python3
"""Narrative Tracker — extraction de narratifs crypto a la Alpha Zen.

Pipeline:
  1. Scan news_cache.json (articles crypto des 10 sources RSS)
  2. Compte mentions par narratif (dictionnaire de keywords)
  3. Fetch prix CoinGecko pour tokens associes (free tier, no key)
  4. Calcule score momentum composite (60% prix / 30% mentions / 10% volume)
  5. Calcule filtre tendance BTC (MA100j -> mode ALPHA ou ZEN)
  6. Output narratives_cache.json consomme par Narrative_Tracker.Rmd
"""
# ── Global timeout safeguard (30 min) — auto-tué si bloqué sur un I/O réseau,
#    libère le lock pour le prochain cycle launchd. Sans ça, un script bloqué
#    monopolise indéfiniment le verrou et empêche tous les refresh suivants.
import os
import signal as _signal, sys as _sys
def _global_timeout_handler(signum, frame):
    print(f"[fatal] global timeout (30 min) reached — aborting to free lock for next launchd cycle.", file=_sys.stderr)
    _sys.exit(2)
try:
    _signal.signal(_signal.SIGALRM, _global_timeout_handler)
    _signal.alarm(30 * 60)
except Exception:
    pass

import json, re, time, sys, base64
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import urllib.request
import urllib.error


# ─────────────────────────────────────────────────────────────────────────
# Favicon inliner — fetches the logo once, encodes as base64 data URI so
# the HTML has zero external favicon requests (= zero 404s in the console).
# Falls back to a generated SVG letter-avatar if all sources fail.
# ─────────────────────────────────────────────────────────────────────────
_FAVICON_CACHE = {}

def _letter_avatar_data_uri(symbol, color="#6a7094"):
    letter = ((symbol or "?")[0] or "?").upper()
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 22 22" '
        f'width="22" height="22">'
        f'<rect width="22" height="22" rx="4" fill="{color}"/>'
        f'<text x="11" y="15" text-anchor="middle" font-family="DM Mono,monospace" '
        f'font-size="11" font-weight="700" fill="#fff">{letter}</text>'
        f'</svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def _sniff_mime(blob):
    if not blob:
        return None
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:2] == b"\xff\xd8":
        return "image/jpeg"
    if blob.startswith(b"GIF8"):
        return "image/gif"
    if blob[:4] == b"<svg" or b"<svg" in blob[:200]:
        return "image/svg+xml"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob[:2] == b"\x00\x00" and blob[2:3] in (b"\x01", b"\x02"):
        return "image/x-icon"
    return None


def inline_favicon(domain, symbol, fallback_color="#6a7094"):
    """Fetch a favicon for `domain` and return a data:... URI. Letter avatar fallback."""
    key = (domain or "").lower()
    if key and key in _FAVICON_CACHE:
        cached = _FAVICON_CACHE[key]
        if cached == "__FALLBACK__":
            return _letter_avatar_data_uri(symbol, fallback_color)
        return cached

    if not domain:
        return _letter_avatar_data_uri(symbol, fallback_color)

    candidates = [
        f"https://icons.duckduckgo.com/ip3/{domain}.ico",
        f"https://www.google.com/s2/favicons?domain={domain}&sz=64",
        f"https://favicon.im/{domain}?larger=true",
    ]
    req_headers = {"User-Agent": "Mozilla/5.0 (favicon-inliner)"}
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=4) as r:
                status = getattr(r, "status", 200)
                if status != 200:
                    continue
                blob = r.read(32 * 1024)
                if len(blob) < 300:
                    continue
                mime = _sniff_mime(blob) or "image/x-icon"
                uri = f"data:{mime};base64," + base64.b64encode(blob).decode()
                _FAVICON_CACHE[key] = uri
                return uri
        except Exception:
            continue

    _FAVICON_CACHE[key] = "__FALLBACK__"
    return _letter_avatar_data_uri(symbol, fallback_color)

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
ROOT = CACHE_DIR  # legacy name kept for minimal diff
NEWS_CACHE = CACHE_DIR / "news_cache.json"
OUT_CACHE  = CACHE_DIR / "narratives_cache.json"
OUT_CACHE_JS = CACHE_DIR / "narratives_cache.js"
HIST_CACHE = CACHE_DIR / "narratives_history_cache.json"
YF_SYMBOL_CACHE = CACHE_DIR / "yf_crypto_symbol_map.json"  # cg_id -> symbole Yahoo résolu
LOCK_FILE  = CACHE_DIR / "narratives.lock"
# Cache SQLite yfinance PRIVÉ à ce script (2026-07-31), cf. fetch_tradfi.py.
# Ce script tourne À CHAQUE HEURE PILE et purgeait le cache PARTAGÉ
# ~/Library/Caches/py-yfinance. Tout fetcher yfinance en cours (fetch_tradfi
# 10:30 ~25 min, fetch_tradfi_hist plusieurs heures…) perdait alors le fichier
# sous son handle SQLite ouvert → OperationalError('no such table: _tz_kv') en
# cascade → 0 ticker récupéré. Seul le run TradFi de 20:00, qui se termine avant
# la purge de 21:00, survivait : d'où un tracker figé ~16 h sur la veille.
# Un répertoire par script rend la collision structurellement impossible.
YF_CACHE_DIR = Path.home() / "Library" / "Caches" / "py-yfinance-narratives"
COINGECKO  = "https://api.coingecko.com/api/v3"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


# ─────────────────────────────────────────────────────────────────────────
# Robustness helpers — singleton lock + yfinance SQLite reset + Stooq fallback
# Same pattern as fetch_tradfi.py: prevents launchd overlap + WAL corruption,
# adds a US-only price fallback for crypto-equities (COIN, MSTR, MARA…) when
# Yahoo rate-limits, and gap-fills missing assets from the previous cache.
# ─────────────────────────────────────────────────────────────────────────
def acquire_singleton_lock():
    import fcntl, os
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print("[lock] another fetch_narratives instance is running — exiting", file=sys.stderr)
        fd.close()
        return None
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


# Cache yfinance par défaut, PARTAGÉ par la quinzaine de fetchers qui n'ont pas
# de répertoire privé. Purger celui-ci casse tous les runs concurrents : le
# garde-fou ci-dessous refuse de le faire, quoi qu'on mette dans YF_CACHE_DIR.
YF_SHARED_DIR = Path.home() / "Library" / "Caches" / "py-yfinance"


def _init_yfinance_cache():
    """Isole les caches SQLite de yfinance (tz + cookies + isin) dans
    YF_CACHE_DIR, privé à ce script. À appeler AVANT tout usage de yfinance :
    sinon la lib s'attache au cache partagé par défaut et la cascade
    `no such table: _tz_kv` peut revenir."""
    try:
        import yfinance as yf
        YF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # set_tz_cache_location() redirige en fait tz + cookies + isin.
        yf.set_tz_cache_location(str(YF_CACHE_DIR))
        print(f"[info] yfinance tz-cache isolé dans {YF_CACHE_DIR}")
    except Exception as e:
        print(f"[warn] set_tz_cache_location: {e}", file=sys.stderr)


def reset_yfinance_cache():
    # GARDE-FOU (incident 2026-07-31) : ne JAMAIS purger le cache partagé —
    # d'autres fetchers y ont des handles SQLite ouverts.
    if YF_CACHE_DIR.resolve() == YF_SHARED_DIR.resolve():
        print("[reset] ABANDON : YF_CACHE_DIR pointe sur le cache PARTAGÉ "
              f"({YF_SHARED_DIR}). Le purger casserait les fetchers yfinance "
              "concurrents. Utiliser un répertoire privé à ce script.",
              file=sys.stderr)
        return
    if not YF_CACHE_DIR.exists():
        return
    removed = 0
    for f in YF_CACHE_DIR.glob("*.db*"):
        try:
            f.unlink()
            removed += 1
        except Exception:
            pass
    if removed:
        print(f"[reset] cleared {removed} yfinance SQLite file(s)", file=sys.stderr)


def load_previous_cache():
    if not OUT_CACHE.exists():
        return {}
    try:
        with open(OUT_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] could not load prev cache: {e}", file=sys.stderr)
        return {}


def fetch_stooq_snapshot_us(yahoo_sym):
    """US-equity snapshot fallback via Stooq daily CSV (no API key, no rate-limit doc).
    Returns dict with last/perf_7d/perf_30d or None. Crypto-equities here are all US."""
    stooq_sym = yahoo_sym.lower().replace("-", "") + ".us"
    url = f"https://stooq.com/q/d/l/?s={stooq_sym}&i=d"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", errors="ignore")
        lines = raw.strip().split("\n")
        if len(lines) < 25 or not lines[0].lower().startswith("date"):
            return None
        closes = []
        cutoff = time.time() - 60 * 86400
        for ln in lines[1:]:
            parts = ln.split(",")
            if len(parts) < 5:
                continue
            try:
                dt = datetime.strptime(parts[0], "%Y-%m-%d")
                ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
                if ts < cutoff:
                    continue
                px = float(parts[4])
                if px > 0:
                    closes.append(px)
            except Exception:
                continue
        if len(closes) < 2:
            return None
        last = closes[-1]
        p7   = closes[-6]  if len(closes) >= 6  else None
        p30  = closes[-22] if len(closes) >= 22 else None
        return {
            "last": last,
            "perf_7d":  ((last / p7)  - 1) * 100 if p7  and p7  > 0 else None,
            "perf_30d": ((last / p30) - 1) * 100 if p30 and p30 > 0 else None,
        }
    except Exception as e:
        print(f"[stooq] {yahoo_sym}: {e}", file=sys.stderr)
        return None

# Historical data: depth and TTL for the on-disk cache
HIST_DAYS = 1825  # ~5 ans via CryptoCompare (gratuit, sans clé)
HIST_TOP_N_PER_NARRATIVE = 3  # fetch top 3 candidates; index uses first with data (fallback)
HIST_CACHE_TTL_HOURS = 24


# ─────────────────────────────────────────────────────────────────────────
# DICTIONNAIRE DES NARRATIFS
# Chaque narratif = liste de keywords (lowercase, recherche substring)
# + liste de tokens CoinGecko IDs pondere equipondere par narratif.
# Edit ici pour ajuster la taxonomie.
# ─────────────────────────────────────────────────────────────────────────
# Filtre market cap : seuls les tokens au-dessus de ce rang sont inclus
# dans les narratifs. Ajuster si trop restrictif (200 = top 200 par mcap).
MAX_MCAP_RANK = 300

# Short descriptive blurbs per narrative (shown in UI tooltips).
NARRATIVE_DESC = {
    "Bitcoin Institutional":     "BTC + proxies d'exposition institutionnelle (WBTC) et actions adossées au Bitcoin (MSTR, mineurs).",
    "Ethereum":                  "L'actif ETH, ses dérivés liquid staking (stETH, wstETH), les flux institutionnels et les ETH treasury companies (BitMine).",
    "L1 Smart Contracts":        "Blockchains alternatives à Ethereum : Solana, BNB, Cardano, TON, Avalanche, Aptos, Sui, etc.",
    "Payment Coins":             "Cryptos orientées paiement/transfert de valeur : XRP, Stellar, Litecoin, Bitcoin Cash, Kaspa.",
    "Ethereum L2s":              "Rollups et scaling Ethereum : Arbitrum, Optimism, Polygon, Mantle, Base, Starknet, zkSync.",
    "AI & Agents":               "Tokens de l'écosystème IA crypto : Bittensor, Worldcoin, Virtual Protocol, Fetch.ai, AI agents.",
    "DePIN":                     "Decentralized Physical Infrastructure Networks : Render (GPU), Helium, Filecoin, IoTeX.",
    "RWA":                       "Real World Assets : tokenisation de bons du Trésor (BUIDL, Ondo), or (PAXG), actifs réels on-chain.",
    "Restaking":                 "EigenLayer et Liquid Restaking Tokens : ether.fi, Renzo, Kelp, Puffer, Symbiotic.",
    "BTC L2s & Ordinals":        "Scaling Bitcoin et inscriptions : Stacks, Ordinals, Runes, Babylon, Merlin, Core.",
    "Memecoins":                 "Tokens meme : DOGE, SHIB, PEPE, WIF, BONK, TRUMP, FARTCOIN, et autres pump.fun.",
    "Modular / DA":              "Data Availability et blockchains modulaires : Celestia, Dymension, Avail, AltLayer.",
    "ZK / Privacy":              "Zero-knowledge proofs et cryptos privées : Monero, Zcash, Starknet, zkSync, Aztec, Mina.",
    "Liquid Staking":            "Tokens de liquid staking : Lido, Rocket Pool, Jito, Marinade, Frax Ether.",
    "DEX & AMM":                 "Exchanges décentralisés spot : Uniswap, Curve, PancakeSwap, Aerodrome, Raydium, Jupiter.",
    "Perp DEX":                  "Plateformes de perpétuels décentralisés : Hyperliquid, dYdX, GMX, Drift, Jupiter Perps.",
    "Lending & Yield":           "Protocoles de prêt et yield : Aave, Sky (Maker), Morpho, Compound, Pendle, Yearn.",
    "Gaming / SocialFi":         "GameFi et social tokens : Chiliz, Pudgy Penguins, Axie, Sandbox, Immutable, Ronin, ENS.",
    "NFT":                       "NFT marketplaces (Blur, LooksRare, X2Y2, Rarible) + collection tokens (ApeCoin/BAYC, Pudgy Penguins, Treasure/Magic, SuperVerse). Secteur cyclique du JPEG on-chain.",
    "Solana Ecosystem":          "Tokens natifs/majeurs de l'écosystème Solana : SOL, JUP, BONK, WIF, PYTH, Raydium, Jito.",
    "Prediction Markets":        "Marchés prédictifs : Gnosis, Azuro, Polymarket (off-token). Secteur limité par tokenisation.",
    "Stablecoins":               "Tokens gouvernance/issuer des stablecoins : Ethena, Sky, Maker, Frax, Circle (CRCL), PayPal.",
    "Exchange Tokens":           "Tokens utilitaires des CEX : BNB, OKB, CRO, LEO, BGB, KCS, HTX, Bitget, Gate.",
    "Bitcoin Miners":            "Actions des mineurs Bitcoin cotés : MARA, RIOT, CLSK, HUT, BITF, CIFR, WULF, CORZ, APLD, IREN.",
    "Web3 Exchanges & Fintech":  "Actions des exchanges/fintech crypto-exposed : Coinbase, Robinhood, Block, PayPal, Circle, Galaxy.",
}

# Crypto-exposed US stocks (Yahoo symbols). Hardcoded metadata since
# Yahoo's free endpoints are flaky on company names.
CRYPTO_STOCKS = {
    "MSTR": {"name": "Strategy (MicroStrategy)", "domain": "strategy.com",         "exchange": "NASDAQ"},
    "COIN": {"name": "Coinbase",                 "domain": "coinbase.com",         "exchange": "NASDAQ"},
    "HOOD": {"name": "Robinhood",                "domain": "robinhood.com",        "exchange": "NASDAQ"},
    "MARA": {"name": "MARA Holdings",            "domain": "mara.com",             "exchange": "NASDAQ"},
    "RIOT": {"name": "Riot Platforms",           "domain": "riotplatforms.com",    "exchange": "NASDAQ"},
    "CLSK": {"name": "CleanSpark",               "domain": "cleanspark.com",       "exchange": "NASDAQ",
             "icon_url": "https://cleanspark.com/favicon.ico"},
    "HUT":  {"name": "Hut 8",                    "domain": "hut8.com",             "exchange": "NASDAQ"},
    "BITF": {"name": "Bitfarms",                 "domain": "bitfarms.com",         "exchange": "NASDAQ"},
    "CIFR": {"name": "Cipher Mining",            "domain": "ciphermining.com",     "exchange": "NASDAQ"},
    "WULF": {"name": "TeraWulf",                 "domain": "terawulf.com",         "exchange": "NASDAQ"},
    "CORZ": {"name": "Core Scientific",          "domain": "corescientific.com",   "exchange": "NASDAQ"},
    "APLD": {"name": "Applied Digital",          "domain": "applieddigital.com",   "exchange": "NASDAQ"},
    "IREN": {"name": "IREN (Iris Energy)",       "domain": "iren.com",             "exchange": "NASDAQ"},
    "SMLR": {"name": "Semler Scientific",        "domain": "semlerscientific.com", "exchange": "NASDAQ"},
    "XYZ":  {"name": "Block (ex-Square)",        "domain": "block.xyz",            "exchange": "NYSE"},
    "PYPL": {"name": "PayPal (PYUSD)",           "domain": "paypal.com",           "exchange": "NASDAQ"},
    "GLXY": {"name": "Galaxy Digital",           "domain": "galaxy.com",           "exchange": "NASDAQ"},
    "CRCL": {"name": "Circle Internet",          "domain": "circle.com",           "exchange": "NYSE"},
    "BMNR": {"name": "BitMine Immersion (ETH treasury)", "domain": "bitminetech.io",  "exchange": "NYSE American"},
}

NARRATIVES = {
    "Bitcoin Institutional": {
        "icon": "fa-building",
        "color": "#f7931a",
        "keywords": [
            "bitcoin etf", "btc etf", "spot etf", "blackrock bitcoin", "ibit",
            "fidelity bitcoin", "bitwise", "microstrategy", "strategy inc",
            "strategy (", "strategy bought", "mstr ", "bitcoin treasury",
            "corporate bitcoin", "treasury company",
            "bitcoin reserve", "sovereign bitcoin", "pension bitcoin",
            "morgan stanley bitcoin", "goldman bitcoin", "marathon digital",
            "riot platforms", "cleanspark", "semler scientific",
        ],
        "tokens": ["bitcoin", "wrapped-bitcoin", "coinbase-wrapped-btc"],
        "stocks": ["MSTR", "SMLR", "MARA", "RIOT", "CLSK", "HUT"],
    },

    "Ethereum": {
        "icon": "fa-ethereum",
        "color": "#627eea",
        "keywords": [
            "ethereum", "vitalik", " eth ", "eth etf", "eth upgrade", "dencun",
            "eip-", "pectra", "proto-danksharding", "merge ethereum",
            "ether foundation", "eth staking", "eth l1", "ethereum foundation",
            "eth price", "ether price", "eth inflation",
            "bitmine immersion", "bmnr ", "tom lee ethereum", "eth treasury",
        ],
        "tokens": [
            "ethereum", "wrapped-steth",
            "weth", "ether-fi-staked-eth",
        ],
        "stocks": ["BMNR"],
    },

    "L1 Smart Contracts": {
        "icon": "fa-link",
        "color": "#6366f1",
        "keywords": [
            "solana", "cardano", "hoskinson", "binance smart", "bnb chain",
            "tron network", "justin sun", "open network", " ton ", "toncoin",
            "avalanche", "polkadot", "cosmos hub", "near protocol", "aptos",
            " sui ", "hedera", "algorand", "multiversx", "elrond", "tezos",
            "vechain", "fantom", " sonic ", "monad", "kaia", "injective",
            "canton network", "conflux", "sei network",
        ],
        "tokens": [
            "tron", "cardano", "the-open-network",
            "avalanche-2", "polkadot", "near", "aptos", "sui",
            "cosmos", "hedera-hashgraph", "algorand", "tezos",
            "internet-computer", "vechain", "injective-protocol",
            "sei-network", "flare-networks", "iota", "neo", "kaia",
            "conflux-token", "sonic-3", "canton-network",
            "monad", "plasma", "bittorrent", "sun-token",
            "xdce-crowd-sale", "pi-network", "flow",
        ],
    },

    "Payment Coins": {
        "icon": "fa-money-bill-transfer",
        "color": "#06b6d4",
        "keywords": [
            "ripple", " xrp ", "xrp ledger", "xrp etf", "litecoin", " ltc ",
            "stellar", " xlm ", "bitcoin cash", " bch ", "kaspa", " kas ",
            "cross-border", "payment rail", "remittance",
        ],
        "tokens": [
            "ripple", "stellar", "litecoin", "bitcoin-cash",
            "kaspa", "bitcoin-cash-sv", "ethereum-classic",
            "dash", "decred",
        ],
    },

    "Ethereum L2s": {
        "icon": "fa-layer-group",
        "color": "#7c3aed",
        "keywords": [
            "arbitrum", "optimism", "mantle network", " mnt ", "polygon",
            "base chain", "base network", "coinbase base", "metis",
            "linea", "zora", "scroll", "blast l2", "l2 ethereum",
            "layer 2 ethereum", "ethereum scaling", "op stack", "op mainnet",
            "layerzero",
        ],
        "tokens": [
            "arbitrum", "optimism", "mantle", "polygon-ecosystem-token",
            "starknet", "layerzero", "scroll",
            "zksync",
        ],
    },
    # NOTE 2026-06-03 : aerodrome-finance RETIRÉ d'Ethereum L2s (mauvaise
    # classification — AERO est un DEX sur Base, pas un token L2). Reste
    # légitimement dans DEX & AMM. Idem retiré du keywords ci-dessus.

    "AI & Agents": {
        "icon": "fa-robot",
        "color": "#a78bfa",
        "keywords": [
            "ai agent", "ai agents", "autonomous agent", "agentic",
            "virtuals protocol", "virtuals ", " ai16z", "fartcoin", "goatseus",
            "ai token", "ai crypto", "ai coin", "artificial intelligence", "llm",
            "vibe coding", "ai vibe", "anthropic", "openai", "chatgpt",
            "bittensor", "worldcoin", "fetch.ai", "fetch ai", "asi alliance",
            "the graph", " grass ", "jasmy", "kite ai",
        ],
        "tokens": [
            "bittensor", "worldcoin-wld", "the-graph", "fetch-ai",
            "ocean-protocol", "singularitynet", "virtual-protocol",
            "ai16z", "fartcoin", "goatseus-maximus", "io", "nosana",
            "akash-network", "render-token", "venice-token", "grass",
            "jasmycoin", "kite-2",
        ],
    },

    "DePIN": {
        "icon": "fa-network-wired",
        "color": "#5eaff6",
        "keywords": [
            "depin", "decentralized physical", "decentralized compute",
            "helium", "render network", "render token", "akash",
            "iotex", "filecoin", "arweave", "hivemapper", "weatherxm",
            "decentralized gpu", "decentralized wireless", "decentralized storage",
            "theta network", "livepeer", "ankr", "telcoin",
        ],
        "tokens": [
            "render-token", "helium", "akash-network", "filecoin",
            "iotex", "arweave", "theta-token", "livepeer", "ankr",
            "io", "nosana", "telcoin",
            "jasmycoin", "grass", "walrus-2",
            # Ajouts 2026-05-21 : DePIN protocoles avec rev tracée
            "aethir", "geodnet",
        ],
    },

    "RWA": {
        "icon": "fa-building-columns",
        "color": "#34d399",
        "keywords": [
            "rwa", "real world asset", "real-world asset", "tokenized treasury",
            "tokenization", "tokenize", "tokenized", "ondo", "maple finance",
            "centrifuge", "buidl", "blackrock buidl", "treasury token",
            "private credit", "on-chain credit", "tokenized stock", "tokenized equit",
            "mantra", "chainlink", "tokenized gold", "pax gold", "tether gold",
            "tokenized t-bill", "tokenized treasury", "provenance", "blockchain capital",
        ],
        "tokens": [
            "chainlink",
            "blackrock-usd-institutional-digital-liquidity-fund",
            "pax-gold", "tether-gold", "kinesis-gold", "kinesis-silver",
            "janus-henderson-anemoy-treasury-fund",
            "ondo-us-dollar-yield", "ondo-finance",
            "spiko-eu-t-bills-money-market-fund",
            "superstate-short-duration-us-government-securities-fund-ustb",
            "hash-2", "ousg",
            "janus-henderson-anemoy-aaa-clo-fund",
            "hastra-prime", "syrup",
            "spiko-us-t-bills-money-market-fund",
            "pendle",
            "circle-internet-group-ondo-tokenized-stock",
            "quant-network", "eutbl", "ylds", "story-2",
        ],
        "stocks": [],
    },

    "Restaking": {
        "icon": "fa-layer-group",
        "color": "#f59e0b",
        "keywords": [
            "restaking", "eigenlayer", "eigen layer", "liquid restaking", " lrt",
            "ether.fi", "etherfi", "renzo", "kelp dao", "puffer ", "symbiotic",
            "avs network", "actively validated",
        ],
        "tokens": ["ether-fi", "eigenlayer", "renzo", "kelp-dao",
                   "puffer-finance", "swell-network"],
    },

    "BTC L2s & Ordinals": {
        "icon": "fa-bitcoin-sign",
        "color": "#ff9800",
        "keywords": [
            "bitcoin l2", "btc l2", "bitcoin layer 2", "stacks protocol", "merlin",
            "bitlayer", "bob bitcoin", "rootstock", "babylon chain",
            "runes protocol", "runes ", "ordinals", "brc-20", "bitcoin defi",
            "bitcoin dapp", "bitcoin staking", "bitcoin script", "core chain",
        ],
        "tokens": ["blockstack", "ordinals", "coredaoorg", "merlin-chain",
                   "babylon", "rootstock", "bounce-bit"],
    },

    "Memecoins": {
        "icon": "fa-dog",
        "color": "#ff4d6a",
        "keywords": [
            "memecoin", "meme coin", "memecoins", "meme coins",
            "dogecoin", "doge ", "shiba inu", " shib ", "pepe ", "pepecoin",
            "dogwifhat", "wif ", "bonk ", "popcat", "brett ",
            "meme token", "shitcoin", "pump.fun", "pumpfun", "pump fun",
            "floki", "mog coin", "neiro", "pudgy penguins", "fartcoin",
            "trump token", " trump ", "memecore",
        ],
        "tokens": [
            "dogecoin", "memecore", "shiba-inu", "pepe", "pump-fun",
            "official-trump", "bonk", "pudgy-penguins", "apenft",
            "spx6900", "floki", "ape-and-pepe", "terra-luna",
            "dogwifcoin", "fartcoin", "ordinals", "mog-coin",
            "neiro-ethereum", "book-of-meme", "memecoin", "turbo",
            "based-brett",
        ],
    },

    "Modular / DA": {
        "icon": "fa-cubes",
        "color": "#60a5fa",
        "keywords": [
            "modular blockchain", "data availability", "celestia", "tia token",
            "eigenda", "avail blockchain", "avail network", "dymension",
            "rollup as a service", "sovereign rollup", "raas ",
        ],
        "tokens": ["celestia", "dymension", "avail", "altlayer"],
    },

    "ZK / Privacy": {
        "icon": "fa-user-secret",
        "color": "#c084fc",
        "keywords": [
            "zk proof", "zero knowledge", "zero-knowledge", "zk-rollup", "zk rollup",
            "starknet", "stark", "zksync", "zk sync",
            "scroll zk", "polygon zkevm", "zkevm", "aztec network", "aleo",
            "zcash", "monero", "mina protocol", "privacy coin",
            "midnight network", "beldex",
        ],
        "tokens": [
            "monero", "zcash", "polygon-ecosystem-token", "beldex",
            "midnight-3", "decred", "starknet", "zksync", "scroll",
            "mina-protocol", "oasis-network",
        ],
    },

    "Liquid Staking": {
        "icon": "fa-droplet",
        "color": "#22d3ee",
        "keywords": [
            "liquid staking", "lido", "rocket pool", "rocketpool",
            "jito ", "marinade", "frax ether", "lsd ", "lst ",
            "steth", "wsteth", "staking token",
        ],
        "tokens": [
            "lido-dao", "lido-earn-eth", "rocket-pool",
            "jito-governance-token", "marinade", "frax-ether",
            "staked-ether", "stader", "binance-staked-ether",
        ],
    },

    "Perp DEX": {
        "icon": "fa-arrow-trend-up",
        "color": "#14b8a6",
        "keywords": [
            "perp dex", "perpetual dex", "perpetual futures", "perps ",
            "hyperliquid", " hype ", "dydx", "gmx ", "jupiter perps",
            "drift protocol", "aevo ", "lighter ", "vertex protocol",
            "paradex", "synfutures", "on-chain perpetual",
            "decentralized perp",
        ],
        "tokens": [
            "hyperliquid", "dydx-chain", "gmx", "drift-protocol",
            "lighter", "vertex-protocol", "jupiter-exchange-solana",
            "synthetix-network-token", "aevo-exchange",
            # Ajouts 2026-05-21 : Perp DEX avec revenue tracée
            "gains-network", "bluefin",
        ],
    },

    "DEX & AMM": {
        "icon": "fa-arrows-left-right-to-line",
        "color": "#2dd4bf",
        "keywords": [
            "uniswap", "pancakeswap", "curve finance", "sushiswap",
            "balancer", "1inch", "aerodrome", "raydium exchange",
            "decentralized exchange", "spot dex", "amm ",
            "automated market maker",
        ],
        "tokens": [
            "uniswap", "pancakeswap-token", "curve-dao-token",
            "aerodrome-finance", "raydium", "jupiter-exchange-solana",
            "sushi", "balancer", "1inch", "dexe", "osmosis",
            # Ajouts 2026-05-21 (audit revenue) : DEX avec rev > $5M/an absentés
            "cow-protocol", "thorchain", "thena", "quickswap", "thorswap",
        ],
    },

    "Lending & Yield": {
        "icon": "fa-hand-holding-dollar",
        "color": "#4ade80",
        "keywords": [
            "aave ", "maker dao", "makerdao", "sky protocol", "sky ecosystem",
            "compound finance", "morpho", "pendle", "yearn", "convex",
            "lending protocol", "money market", "defi yield",
            "overcollateralized", "olympus dao", "kamino",
        ],
        "tokens": [
            "aave", "morpho", "compound-governance-token",
            "pendle", "olympus", "yearn-finance", "convex-finance",
            "kamino", "syrup", "zebec-network",
            # Ajout 2026-05-21 : Fluid (Instadapp lending, rev $13M) — CG id = 'instadapp'
            "instadapp",
        ],
    },

    "Gaming / SocialFi": {
        "icon": "fa-gamepad",
        "color": "#fb923c",
        "keywords": [
            "gamefi", "game finance", "play to earn", "p2e ", "play-to-earn",
            "immutable x", "immutable zkevm", "gala games", "axie infinity",
            "the sandbox", "decentraland", "beam network", "pixels game",
            "friend.tech", "farcaster", "lens protocol", "socialfi",
            "on-chain game", "blockchain gaming", "ronin",
            "chiliz", "ethereum name service", " ens ",
            "brave browser", "basic attention",
        ],
        "tokens": [
            "chiliz", "hastra-prime",
            "ethereum-name-service", "the-sandbox", "undeads-games",
            "axie-infinity", "decentraland",
            "immutable-x", "gala", "beam-2", "ronin",
            "enjincoin", "echelon-prime", "merit-circle",
            "basic-attention-token",
        ],
    },

    "NFT": {
        "icon": "fa-image",
        "color": "#ec4899",
        "max_rank": 800,  # Le marché NFT étant cyclique-déprimé, relax le filtre top-300
        "keywords": [
            "nft ", " nfts ", "nft market", "nft marketplace", "nft collection",
            "nft trading", "nft floor", "blue chip nft", "pfp project",
            "bayc ", "bored ape", "apecoin", "mayc ",
            "pudgy penguins", " pengu ", "azuki", "doodles", "moonbirds",
            "milady", "clonex", "clone x", "cryptopunks",
            "blur.io", "blur marketplace", "looksrare", "x2y2", "rarible",
            "magic eden", "opensea", "superrare", "superverse",
            "nft mint", "nft drop", "digital collectible", "jpeg ", "pfp ",
            "ordinals nft",
        ],
        "tokens": [
            # Collection / ecosystem tokens (PFP, BAYC, etc.)
            "apecoin", "pudgy-penguins", "memecoin",
            # Marketplaces
            "blur", "looksrare", "rari-governance-token", "superrare",
            # NFT infrastructure + gaming NFT crossovers
            "immutable-x", "treasure", "echelon-prime", "superverse",
        ],
    },

    "Solana Ecosystem": {
        "icon": "fa-bolt",
        "color": "#9945ff",
        "keywords": [
            "solana", " sol ", "jupiter exchange", "jupiter aggregator",
            "raydium", "jito ", "drift protocol", "kamino", "pyth",
            "jupiter perps", "phantom wallet",
        ],
        "tokens": [
            "solana", "pump-fun", "render-token", "jupiter-exchange-solana",
            "bonk", "pyth-network", "dogwifcoin", "fartcoin",
            "helium", "raydium", "jito-governance-token",
            "drift-protocol", "kamino", "tensor", "wormhole",
            # Ajouts 2026-05-21 (audit revenue) : protocoles Sol DeFi avec rev >$10M
            "meteora", "metaplex", "ore",
        ],
    },

    "Prediction Markets": {
        "icon": "fa-chart-column",
        "color": "#ec4899",
        "keywords": [
            "prediction market", "prediction markets", "polymarket",
            "kalshi", "event contract", "betting market", "augur",
            "gnosis prediction", "manifold market", "azuro protocol",
            "sports betting crypto", "election betting", "odds market",
        ],
        # Prediction markets is a small sector with limited tokenization.
        # Polymarket has no token. GNO, AUGUR, ZKP (betting-adjacent) are listed.
        "tokens": ["gnosis", "augur", "azuro-protocol", "zeus-network"],
    },

    "Stablecoins": {
        "icon": "fa-dollar-sign",
        "color": "#10b981",
        "keywords": [
            "stablecoin", "stable coin", "usdt", "tether", "usdc", "circle",
            "dai stablecoin", "frax usd", "ethena", "usde ", "pyusd",
            "stablecoin issuer", "stablecoin bill", "stablecoin legislation",
            "stablecoin market cap", "genius act", "rlusd", "ripple usd",
        ],
        # Tokens = UNIQUEMENT gouvernance / issuer des protocoles stablecoin.
        # Les stablecoins eux-mêmes (USDT/USDC/DAI/USDE/USDF/FRAX la stable, etc.)
        # sont par construction peg à 1$ — leur mcap reflète seulement la
        # circulation, pas une thèse d'investissement avec rendement attendu.
        # On garde donc UNIQUEMENT les tokens qui captent la croissance du
        # secteur stablecoin (frais, gouvernance, partage de profits du peg).
        "tokens": [
            "ethena",                  # ENA  : gov d'Ethena Labs (issuer USDe)
            "frax-share",              # FXS  : gov de Frax Finance (issuer frxUSD)
            "sky",                     # SKY  : gov ex-Maker (issuer USDS)
            "maker",                   # MKR  : gov historique Sky/Maker (issuer DAI)
            "liquity",                 # LQTY : gov de Liquity (issuer LUSD)
            "usual",                   # USUAL: gov d'Usual Money (issuer USD0)
            "reserve-rights-token",    # RSR  : gov de Reserve Protocol (issuer eUSD)
            "inverse-finance",         # INV  : gov d'Inverse Finance (issuer DOLA)
            # NOTE : on ne met PAS frax (legacy stable), falcon-finance (USDF stable),
            # liquity-usd, usde, usds, dai, usdt, usdc, etc. — toutes pegged.
        ],
        "stocks": ["CRCL", "PYPL"],
    },

    "Exchange Tokens": {
        "icon": "fa-landmark",
        "color": "#fbbf24",
        "keywords": [
            "bnb ", "binance coin", "okx exchange", "okb ", "cronos",
            "crypto.com", "bitget token", "kucoin", " leo ", "bitfinex",
            "gate.io", " ht ", "exchange token", "whitebit", "swissborg",
            "trust wallet token",
        ],
        "tokens": [
            "binancecoin", "whitebit", "leo-token", "crypto-com-chain",
            "okb", "htx-dao", "bitget-token", "kucoin-shares",
            "gatechain-token", "nexo", "just", "swissborg",
            "btse-token", "trust-wallet-token",
        ],
    },

    "Bitcoin Miners": {
        "icon": "fa-industry",
        "color": "#f97316",
        "keywords": [
            "marathon digital", "mara holdings", "riot platforms",
            "cleanspark", "clsk ", "hut 8", "bitfarms", "terawulf",
            "core scientific", "applied digital", "cipher mining",
            "iris energy", "iren ", "bitcoin mining", "bitcoin miners",
            "hash rate", "hashrate", "halving",
        ],
        "tokens": [],
        "stocks": [
            "MARA", "RIOT", "CLSK", "HUT", "BITF", "CIFR",
            "WULF", "CORZ", "APLD", "IREN",
        ],
    },

    "Web3 Exchanges & Fintech": {
        "icon": "fa-chart-pie",
        "color": "#a855f7",
        "keywords": [
            "coinbase global", "coinbase earnings", "coinbase revenue",
            " coin stock", "robinhood crypto", " hood ", "block inc",
            "square block", "xyz stock", "paypal stablecoin", "paypal usd",
            "galaxy digital", "circle internet", "circle ipo",
            "crypto exchange public", "crypto brokerage",
        ],
        "tokens": [],
        "stocks": ["COIN", "HOOD", "XYZ", "GLXY"],
    },
}


# ─────────────────────────────────────────────────────────────────────────
# HTTP helper with retries
# ─────────────────────────────────────────────────────────────────────────
def _http_json(url, retries=3, pause=3.0):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            # CG free tier rate-limit: longer backoff on 429
            delay = 30.0 if e.code == 429 else pause * (i + 1)
            if i < retries:
                time.sleep(delay)
        except Exception as e:
            last = e
            if i < retries:
                time.sleep(pause * (i + 1))
    print(f"[warn] GET {url[:80]} failed: {last}", file=sys.stderr)
    return None


# ─────────────────────────────────────────────────────────────────────────
# NEWS SCAN
# Parse pubDate + title + link, count mentions per narrative per day.
# ─────────────────────────────────────────────────────────────────────────
def parse_pubdate(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s.replace("GMT", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    return None


def scan_news(news):
    """Returns dict: narrative -> {total, daily:{YYYY-MM-DD:count}, articles:[...]}"""
    result = {n: {"total": 0, "daily": defaultdict(int), "articles": []}
              for n in NARRATIVES}
    for art in news:
        title = (art.get("title") or "").lower()
        link  = (art.get("link")  or "").lower()
        blob  = title + " " + link
        dt = parse_pubdate(art.get("pubDate"))
        if dt is None:
            continue
        day = dt.strftime("%Y-%m-%d")
        for narr, cfg in NARRATIVES.items():
            for kw in cfg["keywords"]:
                if kw in blob:
                    result[narr]["total"] += 1
                    result[narr]["daily"][day] += 1
                    if len(result[narr]["articles"]) < 20:
                        result[narr]["articles"].append({
                            "title": art.get("title", ""),
                            "link":  art.get("link", ""),
                            "src":   art.get("src", ""),
                            "date":  day,
                        })
                    break
    for n in result:
        result[n]["daily"] = dict(sorted(result[n]["daily"].items()))
    return result


def news_momentum(daily_counts, today):
    """Returns (mentions_7d, mentions_prev7d, mentions_30d, accel_pct).
    accel_pct = (7d / avg_last_4weeks) - 1, clipped to [-1, +3]."""
    if not daily_counts:
        return 0, 0, 0, 0.0
    def sum_range(days_ago_from, days_ago_to):
        s = 0
        for i in range(days_ago_from, days_ago_to):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            s += daily_counts.get(d, 0)
        return s
    m7  = sum_range(0, 7)
    mp7 = sum_range(7, 14)
    m30 = sum_range(0, 30)
    baseline = m30 / 4.0 if m30 else 0
    if baseline <= 0:
        accel = 1.0 if m7 > 0 else 0.0
    else:
        accel = (m7 / baseline) - 1.0
    accel = max(-1.0, min(3.0, accel))
    return m7, mp7, m30, round(accel, 3)


# ─────────────────────────────────────────────────────────────────────────
# COINGECKO PRICES (free tier, no key)
# One batched market call for all tokens + per-token 30d history.
# ─────────────────────────────────────────────────────────────────────────
def fetch_cg_markets(ids):
    """Batched markets (prices, change 7d/30d, volume, mcap)."""
    if not ids:
        return {}
    out = {}
    # CG free tier: small batches (40 ids) + aggressive sleep to stay under ~10 calls/min
    BATCH = 40
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i+BATCH]
        url = (f"{COINGECKO}/coins/markets?vs_currency=usd"
               f"&ids={','.join(chunk)}"
               "&order=market_cap_desc&per_page=250&sparkline=false"
               "&price_change_percentage=7d,30d")
        data = _http_json(url)
        if data:
            for row in data:
                out[row["id"]] = row
        if i + BATCH < len(ids):
            time.sleep(12.0)  # respect CG free-tier (~5 calls/min safe)
    return out


def fetch_stocks(symbols):
    """Fetch US/CA equities via yfinance (no API key).
    Returns dict keyed by '$SYMBOL' to match the unified assets dict."""
    if not symbols:
        return {}
    try:
        import yfinance as yf
    except ImportError:
        print("[warn] yfinance not installed; skipping stocks", file=sys.stderr)
        return {}

    out = {}
    sym_str = " ".join(symbols)
    try:
        hist = yf.download(sym_str, period="45d", interval="1d",
                           group_by="ticker", progress=False, threads=True,
                           auto_adjust=False)
    except Exception as e:
        print(f"[warn] yf.download failed: {e}", file=sys.stderr)
        return out

    for sym in symbols:
        try:
            # Extract close series; single-ticker mode returns a flat df
            if hasattr(hist.columns, "get_level_values") and sym in hist.columns.get_level_values(0):
                closes = hist[sym]["Close"].dropna()
                vols   = hist[sym].get("Volume")
            else:
                closes = hist["Close"].dropna() if "Close" in hist.columns else None
                vols   = hist.get("Volume") if "Volume" in hist.columns else None
            if closes is None or len(closes) < 2:
                print(f"[warn] stock {sym}: not enough data", file=sys.stderr)
                continue

            closes_arr = closes.values
            last = float(closes_arr[-1])
            p7   = float(closes_arr[-6])  if len(closes_arr) >= 6  else None   # ~7 biz days
            p30  = float(closes_arr[-22]) if len(closes_arr) >= 22 else None   # ~30 biz days
            perf_7d  = ((last / p7)  - 1) * 100 if p7  and p7 > 0  else None
            perf_30d = ((last / p30) - 1) * 100 if p30 and p30 > 0 else None

            # Volume $ = last price × last daily volume
            vol_usd = 0
            try:
                if vols is not None and len(vols) > 0:
                    v = float(vols.dropna().iloc[-1])
                    vol_usd = v * last
            except Exception:
                pass

            # Market cap via fast_info (fast, no dependency on .info)
            mcap = 0
            try:
                fi = yf.Ticker(sym).fast_info
                mcap = float(getattr(fi, "market_cap", 0) or 0)
            except Exception:
                pass

            meta = CRYPTO_STOCKS.get(sym, {})
            # Favicon resolved + inlined as base64 data URI post-hoc (parallel
            # pass below) so the HTML makes zero external icon requests.
            explicit_icon = meta.get("icon_url")
            domain = meta.get("domain", "")
            if explicit_icon:
                icon, icon_fb = explicit_icon, ""
            else:
                icon, icon_fb = "", ""
            out["$" + sym] = {
                "id": "$" + sym,
                "symbol": sym,
                "name": meta.get("name", sym),
                "image": icon,
                "image_fallback": icon_fb,
                "current_price": last,
                "market_cap": mcap,
                "market_cap_rank": None,  # stocks bypass the rank filter
                "total_volume": vol_usd,
                "price_change_percentage_7d_in_currency":  perf_7d,
                "price_change_percentage_30d_in_currency": perf_30d,
                "is_stock": True,
                "exchange": meta.get("exchange", "NYSE"),
            }
        except Exception as e:
            print(f"[warn] stock {sym}: {e}", file=sys.stderr)

    # ── Inline favicons in parallel (1 HTTP request per unique domain) ──
    def _inline_url(url, sym):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (favicon-inliner)"})
            with urllib.request.urlopen(req, timeout=4) as r:
                blob = r.read(32 * 1024)
                if len(blob) >= 300:
                    mime = _sniff_mime(blob) or "image/x-icon"
                    return "data:{};base64,{}".format(mime, base64.b64encode(blob).decode())
        except Exception:
            pass
        return None

    to_fetch = {}
    for sym_key, row in out.items():
        sym = row.get("symbol") or sym_key
        meta = CRYPTO_STOCKS.get(sym, {})
        existing = row.get("image") or ""
        # Inline any explicit icon_url too (no external requests at render time)
        if existing.startswith("http"):
            uri = _inline_url(existing, sym)
            if uri:
                row["image"] = uri
                continue
            # fall through to domain-based cascade if explicit icon failed
            row["image"] = ""
        if row.get("image"):
            continue
        dom = meta.get("domain") or ""
        if not dom:
            row["image"] = _letter_avatar_data_uri(sym)
            continue
        to_fetch.setdefault(dom, []).append(sym_key)

    if to_fetch:
        print(f"[icons] inlining {len(to_fetch)} favicons…", file=sys.stderr)
        def _fetch(item):
            dom, keys = item
            sym = out[keys[0]].get("symbol") or keys[0]
            return dom, inline_favicon(dom, sym)
        with ThreadPoolExecutor(max_workers=16) as ex:
            for dom, uri in ex.map(_fetch, to_fetch.items()):
                for k in to_fetch[dom]:
                    out[k]["image"] = uri

    # ── Stooq snapshot fallback for tickers yfinance dropped (US-only here) ──
    missing = [s for s in symbols if ("$" + s) not in out]
    if missing:
        print(f"[info] Stooq snapshot fallback for {len(missing)} missing stocks…", file=sys.stderr)
        recovered = 0
        for sym in missing:
            snap = fetch_stooq_snapshot_us(sym)
            if not snap:
                continue
            meta = CRYPTO_STOCKS.get(sym, {})
            out["$" + sym] = {
                "id": "$" + sym,
                "symbol": sym,
                "name": meta.get("name", sym),
                "image": "",
                "image_fallback": "",
                "current_price": snap["last"],
                "market_cap": 0,
                "market_cap_rank": None,
                "total_volume": 0,
                "price_change_percentage_7d_in_currency":  snap["perf_7d"],
                "price_change_percentage_30d_in_currency": snap["perf_30d"],
                "is_stock": True,
                "exchange": meta.get("exchange", "NYSE"),
                "_source": "stooq",
            }
            recovered += 1
            time.sleep(0.3)
        print(f"[info] Stooq recovered {recovered}/{len(missing)} stocks", file=sys.stderr)
    return out


def _history_cache_load():
    if not HIST_CACHE.exists():
        return None, float("inf")
    age_h = (time.time() - HIST_CACHE.stat().st_mtime) / 3600
    try:
        return json.load(open(HIST_CACHE, "r", encoding="utf-8")), age_h
    except Exception:
        return None, float("inf")


def _downsample(ts_price_list, daily_cutoff_days=90):
    """Keep daily data for the most recent N days, weekly for older.
    Reduces ~1825 daily points to ~350 points."""
    if not ts_price_list or len(ts_price_list) < 100:
        return ts_price_list
    cutoff = time.time() - daily_cutoff_days * 86400
    old    = [(ts, px) for ts, px in ts_price_list if ts < cutoff]
    recent = [(ts, px) for ts, px in ts_price_list if ts >= cutoff]
    old_weekly = old[::7]  # every 7th point
    return old_weekly + recent


def _yf_chart_closes(sess, yf_sym, p1, p2):
    """Renvoie [(ts_sec, close), ...] daily pour un symbole Yahoo, ou []."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}"
           f"?period1={p1}&period2={p2}&interval=1d")
    try:
        r = sess.get(url, timeout=25)
        res = (r.json().get("chart", {}).get("result") or [None])[0]
        if not res or not res.get("timestamp"):
            return []
        ts = res["timestamp"]
        cl = (res.get("indicators", {}).get("quote", [{}])[0] or {}).get("close", []) or []
        return [(int(t), float(c)) for t, c in zip(ts, cl) if c is not None and c > 0]
    except Exception:
        return []


def _yf_search_usd_symbols(sess, name):
    """Résout un nom de crypto en symboles Yahoo '-USD' candidats via l'API search.
    Yahoo désambiguïse les tickers en collision par suffixe numéroté
    (ex. Uniswap = UNI7083-USD, pas UNI-USD)."""
    from urllib.parse import quote
    try:
        r = sess.get(f"https://query1.finance.yahoo.com/v1/finance/search"
                     f"?q={quote(name)}&quotesCount=8&newsCount=0", timeout=15)
        return [x.get("symbol") for x in r.json().get("quotes", [])
                if str(x.get("symbol", "")).endswith("-USD")]
    except Exception:
        return []


def fetch_token_histories(tokens_meta, days=HIST_DAYS):
    """Fetch daily price history via Yahoo Finance (curl_cffi impersonate).
    CryptoCompare gratuit est mort depuis ~juin 2026 (401 'API key required',
    racheté par CoinDesk). Yahoo bloque urllib (429) → curl_cffi obligatoire.

    tokens_meta: {cg_id: {'symbol':..., 'name':..., 'price':...}}
    Returns: {cg_id: [(ts_sec, price), ...]}

    Garde-fou collision : Yahoo a souvent un VIEUX coin mort sous '{SYM}-USD'
    (ex. UNI-USD ≠ Uniswap). On rejette toute série dont le dernier close diverge
    fortement du prix CoinGecko de référence, puis on résout le bon symbole
    numéroté via l'API search. La résolution (cg_id→symbole) est mémorisée pour
    accélérer les runs suivants. Les tokens non résolus retombent sur le gap-fill
    du cache disque (narratives_history_cache.json)."""
    try:
        from curl_cffi import requests as creq
    except ImportError:
        print("[warn] curl_cffi indisponible — token histories non rafraîchies", file=sys.stderr)
        return {}

    # Seed : symboles Yahoo numérotés que l'API search ne renvoie PAS pour ces
    # cg_id (vérifiés manuellement 2026-06-13). Toujours re-validés par le garde-fou
    # prix ci-dessous, donc sans risque si Yahoo renumérote un jour.
    _YF_SYMBOL_SEED = {
        "jupiter-exchange-solana": "JUP29210-USD",
        "polygon-ecosystem-token": "POL28321-USD",
    }
    # Résolution persistée cg_id -> symbole Yahoo (évite la recherche à chaque run)
    symmap = dict(_YF_SYMBOL_SEED)
    if YF_SYMBOL_CACHE.exists():
        try:
            symmap.update(json.load(open(YF_SYMBOL_CACHE, "r", encoding="utf-8")))
        except Exception:
            pass

    sess = creq.Session(impersonate="chrome120")
    p2 = int(time.time())
    p1 = p2 - max(days, 400) * 86400

    def price_ok(last, ref):
        # collisions = écart de plusieurs ordres de grandeur ; vraie volatilité ≪ 2×
        return bool(ref) and bool(last) and 0.5 <= (last / ref) <= 2.0

    out = {}
    items = sorted(tokens_meta.items())
    for i, (cg_id, meta) in enumerate(items):
        sym = (meta.get("symbol") or "").strip()
        name = meta.get("name") or sym
        ref = meta.get("price")

        # Ordre d'essai : symbole mémorisé, puis {SYM}-USD brut
        tried = []
        candidates = []
        if symmap.get(cg_id):
            candidates.append(symmap[cg_id])
        if sym:
            candidates.append(f"{sym}-USD")
        raw, chosen = [], None
        for c in candidates:
            if not c or c in tried:
                continue
            tried.append(c)
            pts = _yf_chart_closes(sess, c, p1, p2)
            if pts and (ref is None or price_ok(pts[-1][1], ref)):
                raw, chosen = pts, c
                break
            time.sleep(0.12)

        # Résolution via search si le ticker brut était une collision / vide
        if not raw and ref:
            for c in _yf_search_usd_symbols(sess, name)[:6]:
                if c in tried:
                    continue
                tried.append(c)
                pts = _yf_chart_closes(sess, c, p1, p2)
                if pts and price_ok(pts[-1][1], ref):
                    raw, chosen = pts, c
                    break
                time.sleep(0.12)

        if raw:
            symmap[cg_id] = chosen
            # Filtre phase pre-market/OTC : ignore tant que prix < 1% du max
            max_px = max(px for _, px in raw)
            threshold = max_px * 0.01
            raw = [(ts, px) for ts, px in raw if px >= threshold]
            out[cg_id] = _downsample(raw)
            print(f"[hist] {sym} via {chosen}: {len(raw)} raw → {len(out[cg_id])} pts")
        else:
            print(f"[warn] {sym} ({cg_id}): aucun match Yahoo — gap-fill cache", file=sys.stderr)
        if i < len(items) - 1:
            time.sleep(0.2)

    try:
        with open(YF_SYMBOL_CACHE, "w", encoding="utf-8") as f:
            json.dump(symmap, f, ensure_ascii=False, indent=0)
    except Exception as e:
        print(f"[warn] failed to write yf symbol map: {e}", file=sys.stderr)
    return out


def fetch_stock_histories(symbols, days=HIST_DAYS):
    """Fetch daily stock history via yfinance. Returns dict: $SYM -> [(ts_sec, price), ...]"""
    if not symbols:
        return {}
    try:
        import yfinance as yf
    except ImportError:
        return {}
    period = "5y"  # yfinance allows 5 years (free, no key needed)
    try:
        hist = yf.download(" ".join(symbols), period=period, interval="1d",
                           group_by="ticker", progress=False, threads=True,
                           auto_adjust=False)
    except Exception as e:
        print(f"[warn] yf history: {e}", file=sys.stderr)
        return {}
    out = {}
    for sym in symbols:
        try:
            if hasattr(hist.columns, "get_level_values") and sym in hist.columns.get_level_values(0):
                closes = hist[sym]["Close"].dropna()
            else:
                closes = hist["Close"].dropna() if "Close" in hist.columns else None
            if closes is None or len(closes) < 10:
                continue
            raw = [(int(d.timestamp()), float(v)) for d, v in closes.items()]
            out["$" + sym] = _downsample(raw)
            print(f"[hist] ${sym}: {len(raw)} raw → {len(out['$' + sym])} pts")
        except Exception as e:
            print(f"[warn] history {sym}: {e}", file=sys.stderr)
    return out


def _index_grid(days_sorted):
    """Grille de dates GLOBALE, partagée par tous les tokens du narratif.

    POURQUOI : `_downsample` garde `old[::7]` — un point sur 7 — en comptant
    depuis le PREMIER point de chaque token, donc depuis sa date de listing.
    Deux tokens listés à 3 jours d'écart retiennent deux jeux de jours disjoints.
    Sur l'union de ces jours (ancien code), une barre n'agrégeait qu'un token sur
    trois : au-delà des 90 derniers jours, 100 % des barres de la plupart des
    narratifs avaient ≤ 2 tokens présents. L'indice « narratif » n'était donc
    plus qu'une mosaïque de rendements hebdomadaires de tokens isolés.

    On reconstruit une grille unique, ancrée sur le jour le plus récent, cadencée
    comme le downsampling amont (quotidien ≤ 90 j, hebdo au-delà).
    """
    if not days_sorted:
        return []
    DAY = 86400
    ts_of = {}
    for d in days_sorted:
        try:
            ts_of[d] = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            continue
    days = [d for d in days_sorted if d in ts_of]
    if not days:
        return []
    now = ts_of[days[-1]]
    grid, last_ts = [], None
    for d in reversed(days):
        t = ts_of[d]
        step = 0 if (now - t) / DAY <= 90 else 7
        if last_ts is None or (last_ts - t) >= (step - 1) * DAY:
            grid.append(d)
            last_ts = t
    grid.reverse()
    return grid


def compute_narrative_index(narr_stat, histories, n_top=HIST_TOP_N_PER_NARRATIVE):
    """Narrative index = mcap-weighted returns-based index across top N tokens.
    Returns-based (like S&P500) with daily cap at ±30% to mute launch artifacts.
    Using multiple tokens gives each narrative a distinct signature, so narratives
    sharing their #1 token (e.g. BNB in L1 and Exchange Tokens) aren't perfectly
    correlated."""
    tokens = narr_stat["tokens"][:n_top]  # already sorted by mcap desc
    if not tokens:
        return None

    day_series = {}
    weights = {}
    used_symbols = []
    for t in tokens:
        key = ("$" + t["symbol"]) if t.get("is_stock") else t["id"]
        h = histories.get(key)
        if not h or len(h) < 30:  # require at least 30 days of data
            continue
        per_day = {}
        for ts, px in h:
            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if day not in per_day:
                per_day[day] = px
        if len(per_day) < 30:
            continue
        day_series[key] = per_day
        weights[key] = t["mcap"] or 1.0
        used_symbols.append(t["symbol"])

    if not day_series:
        return None

    # UNION of all dates — gives max historical depth
    all_days = set()
    for s in day_series.values():
        all_days.update(s.keys())
    if len(all_days) < 5:
        return None
    days_sorted = sorted(all_days)

    # Grille commune à tous les constituants (cf. _index_grid) : `_downsample`
    # amincit chaque token sur SA propre horloge (`old[::7]`, compté depuis sa
    # date de listing), donc les jours conservés sont déphasés d'un token à
    # l'autre. Sur l'union, une barre n'agrégeait qu'1 token sur 3 — hors des
    # 90 derniers jours, 100 % des barres de la plupart des narratifs. L'indice
    # « narratif » se réduisait au rendement d'un seul token, clippé à ±30 %.
    grid = _index_grid(days_sorted)
    if len(grid) < 5:
        grid = days_sorted

    # Forward-fill sur la grille : dernier cours connu ≤ date de grille, pour
    # CHAQUE token. Chaque barre couvre alors le même intervalle pour tout le
    # panier, et un token isolé ne peut plus piloter l'indice.
    ff = {}
    for k, series in day_series.items():
        own_days = sorted(series.keys())
        col, j, p, n_own = [], -1, 0, len(own_days)
        for g in grid:
            while p < n_own and own_days[p] <= g:
                j = p
                p += 1
            col.append(series[own_days[j]] if j >= 0 else None)
        ff[k] = col

    # ── Returns-based index (like S&P 500) ──
    # Starts at 100. Each bar: mcap-weighted average of the bar returns across
    # tokens already listed. New tokens entering contribute 0% on their first
    # bar (no jump artifact).
    values = [100.0]
    for i in range(1, len(grid)):
        # Clip extreme returns to ±30% to mute launch-day pumps / exchange
        # relistings / data glitches which would otherwise distort the index.
        w_daily_ret = 0.0
        w_sum = 0.0
        for k in day_series:
            prev_px = ff[k][i - 1]
            cur_px  = ff[k][i]
            if prev_px is None or cur_px is None or prev_px <= 0:
                continue  # token pas encore listé à cette date
            ret = (cur_px / prev_px) - 1.0
            ret = max(-0.30, min(0.30, ret))  # clip ±30%
            w_daily_ret += ret * weights[k]
            w_sum += weights[k]

        if w_sum > 0:
            w_daily_ret /= w_sum

        # Keep full float64 precision during compounding. Rounding here (e.g. to
        # 2 decimals) caused a stuck-plateau bug for narratives that drew down
        # > 99% from peak (DePIN starting at FIL 2021 peak): once the index fell
        # under ~0.005, every subsequent step rounded back to 0.01 regardless of
        # real return, producing a flat line and zero momentum on the chart.
        values.append(values[-1] * (1.0 + w_daily_ret))

    # Round only on final output (6 sig. figures = readable JSON + preserved
    # dynamic range down to ~1e-4 of the starting value).
    values_out = [round(v, 6) for v in values]
    return {
        "dates":  grid,
        "values": values_out,
        "tokens": [t["symbol"] for t in tokens if (("$" + t["symbol"]) in day_series) or (t.get("id") in day_series)],
    }


def fetch_btc_history(days=120):
    """Daily BTC prices for MA100 trend filter.
    MA100 sélectionnée après backtest 2010-2026 (MA100 = 5979× return, MA200 = 72×,
    risk-adjusted 85× meilleur). MA100 est mieux calibrée au cycle halving 4 ans."""
    url = f"{COINGECKO}/coins/bitcoin/market_chart?vs_currency=usd&days={days}&interval=daily"
    d = _http_json(url)
    if not d or "prices" not in d:
        return None
    return [p[1] for p in d["prices"]]


def compute_trend_filter(btc_prices):
    """Alpha ZEN Bouclier : BTC > MA100 + perf_30d > -5% -> ALPHA, sinon ZEN.
    MA100 calibrée sur le cycle halving BTC (4 ans), vs MA200 (canonique actions).
    Backtest 2010-2026: MA100 = 5979× return vs MA200 = 72×."""
    if not btc_prices or len(btc_prices) < 100:
        return {"mode": "unknown", "btc_px": None, "ma200": None,
                "perf_30d": None, "distance_ma200": None}
    px = btc_prices[-1]
    ma100 = sum(btc_prices[-100:]) / 100.0
    perf_30d = (px / btc_prices[-31] - 1.0) * 100.0 if len(btc_prices) > 31 else 0.0
    dist = (px / ma100 - 1.0) * 100.0
    if px > ma100 and perf_30d > -5:
        mode = "alpha"   # deployed on narratives
    elif px > ma100 and perf_30d <= -5:
        mode = "caution" # above MA but weakening
    else:
        mode = "zen"     # 100% stables
    # Clé "ma200" conservée pour compat UI existante (affichée comme "MA100" dans le Rmd),
    # "ma_window" = vraie fenêtre utilisée pour documentation
    return {
        "mode": mode,
        "btc_px": round(px, 0),
        "ma200":  round(ma100, 0),  # legacy key name, contient MA100 pour crypto
        "ma_window": 100,
        "perf_30d": round(perf_30d, 2),
        "distance_ma200": round(dist, 2),
    }


# ─────────────────────────────────────────────────────────────────────────
# SCORING ALPHA ZEN STYLE
#   score = 0.60 * price_momentum + 0.30 * news_momentum + 0.10 * volume
#   price_momentum = 0.5*perf_7d + 0.5*perf_30d (mcap-weighted across tokens)
# ─────────────────────────────────────────────────────────────────────────
def narrative_stats(narr, cfg, scan, assets, max_rank=MAX_MCAP_RANK):
    # Override per-narrative (ex : NFT dont les tokens sont hors top 300 par nature cyclique)
    max_rank = cfg.get("max_rank", max_rank)
    """
    assets: unified dict keyed by CG id (e.g. 'bitcoin') OR '$SYM' for stocks.
    - Crypto tokens: filtered by market_cap_rank <= max_rank.
    - Stocks: always included if data is available (no rank filter).
    """
    token_rows = []
    total_mcap = 0.0
    vol_sum    = 0.0

    # Combine crypto tokens + stocks into the same iteration
    asset_ids = list(cfg.get("tokens", [])) + [
        "$" + s for s in cfg.get("stocks", [])
    ]
    for aid in asset_ids:
        m = assets.get(aid)
        if not m:
            continue
        is_stock = bool(m.get("is_stock"))
        rank = m.get("market_cap_rank")
        if not is_stock:
            # Crypto rank filter
            if rank is None or rank > max_rank:
                continue
        mcap = m.get("market_cap") or 0
        vol  = m.get("total_volume") or 0
        p7  = m.get("price_change_percentage_7d_in_currency")
        p30 = m.get("price_change_percentage_30d_in_currency")
        sym_upper = (m.get("symbol") or "").upper()
        sym_lower = sym_upper.lower()
        # Logo chain — crypto: (1) CoinGecko CDN image (exists for all top 200
        # tokens incl. newer memecoins like PUMP, BONK, PENGU that are missing
        # from jsdelivr), (2) jsdelivr cryptocurrency-icons SVG fallback, (3)
        # letter avatar via narFaviconFallback if both 404.
        # Stocks : chain set by fetch_stocks() via (image, image_fallback).
        if is_stock:
            img_url      = m.get("image") or ""
            img_fallback = m.get("image_fallback") or ""
        else:
            cg_image = m.get("image") or ""
            jsdeliver_svg = (os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research")
                             if sym_lower else "")
            img_url      = cg_image or jsdeliver_svg
            img_fallback = jsdeliver_svg if cg_image and jsdeliver_svg and cg_image != jsdeliver_svg else ""
        token_rows.append({
            "id": aid,
            "symbol": sym_upper,
            "name": m.get("name") or aid,
            "image": img_url,
            "image_fallback": img_fallback,
            "price": m.get("current_price") or 0,
            "mcap": mcap,
            "mcap_rank": rank,
            "volume": vol,
            "perf_7d": p7,
            "perf_30d": p30,
            "is_stock": is_stock,
            "exchange": m.get("exchange") if is_stock else None,
        })
        total_mcap += mcap or 0
        vol_sum    += vol or 0
    # Sort by mcap desc inside the narrative
    token_rows.sort(key=lambda t: t["mcap"] or 0, reverse=True)

    # Outlier detection: if the top token holds >60% of total mcap,
    # the narrative's momentum is essentially driven by one asset.
    outlier = False
    dominant_pct = 0.0
    dominant_sym = None
    if token_rows and total_mcap > 0:
        top = token_rows[0]
        dominant_pct = (top["mcap"] or 0) / total_mcap
        dominant_sym = top["symbol"]
        if dominant_pct > 0.60 and len(token_rows) > 1:
            outlier = True

    # Price momentum weighted by mcap (fallback: equal)
    def w_avg(key):
        num = 0.0; den = 0.0
        for r in token_rows:
            v = r.get(key)
            if v is None:
                continue
            w = r["mcap"] if total_mcap > 0 else 1.0
            num += v * w
            den += w
        return (num / den) if den > 0 else None
    perf_7d_w  = w_avg("perf_7d")
    perf_30d_w = w_avg("perf_30d")
    if perf_7d_w is None and perf_30d_w is None:
        price_mom = 0.0
    else:
        a = perf_7d_w if perf_7d_w is not None else 0
        b = perf_30d_w if perf_30d_w is not None else 0
        price_mom = 0.5 * a + 0.5 * b  # in percent

    # News momentum from scan result
    today = datetime.now(timezone.utc)
    m7, mp7, m30, accel = news_momentum(scan[narr]["daily"], today)

    return {
        "narrative": narr,
        "icon": cfg["icon"],
        "color": cfg["color"],
        "tokens": token_rows,
        "total_mcap_b":  round(total_mcap / 1e9, 2),
        "total_volume_b": round(vol_sum / 1e9, 2),
        "perf_7d":  round(perf_7d_w, 2)  if perf_7d_w  is not None else None,
        "perf_30d": round(perf_30d_w, 2) if perf_30d_w is not None else None,
        "price_momentum": round(price_mom, 2),
        "mentions_7d":  m7,
        "mentions_prev7d": mp7,
        "mentions_30d": m30,
        "mention_accel": accel,   # -1..+3
        "mention_total": scan[narr]["total"],
        "daily_mentions": scan[narr]["daily"],
        "articles": scan[narr]["articles"][:8],
        "outlier":       outlier,
        "dominant_pct":  round(dominant_pct * 100, 1),
        "dominant_sym":  dominant_sym,
        "description":   NARRATIVE_DESC.get(narr, ""),
    }


def rank_normalize(values):
    """Percentile-rank normalization -> 0..100 scale (Alpha Zen style)."""
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if not valid:
        return [50.0] * len(values)
    valid.sort(key=lambda x: x[1])
    out = [50.0] * len(values)
    n = len(valid)
    for rank, (idx, _) in enumerate(valid):
        out[idx] = round(100.0 * rank / max(1, n - 1), 1) if n > 1 else 50.0
    return out


# ─────────────────────────────────────────────────────────────────────────
# MOMENTUM HELPERS (Thami Kabaj / Alpha Zen style — long-horizon + relatif)
# ─────────────────────────────────────────────────────────────────────────
def _perf_over_days(history, n_days):
    """Return pct over n_days from a [(ts_sec, px), ...] series."""
    if not history or len(history) < 2:
        return None
    last_ts, last_px = history[-1]
    target_ts = last_ts - n_days * 86400
    past_px = None
    for ts, px in history:
        if ts <= target_ts:
            past_px = px
        else:
            break
    if past_px is None or past_px <= 0:
        return None
    return (last_px / past_px - 1.0) * 100.0


def _above_ma50(history):
    """True if last price > 50-day SMA. Uses the daily-granularity tail."""
    if not history or len(history) < 30:
        return None
    last_ts = history[-1][0]
    cutoff = last_ts - 50 * 86400
    window = [px for ts, px in history if ts >= cutoff]
    if len(window) < 20:
        return None
    return history[-1][1] > (sum(window) / len(window))


def augment_with_momentum_metrics(stats_list, histories):
    """Ajoute momentum long-terme, relatif vs BTC, breadth, signal LONG/FLAT
    et trend_age_days. A appeler APRES compute_narrative_index."""
    btc_hist = histories.get("bitcoin")
    btc_90d  = _perf_over_days(btc_hist, 90)  if btc_hist else None
    btc_180d = _perf_over_days(btc_hist, 180) if btc_hist else None

    for s in stats_list:
        tokens = s["tokens"][:HIST_TOP_N_PER_NARRATIVE]

        # ── Momentum long terme (mcap-weighted, via histories) ──
        num90 = den90 = num180 = den180 = 0.0
        leaders_above = leaders_total = 0
        for t in tokens:
            key = ("$" + t["symbol"]) if t.get("is_stock") else t["id"]
            h = histories.get(key)
            if not h:
                continue
            w = t["mcap"] or 0
            p90  = _perf_over_days(h, 90)
            p180 = _perf_over_days(h, 180)
            if p90 is not None:
                num90 += p90 * w; den90 += w
            if p180 is not None:
                num180 += p180 * w; den180 += w
            above = _above_ma50(h)
            if above is not None:
                leaders_total += 1
                if above:
                    leaders_above += 1
        perf_90d_w  = (num90 / den90)   if den90  > 0 else None
        perf_180d_w = (num180 / den180) if den180 > 0 else None
        leaders_pct = (100.0 * leaders_above / leaders_total) if leaders_total else None

        # ── Momentum RELATIF vs BTC (cœur de l'approche) ──
        rel_mom_90d  = (perf_90d_w  - btc_90d)  if (perf_90d_w  is not None and btc_90d  is not None) else None
        rel_mom_180d = (perf_180d_w - btc_180d) if (perf_180d_w is not None and btc_180d is not None) else None

        # ── Breadth 30j : % de tokens du narratif avec perf_30d > 0 ──
        pos = total = 0
        for t in s["tokens"]:
            p = t.get("perf_30d")
            if p is None:
                continue
            total += 1
            if p > 0:
                pos += 1
        breadth_30d = (100.0 * pos / total) if total else None

        # ── Signal LONG/FLAT & trend_age (index narratif vs MA50) ──
        signal = "flat"
        signal_reason = "no data"
        trend_age = 0
        h_idx = s.get("history")
        if h_idx and h_idx.get("values") and len(h_idx["values"]) >= 50:
            vals = h_idx["values"]
            ma50 = sum(vals[-50:]) / 50.0
            last = vals[-1]
            above_ma = last > ma50
            ok_breadth = breadth_30d is not None and breadth_30d > 50.0
            if above_ma and ok_breadth:
                signal = "long"
                signal_reason = f"idx>MA50 & breadth {breadth_30d:.0f}%>50%"
            elif above_ma:
                signal_reason = f"idx>MA50 mais breadth {breadth_30d:.0f}%≤50%"
            elif ok_breadth:
                signal_reason = "breadth OK mais idx<MA50"
            else:
                signal_reason = "idx<MA50 & breadth≤50%"
            # Trend age : nb de jours consécutifs dans le régime actuel
            current_sign = None
            for i in range(len(vals) - 1, 48, -1):
                ma = sum(vals[i-49:i+1]) / 50.0
                sign = vals[i] > ma
                if current_sign is None:
                    current_sign = sign
                    trend_age = 1
                elif sign == current_sign:
                    trend_age += 1
                else:
                    break

        s["perf_90d_w"]             = round(perf_90d_w, 2)  if perf_90d_w  is not None else None
        s["perf_180d_w"]            = round(perf_180d_w, 2) if perf_180d_w is not None else None
        s["rel_mom_90d"]            = round(rel_mom_90d, 2) if rel_mom_90d is not None else None
        s["rel_mom_180d"]           = round(rel_mom_180d, 2) if rel_mom_180d is not None else None
        s["breadth_30d"]            = round(breadth_30d, 1) if breadth_30d is not None else None
        s["leaders_above_ma50_pct"] = round(leaders_pct, 1) if leaders_pct is not None else None
        s["signal"]                 = signal
        s["signal_reason"]          = signal_reason
        s["trend_age_days"]         = trend_age


def compute_composite(stats_list):
    """Composite 0..100 — style momentum cyclique (Thami Kabaj / Alpha ZEN):
        50% momentum RELATIF vs BTC (90j)     → force du narratif, pas juste son prix
        20% breadth (% tokens narratif >0 sur 30j) → confirme la largeur du move
        20% momentum court terme (7j+30j)      → réactivité / timing
        10% news acceleration                   → attention (poids réduit, signal bruité)
    """
    rel_vals     = [s.get("rel_mom_90d")    for s in stats_list]
    breadth_vals = [s.get("breadth_30d")    for s in stats_list]
    px_vals      = [s.get("price_momentum") for s in stats_list]
    news_vals    = [s.get("mention_accel")  for s in stats_list]

    rel_rk     = rank_normalize(rel_vals)
    breadth_rk = rank_normalize(breadth_vals)
    px_rk      = rank_normalize(px_vals)
    news_rk    = rank_normalize(news_vals)

    for i, s in enumerate(stats_list):
        s["score_rel_mom"] = rel_rk[i]
        s["score_breadth"] = breadth_rk[i]
        s["score_price"]   = px_rk[i]
        s["score_news"]    = news_rk[i]
        s["score"] = round(
            0.50 * rel_rk[i] + 0.20 * breadth_rk[i]
            + 0.20 * px_rk[i] + 0.10 * news_rk[i], 1
        )
    stats_list.sort(key=lambda s: s["score"], reverse=True)
    for i, s in enumerate(stats_list):
        s["rank"] = i + 1
    return stats_list


# ─────────────────────────────────────────────────────────────────────────
# BREADTH HISTORY — ampleur du marché dans le temps (pour la jauge du dashboard)
# ─────────────────────────────────────────────────────────────────────────
def compute_breadth_history(narratives, histories, key_fn,
                            step_days=7, max_points=180, span_days=30,
                            min_narr_frac=0.4, recent_tol_days=14):
    """Série historique = à chaque date, moyenne SUR LES NARRATIFS du % de tokens
    du narratif dont le rendement {span_days}j est > 0. Même définition que la
    jauge breadth (breadth_30d moyen), reconstruite dans le temps depuis les
    historiques de prix par token. Renvoie (series, breadth_now) avec
    series = [{"t": ts_sec, "breadth": float, "n": int}] en ordre chronologique.
    breadth_now (= dernier point) sert de valeur d'aiguille → aiguille == fin de
    courbe par construction."""
    H = {}
    for k, v in histories.items():
        try:
            pts = sorted((int(a), float(b)) for a, b in v if b and float(b) > 0)
        except Exception:
            continue
        if len(pts) >= 2:
            H[k] = pts
    if not H:
        return [], None
    now = max(pts[-1][0] for pts in H.values())
    span = span_days * 86400
    rtol = recent_tol_days * 86400
    N = len(narratives)
    min_narr = max(5, int(round(N * min_narr_frac)))

    def px_at(pts, target, require_recent):
        lo, hi, idx = 0, len(pts) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if pts[mid][0] <= target:
                idx = mid; lo = mid + 1
            else:
                hi = mid - 1
        if idx < 0:
            return None
        if require_recent and (target - pts[idx][0]) > rtol:
            return None
        return pts[idx][1]

    narr_keys = []
    for n in narratives:
        ks = [key_fn(t) for t in n.get("tokens", []) if key_fn(t) in H]
        narr_keys.append(ks)

    series = []
    for i in range(max_points):
        t = now - i * step_days * 86400
        if t <= 0:
            break
        per = []
        for ks in narr_keys:
            pos = tot = 0
            for k in ks:
                pts = H[k]
                p_now = px_at(pts, t, True)
                p_old = px_at(pts, t - span, False)
                if p_now is None or p_old is None or p_old <= 0:
                    continue
                tot += 1
                if (p_now / p_old - 1.0) > 0:
                    pos += 1
            if tot > 0:
                per.append(100.0 * pos / tot)
        if len(per) >= min_narr:
            series.append({"t": int(t), "breadth": round(sum(per) / len(per), 2), "n": len(per)})
        elif series:
            break
    series.reverse()
    return series, (series[-1]["breadth"] if series else None)


def _breadth_key_crypto(t):
    return ("$" + t["symbol"]) if t.get("is_stock") else t["id"]


# ─────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────
def main():
    # Single-instance lock + yfinance DB reset (same defenses as fetch_tradfi.py)
    _lock = acquire_singleton_lock()
    if _lock is None:
        sys.exit(0)
    reset_yfinance_cache()
    _init_yfinance_cache()
    prev_cache = load_previous_cache()

    if not NEWS_CACHE.exists():
        print(f"[error] {NEWS_CACHE} missing; run fetch_news.py first.", file=sys.stderr)
        sys.exit(1)
    with open(NEWS_CACHE, "r", encoding="utf-8") as f:
        news_data = json.load(f)
    crypto_news = news_data.get("crypto", [])
    print(f"[info] {len(crypto_news)} crypto articles loaded")

    scan = scan_news(crypto_news)
    total_matches = sum(v["total"] for v in scan.values())
    print(f"[info] {total_matches} narrative matches across {len(NARRATIVES)} narratives")

    all_ids = sorted({tid for cfg in NARRATIVES.values() for tid in cfg.get("tokens", [])})
    print(f"[info] fetching CoinGecko markets for {len(all_ids)} tokens…")
    mkts = fetch_cg_markets(all_ids)
    print(f"[info] got prices for {len(mkts)}/{len(all_ids)} tokens")

    all_stocks = sorted({s for cfg in NARRATIVES.values() for s in cfg.get("stocks", [])})
    print(f"[info] fetching yfinance quotes for {len(all_stocks)} stocks…")
    stocks_data = fetch_stocks(all_stocks)
    print(f"[info] got prices for {len(stocks_data)}/{len(all_stocks)} stocks (live)")

    # ── Gap-fill from previous cache (last known good values, marked stale) ──
    prev_assets = {}
    if prev_cache.get("narratives"):
        for n in prev_cache["narratives"]:
            for t in n.get("tokens", []):
                tid = t.get("id")
                if tid:
                    prev_assets[tid] = t
    filled_stale = 0
    for tid in all_ids:
        if tid in mkts:
            continue
        prev = prev_assets.get(tid)
        if not prev:
            continue
        mkts[tid] = {**prev, "_stale": True, "_source": prev.get("_source", "prev_cache")}
        filled_stale += 1
    for sym in all_stocks:
        key = "$" + sym
        if key in stocks_data:
            continue
        prev = prev_assets.get(key)
        if not prev:
            continue
        stocks_data[key] = {**prev, "_stale": True, "_source": prev.get("_source", "prev_cache")}
        filled_stale += 1
    if filled_stale:
        print(f"[info] gap-filled {filled_stale} assets from previous cache (marked _stale=true)")
    total_assets = len(all_ids) + len(all_stocks)
    fresh = (len(mkts) - sum(1 for v in mkts.values() if v.get("_stale"))) \
          + (len(stocks_data) - sum(1 for v in stocks_data.values() if v.get("_stale")))
    coverage_pct = round(100 * fresh / max(1, total_assets), 1)
    print(f"[info] live coverage: {coverage_pct}% ({fresh}/{total_assets} fresh)")

    # Unified assets dict: crypto ids + '$SYM' stock ids
    assets = {**mkts, **stocks_data}

    print("[info] fetching BTC history for trend filter…")
    btc_hist = fetch_btc_history(220)
    trend = compute_trend_filter(btc_hist or [])
    print(f"[info] BTC trend filter = {trend['mode']} (px={trend['btc_px']}, ma100={trend['ma200']})")

    stats_list = []
    for narr, cfg in NARRATIVES.items():
        s = narrative_stats(narr, cfg, scan, assets)
        stats_list.append(s)

    # ─────────── HISTORICAL INDEX PER NARRATIVE ───────────
    # Collect top N tokens per narrative
    # Build {cg_id: {symbol,name,price}} map for Yahoo + stock symbols for yfinance.
    # name + price servent à résoudre/valider le symbole Yahoo (anti-collision).
    needed_cg = {}   # cg_id -> {"symbol","name","price"}
    needed_yf = set()
    for s in stats_list:
        for t in s["tokens"][:HIST_TOP_N_PER_NARRATIVE]:
            if t.get("is_stock"):
                needed_yf.add(t["symbol"])
            else:
                needed_cg[t["id"]] = {
                    "symbol": t.get("symbol"),
                    "name": t.get("name"),
                    "price": t.get("price"),
                }

    cached_hist, age_h = _history_cache_load()
    if cached_hist and age_h < HIST_CACHE_TTL_HOURS and \
       set(cached_hist.keys()) >= set(needed_cg.keys()) | {"$" + s for s in needed_yf}:
        print(f"[info] using cached histories (age {age_h:.1f}h, {len(cached_hist)} assets)")
        histories = {k: [(int(a), float(b)) for a, b in v] for k, v in cached_hist.items()}
    else:
        print(f"[info] fetching histories via Yahoo: {len(needed_cg)} crypto + {len(needed_yf)} stocks "
              f"({HIST_DAYS}d each) — est. ~{int(len(needed_cg)*0.5 + 10)}s")
        token_hist = fetch_token_histories(needed_cg, days=HIST_DAYS)
        stock_hist = fetch_stock_histories(needed_yf, days=HIST_DAYS)
        histories = {**token_hist, **stock_hist}
        # Gap-fill histories from previous narratives_history_cache.json (on disk)
        if HIST_CACHE.exists():
            try:
                with open(HIST_CACHE, "r", encoding="utf-8") as f:
                    prev_h = json.load(f)
                h_filled = 0
                wanted_keys = set(needed_cg.keys()) | {"$" + s for s in needed_yf}
                for k in wanted_keys:
                    if k in histories and len(histories[k]) >= 10:
                        continue
                    ph = prev_h.get(k)
                    if ph and len(ph) >= 10:
                        histories[k] = [(int(a), float(b)) for a, b in ph]
                        h_filled += 1
                if h_filled:
                    print(f"[info] gap-filled {h_filled} histories from previous cache")
            except Exception as e:
                print(f"[warn] could not read prev hist cache: {e}", file=sys.stderr)
        try:
            with open(HIST_CACHE, "w", encoding="utf-8") as f:
                json.dump({k: [[a, b] for a, b in v] for k, v in histories.items()}, f)
            print(f"[ok] wrote history cache ({len(histories)} assets)")
        except Exception as e:
            print(f"[warn] failed to write history cache: {e}", file=sys.stderr)

    for s in stats_list:
        s["history"] = compute_narrative_index(s, histories)

    # ─────────── MOMENTUM METRICS (long-horizon + relatif BTC + breadth + signal) ───────────
    augment_with_momentum_metrics(stats_list, histories)
    stats_list = compute_composite(stats_list)

    # Coverage stat: how many unique top-200 crypto tokens end up in at least 1 narrative
    covered_ids = set()
    for s in stats_list:
        for t in s["tokens"]:
            if not t.get("is_stock") and t.get("mcap_rank"):
                covered_ids.add(t["id"])

    # ── Historique LONG de BTC (~11-12 ans) pour graphe régimes ZEN/ALPHA ──
    # Source : Yahoo Finance v8 chart (BTC-USD, daily, depuis oct. 2014).
    # CryptoCompare (racheté par CoinDesk) exige désormais une clé API → mort
    # pour l'accès gratuit. Yahoo bloque urllib (429) mais répond via curl_cffi
    # impersonate (cf. project_yahoo_curl_cffi_required). Cache de secours dédié
    # pour ne jamais re-vider le graphe sur un échec ponctuel.
    btc_longhist = []
    BTC_LONGHIST_CACHE = CACHE_DIR / "btc_longhist_cache.json"
    try:
        from curl_cffi import requests as _creq
        p1 = 1412121600  # 2014-10-01 (premier point BTC-USD sur Yahoo)
        p2 = int(time.time())
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD"
               f"?period1={p1}&period2={p2}&interval=1d")
        r = _creq.get(url, impersonate="chrome120", timeout=30)
        d = r.json()
        res = (d.get("chart", {}).get("result") or [None])[0]
        all_pts = []
        if res:
            ts = res.get("timestamp", []) or []
            closes = (res.get("indicators", {}).get("quote", [{}])[0] or {}).get("close", []) or []
            all_pts = [(int(t), float(c)) for t, c in zip(ts, closes)
                       if c is not None and c > 0]
        if all_pts:
            # Dédup par jour UTC + tri
            seen = set(); uniq = []
            for tstamp, px in sorted(all_pts):
                day = datetime.fromtimestamp(tstamp, tz=timezone.utc).strftime("%Y-%m-%d")
                if day in seen: continue
                seen.add(day); uniq.append((tstamp, px))
            uniq = [(t, px) for t, px in uniq if px >= 0.001]
            # Downsample: daily sur les 2 dernières années, hebdo avant
            cutoff = time.time() - 730 * 86400
            old    = [p for p in uniq if p[0] < cutoff]
            recent = [p for p in uniq if p[0] >= cutoff]
            old_weekly = old[::7]
            btc_longhist = [[t, round(px, 4)] for t, px in old_weekly + recent]
            print(f"[ok] BTC long history (Yahoo): {len(uniq)} raw → {len(btc_longhist)} pts (downsampled)")
            try:
                with open(BTC_LONGHIST_CACHE, "w", encoding="utf-8") as f:
                    json.dump(btc_longhist, f)
            except Exception as e:
                print(f"[warn] failed to write btc_longhist cache: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] BTC longhist fetch failed: {e}", file=sys.stderr)
    # Fallback : recharge le dernier btc_longhist connu si le fetch a échoué
    if not btc_longhist and BTC_LONGHIST_CACHE.exists():
        try:
            with open(BTC_LONGHIST_CACHE, "r", encoding="utf-8") as f:
                btc_longhist = json.load(f)
            print(f"[info] btc_longhist from fallback cache ({len(btc_longhist)} pts)")
        except Exception as e:
            print(f"[warn] could not read btc_longhist cache: {e}", file=sys.stderr)

    # ── Ampleur du marché dans le temps (jauge dashboard) ──
    breadth_series, breadth_now = compute_breadth_history(stats_list, histories, _breadth_key_crypto)
    print(f"[ok] breadth_history: {len(breadth_series)} pts, now={breadth_now}")

    out = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "news_updated": news_data.get("updated"),
        "articles_scanned": len(crypto_news),
        "total_matches": total_matches,
        "trend_filter": trend,
        "coverage": {
            "crypto_tokens_in_narratives": len(covered_ids),
            "stocks_in_narratives": len(stocks_data),
            "max_mcap_rank": MAX_MCAP_RANK,
            "live_pct": coverage_pct,
            "stale_filled": filled_stale,
        },
        "narratives": stats_list,
        "btc_longhist": btc_longhist,  # ~13 ans pour graphe régimes ZEN/ALPHA
        "breadth_history": breadth_series,  # % tokens en hausse 30j, moyenne narratifs
        "breadth_now": breadth_now,         # dernier point = valeur d'aiguille jauge
    }
    with open(OUT_CACHE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[ok] wrote {OUT_CACHE}")
    # Cache JS live consomme par Narrative_Tracker.html (override de __CRYPTO_DATA__ inline)
    with open(OUT_CACHE_JS, "w", encoding="utf-8") as f:
        f.write("window.__CRYPTO_LIVE__=" + json.dumps(out, ensure_ascii=False, separators=(",", ":")) + ";\n")
    print(f"[ok] wrote {OUT_CACHE_JS}")
    # Wrapper LEGER pour la tuile "Mode Crypto" de l'Accueil (file://-safe, MEME
    # source trend_filter que le badge ZEN/ALPHA de Narrative_Tracker). Evite de
    # charger les ~500K du cache complet juste pour 2 champs. Reecrit a chaque run
    # => la tuile reste live et coherente sans re-render de index.Rmd.
    mode_js = CACHE_DIR / "mode_crypto_live.js"
    mode_crypto = {
        "mode": trend.get("mode"),
        "dist_pct": trend.get("distance_ma200"),
        "price": trend.get("btc_px"),
        "ma": trend.get("ma200"),
        "perf_30d": trend.get("perf_30d"),
        "ref_asset": "BTC",
        "ma_label": "MA100",
        "updated": out.get("updated"),
    }
    with open(mode_js, "w", encoding="utf-8") as f:
        f.write("window.__MODE_CRYPTO_LIVE__=" + json.dumps(mode_crypto, ensure_ascii=False, separators=(",", ":")) + ";\n")
    print(f"[ok] wrote {mode_js}")
    print("\nTop 10 narratives (momentum cyclique):")
    for s in stats_list[:10]:
        rel = s.get("rel_mom_90d")
        br  = s.get("breadth_30d")
        sig = s.get("signal", "?").upper()
        age = s.get("trend_age_days", 0)
        rel_s = f"{rel:+6.1f}%" if rel is not None else "  n/a "
        br_s  = f"{br:4.0f}%"   if br  is not None else " n/a"
        print(f"  #{s['rank']:>2} {s['narrative']:<22} score={s['score']:5.1f}  "
              f"[{sig:4} {age:>3}j]  rel90={rel_s}  breadth={br_s}  "
              f"px={s['price_momentum']:+5.1f}%")


if __name__ == "__main__":
    main()
