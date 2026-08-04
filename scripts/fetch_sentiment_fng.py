#!/usr/bin/env python3
"""Sentiment Marche — Reproduction transparente du CNN Fear & Greed Index.

REFONTE 2026-05-26 : remplace l'ancien fetch_sentiment_index.py (17 termes GT + VIX,
indicateur descriptif) par un VRAI composite multi-dimensions a la CNN.

METHODOLOGIE — 6 SOUS-COMPOSANTES (modele CNN F&G, adaptations transparentes)
=============================================================================
Chaque sous-composante est convertie en score 0-100 via percentile EXPANDING
(no look-ahead). La composite finale = moyenne arithmetique equal-weighted des
sous-composantes disponibles a la date t (minimum 4 sur 6).
Echelle 0=peur extreme, 100=greed extreme (style CNN F&G).

  1. STOCK PRICE MOMENTUM     SPY / SMA(SPY, 125j) - 1
                              Source : Twelve Data SPY (1993+)

  2. STOCK PRICE STRENGTH     % des 9 SPDR sector ETFs > leur SMA 200j
                              (adaptation : CNN utilise NYSE 52w hi-lo, non
                              accessible librement. Sector breadth est un proxy
                              d'amplitude reconnu, plus stable que 52w hi-lo.)
                              Source : Twelve Data XLK/XLF/XLE/XLI/XLV/XLY/XLP/XLU/XLB (1998+)

  3. STOCK PRICE BREADTH      McClellan-style : MA 10j du % de sectors avec
                              rendement journalier positif (advance ratio)
                              Source : meme panier 9 sectors (1998+)

  4. MARKET VOLATILITY        MA 50j du VIX, INVERSE
                              Source : FRED VIXCLS (1990+)

  5. SAFE HAVEN DEMAND        SPY 20j ret - TLT 20j ret
                              Positive (stocks > bonds) = greed
                              Source : Twelve Data SPY (1993+), TLT (2002+)

  6. JUNK BOND DEMAND         Moody's Baa - 10Y Treasury spread, INVERSE
                              Source : FRED BAA10Y (1986+)
                              (BAA10Y au lieu de BAMLH0A0HYM2 car FRED a
                              tronque l'ICE OAS a 3 ans en avril 2026, voir
                              memo project_backtest_credit_spread.md)

NOTE : Le CNN F&G original inclut une 7e composante "Put/Call Ratio CBOE".
L'API CBOE est bloquee depuis 2024, et le CBOE SKEW (substitut potentiel) n'est
pas disponible sur Twelve Data free tier. La 7e composante "Tail Risk (SKEW)"
sera ajoutee si une source alternative est identifiee. La methodologie est
documentee pour permettre cet ajout sans casser le schema JSON.

NO LOOK-AHEAD : chaque sous-score est calcule par percentile EXPANDING sur
l'historique disponible a la date t. Aucune donnee future utilisee.

SOURCES & RATE LIMITS
=====================
  Twelve Data (free tier "basic")  : 8 req/min, 800 req/jour, 11 symboles utilises
                                     (1 refresh quotidien = 11 credits)
  FRED API                         : aucune limite stricte, 2 series utilisees
  Cache TTL                        : 12h (1 refresh/jour suffit)

SORTIE
======
  data/sentiment_index.json     (compat. ascendante : meme schema)
    - monthly.{dates, sentiment_index, sp500, components{...}}
    - daily.{dates, sentiment_index, sp500, components{...}}
    - current.{value, class_label, color, components, prev_*}
    - method.* (sources, formules, periodes)

  data/sentiment_index_meta.json
"""
import argparse
import fcntl
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ──────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
CANDIDATE_SITE_DIRS = [
    Path.home() / "Desktop" / "Site_Crypto_Finance",
    SCRIPT_DIR.parent.parent,
    Path.home() / "Library" / "Application Support" / "SiteCryptoFinance",
]
SITE_DIR = next((p for p in CANDIDATE_SITE_DIRS if (p / "data").exists()), CANDIDATE_SITE_DIRS[0])
DATA_DIR = SITE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

CACHE_DIR_TCC = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR_TCC.mkdir(parents=True, exist_ok=True)
LOCK_FILE = CACHE_DIR_TCC / ".fetch_sentiment_fng.lock"

# Symboles utilises
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLI", "XLV", "XLY", "XLP", "XLU", "XLB"]
TD_SYMBOLS = ["SPY", "TLT"] + SECTOR_ETFS

