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
N_COINS = 300      # profondeur du spectre de la carte (top 100 / 200 / 300)
MIN_OK = 40        # seuil merge-preserve (run sain)
M3_DAYS = 90       # fenetre « 3 mois » de la carte
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
    """[(id, symbol, current_price), ...] du top N par market cap.

    CoinGecko plafonne /coins/markets a 250 lignes par page : au-dela on pagine."""
    out = []
    page = 1
    while len(out) < n:
        per = min(250, n - len(out))
        data = cg_get(
            f"/coins/markets?vs_currency=usd&order=market_cap_desc&per_page={per}&page={page}&sparkline=false"
        )
        if not data:
            break
        for c in data:
            if c.get("id"):
                out.append((c["id"], (c.get("symbol") or "").lower(), c.get("current_price")))
        if len(data) < per:
            break                              # plus rien a paginer
        page += 1
        time.sleep(2.5)                        # free tier : espacer les pages
    return out[:n]


def binance_daily_series(symbol_usdt, start_ms):
    """Candles journalieres depuis start_ms -> [(openTime_ms, open), ...] ou [] si absent.

    UN SEUL appel sert les deux bases (1er janvier et 3 mois) : le 1er janvier etant
    toujours anterieur a la fenetre de 3 mois, la meme serie les contient toutes deux."""
    url = f"{BINANCE_BASE}?symbol={symbol_usdt}&interval=1d&startTime={start_ms}&limit=500"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return []
        d = r.json()
        if isinstance(d, list) and d:
            return [(int(k[0]), float(k[1])) for k in d]   # [openTime, open, high, low, close, ...]
    except Exception:
        return []
    return []


def _open_at_or_before(series, ts_ms):
    """Ouverture de la derniere bougie commencant a ts_ms ou avant (None si aucune)."""
    val = None
    for t, o in series:
        if t <= ts_ms:
            val = o
        else:
            break
    return round(val, 8) if val else None


def fetch_yahoo_bases(yahoo_syms, rng="ytd"):
    """{yahoo_symbol: base} via spark (prevClose, fallback 1er close), batch de 10."""
    out = {}
    for i in range(0, len(yahoo_syms), 10):
        batch = yahoo_syms[i:i + 10]
        sy = urllib.parse.quote(",".join(batch), safe=",")
        url = f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={sy}&range={rng}&interval=1d"
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


