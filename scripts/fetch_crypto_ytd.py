#!/usr/bin/env python3
"""Crypto Bubble Map — base de prix YTD (pour le bouton "YTD" de la carte bulle crypto).

CoinGecko /coins/markets (utilise live cote JS) n'expose PAS de variation YTD, et
/coins/{id}/history est inutilisable sur le free tier (429 systematique sans cle).
On pre-calcule donc, pour le top 100 par market cap, le PRIX DE REFERENCE au 1er
janvier de l'annee courante.

Source = BINANCE klines (SYMBOL+USDT, candle journaliere du 1er janv -> open). Binance
a des symboles CURÉS (pas de collision contrairement a Yahoo {SYMBOL}-USD ou TAO-USD /
UNI-USD renvoient un AUTRE token au prix ridicule). Fallback Yahoo cure (YAHOO_OVERRIDE)
pour les coins absents de Binance (ex. HYPE). Garde-fou final : on rejette toute base
dont l'ecart avec le prix CoinGecko courant est aberrant (>30x) — tue les mauvais matchs.

Le JS calcule la perf YTD en LIVE :  ytd% = (current_price - base) / base * 100
=> suit le prix live ; la base ne change qu'une fois par an. Cle = coin id CoinGecko.

Sorties (~/Library/Caches/site_crypto_finance/) :
  crypto_ytd_cache.{json,js} -> window.__CRYPTO_YTD_BASE__ = {"bitcoin": 87648.21, ...}
                                + window.__CRYPTO_YTD_YEAR__ = 2026
Charge par index.html (document.write). Lance par launchd toutes les 6h.
Resilience : merge-preserve par coin + seuil MIN_OK (jamais d'ecrasement par du vide).
"""
import json
import sys
import time
import urllib.parse
import warnings
from datetime import datetime, timezone
from pathlib import Path

import requests

warnings.filterwarnings("ignore")

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_JSON = CACHE_DIR / "crypto_ytd_cache.json"
CACHE_JS = CACHE_DIR / "crypto_ytd_cache.js"
CACHE_MAX_HOURS = 5

CG_BASE = "https://api.coingecko.com/api/v3"
BINANCE_BASE = "https://api.binance.com/api/v3/klines"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
N_COINS = 100
MIN_OK = 40        # seuil merge-preserve (run sain)
SANITY_RATIO = 12  # rejette base si max(base,cur)/min(base,cur) > 12 (YTD aberrant >+1100%/-92%
                   # = quasi toujours une collision de symbole Yahoo, pas un vrai mover)

# Coins absents de Binance spot -> symbole Yahoo CURÉ (verifie a la main, X-USD seul
# renvoie un token bidon). Ne PAS mettre les coins qui marchent deja sur Binance.
YAHOO_OVERRIDE = {
    "hype": "HYPE32196-USD",          # Hyperliquid (pas de paire spot Binance)
}
# Stablecoins : YTD ~ 0, inutile (et evite de polluer la couverture)
SKIP_SYMBOLS = {"usdt", "usdc", "dai", "busd", "tusd", "usde", "fdusd", "usds", "pyusd",
                "usdg", "gusd", "rlusd", "usdd", "usdf", "usd0", "usdtb", "usdy", "gho",
                "frax", "lusd", "crvusd"}


def log(msg):
    sys.stderr.write(f"[CryptoYTD] {datetime.now().strftime('%H:%M:%S')} {msg}\n")
    sys.stderr.flush()


def cg_get(path, retry=0):
    try:
        r = requests.get(CG_BASE + path, headers=HEADERS, timeout=25)
        if r.status_code == 200:
            return r.json()
        if (r.status_code == 429 or r.status_code >= 500) and retry < 5:
            wait = 8 * (retry + 1)
            log(f"CG HTTP {r.status_code}, wait {wait}s (retry {retry + 1}/5)")
            time.sleep(wait)
            return cg_get(path, retry + 1)
        log(f"CG HTTP {r.status_code} on {path[:50]} - abandon")
        return None
    except Exception as e:
        if retry < 5:
            wait = 8 * (retry + 1)
            log(f"CG exc {e}, wait {wait}s (retry {retry + 1}/5)")
            time.sleep(wait)
            return cg_get(path, retry + 1)
        log(f"CG exc {e} - abandon")
        return None


def fetch_top_coins(n=N_COINS):
    """[(id, symbol, current_price), ...] du top N par market cap."""
    data = cg_get(
        f"/coins/markets?vs_currency=usd&order=market_cap_desc&per_page={n}&page=1&sparkline=false"
    )
    if not data:
        return []
    out = []
    for c in data:
        if c.get("id"):
            out.append((c["id"], (c.get("symbol") or "").lower(), c.get("current_price")))
    return out


def binance_jan1_open(symbol_usdt, jan1_ms, jan2_ms):
    """Open de la candle journaliere du 1er janv, ou None si symbole absent."""
    url = f"{BINANCE_BASE}?symbol={symbol_usdt}&interval=1d&startTime={jan1_ms}&endTime={jan2_ms}&limit=1"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        d = r.json()
        if isinstance(d, list) and d:
            return round(float(d[0][1]), 8)   # [openTime, open, high, low, close, ...]
    except Exception:
        return None
    return None