# Caches TTL : 12h par source brute
CACHE_FILES = {
    "vix": CACHE_DIR_TCC / "fng_vix_daily.json",
    "baa": CACHE_DIR_TCC / "fng_baa10y_daily.json",
    "spy": CACHE_DIR_TCC / "fng_spy_daily.json",
    "tlt": CACHE_DIR_TCC / "fng_tlt_daily.json",
}
for etf in SECTOR_ETFS:
    CACHE_FILES[f"sector_{etf}"] = CACHE_DIR_TCC / f"fng_sector_{etf}_daily.json"

CACHE_TTL_HOURS = 12

# Outputs
OUT_CACHE = DATA_DIR / "sentiment_index.json"
OUT_META = DATA_DIR / "sentiment_index_meta.json"

# FRED API
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"

# Twelve Data API (cle stockee aussi en env pour rotation possible)
TD_API_KEY = os.environ.get("TD_API_KEY", "")
TD_API_URL = "https://api.twelvedata.com/time_series"
TD_RATE_DELAY_SEC = 8.0  # 8 req/min -> 1 req par 7.5s, on prend marge 8s

# Fenetres de calcul
MIN_WINDOW = 252                  # 1 annee de trading pour percentile expanding
MIN_COMPONENTS = 4                # composantes minimum pour calculer le F&G

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# ──────────────────────────────────────────────────────────────────────────
# Utilitaires
# ──────────────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def acquire_lock():
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        log("Une autre instance tourne deja. Exit 0.")
        sys.exit(0)


def cache_load_if_fresh(name):
    p = CACHE_FILES.get(name)
    if not p or not p.exists():
        return None
    try:
        age_h = (datetime.now().timestamp() - p.stat().st_mtime) / 3600
        if age_h < CACHE_TTL_HOURS:
            log(f"  Cache '{name}' frais ({age_h:.1f}h), reutilisation.")
            return json.loads(p.read_text())
    except Exception:
        return None
    return None


def cache_load_stale(name):
    p = CACHE_FILES.get(name)
    if not p or not p.exists():
        return None
    try:
        age_h = (datetime.now().timestamp() - p.stat().st_mtime) / 3600
        log(f"  Cache '{name}' STALE ({age_h:.1f}h, fallback).")
        return json.loads(p.read_text())
    except Exception:
        return None


def cache_save(name, payload):
    p = CACHE_FILES.get(name)
    if p:
        p.write_text(json.dumps(payload, ensure_ascii=False))


