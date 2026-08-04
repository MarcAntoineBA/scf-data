#!/usr/bin/env python3
"""Crypto Narrative Fundamentals — mcap-weighted metrics per narrative.

Parallel de fetch_tradfi_fundamentals.py mais pour la crypto.

Sources :
- CoinGecko /coins/markets (batch) : mcap, FDV, volume, circulating, perf 7d/30d/1y
- DeFiLlama /protocols : TVL par gecko_id match
- DeFiLlama /overview/fees?dataType=dailyFees : FEES 1y top-line par gecko_id
  (= "Sales" en termes TradFi — montant total payé par les users, pas la part
   protocole/holders. Switch 2026-06-04 depuis dailyRevenue : un P/S compare
   le prix au CA top-line, comme Yahoo totalRevenue pour les actions.)

Métriques par narrative (pondérées par market cap) :
- mcap_total_b, fdv_total_b, volume_total_b
- circ_pct : circulating / FDV
- vol_mcap_pct : volume 24h / market cap (proxy liquidité)
- tvl_total_b, mc_tvl : MC / TVL (valorisation vs TVL du secteur DeFi)
- ps_ttm : MC_secteur / Fees_TTM_secteur (Fees = top-line sales, pas revenue holders)
- perf_7d, perf_30d, perf_1y
- dominance_pct : mcap_top_token / mcap_total × 100
- n_tokens, n_with_tvl, n_with_rev

Output :
- narratives_fundamentals_cache.json
- narratives_fundamentals_cache.js (window.__NARRATIVES_FUNDAMENTALS__)
"""
# ── Global timeout safeguard (30 min) — auto-tué si bloqué sur un I/O réseau,
#    libère le lock pour le prochain cycle launchd. Sans ça, un script bloqué
#    monopolise indéfiniment le verrou et empêche tous les refresh suivants.
import signal as _signal, sys as _sys
def _global_timeout_handler(signum, frame):
    print(f"[fatal] global timeout (30 min) reached — aborting to free lock for next launchd cycle.", file=_sys.stderr)
    _sys.exit(2)
try:
    _signal.signal(_signal.SIGALRM, _global_timeout_handler)
    _signal.alarm(30 * 60)
except Exception:
    pass

import json
import os
import random
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "narratives_fundamentals_cache.json"
OUT_JS   = CACHE_DIR / "narratives_fundamentals_cache.js"
TRACKER_CACHE = CACHE_DIR / "narratives_cache.json"   # written by fetch_narratives.py
LOCK_FILE = CACHE_DIR / "narratives_fundamentals.lock"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def acquire_singleton_lock():
    import fcntl, os
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print("[lock] another fetch_narratives_fundamentals instance is running — exiting", file=sys.stderr)
        fd.close()
        return None
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


def load_previous_cache():
    if not OUT_JSON.exists():
        return {}
    try:
        with OUT_JSON.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] could not load prev funda cache: {e}", file=sys.stderr)
        return {}


def load_tracker_data():
    """Read narratives_cache.json (price + mcap + momentum). Returns:
        {
          'tokens':   {cg_id: {price, mcap, volume, perf_7d, perf_30d}},
          'momentum': {narrative_name: {score, signal, rank, rel_mom_90d, breadth_30d, ...}},
          'updated':  '...'
        }
    Single source of truth for prices/mcap. Empty if cache missing — funda
    will still run but with no cross-data."""
    if not TRACKER_CACHE.exists():
        print(f"[warn] {TRACKER_CACHE.name} missing — funda will use only CoinGecko prices", file=sys.stderr)
        return {"tokens": {}, "momentum": {}, "updated": None}
    try:
        with TRACKER_CACHE.open("r", encoding="utf-8") as f:
            tc = json.load(f)
    except Exception as e:
        print(f"[warn] cannot read {TRACKER_CACHE.name}: {e}", file=sys.stderr)
        return {"tokens": {}, "momentum": {}, "updated": None}
    tokens = {}
    stocks_by_narrative = {}   # {narrative_name: [{symbol, name, mcap_b, perf_*, ...}, …]}
    momentum = {}
    for n in tc.get("narratives", []):
        sec = n.get("narrative")
        if sec:
            momentum[sec] = {
                "score":            n.get("score"),
                "rank":             n.get("rank"),
                "signal":           n.get("signal"),
                "signal_reason":    n.get("signal_reason"),
                "trend_age_days":   n.get("trend_age_days"),
                "rel_mom_90d":      n.get("rel_mom_90d"),
                "rel_mom_180d":     n.get("rel_mom_180d"),
                "breadth_30d":      n.get("breadth_30d"),
                "price_momentum":   n.get("price_momentum"),
                "perf_90d_w":       n.get("perf_90d_w"),
                "perf_180d_w":      n.get("perf_180d_w"),
            }
        n_stocks = []
        for t in n.get("tokens", []):
            tid = t.get("id") or t.get("symbol")
            if t.get("is_stock"):
                # Stock entry → injecté plus bas dans l'agrégation du narratif
                # afin que le panier "fundamentals" reflète EXACTEMENT le panier
                # "tracker" (single source of truth — mêmes constituants partout).
                mcap_usd = t.get("mcap") or 0
                if mcap_usd <= 0:
                    continue
                n_stocks.append({
                    "id":         "stock:" + (t.get("symbol") or tid),
                    "symbol":     t.get("symbol") or tid,
                    "name":       t.get("name") or t.get("symbol") or tid,
                    "image":      t.get("image"),
                    "is_stock":   True,
                    "price":      t.get("price"),
                    "mcap_b":     round(mcap_usd / 1e9, 3),
                    "fdv_b":      round(mcap_usd / 1e9, 3),  # actions : pas de FDV crypto-style → on prend mcap
                    "vol_b":      round((t.get("volume") or 0) / 1e9, 3),
                    "circ_pct":   100.0,                     # toutes les actions outstanding sont en circulation
                    "vol_mcap_pct": None,
                    "tvl_b":      None,                      # pas de TVL on-chain pour une action
                    "mc_tvl":     None,
                    "rev_m_1y":   None,                      # revenue boursier ≠ revenue on-chain (couvert par PER tradfi)
                    "ps_ttm":     None,
                    "perf_7d":    t.get("perf_7d"),
                    "perf_30d":   t.get("perf_30d"),
                    "perf_1y":    t.get("perf_1y"),
                    "_price_source": "tracker_stock",
                })
            elif tid and tid not in tokens:
                tokens[tid] = {
                    "price":    t.get("price"),
                    "mcap":     t.get("mcap"),
                    "volume":   t.get("volume"),
                    "perf_7d":  t.get("perf_7d"),
                    "perf_30d": t.get("perf_30d"),
                    "_stale":   t.get("_stale"),
                    "_source":  t.get("_source"),
                }
        if sec and n_stocks:
            stocks_by_narrative[sec] = n_stocks
    n_stocks_total = sum(len(v) for v in stocks_by_narrative.values())
    print(f"[info] tracker data loaded: {len(tokens)} tokens + {n_stocks_total} stocks (across {len(stocks_by_narrative)} narratives), {len(momentum)} narratives (cache mtime: {tc.get('updated')})")
    return {"tokens": tokens, "stocks_by_narrative": stocks_by_narrative, "momentum": momentum, "updated": tc.get("updated")}

try:
    from fetch_narratives import NARRATIVES
except Exception as e:
    print(f"[fatal] Cannot import NARRATIVES: {e}", file=sys.stderr)
    sys.exit(1)