def coins_from_snapshot():
    """Liste de REPLI quand CoinGecko refuse : le top 100 du dernier relevé CoinGecko
    conservé par le collecteur radar (radar_cache.json, data.__CG_COINS__).
    POURQUOI (2026-09-29) : CoinGecko bloquait l'IP du Mac (403) et ce collecteur
    abandonnait → la base 3 mois de la carte crypto n'était jamais produite. La
    liste ne sert qu'à nommer les pièces : les prix de référence viennent de
    Binance/Yahoo, et le prix courant ne sert qu'au contrôle de vraisemblance."""
    try:
        d = json.loads((CACHE_DIR / "radar_cache.json").read_text())
        rows = (d.get("data") or {}).get("__CG_COINS__") or []
        out = [(c["id"], (c.get("symbol") or "").lower(), c.get("current_price"))
               for c in rows if c.get("id")]
        if out:
            log(f"CoinGecko indispo → liste de repli : {len(out)} coins du relevé radar")
        return out[:N_COINS]
    except Exception as e:
        log(f"liste de repli illisible ({e})")
        return []


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
    m3_ms = int((datetime.now(timezone.utc).timestamp() - M3_DAYS * 86400) * 1000)
    prev = load_json(CACHE_JSON) or {}
    prev_base = prev.get("base", {}) if prev.get("year") == year else {}
    prev_m3 = prev.get("m3", {})          # la base 3 mois GLISSE : jamais preservee d'un run a l'autre

    log(f"fetch top {N_COINS} coin IDs (CoinGecko)...")
    coins = fetch_top_coins()
    if not coins:
        coins = coins_from_snapshot()
    if not coins:
        log("ABORT - aucun coin (CoinGecko indispo, pas de liste de repli)")
        return
    log(f"  {len(coins)} coins recus")

    base = {}                              # base au 1er janvier (YTD)
    m3 = {}                                # base a J-90 (3 mois)
    cur_by_id = {cid: cur for cid, _s, cur in coins}
    n_binance = n_yahoo = n_reject = 0

    # 1) Binance d'abord (symboles cures, fiable) ; on collecte les ratés non-stables.
    #    Une seule serie journaliere depuis le 1er janvier porte les DEUX bases.
    misses = []   # [(cid, sym, cur)]
    for cid, sym, cur in coins:
        if sym in SKIP_SYMBOLS:
            continue
        series = binance_daily_series(sym.upper() + "USDT", jan1_ms)
        time.sleep(0.06)
        # Base YTD = OUVERTURE de la bougie du 1er janvier (= clôture du 31/12, comme
        # le prevClose Yahoo). PIÈGE corrigé le 29/09 : « à ou avant jan2_ms » attrapait
        # la bougie du 2 janvier (elle commence PILE à jan2_ms) → base décalée d'un
        # jour (AAVE 148,93 $ au lieu de 146,02 $, ~2 pt d'erreur sur le YTD).
        b = _open_at_or_before(series, jan1_ms) if series else None
        if b is not None and sane(b, cur):
            base[cid] = b; n_binance += 1
            b3 = _open_at_or_before(series, m3_ms)
            if b3 is not None and sane(b3, cur):
                m3[cid] = b3
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
        ybases = fetch_yahoo_bases(ysyms, "ytd")
        ym3 = fetch_yahoo_bases(ysyms, "3mo")
        for cid, sym, cur in misses:
            yb = ybases.get(ysym_of[cid])
            if yb is not None and sane(yb, cur):
                base[cid] = yb; n_yahoo += 1
                y3 = ym3.get(ysym_of[cid])
                if y3 is not None and sane(y3, cur):
                    m3[cid] = y3
            elif yb is not None:
                n_reject += 1

    # 3) merge-preserve : tout coin connu avant mais sans base ce run garde sa derniere base
    #    (uniquement si elle reste plausible vs prix courant -> ne ressuscite pas du garbage).
    #    Reserve a la base ANNUELLE, qui est fixe : une base 3 mois preservee vieillirait
    #    silencieusement et afficherait « 3 mois » sur une fenetre de 3 mois et demi.
    n_preserve = 0
    for cid, p in prev_base.items():
        if cid not in base and sane(p, cur_by_id.get(cid)):
            base[cid] = p; n_preserve += 1

    absents = [s for cid, s, _c in coins if cid not in base and s not in SKIP_SYMBOLS]
    log(f"bases YTD: {n_binance} binance + {n_yahoo} yahoo + {n_preserve} preserved"
        f" | {n_reject} rejetes (aberrants) | {len(absents)} absents: {','.join(absents[:14])}")
    log(f"bases 3 mois: {len(m3)} coins (fenetre glissante, non preservee)")

    if len(base) < MIN_OK and prev_base:
        log(f"run maigre ({len(base)} < {MIN_OK}) — conserve cache precedent")
        return
    if len(m3) < MIN_OK and prev_m3:
        log(f"3 mois maigre ({len(m3)} < {MIN_OK}) — on garde la base 3 mois precedente")
        m3 = prev_m3

    updated = datetime.now().isoformat()
    CACHE_JSON.write_text(json.dumps({"updated": updated, "year": year, "base": base,
                                      "m3": m3, "m3_days": M3_DAYS}, separators=(",", ":")))
    log(f"wrote {CACHE_JSON.name} ({len(base)} coins YTD, {len(m3)} coins 3 mois)")
    with open(CACHE_JS, "w") as f:
        f.write("window.__CRYPTO_YTD_BASE__=" + json.dumps(base, separators=(",", ":")) + ";\n")
        f.write(f"window.__CRYPTO_YTD_YEAR__={year};\n")
        f.write("window.__CRYPTO_M3_BASE__=" + json.dumps(m3, separators=(",", ":")) + ";\n")
        f.write(f"window.__CRYPTO_M3_DAYS__={M3_DAYS};\n")
        f.write(f"window.__CRYPTO_YTD_UPDATED__={json.dumps(updated)};\n")
    log(f"wrote {CACHE_JS.name}")


if __name__ == "__main__":
    main()