def fetch_yahoo_bases(yahoo_syms):
    """{yahoo_symbol: base} via spark range=ytd (prevClose, fallback 1er close), batch de 10."""
    out = {}
    for i in range(0, len(yahoo_syms), 10):
        batch = yahoo_syms[i:i + 10]
        sy = urllib.parse.quote(",".join(batch), safe=",")
        url = f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={sy}&range=ytd&interval=1d"
        for k in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=25)
                if r.status_code == 200:
                    for sym, obj in r.json().items():
                        if not obj:
                            continue
                        base = obj.get("chartPreviousClose")
                        if not base or base <= 0:
                            closes = [c for c in (obj.get("close") or []) if c is not None]
                            base = closes[0] if closes else None
                        if base and base > 0:
                            out[sym] = round(float(base), 8)
                    break
                log(f"Yahoo HTTP {r.status_code} (batch {batch[0]}+) retry {k + 1}/3")
            except Exception as e:
                log(f"Yahoo exc {e} retry {k + 1}/3")
            time.sleep(2 * (k + 1))
        time.sleep(0.3)
    return out


def sane(base, current):
    """Garde-fou : base plausible vs prix courant (tue les mauvais matchs symbole)."""
    if not base or base <= 0:
        return False
    if not current or current <= 0:
        return True            # pas de prix courant pour comparer -> on fait confiance
    hi, lo = max(base, current), min(base, current)
    return (hi / lo) <= SANITY_RATIO


def load_json(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return None


def main():
    force = "--force" in sys.argv
    if CACHE_JSON.exists() and not force:
        age_h = (datetime.now().timestamp() - CACHE_JSON.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS:
            log(f"cache frais ({age_h:.1f}h) - skip")
            return

    year = datetime.now().year
    jan1_ms = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    jan2_ms = jan1_ms + 86400000
    prev = load_json(CACHE_JSON) or {}
    prev_base = prev.get("base", {}) if prev.get("year") == year else {}

    log("fetch top 100 coin IDs (CoinGecko)...")
    coins = fetch_top_coins()
    if not coins:
        log("ABORT - aucun coin (CoinGecko indispo)")
        return

    base = {}
    cur_by_id = {cid: cur for cid, _s, cur in coins}
    n_binance = n_yahoo = n_reject = 0

    # 1) Binance d'abord (symboles cures, fiable) ; on collecte les ratés non-stables
    misses = []   # [(cid, sym, cur)]
    for cid, sym, cur in coins:
        if sym in SKIP_SYMBOLS:
            continue
        b = binance_jan1_open(sym.upper() + "USDT", jan1_ms, jan2_ms)
        time.sleep(0.1)
        if b is not None and sane(b, cur):
            base[cid] = b; n_binance += 1
        else:
            if b is not None:
                n_reject += 1   # Binance a renvoye un prix mais aberrant (rare) -> Yahoo tentera
            misses.append((cid, sym, cur))

    # 2) Yahoo en fallback pour TOUS les ratés Binance (batch), garde-fou sane() vs prix courant
    #    -> tue les collisions Yahoo {SYMBOL}-USD (ex. U-USD, LAB-USD = token bidon a prix ridicule)
    if misses:
        ysym_of = {}
        ysyms = []
        for cid, sym, _cur in misses:
            ys = YAHOO_OVERRIDE.get(sym, sym.upper() + "-USD")
            ysym_of[cid] = ys
            if ys not in ysyms:
                ysyms.append(ys)
        ybases = fetch_yahoo_bases(ysyms)
        for cid, sym, cur in misses:
            yb = ybases.get(ysym_of[cid])
            if yb is not None and sane(yb, cur):
                base[cid] = yb; n_yahoo += 1
            elif yb is not None:
                n_reject += 1

    # 3) merge-preserve : tout coin connu avant mais sans base ce run garde sa derniere base
    #    (uniquement si elle reste plausible vs prix courant -> ne ressuscite pas du garbage)
    n_preserve = 0
    for cid, p in prev_base.items():
        if cid not in base and sane(p, cur_by_id.get(cid)):
            base[cid] = p; n_preserve += 1

    absents = [s for cid, s, _c in coins if cid not in base and s not in SKIP_SYMBOLS]
    log(f"bases: {n_binance} binance + {n_yahoo} yahoo + {n_preserve} preserved"
        f" | {n_reject} rejetes (aberrants) | {len(absents)} absents: {','.join(absents[:14])}")

    if len(base) < MIN_OK and prev_base:
        log(f"run maigre ({len(base)} < {MIN_OK}) — conserve cache precedent")
        return

    updated = datetime.now().isoformat()
    CACHE_JSON.write_text(json.dumps({"updated": updated, "year": year, "base": base}, separators=(",", ":")))
    log(f"wrote {CACHE_JSON.name} ({len(base)} coins)")
    with open(CACHE_JS, "w") as f:
        f.write("window.__CRYPTO_YTD_BASE__=" + json.dumps(base, separators=(",", ":")) + ";\n")
        f.write(f"window.__CRYPTO_YTD_YEAR__={year};\n")
        f.write(f"window.__CRYPTO_YTD_UPDATED__={json.dumps(updated)};\n")
    log(f"wrote {CACHE_JS.name}")


if __name__ == "__main__":
    main()