def _http_get_json(url, timeout=45, attempts=4, sleep=2.0, rate_limit_sleep=35.0):
    """Simple GET with retry + backoff.

    For 429 (rate limit), sleep at least `rate_limit_sleep` seconds
    (CoinGecko free tier = 30/min, so wait >= 30s before retry).
    """
    last_err = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                wait = rate_limit_sleep * (i + 1)
                print(f"[429] rate-limited, sleeping {wait:.0f}s (attempt {i+1}/{attempts})", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code >= 500:
                time.sleep(sleep * (2 ** i))
                continue
            raise
        except Exception as e:
            last_err = e
            time.sleep(sleep * (2 ** i))
    raise RuntimeError(f"GET {url}: {last_err}")


# ─── CoinGecko batch fetch ──────────────────────────────────────────────
def fetch_coingecko_markets(cg_ids):
    """Returns dict cg_id -> market data."""
    out = {}
    # CoinGecko API free : per_page max 250, but we use 80 per batch to be safe
    batch_size = 80
    for i in range(0, len(cg_ids), batch_size):
        batch = cg_ids[i:i+batch_size]
        url = (
            "https://api.coingecko.com/api/v3/coins/markets"
            + "?vs_currency=usd"
            + "&ids=" + urllib.parse.quote(",".join(batch))
            + f"&per_page={batch_size}&page=1"
            + "&price_change_percentage=7d,30d,1y"
        )
        try:
            data = _http_get_json(url, timeout=30)
        except Exception as e:
            print(f"[warn] CG batch #{i//batch_size}: {e}", file=sys.stderr)
            continue
        for row in data or []:
            cid = row.get("id")
            if not cid:
                continue
            out[cid] = row
        print(f"[cg] batch {i//batch_size + 1}: got {len(data or [])} rows")
        time.sleep(5.0)  # polite rate-limit (free tier ~30/min, stay safe)
    return out


# ─── DeFiLlama TVL + Revenue ────────────────────────────────────────────
def fetch_defillama_protocols():
    """Returns list of /protocols rows (raw)."""
    try:
        return _http_get_json("https://api.llama.fi/protocols", timeout=60) or []
    except Exception as e:
        print(f"[warn] DefiLlama /protocols: {e}", file=sys.stderr)
        return []


def fetch_defillama_chains_tvl():
    """Returns (out, web_map) tuple :
       - out      : gecko_id -> chain_tvl_usd
       - web_map  : gecko_id -> DeFiLlama UI chain name (pour /chain/{X} audit link)
    """
    try:
        rows = _http_get_json("https://api.llama.fi/v2/chains", timeout=60) or []
    except Exception as e:
        print(f"[warn] DefiLlama /v2/chains: {e}", file=sys.stderr)
        return {}, {}
    out = {}
    web_map = {}
    for c in rows:
        gid = c.get("gecko_id")
        tvl = c.get("tvl") or 0
        if not gid or tvl <= 0:
            continue
        out[gid] = out.get(gid, 0) + tvl
        # On capture le name UI tel qu'utilisé par DeFiLlama (ex: "Ripple",
        # "Stellar", "Ethereum") pour permettre au frontend de construire un
        # lien /chain/{name} où l'utilisateur retrouve la TVL en headline.
        # On garde le PREMIER name vu (cas Ethereum partage gecko avec L2,
        # mais on veut /chain/Ethereum, pas /chain/Base).
        if gid not in web_map:
            web_map[gid] = c.get("name") or gid
    L2_TVL_INHERITS_ETH = {"base", "linea", "scroll", "blast", "ink"}
    for c in rows:
        slug = (c.get("name") or "").lower().replace(" ", "-")
        if slug in L2_TVL_INHERITS_ETH and not c.get("gecko_id"):
            tvl = c.get("tvl") or 0
            if tvl > 0:
                out["ethereum"] = out.get("ethereum", 0) + tvl
    n = sum(1 for v in out.values() if v > 1e8)
    print(f"[info] chain TVL captured for {n} chains (>$0.1B)", file=sys.stderr)
    return out, web_map


def build_tvl_map(protocols_rows):
    """gecko_id -> (total_tvl_usd, primary_slug).

    DefiLlama découpe chaque protocole en plusieurs slugs (uniswap-v3, uniswap-v2,
    aave-v3, aave-v2, etc.) et le `gecko_id` n'est rempli QUE sur certaines
    versions — d'où le bug où UNI/AAVE/CRV/MKR/PANCAKE n'avaient pas de TVL alors
    qu'ils en ont massivement.

    Fix : on agrège les TVL de TOUTES les versions qui partagent le même
    `parentProtocol` ou le même `gecko_id`. Pour la majorité des protocoles, le
    champ `parentProtocol` (ex : "parent#aave") regroupe les versions.

    Stratégie en 3 passes :
    1. Index direct par gecko_id : prends le TVL le + élevé par gecko_id
    2. Pour les protocoles avec parentProtocol mais pas de gecko_id : agrège
       sous le gecko_id du "parent" (déterminé par d'autres slugs du même parent)
    3. Heuristique slug → gecko_id pour les top protocoles non matchés

    Filtre catégorie : on EXCLUT
      - "CEX" : les exchanges centralisés (OKX/Gate/Bitkub avec gecko_id pollue
        Exchange Tokens avec $25B+ de réserves CEX qui ne sont PAS de la TVL DeFi)
      - "Chain" : la TVL de la chaîne elle-même, déjà couverte par ses protocoles
    """
    # Catégories à EXCLURE du calcul TVL (pas de la "Total Value Locked" DeFi).
    EXCLUDED_CATEGORIES = {"CEX", "Chain"}

    # ── Heuristique slug → gecko_id pour les grands protocoles connus ──
    # Utilisée UNIQUEMENT si aucun matching via direct gecko_id ni via parent.
    # NB: éviter le double counting — un slug qui a été matché par parent ne
    # DOIT PAS être re-matché ici (sinon AAVE V3 → +14B compté 2× → 28B au lieu de 14B).
    SLUG_TO_GECKO = {
        # token-symbol → gecko_id (CoinGecko canonical id)
        "uniswap": "uniswap",
        "uniswap-labs": "uniswap",
        "uniswap-v2": "uniswap",
        "uniswap-v3": "uniswap",
        "uniswap-v4": "uniswap",
        "aave": "aave",
        "aave-v1": "aave",
        "aave-v2": "aave",
        "aave-v3": "aave",
        "compound": "compound-governance-token",
        "compound-v3": "compound-governance-token",
        "compound-finance": "compound-governance-token",
        "curve": "curve-dao-token",
        "curve-finance": "curve-dao-token",
        "curve-dex": "curve-dao-token",
        "pancakeswap-amm": "pancakeswap-token",
        "pancakeswap": "pancakeswap-token",
        "pancakeswap-amm-v3": "pancakeswap-token",
        "makerdao": "maker",
        "sky-lending": "sky",
        "sky": "sky",
        "morpho-blue": "morpho",
        "morpho-aave-v3": "morpho",
        "aerodrome-slipstream": "aerodrome-finance",
        "aerodrome-v1": "aerodrome-finance",
        "raydium": "raydium",
        "raydium-amm": "raydium",
        "raydium-clmm": "raydium",
        "jupiter": "jupiter-exchange-solana",
        "jupiter-aggregator": "jupiter-exchange-solana",
        "balancer": "balancer",
        "balancer-v2": "balancer",
        "balancer-v3": "balancer",
        "1inch-aggregation-protocol": "1inch",
        "1inch-network": "1inch",
        "ethena": "ethena",
        "ethena-usde": "ethena",
        "frax-finance": "frax-share",
        "frax-ether": "frax-share",
        "rocket-pool": "rocket-pool",
        "lido": "lido-dao",
        "convex-finance": "convex-finance",
        # Ajouts 2026-05-21 (audit) : protocoles top mcap sans gecko_id ni parent
        # OU avec un gecko_id sur un slug minoritaire (perte d'agrégation).
        "jito": "jito-governance-token",
        "jito-restaking": "jito-governance-token",
        "pyth-network": "pyth-network",
        "chainlink-ccip": "chainlink",
        "chainlink-data-feeds": "chainlink",
        "chainlink": "chainlink",
        "frax": "frax-share",
        "frax-finance-staked-frax": "frax-share",
        "liquity": "liquity",
        "liquity-v2": "liquity",
        "ondo-finance": "ondo-finance",
        "ondo-flux-finance": "ondo-finance",
    }

    # ── Pass 1 : index parentProtocol → gecko_id (depuis les versions qui en ont) ──
    parent_to_gecko = {}
    for p in protocols_rows:
        parent = p.get("parentProtocol")
        gid = p.get("gecko_id")
        if parent and gid and parent not in parent_to_gecko:
            parent_to_gecko[parent] = gid

    # Override manuel pour les parents dont AUCUNE version n'a de gecko_id
    # renseigné dans /protocols (sinon ils tombent dans la heuristique slug,
    # qui ne couvre pas tous les slugs leaf comme `jito-liquid-staking`).
    # Audit complet 2026-05-21 : on a parcouru toutes les revenue > $5M/an
    # côté DefiLlama et ajouté les parents qui MAPPENT à des tokens présents
    # dans notre taxonomie (CAKE, AERO, ETHFI, DRIFT, GMX, PUMP, BONK, etc.).
    MANUAL_PARENT_TO_GECKO = {
        # Liquid staking / restaking
        "parent#jito":          "jito-governance-token",
        "parent#ether-fi":      "ether-fi",
        "parent#stader":        "stader-labs",
        # Oracles
        "parent#pyth":          "pyth-network",
        "parent#chainlink":     "chainlink",
        # DEXs majeurs (revenue $100M+/an perdue sans ces mappings)
        "parent#pancakeswap":   "pancakeswap-token",
        "parent#aerodrome":     "aerodrome-finance",
        "parent#raydium":       "raydium",
        "parent#meteora":       "meteora",
        "parent#fluid":         "instadapp",
        # Perp DEX
        "parent#drift":         "drift-protocol",
        "parent#gmx":           "gmx",
        "parent#lighter":       "lighter",
        # Memecoin launchpads / trading
        "parent#pump":          "pump-fun",
        "parent#bonkfun":       "bonk",
        # Stablecoin issuers / CDP
        "parent#sky":           "sky",
        "parent#maker":         "sky",
        "parent#ena":           "ethena",
        "parent#ethena":        "ethena",
        # RWA
        "parent#ondo-finance":  "ondo-finance",
        # Perp DEX onchain
        "parent#hyperliquid":   "hyperliquid",
    }
    for parent, gid in MANUAL_PARENT_TO_GECKO.items():
        if parent not in parent_to_gecko:
            parent_to_gecko[parent] = gid

    # ── Pass unique : résolution gecko_id par ligne avec priorité claire ──
    # Pour CHAQUE row on détermine UN seul gecko_id final (ou skip), puis on
    # somme. Cela évite tout double-counting (cf. bug AAVE compté 2× via Pass 2
    # + Pass 3 dans la version précédente).
    # Priorité de résolution : (1) gecko_id direct, (2) parent#X → gecko_id,
    # (3) slug map manuel, (4) name lower-case match avec un autre row de même
    # parent. La priorité (4) couvre les cas où le parent a gecko_id mais une
    # version isolée n'en a pas et n'est pas dans le slug map.
    aggregated = {}  # gecko_id -> {'tvl': total, 'slug': primary, '_max_slug_tvl': float}
    for p in protocols_rows:
        # Skip protocoles non-DeFi qui pollueraient les agrégats sectoriels.
        cat = p.get("category") or ""
        if cat in EXCLUDED_CATEGORIES:
            continue

        gid = p.get("gecko_id")
        if not gid:
            parent = p.get("parentProtocol")
            if parent and parent in parent_to_gecko:
                gid = parent_to_gecko[parent]
        if not gid:
            slug = (p.get("slug") or "").lower()
            gid = SLUG_TO_GECKO.get(slug)
        if not gid:
            continue

        tvl = p.get("tvl") or 0
        if tvl <= 0:
            continue

        if gid not in aggregated:
            aggregated[gid] = {"tvl": 0.0, "slug": p.get("slug"), "_max_slug_tvl": 0}
        aggregated[gid]["tvl"] += tvl
        if tvl > aggregated[gid]["_max_slug_tvl"]:
            aggregated[gid]["_max_slug_tvl"] = tvl
            aggregated[gid]["slug"] = p.get("slug")

    # Convert to legacy format (gecko_id -> (tvl, slug))
    out = {}
    for gid, info in aggregated.items():
        out[gid] = (info["tvl"], info["slug"])
    return out


def build_dlid_to_gecko(protocols_rows):
    """defillama_id (str) -> gecko_id, en appliquant la MÊME logique de
    résolution que build_tvl_map : direct → parent → slug map manuel.

    Pourquoi : le précédent code ne mappait que les ~30% de protocoles
    DefiLlama qui ont un `gecko_id` direct. AAVE V3 (~$125M revenue/an)
    a gecko_id=None — donc il était IGNORÉ — alors qu'AAVE V2 ($1.24M/an)
    était capturé. Résultat : on rapportait AAVE rev=$1.24M au lieu de
    ~$125M. Même bug pour HYPE ($941M perdu), SKY ($250M), CAKE, UNI…
    """
    # Categories à exclure du calcul revenue (cf. build_tvl_map)
    EXCLUDED_CATEGORIES = {"CEX", "Chain"}

    # Manual parent → gecko (même liste exhaustive que build_tvl_map).
    MANUAL_PARENT_TO_GECKO = {
        "parent#jito":          "jito-governance-token",
        "parent#ether-fi":      "ether-fi",
        "parent#stader":        "stader-labs",
        "parent#pyth":          "pyth-network",
        "parent#chainlink":     "chainlink",
        "parent#pancakeswap":   "pancakeswap-token",
        "parent#aerodrome":     "aerodrome-finance",
        "parent#raydium":       "raydium",
        "parent#meteora":       "meteora",
        "parent#fluid":         "instadapp",
        "parent#drift":         "drift-protocol",
        "parent#gmx":           "gmx",
        "parent#lighter":       "lighter",
        "parent#pump":          "pump-fun",
        "parent#bonkfun":       "bonk",
        "parent#sky":           "sky",
        "parent#maker":         "sky",
        "parent#ena":           "ethena",
        "parent#ethena":        "ethena",
        "parent#ondo-finance":  "ondo-finance",
        "parent#hyperliquid":   "hyperliquid",
    }
    SLUG_TO_GECKO = {
        "uniswap":"uniswap","uniswap-labs":"uniswap","uniswap-v2":"uniswap","uniswap-v3":"uniswap","uniswap-v4":"uniswap",
        "aave":"aave","aave-v1":"aave","aave-v2":"aave","aave-v3":"aave",
        "compound":"compound-governance-token","compound-v3":"compound-governance-token","compound-finance":"compound-governance-token",
        "curve":"curve-dao-token","curve-finance":"curve-dao-token","curve-dex":"curve-dao-token",
        "pancakeswap-amm":"pancakeswap-token","pancakeswap":"pancakeswap-token","pancakeswap-amm-v3":"pancakeswap-token",
        "morpho-blue":"morpho","morpho-aave-v3":"morpho",
        "aerodrome-slipstream":"aerodrome-finance","aerodrome-v1":"aerodrome-finance",
        "raydium":"raydium","raydium-amm":"raydium","raydium-clmm":"raydium",
        "jupiter":"jupiter-exchange-solana","jupiter-aggregator":"jupiter-exchange-solana",
        "balancer":"balancer","balancer-v2":"balancer","balancer-v3":"balancer",
        "1inch-aggregation-protocol":"1inch","1inch-network":"1inch",
        "ethena":"ethena","ethena-usde":"ethena",
        "frax-finance":"frax-share","frax-ether":"frax-share","frax":"frax-share","frax-finance-staked-frax":"frax-share",
        "rocket-pool":"rocket-pool","lido":"lido-dao","convex-finance":"convex-finance",
        "jito":"jito-governance-token","jito-restaking":"jito-governance-token","jito-liquid-staking":"jito-governance-token",
        "pyth-network":"pyth-network","chainlink-ccip":"chainlink","chainlink-data-feeds":"chainlink","chainlink":"chainlink",
        "liquity":"liquity","liquity-v2":"liquity","ondo-finance":"ondo-finance","ondo-flux-finance":"ondo-finance",
        "sky-lending":"sky","sky":"sky","sky-money":"sky","sky-rwa":"sky","makerdao":"sky",
    }

    # Build parent→gecko from versions with gecko_id, then merge manual
    parent_to_gecko = {}
    for p in protocols_rows:
        par = p.get("parentProtocol")
        gid = p.get("gecko_id")
        if par and gid and par not in parent_to_gecko:
            parent_to_gecko[par] = gid
    for par, gid in MANUAL_PARENT_TO_GECKO.items():
        if par not in parent_to_gecko:
            parent_to_gecko[par] = gid

    out = {}  # dlid (str) -> gecko_id
    for p in protocols_rows:
        cat = p.get("category") or ""
        if cat in EXCLUDED_CATEGORIES:
            continue
        did = p.get("id")
        if did is None:
            continue
        gid = p.get("gecko_id")
        if not gid:
            par = p.get("parentProtocol")
            if par and par in parent_to_gecko:
                gid = parent_to_gecko[par]
        if not gid:
            slug = (p.get("slug") or "").lower()
            gid = SLUG_TO_GECKO.get(slug)
        if gid:
            out[str(did)] = gid
    return out


def fetch_defillama_revenue(dlid_to_gecko):
    """Returns (rev_map, source_map) tuples per gecko_id.

    - rev_map     : gecko_id -> revenue_1y_usd
    - source_map  : gecko_id -> {"kind": "protocol"|"chain", "slug": <api_slug>, "web": <ui_slug>}
      Permet au frontend de construire un lien d'audit qui pointe EXACTEMENT
      sur la page DeFiLlama où la valeur est affichée (chain page si revenu
      L1 type XRPL/Stellar/Ethereum, /protocol/{slug} si protocole DeFi).

    Aggregation = SOMME des revenue de toutes les versions partageant le
    même gecko_id (au lieu de max). Sans ça, AAVE V2 ($1.24M) écrasait
    AAVE V3 ($125M) → revenue rapporté = $1.24M au lieu de $126.5M.
    """
    # Mapping chain slug API → (gecko_id, DeFiLlama web slug pour /chain/{X}).
    # Le web slug est tel qu'utilisé dans l'URL UI : https://defillama.com/chain/Ripple
    # (où la section "Fees & Revenue" expose le total1y montré dans notre tableau).
    #
    # IMPORTANT méthodo P/S (fix 2026-06-04) : on n'inclut PAS les L2s sans
    # token propre (Base/Linea/Scroll/Blast) dans le revenu ETH. Les fees
    # sequencer L2 reviennent au sequencer, pas à ETH le token ; les compter
    # comme du revenu ETH = double-count + écart de 19% avec ce que DeFiLlama
    # UI / Token Terminal affichent pour Ethereum L1 seul. Convention standard.
    # (TVL inheritance est gardée séparément dans fetch_defillama_chains_tvl
    # car l'argument économique y est plus direct : capital bridgé sur ETH.)
    CHAIN_INFO = {
        "ethereum":       ("ethereum",              "Ethereum"),
        "tron":           ("tron",                  "Tron"),
        "solana":         ("solana",                "Solana"),
        "bsc":            ("binancecoin",           "BSC"),
        "polygon":        ("polygon-ecosystem-token","Polygon"),
        "avalanche":      ("avalanche-2",           "Avalanche"),
        "arbitrum":       ("arbitrum",              "Arbitrum"),
        "op-mainnet":     ("optimism",              "Optimism"),
        "sui":            ("sui",                   "Sui"),
        "ton":            ("the-open-network",      "TON"),
        "near":           ("near",                  "Near"),
        "icp":            ("internet-computer",     "ICP"),
        "injective":      ("injective-protocol",    "Injective"),
        "filecoin":       ("filecoin",              "Filecoin"),
        "starknet":       ("starknet",              "Starknet"),
        "stellar":        ("stellar",               "Stellar"),
        "xrpl":           ("ripple",                "Ripple"),
        "canton":         ("canton-network",        "Canton"),
        "hyperliquid-l1": ("hyperliquid",           "Hyperliquid L1"),
        "pulsechain":     ("pulsechain",            "PulseChain"),
        "hedera":         ("hedera-hashgraph",      "Hedera"),
        "algorand":       ("algorand",              "Algorand"),
        "cosmos":         ("cosmos",                "Cosmos"),
        "polkadot":       ("polkadot",              "Polkadot"),
        "cardano":        ("cardano",               "Cardano"),
    }
    CHAIN_SLUG_TO_GECKO = {slug: info[0] for slug, info in CHAIN_INFO.items()}

    try:
        data = _http_get_json(
            "https://api.llama.fi/overview/fees"
            "?dataType=dailyFees"
            "&excludeTotalDataChart=true"
            "&excludeTotalDataChartBreakdown=true",
            timeout=60)
    except Exception as e:
        print(f"[warn] DefiLlama /overview/fees: {e}", file=sys.stderr)
        return {}
    out = {}
    sources = {}  # gecko_id -> {"kind": "protocol"|"chain", "slug": str, "web": str}
    chain_count = 0
    protos = (data or {}).get("protocols") or []
    for p in protos:
        did = str(p.get("defillamaId") or "")
        # Chain-level entry : le bulk /overview/fees renvoie un total1y chain
        # PARTIEL/FAUX (ETH 129M, SOL 38M, BNB 22M) très inférieur à la réalité.
        # On les IGNORE ici et on récupère le vrai revenu chain depuis l'endpoint
        # dédié /overview/fees/{slug} plus bas (ETH 1156M, SOL 1706M, BNB 345M).
        if did.startswith("chain#") or p.get("category") == "Chain":
            continue
        # Cas standard : protocole DeFi
        if not did:
            continue
        gid = dlid_to_gecko.get(did)
        if not gid:
            continue
        rev = p.get("total1y")
        if rev is None or rev <= 0:
            r30 = p.get("total30d") or 0
            if r30 > 0:
                rev = r30 * 12
        if rev is None or rev <= 0:
            continue
        # SOMME (pas max) — chaque version d'un protocole contribue.
        out[gid] = out.get(gid, 0) + rev
        if gid not in sources:
            sources[gid] = {"kind": "protocol",
                            "slug": p.get("slug") or "",
                            "web":  p.get("slug") or ""}

    # ── Revenu CHAIN NATIVE UNIQUEMENT (fix méthodo 2026-06-04) ────────
    # Pour un L1 (ETH/SOL/BNB/TRX/XRP/XLM/BTC/LTC…), seul le revenu de la
    # chain elle-même est attribuable au token. Les fees des protocoles
    # déployés (Aave, Uniswap, Jupiter…) reviennent aux holders de leurs
    # propres tokens, pas à ETH/SOL. Avant on sommait chain.total1y (=
    # écosystème entier) ce qui surestimait massivement (ETH 5.82B vs
    # 318M réels chain native).
    #
    # Méthode : pour chaque slug, on cherche dans protocols[] l'entrée
    # defillamaId="chain#{slug}" (le chain native) et on prend SON total1y.
    chain_rev = {}        # gecko_id -> revenue $ (chain native uniquement)
    chain_primary = {}    # gecko_id -> (api_slug, web_name) du chain qui contribue
    chain_breakdowns = {} # gecko_id -> [{name, slug, kind:'chain', rev_usd, url}]
    for slug, (gid, web_name) in CHAIN_INFO.items():
        try:
            cd = _http_get_json(
                f"https://api.llama.fi/overview/fees/{slug}"
                "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
                "&dataType=dailyFees", timeout=40)
        except Exception:
            cd = None
        if not cd:
            continue
        # Trouve l'entrée chain native correspondant à ce slug
        native_rev = 0
        native_entry = None
        for p in (cd.get("protocols") or []):
            p_id = str(p.get("defillamaId") or "")
            if p_id == f"chain#{slug}" or (p.get("name") or "").lower() == slug.lower():
                native_rev = p.get("total1y") or 0
                if native_rev <= 0:
                    r30 = p.get("total30d") or 0
                    if r30 > 0:
                        native_rev = r30 * 365.0 / 30.0
                native_entry = p
                break
        # Si pas d'entrée chain explicite (cas de Stellar/XRPL où le natif EST
        # le seul "protocole"), fallback sur chain.total1y du résumé endpoint.
        if native_rev <= 0:
            native_rev = cd.get("total1y") or 0
            if native_rev <= 0:
                r30 = cd.get("total30d") or 0
                if r30 > 0:
                    native_rev = r30 * 365.0 / 30.0
        if native_rev > 0:
            chain_rev[gid] = chain_rev.get(gid, 0) + native_rev
            if gid not in chain_primary:
                chain_primary[gid] = (slug, web_name)
            chain_count += 1
            # Breakdown réduit à une seule ligne : le chain native lui-même.
            # C'est volontaire — on ne veut PAS lister les protocoles déployés
            # car leurs fees ne sont pas attribuables au token chain.
            bd = chain_breakdowns.setdefault(gid, [])
            bd.append({
                "name":    (native_entry.get("name") if native_entry else web_name) or web_name,
                "slug":    slug,
                "kind":    "chain",
                "rev_usd": int(round(native_rev)),
                "url":     f"https://defillama.com/chain/{web_name}",
            })
        time.sleep(0.3)
    for gid, rev in chain_rev.items():
        out[gid] = rev  # chain native uniquement, pas ecosystem
        slug, web = chain_primary[gid]
        sources[gid] = {"kind": "chain", "slug": slug, "web": web}
    if chain_count:
        print(f"[info] revenue : {chain_count} chains (NATIVE only, pas ecosystem) — ETH/SOL/BNB/TRX/XRP réels", file=sys.stderr)
    return out, sources, chain_breakdowns


# ─── Aggregation ────────────────────────────────────────────────────────
def _safe_div(a, b):
    if not b:
        return None
    try:
        return a / b
    except Exception:
        return None


def build_token_fund(cg_row, tvl_map, rev_map, rev_sources=None, tvl_chain_web=None, rev_breakdowns=None):
    """Build per-token fundamentals from CG + DL data.

    rev_sources     : {gecko_id -> {"kind","slug","web"}} produit par
                      fetch_defillama_revenue() pour router audit revenue/PS.
    tvl_chain_web   : {gecko_id -> DeFiLlama chain UI name} produit par
                      fetch_defillama_chains_tvl(), pour router audit TVL des
                      chains L1 natives (XRPL/Stellar/Ethereum/Solana…) vers
                      /chain/{name} où la TVL est en headline.
    rev_breakdowns  : {gecko_id -> [{name,slug,kind,rev_usd,url}…]} la
                      décomposition du revenue chain en composantes (chain
                      native + chaque protocole), permet au modal d'afficher
                      la ventilation auditable au lieu d'un chiffre opaque.
    """
    rev_sources = rev_sources or {}
    tvl_chain_web = tvl_chain_web or {}
    rev_breakdowns = rev_breakdowns or {}
    cid = cg_row.get("id")
    mcap = cg_row.get("market_cap") or 0
    fdv = cg_row.get("fully_diluted_valuation") or mcap
    vol = cg_row.get("total_volume") or 0
    tvl_tuple = tvl_map.get(cid)
    tvl = tvl_tuple[0] if tvl_tuple else None
    # Slug DefiLlama du protocole primaire (pour deep-link audit TVL/revenue/P-S
    # vers defillama.com/protocol/<slug>). "chain" = placeholder L1 sans page
    # protocole → on laisse None (le front-end retombe sur CoinGecko).
    dl_slug = tvl_tuple[1] if tvl_tuple else None
    if dl_slug in (None, "", "chain"):
        dl_slug = None
    rev = rev_map.get(cid)
    rev_src = rev_sources.get(cid) if rev else None
    # Plus de seuil revenue<$100k (consigne 2026-06-03 : laisser passer toutes
    # les valeurs même petites). Le frontend les affichera telles quelles.
    circ_pct = None
    if mcap and fdv and fdv > 0:
        circ_pct = (mcap / fdv) * 100
    vol_mcap_pct = None
    if mcap and mcap > 0:
        vol_mcap_pct = (vol / mcap) * 100
    mc_tvl = None
    if tvl and tvl > 0 and mcap and mcap > 0:
        mc_tvl = mcap / tvl
        # Plus de cap 20x : on garde le ratio brut, l'utilisateur juge.
    ps_ttm = None
    if rev and rev > 0 and mcap and mcap > 0:
        ps_ttm = mcap / rev
    return {
        "id": cid,
        "symbol": (cg_row.get("symbol") or "").upper(),
        "name": cg_row.get("name") or cid,
        "image": cg_row.get("image"),
        "dl_slug": dl_slug,
        # Source d'audit du revenue : protocol|chain + slugs DeFiLlama
        #  - rev_source_slug = slug API (utilisé pour construire l'URL API JSON
        #    qui affiche `total1y` exactement = la valeur cachée)
        #  - rev_source_web  = slug UI (utilisé pour les fallbacks UI)
        "rev_source_kind": rev_src["kind"] if rev_src else None,
        "rev_source_slug": rev_src["slug"] if rev_src else None,
        "rev_source_web":  rev_src["web"]  if rev_src else None,
        # Décomposition du revenue par composante (chain native + protocoles
        # déployés). Le modal l'affiche en tableau auditable où chaque ligne
        # = un lien vers la page DeFiLlama UI où la valeur est lisible.
        "rev_breakdown":   rev_breakdowns.get(cid) if rev else None,
        # Si la TVL vient de la chain L1 native (pas d'un protocole DeFi mappé),
        # on expose le nom UI DeFiLlama pour router /chain/{name} sur la cellule TVL.
        "tvl_chain_web":   tvl_chain_web.get(cid) if (tvl and not dl_slug) else None,
        "price": cg_row.get("current_price"),
        "mcap_b": round((mcap or 0) / 1e9, 3),
        "fdv_b":  round((fdv or 0) / 1e9, 3),
        "vol_b":  round((vol or 0) / 1e9, 3),
        "circ_pct": round(circ_pct, 1) if circ_pct is not None else None,
        "vol_mcap_pct": round(vol_mcap_pct, 2) if vol_mcap_pct is not None else None,
        "tvl_b": round((tvl or 0) / 1e9, 3) if tvl else None,
        "mc_tvl": round(mc_tvl, 2) if mc_tvl is not None else None,
        "rev_m_1y": round(rev / 1e6, 2) if rev else None,
        "ps_ttm": round(ps_ttm, 2) if ps_ttm is not None else None,
        "perf_7d":  cg_row.get("price_change_percentage_7d_in_currency"),
        "perf_30d": cg_row.get("price_change_percentage_30d_in_currency"),
        "perf_1y":  cg_row.get("price_change_percentage_1y_in_currency"),
    }


_WEIGHTED_METRICS = [
    "circ_pct", "vol_mcap_pct", "mc_tvl", "ps_ttm",
    "perf_7d", "perf_30d", "perf_1y",
]

# Winsorisation : on clippe aux p5/p95 (unweighted, sur les tokens du narrative)
# pour éviter qu'un single small cap dégueule la moyenne pondérée
# (ex: ZK/Privacy perf_1y=+802% tiré par un token + des autres -50%).
_WINSOR_PERCENTILES = (5, 95)
_WINSOR_PERF_METRICS = {"perf_7d", "perf_30d", "perf_1y"}


def _percentile(values_sorted, p):
    if not values_sorted:
        return None
    if len(values_sorted) == 1:
        return values_sorted[0]
    k = (len(values_sorted) - 1) * (p / 100.0)
    lo = int(k); hi = min(lo + 1, len(values_sorted) - 1)
    return values_sorted[lo] * (1 - (k - lo)) + values_sorted[hi] * (k - lo)


def aggregate_narrative(tokens):
    """Aggregate token-level fundamentals into sector-level metrics.

    Strategie : on sépare le panier en CRYPTO et STOCKS. Les métriques universelles
    (mcap, vol, perf) sont agrégées sur le panier complet. Les métriques crypto-
    spécifiques (circ/fdv, mc/tvl, p/s ttm) sont calculées UNIQUEMENT sur le sous-
    ensemble crypto — sinon les actions polluent les ratios (ex: stocks ont fdv=mcap
    par construction → tirent circ/fdv à 100% artificiellement).

    Améliorations 2026-05-21 (audit) :
      - Winsorisation 5/95 sur perf_7d/30d/1y (single-token blow-ups clippés)
      - Médiane + min + max + n exposés en `{metric}_median/_min/_max/_n`
      - Outliers loggés dans `outliers_clipped` (token, métrique, raw → clipped)
    """
    if not tokens:
        return {}
    # Drop "defunct" tokens : mcap=0 AND fdv≈0 (CoinGecko returns them as
    # zombie entries — e.g. MKR, MC, LOOKS, RGT, AZUR after deprecation).
    # They show up in modals as "—" everywhere and confuse the user.
    tokens = [t for t in tokens
              if (t.get("mcap_b") or 0) > 0
              or (t.get("fdv_b") or 0) > 0.01
              or t.get("is_stock")]
    crypto = [t for t in tokens if not t.get("is_stock")]
    stocks = [t for t in tokens if t.get("is_stock")]

    # ── Universal aggregates (full basket: crypto + stocks) ──
    mcap_total = sum((t.get("mcap_b") or 0) for t in tokens)
    vol_total  = sum((t.get("vol_b")  or 0) for t in tokens)

    # ── Crypto-only aggregates (for crypto-specific ratios) ──
    cmcap = sum((t.get("mcap_b") or 0) for t in crypto)
    cfdv  = sum((t.get("fdv_b")  or 0) for t in crypto)
    ctvl  = sum((t.get("tvl_b")  or 0) for t in crypto if t.get("tvl_b"))
    crev  = sum((t.get("rev_m_1y") or 0) for t in crypto if t.get("rev_m_1y"))

    # FDV total affiché = crypto fdv + stocks mcap (les actions sont 100% en
    # circulation par construction → fdv = mcap pour elles). Permet d'avoir un
    # mcap_total ≤ fdv_total cohérent.
    fdv_total = cfdv + sum((t.get("mcap_b") or 0) for t in stocks)

    out = {
        "mcap_total_b": round(mcap_total, 2),
        "fdv_total_b":  round(fdv_total, 2),
        "vol_total_b":  round(vol_total, 3),
        "tvl_total_b":  round(ctvl, 3) if ctvl else None,
        "rev_m_1y_total": round(crev, 2) if crev else None,
    }

    # circ_fdv_pct : CRYPTO-ONLY car les actions sont 100% par définition (toutes
    # les shares outstanding sont en circulation). Inclure les stocks tirerait la
    # ratio à 100% artificiellement et masquerait les unlocks à venir crypto.
    if crypto and cfdv > 0 and cmcap > 0:
        out["circ_fdv_pct"] = round(min(cmcap / cfdv * 100, 100.0), 1)
    else:
        out["circ_fdv_pct"] = None  # narratifs stocks-only (Bitcoin Miners, Web3 Exchanges)

    # vol/mcap : OK sur le panier complet (les deux scalent avec stocks)
    out["vol_mcap_pct"]  = round((vol_total / mcap_total * 100), 2) if mcap_total > 0 else None

    # MC/TVL : ratio brut Σmcap_with_tvl / ΣTVL, sans aucun seuil (consigne
    # 2026-06-03 : afficher toute la data). On expose la couverture mcap dans
    # le tooltip pour que l'utilisateur juge la représentativité — pas de
    # masquage, pas de cap, pas de minimum de tokens.
    cmcap_with_tvl = sum((t.get("mcap_b") or 0) for t in crypto if (t.get("tvl_b") or 0) > 0)
    coverage_tvl = (cmcap_with_tvl / cmcap) if cmcap > 0 else 0
    if ctvl and ctvl > 0 and cmcap_with_tvl > 0:
        out["mc_tvl"] = round(cmcap_with_tvl / ctvl, 2)
        out["mc_tvl_coverage_mcap_pct"] = round(coverage_tvl * 100, 1)
    else:
        out["mc_tvl"] = None
        out["mc_tvl_coverage_mcap_pct"] = round(coverage_tvl * 100, 1) if cmcap else 0
    # P/S TTM : Σmcap_with_rev / Σrev sur le PANIER COMPLET (crypto + stocks).
    # - Pour les tokens crypto : revenue = DefiLlama (rev_m_1y, $M)
    # - Pour les stocks       : revenue = Yahoo totalRevenue (_stock_revenue_m, $M)
    # Sans inclure les stocks, Web3 Exchanges & Fintech (100 % HOOD/COIN/XYZ/GLXY)
    # affichait P/S = — alors que les 4 stocks ont chacun leur revenue TTM
    # publique. Idem Bitcoin Miners (100 % miners stocks).
    mcap_with_rev_full = 0.0
    revenue_total_m = 0.0
    for t in tokens:
        if t.get("is_stock"):
            rev_m_t = t.get("_stock_revenue_m")
            # Garde anti gross-pass-through : exclure du P/S une action dont le
            # revenu Yahoo > 3× mcap (Galaxy Digital), sinon il dilue l'agrégat.
            m_chk = (t.get("mcap_b") or 0) * 1000
            if rev_m_t and m_chk > 0 and rev_m_t > 3 * m_chk:
                rev_m_t = None
        else:
            rev_m_t = t.get("rev_m_1y")
        m = t.get("mcap_b") or 0
        if rev_m_t and rev_m_t > 0 and m > 0:
            mcap_with_rev_full += m
            revenue_total_m += rev_m_t
    coverage_rev_full = (mcap_with_rev_full / mcap_total) if mcap_total > 0 else 0
    n_with_rev = sum(1 for t in tokens
                     if (((t.get("_stock_revenue_m") if t.get("is_stock") else t.get("rev_m_1y")) or 0) > 0
                         and (t.get("mcap_b") or 0) > 0))
    n_total_basket = len(tokens)
    out["ps_ttm_n_tokens"] = n_with_rev
    out["ps_ttm_n_total"] = n_total_basket
    # P/S TTM : ratio brut, sans seuil (consigne 2026-06-03 : aucune data
    # bloquée). Couverture mcap exposée dans le tooltip pour la nuance.
    if revenue_total_m > 0 and mcap_with_rev_full > 0:
        out["ps_ttm"] = round(mcap_with_rev_full * 1000 / revenue_total_m, 1)
        out["ps_ttm_coverage_mcap_pct"] = round(coverage_rev_full * 100, 1)
    else:
        out["ps_ttm"] = None
        out["ps_ttm_coverage_mcap_pct"] = round(coverage_rev_full * 100, 1) if mcap_total else 0

    # Mcap-weighted perf — AUCUN seuil de couverture (consigne 2026-06-03 :
    # afficher toute la data). On calcule sur les tokens disponibles, même si
    # ça ne couvre que 6 % du mcap : la couverture est exposée via *_n et le
    # tooltip dominant_pct alerte si un token domine.
    # WINSORISATION 5/95 conservée : c'est une transformation (clip outlier),
    # pas un seuil qui supprime de la data — le headline reste affiché.
    outliers_clipped = []  # {metric, symbol, raw_value, clipped_to}
    for metric in ["perf_7d", "perf_30d", "perf_1y"]:
        # Collect raw (value, weight, symbol) triples
        triples = []
        for t in tokens:
            v = t.get(metric)
            w = t.get("mcap_b") or 0
            if v is None or w <= 0:
                continue
            triples.append((v, w, t.get("symbol")))

        if not triples:
            out[metric] = None
            out[f"{metric}_median"] = None
            out[f"{metric}_min"] = None
            out[f"{metric}_max"] = None
            out[f"{metric}_n"] = 0
            continue

        # Expose audit stats (unweighted)
        vals_sorted = sorted(v for v, _, _ in triples)
        out[f"{metric}_median"] = round(_percentile(vals_sorted, 50), 2)
        out[f"{metric}_min"] = round(vals_sorted[0], 2)
        out[f"{metric}_max"] = round(vals_sorted[-1], 2)
        out[f"{metric}_n"] = len(triples)

        # Winsorize : clip extreme single-token outliers to p5/p95
        if metric in _WINSOR_PERF_METRICS and len(vals_sorted) >= 5:
            lo = _percentile(vals_sorted, _WINSOR_PERCENTILES[0])
            hi = _percentile(vals_sorted, _WINSOR_PERCENTILES[1])
            # Expose thresholds so perAuditNarrFund modal can clip per-token
            # values before computing contrib (Σ contribs = headline reproduit).
            # Sans ça même bug que côté TradFi : modal montrait raw_contrib qui
            # ne s'additionnaient pas au headline winsorisé.
            out[f"{metric}_winsor_lo"] = round(lo, 2)
            out[f"{metric}_winsor_hi"] = round(hi, 2)
            clipped = []
            for (v, w, sym) in triples:
                cv = min(max(v, lo), hi)
                if abs(cv - v) > 1e-6:
                    outliers_clipped.append({
                        "metric": metric, "symbol": sym,
                        "raw_value": round(v, 2), "clipped_to": round(cv, 2),
                    })
                clipped.append((cv, w))
        else:
            clipped = [(v, w) for v, w, _ in triples]

        num = sum(v * w for v, w in clipped)
        den = sum(w for _, w in clipped)
        out[metric] = round(num / den, 2) if den > 0 else None

    # Dominance : mcap of top token / total
    sorted_tokens = sorted(tokens, key=lambda t: -(t.get("mcap_b") or 0))
    top = sorted_tokens[0] if sorted_tokens else None
    out["dominant_sym"] = top.get("symbol") if top else None
    out["dominant_name"] = top.get("name") if top else None
    out["dominant_pct"] = round((top.get("mcap_b") / mcap_total * 100), 1) if top and mcap_total > 0 else None

    # Completeness counts (per-metric coverage — for frontend gating)
    out["n_tokens"] = len(tokens)
    out["n_with_tvl"] = sum(1 for t in tokens if t.get("tvl_b"))
    out["n_with_rev"] = sum(1 for t in tokens if t.get("rev_m_1y"))
    out["coverage"] = {
        "circ_fdv_pct": sum(1 for t in tokens if t.get("circ_pct") is not None and not t.get("is_stock")),
        "vol_mcap_pct": sum(1 for t in tokens if t.get("vol_mcap_pct") is not None),
        "mc_tvl":       out["n_with_tvl"],
        "ps_ttm":       out["n_with_rev"],
        "perf_7d":      sum(1 for t in tokens if t.get("perf_7d")  is not None),
        "perf_30d":     sum(1 for t in tokens if t.get("perf_30d") is not None),
        "perf_1y":      sum(1 for t in tokens if t.get("perf_1y")  is not None),
    }

    # Outliers clipped by winsorisation — for frontend audit modal
    out["outliers_clipped"] = outliers_clipped

    return out


# ─── Main ───────────────────────────────────────────────────────────────
def main():
    # Single-instance lock + load prev cache + load tracker data
    _lock = acquire_singleton_lock()
    if _lock is None:
        sys.exit(0)
    prev_cache = load_previous_cache()
    tracker = load_tracker_data()
    tracker_tokens = tracker["tokens"]
    tracker_stocks = tracker.get("stocks_by_narrative", {})
    tracker_momentum = tracker["momentum"]

    t0 = time.time()
    all_ids = set()
    for narr, meta in NARRATIVES.items():
        for tid in meta.get("tokens", []):
            all_ids.add(tid)
    all_ids = sorted(all_ids)
    print(f"[info] {len(all_ids)} unique CG ids across {len(NARRATIVES)} narratives")

    # 1. CoinGecko batch (still needed for FDV + perf_1y + image; price/mcap/perf_30d
    #    will be OVERRIDDEN by tracker values for consistency)
    print("[info] fetching CoinGecko /coins/markets ...")
    cg_data = fetch_coingecko_markets(all_ids)
    print(f"[info] CG: {len(cg_data)}/{len(all_ids)} fetched ({time.time()-t0:.0f}s)")

    # 2. DeFiLlama TVL + id→gecko map (single /protocols call)
    print("[info] fetching DefiLlama /protocols ...")
    protos = fetch_defillama_protocols()
    tvl_map = build_tvl_map(protos)
    dlid_to_gecko = build_dlid_to_gecko(protos)
    print(f"[info] DL TVL: {len(tvl_map)} gecko-mapped, {len(dlid_to_gecko)} defillama id mappings ({time.time()-t0:.0f}s)")

    # 2b. DeFiLlama chains TVL — pour les L1 natifs (ETH, SOL, BNB, BTC, TRON,
    # HYPE, ARB, POL, AVAX, etc.) dont la chain TVL est tracée séparément.
    # On SOMME chain_tvl + protocol_tvl par gecko_id ; ainsi ETH affiche les
    # $43B chain-locked + ses protocoles éventuels (mais aucun protocole n'a
    # gecko_id=ethereum, donc en pratique = chain TVL pure).
    print("[info] fetching DefiLlama /v2/chains for L1 chain TVL ...")
    chain_tvl_map, tvl_chain_web_map = fetch_defillama_chains_tvl()
    for gid, ct in chain_tvl_map.items():
        if gid in tvl_map:
            existing_tvl, existing_slug = tvl_map[gid]
            tvl_map[gid] = (existing_tvl + ct, existing_slug)
        else:
            tvl_map[gid] = (ct, "chain")

    # 3. DeFiLlama revenue (joined on defillamaId → gecko_id) + source attribution + breakdown
    print("[info] fetching DefiLlama /overview/fees ...")
    rev_map, rev_sources, rev_breakdowns = fetch_defillama_revenue(dlid_to_gecko)
    print(f"[info] DL rev: {len(rev_map)} gecko-mapped protocols ({time.time()-t0:.0f}s)")

    # 4. Per-token fundamentals (build_token_fund uses CG row, then OVERRIDE
    #    price/mcap/perf with tracker values when available — guarantees the
    #    funda tab and the Narrative Tracker tab show identical numbers)
    token_fund = {}
    overridden = 0
    for cid in all_ids:
        row = cg_data.get(cid)
        if not row:
            continue
        tf = build_token_fund(row, tvl_map, rev_map, rev_sources, tvl_chain_web_map, rev_breakdowns)
        tt = tracker_tokens.get(cid)
        if tt:
            # Override visible fields with tracker values (single source of truth)
            if tt.get("price") is not None:
                tf["price"] = tt["price"]
            if tt.get("mcap"):
                tf["mcap_b"] = round(tt["mcap"] / 1e9, 3)
            if tt.get("volume") is not None:
                tf["vol_b"] = round((tt["volume"] or 0) / 1e9, 3)
            if tt.get("perf_7d") is not None:
                tf["perf_7d"] = tt["perf_7d"]
            if tt.get("perf_30d") is not None:
                tf["perf_30d"] = tt["perf_30d"]
            tf["_price_source"] = "tracker"
            overridden += 1
        else:
            tf["_price_source"] = "coingecko_only"
        token_fund[cid] = tf
    print(f"[info] {len(token_fund)} tokens with fundamentals ({overridden} overridden by tracker_cache)")

    # 4b. Gap-fill from previous funda cache (last known good fundamentals
    #     when CG dropped a token: keeps TVL/revenue + most metrics, refresh
    #     price/mcap/perf from tracker if available)
    prev_token_fund = {}
    for n in (prev_cache.get("narratives", []) if prev_cache else []):
        for t in n.get("tokens", []):
            tid = t.get("id")
            if tid:
                prev_token_fund[tid] = t
    stale_filled = 0
    for tid in all_ids:
        if tid in token_fund:
            continue
        prev = prev_token_fund.get(tid)
        if not prev:
            continue
        merged = {**prev, "_stale_funda": True}
        tt = tracker_tokens.get(tid, {})
        if tt.get("price") is not None:
            merged["price"] = tt["price"]
        if tt.get("mcap"):
            merged["mcap_b"] = round(tt["mcap"] / 1e9, 3)
        if tt.get("perf_30d") is not None:
            merged["perf_30d"] = tt["perf_30d"]
        token_fund[tid] = merged
        stale_filled += 1
    if stale_filled:
        print(f"[info] gap-filled {stale_filled} tokens from previous funda cache (marked _stale_funda=true)")

    # 4c. Fetch perf_1y pour les STOCKS crypto-side (MSTR, COIN, HOOD, MARA, RIOT,
    #     IREN, BITF, etc.) — tradfi_cache.json ne calcule que 7d/30d sur 60 jours
    #     d'historique. Sans ça, les narratives 100 % stocks (Bitcoin Miners,
    #     Web3 Exchanges & Fintech) affichaient `—` pour perf_1y partout.
    #     Yahoo expose le 52WeekChange directement, on l'utilise.
    all_stock_symbols = set()
    for narr, sts in tracker_stocks.items():
        for st in sts:
            sym = st.get("symbol")
            if sym:
                all_stock_symbols.add(sym)
    # Pour chaque stock, on récupère aussi :
    #   - perf_1y (52WeekChange)
    #   - priceToSalesTrailing12Months → ps_ttm équité
    #   - totalRevenue → revenue $ TTM
    # Ainsi les narratives à stocks (Web3 Exchanges, Bitcoin Miners) ou mixtes
    # (Bitcoin Institutional, Stablecoins) peuvent avoir P/S sectoriel.
    stock_fundamentals = {}  # symbol -> {perf_1y, ps_ttm, revenue_usd}
    if all_stock_symbols:
        try:
            import yfinance as yf

            def _fetch_one_stock(sym):
                """Fetch (52WeekChange, ps_ttm, totalRevenue) for one stock.
                Retries .info with backoff because Yahoo quoteSummary rate-limits
                hard (HTTP 429) under concurrent load — single-shot misses 50%+ of
                tickers silently and leaves Bitcoin Miners / Web3 Exchanges with
                no Perf 1Y or revenue data."""
                info = {}
                for attempt in range(3):
                    try:
                        info = yf.Ticker(sym).info or {}
                        if len(info) >= 5:
                            break
                    except Exception:
                        if attempt == 2:
                            return sym, {}
                    if attempt < 2:
                        time.sleep(1.5 * (attempt + 1) + random.uniform(0, 0.8))
                entry = {}
                p = info.get("52WeekChange")
                if p is not None:
                    try: entry["perf_1y"] = float(p) * 100
                    except Exception: pass
                ps = info.get("priceToSalesTrailing12Months")
                if ps is not None:
                    try:
                        psf = float(ps)
                        if 0 < psf < 500: entry["ps_ttm"] = psf
                    except Exception: pass
                rev = info.get("totalRevenue")
                if rev is not None:
                    try:
                        rv = float(rev)
                        if rv > 0: entry["revenue_usd"] = rv
                    except Exception: pass
                return sym, entry

            print(f"[info] fetching perf_1y + ps_ttm + revenue for {len(all_stock_symbols)} stocks via Yahoo (yfinance + backoff)…")
            t0 = time.time()
            failed = []
            with ThreadPoolExecutor(max_workers=6) as ex:
                futures = {ex.submit(_fetch_one_stock, s): s for s in sorted(all_stock_symbols)}
                for fut in as_completed(futures):
                    sym = futures[fut]
                    try:
                        sym, entry = fut.result(timeout=45)
                    except Exception:
                        entry = {}
                    if entry:
                        stock_fundamentals[sym] = entry
                    if not entry or "revenue_usd" not in entry:
                        failed.append(sym)

            # Yahooquery bulk fallback for tickers yfinance missed — financial_data
            # gives totalRevenue/52WeekChange in batches of 40 with one HTTP call,
            # immune to quoteSummary throttling that breaks single-ticker .info.
            if failed:
                try:
                    from yahooquery import Ticker as _YQT
                    print(f"[info] yahooquery fallback for {len(failed)} stocks missing data…")
                    YQ_CHUNK = 40
                    for i in range(0, len(failed), YQ_CHUNK):
                        batch = failed[i:i + YQ_CHUNK]
                        try:
                            yq = _YQT(batch, asynchronous=False, validate=False)
                            fd = yq.financial_data if isinstance(yq.financial_data, dict) else {}
                            ks = yq.key_stats       if isinstance(yq.key_stats, dict) else {}
                            sd = yq.summary_detail  if isinstance(yq.summary_detail, dict) else {}
                        except Exception as e:
                            print(f"[warn] yq stock chunk {i // YQ_CHUNK + 1} failed: {e}", file=sys.stderr)
                            continue
                        for sym in batch:
                            existing = stock_fundamentals.get(sym, {})
                            f = fd.get(sym) if isinstance(fd.get(sym), dict) else {}
                            k = ks.get(sym) if isinstance(ks.get(sym), dict) else {}
                            s = sd.get(sym) if isinstance(sd.get(sym), dict) else {}
                            if "revenue_usd" not in existing:
                                rev = f.get("totalRevenue")
                                try:
                                    if rev is not None and float(rev) > 0:
                                        existing["revenue_usd"] = float(rev)
                                except Exception: pass
                            if "perf_1y" not in existing:
                                p = k.get("52WeekChange")
                                try:
                                    if p is not None:
                                        existing["perf_1y"] = float(p) * 100
                                except Exception: pass
                            if "ps_ttm" not in existing:
                                ps = s.get("priceToSalesTrailing12Months") or k.get("priceToSalesTrailing12Months")
                                try:
                                    if ps is not None:
                                        psf = float(ps)
                                        if 0 < psf < 500: existing["ps_ttm"] = psf
                                except Exception: pass
                            if existing:
                                stock_fundamentals[sym] = existing
                except ImportError:
                    print("[warn] yahooquery unavailable — skipping bulk fallback")

            n_ps = sum(1 for v in stock_fundamentals.values() if "ps_ttm" in v)
            n_p1 = sum(1 for v in stock_fundamentals.values() if "perf_1y" in v)
            n_rv = sum(1 for v in stock_fundamentals.values() if "revenue_usd" in v)
            print(f"[info] stock fundamentals fetched : {n_p1}/{len(all_stock_symbols)} perf_1y, "
                  f"{n_ps}/{len(all_stock_symbols)} ps_ttm, {n_rv}/{len(all_stock_symbols)} revenue "
                  f"in {time.time()-t0:.0f}s")
        except ImportError:
            print("[warn] yfinance unavailable — stock fundamentals left as None")

    # Inject into tracker_stocks lists (mutate in place)
    for narr, sts in tracker_stocks.items():
        for st in sts:
            sym = st.get("symbol")
            f = stock_fundamentals.get(sym, {})
            if "perf_1y" in f and st.get("perf_1y") is None:
                st["perf_1y"] = f["perf_1y"]
            if "ps_ttm" in f:
                st["_stock_ps_ttm"] = f["ps_ttm"]
            if "revenue_usd" in f:
                # Convertir en M$ pour homogénéité avec rev_m_1y crypto (qui est en M)
                st["_stock_revenue_m"] = f["revenue_usd"] / 1e6

    # 5. Per-narrative aggregation + momentum cross-injection from tracker.
    #    On INJECTE les stocks proxies (CRCL, PYPL, MSTR, COIN, MARA, RIOT, etc.)
    #    qui figurent déjà dans le panier de l'onglet Narrative_Tracker. Le but :
    #    UNE SEULE SOURCE DE VÉRITÉ pour les paniers narratifs entre les pages.
    #    Ces actions n'ont pas de TVL/revenue on-chain mais contribuent au mcap
    #    total et à la perf pondérée — exactement comme dans le tracker.
    narratives_out = []
    n_defunct_dropped = 0
    for narr, meta in NARRATIVES.items():
        tokens = [token_fund[tid] for tid in meta.get("tokens", []) if tid in token_fund]
        # Append stocks (proxies actions cotées) — déjà au format dict identique
        stocks = tracker_stocks.get(narr, [])
        basket = tokens + stocks
        # Filter defunct zombie tokens (mcap=0 AND fdv≈0). CoinGecko keeps
        # returning these for deprecated tokens (MKR/maker, MC/merit-circle,
        # LOOKS, RGT, AZUR) — they appear with "—" everywhere and confuse
        # the audit modal. Stocks are kept (their mcap is real).
        basket_clean = []
        for t in basket:
            if t.get("is_stock"):
                # Afficher revenu TTM (Yahoo totalRevenue, $M) + P/S dans le modal,
                # cohérent avec l'agrégat (qui utilise _stock_revenue_m). Sans ça les
                # narratifs 100% actions (Web3 Exchanges, Bitcoin Miners, PYPL/CRCL en
                # Stablecoins) montraient "—" partout malgré un P/S agrégé. Garde anti
                # gross-pass-through : revenu > 3× mcap (Galaxy Digital 58B$ rev / 5.7B$
                # mcap = ventes crypto brutes) → non comparable, pas de P/S.
                rev_m = t.get("_stock_revenue_m")
                mcap_m = (t.get("mcap_b") or 0) * 1000
                if rev_m and rev_m > 0 and not (mcap_m > 0 and rev_m > 3 * mcap_m):
                    t["rev_m_1y"] = round(rev_m, 2)
                    if mcap_m > 0:
                        t["ps_ttm"] = round(mcap_m / rev_m, 1)
                basket_clean.append(t); continue
            # Drop tokens with mcap=0 — they contribute nothing to mcap-weighted
            # aggregates and clutter the audit modal with "—" everywhere.
            # MKR/maker is the textbook case : CoinGecko returns the legacy id
            # with mcap=0 (Maker was rebranded to Sky) but kept a stale fdv=0.15.
            mcap = t.get("mcap_b") or 0
            if mcap > 0:
                basket_clean.append(t)
            else:
                n_defunct_dropped += 1
        agg = aggregate_narrative(basket_clean)
        mom = tracker_momentum.get(narr, {})
        narratives_out.append({
            "narrative": narr,
            "icon": meta.get("icon"),
            "color": meta.get("color"),
            **agg,
            # ── Momentum (from narratives_cache.json — same source as Tracker tab) ──
            "momentum": {k: v for k, v in mom.items() if v is not None} if mom else {},
            "tokens": basket_clean,
            "n_stocks": len(stocks),
        })
    if n_defunct_dropped:
        print(f"[info] dropped {n_defunct_dropped} defunct zombie tokens (mcap=0 AND fdv≈0)", file=sys.stderr)

    narratives_out.sort(key=lambda n: (n.get("mcap_total_b") or 0), reverse=True)

    payload = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M %Z") or
                   datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fetch_ts": int(time.time()),
        "sources": [
            "narratives_cache.json (price + mcap + volume + perf_7d/30d + momentum) — single source of truth, written by fetch_narratives.py",
            "CoinGecko /coins/markets (FDV, perf_1y, fallback price/mcap when tracker missing)",
            "DefiLlama /protocols (TVL par gecko_id)",
            "DefiLlama /overview/fees?dataType=dailyFees (revenue TTM par gecko_id)",
        ],
        "tracker_cache_updated": tracker.get("updated"),
        "n_narratives": len(narratives_out),
        "n_tokens_total": len(all_ids),
        "n_tokens_fetched": len(token_fund),
        "n_overridden_by_tracker": overridden,
        "n_stale_filled": stale_filled,
        "n_with_tvl": sum(1 for t in token_fund.values() if t.get("tvl_b")),
        "n_with_rev": sum(1 for t in token_fund.values() if t.get("rev_m_1y")),
        "narratives": narratives_out,
    }

    with OUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[ok] wrote {OUT_JSON}")
    with OUT_JS.open("w", encoding="utf-8") as f:
        f.write("window.__NARRATIVES_FUNDAMENTALS__=" +
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n")
    print(f"[ok] wrote {OUT_JS}")

    print("\n[summary] top 10 narratives by mcap:")
    for n in narratives_out[:10]:
        ps = n.get("ps_ttm")
        ps_s = f"{ps:.1f}x" if ps else "—"
        tvl = n.get("tvl_total_b")
        tvl_s = f"{tvl:.1f}B" if tvl else "—"
        print(f"  {n['narrative']:<32} mcap={n['mcap_total_b']:>7.1f}B  P/S={ps_s:<7}  TVL={tvl_s:<8}  n={n['n_tokens']}  tvl_n={n['n_with_tvl']}")


if __name__ == "__main__":
    main()