def write_live_wrapper(current_dict):
    """Ecrit ~/Library/Caches/site_crypto_finance/sentiment_index_live.js (window.__SENTIMENT_INDEX_LIVE__).
    Pattern wrapper live JS leger (cf project_accueil_live_wrapper_pattern) : source de
    verite unique partagee par Accueil / Indicateur / Macro_Trends, evite les snapshots
    data_inline.js / val hardcodee qui divergent du cache JSON entre 2 re-renders Rmd
    (origine du bug de divergence Macro_Trends 65 / Accueil 70 / Indicateur 72 le 2026-05-30)."""
    try:
        cur = current_dict or {}
        comps = cur.get("components", {}) or {}
        payload = {
            "current": cur.get("value"),
            "class_label": cur.get("class_label"),
            "prev_week": cur.get("prev_week"),
            "prev_month": cur.get("prev_month"),
            "prev_quarter": cur.get("prev_quarter"),
            "prev_semester": cur.get("prev_semester"),
            "prev_year": cur.get("prev_year"),
            "momentum": comps.get("momentum"),
            "strength": comps.get("strength"),
            "breadth": comps.get("breadth"),
            "volatility": comps.get("volatility"),
            "safehaven": comps.get("safehaven"),
            "junk": comps.get("junk"),
            "last_date": cur.get("date"),
            "_source": "sentiment_index_live.js (wrapper fetch_sentiment_fng.py)",
            "_updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        target = CACHE_DIR_TCC / "sentiment_index_live.js"
        js = "window.__SENTIMENT_INDEX_LIVE__=" + json.dumps(payload, ensure_ascii=False) + ";"
        target.write_text(js)
        log(f"=> {target} ({target.stat().st_size:,} bytes)")
    except Exception as e:
        log(f"[WARN] write_live_wrapper a echoue : {e}")


def percentile_rank(value, sorted_values):
    if not sorted_values or value is None or math.isnan(value):
        return None
    n = len(sorted_values)
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_values[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    cnt_below = lo
    hi2 = lo
    while hi2 < n and sorted_values[hi2] == value:
        hi2 += 1
    cnt_equal = hi2 - lo
    return (cnt_below + 0.5 * cnt_equal) / n


def expanding_percentile(series, invert=False, min_window=MIN_WINDOW):
    import bisect
    out = []
    history_sorted = []
    for x in series:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            out.append(None)
            continue
        bisect.insort(history_sorted, x)
        if len(history_sorted) < min_window:
            out.append(None)
            continue
        p = percentile_rank(x, history_sorted)
        if p is None:
            out.append(None)
        else:
            score = (1 - p) * 100 if invert else p * 100
            out.append(round(score, 2))
    return out


def sma(series, window):
    out = []
    for i in range(len(series)):
        if i + 1 < window:
            out.append(None)
            continue
        chunk = [v for v in series[i + 1 - window:i + 1] if v is not None]
        if len(chunk) < max(1, window // 2):
            out.append(None)
        else:
            out.append(sum(chunk) / len(chunk))
    return out


def pct_return_n(series, n):
    out = []
    for i in range(len(series)):
        if i < n or series[i] is None or series[i - n] is None or series[i - n] == 0:
            out.append(None)
        else:
            out.append((series[i] - series[i - n]) / series[i - n] * 100)
    return out


def daily_return(series):
    out = [None]
    for i in range(1, len(series)):
        if series[i] is None or series[i - 1] is None or series[i - 1] == 0:
            out.append(None)
        else:
            out.append((series[i] - series[i - 1]) / series[i - 1] * 100)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Fetcher Twelve Data (avec rate limiting + retry)
# ──────────────────────────────────────────────────────────────────────────
_last_td_call_ts = [0.0]   # mutable container pour partager entre calls


def td_throttle():
    """Pause si moins de TD_RATE_DELAY_SEC depuis le dernier appel TD."""
    elapsed = time.time() - _last_td_call_ts[0]
    if elapsed < TD_RATE_DELAY_SEC:
        wait = TD_RATE_DELAY_SEC - elapsed
        time.sleep(wait)


def _fetch_td_chunk(symbol, start_date_iso, end_date_iso, max_retries=3):
    """Une requete time_series Twelve Data (outputsize=5000). Retourne {date:close}.
    Si TD renvoie une erreur ou un payload vide, leve RuntimeError (chunk vide
    legitimement = on a depasse la date de cotation, traite par l'appelant)."""
    last_err = None
    for attempt in range(max_retries):
        if attempt > 0:
            wait = min(60, 15 * attempt)
            log(f"    Retry {attempt}/{max_retries-1} apres {wait}s...")
            time.sleep(wait)
        td_throttle()
        try:
            log(f"  Fetch TD {symbol} [{start_date_iso} -> {end_date_iso or 'today'}] (attempt {attempt+1}/{max_retries})...")
            params = {
                "symbol": symbol,
                "interval": "1day",
                "outputsize": 5000,
                "apikey": TD_API_KEY,
                "format": "JSON",
                "start_date": start_date_iso,
            }
            if end_date_iso:
                params["end_date"] = end_date_iso
            url = TD_API_URL + "?" + urlencode(params)
            req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read())
            _last_td_call_ts[0] = time.time()

            if isinstance(payload, dict) and payload.get("status") == "error":
                code = payload.get("code")
                msg = payload.get("message", "?")
                if code in (429, 400) and "limit" in msg.lower():
                    raise RuntimeError(f"rate limit : {msg}")
                raise RuntimeError(f"TD error {code}: {msg}")

            values = payload.get("values") or []
            out = {}
            for v in values:
                dt = v.get("datetime")
                c = v.get("close")
                if not dt or c is None or c == "":
                    continue
                try:
                    out[dt] = round(float(c), 4)
                except (ValueError, TypeError):
                    continue
            return out
        except Exception as e:
            last_err = e
            err_str = str(e)
            if "rate" in err_str.lower() or "limit" in err_str.lower() or "429" in err_str:
                continue
            if attempt >= 1:
                break
    raise RuntimeError(f"chunk fail: {last_err}")


def fetch_td_daily(symbol, cache_name, start_year=1990, max_retries=3):
    """Twelve Data time_series daily, AVEC pagination vers le passe. Cache 12h.
    Retourne dict {YYYY-MM-DD: close}.

    TD limite outputsize=5000 par requete (~20 ans de jours ouvres). Pour les
    symboles dont l'historique reel depasse cette fenetre (SPY 1993+, sectors
    1998+, TLT 2002+), on enchaine des chunks vers le passe via end_date jusqu'a
    atteindre start_year ou la date de cotation. Sinon la composite F&G demarre
    artificiellement vers 2007 (warmup percentile 252j) au lieu d'utiliser tout
    l'historique disponible."""
    start_iso = f"{start_year}-01-01"
    cached = cache_load_if_fresh(cache_name)
    if cached and min(cached.keys()) <= start_iso:
        return cached

    # Si on a un cache frais mais incomplet en historique, on l'etend uniquement
    # vers l'arriere ; sinon on part du premier chunk (jusqu'a aujourd'hui).
    out = dict(cached) if cached else {}
    if out:
        earliest_iso = min(out.keys())
        end_date_iso = (date.fromisoformat(earliest_iso) - timedelta(days=1)).isoformat()
    else:
        end_date_iso = None  # premier chunk : jusqu'a aujourd'hui

    chunks_fetched = 0
    MAX_CHUNKS = 6  # garde-fou (1993->2026 = 2 chunks SPY ; sectors 1998->2026 = 2)
    try:
        while chunks_fetched < MAX_CHUNKS:
            chunk = _fetch_td_chunk(symbol, start_iso, end_date_iso, max_retries)
            chunks_fetched += 1
            if not chunk:
                break  # plus de donnees disponibles cote TD (debut de cotation atteint)
            out.update(chunk)
            chunk_earliest = min(chunk.keys())
            # Stop si on a atteint le start_year, ou si le chunk est sous-dimensionne
            # (= TD a renvoye moins que outputsize, donc la cotation commence dans ce chunk).
            if chunk_earliest <= start_iso or len(chunk) < 4500:
                break
            end_date_iso = (date.fromisoformat(chunk_earliest) - timedelta(days=1)).isoformat()
    except RuntimeError as e:
        log(f"    WARN Twelve Data {symbol} pagination interrompue : {e}")
        if not out:
            stale = cache_load_stale(cache_name)
            if stale:
                return stale
            return {}

    if len(out) < 200:
        log(f"    WARN {symbol} trop court : {len(out)} jours")
        return cache_load_stale(cache_name) or {}

    cache_save(cache_name, out)
    log(f"    Twelve Data {symbol} OK : {len(out)} jours ({min(out)} -> {max(out)}, {chunks_fetched} chunk(s))")
    return out


def fetch_fred_daily(series_id, cache_name, start="1990-01-01"):
    cached = cache_load_if_fresh(cache_name)
    if cached:
        return cached

    log(f"  Fetch FRED {series_id}...")
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "observation_start": start,
    }
    url = FRED_API_URL + "?" + urlencode(params)
    try:
        with urlopen(url, timeout=60) as resp:
            data = json.loads(resp.read())
    except (HTTPError, URLError, TimeoutError) as e:
        log(f"    WARN FRED {series_id} echec : {e}")
        return cache_load_stale(cache_name) or {}

    obs = data.get("observations", [])
    out = {}
    for ob in obs:
        v = ob.get("value")
        d = ob.get("date")
        if v in (None, ".", "") or not d:
            continue
        try:
            out[d] = float(v)
        except ValueError:
            continue
    if out:
        cache_save(cache_name, out)
    log(f"    FRED {series_id} OK : {len(out)} obs ({min(out) if out else '?'} -> {max(out) if out else '?'})")
    return out


# ──────────────────────────────────────────────────────────────────────────
# Alignement
# ──────────────────────────────────────────────────────────────────────────
def align_series(d, dates, ffill_limit=5):
    out = []
    last_val = None
    last_idx_since = None
    for i, dt in enumerate(dates):
        if dt in d:
            last_val = d[dt]
            last_idx_since = i
            out.append(last_val)
        else:
            if last_val is not None and last_idx_since is not None and (i - last_idx_since) <= ffill_limit:
                out.append(last_val)
            else:
                out.append(None)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Composantes
# ──────────────────────────────────────────────────────────────────────────
def compute_sector_breadth_strength(dates, sector_series_aligned):
    n_sectors = len(sector_series_aligned)
    n_dates = len(dates)

    sector_sma200 = [sma(s, 200) for s in sector_series_aligned]
    sector_ret = [daily_return(s) for s in sector_series_aligned]

    strength_raw = []
    advance_pct_raw = []
    min_avail = int(n_sectors * 0.7)
    for i in range(n_dates):
        above = 0
        total = 0
        for j in range(n_sectors):
            px = sector_series_aligned[j][i]
            ma = sector_sma200[j][i]
            if px is not None and ma is not None:
                total += 1
                if px > ma:
                    above += 1
        if total >= min_avail:
            strength_raw.append(above / total)
        else:
            strength_raw.append(None)

        up = 0
        total_r = 0
        for j in range(n_sectors):
            r = sector_ret[j][i]
            if r is not None:
                total_r += 1
                if r > 0:
                    up += 1
        if total_r >= min_avail:
            advance_pct_raw.append(up / total_r)
        else:
            advance_pct_raw.append(None)

    breadth_raw = sma(advance_pct_raw, 10)
    return strength_raw, breadth_raw


def compute_components(dates, spy, vix, baa, tlt, sector_series):
    """Calcule les 6 sous-composantes 0-100."""
    log("Step 3: calcul des 6 sous-composantes")

    # 1. MOMENTUM = SPY / SMA(SPY, 125) - 1
    spy_sma125 = sma(spy, 125)
    momentum_raw = []
    for px, ma in zip(spy, spy_sma125):
        if px is None or ma is None or ma == 0:
            momentum_raw.append(None)
        else:
            momentum_raw.append(px / ma - 1.0)
    momentum_score = expanding_percentile(momentum_raw, invert=False)
    log(f"    1. Momentum:    {sum(1 for x in momentum_score if x is not None):5d} pts non-null")

    # 2 + 3. STRENGTH + BREADTH
    strength_raw, breadth_raw = compute_sector_breadth_strength(dates, sector_series)
    strength_score = expanding_percentile(strength_raw, invert=False)
    breadth_score = expanding_percentile(breadth_raw, invert=False)
    log(f"    2. Strength:    {sum(1 for x in strength_score if x is not None):5d} pts non-null (sector breadth %above MA200)")
    log(f"    3. Breadth:     {sum(1 for x in breadth_score if x is not None):5d} pts non-null (sector advance ratio MA10)")

    # 4. VOLATILITY = MA50 VIX, INVERSE
    vix_50d = sma(vix, 50)
    vix_score = expanding_percentile(vix_50d, invert=True)
    log(f"    4. Volatility:  {sum(1 for x in vix_score if x is not None):5d} pts non-null")

    # 5. SAFE HAVEN = SPY 20j - TLT 20j
    spy_ret20 = pct_return_n(spy, 20)
    tlt_ret20 = pct_return_n(tlt, 20)
    safehaven_raw = []
    for s, t in zip(spy_ret20, tlt_ret20):
        if s is None or t is None:
            safehaven_raw.append(None)
        else:
            safehaven_raw.append(s - t)
    safehaven_score = expanding_percentile(safehaven_raw, invert=False)
    log(f"    5. SafeHaven:   {sum(1 for x in safehaven_score if x is not None):5d} pts non-null")

    # 6. JUNK BOND = BAA10Y, INVERSE
    junk_score = expanding_percentile(baa, invert=True)
    log(f"    6. JunkBond:    {sum(1 for x in junk_score if x is not None):5d} pts non-null")

    return {
        "momentum":   momentum_score,
        "strength":   strength_score,
        "breadth":    breadth_score,
        "volatility": vix_score,
        "safehaven":  safehaven_score,
        "junk":       junk_score,
    }


def composite_score(components, n_dates):
    keys = ["momentum", "strength", "breadth", "volatility", "safehaven", "junk"]
    out = []
    n_used = []
    for i in range(n_dates):
        vals = [components[k][i] for k in keys if components[k][i] is not None]
        if not vals:
            out.append(None)
            n_used.append(0)
        else:
            out.append(round(sum(vals) / len(vals), 2))
            n_used.append(len(vals))
    return out, n_used


# ──────────────────────────────────────────────────────────────────────────
# Resample daily -> monthly
# ──────────────────────────────────────────────────────────────────────────
def resample_monthly(dates, *series_lists):
    by_month = {}
    for i, d in enumerate(dates):
        ym = d[:7]
        by_month.setdefault(ym, []).append(i)
    out_dates = []
    out_series = [[] for _ in series_lists]
    for ym in sorted(by_month):
        idxs = by_month[ym]
        out_dates.append(ym + "-01")
        for j, s in enumerate(series_lists):
            v = None
            for idx in reversed(idxs):
                if s[idx] is not None:
                    v = s[idx]
                    break
            out_series[j].append(v)
    return (out_dates, *out_series)


# ──────────────────────────────────────────────────────────────────────────
# Labels / colors (style CNN F&G)
# ──────────────────────────────────────────────────────────────────────────
def fng_label(v):
    if v is None:
        return "N/A"
    if v >= 75: return "Euphorie"
    if v >= 55: return "Greed"
    if v >= 45: return "Neutre"
    if v >= 25: return "Peur"
    return "Peur extreme"


def fng_color(v):
    if v is None:
        return "#94a3b8"
    if v >= 75: return "#27ae60"
    if v >= 55: return "#74b9ff"
    if v >= 45: return "#94a3b8"
    if v >= 25: return "#e67e22"
    return "#e74c3c"


# ──────────────────────────────────────────────────────────────────────────
# Pipeline principal
# ──────────────────────────────────────────────────────────────────────────
def compute_sentiment_fng():
    log("=== Sentiment Marche (CNN F&G reproduit, 6 composantes) ===")
    log("Step 1: fetch des sources")

    # FRED (rapide, sans rate-limit)
    vix_d = fetch_fred_daily("VIXCLS", "vix", start="1990-01-01")
    baa_d = fetch_fred_daily("BAA10Y", "baa", start="1986-01-01")

    # Twelve Data : 11 symboles (8 req/min => ~88s pour tout fetcher)
    log(f"Step 1b: fetch Twelve Data ({len(TD_SYMBOLS)} symboles, ~{len(TD_SYMBOLS) * 8}s)")
    spy_d = fetch_td_daily("SPY", "spy", start_year=1993)
    tlt_d = fetch_td_daily("TLT", "tlt", start_year=2002)
    sector_data = {}
    for etf in SECTOR_ETFS:
        sector_data[etf] = fetch_td_daily(etf, f"sector_{etf}", start_year=1998)

    # Step 2: calendrier base sur SPY (le plus fiable, longue history)
    sources = {"SPY": spy_d, "TLT": tlt_d, "VIX": vix_d, "BAA": baa_d}
    for etf in SECTOR_ETFS:
        sources[etf] = sector_data[etf]
    for k, d in sources.items():
        if d:
            log(f"  {k:5s}: {len(d):>6} jours  ({min(d)} -> {max(d)})")
        else:
            log(f"  {k:5s}: VIDE")

    if not spy_d:
        log("ERREUR : SPY vide, abort.")
        sys.exit(1)

    all_dates = sorted(spy_d.keys())
    log(f"Step 2: calendrier {all_dates[0]} -> {all_dates[-1]} ({len(all_dates)} jours)")

    spy_s = align_series(spy_d, all_dates, ffill_limit=5)
    vix_s = align_series(vix_d, all_dates, ffill_limit=5)
    baa_s = align_series(baa_d, all_dates, ffill_limit=10)
    tlt_s = align_series(tlt_d, all_dates, ffill_limit=5)
    sector_aligned = [align_series(sector_data[etf], all_dates, ffill_limit=5) for etf in SECTOR_ETFS]

    # Step 3-4 : composantes + composite
    components = compute_components(all_dates, spy_s, vix_s, baa_s, tlt_s, sector_aligned)
    fng_series, n_used = composite_score(components, len(all_dates))

    fng_series = [v if n >= MIN_COMPONENTS else None for v, n in zip(fng_series, n_used)]

    log(f"Step 4: F&G composite = {sum(1 for v in fng_series if v is not None)} jours valides")

    # Step 5 : resample mensuel
    keys = ["momentum", "strength", "breadth", "volatility", "safehaven", "junk"]
    monthly = resample_monthly(
        all_dates, fng_series, spy_s,
        components["momentum"], components["strength"], components["breadth"],
        components["volatility"], components["safehaven"], components["junk"],
    )
    monthly_dates = monthly[0]
    monthly_fng = monthly[1]
    monthly_sp = monthly[2]
    monthly_components = {k: monthly[3 + i] for i, k in enumerate(keys)}
    log(f"Step 5: resample mensuel = {len(monthly_dates)} mois")

    # Step 6 : current snapshot
    last_idx = next((i for i in range(len(fng_series) - 1, -1, -1) if fng_series[i] is not None), -1)
    if last_idx < 0:
        log("ERREUR: aucun F&G valide.")
        sys.exit(1)
    last_date = all_dates[last_idx]
    cur_val = fng_series[last_idx]

    def find_back_daily(days_back):
        idx = last_idx - days_back
        if idx < 0:
            return None
        while idx >= 0 and fng_series[idx] is None:
            idx -= 1
        return fng_series[idx] if idx >= 0 else None

    history_changes = {
        "1w":  find_back_daily(5),
        "1m":  find_back_daily(21),
        "3m":  find_back_daily(63),
        "6m":  find_back_daily(126),
        "12m": find_back_daily(252),
    }

    current_components = {k: components[k][last_idx] for k in keys}

    spy_sma125_full = sma(spy_s, 125)
    vix_50d_full = sma(vix_s, 50)
    current_raw = {
        "spy": spy_s[last_idx],
        "spy_sma125": spy_sma125_full[last_idx],
        "spy_pct_above_ma125": ((spy_s[last_idx] / spy_sma125_full[last_idx] - 1) * 100) if spy_s[last_idx] and spy_sma125_full[last_idx] else None,
        "vix": vix_s[last_idx],
        "vix_50d_ma": vix_50d_full[last_idx],
        "baa10y_spread": baa_s[last_idx],
        "tlt": tlt_s[last_idx],
        "n_sectors_above_ma200": sum(1 for j in range(len(SECTOR_ETFS))
                                     if sector_aligned[j][last_idx] is not None and
                                        sma(sector_aligned[j], 200)[last_idx] is not None and
                                        sector_aligned[j][last_idx] > sma(sector_aligned[j], 200)[last_idx]),
    }

    current = {
        "date": last_date,
        "value": cur_val,
        "class_label": fng_label(cur_val),
        "color": fng_color(cur_val),
        "prev_week":     history_changes["1w"],
        "prev_month":    history_changes["1m"],
        "prev_quarter":  history_changes["3m"],
        "prev_semester": history_changes["6m"],
        "prev_year":     history_changes["12m"],
        "components": {k: (round(v, 2) if v is not None else None) for k, v in current_components.items()},
        "n_components_used": n_used[last_idx],
        "raw_inputs": {k: (round(v, 4) if isinstance(v, (int, float)) else v) for k, v in current_raw.items()},
    }

    # Output
    out = {
        "schema_version": "2.0",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "CNN Fear & Greed reproduction (Twelve Data + FRED)",
        "method": {
            "name": "CNN Fear & Greed Index — reproduction transparente (6 composantes)",
            "components": [
                {"key": "momentum",   "label": "Stock Price Momentum",  "source": "Twelve Data SPY",     "formula": "SPY / SMA(SPY, 125j) - 1",                "invert": False, "weight": "1/6"},
                {"key": "strength",   "label": "Stock Price Strength",  "source": "Twelve Data 9 ETFs",  "formula": "% des 9 SPDR sectors > SMA 200j",         "invert": False, "weight": "1/6"},
                {"key": "breadth",    "label": "Stock Price Breadth",   "source": "Twelve Data 9 ETFs",  "formula": "MA 10j du % sectors avec rendement>0",    "invert": False, "weight": "1/6"},
                {"key": "volatility", "label": "Market Volatility",     "source": "FRED VIXCLS",         "formula": "MA 50j du VIX, INVERSE",                  "invert": True,  "weight": "1/6"},
                {"key": "safehaven",  "label": "Safe Haven Demand",     "source": "Twelve Data SPY+TLT", "formula": "SPY 20j ret - TLT 20j ret",               "invert": False, "weight": "1/6"},
                {"key": "junk",       "label": "Junk Bond Demand",      "source": "FRED BAA10Y",         "formula": "Baa - 10Y Treasury spread, INVERSE",      "invert": True,  "weight": "1/6"},
            ],
            "weighting": "equal-weighted (1/6 par sous-composante)",
            "scale": "0 = peur extreme, 100 = greed extreme (style CNN Fear & Greed)",
            "normalization": "percentile EXPANDING par sous-composante (no look-ahead, MIN_WINDOW=252 jours)",
            "frequency": "daily",
            "hist_start": all_dates[0],
            "sector_etfs": SECTOR_ETFS,
            "min_components_for_score": MIN_COMPONENTS,
            "adaptations_vs_cnn": {
                "momentum": "SPY (ETF S&P 500) au lieu de ^GSPC (index) car SPX requiert plan Pro Twelve Data. Difference de prix negligeable (SPY tracker SP500 fidelement).",
                "strength": "CNN utilise NYSE 52w hi-lo (non accessible librement). Remplace par % des 9 SPDR sectors > SMA 200j (proxy d'amplitude reconnu).",
                "breadth":  "CNN utilise McClellan Volume Summation NYSE (non accessible librement). Remplace par MA 10j du % de sectors avec rendement positif (proxy McClellan).",
                "putcall":  "CNN inclut un Put/Call ratio CBOE (7e composante). L'API CBOE est bloquee depuis 2024. Le CBOE SKEW (substitut potentiel) n'est pas disponible sur Twelve Data free tier. Cette 7e composante sera ajoutee ulterieurement si une source alternative est trouvee.",
                "junk":     "BAA10Y (Moody's Baa - 10Y Treasury) au lieu de BAMLH0A0HYM2 car FRED a tronque l'ICE OAS a 3 ans en avril 2026.",
            }
        },
        "current": current,
        "monthly": {
            "dates": monthly_dates,
            "sentiment_index": monthly_fng,
            "sp500": monthly_sp,  # NB : c'est SPY (ETF), pas l'index SPX
            "components": monthly_components,
        },
        "daily": {
            "dates": all_dates,
            "sentiment_index": fng_series,
            "sp500": spy_s,       # NB : c'est SPY (ETF), pas l'index SPX
            "components": components,
        },
    }
    return out


def sanity_check(out):
    cur = out["current"]["value"]
    if cur is None or not isinstance(cur, (int, float)) or not (0 <= cur <= 100):
        raise ValueError(f"SANITY FAIL: current.value={cur} hors [0,100]")

    daily_si = out["daily"]["sentiment_index"]
    n_daily_valid = sum(1 for v in daily_si if v is not None)
    if n_daily_valid < 1000:
        raise ValueError(f"SANITY FAIL: seulement {n_daily_valid} jours F&G valides")

    n_comps = sum(1 for v in out["current"]["components"].values() if v is not None)
    if n_comps < MIN_COMPONENTS:
        raise ValueError(f"SANITY FAIL: {n_comps}/6 composantes valides aujourd'hui (min={MIN_COMPONENTS})")

    log(f"  Sanity OK : cur={cur:.1f}, daily valid={n_daily_valid}, {n_comps}/6 composantes")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="invalide tous les caches")
    args = ap.parse_args()

    fd = acquire_lock()
    try:
        if args.force:
            for p in CACHE_FILES.values():
                if p.exists():
                    p.unlink()
                    log(f"  Cache supprime: {p.name}")

        out = compute_sentiment_fng()
        sanity_check(out)

        tmp = OUT_CACHE.with_suffix(OUT_CACHE.suffix + ".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False))
        tmp.replace(OUT_CACHE)
        log(f"=> {OUT_CACHE} ({OUT_CACHE.stat().st_size:,} bytes)")

        meta = {
            "updated_at": out["updated_at"],
            "n_daily": len(out["daily"]["dates"]),
            "n_monthly": len(out["monthly"]["dates"]),
            "date_min": out["daily"]["dates"][0],
            "date_max": out["daily"]["dates"][-1],
            "current_value": out["current"]["value"],
            "current_date": out["current"]["date"],
            "current_label": out["current"]["class_label"],
            "components_today": out["current"]["components"],
            "raw_inputs_today": out["current"]["raw_inputs"],
            "source": out["source"],
        }
        OUT_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        log(f"=> {OUT_META}")

        write_live_wrapper(out.get("current"))

        c = out["current"]
        log("")
        log("=" * 70)
        log(f"Sentiment Marche (CNN F&G, 6 composantes) @ {c['date']} = {c['value']}/100 ({c['class_label']})")
        log("  Composantes (sur 100, equal-weighted) :")
        for k in ["momentum", "strength", "breadth", "volatility", "safehaven", "junk"]:
            v = c["components"].get(k)
            log(f"    {k:12s} = {v if v is not None else 'N/A'}")
        log(f"  History : 1w={c['prev_week']} | 1m={c['prev_month']} | 3m={c['prev_quarter']} | 6m={c['prev_semester']} | 1y={c['prev_year']}")
        log("=" * 70)

    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
