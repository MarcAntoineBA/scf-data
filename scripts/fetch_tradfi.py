#!/usr/bin/env python3
"""TradFi Tracker — narratifs boursiers mondiaux (momentum approach).

Parallele de fetch_narratives.py mais pour les actions mondiales :
  - ~20 narratifs sectoriels et géographiques
  - ~160 actions couvrant US / Europe / Asie / Emergents
  - Tout via yfinance (gratuit, supporte tous les exchanges via suffixes)
  - News via flux RSS tradfi deja presents dans news_cache.json (section 'macro')
  - Filtre tendance = S&P 500 vs MA200 (au lieu de BTC vs MA200)
"""
import json, re, time, sys, base64, hashlib, os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
import urllib.error
import warnings

warnings.filterwarnings("ignore")

# Force un timeout court sur toutes les requetes yfinance (curl_cffi sous le capot).
# Sans ca, un ticker stuck peut bloquer 60 min sur une seule requete (constate
# 2026-05-06 : '002594.SZ' Timeout 3660558ms = 61 min ; re-constate 2026-07-28 :
# '1816.HK' Timeout 3630240ms = 60 min, qui consommait tout le budget watchdog
# avant que le batch finisse → hard-kill a 3000s → AUCUNE donnee ecrite).
#
# PIEGE (cause du bug 2026-07-28) : la version precedente utilisait
# kwargs.setdefault("timeout", 20), qui ne s'applique QUE si l'appelant n'a rien
# passe. Or yfinance/data.py passe TOUJOURS timeout= explicitement (defaut 30,
# parfois None sur certains chemins) → le setdefault n'a jamais eu le moindre
# effet sur les appels yfinance. Il faut ECRASER, pas completer.
_YF_MAX_TIMEOUT = 20  # secondes, par requete HTTP
try:
    import curl_cffi.requests as _ccr
    _orig_request = _ccr.Session.request
    def _patched_request(self, method, url, *args, **kwargs):
        t = kwargs.get("timeout")
        if isinstance(t, tuple):
            # (connect, read) → plafonne les deux composantes
            kwargs["timeout"] = tuple(min(x, _YF_MAX_TIMEOUT) if isinstance(x, (int, float))
                                      else _YF_MAX_TIMEOUT for x in t)
        elif not isinstance(t, (int, float)) or t > _YF_MAX_TIMEOUT:
            # couvre None (= pas de timeout du tout → hang 60 min) et 30s
            kwargs["timeout"] = _YF_MAX_TIMEOUT
        return _orig_request(self, method, url, *args, **kwargs)
    _ccr.Session.request = _patched_request
except Exception:
    pass  # curl_cffi pas installe, yfinance utilisera requests standard


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
    """
    Return a data:image/... URI for this domain's favicon, or a letter-avatar
    SVG if no valid icon can be fetched. Cached per-domain in this process.
    """
    key = (domain or "").lower()
    if key and key in _FAVICON_CACHE:
        cached = _FAVICON_CACHE[key]
        # If we cached an avatar for this domain, regen with the current symbol
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
                blob = r.read(32 * 1024)  # cap at 32 KB
                # Reject the tiny generic globe / suspiciously small blobs
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
ROOT = CACHE_DIR

# Global script timeout safeguard — 30 minutes max. Si une étape (typiquement le
# fetch favicon ou yfinance batch) reste bloquée sur du I/O, on abort plutôt
# que de monopoliser le lock et bloquer toute la chaîne launchd pendant des heures.
# Le launchd retentera au prochain cycle (1h) avec un cache encore frais (24h).
import signal as _signal
def _global_timeout_handler(signum, frame):
    print(f"[fatal] global timeout (30 min) — aborting to free the lock for next launchd cycle.", file=sys.stderr)
    sys.exit(2)
try:
    _signal.signal(_signal.SIGALRM, _global_timeout_handler)
    _signal.alarm(45 * 60)
except Exception:
    pass  # Windows / non-Unix : signal.alarm pas dispo, tant pis

# Hard kill watchdog via threading.Timer + os._exit(2). SIGALRM peut être avalé
# si Python est bloqué dans un C-extension (ex: requests/urllib3 retry sur
# EINTR à l'intérieur de yahooquery), ce qui a déjà causé un hang de 17h+.
# os._exit() court-circuite l'interpréteur — le seul moyen fiable de tuer le
# process quoi qu'il arrive. Armé à 25 min pour fire AVANT le SIGALRM.
import threading as _threading, os as _os_hk
def _hard_kill_watchdog():
    try:
        print("[fatal] hard watchdog (25 min) — os._exit to free the lock.", file=sys.stderr)
        sys.stderr.flush()
    except Exception:
        pass
    _os_hk._exit(2)
_HK_TIMER = _threading.Timer(40 * 60, _hard_kill_watchdog)
_HK_TIMER.daemon = True
_HK_TIMER.start()
# ── Budget wall-clock de la phase "quotes" (garde-fou 2026-07-28) ─────────
# Le wrapper externe hard-kill le process a 3000s. Avant ce garde-fou, un
# Yahoo lent faisait passer TOUT le budget dans la boucle de chunks : le
# process etait tue avant la phase d'ecriture → aucun fichier produit → la page
# restait figee sur la veille (constate : 14.7h de retard, 2 runs perdus).
#
# On borne donc la phase quotes en horloge REELLE (time.time(), pas un
# threading.Timer : ces derniers ne ticquent pas pendant la veille macOS, c'est
# precisement pourquoi le hard-kill interne a 40 min n'a jamais fire). Au-dela
# du budget on arrete de fetcher et on continue : les tickers manquants sont
# repris du cache precedent (marques _stale) et la couverture reelle est
# affichee. Mieux vaut une page a 90% fraiche que pas de page du tout.
_RUN_START = time.time()
QUOTE_PHASE_BUDGET_S = 1500   # 25 min sur les 50 min avant hard-kill externe

NEWS_CACHE = CACHE_DIR / "news_cache.json"
OUT_CACHE  = CACHE_DIR / "tradfi_cache.json"
OUT_CACHE_JS = CACHE_DIR / "tradfi_cache.js"
HIST_CACHE = CACHE_DIR / "tradfi_history_cache.json"
LOCK_FILE  = CACHE_DIR / "tradfi.lock"
# Cache SQLite yfinance PRIVÉ à ce script (2026-07-31).
# Avant : tous les fetchers (fetch_tradfi_hist, fetch_narratives,
# fetch_tradfi_fundamentals…) partageaient ~/Library/Caches/py-yfinance. Quand
# deux tournent en même temps, le reset SQLite de l'un casse le handle ouvert de
# l'autre → OperationalError('no such table: _tz_kv') en cascade, et des chunks
# entiers de tickers reviennent vides (observé pendant la reprise du 31/07 :
# chunks 5 et 6 à 0/50). Un répertoire par script supprime la collision.
YF_CACHE_DIR = Path.home() / "Library" / "Caches" / "py-yfinance-tradfi"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


# ─────────────────────────────────────────────────────────────────────────
# Robustness helpers — single-instance lock + yfinance SQLite reset
# Without these, launchd can spawn overlapping runs whose threads collide on
# the yfinance ticker-tz SQLite, leaving stale -wal/-shm files that make every
# subsequent run fail with OperationalError('unable to open database file').
# ─────────────────────────────────────────────────────────────────────────
def _read_lock_pid():
    try:
        with open(LOCK_FILE, "r") as f:
            return int((f.read() or "0").strip())
    except Exception:
        return 0


def _proc_etime_seconds(pid):
    """Return elapsed running time of pid in seconds, or 0 if not running."""
    try:
        import subprocess as _sp
        out = _sp.check_output(["ps", "-p", str(pid), "-o", "etimes="],
                               stderr=_sp.DEVNULL).decode().strip()
        return int(out) if out else 0
    except Exception:
        return 0


def acquire_singleton_lock():
    """Non-blocking flock so only one fetch_tradfi runs at a time.
    Returns the lock fd (kept open for process lifetime) or None if busy.
    Self-heal: if the holder PID has been running > 35 min (way past our 25-min
    hard watchdog), it's a zombie hang — force-kill it and reclaim."""
    import fcntl, os, signal as _sig, time as _t
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        holder = _read_lock_pid()
        etime = _proc_etime_seconds(holder) if holder else 0
        if holder and etime > 35 * 60:
            print(f"[lock] holder pid={holder} stuck for {etime}s — sending SIGKILL and reclaiming",
                  file=sys.stderr)
            try:
                os.kill(holder, _sig.SIGKILL)
            except Exception as e:
                print(f"[lock] kill failed: {e}", file=sys.stderr)
            _t.sleep(1)
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                print("[lock] could not reclaim after kill — exiting", file=sys.stderr)
                fd.close()
                return None
        else:
            print(f"[lock] another fetch_tradfi instance is running (pid={holder}, etime={etime}s) — exiting",
                  file=sys.stderr)
            fd.close()
            return None
    fd.seek(0)
    fd.truncate()
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


# Cache yfinance par défaut, PARTAGÉ par les fetchers sans répertoire privé.
# Le purger casse tous les runs concurrents : le garde-fou ci-dessous refuse de
# le faire, quoi qu'on mette dans YF_CACHE_DIR.
YF_SHARED_DIR = Path.home() / "Library" / "Caches" / "py-yfinance"


def reset_yfinance_cache():
    """Remove yfinance's SQLite cache files. yfinance recreates on demand.
    Cheap (<5ms) and prevents the WAL-corruption cascade that kills mass fetches."""
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


def _atomic_write_text(path, text):
    """Ecrit `text` de façon ATOMIQUE (tmp + fsync + os.replace).

    GARDE-FOU (incident 2026-07-31) : le cache principal était écrit en direct
    (`open(OUT_CACHE, "w")`, 25 Mo). Un hard-kill du watchdog en plein write
    laissait un JSON TRONQUÉ ; au run suivant `load_previous_cache()` échouait
    ("Expecting ',' delimiter") → plus aucun gap-fill possible → cache à 0 stock.
    Avec tmp+replace, le fichier visible est toujours soit l'ancien complet,
    soit le nouveau complet — jamais un demi-fichier."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


MIN_COVERED_ABS = 50      # plancher absolu de tickers placés en secteur
COLLAPSE_RATIO  = 0.50    # < 50% de la couverture du run précédent = effondrement


def write_guard(n_covered, prev_covered):
    """Décide si le résultat d'un run peut écraser le cache sur disque.

    Retourne (raison_degradation | None, keep_prev). `keep_prev=True` = ne PAS
    écrire, garder le cache précédent. Voir le bloc GARDE-FOU dans main()."""
    abort = None
    if n_covered < MIN_COVERED_ABS:
        abort = f"seulement {n_covered} tickers placés en secteur (plancher {MIN_COVERED_ABS})"
    elif prev_covered and n_covered < COLLAPSE_RATIO * prev_covered:
        abort = (f"effondrement de couverture : {n_covered} tickers vs {prev_covered} "
                 f"au run précédent (< {int(COLLAPSE_RATIO * 100)}%)")
    # On ne protège que si le cache sur disque est LUI-MÊME sain, sinon une
    # mauvaise journée figerait le pipeline pour toujours.
    return abort, bool(abort) and prev_covered >= MIN_COVERED_ABS


def load_previous_cache():
    """Load the last successful tradfi_cache.json so we can fill gaps for any
    ticker that today's fetch couldn't recover. Returns {} if no prior cache.

    2026-07-31 : repli sur le sibling `tradfi_cache.js` (MEME JSON, écrit
    séparément) si le .json est illisible. Le gap-fill est le seul filet quand
    Yahoo est injoignable — le perdre transforme une panne réseau passagère en
    page vide."""
    for src in (OUT_CACHE, OUT_CACHE_JS):
        if not src.exists():
            continue
        try:
            txt = src.read_text(encoding="utf-8")
            if src.suffix == ".js":
                i, j = txt.find("{"), txt.rfind("}")
                if i < 0 or j < 0:
                    raise ValueError("no JSON object in js wrapper")
                txt = txt[i:j + 1]
            data = json.loads(txt)
            if src is not OUT_CACHE:
                print(f"[warn] prev cache recovered from {src.name} (le .json est illisible)",
                      file=sys.stderr)
            return data
        except Exception as e:
            print(f"[warn] could not load prev cache ({src.name}): {e}", file=sys.stderr)
    return {}


def fetch_stooq_snapshot(yahoo_sym):
    """Stooq snapshot fallback for fetch_stocks: parses the same daily CSV as
    fetch_stooq_history but extracts last close + 7d/30d perf in *local* ccy.
    Caller is expected to FX-adjust afterwards."""
    raw = fetch_stooq_history(yahoo_sym, days=60)
    if not raw or len(raw) < 2:
        return None
    last_ts, last_px = raw[-1]
    p7  = raw[-6][1]  if len(raw) >= 6  else None
    p30 = raw[-22][1] if len(raw) >= 22 else None
    return {
        "last_local": last_px,
        "perf_7d":  ((last_px / p7)  - 1) * 100 if p7  and p7  > 0 else None,
        "perf_30d": ((last_px / p30) - 1) * 100 if p30 and p30 > 0 else None,
    }


HIST_DAYS = 1825            # 5 ans
HIST_TOP_N_PER_NARRATIVE = 3
HIST_CACHE_TTL_HOURS = 24

# ── Currency detection from Yahoo exchange suffix ──
# Maps ticker suffix → currency code (for FX conversion to USD)
SUFFIX_CURRENCY = {
    "":  "USD",  # US default
    "TO": "CAD",  # Toronto
    "V":  "CAD",  # TSX Venture
    "L":  "GBP",  # London (some are GBX = pence, see special-case)
    "PA": "EUR",  # Paris
    "AS": "EUR",  # Amsterdam
    "DE": "EUR",  # XETRA Frankfurt
    "F":  "EUR",  # Frankfurt
    "MI": "EUR",  # Milan
    "MC": "EUR",  # Madrid
    "BR": "EUR",  # Brussels
    "IR": "EUR",  # Ireland
    "LS": "EUR",  # Lisbon
    "SW": "CHF",  # Swiss
    "CO": "DKK",  # Copenhagen
    "ST": "SEK",  # Stockholm
    "OL": "NOK",  # Oslo
    "HE": "EUR",  # Helsinki
    "T":  "JPY",  # Tokyo
    "HK": "HKD",  # Hong Kong
    "NS": "INR",  # NSE India
    "BO": "INR",  # BSE India
    "SS": "CNY",  # Shanghai
    "SZ": "CNY",  # Shenzhen
    "KS": "KRW",  # Seoul KOSPI
    "KQ": "KRW",  # KOSDAQ
    "AX": "AUD",  # Australia
    "TW": "TWD",  # Taiwan
    "SI": "SGD",  # Singapore
    "SA": "BRL",  # São Paulo
    "MX": "MXN",  # Mexico
    "SR": "SAR",  # Saudi Arabia (Tadawul)
    "AE": "AED",  # UAE (ADX Abu Dhabi, DFM Dubai)
    "KL": "MYR",  # Malaysia (Bursa)
    "BK": "THB",  # Thailand
    "JK": "IDR",  # Indonesia
    "JO": "ZAR",  # Johannesburg (Afrique du Sud)
}


def detect_currency(yahoo_symbol):
    """Infer currency from Yahoo ticker suffix."""
    if not yahoo_symbol or "." not in yahoo_symbol:
        return "USD"
    suffix = yahoo_symbol.rsplit(".", 1)[-1]
    return SUFFIX_CURRENCY.get(suffix, "USD")


def fetch_fx_rates(currencies):
    """Fetch up to 15y daily FX rates vs USD for each non-USD currency.
    Returns {CCY: {YYYY-MM-DD: rate_1_CCY_in_USD}}.

    Two-stage fetch :
      1) Direct {CCY}USD=X (e.g. EURUSD=X, JPYUSD=X). Works for major currencies.
      2) Fallback inverse USD{CCY}=X then invert (1/rate). Yahoo only exposes
         the inverse pair for many exotic currencies (IDR, THB, MYR…). Without
         this fallback, the direct fetch returned nothing → apply_fx silently
         left local-currency values in place → BBCA.JK / BMRI.JK / ASII.JK
         leaked raw IDR (~16k IDR per USD) into mcap-weighted sectors and
         blew Banques Asie to 1120 T$, Auto & EV to 241 T$.

    Period must match the stock-history depth (HIST_MAX_DAYS) so non-USD
    histories are converted to USD across the full range."""
    non_usd = sorted({c for c in currencies if c and c != "USD"})
    if not non_usd:
        return {}
    try:
        import yfinance as yf
    except ImportError:
        return {}

    def _download_pair(sym):
        # Retry avec backoff : Yahoo throttle en rafale le matin. Sans retry, un
        # seul refus → devise absente → TOUS ses tickers exclus des agregats
        # (constate 2026-07-28 : KRW/CNY/HKD/SAR/MYR tous perdus d'un coup).
        h = None
        for _attempt in range(3):
            try:
                h = yf.download(sym, period="max", interval="1d",
                                progress=False, threads=False, auto_adjust=False)
            except Exception as e:
                print(f"[warn] FX {sym}: download failed (try {_attempt+1}/3): {e}",
                      file=sys.stderr)
                h = None
            if h is not None and not h.empty:
                break
            if _attempt < 2:
                _fx_sleep = 4 * (_attempt + 1) ** 2   # 4s, 16s
                print(f"[warn] FX {sym}: vide, retry dans {_fx_sleep}s", file=sys.stderr)
                time.sleep(_fx_sleep)
        if h is None or h.empty:
            return None
        # yfinance returns multi-index columns even for a single ticker
        # (e.g. ('Close', 'USDIDR=X')). Flatten to a single Series of float.
        try:
            if hasattr(h.columns, "get_level_values") and "Close" in h.columns.get_level_values(0):
                close_df = h["Close"]
                # `close_df` is a DataFrame with one column → squeeze to Series
                if hasattr(close_df, "squeeze"):
                    closes = close_df.squeeze("columns") if close_df.ndim == 2 else close_df
                else:
                    closes = close_df.iloc[:, 0] if close_df.ndim == 2 else close_df
            elif "Close" in h.columns:
                closes = h["Close"]
            else:
                return None
            closes = closes.dropna()
        except Exception as e:
            print(f"[warn] FX {sym}: parse failed: {e}", file=sys.stderr)
            return None
        if closes is None or len(closes) < 2:
            return None
        return closes

    out = {}
    for c in non_usd:
        # Stage 1 — direct pair
        closes = _download_pair(f"{c}USD=X")
        if closes is not None:
            per_day = {d.strftime("%Y-%m-%d"): float(v) for d, v in closes.items()}
            out[c] = per_day
            latest = list(per_day.values())[-1]
            print(f"[fx] {c}: {len(per_day)} days direct (latest 1 {c} = {latest:.6f} USD)")
            continue
        # Stage 2 — inverse pair, then invert
        inv = _download_pair(f"USD{c}=X")
        if inv is not None:
            per_day = {}
            for d, v in inv.items():
                try:
                    f = float(v)
                    if f > 0:
                        per_day[d.strftime("%Y-%m-%d")] = 1.0 / f
                except Exception:
                    continue
            if len(per_day) >= 2:
                out[c] = per_day
                latest = list(per_day.values())[-1]
                print(f"[fx] {c}: {len(per_day)} days INVERSE USD{c}=X (latest 1 {c} = {latest:.8f} USD)")
                continue
        # Both failed → hard log so apply_fx callers know
        print(f"[FX_FAIL] {c}: no rate available (direct + inverse both empty) — tickers in {c} will use FX_RATES_MISSING sentinel and be EXCLUDED from aggregates", file=sys.stderr)

    # ── Repli sur le dernier FX connu (garde-fou 2026-07-28) ───────────────
    # Un refus Yahoo momentane sur les paires de change faisait exclure TOUS les
    # tickers de la devise concernee (KRW/CNY/HKD/SAR/MYR d'un coup → l'Asie
    # entiere disparaissait des agregats). Un taux de change bouge de <1%/jour :
    # reutiliser celui d'hier est infiniment plus juste que jeter la moitie du
    # monde. On ne touche PAS au prix des actions, seulement a la conversion.
    _fx_persist = CACHE_DIR / "tradfi_fx_cache.json"
    prev_fx = {}
    try:
        if _fx_persist.exists():
            prev_fx = json.loads(_fx_persist.read_text())
    except Exception as e:
        print(f"[warn] FX cache illisible: {e}", file=sys.stderr)

    for c in non_usd:
        if c in out:
            continue
        rescued = prev_fx.get(c)
        if rescued and len(rescued) >= 2:
            out[c] = rescued
            last_day = max(rescued)
            print(f"[FX_FALLBACK] {c}: fetch Yahoo echoue → repli sur le cache FX "
                  f"({len(rescued)} jours, dernier {last_day}) — tickers CONSERVES",
                  file=sys.stderr)
        else:
            print(f"[FX_FAIL] {c}: aucun repli en cache non plus — tickers exclus",
                  file=sys.stderr)

    # Persiste le FX frais pour servir de repli aux prochains runs. On fusionne
    # avec l'existant pour ne jamais perdre une devise absente de ce run-ci.
    try:
        merged = dict(prev_fx)
        merged.update(out)
        _tmp = _fx_persist.with_suffix(".json.tmp")
        _tmp.write_text(json.dumps(merged))
        _tmp.replace(_fx_persist)   # write atomique : jamais de JSON tronque
    except Exception as e:
        print(f"[warn] FX cache non ecrit: {e}", file=sys.stderr)

    return out


# Sentinel: list of currencies for which FX is missing this run. Populated by
# fetch_fx_rates() callers, consumed by apply_fx() to refuse silent passthrough.
_FX_MISSING_LOGGED = set()


def fetch_stooq_history(yahoo_sym, days=5500):
    """Fallback: fetch daily history from Stooq (CSV).
    Stooq uses different ticker format: US stocks get '.us' suffix, HK gets '.hk', etc.
    Returns list of (ts_sec, close) or [] on failure."""
    # Map Yahoo suffix → Stooq suffix
    mapping = {
        "": ".us",     # US stocks: add .us
        "L": ".uk", "PA": ".fr", "AS": ".nl", "DE": ".de", "SW": ".ch",
        "MI": ".it", "MC": ".es", "CO": ".dk", "ST": ".se", "OL": ".no",
        "T": ".jp", "HK": ".hk", "NS": ".in", "KS": ".kr",
    }
    if "." in yahoo_sym:
        base, suf = yahoo_sym.rsplit(".", 1)
        stooq_sym = (base + mapping.get(suf, "." + suf.lower())).lower()
    else:
        stooq_sym = (yahoo_sym + ".us").lower()
    stooq_sym = stooq_sym.replace("-", "")  # Stooq doesn't use hyphens
    url = f"https://stooq.com/q/d/l/?s={stooq_sym}&i=d"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", errors="ignore")
        lines = raw.strip().split("\n")
        if len(lines) < 5 or not lines[0].lower().startswith("date"):
            return []
        out = []
        cutoff = time.time() - days * 86400
        for ln in lines[1:]:
            parts = ln.split(",")
            if len(parts) < 5:
                continue
            try:
                dt = datetime.strptime(parts[0], "%Y-%m-%d")
                ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
                if ts < cutoff:
                    continue
                close = float(parts[4])
                if close > 0:
                    out.append((ts, close))
            except Exception:
                continue
        return out
    except Exception as e:
        print(f"[stooq] {yahoo_sym}: {e}", file=sys.stderr)
        return []


def normalize_local_price(price_local, symbol):
    """Yahoo quotes London .L tickers in GBp (pence = 1/100 GBP) but reports
    currency as 'GBP' in fast_info for many of them. Without this divide by
    100, all UK stocks (Barclays, HSBC, Shell, AZN, Glencore, BP, Rio…) come
    out 100x too high in USD, dominating any mcap-weighted basket.

    Johannesburg .JO tickers have the same quirk: priced in ZAc (rand cents =
    1/100 ZAR) while currency is reported as ZAR. Same /100 normalization
    (Naspers, MTN, Standard Bank, Shoprite…)."""
    if symbol and (symbol.endswith(".L") or symbol.endswith(".JO")):
        return price_local / 100.0
    return price_local


def apply_fx(price_local, currency, fx_rates, date_str=None, symbol=None):
    """Convert local-currency price to USD using the rate on a given date
    (or the latest available if date not found). When `symbol` is provided,
    Yahoo's pence-quoting for London .L tickers is normalized first.

    Returns None when the FX rate is missing for a non-USD currency. This is
    a behavioural change from the previous silent passthrough: callers must
    treat None as "exclude this stock from aggregates" rather than mixing
    raw local-currency values into mcap-weighted means (the bug that made
    BBCA.JK contribute 731 113 B$ to Banques Asie)."""
    price_local = normalize_local_price(price_local, symbol)
    if currency == "USD" or not currency:
        return price_local
    per_day = fx_rates.get(currency)
    if not per_day:
        # Loud first time per currency, silent thereafter to avoid log spam.
        if currency not in _FX_MISSING_LOGGED:
            _FX_MISSING_LOGGED.add(currency)
            print(f"[FX_MISSING] {currency} not in fx_rates → returning None for {symbol or '?'} (excluded from aggregates)", file=sys.stderr)
        return None
    rate = per_day.get(date_str) if date_str else None
    if rate is None:
        # Find nearest prior available date
        sorted_days = sorted(per_day.keys())
        best = None
        for d in sorted_days:
            if date_str is None or d <= date_str:
                best = d
            else:
                break
        if best is None and sorted_days:
            # date_str is OLDER than all available FX rates — use the OLDEST
            # available rate as a best-effort fallback. This is way better
            # than returning raw local currency (which would silently leave
            # KRW/JPY values dominating mcap-weight calculations).
            best = sorted_days[0]
        if best is not None:
            rate = per_day[best]
    if rate is None or rate <= 0:
        return price_local
    return price_local * rate


# ─────────────────────────────────────────────────────────────────────────
# STOCK METADATA : nom + domaine (pour favicon) + exchange label
# Yahoo Finance symbols with exchange suffixes:
#   .L  London       .PA Paris      .DE XETRA (Germany)     .AS Amsterdam
#   .SW Swiss        .MI Milan      .MC Madrid              .CO Copenhagen
#   .T  Tokyo        .HK Hong Kong  .NS NSE India           .SS Shanghai
#   .TO Toronto      .ST Stockholm  .OL Oslo                .AX Australia
# ─────────────────────────────────────────────────────────────────────────
STOCKS = {
    # ──── US Tech / Mag 7 ────────────────────────────────────────────
    "AAPL":   {"name": "Apple",               "domain": "apple.com",       "x": "NASDAQ", "region": "US"},
    "MSFT":   {"name": "Microsoft",           "domain": "microsoft.com",   "x": "NASDAQ", "region": "US"},
    "GOOGL":  {"name": "Alphabet (Google)",   "domain": "abc.xyz",         "x": "NASDAQ", "region": "US"},
    "AMZN":   {"name": "Amazon",              "domain": "amazon.com",      "x": "NASDAQ", "region": "US"},
    "META":   {"name": "Meta Platforms",      "domain": "meta.com",        "x": "NASDAQ", "region": "US"},
    "NVDA":   {"name": "NVIDIA",              "domain": "nvidia.com",      "x": "NASDAQ", "region": "US"},
    "TSLA":   {"name": "Tesla",               "domain": "tesla.com",       "x": "NASDAQ", "region": "US"},
    # ──── AI & Semi ─────────────────────────────────────────────────
    "AMD":    {"name": "AMD",                 "domain": "amd.com",         "x": "NASDAQ", "region": "US"},
    "AVGO":   {"name": "Broadcom",            "domain": "broadcom.com",    "x": "NASDAQ", "region": "US"},
    "INTC":   {"name": "Intel",               "domain": "intel.com",       "x": "NASDAQ", "region": "US"},
    "MU":     {"name": "Micron",              "domain": "micron.com",      "x": "NASDAQ", "region": "US"},
    "QCOM":   {"name": "Qualcomm",            "domain": "qualcomm.com",    "x": "NASDAQ", "region": "US"},
    "ARM":    {"name": "Arm Holdings",        "domain": "arm.com",         "x": "NASDAQ (ADR)", "region": "UK"},
    "TSM":    {"name": "TSMC",                "domain": "tsmc.com",        "x": "NYSE (ADR)", "region": "Korea_Taiwan"},
    "ASML.AS":{"name": "ASML Holding",        "domain": "asml.com",        "x": "Amsterdam", "region": "Europe"},
    "SMCI":   {"name": "Super Micro",         "domain": "supermicro.com",  "x": "NASDAQ", "region": "US"},
    # ──── Cloud / SaaS ───────────────────────────────────────────────
    "CRM":    {"name": "Salesforce",          "domain": "salesforce.com",  "x": "NYSE", "region": "US"},
    "ORCL":   {"name": "Oracle",              "domain": "oracle.com",      "x": "NYSE", "region": "US"},
    "NOW":    {"name": "ServiceNow",          "domain": "servicenow.com",  "x": "NYSE", "region": "US"},
    "ADBE":   {"name": "Adobe",               "domain": "adobe.com",       "x": "NASDAQ", "region": "US"},
    "SNOW":   {"name": "Snowflake",           "domain": "snowflake.com",   "x": "NYSE", "region": "US"},
    "DDOG":   {"name": "Datadog",             "domain": "datadoghq.com",   "x": "NASDAQ", "region": "US"},
    "SAP.DE": {"name": "SAP",                 "domain": "sap.com",         "x": "XETRA", "region": "Europe"},
    "MDB":    {"name": "MongoDB",             "domain": "mongodb.com",     "x": "NASDAQ", "region": "US"},
    # ──── Cybersec ───────────────────────────────────────────────────
    "PANW":   {"name": "Palo Alto Networks",  "domain": "paloaltonetworks.com","x": "NASDAQ", "region": "US"},
    "CRWD":   {"name": "CrowdStrike",         "domain": "crowdstrike.com", "x": "NASDAQ", "region": "US"},
    "NET":    {"name": "Cloudflare",          "domain": "cloudflare.com",  "x": "NYSE", "region": "US"},
    "FTNT":   {"name": "Fortinet",            "domain": "fortinet.com",    "x": "NASDAQ", "region": "US"},
    "ZS":     {"name": "Zscaler",             "domain": "zscaler.com",     "x": "NASDAQ", "region": "US"},
    "OKTA":   {"name": "Okta",                "domain": "okta.com",        "x": "NASDAQ", "region": "US"},
    # ──── US Banks ───────────────────────────────────────────────────
    "JPM":    {"name": "JPMorgan Chase",      "domain": "jpmorganchase.com","x": "NYSE", "region": "US"},
    "BAC":    {"name": "Bank of America",     "domain": "bankofamerica.com","x": "NYSE", "region": "US"},
    "WFC":    {"name": "Wells Fargo",         "domain": "wellsfargo.com",  "x": "NYSE", "region": "US"},
    "C":      {"name": "Citigroup",           "domain": "citigroup.com",   "x": "NYSE", "region": "US"},
    "GS":     {"name": "Goldman Sachs",       "domain": "goldmansachs.com","x": "NYSE", "region": "US"},
    "MS":     {"name": "Morgan Stanley",      "domain": "morganstanley.com","x": "NYSE", "region": "US"},
    "USB":    {"name": "US Bancorp",          "domain": "usbank.com",      "x": "NYSE", "region": "US"},
    # ──── European Banks ─────────────────────────────────────────────
    "HSBA.L": {"name": "HSBC",                "domain": "hsbc.com",        "x": "London", "region": "UK"},
    "BNP.PA": {"name": "BNP Paribas",         "domain": "bnpparibas.com",  "x": "Paris", "region": "Europe"},
    "DBK.DE": {"name": "Deutsche Bank",       "domain": "db.com",          "x": "XETRA", "region": "Europe"},
    "UBSG.SW":{"name": "UBS Group",           "domain": "ubs.com",         "x": "Swiss", "region": "Europe"},
    "SAN.MC": {"name": "Banco Santander",     "domain": "santander.com",   "x": "Madrid", "region": "Europe"},
    "BARC.L": {"name": "Barclays",            "domain": "barclays.com",    "x": "London", "region": "UK"},
    "ISP.MI": {"name": "Intesa Sanpaolo",     "domain": "intesasanpaolo.com","x": "Milan", "region": "Europe"},
    # ──── Insurance ──────────────────────────────────────────────────
    "BRK-B":  {"name": "Berkshire Hathaway",  "domain": "berkshirehathaway.com","x": "NYSE", "region": "US"},
    "AIG":    {"name": "AIG",                 "domain": "aig.com",         "x": "NYSE", "region": "US"},
    "ALV.DE": {"name": "Allianz",             "domain": "allianz.com",     "x": "XETRA", "region": "Europe"},
    "CS.PA":  {"name": "AXA",                 "domain": "axa.com",         "x": "Paris", "region": "Europe"},
    "PRU.L":  {"name": "Prudential plc",      "domain": "prudentialplc.com","x": "London", "region": "UK"},
    "MUV2.DE":{"name": "Munich Re",           "domain": "munichre.com",    "x": "XETRA", "region": "Europe"},
    # ──── Asset Managers & Exchanges ────────────────────────────────
    "BLK":    {"name": "BlackRock",           "domain": "blackrock.com",   "x": "NYSE", "region": "US"},
    "BX":     {"name": "Blackstone",          "domain": "blackstone.com",  "x": "NYSE", "region": "US"},
    "KKR":    {"name": "KKR",                 "domain": "kkr.com",         "x": "NYSE", "region": "US"},
    "ICE":    {"name": "Intercontinental Ex.","domain": "ice.com",         "x": "NYSE", "region": "US"},
    "CME":    {"name": "CME Group",           "domain": "cmegroup.com",    "x": "NASDAQ", "region": "US"},
    # ──── Defense & Aerospace ───────────────────────────────────────
    "LMT":    {"name": "Lockheed Martin",     "domain": "lockheedmartin.com","x": "NYSE", "region": "US"},
    "RTX":    {"name": "RTX Corp",            "domain": "rtx.com",         "x": "NYSE", "region": "US"},
    "GD":     {"name": "General Dynamics",    "domain": "gd.com",          "x": "NYSE", "region": "US"},
    "NOC":    {"name": "Northrop Grumman",    "domain": "northropgrumman.com","x": "NYSE", "region": "US"},
    "BA":     {"name": "Boeing",              "domain": "boeing.com",      "x": "NYSE", "region": "US"},
    "AIR.PA": {"name": "Airbus",              "domain": "airbus.com",      "x": "Paris", "region": "Europe"},
    "RHM.DE": {"name": "Rheinmetall",         "domain": "rheinmetall.com", "x": "XETRA", "region": "Europe"},
    "BA.L":   {"name": "BAE Systems",         "domain": "baesystems.com",  "x": "London", "region": "UK"},
    "SAF.PA": {"name": "Safran",              "domain": "safran-group.com","x": "Paris", "region": "Europe"},
    # ──── Oil & Gas Majors ──────────────────────────────────────────
    "XOM":    {"name": "ExxonMobil",          "domain": "exxonmobil.com",  "x": "NYSE", "region": "US"},
    "CVX":    {"name": "Chevron",             "domain": "chevron.com",     "x": "NYSE", "region": "US"},
    "SHEL":   {"name": "Shell",               "domain": "shell.com",       "x": "NYSE (ADR)", "region": "UK"},
    "TTE.PA": {"name": "TotalEnergies",       "domain": "totalenergies.com","x": "Paris", "region": "Europe"},
    "BP.L":   {"name": "BP",                  "domain": "bp.com",          "x": "London", "region": "UK"},
    "EQNR":   {"name": "Equinor",             "domain": "equinor.com",     "x": "NYSE (ADR)", "region": "Europe"},
    "COP":    {"name": "ConocoPhillips",      "domain": "conocophillips.com","x": "NYSE", "region": "US"},
    # ──── Clean Energy ──────────────────────────────────────────────
    "NEE":    {"name": "NextEra Energy",      "domain": "nexteraenergy.com","x": "NYSE", "region": "US"},
    "FSLR":   {"name": "First Solar",         "domain": "firstsolar.com",  "x": "NASDAQ", "region": "US"},
    "ENPH":   {"name": "Enphase Energy",      "domain": "enphase.com",     "x": "NASDAQ", "region": "US"},
    "ORSTED.CO":{"name": "Ørsted",            "domain": "orsted.com",      "x": "Copenhagen", "region": "Europe"},
    # ──── Pharma ────────────────────────────────────────────────────
    "LLY":    {"name": "Eli Lilly",           "domain": "lilly.com",       "x": "NYSE", "region": "US"},
    "NVO":    {"name": "Novo Nordisk",        "domain": "novonordisk.com", "x": "NYSE (ADR)", "region": "Europe"},
    "JNJ":    {"name": "Johnson & Johnson",   "domain": "jnj.com",         "x": "NYSE", "region": "US"},
    "MRK":    {"name": "Merck",               "domain": "merck.com",       "x": "NYSE", "region": "US"},
    "PFE":    {"name": "Pfizer",              "domain": "pfizer.com",      "x": "NYSE", "region": "US"},
    "AZN.L":  {"name": "AstraZeneca",         "domain": "astrazeneca.com", "x": "London", "region": "UK"},
    "ROG.SW": {"name": "Roche Holding",       "domain": "roche.com",       "x": "Swiss", "region": "Europe"},
    "NOVN.SW":{"name": "Novartis",            "domain": "novartis.com",    "x": "Swiss", "region": "Europe"},
    "ABBV":   {"name": "AbbVie",              "domain": "abbvie.com",      "x": "NYSE", "region": "US"},
    # ──── Luxury Europe ─────────────────────────────────────────────
    "MC.PA":  {"name": "LVMH",                "domain": "lvmh.com",        "x": "Paris", "region": "Europe"},
    "KER.PA": {"name": "Kering",              "domain": "kering.com",      "x": "Paris", "region": "Europe"},
    "RMS.PA": {"name": "Hermès",              "domain": "hermes.com",      "x": "Paris", "region": "Europe"},
    "CFR.SW": {"name": "Richemont",           "domain": "richemont.com",   "x": "Swiss", "region": "Europe"},
    "OR.PA":  {"name": "L'Oréal",             "domain": "loreal.com",      "x": "Paris", "region": "Europe"},
    # ──── Consumer Defensive ────────────────────────────────────────
    "PG":     {"name": "Procter & Gamble",    "domain": "pg.com",          "x": "NYSE", "region": "US"},
    "KO":     {"name": "Coca-Cola",           "domain": "coca-colacompany.com","x": "NYSE", "region": "US"},
    "PEP":    {"name": "PepsiCo",             "domain": "pepsico.com",     "x": "NASDAQ", "region": "US"},
    "WMT":    {"name": "Walmart",             "domain": "walmart.com",     "x": "NYSE", "region": "US"},
    "COST":   {"name": "Costco",              "domain": "costco.com",      "x": "NASDAQ", "region": "US"},
    "NESN.SW":{"name": "Nestlé",              "domain": "nestle.com",      "x": "Swiss", "region": "Europe"},
    "UNA.AS": {"name": "Unilever",            "domain": "unilever.com",    "x": "Amsterdam", "region": "Europe"},
    # ──── EV & Auto ──────────────────────────────────────────────────
    "NIO":    {"name": "NIO",                 "domain": "nio.com",         "x": "NYSE", "region": "China"},
    "RIVN":   {"name": "Rivian",              "domain": "rivian.com",      "x": "NASDAQ", "region": "US"},
    "LCID":   {"name": "Lucid Motors",        "domain": "lucidmotors.com", "x": "NASDAQ", "region": "US"},
    "F":      {"name": "Ford",                "domain": "ford.com",        "x": "NYSE", "region": "US"},
    "GM":     {"name": "General Motors",      "domain": "gm.com",          "x": "NYSE", "region": "US"},
    "TM":     {"name": "Toyota",              "domain": "toyota.com",      "x": "NYSE (ADR)", "region": "Japan"},
    "VOW3.DE":{"name": "Volkswagen",          "domain": "volkswagen-group.com","x": "XETRA", "region": "Europe"},
    "RACE":   {"name": "Ferrari",             "domain": "ferrari.com",     "x": "NYSE", "region": "Europe"},
    "MBG.DE": {"name": "Mercedes-Benz",       "domain": "mercedes-benz.com","x": "XETRA", "region": "Europe"},
    "P911.DE":{"name": "Porsche AG",          "domain": "porsche.com",     "x": "XETRA", "region": "Europe"},
    # ──── Japan ──────────────────────────────────────────────────────
    "6758.T": {"name": "Sony Group",          "domain": "sony.com",        "x": "Tokyo", "region": "Japan"},
    "7203.T": {"name": "Toyota Motor (Tokyo)","domain": "toyota.com",      "x": "Tokyo", "region": "Japan"},
    "9984.T": {"name": "SoftBank Group",      "domain": "softbank.jp",     "x": "Tokyo", "region": "Japan"},
    "9983.T": {"name": "Fast Retailing (Uniqlo)","domain": "fastretailing.com","x": "Tokyo", "region": "Japan"},
    "6861.T": {"name": "Keyence",             "domain": "keyence.com",     "x": "Tokyo", "region": "Japan"},
    "8306.T": {"name": "Mitsubishi UFJ",      "domain": "mufg.jp",         "x": "Tokyo", "region": "Japan"},
    # ──── China / Hong Kong ─────────────────────────────────────────
    "BABA":   {"name": "Alibaba (ADR)",       "domain": "alibabagroup.com","x": "NYSE (ADR)", "region": "China"},
    "PDD":    {"name": "PDD Holdings",        "domain": "pddholdings.com", "x": "NASDAQ", "region": "China"},
    "JD":     {"name": "JD.com",              "domain": "jd.com",          "x": "NASDAQ", "region": "China"},
    "0700.HK":{"name": "Tencent",             "domain": "tencent.com",     "x": "Hong Kong", "region": "China"},
    "9988.HK":{"name": "Alibaba (HK)",        "domain": "alibabagroup.com","x": "Hong Kong", "region": "China"},
    "1398.HK":{"name": "ICBC",                "domain": "icbc.com.cn",     "x": "Hong Kong", "region": "China"},
    "3690.HK":{"name": "Meituan",             "domain": "meituan.com",     "x": "Hong Kong", "region": "China"},
    "1211.HK":{"name": "BYD Company",         "domain": "byd.com",         "x": "Hong Kong", "region": "China"},
    # ──── India ─────────────────────────────────────────────────────
    "RELIANCE.NS":{"name": "Reliance Industries","domain": "ril.com",      "x": "NSE", "region": "India"},
    "TCS.NS": {"name": "Tata Consultancy",    "domain": "tcs.com",         "x": "NSE", "region": "India"},
    "INFY":   {"name": "Infosys (ADR)",       "domain": "infosys.com",     "x": "NYSE (ADR)", "region": "India"},
    "HDFCBANK.NS":{"name":"HDFC Bank",        "domain": "hdfcbank.com",    "x": "NSE", "region": "India"},
    # ──── Gold Miners ───────────────────────────────────────────────
    "NEM":    {"name": "Newmont",             "domain": "newmont.com",     "x": "NYSE", "region": "US"},
    "GOLD":   {"name": "Barrick Gold",        "domain": "barrick.com",     "x": "NYSE (ADR)", "region": "Canada"},
    "AEM":    {"name": "Agnico Eagle",        "domain": "agnicoeagle.com", "x": "NYSE (ADR)", "region": "Canada"},
    "FNV":    {"name": "Franco-Nevada",       "domain": "franco-nevada.com","x": "NYSE (ADR)", "region": "Canada"},
    "WPM":    {"name": "Wheaton Precious Metals","domain": "wheatonpm.com","x": "NYSE (ADR)", "region": "Canada"},
    "KGC":    {"name": "Kinross Gold",        "domain": "kinross.com",     "x": "NYSE (ADR)", "region": "Canada"},
    "EVN.AX": {"name": "Evolution Mining",    "domain": "evolutionmining.com.au","x": "ASX", "region": "Australia"},
    "GFI":    {"name": "Gold Fields",         "domain": "goldfields.com",  "x": "NYSE (ADR)", "region": "Afrique"},
    "AU":     {"name": "AngloGold Ashanti",   "domain": "anglogoldashanti.com","x": "NYSE (ADR)", "region": "Afrique"},
    # ──── REITs ─────────────────────────────────────────────────────
    "O":      {"name": "Realty Income",       "domain": "realtyincome.com","x": "NYSE", "region": "US"},
    "PLD":    {"name": "Prologis",            "domain": "prologis.com",    "x": "NYSE", "region": "US"},
    "AMT":    {"name": "American Tower",      "domain": "americantower.com","x": "NYSE", "region": "US"},
    "EQIX":   {"name": "Equinix",             "domain": "equinix.com",     "x": "NASDAQ", "region": "US"},
    "SPG":    {"name": "Simon Property",      "domain": "simon.com",       "x": "NYSE", "region": "US"},
    # ──── Media & Streaming ─────────────────────────────────────────
    "NFLX":   {"name": "Netflix",             "domain": "netflix.com",     "x": "NASDAQ", "region": "US"},
    "DIS":    {"name": "Walt Disney",         "domain": "thewaltdisneycompany.com","x": "NYSE", "region": "US"},
    "WBD":    {"name": "Warner Bros Discovery","domain": "wbd.com",        "x": "NASDAQ", "region": "US"},
    "CMCSA":  {"name": "Comcast",             "domain": "comcast.com",     "x": "NASDAQ", "region": "US"},
    "SPOT":   {"name": "Spotify",             "domain": "spotify.com",     "x": "NYSE", "region": "Europe"},
    # ──── Retail & E-commerce (non-Mag7) ────────────────────────────
    "SHOP":   {"name": "Shopify",             "domain": "shopify.com",     "x": "NYSE", "region": "Canada"},
    "MELI":   {"name": "MercadoLibre",        "domain": "mercadolibre.com","x": "NASDAQ", "region": "LatAm"},
    "SE":     {"name": "Sea Limited",         "domain": "sea.com",         "x": "NYSE", "region": "ASEAN"},
    # ──── Industrials (beyond defense) ──────────────────────────────
    "GE":     {"name": "GE Aerospace",        "domain": "geaerospace.com", "x": "NYSE", "region": "US"},
    "HON":    {"name": "Honeywell",           "domain": "honeywell.com",   "x": "NYSE", "region": "US"},
    "CAT":    {"name": "Caterpillar",         "domain": "caterpillar.com", "x": "NYSE", "region": "US"},
    "DE":     {"name": "John Deere",          "domain": "deere.com",       "x": "NYSE", "region": "US"},
    "SIE.DE": {"name": "Siemens",             "domain": "siemens.com",     "x": "XETRA", "region": "Europe"},
    # ──── Fintech & Payments ─────────────────────────────────────────
    "V":      {"name": "Visa",                "domain": "visa.com",        "x": "NYSE", "region": "US"},
    "MA":     {"name": "Mastercard",          "domain": "mastercard.com",  "x": "NYSE", "region": "US"},
    "ADYEN.AS":{"name":"Adyen",               "domain": "adyen.com",       "x": "Amsterdam", "region": "Europe"},
    "NU":     {"name": "Nu Holdings",         "domain": "nu.com.br",       "x": "NYSE", "region": "LatAm"},
    "SOFI":   {"name": "SoFi Technologies",   "domain": "sofi.com",        "x": "NASDAQ", "region": "US"},
    "AFRM":   {"name": "Affirm",              "domain": "affirm.com",      "x": "NASDAQ", "region": "US"},
    "FI":     {"name": "Fiserv",              "domain": "fiserv.com",      "x": "NYSE", "region": "US"},
    "WISE.L": {"name": "Wise",                "domain": "wise.com",        "x": "London", "region": "UK"},
    # ──── Biotech ────────────────────────────────────────────────────
    "AMGN":   {"name": "Amgen",               "domain": "amgen.com",       "x": "NASDAQ", "region": "US"},
    "GILD":   {"name": "Gilead Sciences",     "domain": "gilead.com",      "x": "NASDAQ", "region": "US"},
    "REGN":   {"name": "Regeneron",           "domain": "regeneron.com",   "x": "NASDAQ", "region": "US"},
    "VRTX":   {"name": "Vertex Pharmaceuticals","domain": "vrtx.com",      "x": "NASDAQ", "region": "US"},
    "MRNA":   {"name": "Moderna",             "domain": "modernatx.com",   "x": "NASDAQ", "region": "US"},
    "BNTX":   {"name": "BioNTech (ADR)",      "domain": "biontech.com",    "x": "NASDAQ (ADR)", "region": "Europe"},
    "ILMN":   {"name": "Illumina",            "domain": "illumina.com",    "x": "NASDAQ", "region": "US"},
    "BIIB":   {"name": "Biogen",              "domain": "biogen.com",      "x": "NASDAQ", "region": "US"},
    # ──── Utilities ──────────────────────────────────────────────────
    "DUK":    {"name": "Duke Energy",         "domain": "duke-energy.com", "x": "NYSE", "region": "US"},
    "SO":     {"name": "Southern Company",    "domain": "southerncompany.com","x": "NYSE", "region": "US"},
    "AEP":    {"name": "American Electric Power","domain": "aep.com",      "x": "NASDAQ", "region": "US"},
    "D":      {"name": "Dominion Energy",     "domain": "dominionenergy.com","x": "NYSE", "region": "US"},
    "ENEL.MI":{"name": "Enel",                "domain": "enel.com",        "x": "Milan", "region": "Europe"},
    "IBE.MC": {"name": "Iberdrola",           "domain": "iberdrola.com",   "x": "Madrid", "region": "Europe"},
    "NG.L":   {"name": "National Grid",       "domain": "nationalgrid.com","x": "London", "region": "UK"},
    "ENGI.PA":{"name": "Engie",               "domain": "engie.com",       "x": "Paris", "region": "Europe"},
    # ──── Telecoms ──────────────────────────────────────────────────
    "VZ":     {"name": "Verizon",             "domain": "verizon.com",     "x": "NYSE", "region": "US"},
    "T":      {"name": "AT&T",                "domain": "att.com",         "x": "NYSE", "region": "US"},
    "TMUS":   {"name": "T-Mobile US",         "domain": "t-mobile.com",    "x": "NASDAQ", "region": "US"},
    "VOD.L":  {"name": "Vodafone",            "domain": "vodafone.com",    "x": "London", "region": "UK"},
    "DTE.DE": {"name": "Deutsche Telekom",    "domain": "telekom.com",     "x": "XETRA", "region": "Europe"},
    "ORAN":   {"name": "Orange (ADR)",        "domain": "orange.com",      "x": "NYSE (ADR)", "region": "Europe"},
    "NTT":    {"name": "NTT (ADR)",           "domain": "global.ntt",      "x": "NYSE (ADR)", "region": "Japan"},
    "BT-A.L": {"name": "BT Group",            "domain": "bt.com",          "x": "London", "region": "UK"},
    # ──── Travel & Hospitality ──────────────────────────────────────
    "BKNG":   {"name": "Booking Holdings",    "domain": "bookingholdings.com","x": "NASDAQ", "region": "US"},
    "MAR":    {"name": "Marriott Intl.",      "domain": "marriott.com",    "x": "NASDAQ", "region": "US"},
    "HLT":    {"name": "Hilton Worldwide",    "domain": "hilton.com",      "x": "NYSE", "region": "US"},
    "ABNB":   {"name": "Airbnb",              "domain": "airbnb.com",      "x": "NASDAQ", "region": "US"},
    "DAL":    {"name": "Delta Air Lines",     "domain": "delta.com",       "x": "NYSE", "region": "US"},
    "UAL":    {"name": "United Airlines",     "domain": "united.com",      "x": "NASDAQ", "region": "US"},
    "LUV":    {"name": "Southwest Airlines",  "domain": "southwest.com",   "x": "NYSE", "region": "US"},
    "RYAAY":  {"name": "Ryanair (ADR)",       "domain": "ryanair.com",     "x": "NASDAQ (ADR)", "region": "Europe"},
    "LHA.DE": {"name": "Lufthansa",           "domain": "lufthansagroup.com","x": "XETRA", "region": "Europe"},
    "AF.PA":  {"name": "Air France-KLM",      "domain": "airfranceklm.com","x": "Paris", "region": "Europe"},
    # ──── Materials & Mining (non-gold) ─────────────────────────────
    "BHP":    {"name": "BHP Group",           "domain": "bhp.com",         "x": "NYSE (ADR)", "region": "Australia"},
    "RIO":    {"name": "Rio Tinto",           "domain": "riotinto.com",    "x": "NYSE (ADR)", "region": "UK"},
    "VALE":   {"name": "Vale S.A.",           "domain": "vale.com",        "x": "NYSE (ADR)", "region": "LatAm"},
    "FCX":    {"name": "Freeport-McMoRan",    "domain": "fcx.com",         "x": "NYSE", "region": "US"},
    "ALB":    {"name": "Albemarle (lithium)", "domain": "albemarle.com",   "x": "NYSE", "region": "US"},
    "SQM":    {"name": "SQM (lithium Chile)", "domain": "sqm.com",         "x": "NYSE (ADR)", "region": "US"},
    "MT":     {"name": "ArcelorMittal",       "domain": "arcelormittal.com","x": "NYSE (ADR)", "region": "Europe"},
    "NUE":    {"name": "Nucor",               "domain": "nucor.com",       "x": "NYSE", "region": "US"},
    # ──── Consommation défensive — enrichi ──────────────────────────
    "CL":     {"name": "Colgate-Palmolive",   "domain": "colgatepalmolive.com","x": "NYSE", "region": "US"},
    "KMB":    {"name": "Kimberly-Clark",      "domain": "kimberly-clark.com","x": "NYSE", "region": "US"},
    "MDLZ":   {"name": "Mondelēz",            "domain": "mondelezinternational.com","x": "NASDAQ", "region": "US"},
    "GIS":    {"name": "General Mills",       "domain": "generalmills.com","x": "NYSE", "region": "US"},
    # ──── Défense Europe — enrichi ───────────────────────────────────
    "LDO.MI": {"name": "Leonardo",            "domain": "leonardo.com",    "x": "Milan", "region": "Europe"},
    "AM.PA":  {"name": "Dassault Aviation",   "domain": "dassault-aviation.com","x": "Paris", "region": "Europe"},
    "HO.PA":  {"name": "Thales",              "domain": "thalesgroup.com", "x": "Paris", "region": "Europe"},
    # ──── Énergie propre — enrichi ───────────────────────────────────
    "SEDG":   {"name": "SolarEdge",           "domain": "solaredge.com",   "x": "NASDAQ", "region": "US"},
    "PLUG":   {"name": "Plug Power",          "domain": "plugpower.com",   "x": "NASDAQ", "region": "US"},
    "BE":     {"name": "Bloom Energy",        "domain": "bloomenergy.com", "x": "NYSE", "region": "US"},
    "VWS.CO": {"name": "Vestas Wind Systems", "domain": "vestas.com",      "x": "Copenhagen", "region": "Europe"},
    # ──── Semi/AI additional ─────────────────────────────────────────
    "005930.KS":{"name":"Samsung Electronics","domain": "samsung.com",     "x": "Seoul", "region": "Korea_Taiwan"},
    "STM":    {"name": "STMicroelectronics",  "domain": "st.com",          "x": "NYSE (ADR)", "region": "Europe"},
    "MRVL":   {"name": "Marvell Technology",  "domain": "marvell.com",     "x": "NASDAQ", "region": "US"},
    "ADI":    {"name": "Analog Devices",      "domain": "analog.com",      "x": "NASDAQ", "region": "US"},
    "NXPI":   {"name": "NXP Semiconductors",  "domain": "nxp.com",         "x": "NASDAQ", "region": "Europe"},
    "ANET":   {"name": "Arista Networks",     "domain": "arista.com",      "x": "NYSE", "region": "US"},
    "VRT":    {"name": "Vertiv (AI cooling)", "domain": "vertiv.com",      "x": "NYSE", "region": "US"},
    "DELL":   {"name": "Dell (AI servers)",   "domain": "dell.com",        "x": "NYSE", "region": "US"},
    # ──── Cloud/SaaS additional ──────────────────────────────────────
    "IBM":    {"name": "IBM (hybrid cloud)",  "domain": "ibm.com",         "x": "NYSE", "region": "US"},
    "WDAY":   {"name": "Workday",             "domain": "workday.com",     "x": "NASDAQ", "region": "US"},
    "TEAM":   {"name": "Atlassian",           "domain": "atlassian.com",   "x": "NASDAQ", "region": "US"},
    "TWLO":   {"name": "Twilio",              "domain": "twilio.com",      "x": "NYSE", "region": "US"},
    "SNPS":   {"name": "Synopsys",            "domain": "synopsys.com",    "x": "NASDAQ", "region": "US"},
    "CDNS":   {"name": "Cadence Design",      "domain": "cadence.com",     "x": "NASDAQ", "region": "US"},
    # ──── Cybersecurity additional ──────────────────────────────────
    "S":      {"name": "SentinelOne",         "domain": "sentinelone.com", "x": "NYSE", "region": "US"},
    "CYBR":   {"name": "CyberArk",            "domain": "cyberark.com",    "x": "NASDAQ", "region": "US"},
    # ──── US Banks additional ────────────────────────────────────────
    "PNC":    {"name": "PNC Financial",       "domain": "pnc.com",         "x": "NYSE", "region": "US"},
    "COF":    {"name": "Capital One",         "domain": "capitalone.com",  "x": "NYSE", "region": "US"},
    "SCHW":   {"name": "Charles Schwab",      "domain": "schwab.com",      "x": "NYSE", "region": "US"},
    # ──── Banques Europe additional ─────────────────────────────────
    "GLE.PA": {"name": "Société Générale",    "domain": "societegenerale.com","x": "Paris", "region": "Europe"},
    "ACA.PA": {"name": "Crédit Agricole",     "domain": "credit-agricole.com","x": "Paris", "region": "Europe"},
    "INGA.AS":{"name": "ING Group",           "domain": "ing.com",         "x": "Amsterdam", "region": "Europe"},
    # ──── Assurance additional ──────────────────────────────────────
    "MET":    {"name": "MetLife",             "domain": "metlife.com",     "x": "NYSE", "region": "US"},
    "TRV":    {"name": "Travelers",           "domain": "travelers.com",   "x": "NYSE", "region": "US"},
    "CB":     {"name": "Chubb",               "domain": "chubb.com",       "x": "NYSE", "region": "US"},
    "ZURN.SW":{"name": "Zurich Insurance",    "domain": "zurich.com",      "x": "Swiss", "region": "Europe"},
    "AV.L":   {"name": "Aviva",               "domain": "aviva.com",       "x": "London", "region": "UK"},
    # ──── Exchanges additional ──────────────────────────────────────
    "DB1.DE": {"name": "Deutsche Börse",      "domain": "deutsche-boerse.com","x": "XETRA", "region": "Europe"},
    "LSEG.L": {"name": "LSE Group",           "domain": "lseg.com",        "x": "London", "region": "UK"},
    "NDAQ":   {"name": "Nasdaq Inc.",         "domain": "nasdaq.com",      "x": "NASDAQ", "region": "US"},
    # ──── Defense additional ────────────────────────────────────────
    "HII":    {"name": "Huntington Ingalls",  "domain": "hii.com",         "x": "NYSE", "region": "US"},
    "HEI":    {"name": "HEICO (aviation parts)","domain": "heico.com",     "x": "NYSE", "region": "US"},
    # ──── Oil Services + independents ───────────────────────────────
    "EOG":    {"name": "EOG Resources",       "domain": "eogresources.com","x": "NYSE", "region": "US"},
    "MPC":    {"name": "Marathon Petroleum",  "domain": "marathonpetroleum.com","x": "NYSE", "region": "US"},
    "PSX":    {"name": "Phillips 66",         "domain": "phillips66.com",  "x": "NYSE", "region": "US"},
    "VLO":    {"name": "Valero",              "domain": "valero.com",      "x": "NYSE", "region": "US"},
    "SLB":    {"name": "Schlumberger",        "domain": "slb.com",         "x": "NYSE", "region": "US"},
    "HAL":    {"name": "Halliburton",         "domain": "halliburton.com", "x": "NYSE", "region": "US"},
    # ──── Pharma additional ─────────────────────────────────────────
    "BMY":    {"name": "Bristol-Myers Squibb","domain": "bms.com",         "x": "NYSE", "region": "US"},
    "GSK.L":  {"name": "GSK",                 "domain": "gsk.com",         "x": "London", "region": "UK"},
    "SAN.PA": {"name": "Sanofi",              "domain": "sanofi.com",      "x": "Paris", "region": "Europe"},
    # ──── Luxury additional ─────────────────────────────────────────
    "BRBY.L": {"name": "Burberry",            "domain": "burberry.com",    "x": "London", "region": "UK"},
    "EL":     {"name": "Estée Lauder",        "domain": "elcompanies.com", "x": "NYSE", "region": "US"},
    # ──── Auto additional ───────────────────────────────────────────
    "STLA":   {"name": "Stellantis",          "domain": "stellantis.com",  "x": "NYSE", "region": "Europe"},
    "HMC":    {"name": "Honda Motor (ADR)",   "domain": "honda.com",       "x": "NYSE (ADR)", "region": "Japan"},
    "XPEV":   {"name": "XPeng",               "domain": "xiaopeng.com",    "x": "NYSE (ADR)", "region": "China"},
    "LI":     {"name": "Li Auto",             "domain": "lixiang.com",     "x": "NASDAQ (ADR)", "region": "China"},
    # ──── Japan additional mega caps ────────────────────────────────
    "8058.T": {"name": "Mitsubishi Corp.",    "domain": "mitsubishicorp.com","x": "Tokyo", "region": "Japan"},
    "8316.T": {"name": "Sumitomo Mitsui FG",  "domain": "smfg.co.jp",      "x": "Tokyo", "region": "Japan"},
    "6501.T": {"name": "Hitachi",             "domain": "hitachi.com",     "x": "Tokyo", "region": "Japan"},
    "4063.T": {"name": "Shin-Etsu Chemical",  "domain": "shinetsu.co.jp",  "x": "Tokyo", "region": "Japan"},
    "6098.T": {"name": "Recruit Holdings",    "domain": "recruit-holdings.com","x": "Tokyo", "region": "Japan"},
    # ──── China/HK additional ───────────────────────────────────────
    "2318.HK":{"name": "Ping An Insurance",   "domain": "pingan.com",      "x": "Hong Kong", "region": "China"},
    "0941.HK":{"name": "China Mobile",        "domain": "chinamobile.com", "x": "Hong Kong", "region": "China"},
    "0939.HK":{"name": "CCB (Construction Bk)","domain": "ccb.com",        "x": "Hong Kong", "region": "China"},
    # ──── India additional ──────────────────────────────────────────
    "ICICIBANK.NS":{"name":"ICICI Bank",      "domain": "icicibank.com",   "x": "NSE", "region": "India"},
    "BHARTIARTL.NS":{"name":"Bharti Airtel",  "domain": "airtel.in",       "x": "NSE", "region": "India"},
    "SBIN.NS": {"name": "State Bank of India","domain": "sbi.co.in",       "x": "NSE", "region": "India"},
    "HINDUNILVR.NS":{"name":"Hindustan Unilever","domain": "hul.co.in",    "x": "NSE", "region": "India"},
    # ──── Industrials additional ────────────────────────────────────
    "MMM":    {"name": "3M",                  "domain": "3m.com",          "x": "NYSE", "region": "US"},
    "UPS":    {"name": "UPS",                 "domain": "ups.com",         "x": "NYSE", "region": "US"},
    "UNP":    {"name": "Union Pacific (rail)","domain": "up.com",          "x": "NYSE", "region": "US"},
    "SU.PA":  {"name": "Schneider Electric",  "domain": "se.com",          "x": "Paris", "region": "Europe"},
    "ABBN.SW":{"name": "ABB",                 "domain": "abb.com",         "x": "Swiss", "region": "Europe"},
    "AI.PA":  {"name": "Air Liquide",         "domain": "airliquide.com",  "x": "Paris", "region": "Europe"},
    "LIN":    {"name": "Linde",               "domain": "linde.com",       "x": "NYSE", "region": "US"},
    # ──── Fintech additional ────────────────────────────────────────
    "AXP":    {"name": "American Express",    "domain": "americanexpress.com","x": "NYSE", "region": "US"},
    "DFS":    {"name": "Discover Financial",  "domain": "discover.com",    "x": "NYSE", "region": "US"},
    # ──── Biotech additional ────────────────────────────────────────
    "ISRG":   {"name": "Intuitive Surgical",  "domain": "intuitivesurgical.com","x": "NASDAQ", "region": "US"},
    # ──── Retail US traditional (not in e-commerce) ─────────────────
    "HD":     {"name": "Home Depot",          "domain": "homedepot.com",   "x": "NYSE", "region": "US"},
    "LOW":    {"name": "Lowe's",              "domain": "lowes.com",       "x": "NYSE", "region": "US"},
    "TGT":    {"name": "Target",              "domain": "target.com",      "x": "NYSE", "region": "US"},
    # ──── Telecoms additional ───────────────────────────────────────
    "AMX":    {"name": "América Móvil (ADR)", "domain": "americamovil.com","x": "NYSE (ADR)", "region": "LatAm"},
    # ──── Small Caps US (Russell 2000 representative) ───────────────
    "AXON":   {"name": "Axon Enterprise",     "domain": "axon.com",        "x": "NASDAQ", "region": "US"},
    "PLTR":   {"name": "Palantir Technologies","domain": "palantir.com",   "x": "NASDAQ", "region": "US"},
    "RKLB":   {"name": "Rocket Lab",          "domain": "rocketlabusa.com","x": "NASDAQ", "region": "US"},
    "ACHR":   {"name": "Archer Aviation (eVTOL)","domain": "archer.com",   "x": "NYSE", "region": "US"},
    "JOBY":   {"name": "Joby Aviation (eVTOL)","domain": "jobyaviation.com","x": "NYSE", "region": "US"},
    "IONQ":   {"name": "IonQ (quantum)",      "domain": "ionq.com",        "x": "NYSE", "region": "US"},
    "RGTI":   {"name": "Rigetti Computing",   "domain": "rigetti.com",     "x": "NASDAQ", "region": "US"},
    "SMR":    {"name": "NuScale Power (SMR)", "domain": "nuscalepower.com","x": "NYSE", "region": "US"},
    "CVNA":   {"name": "Carvana",             "domain": "carvana.com",     "x": "NYSE", "region": "US"},
    "TOST":   {"name": "Toast (restaurant tech)","domain": "toasttab.com", "x": "NYSE", "region": "US"},
    "LUNR":   {"name": "Intuitive Machines",  "domain": "intuitivemachines.com","x": "NASDAQ", "region": "US"},
    "BROS":   {"name": "Dutch Bros",          "domain": "dutchbros.com",   "x": "NYSE", "region": "US"},
    # ──── Logiciels Europe ──────────────────────────────────────────
    "DSY.PA": {"name": "Dassault Systèmes",   "domain": "3ds.com",         "x": "Paris", "region": "Europe"},
    "CAP.PA": {"name": "Capgemini",           "domain": "capgemini.com",   "x": "Paris", "region": "Europe"},
    "SOP.PA": {"name": "Sopra Steria",        "domain": "soprasteria.com", "x": "Paris", "region": "Europe"},
    "NEM.DE": {"name": "Nemetschek",          "domain": "nemetschek.com",  "x": "XETRA", "region": "Europe"},
    "HEXA-B.ST":{"name":"Hexagon AB",         "domain": "hexagon.com",     "x": "Stockholm", "region": "Europe"},
    "TEMN.SW":{"name": "Temenos",             "domain": "temenos.com",     "x": "Swiss", "region": "Europe"},
    "ACN":    {"name": "Accenture",           "domain": "accenture.com",   "x": "NYSE", "region": "Europe"},
    "IFX.DE": {"name": "Infineon",            "domain": "infineon.com",    "x": "XETRA", "region": "Europe"},
    # ──── Nucléaire & SMR ───────────────────────────────────────────
    "CEG":    {"name": "Constellation Energy","domain": "constellationenergy.com","x": "NASDAQ", "region": "US"},
    "VST":    {"name": "Vistra Corp",         "domain": "vistracorp.com",  "x": "NYSE", "region": "US"},
    "CCJ":    {"name": "Cameco (uranium)",    "domain": "cameco.com",      "x": "NYSE", "region": "Canada"},
    "OKLO":   {"name": "Oklo (SMR)",          "domain": "oklo.com",        "x": "NYSE", "region": "US"},
    "BWXT":   {"name": "BWX Technologies",    "domain": "bwxt.com",        "x": "NYSE", "region": "US"},
    "LEU":    {"name": "Centrus Energy (HALEU)","domain":"centrusenergy.com","x": "NYSE", "region": "US"},
    "UEC":    {"name": "Uranium Energy",      "domain": "uraniumenergy.com","x": "NYSE", "region": "US"},
    "UUUU":   {"name": "Energy Fuels",        "domain": "energyfuels.com", "x": "NYSE", "region": "US"},
    # ──── Quantum Computing ─────────────────────────────────────────
    "QBTS":   {"name": "D-Wave Quantum",      "domain": "dwavesys.com",    "x": "NYSE (ADR)", "region": "Canada"},
    "QUBT":   {"name": "Quantum Computing Inc.","domain": "quantumcomputinginc.com","x": "NASDAQ", "region": "US"},
    "ARQQ":   {"name": "Arqit Quantum",       "domain": "arqit.uk",        "x": "NASDAQ (ADR)", "region": "UK"},
    "688027.SS":{"name":"QuantumCTek (Guodun Liangzi)","domain":"quantum-info.com","x":"Shanghai STAR","region":"China"},
    # ──── Restauration & Fast Food ──────────────────────────────────
    "MCD":    {"name": "McDonald's",          "domain": "mcdonalds.com",   "x": "NYSE", "region": "US"},
    "SBUX":   {"name": "Starbucks",           "domain": "starbucks.com",   "x": "NASDAQ", "region": "US"},
    "CMG":    {"name": "Chipotle Mexican Grill","domain": "chipotle.com",  "x": "NYSE", "region": "US"},
    "QSR":    {"name": "Restaurant Brands (Burger King)","domain": "rbi.com","x": "NYSE", "region": "US"},
    "YUM":    {"name": "Yum! Brands (KFC, Pizza Hut)","domain": "yum.com", "x": "NYSE", "region": "US"},
    "DRI":    {"name": "Darden Restaurants",  "domain": "darden.com",      "x": "NYSE", "region": "US"},
    "DPZ":    {"name": "Domino's Pizza",      "domain": "dominos.com",     "x": "NYSE", "region": "US"},
    "WEN":    {"name": "Wendy's",             "domain": "wendys.com",      "x": "NASDAQ", "region": "US"},
    # ──── Tabac & Spiritueux ────────────────────────────────────────
    "PM":     {"name": "Philip Morris Intl.", "domain": "pmi.com",         "x": "NYSE", "region": "US"},
    "MO":     {"name": "Altria",              "domain": "altria.com",      "x": "NYSE", "region": "US"},
    "BTI":    {"name": "British American Tobacco","domain":"bat.com",      "x": "NYSE (ADR)", "region": "UK"},
    "DEO":    {"name": "Diageo (ADR)",        "domain": "diageo.com",      "x": "NYSE (ADR)", "region": "UK"},
    "RI.PA":  {"name": "Pernod Ricard",       "domain": "pernod-ricard.com","x": "Paris", "region": "Europe"},
    "RCO.PA": {"name": "Rémy Cointreau",      "domain": "remy-cointreau.com","x": "Paris", "region": "Europe"},
    "STZ":    {"name": "Constellation Brands","domain": "cbrands.com",     "x": "NYSE", "region": "US"},
    "BUD":    {"name": "AB InBev (ADR)",      "domain": "ab-inbev.com",    "x": "NYSE (ADR)", "region": "Europe"},
    # ──── Semi Asie (ajoutés pour AI & Semi global) ─────────────────
    "2330.TW":{"name": "TSMC (Taipei)",       "domain": "tsmc.com",        "x": "Taipei", "region": "Korea_Taiwan"},
    "000660.KS":{"name":"SK Hynix",           "domain": "skhynix.com",     "x": "Seoul", "region": "Korea_Taiwan"},
    "8035.T": {"name": "Tokyo Electron",      "domain": "tel.com",         "x": "Tokyo", "region": "Japan"},
    "2317.TW":{"name": "Hon Hai (Foxconn)",   "domain": "honhai.com",      "x": "Taipei", "region": "Korea_Taiwan"},
    # ──── Banques Asie ──────────────────────────────────────────────
    "D05.SI": {"name": "DBS Group",           "domain": "dbs.com",         "x": "Singapore", "region": "ASEAN"},
    # ──── Robotique Japon + Europe ──────────────────────────────────
    "6954.T": {"name": "FANUC",               "domain": "fanuc.com",       "x": "Tokyo", "region": "Japan"},
    "6506.T": {"name": "Yaskawa Electric",    "domain": "yaskawa.com",     "x": "Tokyo", "region": "Japan"},
    "6594.T": {"name": "Nidec",               "domain": "nidec.com",       "x": "Tokyo", "region": "Japan"},
    "KGX.DE": {"name": "Kion Group",          "domain": "kiongroup.com",   "x": "XETRA", "region": "Europe"},
    "ROK":    {"name": "Rockwell Automation", "domain": "rockwellautomation.com","x": "NYSE", "region": "US"},
    "SYM":    {"name": "Symbotic",            "domain": "symbotic.com",    "x": "NASDAQ", "region": "US"},
    # ──── Space Economy ─────────────────────────────────────────────
    "SPCX":   {"name": "SpaceX",              "domain": "spacex.com",      "x": "NASDAQ", "region": "US"},
    "ASTS":   {"name": "AST SpaceMobile",     "domain": "ast-science.com", "x": "NASDAQ", "region": "US"},
    "PL":     {"name": "Planet Labs",         "domain": "planet.com",      "x": "NYSE", "region": "US"},
    "BKSY":   {"name": "BlackSky",            "domain": "blacksky.com",    "x": "NYSE", "region": "US"},
    # ──── Shipping & Maritime ───────────────────────────────────────
    "ZIM":    {"name": "ZIM Integrated Shipping","domain":"zim.com",       "x": "NYSE", "region": "Middle_East"},
    "GOGL":   {"name": "Golden Ocean",        "domain": "goldenocean.bm",  "x": "NASDAQ", "region": "US"},
    "SBLK":   {"name": "Star Bulk Carriers",  "domain": "starbulk.com",    "x": "NASDAQ", "region": "US"},
    "FLNG":   {"name": "FLEX LNG",            "domain": "flexlng.com",     "x": "NYSE", "region": "US"},
    "MAERSK-B.CO":{"name":"A.P. Møller-Mærsk","domain": "maersk.com",      "x": "Copenhagen", "region": "Europe"},
    "HLAG.DE":{"name": "Hapag-Lloyd",         "domain": "hapag-lloyd.com", "x": "XETRA", "region": "Europe"},
    "9104.T": {"name": "Mitsui O.S.K. Lines", "domain": "mol.co.jp",       "x": "Tokyo", "region": "Japan"},
    "9107.T": {"name": "Kawasaki Kisen",      "domain": "kline.co.jp",     "x": "Tokyo", "region": "Japan"},
    "2603.TW":{"name": "Evergreen Marine",    "domain": "evergreen-marine.com","x": "Taipei", "region": "Korea_Taiwan"},
    # ──── AI Software / Data ────────────────────────────────────────
    "AI":     {"name": "C3.ai",               "domain": "c3.ai",           "x": "NYSE", "region": "US"},
    "CFLT":   {"name": "Confluent",           "domain": "confluent.io",    "x": "NASDAQ", "region": "US"},
    "ESTC":   {"name": "Elastic",             "domain": "elastic.co",      "x": "NYSE", "region": "US"},
    # ──── Paris sportifs & Casinos ──────────────────────────────────
    "DKNG":   {"name": "DraftKings",          "domain": "draftkings.com",  "x": "NASDAQ", "region": "US"},
    "FLUT":   {"name": "Flutter Entertainment","domain": "flutter.com",    "x": "NYSE", "region": "US"},
    "MGM":    {"name": "MGM Resorts",         "domain": "mgmresorts.com",  "x": "NYSE", "region": "US"},
    "LVS":    {"name": "Las Vegas Sands",     "domain": "sands.com",       "x": "NYSE", "region": "US"},
    "CZR":    {"name": "Caesars Entertainment","domain": "caesars.com",    "x": "NASDAQ", "region": "US"},
    "EVO.ST": {"name": "Evolution AB",        "domain": "evolution.com",   "x": "Stockholm", "region": "Europe"},
    "0027.HK":{"name": "Galaxy Entertainment","domain": "galaxyentertainment.com","x": "Hong Kong", "region": "China"},
    "1128.HK":{"name": "Wynn Macau",          "domain": "wynnmacau.com",   "x": "Hong Kong", "region": "China"},
    # ──── Agriculture & Agroalim ────────────────────────────────────
    "ADM":    {"name": "Archer Daniels Midland","domain": "adm.com",       "x": "NYSE", "region": "US"},
    "BG":     {"name": "Bunge Global",        "domain": "bunge.com",       "x": "NYSE", "region": "US"},
    "CTVA":   {"name": "Corteva",             "domain": "corteva.com",     "x": "NYSE", "region": "US"},
    "MOS":    {"name": "Mosaic",              "domain": "mosaicco.com",    "x": "NYSE", "region": "US"},
    "NTR":    {"name": "Nutrien",             "domain": "nutrien.com",     "x": "NYSE", "region": "US"},
    # ──── Pharma Asie ───────────────────────────────────────────────
    "4502.T": {"name": "Takeda",              "domain": "takeda.com",      "x": "Tokyo", "region": "Japan"},
    "4568.T": {"name": "Daiichi Sankyo",      "domain": "daiichisankyo.com","x": "Tokyo", "region": "Japan"},
    # ──── Mineurs d'or mondial ──────────────────────────────────────
    "NST.AX": {"name": "Northern Star Resources","domain": "nsrltd.com",   "x": "ASX", "region": "Australia"},
    "0601.HK":{"name": "Zijin Mining",        "domain": "zijinmining.com", "x": "Hong Kong", "region": "China"},
    # ──── Luxe Asie ─────────────────────────────────────────────────
    "1929.HK":{"name": "Chow Tai Fook Jewellery","domain":"ctfjewellery.com","x":"Hong Kong", "region": "China"},
    "600519.SS":{"name":"Kweichow Moutai",    "domain": "moutaichina.com", "x": "Shanghai", "region": "China"},
    # ──── Auto Asie ─────────────────────────────────────────────────
    "005380.KS":{"name":"Hyundai Motor",      "domain": "hyundai.com",     "x": "Seoul", "region": "Korea_Taiwan"},
    # ──── LatAm ─────────────────────────────────────────────────────
    "PBR":    {"name": "Petrobras (ADR)",     "domain": "petrobras.com.br","x": "NYSE (ADR)", "region": "LatAm"},
    "ITUB":   {"name": "Itaú Unibanco (ADR)", "domain": "itau.com.br",     "x": "NYSE (ADR)", "region": "LatAm"},
    "BBD":    {"name": "Banco Bradesco (ADR)","domain": "bradesco.com.br", "x": "NYSE (ADR)", "region": "LatAm"},
    "ABEV":   {"name": "Ambev (ADR)",         "domain": "ambev.com.br",    "x": "NYSE (ADR)", "region": "LatAm"},
    # ──── Chine Tech additions ──────────────────────────────────────
    "BIDU":   {"name": "Baidu (ADR)",         "domain": "baidu.com",       "x": "NASDAQ (ADR)", "region": "China"},
    "NTES":   {"name": "NetEase (ADR)",       "domain": "neteaseglobal.com","x": "NASDAQ (ADR)", "region": "China"},
    "9888.HK":{"name": "Baidu (HK)",          "domain": "baidu.com",       "x": "Hong Kong", "region": "China"},
    # ──── Santé/Assurance additional ────────────────────────────────
    "UNH":    {"name": "UnitedHealth Group",  "domain": "unitedhealthgroup.com","x":"NYSE", "region": "US"},

    # ──── ENRICHISSEMENT v2 — tickers mondiaux par region ────────────
    # US extras
    "UNH":  {"name": "UnitedHealth Group",    "domain": "unitedhealthgroup.com","x":"NYSE","region":"US"},
    "TRU":  {"name": "TransUnion",            "domain": "transunion.com",   "x": "NYSE","region":"US"},
    "CVS":  {"name": "CVS Health",            "domain": "cvshealth.com",   "x": "NYSE","region":"US"},
    "HUM":  {"name": "Humana",                "domain": "humana.com",      "x": "NYSE","region":"US"},
    "CI":   {"name": "Cigna Group",           "domain": "cigna.com",       "x": "NYSE","region":"US"},
    "HCA":  {"name": "HCA Healthcare",        "domain": "hcahealthcare.com","x": "NYSE","region":"US"},
    "TFC":  {"name": "Truist Financial",      "domain": "truist.com",      "x": "NYSE","region":"US"},
    "MTB":  {"name": "M&T Bank",              "domain": "mtb.com",         "x": "NYSE","region":"US"},
    "FITB": {"name": "Fifth Third Bank",      "domain": "53.com",          "x": "NASDAQ","region":"US"},
    "HBAN": {"name": "Huntington Bancshares", "domain": "huntington.com",  "x": "NASDAQ","region":"US"},
    "RF":   {"name": "Regions Financial",     "domain": "regions.com",     "x": "NYSE","region":"US"},
    "KEY":  {"name": "KeyCorp",               "domain": "key.com",         "x": "NYSE","region":"US"},
    "CFG":  {"name": "Citizens Financial",    "domain": "citizensbank.com","x": "NYSE","region":"US"},
    "STT":  {"name": "State Street",          "domain": "statestreet.com", "x": "NYSE","region":"US"},
    "NTRS": {"name": "Northern Trust",        "domain": "northerntrust.com","x": "NASDAQ","region":"US"},
    "AMP":  {"name": "Ameriprise Financial",  "domain": "ameriprise.com",  "x": "NYSE","region":"US"},
    "RJF":  {"name": "Raymond James",         "domain": "raymondjames.com","x": "NYSE","region":"US"},
    "TROW": {"name": "T. Rowe Price",         "domain": "troweprice.com",  "x": "NASDAQ","region":"US"},
    "IVZ":  {"name": "Invesco",               "domain": "invesco.com",     "x": "NYSE","region":"US"},
    "BEN":  {"name": "Franklin Resources",    "domain": "franklintempleton.com","x":"NYSE","region":"US"},
    "HIG":  {"name": "Hartford Financial",    "domain": "thehartford.com", "x": "NYSE","region":"US"},
    "PGR":  {"name": "Progressive",           "domain": "progressive.com", "x": "NYSE","region":"US"},
    "ALL":  {"name": "Allstate",              "domain": "allstate.com",    "x": "NYSE","region":"US"},
    "LULU": {"name": "Lululemon Athletica",   "domain": "lululemon.com",   "x": "NASDAQ","region":"US"},
    "NKE":  {"name": "Nike",                  "domain": "nike.com",        "x": "NYSE","region":"US"},
    "UA":   {"name": "Under Armour",          "domain": "underarmour.com", "x": "NYSE","region":"US"},
    "RL":   {"name": "Ralph Lauren",          "domain": "ralphlauren.com", "x": "NYSE","region":"US"},
    "TPR":  {"name": "Tapestry (Coach)",      "domain": "tapestry.com",    "x": "NYSE","region":"US"},
    "CPRI": {"name": "Capri Holdings",        "domain": "capriholdings.com","x":"NYSE","region":"US"},
    "PVH":  {"name": "PVH (Calvin Klein)",    "domain": "pvh.com",         "x": "NYSE","region":"US"},
    "F":    {"name": "Ford",                  "domain": "ford.com",        "x": "NYSE","region":"US"},
    "GOOG": {"name": "Alphabet (C shares)",   "domain": "abc.xyz",         "x": "NASDAQ","region":"US"},
    "TEM":  {"name": "Tempus AI",             "domain": "tempus.com",      "x": "NASDAQ","region":"US"},
    "PATH": {"name": "UiPath",                "domain": "uipath.com",      "x": "NYSE","region":"US"},
    "GTLB": {"name": "GitLab",                "domain": "gitlab.com",      "x": "NASDAQ","region":"US"},
    "PLNT": {"name": "Planet Fitness",        "domain": "planetfitness.com","x":"NYSE","region":"US"},
    "DHR":  {"name": "Danaher",               "domain": "danaher.com",     "x": "NYSE","region":"US"},
    "TMO":  {"name": "Thermo Fisher",         "domain": "thermofisher.com","x": "NYSE","region":"US"},
    "ABT":  {"name": "Abbott Laboratories",   "domain": "abbott.com",      "x": "NYSE","region":"US"},
    "CSCO": {"name": "Cisco Systems",         "domain": "cisco.com",       "x": "NASDAQ","region":"US"},
    "HPE":  {"name": "Hewlett Packard Enterprise","domain":"hpe.com",      "x": "NYSE",  "region":"US"},
    "INTU": {"name": "Intuit",                "domain": "intuit.com",      "x": "NASDAQ","region":"US"},
    "LRCX": {"name": "Lam Research",          "domain": "lamresearch.com", "x": "NASDAQ","region":"US"},
    "KLAC": {"name": "KLA Corporation",       "domain": "kla.com",         "x": "NASDAQ","region":"US"},
    "AMAT": {"name": "Applied Materials",     "domain": "appliedmaterials.com","x":"NASDAQ","region":"US"},
    "ON":   {"name": "ON Semiconductor",      "domain": "onsemi.com",      "x": "NASDAQ","region":"US"},
    "MCHP": {"name": "Microchip Technology",  "domain": "microchip.com",   "x": "NASDAQ","region":"US"},
    "TXN":  {"name": "Texas Instruments",     "domain": "ti.com",          "x": "NASDAQ","region":"US"},
    "CNQ":  {"name": "Canadian Natural Resources","domain":"cnrl.com",     "x": "TSX","region":"Canada"},
    # Canada additions
    "RY.TO":   {"name": "Royal Bank of Canada","domain":"rbc.com",         "x": "TSX","region":"Canada"},
    "TD.TO":   {"name": "Toronto-Dominion Bank","domain":"td.com",         "x": "TSX","region":"Canada"},
    "BNS.TO":  {"name": "Bank of Nova Scotia","domain":"scotiabank.com",   "x": "TSX","region":"Canada"},
    "BMO.TO":  {"name": "Bank of Montreal",   "domain": "bmo.com",         "x": "TSX","region":"Canada"},
    "CM.TO":   {"name": "CIBC",               "domain": "cibc.com",        "x": "TSX","region":"Canada"},
    "ENB.TO":  {"name": "Enbridge",           "domain": "enbridge.com",    "x": "TSX","region":"Canada"},
    "TRP.TO":  {"name": "TC Energy",          "domain": "tcenergy.com",    "x": "TSX","region":"Canada"},
    "SU.TO":   {"name": "Suncor Energy",      "domain": "suncor.com",      "x": "TSX","region":"Canada"},
    "CNR.TO":  {"name": "Canadian National Railway","domain":"cn.ca",      "x": "TSX","region":"Canada"},
    "CP.TO":   {"name": "Canadian Pacific Kansas","domain":"cpkcr.com",    "x": "TSX","region":"Canada"},
    "BCE.TO":  {"name": "BCE (Bell Canada)",  "domain": "bce.ca",          "x": "TSX","region":"Canada"},
    "SHOP.TO": {"name": "Shopify (TSX)",      "domain": "shopify.com",     "x": "TSX","region":"Canada"},
    "WCN.TO":  {"name": "Waste Connections", "domain": "wasteconnections.com","x":"TSX","region":"Canada"},
    "MFC.TO":  {"name": "Manulife Financial", "domain": "manulife.com",    "x": "TSX","region":"Canada"},
    "ATD.TO":  {"name": "Alimentation Couche-Tard","domain":"couche-tard.com","x":"TSX","region":"Canada"},
    # UK additions
    "ULVR.L":  {"name": "Unilever (UK)",      "domain": "unilever.com",    "x": "London","region":"UK"},
    "DGE.L":   {"name": "Diageo (UK)",        "domain": "diageo.com",      "x": "London","region":"UK"},
    "RIO.L":   {"name": "Rio Tinto (UK)",     "domain": "riotinto.com",    "x": "London","region":"UK"},
    "GLEN.L":  {"name": "Glencore",           "domain": "glencore.com",    "x": "London","region":"UK"},
    "REL.L":   {"name": "RELX",               "domain": "relx.com",        "x": "London","region":"UK"},
    "LLOY.L":  {"name": "Lloyds Banking",     "domain": "lloydsbankinggroup.com","x": "London","region":"UK"},
    "NWG.L":   {"name": "NatWest Group",      "domain": "natwestgroup.com","x": "London","region":"UK"},
    "STAN.L":  {"name": "Standard Chartered", "domain": "sc.com",          "x": "London","region":"UK"},
    "ROR.L":   {"name": "Rotork",             "domain": "rotork.com",      "x": "London","region":"UK"},
    "SGE.L":   {"name": "Sage Group",         "domain": "sage.com",        "x": "London","region":"UK"},
    "BKG.L":   {"name": "Berkeley Group",     "domain": "berkeleygroup.co.uk","x": "London","region":"UK"},
    "RTO.L":   {"name": "Rentokil",           "domain": "rentokil-initial.com","x": "London","region":"UK"},
    # Europe continental additions
    "PRX.AS": {"name": "Prosus",              "domain": "prosus.com",      "x": "Amsterdam","region":"Europe"},
    "RNO.PA": {"name": "Renault",             "domain": "renault.com",     "x": "Paris","region":"Europe"},
    "AD.AS":  {"name": "Ahold Delhaize",      "domain": "aholddelhaize.com","x":"Amsterdam","region":"Europe"},
    "HEIA.AS":{"name": "Heineken",            "domain": "heineken.com",    "x": "Amsterdam","region":"Europe"},
    "ABI.BR": {"name": "AB InBev (Brussels)", "domain": "ab-inbev.com",    "x": "Brussels","region":"Europe"},
    "UCB.BR": {"name": "UCB",                 "domain": "ucb.com",         "x": "Brussels","region":"Europe"},
    "EL.PA":  {"name": "EssilorLuxottica",    "domain": "essilorluxottica.com","x":"Paris","region":"Europe"},
    "PUB.PA": {"name": "Publicis Groupe",     "domain": "publicisgroupe.com","x": "Paris","region":"Europe"},
    "CA.PA":  {"name": "Carrefour",           "domain": "carrefour.com",   "x": "Paris","region":"Europe"},
    "VIE.PA": {"name": "Veolia",              "domain": "veolia.com",      "x": "Paris","region":"Europe"},
    "MC.SW":  {"name": "Swatch Group",        "domain": "swatchgroup.com", "x": "Swiss","region":"Europe"},
    "GIVN.SW":{"name": "Givaudan",            "domain": "givaudan.com",    "x": "Swiss","region":"Europe"},
    "FME.DE": {"name": "Fresenius Medical",   "domain": "freseniusmedicalcare.com","x":"XETRA","region":"Europe"},
    "BAS.DE": {"name": "BASF",                "domain": "basf.com",        "x": "XETRA","region":"Europe"},
    "BAYN.DE":{"name": "Bayer",               "domain": "bayer.com",       "x": "XETRA","region":"Europe"},
    "DTG.DE": {"name": "Daimler Truck",       "domain": "daimlertruck.com","x": "XETRA","region":"Europe"},
    "CON.DE": {"name": "Continental",         "domain": "continental.com", "x": "XETRA","region":"Europe"},
    "ADS.DE": {"name": "Adidas",              "domain": "adidas.com",      "x": "XETRA","region":"Europe"},
    "PUM.DE": {"name": "Puma",                "domain": "puma.com",        "x": "XETRA","region":"Europe"},
    "CPR.MI": {"name": "Campari Group",       "domain": "camparigroup.com","x": "Milan","region":"Europe"},
    "G.MI":   {"name": "Assicurazioni Generali","domain":"generali.com",   "x": "Milan","region":"Europe"},
    "ENI.MI": {"name": "Eni",                 "domain": "eni.com",         "x": "Milan","region":"Europe"},
    "UCG.MI": {"name": "UniCredit",           "domain": "unicreditgroup.eu","x": "Milan","region":"Europe"},
    "MONC.MI":{"name": "Moncler",             "domain": "monclergroup.com","x": "Milan","region":"Europe"},
    "FER.MC": {"name": "Ferrovial",           "domain": "ferrovial.com",   "x": "Madrid","region":"Europe"},
    "ITX.MC": {"name": "Inditex (Zara)",      "domain": "inditex.com",     "x": "Madrid","region":"Europe"},
    "NDA-FI.HE":{"name":"Nordea Bank",        "domain":"nordea.com",       "x": "Helsinki","region":"Europe"},
    "EQNR.OL":{"name": "Equinor (Oslo)",      "domain": "equinor.com",     "x": "Oslo","region":"Europe"},
    "NOVO-B.CO":{"name":"Novo Nordisk (DK)",  "domain":"novonordisk.com",  "x": "Copenhagen","region":"Europe"},
    "CARL-B.CO":{"name":"Carlsberg",          "domain": "carlsberggroup.com","x":"Copenhagen","region":"Europe"},
    "KNEBV.HE":{"name": "Kone",               "domain": "kone.com",        "x": "Helsinki","region":"Europe"},
    "CRH.L":  {"name": "CRH",                 "domain": "crh.com",         "x": "London","region":"Europe"},
    "ATLN.SW":{"name": "Alcon",               "domain": "alcon.com",       "x": "Swiss","region":"Europe"},
    "LONN.SW":{"name": "Lonza Group",         "domain": "lonza.com",       "x": "Swiss","region":"Europe"},
    "SIKA.SW":{"name": "Sika",                "domain": "sika.com",        "x": "Swiss","region":"Europe"},
    "PRX.L":  {"name": "Prudential (UK-2)",   "domain":"prudentialplc.com","x": "London","region":"UK"},
    # Japan additions
    "6902.T": {"name": "DENSO",               "domain": "denso.com",       "x": "Tokyo","region":"Japan"},
    "7267.T": {"name": "Honda Motor (Tokyo)", "domain": "honda.com",       "x": "Tokyo","region":"Japan"},
    "7201.T": {"name": "Nissan Motor",        "domain": "nissan-global.com","x": "Tokyo","region":"Japan"},
    "6920.T": {"name": "Lasertec",            "domain": "lasertec.co.jp",  "x": "Tokyo","region":"Japan"},
    "8001.T": {"name": "Itochu",              "domain": "itochu.co.jp",    "x": "Tokyo","region":"Japan"},
    "8031.T": {"name": "Mitsui & Co.",        "domain": "mitsui.com",      "x": "Tokyo","region":"Japan"},
    "9432.T": {"name": "NTT (Tokyo)",         "domain": "ntt.co.jp",       "x": "Tokyo","region":"Japan"},
    "9433.T": {"name": "KDDI",                "domain": "kddi.com",        "x": "Tokyo","region":"Japan"},
    "9434.T": {"name": "SoftBank Corp.",      "domain": "softbank.jp",     "x": "Tokyo","region":"Japan"},
    "4661.T": {"name": "Oriental Land (Tokyo Disney)","domain":"olc.co.jp","x": "Tokyo","region":"Japan"},
    "9202.T": {"name": "ANA Holdings",        "domain": "ana.co.jp",       "x": "Tokyo","region":"Japan"},
    "9201.T": {"name": "Japan Airlines",      "domain": "jal.com",         "x": "Tokyo","region":"Japan"},
    "4543.T": {"name": "Terumo",              "domain": "terumo.com",      "x": "Tokyo","region":"Japan"},
    "4911.T": {"name": "Shiseido",            "domain": "shiseido.com",    "x": "Tokyo","region":"Japan"},
    "8766.T": {"name": "Tokio Marine",        "domain": "tokiomarine.com", "x": "Tokyo","region":"Japan"},
    # China additions
    "0005.HK":{"name": "HSBC (HK)",           "domain": "hsbc.com",        "x": "Hong Kong","region":"China"},
    "2388.HK":{"name": "BOC Hong Kong",       "domain": "bochk.com",       "x": "Hong Kong","region":"China"},
    "9618.HK":{"name": "JD.com (HK)",         "domain": "jd.com",          "x": "Hong Kong","region":"China"},
    "1810.HK":{"name": "Xiaomi",              "domain": "mi.com",          "x": "Hong Kong","region":"China"},
    "9999.HK":{"name": "NetEase (HK)",        "domain": "neteaseglobal.com","x": "Hong Kong","region":"China"},
    "0968.HK":{"name": "Xinyi Solar",         "domain": "xinyisolar.com",  "x": "Hong Kong","region":"China"},
    "2020.HK":{"name": "Anta Sports",         "domain": "anta.com",        "x": "Hong Kong","region":"China"},
    "1088.HK":{"name": "China Shenhua Energy","domain": "shenhuagroup.com","x": "Hong Kong","region":"China"},
    "0386.HK":{"name": "Sinopec",             "domain": "sinopec.com",     "x": "Hong Kong","region":"China"},
    "0883.HK":{"name": "CNOOC",               "domain": "cnoocltd.com",    "x": "Hong Kong","region":"China"},
    "3888.HK":{"name": "Kingsoft",            "domain": "kingsoft.com",    "x": "Hong Kong","region":"China"},
    "9626.HK":{"name": "Bilibili (HK)",       "domain": "bilibili.com",    "x": "Hong Kong","region":"China"},
    "601318.SS":{"name":"Ping An (Shanghai)", "domain": "pingan.com",      "x": "Shanghai","region":"China"},
    "601857.SS":{"name":"PetroChina (Shanghai)","domain":"petrochina.com.cn","x":"Shanghai","region":"China"},
    "300750.SZ":{"name":"CATL (Contemporary Amperex)","domain":"catl.com","x": "Shenzhen","region":"China"},
    "002594.SZ":{"name":"BYD (Shenzhen)",     "domain": "byd.com",         "x": "Shenzhen","region":"China"},
    "300015.SZ":{"name":"Aier Eye Hospital",  "domain": "aier.com.cn",     "x": "Shenzhen","region":"China"},
    # Korea & Taiwan additions
    "035420.KS":{"name":"Naver",              "domain": "navercorp.com",   "x": "Seoul","region":"Korea_Taiwan"},
    "035720.KS":{"name":"Kakao",              "domain": "kakaocorp.com",   "x": "Seoul","region":"Korea_Taiwan"},
    "051910.KS":{"name":"LG Chem",            "domain": "lgchem.com",      "x": "Seoul","region":"Korea_Taiwan"},
    "373220.KS":{"name":"LG Energy Solution", "domain": "lgensol.com",     "x": "Seoul","region":"Korea_Taiwan"},
    "006400.KS":{"name":"Samsung SDI",        "domain": "samsungsdi.com",  "x": "Seoul","region":"Korea_Taiwan"},
    "2454.TW":{"name":"MediaTek",             "domain": "mediatek.com",    "x": "Taipei","region":"Korea_Taiwan"},
    "2382.TW":{"name":"Quanta Computer",      "domain": "quantatw.com",    "x": "Taipei","region":"Korea_Taiwan"},
    "2308.TW":{"name":"Delta Electronics (TW)","domain":"deltaww.com",     "x": "Taipei","region":"Korea_Taiwan"},
    "1301.TW":{"name":"Formosa Plastics",     "domain": "fpcusa.com",      "x": "Taipei","region":"Korea_Taiwan"},
    "2881.TW":{"name":"Fubon Financial",      "domain": "fubon.com",       "x": "Taipei","region":"Korea_Taiwan"},
    # India additions
    "BAJFINANCE.NS":{"name":"Bajaj Finance",  "domain": "bajajfinserv.in", "x": "NSE","region":"India"},
    "MARUTI.NS":{"name":"Maruti Suzuki",      "domain": "marutisuzuki.com","x": "NSE","region":"India"},
    "ADANIENT.NS":{"name":"Adani Enterprises","domain": "adani.com",       "x": "NSE","region":"India"},
    "ITC.NS":{"name":"ITC Limited",           "domain": "itcportal.com",   "x": "NSE","region":"India"},
    "LT.NS":{"name":"Larsen & Toubro",        "domain": "larsentoubro.com","x": "NSE","region":"India"},
    "WIPRO.NS":{"name":"Wipro",               "domain": "wipro.com",       "x": "NSE","region":"India"},
    "KOTAKBANK.NS":{"name":"Kotak Mahindra Bank","domain":"kotak.com",     "x": "NSE","region":"India"},
    "AXISBANK.NS":{"name":"Axis Bank",        "domain": "axisbank.com",    "x": "NSE","region":"India"},
    "SUNPHARMA.NS":{"name":"Sun Pharmaceutical","domain":"sunpharma.com",  "x": "NSE","region":"India"},
    "TATAMOTORS.NS":{"name":"Tata Motors",    "domain": "tatamotors.com",  "x": "NSE","region":"India"},
    "BAJAJ-AUTO.NS":{"name":"Bajaj Auto",     "domain": "bajajauto.com",   "x": "NSE","region":"India"},
    "NTPC.NS":{"name":"NTPC (India utility)", "domain": "ntpc.co.in",      "x": "NSE","region":"India"},
    # LatAm additions
    "VIST":   {"name": "Vista Energy",        "domain": "vistaenergy.com", "x": "NYSE","region":"LatAm"},
    "CX":     {"name": "Cemex (ADR)",         "domain": "cemex.com",       "x": "NYSE","region":"LatAm"},
    "KOF":    {"name": "Coca-Cola Femsa",     "domain": "coca-colafemsa.com","x":"NYSE","region":"LatAm"},
    "FMX":    {"name": "Fomento Económico Mexicano (FEMSA)","domain":"femsa.com","x":"NYSE","region":"LatAm"},
    "WALMEX.MX":{"name":"Walmart de México",  "domain": "walmartmexico.com","x":"Mexico","region":"LatAm"},
    "GMEXICOB.MX":{"name":"Grupo México",     "domain": "gmexico.com",     "x": "Mexico","region":"LatAm"},
    "BBVA":   {"name": "BBVA (ADR)",          "domain": "bbva.com",        "x": "NYSE","region":"Europe"},
    # Australia additions
    "CBA.AX": {"name": "Commonwealth Bank of Australia","domain":"commbank.com.au","x":"ASX","region":"Australia"},
    "WBC.AX": {"name": "Westpac Banking",     "domain": "westpac.com.au",  "x": "ASX","region":"Australia"},
    "NAB.AX": {"name": "NAB (Australia)",     "domain": "nab.com.au",      "x": "ASX","region":"Australia"},
    "ANZ.AX": {"name": "ANZ Banking",         "domain": "anz.com.au",      "x": "ASX","region":"Australia"},
    "BHP.AX": {"name": "BHP (ASX)",           "domain": "bhp.com",         "x": "ASX","region":"Australia"},
    "RIO.AX": {"name": "Rio Tinto (ASX)",     "domain": "riotinto.com",    "x": "ASX","region":"Australia"},
    "FMG.AX": {"name": "Fortescue Metals",    "domain": "fmgl.com.au",     "x": "ASX","region":"Australia"},
    "WOW.AX": {"name": "Woolworths Group",    "domain": "woolworthsgroup.com.au","x":"ASX","region":"Australia"},
    "CSL.AX": {"name": "CSL Limited",         "domain": "csl.com",         "x": "ASX","region":"Australia"},
    "TLS.AX": {"name": "Telstra",             "domain": "telstra.com.au",  "x": "ASX","region":"Australia"},
    "QAN.AX": {"name": "Qantas Airways",      "domain": "qantas.com",      "x": "ASX","region":"Australia"},
    "WES.AX": {"name": "Wesfarmers",          "domain": "wesfarmers.com.au","x":"ASX","region":"Australia"},
    "MQG.AX": {"name": "Macquarie Group",     "domain": "macquarie.com",   "x": "ASX","region":"Australia"},
    "NCM.AX": {"name": "Newmont (Aus listing)","domain":"newmont.com",     "x": "ASX","region":"Australia"},
    # ASEAN additions
    "O39.SI": {"name": "OCBC Bank",           "domain": "ocbc.com",        "x": "Singapore","region":"ASEAN"},
    "U11.SI": {"name": "UOB Bank",            "domain": "uob.com.sg",      "x": "Singapore","region":"ASEAN"},
    "Z74.SI": {"name": "Singtel",             "domain": "singtel.com",     "x": "Singapore","region":"ASEAN"},
    "C6L.SI": {"name": "Singapore Airlines",  "domain": "singaporeair.com","x": "Singapore","region":"ASEAN"},
    "N2IU.SI":{"name":"Mapletree Ind. Trust", "domain": "mapletreeindustrialtrust.com","x":"Singapore","region":"ASEAN"},
    "BBCA.JK":{"name": "Bank Central Asia (Indonesia)","domain":"bca.co.id","x":"Jakarta","region":"ASEAN"},
    "BMRI.JK":{"name": "Bank Mandiri",        "domain": "bankmandiri.co.id","x":"Jakarta","region":"ASEAN"},
    "1155.KL":{"name": "Maybank",             "domain": "maybank.com",     "x": "Bursa Malaysia","region":"ASEAN"},
    "PTT.BK": {"name": "PTT (Thailand)",      "domain": "pttplc.com",      "x": "Bangkok","region":"ASEAN"},
    "ADVANC.BK":{"name":"Advanced Info Service","domain":"ais.co.th",     "x": "Bangkok","region":"ASEAN"},
    # Middle East additions
    "2222.SR":{"name":"Saudi Aramco",       "domain": "aramco.com",      "x": "Tadawul","region":"Middle_East"},
    "1120.SR":{"name":"Al Rajhi Bank",        "domain": "alrajhibank.com.sa","x":"Tadawul","region":"Middle_East"},
    "2082.SR":{"name":"ACWA Power",           "domain": "acwapower.com",   "x": "Tadawul","region":"Middle_East"},
    "EMIRATESNBD.AE":{"name":"Emirates NBD",  "domain":"emiratesnbd.com",  "x": "Dubai","region":"Middle_East"},
    "EAND.AE":{"name":"Etisalat (e&)",        "domain": "etisalat.ae",     "x": "Abu Dhabi","region":"Middle_East"},
    # ──── Sportswear & Fitness (nouveau secteur) ────────────────────
    "NKE":    {"name": "Nike",                "domain": "nike.com",        "x": "NYSE","region":"US"},
    "LULU":   {"name": "Lululemon",           "domain": "lululemon.com",   "x": "NASDAQ","region":"US"},
    "PLNT":   {"name": "Planet Fitness",      "domain": "planetfitness.com","x":"NYSE","region":"US"},
    "ADS.DE": {"name": "Adidas",              "domain": "adidas.com",      "x": "XETRA","region":"Europe"},
    "PUM.DE": {"name": "Puma",                "domain": "puma.com",        "x": "XETRA","region":"Europe"},
    "2020.HK":{"name": "Anta Sports",         "domain": "anta.com",        "x": "Hong Kong","region":"China"},
    "DECK":   {"name": "Deckers (UGG, Hoka)", "domain": "deckers.com",     "x": "NYSE","region":"US"},
    "ONON":   {"name": "On Holding",          "domain": "on-running.com",  "x": "NYSE","region":"Europe"},
    "CROX":   {"name": "Crocs",               "domain": "crocs.com",       "x": "NASDAQ","region":"US"},
    "JD.L":   {"name": "JD Sports Fashion",   "domain": "jdsports.com",    "x": "London","region":"UK"},
    # ──── Chine — ajouts pour couverture sectorielle ───────────────
    "0981.HK":{"name":"SMIC",                 "domain":"smics.com",        "x":"Hong Kong","region":"China"},
    "1347.HK":{"name":"Hua Hong Semiconductor","domain":"huahonggrace.com","x":"Hong Kong","region":"China"},
    "002371.SZ":{"name":"Naura Technology",   "domain":"naura.com",        "x":"Shenzhen","region":"China"},
    "300308.SZ":{"name":"Zhongji Innolight",  "domain":"innolight.com",    "x":"Shenzhen","region":"China"},
    "1766.HK":{"name":"CRRC Corporation",     "domain":"crrcgc.cc",        "x":"Hong Kong","region":"China"},
    "3808.HK":{"name":"Sinotruk",             "domain":"sinotruk.com",     "x":"Hong Kong","region":"China"},
    "601668.SS":{"name":"China State Construction","domain":"cscec.com",   "x":"Shanghai","region":"China"},
    "1157.HK":{"name":"Zoomlion Heavy Industry","domain":"zoomlion.com",   "x":"Hong Kong","region":"China"},
    "2269.HK":{"name":"WuXi Biologics",       "domain":"wuxibiologics.com","x":"Hong Kong","region":"China"},
    "2359.HK":{"name":"WuXi AppTec",          "domain":"wuxiapptec.com",   "x":"Hong Kong","region":"China"},
    "600276.SS":{"name":"Jiangsu Hengrui Pharma","domain":"hrs.com.cn",    "x":"Shanghai","region":"China"},
    "BGNE":   {"name":"BeiGene (ADR)",        "domain":"beigene.com",      "x":"NASDAQ (ADR)","region":"China"},
    "1801.HK":{"name":"Innovent Biologics",   "domain":"innoventbio.com",  "x":"Hong Kong","region":"China"},
    "600887.SS":{"name":"Yili Group (dairy)", "domain":"yili.com",         "x":"Shanghai","region":"China"},
    "2319.HK":{"name":"China Mengniu Dairy",  "domain":"mengniuir.com",    "x":"Hong Kong","region":"China"},
    "0168.HK":{"name":"Tsingtao Brewery",     "domain":"tsingtao.com.cn",  "x":"Hong Kong","region":"China"},
    "0291.HK":{"name":"China Resources Beer", "domain":"crbeer.com.hk",    "x":"Hong Kong","region":"China"},
    "000858.SZ":{"name":"Wuliangye Yibin",    "domain":"wuliangye.com.cn", "x":"Shenzhen","region":"China"},
    "YUMC":   {"name":"Yum China (KFC, Pizza Hut)","domain":"yumchina.com","x":"NYSE","region":"China"},
    "6862.HK":{"name":"Haidilao International","domain":"haidilao.com",    "x":"Hong Kong","region":"China"},
    "600900.SS":{"name":"China Yangtze Power","domain":"cypc.com.cn",      "x":"Shanghai","region":"China"},
    "1816.HK":{"name":"CGN Power",            "domain":"cgnpower.com.cn",  "x":"Hong Kong","region":"China"},
    "TCOM":   {"name":"Trip.com Group (ADR)", "domain":"trip.com",         "x":"NASDAQ (ADR)","region":"China"},
    "1919.HK":{"name":"COSCO Shipping",       "domain":"coscoshipping.com","x":"Hong Kong","region":"China"},
    "LU":     {"name":"Lufax (ADR)",          "domain":"lu.com",           "x":"NYSE (ADR)","region":"China"},
    "FUTU":   {"name":"Futu Holdings",        "domain":"futuholdings.com", "x":"NASDAQ (ADR)","region":"China"},
    "0268.HK":{"name":"Kingdee International","domain":"kingdee.com",      "x":"Hong Kong","region":"China"},
    "601012.SS":{"name":"Longi Green Energy", "domain":"longi.com",        "x":"Shanghai","region":"China"},
    "0753.HK":{"name":"Air China",            "domain":"airchina.com.cn",  "x":"Hong Kong","region":"China"},
    # ──── Japon — leaders manquants ────────────────────────────────
    "7974.T": {"name":"Nintendo",             "domain":"nintendo.com",     "x":"Tokyo","region":"Japan"},
    "7832.T": {"name":"Bandai Namco",         "domain":"bandainamco.com",  "x":"Tokyo","region":"Japan"},
    "9684.T": {"name":"Square Enix",          "domain":"square-enix.com",  "x":"Tokyo","region":"Japan"},
    "9766.T": {"name":"Konami Group",         "domain":"konami.com",       "x":"Tokyo","region":"Japan"},
    "4503.T": {"name":"Astellas Pharma",      "domain":"astellas.com",     "x":"Tokyo","region":"Japan"},
    "4523.T": {"name":"Eisai",                "domain":"eisai.com",        "x":"Tokyo","region":"Japan"},
    "3382.T": {"name":"Seven & i Holdings",   "domain":"7andi.com",        "x":"Tokyo","region":"Japan"},
    "6301.T": {"name":"Komatsu",              "domain":"komatsu.com",      "x":"Tokyo","region":"Japan"},
    "1605.T": {"name":"INPEX (Japan oil)",    "domain":"inpex.co.jp",      "x":"Tokyo","region":"Japan"},
    "8601.T": {"name":"Daiwa Securities",     "domain":"daiwa-grp.jp",     "x":"Tokyo","region":"Japan"},
    # ──── Corée / Taïwan — leaders manquants ───────────────────────
    "005490.KS":{"name":"POSCO Holdings",     "domain":"posco.com",        "x":"Seoul","region":"Korea_Taiwan"},
    "000270.KS":{"name":"Kia Motors",         "domain":"kia.com",          "x":"Seoul","region":"Korea_Taiwan"},
    "017670.KS":{"name":"SK Telecom",         "domain":"sktelecom.com",    "x":"Seoul","region":"Korea_Taiwan"},
    "CPNG":    {"name":"Coupang (Korean ecom)","domain":"aboutcoupang.com","x":"NYSE (ADR)","region":"Korea_Taiwan"},
    "UMC":     {"name":"UMC (United Micro)",  "domain":"umc.com",          "x":"NYSE (ADR)","region":"Korea_Taiwan"},
    "3711.TW": {"name":"ASE Technology",      "domain":"aseglobal.com",    "x":"Taipei","region":"Korea_Taiwan"},
    "3008.TW": {"name":"Largan Precision",    "domain":"largan.com.tw",    "x":"Taipei","region":"Korea_Taiwan"},
    # ──── UK — leaders manquants ───────────────────────────────────
    "RR.L":    {"name":"Rolls-Royce",         "domain":"rolls-royce.com",  "x":"London","region":"UK"},
    "AAL.L":   {"name":"Anglo American",      "domain":"angloamerican.com","x":"London","region":"UK"},
    "EXPN.L":  {"name":"Experian",            "domain":"experianplc.com",  "x":"London","region":"UK"},
    "RKT.L":   {"name":"Reckitt Benckiser",   "domain":"reckitt.com",      "x":"London","region":"UK"},
    "LGEN.L":  {"name":"Legal & General",     "domain":"legalandgeneral.com","x":"London","region":"UK"},
    "HLN.L":   {"name":"Haleon (consumer health)","domain":"haleon.com",   "x":"London","region":"UK"},
    # ──── Europe continentale — leaders manquants ──────────────────
    "TEF":     {"name":"Telefónica (ADR)",    "domain":"telefonica.com",   "x":"NYSE (ADR)","region":"Europe"},
    "PHG":     {"name":"Philips (ADR)",       "domain":"philips.com",      "x":"NYSE (ADR)","region":"Europe"},
    "VOLV-B.ST":{"name":"Volvo Group",        "domain":"volvogroup.com",   "x":"Stockholm","region":"Europe"},
    "ERIC-B.ST":{"name":"Ericsson",           "domain":"ericsson.com",     "x":"Stockholm","region":"Europe"},
    "SAND.ST": {"name":"Sandvik",             "domain":"sandvik.com",      "x":"Stockholm","region":"Europe"},
    "ATCO-A.ST":{"name":"Atlas Copco",        "domain":"atlascopco.com",   "x":"Stockholm","region":"Europe"},
    "ARGX.BR": {"name":"Argenx (biotech)",    "domain":"argenx.com",       "x":"Brussels","region":"Europe"},
    "GRF.MC":  {"name":"Grifols (pharma)",    "domain":"grifols.com",      "x":"Madrid","region":"Europe"},
    "AMS.MC":  {"name":"Amadeus IT Group",    "domain":"amadeus.com",      "x":"Madrid","region":"Europe"},
    "AENA.MC": {"name":"Aena (airports)",     "domain":"aena.es",          "x":"Madrid","region":"Europe"},
    "WKL.AS":  {"name":"Wolters Kluwer",      "domain":"wolterskluwer.com","x":"Amsterdam","region":"Europe"},
    "DSFIR.AS":{"name":"DSM-Firmenich",       "domain":"dsm-firmenich.com","x":"Amsterdam","region":"Europe"},
    # ──── Inde — leaders manquants ─────────────────────────────────
    "HCLTECH.NS":{"name":"HCL Technologies",  "domain":"hcl.com",          "x":"NSE","region":"India"},
    "TECHM.NS":{"name":"Tech Mahindra",       "domain":"techmahindra.com", "x":"NSE","region":"India"},
    "DRREDDY.NS":{"name":"Dr. Reddy's Labs",  "domain":"drreddys.com",     "x":"NSE","region":"India"},
    "ASIANPAINT.NS":{"name":"Asian Paints",   "domain":"asianpaints.com",  "x":"NSE","region":"India"},
    "CIPLA.NS":{"name":"Cipla",               "domain":"cipla.com",        "x":"NSE","region":"India"},
    # ──── Canada — leaders manquants ───────────────────────────────
    "RCI-B.TO":{"name":"Rogers Communications","domain":"rogers.com",      "x":"TSX","region":"Canada"},
    "T.TO":    {"name":"Telus",               "domain":"telus.com",        "x":"TSX","region":"Canada"},
    "OTEX.TO": {"name":"OpenText",            "domain":"opentext.com",     "x":"TSX","region":"Canada"},
    "CSU.TO":  {"name":"Constellation Software","domain":"csisoftware.com","x":"TSX","region":"Canada"},
    "L.TO":    {"name":"Loblaw Companies",    "domain":"loblaws.ca",       "x":"TSX","region":"Canada"},
    "BHC.TO":  {"name":"Bausch Health",       "domain":"bauschhealth.com", "x":"TSX","region":"Canada"},
    # ──── Australie — leaders manquants ────────────────────────────
    "XRO.AX":  {"name":"Xero",                "domain":"xero.com",         "x":"ASX","region":"Australia"},
    "WTC.AX":  {"name":"WiseTech Global",     "domain":"wisetechglobal.com","x":"ASX","region":"Australia"},
    "STO.AX":  {"name":"Santos (Aus oil)",    "domain":"santos.com",       "x":"ASX","region":"Australia"},
    # ──── ASEAN — leaders manquants ────────────────────────────────
    "C31.SI":  {"name":"CapitaLand",          "domain":"capitaland.com",   "x":"Singapore","region":"ASEAN"},
    "J36.SI":  {"name":"Jardine Matheson",    "domain":"jardines.com",     "x":"Singapore","region":"ASEAN"},
    "AOT.BK":  {"name":"Airports of Thailand","domain":"airportthai.co.th","x":"Bangkok","region":"ASEAN"},
    "CPALL.BK":{"name":"CP All (7-Eleven Thai)","domain":"cpall.co.th",    "x":"Bangkok","region":"ASEAN"},
    "ASII.JK": {"name":"Astra International", "domain":"astra.co.id",      "x":"Jakarta","region":"ASEAN"},
    # ──── Moyen-Orient — leaders manquants ─────────────────────────
    "2010.SR": {"name":"SABIC (chimie)",      "domain":"sabic.com",        "x":"Tadawul","region":"Middle_East"},
    "7010.SR": {"name":"Saudi Telecom (STC)", "domain":"stc.com.sa",       "x":"Tadawul","region":"Middle_East"},
    "1180.SR": {"name":"Saudi National Bank", "domain":"alahli.com",       "x":"Tadawul","region":"Middle_East"},
    "TEVA":    {"name":"Teva Pharmaceutical (ADR)","domain":"tevapharm.com","x":"NYSE (ADR)","region":"Middle_East"},
    "CHKP":    {"name":"Check Point Software","domain":"checkpoint.com",   "x":"NASDAQ","region":"Middle_East"},
    "NICE":    {"name":"NICE (Israel AI)",    "domain":"nice.com",         "x":"NASDAQ","region":"Middle_East"},
    # ──── LatAm — leaders manquants ────────────────────────────────
    "BBAS3.SA":{"name":"Banco do Brasil",     "domain":"bb.com.br",        "x":"B3","region":"LatAm"},
    "GFNORTEO.MX":{"name":"Banorte",          "domain":"banorte.com",      "x":"Mexico","region":"LatAm"},
    "JBSS3.SA":{"name":"JBS (meat global #1)","domain":"jbs.com.br",       "x":"B3","region":"LatAm"},
    # ──── Robotique Chine — leaders manquants ─────────────────────
    "300124.SZ":{"name":"Inovance Technology","domain":"inovance.com",     "x":"Shenzhen","region":"China"},
    "000333.SZ":{"name":"Midea Group (owns KUKA)","domain":"midea.com",    "x":"Shenzhen","region":"China"},
    "002008.SZ":{"name":"Han's Laser Technology","domain":"hanslaser.net", "x":"Shenzhen","region":"China"},
    "002747.SZ":{"name":"Estun Automation",   "domain":"estun.com",        "x":"Shenzhen","region":"China"},
    "HOLI":    {"name":"Hollysys Automation (ADR)","domain":"hollysys.com","x":"NASDAQ (ADR)","region":"China"},
    # ──── Gaps config identifiés — ajouts finaux ───────────────────
    "DLR":    {"name":"Digital Realty Trust","domain":"digitalrealty.com","x":"NYSE","region":"US"},
    "WELL":   {"name":"Welltower",           "domain":"welltower.com",    "x":"NYSE","region":"US"},
    "VICI":   {"name":"VICI Properties",     "domain":"viciproperties.com","x":"NYSE","region":"US"},
    "0823.HK":{"name":"Link REIT",           "domain":"linkreit.com",     "x":"Hong Kong","region":"China"},
    "URW.AS": {"name":"Unibail-Rodamco-Westfield","domain":"urw.com",     "x":"Amsterdam","region":"Europe"},
    "DD":     {"name":"DuPont de Nemours",   "domain":"dupont.com",       "x":"NYSE","region":"US"},
    "DOW":    {"name":"Dow Inc.",            "domain":"dow.com",          "x":"NYSE","region":"US"},
    "LYB":    {"name":"LyondellBasell",      "domain":"lyondellbasell.com","x":"NYSE","region":"US"},
    "YAR.OL": {"name":"Yara International",  "domain":"yara.com",         "x":"Oslo","region":"Europe"},
    "SDF.DE": {"name":"K+S AG (potash)",     "domain":"kpluss.com",       "x":"XETRA","region":"Europe"},
    "MP":     {"name":"MP Materials (rare earths)","domain":"mpmaterials.com","x":"NYSE","region":"US"},
    "SCCO":   {"name":"Southern Copper",     "domain":"southerncoppercorp.com","x":"NYSE","region":"LatAm"},
    "TECK":   {"name":"Teck Resources",      "domain":"teck.com",         "x":"NYSE (ADR)","region":"Canada"},
    "CLF":    {"name":"Cleveland-Cliffs",    "domain":"clevelandcliffs.com","x":"NYSE","region":"US"},
    "TSN":    {"name":"Tyson Foods",         "domain":"tysonfoods.com",   "x":"NYSE","region":"US"},
    "FMC":    {"name":"FMC Corporation (agri chem)","domain":"fmc.com",   "x":"NYSE","region":"US"},
    "CRSP":   {"name":"CRISPR Therapeutics", "domain":"crisprtx.com",     "x":"NASDAQ","region":"US"},
    "BEAM":   {"name":"Beam Therapeutics",   "domain":"beamtx.com",       "x":"NASDAQ","region":"US"},
    "EBAY":   {"name":"eBay",                "domain":"ebay.com",         "x":"NASDAQ","region":"US"},
    "ETSY":   {"name":"Etsy",                "domain":"etsy.com",         "x":"NASDAQ","region":"US"},
    # ──── Regional Champions (ajouts 2026-04-24 pour combler secteurs mono-région) ─
    # Nucléaire & SMR — hors US
    "EDF.PA":    {"name":"EDF",                 "domain":"edf.fr",          "x":"Paris","region":"Europe"},
    "9503.T":    {"name":"Kansai Electric Power","domain":"kepco.co.jp",    "x":"Tokyo","region":"Japan"},
    "7011.T":    {"name":"Mitsubishi Heavy Industries","domain":"mhi.com",  "x":"Tokyo","region":"Japan"},
    "015760.KS": {"name":"KEPCO",               "domain":"kepco.co.kr",     "x":"Seoul","region":"Korea_Taiwan"},
    "034020.KS": {"name":"Doosan Enerbility",   "domain":"doosanenerbility.com","x":"Seoul","region":"Korea_Taiwan"},
    # REITs — hors US
    "VNA.DE":    {"name":"Vonovia",             "domain":"vonovia.de",      "x":"XETRA","region":"Europe"},
    "URW.PA":    {"name":"Unibail-Rodamco-Westfield","domain":"urw.com",    "x":"Paris","region":"Europe"},
    "LEG.DE":    {"name":"LEG Immobilien",      "domain":"leg-se.com",      "x":"XETRA","region":"Europe"},
    "LAND.L":    {"name":"Land Securities",     "domain":"landsec.com",     "x":"LSE","region":"UK"},
    "SGRO.L":    {"name":"SEGRO",               "domain":"segro.com",       "x":"LSE","region":"UK"},
    "BLND.L":    {"name":"British Land",        "domain":"britishland.com", "x":"LSE","region":"UK"},
    "8802.T":    {"name":"Mitsubishi Estate",   "domain":"mec.co.jp",       "x":"Tokyo","region":"Japan"},
    "8951.T":    {"name":"Nippon Building Fund","domain":"nbf-m.com",       "x":"Tokyo","region":"Japan"},
    "GMG.AX":    {"name":"Goodman Group",       "domain":"goodman.com",     "x":"ASX","region":"Australia"},
    "A17U.SI":   {"name":"Ascendas REIT",       "domain":"capitaland.com",  "x":"Singapore","region":"ASEAN"},
    # Logiciels & IT Services — Japon
    "6702.T":    {"name":"Fujitsu",             "domain":"fujitsu.com",     "x":"Tokyo","region":"Japan"},
    "6701.T":    {"name":"NEC",                 "domain":"nec.com",         "x":"Tokyo","region":"Japan"},
    "9613.T":    {"name":"NTT Data",            "domain":"nttdata.com",     "x":"Tokyo","region":"Japan"},
    "4684.T":    {"name":"Obic",                "domain":"obic.co.jp",      "x":"Tokyo","region":"Japan"},
    # Cybersécurité — Europe
    "HO.PA":     {"name":"Thales",              "domain":"thalesgroup.com", "x":"Paris","region":"Europe"},
    # Biotech — Asie
    "068270.KS": {"name":"Celltrion",           "domain":"celltrion.com",   "x":"Seoul","region":"Korea_Taiwan"},
    "207940.KS": {"name":"Samsung Biologics",   "domain":"samsungbiologics.com","x":"Seoul","region":"Korea_Taiwan"},
    "4507.T":    {"name":"Shionogi",            "domain":"shionogi.com",    "x":"Tokyo","region":"Japan"},
    # ──── Regional Champions batch 2 (2026-04-24) ────────────────────
    # Agriculture & Agroalim — globalisation
    "ADM":       {"name":"Archer Daniels Midland","domain":"adm.com",        "x":"NYSE","region":"US"},
    "BG":        {"name":"Bunge",                "domain":"bunge.com",       "x":"NYSE","region":"US"},
    "CF":        {"name":"CF Industries",        "domain":"cfindustries.com","x":"NYSE","region":"US"},
    "MOS":       {"name":"Mosaic",               "domain":"mosaicco.com",    "x":"NYSE","region":"US"},
    "NTR":       {"name":"Nutrien",              "domain":"nutrien.com",     "x":"NYSE (ADR)","region":"Canada"},
    "BAYN.DE":   {"name":"Bayer",                "domain":"bayer.com",       "x":"XETRA","region":"Europe"},
    "F34.SI":    {"name":"Wilmar International", "domain":"wilmar-international.com","x":"Singapore","region":"ASEAN"},
    "0506.HK":   {"name":"COFCO Joycome",        "domain":"cofco.com",       "x":"Hong Kong","region":"China"},
    # Space Economy — ex-US
    "9348.T":    {"name":"ispace",               "domain":"ispace-inc.com",  "x":"Tokyo","region":"Japan"},
    "ETL.PA":    {"name":"Eutelsat Communications","domain":"eutelsat.com",  "x":"Paris","region":"Europe"},
    "IRDM":      {"name":"Iridium Communications","domain":"iridium.com",    "x":"NASDAQ","region":"US"},
    # Défense & Aérospatial — Asie + Israel
    "012450.KS": {"name":"Hanwha Aerospace",     "domain":"hanwhaaerospace.com","x":"Seoul","region":"Korea_Taiwan"},
    "047810.KS": {"name":"Korea Aerospace Industries","domain":"koreaaero.com","x":"Seoul","region":"Korea_Taiwan"},
    "7013.T":    {"name":"IHI Corp",             "domain":"ihi.co.jp",       "x":"Tokyo","region":"Japan"},
    "7012.T":    {"name":"Kawasaki Heavy Industries","domain":"khi.co.jp",   "x":"Tokyo","region":"Japan"},
    "ESLT":      {"name":"Elbit Systems",        "domain":"elbitsystems.com","x":"NASDAQ (ADR)","region":"Middle_East"},
    # Lithium & Chimie — ex-US/Europe
    "SQM":       {"name":"Sociedad Química y Minera","domain":"sqm.com",     "x":"NYSE (ADR)","region":"LatAm"},
    "002460.SZ": {"name":"Ganfeng Lithium",      "domain":"ganfenglithium.com","x":"Shenzhen","region":"China"},
    "002466.SZ": {"name":"Tianqi Lithium",       "domain":"tianqilithium.com","x":"Shenzhen","region":"China"},
    "PLS.AX":    {"name":"Pilbara Minerals",     "domain":"pilbaraminerals.com.au","x":"ASX","region":"Australia"},
    # Tabac & Spiritueux — Asie
    "2914.T":    {"name":"Japan Tobacco",        "domain":"jt.com",          "x":"Tokyo","region":"Japan"},
    "033780.KS": {"name":"KT&G",                 "domain":"ktng.com",        "x":"Seoul","region":"Korea_Taiwan"},
    "ITC.NS":    {"name":"ITC Limited",          "domain":"itcportal.com",   "x":"NSE","region":"India"},
    # Paris sportifs & Casinos
    "9766.T":    {"name":"Konami Group",         "domain":"konami.com",      "x":"Tokyo","region":"Japan"},
    "6460.T":    {"name":"Sega Sammy Holdings",  "domain":"segasammy.co.jp", "x":"Tokyo","region":"Japan"},
    "ALL.AX":    {"name":"Aristocrat Leisure",   "domain":"aristocrat.com",  "x":"ASX","region":"Australia"},
    # Restauration & Fast Food — Asie
    "7550.T":    {"name":"Zensho Holdings",      "domain":"zensho.co.jp",    "x":"Tokyo","region":"Japan"},
    "3197.T":    {"name":"Skylark Holdings",     "domain":"skylark.co.jp",   "x":"Tokyo","region":"Japan"},
    "JFC.PS":    {"name":"Jollibee Foods",       "domain":"jollibee.com.ph", "x":"Philippines","region":"ASEAN"},
    # Utilities — ex-US/Europe
    "FTS.TO":    {"name":"Fortis",               "domain":"fortisinc.com",   "x":"TSX","region":"Canada"},
    "9501.T":    {"name":"TEPCO (Tokyo Electric)","domain":"tepco.co.jp",    "x":"Tokyo","region":"Japan"},
    "NTPC.NS":   {"name":"NTPC Limited",         "domain":"ntpc.co.in",      "x":"NSE","region":"India"},
    "AGL.AX":    {"name":"AGL Energy",           "domain":"agl.com.au",      "x":"ASX","region":"Australia"},
    # Cybersécurité — Japon
    "4704.T":    {"name":"Trend Micro",          "domain":"trendmicro.com",  "x":"Tokyo","region":"Japan"},
    # Sportswear & Fitness — Asie
    "7936.T":    {"name":"Asics",                "domain":"asics.com",       "x":"Tokyo","region":"Japan"},
    "2020.HK":   {"name":"Anta Sports",          "domain":"anta.com",        "x":"Hong Kong","region":"China"},
    # ──── Regional Champions batch 3 (2026-04-24) ────────────────────
    # Assurance
    "LICI.NS":   {"name":"Life Insurance Corp of India","domain":"licindia.in","x":"NSE","region":"India"},
    "HDFCLIFE.NS":{"name":"HDFC Life Insurance", "domain":"hdfclife.com",    "x":"NSE","region":"India"},
    "SBILIFE.NS":{"name":"SBI Life Insurance",   "domain":"sbilife.co.in",   "x":"NSE","region":"India"},
    "8750.T":    {"name":"Dai-ichi Life Holdings","domain":"dai-ichi-life.co.jp","x":"Tokyo","region":"Japan"},
    "QBE.AX":    {"name":"QBE Insurance Group",  "domain":"qbe.com",         "x":"ASX","region":"Australia"},
    "IAG.AX":    {"name":"Insurance Australia Group","domain":"iag.com.au",  "x":"ASX","region":"Australia"},
    "032830.KS": {"name":"Samsung Life",         "domain":"samsunglife.com", "x":"Seoul","region":"Korea_Taiwan"},
    # Asset Managers & Bourses
    "BN.TO":     {"name":"Brookfield Corporation","domain":"brookfield.com", "x":"TSX","region":"Canada"},
    "BAM":       {"name":"Brookfield Asset Management","domain":"bam.brookfield.com","x":"NYSE (ADR)","region":"Canada"},
    "SLF.TO":    {"name":"Sun Life Financial",   "domain":"sunlife.com",     "x":"TSX","region":"Canada"},
    "0388.HK":   {"name":"HKEX",                 "domain":"hkexgroup.com",   "x":"Hong Kong","region":"China"},
    "S68.SI":    {"name":"Singapore Exchange (SGX)","domain":"sgx.com",      "x":"Singapore","region":"ASEAN"},
    "ASX.AX":    {"name":"ASX Limited",          "domain":"asx.com.au",      "x":"ASX","region":"Australia"},
    # Média & Streaming
    "ITV.L":     {"name":"ITV plc",              "domain":"itv.com",         "x":"LSE","region":"UK"},
    "WPP.L":     {"name":"WPP plc",              "domain":"wpp.com",         "x":"LSE","region":"UK"},
    "ZEEL.NS":   {"name":"Zee Entertainment",    "domain":"zee.com",         "x":"NSE","region":"India"},
    # Voyages & Hôtellerie
    "IHG.L":     {"name":"InterContinental Hotels Group","domain":"ihg.com", "x":"LSE","region":"UK"},
    "INDIGO.NS": {"name":"InterGlobe Aviation (IndiGo)","domain":"goindigo.in","x":"NSE","region":"India"},
    # Énergie propre
    "SSE.L":     {"name":"SSE plc",              "domain":"sse.com",         "x":"LSE","region":"UK"},
    "ADANIGREEN.NS":{"name":"Adani Green Energy","domain":"adanigreenenergy.com","x":"NSE","region":"India"},
    "TATAPOWER.NS":{"name":"Tata Power",         "domain":"tatapower.com",   "x":"NSE","region":"India"},
    # Auto & EV
    "MG.TO":     {"name":"Magna International",  "domain":"magna.com",       "x":"TSX","region":"Canada"},
    # Paris sportifs & Casinos
    "ENT.L":     {"name":"Entain plc",           "domain":"entaingroup.com", "x":"LSE","region":"UK"},
    # Industriels
    "WEGE3.SA":   {"name":"WEG SA",               "domain":"weg.net",         "x":"B3","region":"LatAm"},
    "028260.KS": {"name":"Samsung C&T",          "domain":"samsungcnt.com",  "x":"Seoul","region":"Korea_Taiwan"},
    "267250.KS": {"name":"HD Hyundai Heavy",     "domain":"hd.com",          "x":"Seoul","region":"Korea_Taiwan"},
    # Luxe mondial
    "TITAN.NS":  {"name":"Titan Company (Tata)", "domain":"titancompany.in", "x":"NSE","region":"India"},
    "090430.KS": {"name":"Amorepacific",         "domain":"apgroup.com",     "x":"Seoul","region":"Korea_Taiwan"},
    # Consommation défensive — Asie
    "2503.T":    {"name":"Kirin Holdings",       "domain":"kirinholdings.com","x":"Tokyo","region":"Japan"},
    "2502.T":    {"name":"Asahi Group Holdings", "domain":"asahigroup-holdings.com","x":"Tokyo","region":"Japan"},
    "097950.KS": {"name":"CJ CheilJedang",       "domain":"cj.co.kr",        "x":"Seoul","region":"Korea_Taiwan"},
    "051900.KS": {"name":"LG Household & Health Care","domain":"lghh.co.kr", "x":"Seoul","region":"Korea_Taiwan"},
    # Agriculture & Agroalim — hors US
    "2802.T":    {"name":"Ajinomoto",            "domain":"ajinomoto.com",   "x":"Tokyo","region":"Japan"},
    "2801.T":    {"name":"Kikkoman",             "domain":"kikkoman.com",    "x":"Tokyo","region":"Japan"},
    "ABF.L":     {"name":"Associated British Foods","domain":"abf.co.uk",    "x":"LSE","region":"UK"},
    "BRITANNIA.NS":{"name":"Britannia Industries","domain":"britannia.co.in","x":"NSE","region":"India"},
    "NESTLEIND.NS":{"name":"Nestle India",       "domain":"nestle.in",       "x":"NSE","region":"India"},
    # Fintech & Paiements — Asie
    "7169.T":    {"name":"GMO Payment Gateway",  "domain":"gmo-pg.com",      "x":"Tokyo","region":"Japan"},
    "4385.T":    {"name":"Mercari",              "domain":"mercari.com",     "x":"Tokyo","region":"Japan"},
    "377300.KS": {"name":"KakaoPay",             "domain":"kakaopay.com",    "x":"Seoul","region":"Korea_Taiwan"},
    # Lithium & Chimie — Japon purs chimistes
    "4005.T":    {"name":"Sumitomo Chemical",    "domain":"sumitomo-chem.co.jp","x":"Tokyo","region":"Japan"},
    "4188.T":    {"name":"Mitsubishi Chemical Group","domain":"mcgc.com",    "x":"Tokyo","region":"Japan"},
    "4183.T":    {"name":"Mitsui Chemicals",     "domain":"mitsuichemicals.com","x":"Tokyo","region":"Japan"},
    # Cloud & SaaS — Japon
    "4307.T":    {"name":"Nomura Research Institute","domain":"nri.com",     "x":"Tokyo","region":"Japan"},
    # AI Software & Data — Japon
    "4751.T":    {"name":"CyberAgent",           "domain":"cyberagent.co.jp","x":"Tokyo","region":"Japan"},
    # ──── Métadonnées manquantes (étaient sans nom/favicon, région défaut US) ────
    "AVB":  {"name":"AvalonBay Communities",  "domain":"avalonbay.com",     "x":"NYSE","region":"US"},
    "CCI":  {"name":"Crown Castle",           "domain":"crowncastle.com",   "x":"NYSE","region":"US"},
    "PSA":  {"name":"Public Storage",         "domain":"publicstorage.com", "x":"NYSE","region":"US"},
    "BSX":  {"name":"Boston Scientific",      "domain":"bostonscientific.com","x":"NYSE","region":"US"},
    "EW":   {"name":"Edwards Lifesciences",   "domain":"edwards.com",       "x":"NYSE","region":"US"},
    "MDT":  {"name":"Medtronic",              "domain":"medtronic.com",     "x":"NYSE","region":"US"},
    "SYK":  {"name":"Stryker",                "domain":"stryker.com",       "x":"NYSE","region":"US"},
    "ZTS":  {"name":"Zoetis",                 "domain":"zoetis.com",        "x":"NYSE","region":"US"},
    "ONC":  {"name":"BeOne Medicines",        "domain":"beonemedicines.com","x":"NASDAQ","region":"US"},
    "DG":   {"name":"Dollar General",         "domain":"dollargeneral.com", "x":"NYSE","region":"US"},
    "DLTR": {"name":"Dollar Tree",            "domain":"dollartree.com",    "x":"NASDAQ","region":"US"},
    "ROST": {"name":"Ross Stores",            "domain":"rossstores.com",    "x":"NASDAQ","region":"US"},
    "TJX":  {"name":"TJX Companies",          "domain":"tjx.com",           "x":"NYSE","region":"US"},
    "EA":   {"name":"Electronic Arts",        "domain":"ea.com",            "x":"NASDAQ","region":"US"},
    "TTWO": {"name":"Take-Two Interactive",   "domain":"take2games.com",    "x":"NASDAQ","region":"US"},
    "RBLX": {"name":"Roblox",                 "domain":"roblox.com",        "x":"NYSE","region":"US"},
    "EMR":  {"name":"Emerson Electric",       "domain":"emerson.com",       "x":"NYSE","region":"US"},
    "ETN":  {"name":"Eaton",                  "domain":"eaton.com",         "x":"NYSE","region":"US"},
    "ITW":  {"name":"Illinois Tool Works",    "domain":"itw.com",           "x":"NYSE","region":"US"},
    "PH":   {"name":"Parker Hannifin",        "domain":"parker.com",        "x":"NYSE","region":"US"},
    "PYPL": {"name":"PayPal Holdings",        "domain":"paypal.com",        "x":"NASDAQ","region":"US"},
    "XYZ":  {"name":"Block (ex-Square)",      "domain":"block.xyz",         "x":"NYSE","region":"US"},
    # ──── Afrique (Afrique du Sud) — ADR USD + .JO (cotés en ZAc, /100) ────
    "SSL":    {"name":"Sasol",                "domain":"sasol.com",          "x":"NYSE (ADR)","region":"Afrique"},
    "SBSW":   {"name":"Sibanye-Stillwater",   "domain":"sibanyestillwater.com","x":"NYSE (ADR)","region":"Afrique"},
    "HMY":    {"name":"Harmony Gold",         "domain":"harmony.co.za",      "x":"NYSE (ADR)","region":"Afrique"},
    "NPN.JO": {"name":"Naspers",              "domain":"naspers.com",        "x":"Johannesburg","region":"Afrique"},
    "MTN.JO": {"name":"MTN Group",            "domain":"mtn.com",            "x":"Johannesburg","region":"Afrique"},
    "SBK.JO": {"name":"Standard Bank",        "domain":"standardbank.com",   "x":"Johannesburg","region":"Afrique"},
    "FSR.JO": {"name":"FirstRand",            "domain":"firstrand.co.za",    "x":"Johannesburg","region":"Afrique"},
    "SHP.JO": {"name":"Shoprite",             "domain":"shopriteholdings.co.za","x":"Johannesburg","region":"Afrique"},
    # ──── 2e vague — comblement de cellules secteur×région ────
    "BESI.AS":{"name":"BE Semiconductor",     "domain":"besi.com",           "x":"Amsterdam","region":"Europe"},
    "ASM.AS": {"name":"ASM International",     "domain":"asm.com",            "x":"Amsterdam","region":"Europe"},
    "NEXI.MI":{"name":"Nexi",                 "domain":"nexigroup.com",      "x":"Milan","region":"Europe"},
    "EDEN.PA":{"name":"Edenred",              "domain":"edenred.com",        "x":"Paris","region":"Europe"},
    "PAYTM.NS":{"name":"Paytm (One97)",       "domain":"paytm.com",          "x":"NSE","region":"India"},
    "REL.L":  {"name":"RELX",                 "domain":"relx.com",           "x":"London","region":"UK"},
    "INF.L":  {"name":"Informa",              "domain":"informa.com",        "x":"London","region":"UK"},
    "PSON.L": {"name":"Pearson",              "domain":"pearson.com",        "x":"London","region":"UK"},
    "VIV.PA": {"name":"Vivendi",              "domain":"vivendi.com",        "x":"Paris","region":"Europe"},
    "TER":    {"name":"Teradyne",             "domain":"teradyne.com",       "x":"NASDAQ","region":"US"},
    "CGNX":   {"name":"Cognex",               "domain":"cognex.com",         "x":"NASDAQ","region":"US"},
    "BXB.AX": {"name":"Brambles",             "domain":"brambles.com",       "x":"ASX","region":"Australia"},
    "1177.HK":{"name":"Sino Biopharmaceutical","domain":"sinobiopharm.com",  "x":"Hong Kong","region":"China"},
    # ──── Comblement trous Chine (Cyber + Logiciels) ────
    "601360.SS":{"name":"360 Security",        "domain":"360.cn",            "x":"Shanghai","region":"China"},
    "300454.SZ":{"name":"Sangfor Technologies","domain":"sangfor.com",       "x":"Shenzhen","region":"China"},
    "688111.SS":{"name":"Kingsoft Office (WPS)","domain":"wps.com",          "x":"Shanghai STAR","region":"China"},
    "600588.SS":{"name":"Yonyou Network",      "domain":"yonyou.com",        "x":"Shanghai","region":"China"},
    # ──── Comblement trous Inde ────
    "HAL.NS":   {"name":"Hindustan Aeronautics","domain":"hal-india.co.in",  "x":"NSE","region":"India"},
    "BEL.NS":   {"name":"Bharat Electronics",   "domain":"bel-india.in",     "x":"NSE","region":"India"},
    "MAZDOCK.NS":{"name":"Mazagon Dock",        "domain":"mazagondock.in",   "x":"NSE","region":"India"},
    "JSWSTEEL.NS":{"name":"JSW Steel",          "domain":"jsw.in",           "x":"NSE","region":"India"},
    "TATASTEEL.NS":{"name":"Tata Steel",        "domain":"tatasteel.com",    "x":"NSE","region":"India"},
    "COALINDIA.NS":{"name":"Coal India",        "domain":"coalindia.in",     "x":"NSE","region":"India"},
    "HDFCAMC.NS":{"name":"HDFC Asset Management","domain":"hdfcfund.com",     "x":"NSE","region":"India"},
    "BSE.NS":   {"name":"BSE Ltd",              "domain":"bseindia.com",     "x":"NSE","region":"India"},
    "VBL.NS":   {"name":"Varun Beverages",      "domain":"varunbeverages.com","x":"NSE","region":"India"},
    "UNITDSPR.NS":{"name":"United Spirits",     "domain":"diageoindia.com",  "x":"NSE","region":"India"},
    "BIOCON.NS":{"name":"Biocon",               "domain":"biocon.com",       "x":"NSE","region":"India"},
    "JUBLFOOD.NS":{"name":"Jubilant FoodWorks", "domain":"jubilantfoodworks.com","x":"NSE","region":"India"},
    "PIDILITIND.NS":{"name":"Pidilite Industries","domain":"pidilite.com",   "x":"NSE","region":"India"},
    "SRF.NS":   {"name":"SRF Limited",          "domain":"srf.com",          "x":"NSE","region":"India"},
}


# ─────────────────────────────────────────────────────────────────────────
# NARRATIVES
# Chaque narratif = keywords (pour matcher les news) + liste de tickers
# ─────────────────────────────────────────────────────────────────────────
NARRATIVE_DESC = {
    # ── THÈMES SECTORIELS (axe 1) ──────────────────────────────────
    "Big Tech & Électronique": "Géants tech mondiaux et fabricants d'électronique grand public : les Mag 7 US (Apple, Microsoft, Alphabet, Amazon, Meta, NVIDIA, Tesla) + Big Tech historique (Oracle, IBM, Cisco, Intel, HPE) + électronique (Xiaomi).",
    "AI & Semi-conducteurs":  "Producteurs de puces et infra IA mondial : NVIDIA, AMD, Broadcom, TSMC (ADR + Taipei), ASML, SK Hynix, Samsung, Tokyo Electron, ARM, Super Micro, Shin-Etsu.",
    "Cloud & SaaS":           "Software enterprise et cloud : Microsoft, Oracle, Salesforce, ServiceNow, SAP, Adobe, Snowflake, IBM, Workday, Atlassian.",
    "AI Software & Data":     "Pure-players IA et data platforms : Palantir, Snowflake, Datadog, Cloudflare, C3.ai, MongoDB, Confluent, Elastic — distinct du Cloud/SaaS legacy.",
    "Logiciels & IT Services":"Éditeurs et ESN non-US : Dassault Systèmes, SAP, Capgemini, Sopra Steria, Hexagon, Nemetschek, Temenos, Infineon, Accenture, TCS, Infosys.",
    "Cybersécurité":          "Leaders cybersec cotés : Palo Alto, CrowdStrike, Cloudflare, Fortinet, Zscaler, Okta, SentinelOne, CyberArk.",
    "Banques":                "Grandes banques commerciales et d'investissement mondiales. La géographie est portée par l'axe Région : US (JPM, BAC, Wells, Citi, Goldman, Morgan Stanley), Europe (HSBC, BNP, Deutsche Bank, UBS, Santander, Barclays, ING), Asie (MUFG, SMFG, ICBC, CCB, HDFC, ICICI, SBI, DBS), Pacifique (CBA, Westpac, NAB, ANZ, Macquarie), Canada (RBC, TD, Scotiabank, BMO, CIBC) et Golfe (Al Rajhi, SNB, Emirates NBD).",
    "Assurance":              "Assureurs mondiaux : AIG, MetLife, Travelers, Chubb, Allianz, AXA, Prudential, Munich Re, Zurich, Aviva, Manulife, Sun Life, UnitedHealth.",
    "Asset Managers & Bourses":"Gestionnaires d'actifs, holdings et exchanges : Berkshire Hathaway, BlackRock, Blackstone, KKR, Brookfield, ICE, CME, Deutsche Börse, LSE, Nasdaq, SoftBank.",
    "Défense & Aérospatial":  "Défense/aéro mondial : Lockheed, RTX, General Dynamics, Northrop, Boeing, Airbus, Rheinmetall, BAE, Safran, Leonardo, Thales, Dassault Aviation, Axon.",
    "Space Economy":          "Nouvelle économie spatiale : Rocket Lab, Intuitive Machines, AST SpaceMobile, Planet Labs, BlackSky, Archer Aviation, Joby (eVTOL).",
    "Pétrole & Gaz":          "Majors pétrolières : ExxonMobil, Chevron, Shell, TotalEnergies, BP, Equinor, ConocoPhillips + Petrobras, Reliance.",
    "Énergie propre":         "Solaire/éolien/renouvelables : NextEra, First Solar, Enphase, SolarEdge, Plug, Bloom, Ørsted, Vestas.",
    "Nucléaire & SMR":        "Renaissance du nucléaire (Big Tech deals IA) : Constellation Energy, Vistra, Cameco (uranium), Oklo/NuScale/BWXT (SMR), Centrus (HALEU), Uranium Energy, Energy Fuels.",
    "Utilities":              "Services aux collectivités : Duke Energy, Southern, AEP, Dominion, Enel, Iberdrola, National Grid, Engie.",
    "Métaux & Mines":         "Mineurs de métaux industriels (hors or) : BHP, Rio Tinto, Vale (fer), Freeport-McMoRan (cuivre), ArcelorMittal, Nucor (acier).",
    "Mineurs d'or mondial":   "Producteurs aurifères mondiaux : Newmont, Barrick, Agnico Eagle, Franco-Nevada, Wheaton, Northern Star (Australie), Zijin (Chine).",
    "Lithium & Chimie":       "Lithium (Albemarle, SQM) + gaz industriels (Linde, Air Liquide) — play transition énergétique.",
    "Pharma mondial":         "Géants pharmaceutiques : Lilly, Novo Nordisk, J&J, Merck, Pfizer, AstraZeneca, Roche, Novartis, AbbVie, BMS, GSK, Sanofi, Takeda, Daiichi Sankyo.",
    "Biotech":                "Biotechs pures : Amgen, Gilead, Regeneron, Vertex, Moderna, BioNTech, Illumina, Biogen, Intuitive Surgical.",
    "Luxe mondial":           "Leaders du luxe mondial : LVMH, Kering, Hermès, Richemont, L'Oréal, Burberry, Estée Lauder + Chow Tai Fook (Asie), Moutai (spiritueux premium Chine).",
    "Consommation défensive": "Mega caps consommation : P&G, Coca-Cola, PepsiCo, Walmart, Costco, Nestlé, Unilever, Colgate, Mondelēz, General Mills, Hindustan Unilever.",
    "Restauration & Fast Food":"Chaînes de restauration cotées : McDonald's, Starbucks, Chipotle, Restaurant Brands (Burger King), Yum!, Darden, Domino's, Wendy's, Dutch Bros.",
    "Tabac & Spiritueux":     "Defensive dividends : Philip Morris, Altria, BAT, Diageo, Pernod Ricard, Rémy Cointreau, Constellation Brands, AB InBev.",
    "Retail traditionnel US": "Big-box retailers et discount US : Home Depot, Lowe's, Target, Walmart, Costco, Dollar General, Dollar Tree, TJX, Ross Stores.",
    "Sportswear & Fitness":   "Vêtements et équipement sport + fitness : Nike, Lululemon, Adidas, Puma, Anta (Chine), Planet Fitness, Deckers (UGG/Hoka), On Holding, Crocs, JD Sports.",
    "E-commerce & Retail tech":"E-commerce mondial : Shopify, MercadoLibre, Sea, Amazon + Alibaba, PDD, JD, Meituan + Carvana.",
    "Auto & EV":              "Constructeurs auto classiques + électriques : Tesla, NIO, Rivian, Lucid, Ford, GM, Toyota, VW, Mercedes, Porsche, Ferrari, BYD, Stellantis, Honda, XPeng, Li Auto, Hyundai.",
    "Fintech & Paiements":    "Réseaux de paiement et néo-banques : Visa, Mastercard, Amex, Discover, Adyen, Nu Bank, SoFi, Affirm, Fiserv, Wise, Toast.",
    "Télécoms":               "Opérateurs télécom : Verizon, AT&T, T-Mobile, Vodafone, Deutsche Telekom, Orange, NTT, BT, América Móvil, Bharti Airtel, China Mobile.",
    "Média & Streaming":      "Contenu et streaming : Netflix, Disney, Warner Bros Discovery, Comcast, Spotify, Sony, Tencent.",
    "Voyages & Hôtellerie":   "Tourisme, hôtels, compagnies aériennes : Booking, Marriott, Hilton, Airbnb, Delta, United, Southwest, Ryanair, Lufthansa, Air France.",
    "Paris sportifs & Casinos":"Gaming et casinos cotés : DraftKings, Flutter, Evolution AB, MGM, Las Vegas Sands, Caesars, Galaxy Entertainment (Macau), Wynn Macau.",
    "Industriels":            "Industriels diversifiés : GE, Honeywell, Caterpillar, John Deere, Siemens, 3M, UPS, Union Pacific, Schneider, ABB, Hitachi.",
    "Robotique & Automation": "Robotique industrielle et warehouse : Rockwell, Symbotic, Keyence, FANUC, Yaskawa, Nidec, Kion. Thème IA→physique 2026.",
    "Shipping & Maritime":    "Transport maritime conteneurs + vrac : ZIM, Maersk, Hapag-Lloyd, Mitsui OSK, Kawasaki Kisen, Evergreen + Golden Ocean, Star Bulk, FLEX LNG.",
    "Agriculture & Agroalim": "Commodities alimentaires & intrants : ADM, Bunge, Corteva, Mosaic, Nutrien — exposition cycle agricole mondial.",
    "Quantum Computing":      "Pure-players quantum cotés : IonQ, Rigetti, D-Wave, Quantum Computing Inc., Arqit. Thème spéculatif haute volatilité.",
    "REITs":                  "Immobilier coté (Real Estate Investment Trusts) : Realty Income, Prologis, American Tower, Equinix, Simon Property.",
}

NARRATIVES = {
    # ════════════════════════════════════════════════════════════════
    #  AXE 1 — THÈMES SECTORIELS (axis="theme")
    # ════════════════════════════════════════════════════════════════
    "Big Tech & Électronique": {
        "axis": "theme",
        "icon": "fa-microchip",
        "color": "#0ea5e9",
        "keywords": [
            "mag 7", "mag7", "magnificent seven", "big tech",
            "apple ", "microsoft ", "alphabet", " google ", "amazon ",
            "meta ", "facebook", "nvidia", "tesla",
        ],
        # Mag 7 actuels + leaders historiques Big Tech (IBM/ORCL/CSCO/INTC dominaient
        # le top mcap tech 2010-2017 ; les inclure permet à l'index de refléter la
        # réalité économique du secteur sur 15 ans, pas juste l'ère NVDA/TSLA post-2020).
        # GOOG (class C) retiré : tracker en tandem avec GOOGL (class A) → l'avoir
        # 2× double artificiellement la pondération Alphabet du basket.
        "tickers": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
                    "ORCL", "IBM", "CSCO", "INTC", "HPE", "1810.HK"],
    },
    "AI & Semi-conducteurs": {
        "axis": "theme",
        "icon": "fa-robot",
        "color": "#a78bfa",
        "keywords": [
            "artificial intelligence", " ai ", "ai chip", "semiconductor",
            "semi-conducteur", "nvidia", "amd ", "tsmc", "asml", "broadcom",
            "intel ", "micron", "qualcomm", "arm holdings", "super micro",
            "sk hynix", "samsung electronics", "tokyo electron", "shin-etsu",
            "chatgpt", "anthropic", "openai", "generative ai",
        ],
        "tickers": ["NVDA", "AMD", "TSM", "2330.TW", "ASML.AS", "AVGO", "INTC", "MU",
                    "QCOM", "ARM", "SMCI", "005930.KS", "000660.KS", "8035.T", "IFX.DE",
                    "0981.HK", "1347.HK", "002371.SZ", "300308.SZ",
                    "4063.T", "STM", "MRVL", "ADI", "NXPI", "ANET", "VRT", "DELL", "LRCX", "KLAC", "AMAT", "ON", "MCHP", "TXN", "2454.TW", "2382.TW", "2308.TW", "6920.T", "UMC", "3711.TW", "3008.TW", "BESI.AS", "ASM.AS"],
    },
    "Cloud & SaaS": {
        "axis": "theme",
        "icon": "fa-cloud",
        "color": "#38bdf8",
        "keywords": [
            "cloud computing", "saas ", "software as a service",
            "microsoft azure", "aws ", "google cloud", "salesforce",
            "servicenow", "oracle cloud", "adobe cloud",
        ],
        "tickers": ["MSFT", "ORCL", "CRM", "NOW", "ADBE", "IBM", "WDAY", "TEAM", "TWLO", "SNPS", "CDNS", "CSCO", "INTU", "PRX.AS", "SGE.L", "0268.HK", "OTEX.TO", "CSU.TO", "XRO.AX", "AMS.MC", "BABA", "4307.T"],
    },
    "AI Software & Data": {
        "axis": "theme",
        "icon": "fa-brain",
        "color": "#7c3aed",
        "keywords": [
            "palantir", "snowflake", "datadog", "mongodb", "cloudflare",
            "c3.ai", " c3 ai", "confluent", "elastic nv", "elasticsearch",
            "data platform", "vector database", "rag ",
        ],
        "tickers": ["PLTR", "SNOW", "DDOG", "NET", "MDB", "AI", "CFLT", "ESTC", "TEM", "PATH", "GTLB", "BIDU", "EXPN.L", "WKL.AS", "NICE", "WTC.AX", "GOOGL", "META", "REL.L"],
    },
    "Logiciels & IT Services": {
        "axis": "theme",
        "icon": "fa-code",
        "color": "#6366f1",
        "keywords": [
            "dassault systemes", "dassault systèmes", "3dexperience",
            " sap ag", "sap se", "s/4hana", "capgemini", "sopra steria",
            "nemetschek", "hexagon ab", "temenos", "accenture", "infineon",
            "tata consultancy", " tcs ", "infosys",
            "logiciel européen", "european software", "enterprise software",
            "editeur de logiciels", "cao ", "plm ",
        ],
        "tickers": ["SAP.DE", "DSY.PA", "ACN", "CAP.PA", "SOP.PA",
                    "HEXA-B.ST", "NEM.DE", "TEMN.SW",
                    "TCS.NS", "INFY", "WIPRO.NS", "HCLTECH.NS", "TECHM.NS",
                    "6702.T", "6701.T", "9613.T", "4684.T", "688111.SS", "600588.SS"],
    },
    "Cybersécurité": {
        "axis": "theme",
        "icon": "fa-shield-halved",
        "color": "#ef4444",
        "keywords": [
            "cybersecurity", "cybersécurité", "cyber attack", "ransomware",
            "palo alto networks", "crowdstrike", "fortinet",
            "zscaler", "okta ", "sentinelone", "cyberark",
        ],
        # HO.PA (Thales) retire : cyber = ~15% du CA, primarily defense (deja dans Defense).
        # CYBR (CyberArk) retire : delistee Nov 2024, rachetee par PANW (deja present).
        "tickers": ["PANW", "CRWD", "FTNT", "ZS", "OKTA", "S", "CHKP", "4704.T", "601360.SS", "300454.SZ"],
    },
    "Banques": {
        "axis": "theme",
        "icon": "fa-building-columns",
        "color": "#34d399",
        "keywords": [
            "jpmorgan", "jp morgan", "bank of america", "wells fargo",
            "citigroup", "goldman sachs", "morgan stanley", "us bank", "dimon",
            "hsbc", "bnp paribas", "deutsche bank", "ubs ", "santander",
            "barclays", "intesa", "european bank", "banque",
            "société générale", "credit agricole", "ing group",
            "mitsubishi ufj", " mufg ", "sumitomo mitsui", " smfg ",
            "icbc ", "china construction bank", " ccb ",
            "hdfc bank", "icici bank", "state bank of india", " sbi ",
            "dbs group", "bank earnings", "commercial bank",
        ],
        # Secteur unifié : la géographie est portée par l'axe Région (US / Europe / Asie /
        # Pacifique / Canada / Golfe). Ping An (601318.SS) déplacé vers Assurance (assureur).
        "tickers": [
            # US
            "JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "PNC", "COF", "SCHW",
            "TFC", "MTB", "FITB", "HBAN", "RF", "KEY", "CFG", "STT", "NTRS",
            # Europe
            "HSBA.L", "BNP.PA", "DBK.DE", "UBSG.SW", "SAN.MC", "BARC.L", "ISP.MI",
            "GLE.PA", "ACA.PA", "INGA.AS", "LLOY.L", "NWG.L", "STAN.L", "NDA-FI.HE",
            "UCG.MI", "BBVA",
            # Asie
            "8306.T", "8316.T", "1398.HK", "0939.HK", "HDFCBANK.NS", "ICICIBANK.NS",
            "SBIN.NS", "D05.SI", "0005.HK", "2388.HK", "KOTAKBANK.NS", "AXISBANK.NS",
            "O39.SI", "U11.SI", "1155.KL", "BBCA.JK", "BMRI.JK",
            # Pacifique
            "CBA.AX", "WBC.AX", "NAB.AX", "ANZ.AX", "MQG.AX",
            # Canada
            "RY.TO", "TD.TO", "BNS.TO", "BMO.TO", "CM.TO",
            # Golfe / Moyen-Orient
            "1120.SR", "1180.SR", "EMIRATESNBD.AE",
            # Afrique
            "SBK.JO", "FSR.JO",
        ],
    },
    "Assurance": {
        "axis": "theme",
        "icon": "fa-umbrella",
        "color": "#60a5fa",
        "keywords": [
            "aig ", "allianz",
            "axa ", "prudential", "munich re", "insurer", "insurance company",
            "assureur", "metlife", "travelers insurance", "chubb", "ping an",
        ],
        "tickers": ["AIG", "ALV.DE", "CS.PA", "PRU.L", "MUV2.DE",
                    "MET", "TRV", "CB", "ZURN.SW", "AV.L", "HIG", "PGR", "ALL", "G.MI",
                    "8766.T", "MFC.TO", "2318.HK", "601318.SS", "LGEN.L",
                    "LICI.NS", "HDFCLIFE.NS", "SBILIFE.NS", "8750.T", "QBE.AX", "IAG.AX", "032830.KS",
                    "SLF.TO", "UNH"],
    },
    "Asset Managers & Bourses": {
        "axis": "theme",
        "icon": "fa-chart-pie",
        "color": "#818cf8",
        "keywords": [
            "blackrock", "blackstone", "kkr ", "intercontinental exchange",
            "cme group", "asset manager", "private equity", "larry fink",
            "softbank group", "berkshire hathaway", "buffett",
        ],
        "tickers": ["BLK", "BX", "KKR", "ICE", "CME", "DB1.DE",
                    "LSEG.L", "NDAQ", "9984.T", "AMP", "RJF", "TROW", "IVZ", "BEN", "8601.T",
                    "BN.TO", "BAM", "BRK-B", "0388.HK", "S68.SI", "ASX.AX", "HDFCAMC.NS", "BSE.NS"],
    },
    "Défense & Aérospatial": {
        "axis": "theme",
        "icon": "fa-shield",
        "color": "#94a3b8",
        "keywords": [
            "lockheed", "raytheon", " rtx ", "general dynamics", "northrop",
            "boeing", "airbus", "rheinmetall", "bae systems", "safran",
            "leonardo defense", "thales group", "dassault aviation",
            "axon enterprise", "defense stocks", "actions défense",
            "aerospace", "aéronautique",
        ],
        "tickers": ["LMT", "RTX", "GD", "NOC", "BA", "AIR.PA", "RHM.DE",
                    "BA.L", "SAF.PA", "LDO.MI", "AM.PA", "HO.PA", "HII", "HEI", "AXON", "RR.L",
                    "012450.KS", "047810.KS", "7013.T", "7012.T", "ESLT", "ACHR", "JOBY", "HAL.NS", "BEL.NS", "MAZDOCK.NS"],
    },
    "Space Economy": {
        "axis": "theme",
        "icon": "fa-rocket",
        "color": "#8b5cf6",
        "keywords": [
            "spacex", "starlink", "rocket lab", "intuitive machines", "ast spacemobile",
            "planet labs", "blacksky", "archer aviation", "joby aviation",
            "space economy", "space launch", "satellite constellation",
            "evtol ", "commercial space",
        ],
        "tickers": ["SPCX", "RKLB", "LUNR", "ASTS", "PL", "BKSY",
                    "IRDM", "ETL.PA", "9348.T"],
    },
    "Pétrole & Gaz": {
        "axis": "theme",
        "icon": "fa-oil-well",
        "color": "#f59e0b",
        "keywords": [
            "exxon", "exxonmobil", "chevron", "shell", "totalenergies",
            " bp ", "equinor", "conocophillips", "opec", "oil major",
            "crude oil", "brent", "wti", "petrobras", "reliance industries",
        ],
        "tickers": ["XOM", "CVX", "SHEL", "TTE.PA", "BP.L", "COP",
                    "EOG", "MPC", "PSX", "VLO", "SLB", "HAL", "PBR", "RELIANCE.NS", "ENI.MI", "EQNR.OL", "CNQ", "SU.TO", "ENB.TO", "TRP.TO", "2222.SR", "601857.SS", "0386.HK", "0883.HK", "PTT.BK", "VIST", "1605.T", "STO.AX", "SSL"],
    },
    "Énergie propre": {
        "axis": "theme",
        "icon": "fa-solar-panel",
        "color": "#22d3ee",
        "keywords": [
            "nextera", "first solar", "enphase", "ørsted", "orsted",
            "solar stock", "wind energy", "renewable energy",
            "clean energy", "énergie propre", "énergie renouvelable",
        ],
        # 1211.HK (BYD) retire : fondamentalement un constructeur auto (deja dans Auto & EV).
        "tickers": ["NEE", "FSLR", "ENPH", "SEDG", "PLUG", "BE",
                    "ORSTED.CO", "VWS.CO", "0968.HK", "300750.SZ", "373220.KS", "051910.KS", "006400.KS", "2082.SR", "601012.SS",
                    "SSE.L", "ADANIGREEN.NS", "TATAPOWER.NS"],
    },
    "Nucléaire & SMR": {
        "axis": "theme",
        "icon": "fa-atom",
        "color": "#eab308",
        "keywords": [
            "constellation energy", "vistra corp", "cameco", "uranium price",
            "small modular reactor", " smr ", "nuscale", "oklo reactor",
            " oklo ", "bwx technologies", "centrus energy", "haleu",
            "uranium energy corp", "energy fuels", "nuclear renaissance",
            "nuclear power", "ai nuclear deal", "datacenter nuclear",
            "three mile island", "reactor restart",
        ],
        # EDF.PA retire : nationalisee 2023, retiree de la cote, plus de successeur public.
        "tickers": ["CEG", "VST", "CCJ", "OKLO", "SMR", "BWXT",
                    "LEU", "UEC", "UUUU", "1816.HK",
                    "9503.T", "7011.T", "015760.KS", "034020.KS"],
    },
    "Utilities": {
        "axis": "theme",
        "icon": "fa-plug",
        "color": "#0d9488",
        "keywords": [
            "duke energy", "southern company", " aep ",
            "dominion energy", " enel ", "iberdrola",
            "national grid", "engie ", "utility stock",
            "power company", "electric utility",
        ],
        "tickers": ["DUK", "SO", "AEP", "D", "ENEL.MI", "IBE.MC", "NG.L", "ENGI.PA", "600900.SS",
                    "FTS.TO", "9501.T", "NTPC.NS", "AGL.AX"],
    },
    "Métaux & Mines": {
        "axis": "theme",
        "icon": "fa-industry",
        "color": "#a3e635",
        "keywords": [
            " bhp ", "rio tinto", " vale ", "freeport-mcmoran",
            "freeport ", "arcelormittal", " nucor ", "copper price",
            "cuivre", "iron ore", "fer minéral", "mining stock",
            "mineur", "steel price", "acier",
        ],
        "tickers": ["VALE", "FCX", "MT", "NUE", "GLEN.L", "BHP.AX", "RIO.AX", "FMG.AX", "ADANIENT.NS", "GMEXICOB.MX", "1088.HK", "005490.KS", "AAL.L", "MP", "SCCO", "TECK", "CLF", "JSWSTEEL.NS", "TATASTEEL.NS", "COALINDIA.NS"],
    },
    "Mineurs d'or mondial": {
        "axis": "theme",
        "icon": "fa-coins",
        "color": "#fbbf24",
        "keywords": [
            "gold miner", "mineur d'or", "newmont", "barrick gold",
            "agnico eagle", "franco-nevada", "wheaton",
            "northern star resources", "zijin mining",
            "gold price", "prix de l'or",
        ],
        # NCM.AX retire : delistee Nov 2023, rachetee par NEM (deja present).
        "tickers": ["NEM", "GOLD", "AEM", "FNV", "WPM", "NST.AX", "0601.HK",
                    "KGC", "EVN.AX", "GFI", "AU", "SBSW", "HMY"],
    },
    "Lithium & Chimie": {
        "axis": "theme",
        "icon": "fa-flask",
        "color": "#65a30d",
        "keywords": [
            "albemarle", " sqm ", "lithium price", "battery metals",
            "linde ", "air liquide", "industrial gas", "gaz industriel",
            "rare earths", "terres rares",
        ],
        "tickers": ["ALB", "SQM", "LIN", "AI.PA", "BAS.DE", "2010.SR", "DSFIR.AS", "DD", "DOW", "LYB", "YAR.OL", "SDF.DE",
                    "002460.SZ", "002466.SZ", "PLS.AX",
                    "4005.T", "4188.T", "4183.T", "PIDILITIND.NS", "SRF.NS", "ASIANPAINT.NS"],
    },
    "Pharma mondial": {
        "axis": "theme",
        "icon": "fa-pills",
        "color": "#f472b6",
        "keywords": [
            "eli lilly", " lilly ", "novo nordisk", "johnson & johnson",
            " merck", " pfizer", "astrazeneca", " roche ", "novartis",
            "abbvie", "glp-1", "ozempic", "wegovy", "mounjaro",
            "pharma stock", "takeda ", "daiichi sankyo", "sanofi",
        ],
        # Ajouts healthcare US majeurs absents :
        #   UNH (UnitedHealth, premier mcap mondial healthcare ~$500B)
        #   MDT (Medtronic), SYK (Stryker), BSX (Boston Scientific), EW (Edwards) — medtech leaders
        #   ZTS (Zoetis, animal health leader)
        "tickers": ["LLY", "NVO", "JNJ", "MRK", "PFE", "AZN.L",
                    "ROG.SW", "NOVN.SW", "ABBV", "BMY", "GSK.L", "SAN.PA",
                    "MDT", "SYK", "BSX", "EW", "ZTS",
                    "4502.T", "4568.T", "DHR", "TMO", "ABT", "ATLN.SW", "LONN.SW", "UCB.BR", "FME.DE", "CSL.AX", "SUNPHARMA.NS",
                    "2269.HK", "2359.HK", "600276.SS", "4503.T", "4523.T", "PHG", "TEVA", "DRREDDY.NS", "CIPLA.NS", "GRF.MC", "BHC.TO", "1177.HK"],
    },
    "Biotech": {
        "axis": "theme",
        "icon": "fa-dna",
        "color": "#d946ef",
        "keywords": [
            "amgen", "gilead", "regeneron", " vertex pharm",
            "moderna", "biontech", "illumina", "biogen",
            "biotech stock", "gene therapy", "mrna vaccine",
            "fda approval", "clinical trial", "intuitive surgical",
        ],
        # BGNE remplace par ONC : BeiGene rebrandee en BeOne Medicines, nouveau ticker NASDAQ.
        "tickers": ["AMGN", "GILD", "REGN", "VRTX", "MRNA", "BNTX",
                    "ILMN", "BIIB", "ONC", "1801.HK", "ARGX.BR", "CRSP", "BEAM",
                    "068270.KS", "207940.KS", "4507.T", "BIOCON.NS"],
    },
    "Luxe mondial": {
        "axis": "theme",
        "icon": "fa-gem",
        "color": "#c084fc",
        "keywords": [
            "lvmh", "louis vuitton", "hennessy", " moët ", " moet ",
            "kering", "gucci", "yves saint laurent",
            "hermès", "hermes ", "birkin", "kelly bag",
            "richemont", "cartier", "van cleef",
            "l'oréal", "loreal", "rouge",
            "chow tai fook", "kweichow moutai", " moutai ",
            "luxury sector", "luxe européen", "luxe asiatique",
        ],
        "tickers": ["MC.PA", "KER.PA", "RMS.PA", "CFR.SW", "OR.PA", "BRBY.L", "EL",
                    "1929.HK", "RL", "TPR", "CPRI", "PVH", "MC.SW", "MONC.MI", "ITX.MC", "EL.PA", "4911.T",
                    "TITAN.NS", "090430.KS"],
    },
    "Consommation défensive": {
        "axis": "theme",
        "icon": "fa-basket-shopping",
        "color": "#10b981",
        "keywords": [
            "procter", "gamble", "coca-cola", " coca ", "pepsi",
            "walmart", "costco", "nestlé", "nestle", "unilever",
            "hindustan unilever",
            "consumer defensive", "staples",
        ],
        "tickers": ["PG", "KO", "PEP", "WMT", "COST", "NESN.SW", "UNA.AS",
                    "CL", "KMB", "MDLZ", "GIS", "HINDUNILVR.NS", "CVS", "ULVR.L", "AD.AS", "ITC.NS", "KOF", "FMX", "WOW.AX", "WES.AX",
                    "600887.SS", "2319.HK", "RKT.L", "HLN.L", "L.TO",
                    "2503.T", "2502.T", "097950.KS", "051900.KS", "SHP.JO"],
    },
    "Restauration & Fast Food": {
        "axis": "theme",
        "icon": "fa-burger",
        "color": "#f97316",
        "keywords": [
            "mcdonald's", "mcdonalds ", " mcd ", "starbucks", "chipotle",
            "restaurant brands", "burger king", "tim hortons",
            "yum brands", " kfc ", "pizza hut", "taco bell",
            "darden restaurants", "olive garden", "domino's", "dominos pizza",
            "wendy's", "wendys ", "fast food", "restaurant chain",
            "quick service restaurant", "dutch bros",
        ],
        "tickers": ["MCD", "SBUX", "CMG", "QSR", "YUM", "DRI", "DPZ", "WEN", "BROS", "YUMC", "6862.HK",
                    "7550.T", "3197.T", "JFC.PS", "JUBLFOOD.NS"],
    },
    "Tabac & Spiritueux": {
        "axis": "theme",
        "icon": "fa-wine-glass",
        "color": "#be185d",
        "keywords": [
            "philip morris", "altria ", "british american tobacco", " bat ",
            "iqos ", "heated tobacco", "vape ", "zyn ", "nicotine pouch",
            "diageo ", "johnnie walker", "guinness ", "pernod ricard",
            "remy cointreau", "rémy cointreau", "constellation brands",
            "corona beer", "ab inbev", "anheuser-busch", "budweiser",
            "spirits sector", "tobacco stock",
        ],
        # DEO (ADR US) retire : doublon avec DGE.L (cotation primaire Diageo Londres).
        "tickers": ["PM", "MO", "BTI", "DGE.L", "RI.PA", "RCO.PA", "STZ", "BUD", "CARL-B.CO", "HEIA.AS", "CPR.MI",
                    "0168.HK", "0291.HK", "000858.SZ",
                    "2914.T", "033780.KS", "600519.SS", "VBL.NS", "UNITDSPR.NS"],
    },
    "Retail traditionnel US": {
        "axis": "theme",
        "icon": "fa-shop",
        "color": "#14b8a6",
        "keywords": [
            "home depot", "lowe's", "lowes ", "target ", "best buy",
            "ross stores", "tj maxx", "home improvement", "big box retailer",
        ],
        # 3382.T (Seven & i Japon) et CPALL.BK (CP All Thailande) retires : pas US.
        # Ajouts US : DG, DLTR, TJX, ROST (discount + off-price retailers majeurs).
        "tickers": ["HD", "LOW", "TGT", "WMT", "COST", "DG", "DLTR", "TJX", "ROST"],
    },
    "E-commerce & Retail tech": {
        "axis": "theme",
        "icon": "fa-cart-shopping",
        "color": "#06b6d4",
        "keywords": [
            "shopify", "mercadolibre", "sea limited",
            "e-commerce", "online shopping", "marketplace",
            "alibaba e-commerce", "pinduoduo", " pdd ", " jd.com",
            "meituan ", "carvana ",
        ],
        # 9618.HK retire : doublon avec JD (ADR US, primary listing).
        # 1810.HK (Xiaomi) retire : smartphones + EV, pas e-commerce (deja dans Auto & EV).
        "tickers": ["SHOP", "MELI", "SE", "BABA", "PDD", "JD", "3690.HK", "CVNA", "RELIANCE.NS", "CPNG", "AMZN", "EBAY", "ETSY", "NPN.JO", "4385.T"],
    },
    "Auto & EV": {
        "axis": "theme",
        "icon": "fa-car",
        "color": "#fb923c",
        "keywords": [
            "tesla ", " ev ", "electric vehicle", "véhicule électrique",
            "nio ", "rivian", "lucid motors", " ford ", "general motors",
            "toyota", "volkswagen", " vw ", "mercedes", "porsche",
            "ferrari", " byd ", "auto manufacturer", "hyundai motor",
            "stellantis ", "honda motor",
        ],
        # 002594.SZ retire : doublon avec 1211.HK (BYD HK = primary listing).
        "tickers": ["TSLA", "NIO", "RIVN", "LCID", "F", "GM", "7203.T",
                    "VOW3.DE", "RACE", "MBG.DE", "P911.DE", "1211.HK",
                    "STLA", "HMC", "XPEV", "LI", "005380.KS", "RNO.PA", "7267.T", "7201.T", "6902.T", "CON.DE", "MARUTI.NS", "TATAMOTORS.NS", "BAJAJ-AUTO.NS", "DTG.DE", "000270.KS", "VOLV-B.ST", "ASII.JK", "MG.TO"],
    },
    "Fintech & Paiements": {
        "axis": "theme",
        "icon": "fa-credit-card",
        "color": "#0891b2",
        "keywords": [
            " visa ", "mastercard", "adyen", "nu holdings",
            "nubank", " sofi ", "affirm", "fiserv", "wise ",
            "payment processor", "bnpl", "buy now pay later",
            "fintech stock", "paiements électronique", "carte bancaire",
            "toast inc", "american express", "discover financial",
        ],
        # 0700.HK (Tencent) retire : primarily Media/Gaming, WeChat Pay <20% rev (deja dans Media).
        # DFS (Discover) retire : delistee May 2025, rachetee par COF (deja dans Banques US).
        # Ajouts : PYPL (PayPal) et XYZ (Block, ex-Square) — fintech US majeurs absents.
        "tickers": ["V", "MA", "AXP", "PYPL", "XYZ", "ADYEN.AS", "NU", "SOFI",
                    "AFRM", "FI", "WISE.L", "TOST", "BAJFINANCE.NS", "LU", "FUTU",
                    "7169.T", "377300.KS", "NEXI.MI", "EDEN.PA", "PAYTM.NS"],
    },
    "Télécoms": {
        "axis": "theme",
        "icon": "fa-tower-cell",
        "color": "#6366f1",
        "keywords": [
            " verizon", " at&t", " t-mobile", "vodafone",
            "deutsche telekom", "orange sa", "ntt data",
            "bt group", "telecom stock", "5g rollout",
            "bharti airtel", "china mobile", "américa móvil", "america movil",
        ],
        "tickers": ["VZ", "T", "TMUS", "VOD.L", "DTE.DE", "ORAN", "NTT",
                    "BT-A.L", "AMX", "BHARTIARTL.NS", "0941.HK", "9433.T", "9434.T", "TLS.AX", "Z74.SI", "ADVANC.BK", "EAND.AE", "BCE.TO", "017670.KS", "TEF", "ERIC-B.ST", "RCI-B.TO", "T.TO", "7010.SR", "MTN.JO"],
    },
    "Média & Streaming": {
        "axis": "theme",
        "icon": "fa-film",
        "color": "#ec4899",
        "keywords": [
            "netflix", "disney", "warner bros", "comcast", "spotify",
            "streaming wars", "cord cutting", "content studio",
            "tencent ", "sony entertainment",
        ],
        # 9999.HK retire : doublon avec NTES (NetEase ADR US, primary listing).
        # Ajouts gaming US : EA, TTWO (Take-Two), RBLX (Roblox).
        "tickers": ["NFLX", "DIS", "WBD", "CMCSA", "SPOT", "6758.T", "0700.HK", "PUB.PA", "4661.T", "9626.HK", "3888.HK", "035420.KS", "035720.KS", "NTES", "EA", "TTWO", "RBLX", "7974.T", "7832.T", "9684.T",
                    "ITV.L", "WPP.L", "ZEEL.NS", "INF.L", "PSON.L", "VIV.PA", "4751.T"],
    },
    "Voyages & Hôtellerie": {
        "axis": "theme",
        "icon": "fa-plane",
        "color": "#f59e0b",
        "keywords": [
            "booking holdings", "marriott", "hilton worldwide",
            "airbnb", "delta airlines", "united airlines",
            "southwest airlines", "ryanair", "lufthansa",
            "air france", " klm ", "airline stock",
            "travel sector", "hospitality stock",
        ],
        "tickers": ["BKNG", "MAR", "HLT", "ABNB", "DAL", "UAL", "LUV",
                    "RYAAY", "LHA.DE", "AF.PA", "9202.T", "9201.T", "QAN.AX", "C6L.SI", "TCOM", "0753.HK", "AENA.MC", "AOT.BK",
                    "IHG.L", "INDIGO.NS"],
    },
    "Paris sportifs & Casinos": {
        "axis": "theme",
        "icon": "fa-dice",
        "color": "#e11d48",
        "keywords": [
            "draftkings", "flutter entertainment", "evolution ab",
            "sports betting", "online gambling", "mgm resorts",
            "las vegas sands", "caesars entertainment", "wynn resorts",
            "galaxy entertainment", "macau casino", "igaming ",
        ],
        "tickers": ["DKNG", "FLUT", "MGM", "LVS", "CZR", "EVO.ST",
                    "0027.HK", "1128.HK",
                    "9766.T", "6460.T", "ALL.AX", "ENT.L"],
    },
    "Industriels": {
        "axis": "theme",
        "icon": "fa-gears",
        "color": "#84cc16",
        "keywords": [
            " ge aerospace", "general electric", "honeywell",
            "caterpillar", "john deere", "siemens",
            "industrial stock", " 3m ", "union pacific",
            "schneider electric", " abb ", "hitachi",
        ],
        # Ajouts industriels US majeurs absents : ETN (Eaton), EMR (Emerson), PH (Parker Hannifin), ITW.
        "tickers": ["GE", "HON", "CAT", "DE", "SIE.DE", "MMM", "UPS", "UNP", "ETN", "EMR", "PH", "ITW",
                    "SU.PA", "ABBN.SW", "6501.T", "ROR.L", "RTO.L", "SIKA.SW", "KNEBV.HE", "CRH.L", "FER.MC", "8001.T", "8031.T", "LT.NS", "CNR.TO", "CP.TO", "WCN.TO",
                    "1766.HK", "3808.HK", "601668.SS", "1157.HK", "6301.T", "SAND.ST", "ATCO-A.ST", "J36.SI",
                    "WEGE3.SA", "028260.KS", "267250.KS", "BXB.AX", "CX", "000333.SZ"],
    },
    "Robotique & Automation": {
        "axis": "theme",
        "icon": "fa-robot",
        "color": "#0ea5e9",
        "keywords": [
            "rockwell automation", "symbotic ", "keyence ", "fanuc ",
            "yaskawa", "nidec ", "kion group", "industrial robot",
            "warehouse automation", "factory automation", "robotique industrielle",
        ],
        # HOLI retire : passee en privee 2024 (Recco Control), plus de successeur public.
        "tickers": ["ROK", "SYM", "6861.T", "6954.T", "6506.T", "6594.T", "KGX.DE",
                    "300124.SZ", "002008.SZ", "002747.SZ", "TER", "CGNX", "ISRG"],
    },
    "Shipping & Maritime": {
        "axis": "theme",
        "icon": "fa-ship",
        "color": "#0369a1",
        "keywords": [
            " zim ", "zim integrated", "maersk", "hapag-lloyd",
            "mitsui osk", " mol ", "kawasaki kisen", " k-line",
            "evergreen marine", "container shipping", "shipping rates",
            "baltic dry index", "dry bulk", "lng carrier",
            "golden ocean", "star bulk", "flex lng",
        ],
        "tickers": ["ZIM", "GOGL", "SBLK", "FLNG", "MAERSK-B.CO", "1919.HK",
                    "HLAG.DE", "9104.T", "9107.T", "2603.TW"],
    },
    "Agriculture & Agroalim": {
        "axis": "theme",
        "icon": "fa-wheat-awn",
        "color": "#ca8a04",
        "keywords": [
            "archer daniels midland", " adm ", " bunge ", "corteva",
            "mosaic company", "nutrien ", "fertilizer", "potash",
            "seed company", "crop prices", "wheat price", "soybean",
            "agri-food", "agroalimentaire", "agricultural stock",
        ],
        "tickers": ["ADM", "BG", "CTVA", "MOS", "NTR", "JBSS3.SA", "TSN", "FMC",
                    "CF", "BAYN.DE", "F34.SI", "0506.HK",
                    "2802.T", "2801.T", "ABF.L", "BRITANNIA.NS", "NESTLEIND.NS"],
    },
    "Quantum Computing": {
        "axis": "theme",
        "icon": "fa-atom",
        "color": "#a855f7",
        "keywords": [
            "quantum computing", "quantum supremacy", "quantum advantage",
            "qubit ", "qubits ", "ionq ", "rigetti", "d-wave", "dwave",
            "quantum computing inc", "arqit quantum", "quantinuum",
            "topological qubit", "willow quantum", "quantum chip",
            "quantum hardware", "quantum error correction",
        ],
        "tickers": ["IONQ", "RGTI", "QBTS", "QUBT", "ARQQ", "688027.SS"],
    },
    "REITs": {
        "axis": "theme",
        "icon": "fa-building",
        "color": "#a3e635",
        "keywords": [
            "realty income", "prologis", "american tower",
            "equinix", "simon property", "data center reit",
            "reit ", "real estate investment trust",
        ],
        # URW.AS retire : doublon avec URW.PA (Unibail-Rodamco-Westfield, Paris primary).
        # Ajouts US REITs majeurs absents : CCI (Crown Castle, towers), PSA (Public Storage), AVB (AvalonBay, residential).
        "tickers": ["O", "PLD", "AMT", "CCI", "PSA", "AVB", "EQIX", "SPG", "N2IU.SI", "C31.SI", "DLR", "WELL", "VICI", "0823.HK",
                    "VNA.DE", "URW.PA", "LEG.DE", "LAND.L", "SGRO.L", "BLND.L",
                    "8802.T", "8951.T", "GMG.AX", "A17U.SI"],
    },
    "Sportswear & Fitness": {
        "axis": "theme",
        "icon": "fa-shoe-prints",
        "color": "#ef4444",
        "keywords": [
            "nike ", "lululemon", "lulu ", "adidas", "puma ",
            "anta sports", "deckers", "ugg boots", "hoka",
            "on running", "on holding", "crocs", "jd sports",
            "planet fitness", "sportswear", "athleisure",
            "fitness chain", "yoga apparel", "activewear",
        ],
        "tickers": ["NKE", "LULU", "ADS.DE", "PUM.DE", "2020.HK",
                    "PLNT", "DECK", "ONON", "CROX", "JD.L", "7936.T"],
    },

}


# ─────────────────────────────────────────────────────────────────────────
# HTTP helper (for BTC-equivalent trend filter = S&P 500 history)
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
# NEWS SCAN (reuses news_cache.json 'macro' section)
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
# STOCK PRICES via yfinance
# ─────────────────────────────────────────────────────────────────────────
def fetch_stocks(symbols, fx_rates=None):
    """Fetch snapshot quotes. If fx_rates is provided, convert price/mcap/perf to USD."""
    fx_rates = fx_rates or {}
    if not symbols:
        return {}
    try:
        import yfinance as yf
    except ImportError:
        print("[error] yfinance not installed", file=sys.stderr)
        return {}

    out = {}

    # yf.download with 700+ symbols at once hits Yahoo's silent drop limit.
    # Chunk + process each chunk's DataFrame directly (no cross-chunk concat that
    # can produce weird MultiIndex behavior). Retry per chunk with backoff.
    CHUNK = 50
    CHUNK_SLEEP = 1.2
    import time as _time
    total_chunks = (len(symbols) + CHUNK - 1) // CHUNK

    def _process_chunk_df(h, batch):
        """Process a single chunk's DataFrame: extract quotes per symbol into out[]."""
        processed = 0
        for sym in batch:
            try:
                if hasattr(h.columns, "get_level_values") and sym in h.columns.get_level_values(0):
                    sub = h[sym]
                    closes = sub["Close"].dropna() if "Close" in sub.columns else None
                    vols   = sub.get("Volume")
                elif len(batch) == 1:
                    closes = h["Close"].dropna() if "Close" in h.columns else None
                    vols   = h.get("Volume")
                else:
                    continue  # symbol not in this DataFrame
                if closes is None or len(closes) < 2:
                    print(f"[warn] {sym}: no data", file=sys.stderr)
                    continue

                ccy = detect_currency(sym)
                dates = [d.strftime("%Y-%m-%d") for d in closes.index]
                closes_local = list(closes.values)
                closes_usd = [apply_fx(p, ccy, fx_rates, date_str=d, symbol=sym)
                              for p, d in zip(closes_local, dates)]

                # Skip ticker if FX missing (apply_fx returned None anywhere in series).
                # Falls through to silent passthrough was the root cause of the
                # IDR mcap explosion (BBCA.JK contributing 731k B$ to Banques Asie).
                if any(c is None for c in closes_usd):
                    print(f"[skip] {sym}: FX rate missing for {ccy} — ticker excluded", file=sys.stderr)
                    continue

                last = float(closes_usd[-1])
                p7   = float(closes_usd[-6])  if len(closes_usd) >= 6  else None
                p30  = float(closes_usd[-22]) if len(closes_usd) >= 22 else None
                perf_7d  = ((last / p7)  - 1) * 100 if p7  and p7 > 0  else None
                perf_30d = ((last / p30) - 1) * 100 if p30 and p30 > 0 else None

                vol_usd = 0
                try:
                    if vols is not None and len(vols) > 0:
                        v = float(vols.dropna().iloc[-1])
                        vol_usd = v * float(closes_local[-1]) * (last / float(closes_local[-1]) if closes_local[-1] > 0 else 1)
                except Exception:
                    pass

                mcap_local = 0
                try:
                    fi = yf.Ticker(sym).fast_info
                    mcap_local = float(getattr(fi, "market_cap", 0) or 0)
                except Exception:
                    pass
                mcap_usd = (apply_fx(mcap_local, ccy, fx_rates, symbol=sym) or 0) if mcap_local else 0

                meta = STOCKS.get(sym, {})
                domain = meta.get("domain", "")
                out[sym] = {
                    "symbol": sym,
                    "name": meta.get("name", sym),
                    "domain": domain,
                    "exchange": meta.get("x", ""),
                    "currency": ccy,
                    "image":          "",
                    "image_fallback": "",
                    "price": last,
                    "price_local": float(closes_local[-1]),
                    "mcap": mcap_usd,
                    "volume_usd": vol_usd,
                    "perf_7d": perf_7d,
                    "perf_30d": perf_30d,
                }
                processed += 1
            except Exception as e:
                print(f"[warn] {sym}: {e}", file=sys.stderr)
        return processed

    for i in range(0, len(symbols), CHUNK):
        batch = symbols[i:i + CHUNK]
        chunk_n = i // CHUNK + 1
        # Budget epuise → on sort et on laisse la phase d'ecriture se faire.
        # Les chunks non traites seront gap-filles depuis le cache precedent.
        _elapsed = _time.time() - _RUN_START
        if _elapsed > QUOTE_PHASE_BUDGET_S:
            remaining = len(symbols) - i
            print(f"[budget] phase quotes stoppee a {_elapsed/60:.1f} min "
                  f"(budget {QUOTE_PHASE_BUDGET_S/60:.0f} min) — chunks {chunk_n}/{total_chunks} "
                  f"a {total_chunks}/{total_chunks} non fetches, {remaining} tickers "
                  f"repris du cache precedent (_stale). On passe a l'ecriture.",
                  file=sys.stderr)
            break
        got_batch = None
        for attempt in range(4):
            try:
                h = yf.download(" ".join(batch), period="60d", interval="1d",
                                group_by="ticker", progress=False, threads=True,
                                auto_adjust=False)
                ok = 0
                if h is not None and not h.empty and hasattr(h.columns, "get_level_values"):
                    top = set(h.columns.get_level_values(0))
                    ok = sum(1 for s in batch if s in top)
                elif h is not None and not h.empty:
                    ok = 1
                if ok >= max(int(len(batch) * 0.5), 3):
                    got_batch = h
                    break
                wait = 5 * (attempt + 1) ** 2
                print(f"[warn] chunk {chunk_n} got {ok}/{len(batch)} on try {attempt+1}, sleep {wait}s", file=sys.stderr)
                _time.sleep(wait)
            except Exception as e:
                wait = 6 * (attempt + 1) ** 2
                print(f"[warn] chunk {chunk_n} try {attempt+1} failed: {e} — sleep {wait}s", file=sys.stderr)
                _time.sleep(wait)
        if got_batch is not None:
            n_proc = _process_chunk_df(got_batch, batch)
            print(f"[info] chunk {chunk_n}/{total_chunks} processed {n_proc}/{len(batch)}", file=sys.stderr)
        else:
            print(f"[error] chunk {chunk_n} gave up after 4 tries", file=sys.stderr)
        _time.sleep(CHUNK_SLEEP)

    # ── Fallback: yahooquery for tickers yfinance couldn't fetch ──
    # yahooquery hits different Yahoo endpoints (quoteSummary) so often
    # recovers tickers yfinance's chart API drops.
    missing_after_yf = [s for s in symbols if s not in out]
    if missing_after_yf:
        try:
            from yahooquery import Ticker as _YQT
            print(f"[info] yahooquery fallback for {len(missing_after_yf)} missing tickers…", file=sys.stderr)
            YQ_CHUNK = 40
            recovered = 0
            for i in range(0, len(missing_after_yf), YQ_CHUNK):
                # Meme budget wall-clock : si la boucle yfinance a deja tout
                # consomme, le fallback ne doit pas manger la phase d'ecriture.
                _elapsed = _time.time() - _RUN_START
                if _elapsed > QUOTE_PHASE_BUDGET_S:
                    print(f"[budget] fallback yahooquery stoppe a {_elapsed/60:.1f} min — "
                          f"{len(missing_after_yf) - i} tickers laisses au cache precedent.",
                          file=sys.stderr)
                    break
                batch = missing_after_yf[i:i + YQ_CHUNK]
                try:
                    yq = _YQT(batch, asynchronous=False, validate=False)
                    hist_df = yq.history(period="60d", interval="1d")
                    prices  = yq.price if isinstance(yq.price, dict) else {}
                except Exception as e:
                    print(f"[warn] yahooquery chunk {i // YQ_CHUNK + 1} failed: {e}", file=sys.stderr)
                    _time.sleep(2)
                    continue
                for sym in batch:
                    try:
                        # Extract per-symbol closes from multi-index DataFrame
                        if hasattr(hist_df, "loc") and sym in hist_df.index.get_level_values(0):
                            sub = hist_df.loc[sym]
                            if "close" not in sub.columns:
                                continue
                            closes = sub["close"].dropna()
                            vols   = sub.get("volume")
                        else:
                            continue
                        if closes is None or len(closes) < 2:
                            continue
                        ccy = detect_currency(sym)
                        dates = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d) for d in closes.index]
                        closes_local = list(closes.values)
                        closes_usd = [apply_fx(p, ccy, fx_rates, date_str=d, symbol=sym)
                                      for p, d in zip(closes_local, dates)]
                        if any(c is None for c in closes_usd):
                            print(f"[skip-yq] {sym}: FX rate missing for {ccy}", file=sys.stderr)
                            continue
                        last = float(closes_usd[-1])
                        p7   = float(closes_usd[-6])  if len(closes_usd) >= 6  else None
                        p30  = float(closes_usd[-22]) if len(closes_usd) >= 22 else None
                        perf_7d  = ((last / p7)  - 1) * 100 if p7  and p7 > 0  else None
                        perf_30d = ((last / p30) - 1) * 100 if p30 and p30 > 0 else None

                        vol_usd = 0
                        try:
                            if vols is not None and len(vols) > 0:
                                v = float(vols.dropna().iloc[-1])
                                vol_usd = v * float(closes_local[-1])
                                # Note: already local currency volume; convert approx to USD
                                vol_usd = vol_usd * (last / float(closes_local[-1]) if closes_local[-1] > 0 else 1)
                        except Exception:
                            pass

                        # mcap from yahooquery price dict (quoteSummary endpoint)
                        mcap_usd = 0
                        pdat = prices.get(sym)
                        if isinstance(pdat, dict):
                            mcap_local = float(pdat.get("marketCap") or 0)
                            if mcap_local:
                                # yahooquery returns marketCap already in main
                                # currency unit (GBP for .L, not pence) — only
                                # convert FX, no /100 needed here.
                                mcap_usd = (apply_fx(mcap_local, ccy, fx_rates) or 0) if ccy != "USD" else mcap_local

                        meta = STOCKS.get(sym, {})
                        out[sym] = {
                            "symbol": sym,
                            "name": meta.get("name", sym),
                            "domain": meta.get("domain", ""),
                            "exchange": meta.get("x", ""),
                            "currency": ccy,
                            "image":          "",
                            "image_fallback": "",
                            "price": last,
                            "price_local": float(closes_local[-1]),
                            "mcap": mcap_usd,
                            "volume_usd": vol_usd,
                            "perf_7d": perf_7d,
                            "perf_30d": perf_30d,
                        }
                        recovered += 1
                    except Exception as e:
                        print(f"[warn] yahooquery {sym}: {e}", file=sys.stderr)
                _time.sleep(1)
            print(f"[info] yahooquery recovered {recovered}/{len(missing_after_yf)} tickers", file=sys.stderr)
        except ImportError:
            print("[info] yahooquery not installed — skipping fallback (pip install yahooquery to enable)", file=sys.stderr)

    # ── Stooq snapshot fallback for tickers still missing ──
    # Stooq covers US (.us) + most western markets (.uk/.fr/.de/.nl/.ch/.it/.es/.dk/.se/.no/.jp/.hk/.in/.kr).
    # For tickers in unsupported regions (Saudi .SR, Brazil .SA on Bovespa, Indonesia .JK, etc.),
    # Stooq returns nothing and we fall through to the prev-cache merge in main().
    missing_after_yq = [s for s in symbols if s not in out]
    if missing_after_yq:
        print(f"[info] Stooq snapshot fallback for {len(missing_after_yq)} missing tickers…", file=sys.stderr)
        recovered_stq = 0
        for _stq_i, sym in enumerate(missing_after_yq):
            # Boucle sequentielle 1 requete/ticker : sans borne, 800 tickers
            # manquants suffisent a manger tout le budget restant.
            if _stq_i % 10 == 0 and (_time.time() - _RUN_START) > QUOTE_PHASE_BUDGET_S:
                print(f"[budget] fallback Stooq stoppe — {len(missing_after_yq) - _stq_i} "
                      f"tickers laisses au cache precedent.", file=sys.stderr)
                break
            snap = fetch_stooq_snapshot(sym)
            if not snap:
                continue
            ccy = detect_currency(sym)
            last_local = snap["last_local"]
            # Stooq returns London prices in pence too — pass symbol for /100 normalize
            last_usd = (apply_fx(last_local, ccy, fx_rates, symbol=sym) or 0) if ccy != "USD" else last_local
            if not last_usd:
                print(f"[skip-stooq] {sym}: FX rate missing for {ccy}", file=sys.stderr)
                continue
            meta = STOCKS.get(sym, {})
            out[sym] = {
                "symbol": sym,
                "name": meta.get("name", sym),
                "domain": meta.get("domain", ""),
                "exchange": meta.get("x", ""),
                "currency": ccy,
                "image":          "",
                "image_fallback": "",
                "price": last_usd,
                "price_local": last_local,
                "mcap": 0,           # Stooq doesn't return mcap; merge step below will fill
                "volume_usd": 0,
                "perf_7d":  snap["perf_7d"],
                "perf_30d": snap["perf_30d"],
                "_source": "stooq",
            }
            recovered_stq += 1
            time.sleep(0.3)  # polite to Stooq (no public rate-limit doc)
        print(f"[info] Stooq recovered {recovered_stq}/{len(missing_after_yq)} tickers", file=sys.stderr)

    # ── Inline favicons in parallel (1 HTTP round-trip per unique domain) ──
    unique_domains = {}
    for sym, row in out.items():
        dom = row.get("domain") or ""
        if not dom:
            row["image"] = _letter_avatar_data_uri(sym)
            continue
        unique_domains.setdefault(dom, []).append(sym)

    if unique_domains:
        print(f"[icons] inlining {len(unique_domains)} favicons…", file=sys.stderr)
        def _fetch(dom_syms):
            dom, syms = dom_syms
            uri = inline_favicon(dom, syms[0])
            return dom, uri
        with ThreadPoolExecutor(max_workers=16) as ex:
            for dom, uri in ex.map(_fetch, unique_domains.items()):
                for sym in unique_domains[dom]:
                    out[sym]["image"] = uri

    return out


# ─────────────────────────────────────────────────────────────────────────
# S&P 500 TREND FILTER (equivalent au filtre BTC/MA200)
# ─────────────────────────────────────────────────────────────────────────
def fetch_sp500_trend(histories=None):
    """Compute trend filter (mode ALPHA/CAUTION/ZEN) from SPY MA200.

    PREFER : reuse the SPY history already fetched in `histories` dict (15y of
    daily-then-weekly downsampled data) — avoids a separate yfinance call that
    routinely gets rate-limited after the big batch fetch and returns "unknown".

    Fallback : if `histories` doesn't contain enough SPY data, try a fresh
    yfinance call (period="1y").
    """
    closes = None
    # ── Path A : reuse already-fetched SPY history ──
    if histories and "SPY" in histories:
        spy_hist = histories["SPY"]
        if spy_hist and len(spy_hist) >= 50:
            # Sort chronologically, extract close prices
            sorted_hist = sorted(spy_hist, key=lambda p: p[0])
            closes = [float(p[1]) for p in sorted_hist if p[1] and p[1] > 0]

    # ── Path B : fallback to fresh yfinance fetch ──
    if not closes or len(closes) < 50:
        try:
            import yfinance as yf
            for sym in ["SPY", "^GSPC", "VOO"]:
                try:
                    h = yf.download(sym, period="1y", interval="1d",
                                    progress=False, auto_adjust=False)
                    if h is not None and not h.empty and "Close" in h.columns:
                        c = h["Close"].dropna().values
                        if hasattr(c, "ndim") and c.ndim > 1:
                            c = c[:, 0]
                        if len(c) >= 50:
                            closes = [float(v) for v in c if v and v > 0]
                            break
                except Exception:
                    continue
        except Exception as e:
            print(f"[warn] SP500 trend yfinance fallback: {e}", file=sys.stderr)

    if not closes or len(closes) < 50:
        print(f"[warn] SP500 trend: insufficient SPY history ({len(closes) if closes else 0} pts)", file=sys.stderr)
        return {"mode": "unknown", "idx_px": None, "ma200": None,
                "perf_30d": None, "distance_ma200": None}

    # Use last 200 points for MA200 (or all available if < 200, with warning)
    px = closes[-1]
    if len(closes) >= 200:
        ma200 = sum(closes[-200:]) / 200
    else:
        # Histories are downsampled (mixed daily/weekly), so 200 "trading days" might
        # not be present — use available with explicit divisor.
        ma200 = sum(closes) / len(closes)
        print(f"[info] SP500 trend: using MA{len(closes)} (less than 200 pts available)", file=sys.stderr)

    # 30j perf : try to find the bar ~22 trading days ago (or ~30 calendar days
    # if data is daily; otherwise use a proportionally older index).
    idx_30d = max(0, len(closes) - 22)
    perf_30d = ((px / closes[idx_30d]) - 1) * 100 if closes[idx_30d] > 0 else 0.0
    dist = (px / ma200 - 1) * 100 if ma200 > 0 else 0.0
    if px > ma200 and perf_30d > -3:
        mode = "alpha"
    elif px > ma200 and perf_30d <= -3:
        mode = "caution"
    else:
        mode = "zen"
    return {
        "mode": mode,
        "idx_px": round(px, 0),
        "ma200":  round(ma200, 0),
        "perf_30d": round(perf_30d, 2),
        "distance_ma200": round(dist, 2),
    }


# ─────────────────────────────────────────────────────────────────────────
# HISTORY (up to 15y via yfinance, tiered downsampling)
# ─────────────────────────────────────────────────────────────────────────
# Hard cap on history depth to keep cache size predictable. Yahoo "max" can
# return 30+ years for old US tickers (MSFT 1986, AAPL 1980) — we trim to 15y.
HIST_MAX_DAYS = 15 * 365 + 4   # ~15 years

def _downsample(ts_price_list):
    """Tiered downsampling — preserves recent detail, compresses older history.
       - Last 90d   → daily       (every point kept)
       - 90d-2y     → weekly      (≥7d gap)
       - 2-10y      → bi-weekly   (≥14d gap)
       - 10-15y     → tri-weekly  (≥21d gap)
       Result : ~150 pts for 1y IPOs, ~250 pts for 5y, ~500 pts for 15y full.
    """
    if not ts_price_list or len(ts_price_list) < 50:
        return ts_price_list
    DAY = 86400
    now = time.time()
    # Apply HIST_MAX_DAYS cap
    cutoff = now - HIST_MAX_DAYS * DAY
    pts = [(ts, px) for ts, px in ts_price_list if ts >= cutoff]
    pts.sort(key=lambda p: p[0])
    if len(pts) < 50:
        return pts
    out = []
    last_kept = -10**12
    last_pt = pts[-1]
    for ts, px in pts:
        age_days = (now - ts) / DAY
        if   age_days <= 90:   min_gap = 0           # all daily
        elif age_days <= 730:  min_gap = 6 * DAY     # weekly
        elif age_days <= 3650: min_gap = 13 * DAY    # bi-weekly
        else:                  min_gap = 20 * DAY    # tri-weekly
        if (ts - last_kept) >= min_gap or ts == last_pt[0]:
            out.append((ts, px))
            last_kept = ts
    # Always include the very last (most recent) observation
    if out and out[-1][0] != last_pt[0]:
        out.append(last_pt)
    return out


def _history_cache_load():
    if not HIST_CACHE.exists():
        return None, float("inf")
    age_h = (time.time() - HIST_CACHE.stat().st_mtime) / 3600
    try:
        return json.load(open(HIST_CACHE, "r", encoding="utf-8")), age_h
    except Exception:
        return None, float("inf")


def _hist_checkpoint_write(path, hist_map):
    """Écrit HIST_CACHE de façon atomique (tmp + os.replace) pour ne jamais
    laisser un cache tronqué si le process est tué en plein write."""
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: [[a, b] for a, b in v] for k, v in hist_map.items()}, f)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[warn] hist checkpoint write: {e}", file=sys.stderr)


def fetch_stock_histories(symbols, fx_rates=None, checkpoint=None, seed=None):
    """Fetch up to 15y daily history (yfinance period='max', trimmed by _downsample).
       If fx_rates provided, convert to USD per-day.

    ANTI-FREEZE (2026-07-24) : le fetch se fait par CHUNKS et checkpoint le
    HIST_CACHE après chaque chunk. Avant, le download des ~800 tickers se
    faisait en UN seul appel yf.download ; quand il dépassait le deadline
    wall-clock du wrapper (SIGKILL, exit 137), TOUT le travail était perdu et
    le HIST_CACHE jamais réécrit → à >24h il était re-fetch en entier → re-tué
    → gel permanent du cache (constaté 07-22→07-24, 7 hard-kills, données
    figées 53h). Désormais :
      • `seed` (histoires précédentes) amorce `out` → le cache reste COMPLET
        même si le run est tué avant la fin (les symboles pas encore refetch
        gardent leur ancienne valeur au lieu de disparaître).
      • checkpoint après CHAQUE chunk → un run tué laisse un HIST_CACHE frais
        et complet ; le run suivant passe par la voie de réutilisation rapide
        (age<TTL & keys⊇needed) et atteint l'écriture du cache principal.
    Résultat : le fetch devient RÉSUMABLE entre deux runs, la boucle de gel
    ne peut plus se former."""
    fx_rates = fx_rates or {}
    if not symbols:
        return dict(seed or {})
    try:
        import yfinance as yf
    except ImportError:
        return dict(seed or {})

    # Amorçage durable : on part des histoires précédentes pour que le cache
    # reste exploitable même si le process meurt au 1er chunk.
    out = dict(seed or {})
    CHUNK = 100
    batches = [symbols[i:i + CHUNK] for i in range(0, len(symbols), CHUNK)]
    for bi, batch in enumerate(batches, 1):
        try:
            # auto_adjust=True : returns the dividend+split adjusted Close.
            # CRITICAL pour les secteurs dividend-heavy (Tabac, REITs, Utilities,
            # Pétrole) — sinon leur "performance" et donc leurs corrélations sont
            # systématiquement sous-estimées de 4-7%/an cumulé.
            hist = yf.download(" ".join(batch), period="max", interval="1d",
                               group_by="ticker", progress=False, threads=True,
                               auto_adjust=True)
        except Exception as e:
            print(f"[warn] yf max chunk {bi}/{len(batches)}: {e}", file=sys.stderr)
            continue
        failed = []
        for sym in batch:
            try:
                if hasattr(hist.columns, "get_level_values") and sym in hist.columns.get_level_values(0):
                    closes = hist[sym]["Close"].dropna()
                else:
                    closes = hist["Close"].dropna() if "Close" in hist.columns else None
                if closes is None or len(closes) < 10:
                    failed.append(sym)
                    continue
                ccy = detect_currency(sym)
                raw = []
                fx_missing = False
                for d, v in closes.items():
                    day_str = d.strftime("%Y-%m-%d")
                    usd_px = apply_fx(float(v), ccy, fx_rates, date_str=day_str, symbol=sym)
                    if usd_px is None:
                        fx_missing = True
                        break
                    raw.append((int(d.timestamp()), usd_px))
                if fx_missing:
                    print(f"[skip-hist] {sym}: FX rate missing for {ccy}", file=sys.stderr)
                    failed.append(sym)
                    continue
                out[sym] = _downsample(raw)
                print(f"[hist] {sym} ({ccy}): {len(raw)} → {len(out[sym])} pts")
            except Exception as e:
                print(f"[warn] hist {sym}: {e}", file=sys.stderr)
                failed.append(sym)

        # ── Stooq fallback for yfinance failures (per chunk) ──
        if failed:
            print(f"[info] chunk {bi}: {len(failed)} yfinance failed, Stooq fallback: {failed[:5]}...")
            for sym in failed:
                ccy = detect_currency(sym)
                raw_local = fetch_stooq_history(sym)
                if not raw_local:
                    continue
                raw_usd_raw = [(ts, apply_fx(px, ccy, fx_rates,
                                  datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                                  symbol=sym))
                                for ts, px in raw_local]
                if any(px is None for _, px in raw_usd_raw):
                    print(f"[skip-stooq-hist] {sym}: FX rate missing for {ccy}", file=sys.stderr)
                    continue
                out[sym] = _downsample(raw_usd_raw)
                print(f"[stooq] {sym} ({ccy}): {len(raw_local)} → {len(out[sym])} pts (fallback)")
                time.sleep(0.5)  # polite delay

        # ── Checkpoint anti-freeze : le cache survit à un SIGKILL ultérieur ──
        if checkpoint:
            _hist_checkpoint_write(checkpoint, out)
            print(f"[hist-ckpt] chunk {bi}/{len(batches)} → {len(out)} stocks cached", flush=True)

    return out


def fetch_shares_history(symbols, max_workers=8):
    """Fetch historical shares-outstanding (~quarterly granularity) via yfinance.
    Returns {sym: [(ts, shares), …]} sorted by ts.

    Pourquoi : sans ça, on utilise `shares = current_mcap / current_price`
    constant — ce qui sous-estime AAPL 2011 de ~40% (rachats) et sur-estime
    TSLA 2014 (dilutions). yf.Ticker.get_shares_full retourne l'historique
    daily/weekly des shares outstanding sur ~5 ans (parfois plus).

    Au-delà de la fenêtre dispo, on utilise la donnée la + ancienne en
    fallback (mieux que d'utiliser les shares actuelles)."""
    try:
        import yfinance as yf
    except ImportError:
        return {}
    from datetime import datetime, timedelta, timezone as _tz
    end = datetime.now(_tz.utc)
    start = end - timedelta(days=HIST_MAX_DAYS)
    out = {}
    failed = 0
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def _one(sym):
        try:
            ser = yf.Ticker(sym).get_shares_full(start=start, end=end)
            if ser is None or len(ser) == 0:
                return sym, []
            pts = []
            for d, v in ser.items():
                if v is None or v <= 0:
                    continue
                ts = int(d.timestamp())
                pts.append((ts, int(v)))
            pts.sort()
            # Deduplicate same-day (keep last) — keep tuples (ts, shares)
            dedup = {}
            for ts, sh in pts:
                day = datetime.fromtimestamp(ts, _tz.utc).strftime("%Y-%m-%d")
                dedup[day] = (ts, sh)
            return sym, sorted(dedup.values())
        except Exception as e:
            return sym, None
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_one, s): s for s in symbols}
        done = 0
        for fut in as_completed(futures):
            sym, pts = fut.result()
            done += 1
            if pts is None:
                failed += 1
            elif pts:
                out[sym] = _split_adjust_shares(pts)
            if done % 100 == 0:
                print(f"[shares] {done}/{len(symbols)} fetched (got {len(out)}, failed {failed})")
    print(f"[ok] shares history fetched for {len(out)}/{len(symbols)} stocks ({failed} failed)")
    return out


def _split_adjust_shares(shares_pts):
    """Detect & flatten split events.

    yfinance retourne shares NON split-adjusted (AAPL 4.6B en 2019, 18.4B post-2020-split).
    Sans correction, le poids price × shares a un saut artificiel au moment du split.

    Heuristique : un split est un saut de ratio >= 1.5 entre entries <= 60 jours apart.
    La condition temporelle filtre les faux positifs (Class A/C creation chez GOOGL,
    M&A, reorgs progressifs) qui modifient les shares sur plusieurs trimestres."""
    if not shares_pts or len(shares_pts) < 2:
        return shares_pts
    pts = list(shares_pts)
    cum_factor = 1.0
    out_rev = [(pts[-1][0], int(pts[-1][1]))]
    SIXTY_DAYS = 60 * 86400
    for i in range(len(pts) - 2, -1, -1):
        ts_curr, sh_curr = pts[i]
        ts_next, sh_next = pts[i + 1]
        gap = ts_next - ts_curr
        if sh_curr > 0:
            ratio = sh_next / sh_curr
            # Vraie split : grand ratio ET temps court
            if ratio >= 1.5 and gap <= SIXTY_DAYS:
                cum_factor *= ratio
        out_rev.append((ts_curr, int(sh_curr * cum_factor)))
    return list(reversed(out_rev))


def _shares_at(shares_hist, ts, fallback):
    """Return shares-outstanding at time ts. shares_hist = sorted [(ts, sh), …].
    Uses last-known value <= ts ; falls back to oldest available if ts is older
    than all entries ; falls back to `fallback` if no history at all."""
    if not shares_hist:
        return fallback
    if ts < shares_hist[0][0]:
        return shares_hist[0][1]
    # Linear scan acceptable since per-ticker history is ~60-200 quarterly points
    last = shares_hist[0][1]
    for h_ts, sh in shares_hist:
        if h_ts > ts:
            break
        last = sh
    return last


def _index_grid(days_sorted):
    """Grille de dates GLOBALE, partagée par tous les constituants du panier.

    POURQUOI : `_downsample` amincit chaque ticker sur SA propre horloge (règle
    « ≥ N jours depuis le dernier point gardé », ancrée sur son premier point).
    Les jours conservés ne coïncident donc pas d'un ticker à l'autre. Prendre
    l'union de ces jours — ce que faisait l'ancien code — produisait des barres
    où 1 seul ticker sur 41 cotait : l'indice « sectoriel » se réduisait au
    rendement d'une seule action, clippé à ±30 %. D'où les à-pics verticaux
    (Industriels : −30 % le 2026-04-17, avec exactement 1 ticker présent ce
    jour-là ; couverture médiane de 7 tickers sur 41 entre 2016 et 2024).

    On reconstruit donc une grille unique, ancrée sur le jour le plus récent et
    cadencée comme le downsampling amont. Chaque ticker est ensuite forward-fillé
    dessus, si bien que chaque barre couvre le MÊME intervalle pour tout le monde.
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
    grid = []
    last_ts = None
    for d in reversed(days):
        t = ts_of[d]
        age = (now - t) / DAY
        # Mêmes paliers que _downsample() : quotidien / hebdo / bi-hebdo / tri-hebdo.
        if   age <= 90:   step = 0
        elif age <= 730:  step = 7
        elif age <= 3650: step = 14
        else:             step = 21
        # step-1 : tolère le décalage d'un jour férié sans sauter une période.
        if last_ts is None or (last_ts - t) >= (step - 1) * DAY:
            grid.append(d)
            last_ts = t
    grid.reverse()
    return grid


# Au-delà de ce délai sans cotation, un ticker est considéré délisté et sort du
# panier au lieu d'être forward-fillé indéfiniment (poids figé, rendement 0).
_STALE_MAX_DAYS = 120


def compute_narrative_index(narr_stat, histories, shares_hist_all=None):
    """Time-varying mcap-weighted returns-based index, full basket.

    KEY FIX (vs ancien code top-3 + poids fixes) :
    Le poids d'un stock à l'instant t est `price(t) × shares_implicites`,
    où `shares_implicites = current_mcap / current_price` (constant par ticker).
    Du coup quand NVDA cotait $0.50 split-adjusted en 2014, son poids dans le
    panier "AI & Semi" en 2014 = 0.50 × shares ≈ $5B (≈1% du basket de l'époque),
    pas $3.5T comme avant. INTC qui pesait $170B en 2014 prend correctement
    sa part dominante de l'époque. C'est l'auto-rebalancing dynamique d'un
    indice mcap-weighted (S&P 500 style).

    Aussi : on utilise TOUS les tickers du narratif (plus le cap top-3), pour
    capturer les leaders historiques qui sont aujourd'hui mid-cap (IBM/INTC/CSCO
    pour Big Tech ; ORCL pour Cloud ; etc.).
    """
    tokens = narr_stat["tokens"]
    if not tokens:
        return None
    shares_hist_all = shares_hist_all or {}

    day_series = {}
    shares_fallback = {}    # shares = current_mcap / current_price (used when no historical)
    sym_shares_hist = {}    # per-symbol historical shares time-series
    sym_first_ts = {}       # mapping day → ts (for shares lookup)
    used_symbols = []
    for t in tokens:
        sym = t["symbol"]
        h = histories.get(sym)
        if not h or len(h) < 30:
            continue
        cur_px   = t.get("price")
        cur_mcap = t.get("mcap")
        if not cur_px or not cur_mcap or cur_px <= 0 or cur_mcap <= 0:
            continue
        per_day = {}
        ts_map = {}
        for ts, px in h:
            if px is None or px <= 0:
                continue
            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            per_day[day] = px
            ts_map[day] = ts
        if len(per_day) < 30:
            continue
        day_series[sym] = per_day
        sym_first_ts[sym] = ts_map
        shares_fallback[sym] = cur_mcap / cur_px
        sym_shares_hist[sym] = shares_hist_all.get(sym, [])
        used_symbols.append(sym)
    if not day_series:
        return None

    all_days = set()
    for s in day_series.values():
        all_days.update(s.keys())
    if len(all_days) < 5:
        return None
    days_sorted = sorted(all_days)

    # Grille commune à tous les constituants (cf. _index_grid) : sans elle, une
    # barre pouvait n'agréger qu'un seul ticker du panier.
    grid = _index_grid(days_sorted)
    if len(grid) < 5:
        grid = days_sorted
    DAY = 86400

    # Forward-fill de chaque ticker sur la grille : dernier cours connu ≤ date de
    # grille. Un ticker qui n'a pas coté ce jour-là garde son cours (rendement 0
    # sur la barre) au lieu de sortir du dénominateur — comportement standard d'un
    # indice mcap-weighted, et une action isolée ne peut plus piloter l'indice.
    ff_px, ff_sh = {}, {}
    for k, series in day_series.items():
        own_days = sorted(series.keys())
        ts_map   = sym_first_ts[k]
        sh_hist  = sym_shares_hist[k]
        fb       = shares_fallback[k]
        px_col, sh_col = [], []
        j, p, n_own = -1, 0, len(own_days)
        for g in grid:
            while p < n_own and own_days[p] <= g:
                j = p
                p += 1
            if j < 0:
                px_col.append(None)
                sh_col.append(None)
                continue
            d_j  = own_days[j]
            ts_j = ts_map[d_j]
            try:
                g_ts = datetime.strptime(g, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                g_ts = ts_j
            # Délisting / série morte : on sort le ticker du panier plutôt que de
            # figer son poids avec un rendement nul jusqu'à la fin de l'historique.
            if (g_ts - ts_j) / DAY > _STALE_MAX_DAYS:
                px_col.append(None)
                sh_col.append(None)
                continue
            px_col.append(series[d_j])
            sh_col.append(_shares_at(sh_hist, ts_j, fb))
        ff_px[k] = px_col
        ff_sh[k] = sh_col

    # Time-varying mcap-weighted returns-based index using HISTORICAL shares
    # outstanding when available. weight(stock, t) = price(t) × shares_at(t)
    # gives the proper capital deployed at time t (corrects for buybacks/dilutions).
    values = [100.0]
    for i in range(1, len(grid)):
        w_ret = 0.0
        w_sum = 0.0
        for k in day_series:
            prev_px = ff_px[k][i - 1]
            cur_px  = ff_px[k][i]
            # None = pas encore listé (ou délisté) : le ticker n'entre pas dans la
            # barre, donc aucun saut artificiel à son arrivée dans le panier.
            if prev_px is None or cur_px is None or prev_px <= 0:
                continue
            sh = ff_sh[k][i - 1]
            if not sh or sh <= 0:
                continue
            # Capital deployed in this stock at start of bar (time-varying)
            w_k = prev_px * sh
            ret = (cur_px / prev_px) - 1.0
            ret = max(-0.30, min(0.30, ret))   # safeguard contre erreurs de données
            w_ret += ret * w_k
            w_sum += w_k
        if w_sum > 0:
            w_ret /= w_sum
        else:
            w_ret = 0.0
        values.append(values[-1] * (1 + w_ret))

    values_out = [round(v, 6) for v in values]
    return {
        "dates":  grid,
        "values": values_out,
        "tickers": used_symbols,
    }


# ─────────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────────
def narrative_stats(narr, cfg, scan, quotes):
    ticker_rows = []
    total_mcap = 0.0
    vol_sum    = 0.0
    for sym in cfg["tickers"]:
        q = quotes.get(sym)
        if not q:
            continue
        meta = STOCKS.get(sym, {})
        ticker_rows.append({
            "id":       sym,            # for compat with crypto schema
            "symbol":   sym,
            "name":     q.get("name", sym),
            "exchange": q.get("exchange", ""),
            "region":   meta.get("region", "US"),  # filtrage géo dynamique dans l'UI
            "mcap_rank": None,          # tradfi uses global mcap not rank
            "image":          q.get("image", ""),
            "image_fallback": q.get("image_fallback", ""),
            "price":    q.get("price"),
            "mcap":     q.get("mcap", 0),
            "volume":   q.get("volume_usd", 0),
            "perf_7d":  q.get("perf_7d"),
            "perf_30d": q.get("perf_30d"),
            "is_stock": True,
        })
        total_mcap += q.get("mcap") or 0
        vol_sum    += q.get("volume_usd") or 0
    ticker_rows.sort(key=lambda t: t["mcap"] or 0, reverse=True)

    # Outlier detection: top ticker >60% of mcap
    outlier = False
    dominant_pct = 0.0
    dominant_sym = None
    if ticker_rows and total_mcap > 0:
        top = ticker_rows[0]
        dominant_pct = (top["mcap"] or 0) / total_mcap
        dominant_sym = top["symbol"]
        if dominant_pct > 0.60 and len(ticker_rows) > 1:
            outlier = True

    # mcap-weighted perf
    def w_avg(key):
        num = 0.0; den = 0.0
        for r in ticker_rows:
            v = r.get(key)
            if v is None:
                continue
            w = r["mcap"] if total_mcap > 0 else 1.0
            num += v * w
            den += w
        return (num / den) if den > 0 else None
    perf_7d_w  = w_avg("perf_7d")
    perf_30d_w = w_avg("perf_30d")
    price_mom = 0.0
    if perf_7d_w is not None and perf_30d_w is not None:
        price_mom = 0.5 * perf_7d_w + 0.5 * perf_30d_w
    elif perf_7d_w is not None:
        price_mom = perf_7d_w
    elif perf_30d_w is not None:
        price_mom = perf_30d_w

    today = datetime.now(timezone.utc)
    m7, mp7, m30, accel = news_momentum(scan[narr]["daily"], today)

    return {
        "narrative": narr,
        "axis":   cfg.get("axis", "theme"),  # "theme" (sectoriel) ou "geo" (zone)
        "icon":   cfg["icon"],
        "color":  cfg["color"],
        "tokens": ticker_rows,   # same field name as crypto pipeline for UI compat
        "total_mcap_b":  round(total_mcap / 1e9, 2),
        "total_volume_b": round(vol_sum / 1e9, 2),
        "perf_7d":  round(perf_7d_w, 2)  if perf_7d_w  is not None else None,
        "perf_30d": round(perf_30d_w, 2) if perf_30d_w is not None else None,
        "price_momentum": round(price_mom, 2),
        "mentions_7d":  m7,
        "mentions_prev7d": mp7,
        "mentions_30d": m30,
        "mention_accel": accel,
        "mention_total": scan[narr]["total"],
        "daily_mentions": scan[narr]["daily"],
        "articles": scan[narr]["articles"][:8],
        "outlier":  outlier,
        "dominant_pct": round(dominant_pct * 100, 1),
        "dominant_sym": dominant_sym,
        "description":  NARRATIVE_DESC.get(narr, ""),
    }


def rank_normalize(values):
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
# MOMENTUM HELPERS — approche Alpha ZEN appliquée au TradFi
# Benchmark = S&P 500 (SPY) au lieu de BTC.
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
    """True if last price > 50-day SMA."""
    if not history or len(history) < 30:
        return None
    last_ts = history[-1][0]
    cutoff = last_ts - 50 * 86400
    window = [px for ts, px in history if ts >= cutoff]
    if len(window) < 20:
        return None
    return history[-1][1] > (sum(window) / len(window))


SP500_BENCH_KEY = "SPY"  # benchmark ticker for momentum relatif


def augment_with_momentum_metrics(stats_list, histories):
    """Ajoute momentum long-terme, relatif vs S&P 500, breadth, signal LONG/FLAT
    et trend_age_days. A appeler APRES compute_narrative_index."""
    spy_hist = histories.get(SP500_BENCH_KEY)
    sp_90d  = _perf_over_days(spy_hist, 90)  if spy_hist else None
    sp_180d = _perf_over_days(spy_hist, 180) if spy_hist else None

    for s in stats_list:
        tokens = s["tokens"][:HIST_TOP_N_PER_NARRATIVE]

        # ── Momentum long terme (mcap-weighted, via histories) ──
        num90 = den90 = num180 = den180 = 0.0
        leaders_above = leaders_total = 0
        for t in tokens:
            h = histories.get(t["symbol"])
            if not h:
                continue
            w = t.get("mcap") or 0
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

        # ── Momentum RELATIF vs S&P 500 (cœur de l'approche) ──
        rel_mom_90d  = (perf_90d_w  - sp_90d)  if (perf_90d_w  is not None and sp_90d  is not None) else None
        rel_mom_180d = (perf_180d_w - sp_180d) if (perf_180d_w is not None and sp_180d is not None) else None

        # ── Breadth 30j : % d'actions du secteur avec perf_30d > 0 ──
        pos = total = 0
        for t in s["tokens"]:
            p = t.get("perf_30d")
            if p is None:
                continue
            total += 1
            if p > 0:
                pos += 1
        breadth_30d = (100.0 * pos / total) if total else None

        # ── Signal LONG/FLAT & trend_age (index secteur vs MA50) ──
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
    """Composite 0..100 — momentum cyclique Alpha ZEN adapté TradFi (v2, 2026-04-24) :
        55%  momentum RELATIF vs S&P 500 (90j)  → vraie force du secteur vs le marché
        22.5% breadth (% actions secteur >0 sur 30j) → largeur du move
        22.5% momentum court terme (7j+30j)     → réactivité / timing
        0%   news acceleration                   → désactivé : signal RSS trop fragile
                                                    (baseline quasi-vide → accel plafonné à +3
                                                    pour 14 secteurs, aucune discrimination).
                                                    Le rank est conservé en output pour debug.
    """
    rel_vals     = [s.get("rel_mom_90d")    for s in stats_list]
    breadth_vals = [s.get("breadth_30d")    for s in stats_list]
    px_vals      = [s.get("price_momentum") for s in stats_list]
    news_vals    = [s.get("mention_accel")  for s in stats_list]

    rel_rk     = rank_normalize(rel_vals)
    breadth_rk = rank_normalize(breadth_vals)
    px_rk      = rank_normalize(px_vals)
    news_rk    = rank_normalize(news_vals)  # gardé en output pour debug, poids 0

    for i, s in enumerate(stats_list):
        s["score_rel_mom"] = rel_rk[i]
        s["score_breadth"] = breadth_rk[i]
        s["score_price"]   = px_rk[i]
        s["score_news"]    = news_rk[i]
        s["score"] = round(
            0.55  * rel_rk[i] + 0.225 * breadth_rk[i]
            + 0.225 * px_rk[i] + 0.0 * news_rk[i], 1
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
    """Série historique = à chaque date, moyenne SUR LES SECTEURS du % d'actions
    du secteur dont le rendement {span_days}j est > 0. Même définition que la jauge
    breadth (breadth_30d moyen), reconstruite dans le temps depuis les historiques
    de prix par action. Renvoie (series, breadth_now) avec
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


def _breadth_key_tradfi(t):
    return t.get("symbol")


# ─────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────
def _init_yfinance_cache():
    """Isole le cache tz de yfinance dans YF_CACHE_DIR (privé à ce script).
    À appeler AVANT tout usage de yfinance — sinon la lib s'attache au cache
    partagé par défaut et la cascade `no such table: _tz_kv` peut revenir."""
    try:
        import yfinance as yf
        YF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(YF_CACHE_DIR))
        print(f"[info] yfinance tz-cache isolé dans {YF_CACHE_DIR}")
    except Exception as e:
        print(f"[warn] set_tz_cache_location: {e}", file=sys.stderr)


def main():
    # Single-instance lock (prevents launchd overlap that corrupts yfinance's WAL)
    _lock = acquire_singleton_lock()
    if _lock is None:
        sys.exit(0)  # exit 0 so launchd doesn't mark as failure
    # Reset yfinance SQLite to avoid the OperationalError cascade
    reset_yfinance_cache()
    _init_yfinance_cache()
    # Snapshot of last successful cache → used to fill gaps for tickers that
    # neither yfinance, yahooquery nor Stooq could recover this run.
    prev_cache = load_previous_cache()

    if not NEWS_CACHE.exists():
        print(f"[error] {NEWS_CACHE} missing", file=sys.stderr)
        sys.exit(1)
    with open(NEWS_CACHE, "r", encoding="utf-8") as f:
        news_data = json.load(f)
    # Use macro news (finance RSS) for tradfi narratives
    macro_news = news_data.get("macro", [])
    print(f"[info] {len(macro_news)} macro/tradfi articles loaded")

    scan = scan_news(macro_news)
    total_matches = sum(v["total"] for v in scan.values())
    print(f"[info] {total_matches} matches across {len(NARRATIVES)} narratives")

    # Collect all needed currencies before fetching anything else
    all_syms = sorted({sym for cfg in NARRATIVES.values() for sym in cfg["tickers"]})
    needed_ccy = {detect_currency(s) for s in all_syms}
    print(f"[info] fetching FX rates for {len(needed_ccy - {'USD'})} currencies…")
    fx_rates = fetch_fx_rates(needed_ccy)

    print(f"[info] fetching quotes for {len(all_syms)} stocks (USD-adjusted)…")
    quotes = fetch_stocks(all_syms, fx_rates=fx_rates)
    print(f"[info] got {len(quotes)}/{len(all_syms)} stocks (live)")

    # ── Gap-fill from previous cache (last known good values, marked stale) ──
    prev_quotes = {}
    if prev_cache.get("narratives"):
        for n in prev_cache["narratives"]:
            for t in n.get("tokens", []):
                sym = t.get("symbol")
                if sym:
                    prev_quotes[sym] = t
    filled_stale = 0
    for sym in all_syms:
        if sym in quotes:
            # Backfill mcap from prev cache if Stooq fallback couldn't get one
            if not quotes[sym].get("mcap") and sym in prev_quotes:
                quotes[sym]["mcap"] = prev_quotes[sym].get("mcap", 0)
            continue
        prev = prev_quotes.get(sym)
        if not prev:
            continue
        # Copy prev quote, mark as stale so the UI can flag it if needed
        quotes[sym] = {**prev, "_stale": True, "_source": prev.get("_source", "prev_cache")}
        filled_stale += 1
    if filled_stale:
        print(f"[info] gap-filled {filled_stale} stocks from previous cache (marked _stale=true)")
    n_fresh = sum(1 for s in all_syms if s in quotes and not quotes[s].get("_stale"))
    coverage_pct = round(100 * n_fresh / max(1, len(all_syms)), 1)
    # 2026-07-31 : l'ancien libellé calculait "fresh" = total - stale, donc il
    # affichait "812/812 fresh" alors que 0 ticker avait été récupéré et que le
    # gap-fill avait échoué. On compte désormais les tickers réellement frais.
    print(f"[info] live coverage: {coverage_pct}% ({n_fresh}/{len(all_syms)} frais, "
          f"{filled_stale} repris du cache, {len(all_syms) - n_fresh - filled_stale} manquants)")

    # Note : le trend_filter est calculé APRÈS la récupération des histoires (cf.
    # plus bas) car on réutilise la SPY history déjà fetchée — évite un 2e appel
    # yfinance qui se fait rate-limiter après le batch principal.

    stats_list = []
    for narr, cfg in NARRATIVES.items():
        s = narrative_stats(narr, cfg, scan, quotes)
        stats_list.append(s)

    # ─── HISTORIES ───
    # ALL tickers now (pas juste top 3) — le filtre régions JS rebuild
    # l'index secteur sur le sous-ensemble filtré, ce qui exige les histoires
    # complètes côté navigateur.
    needed = set()
    for s in stats_list:
        for t in s["tokens"]:
            needed.add(t["symbol"])
    needed.add(SP500_BENCH_KEY)  # S&P 500 benchmark for rel-mom

    cached_hist, age_h = _history_cache_load()
    if cached_hist and age_h < HIST_CACHE_TTL_HOURS and set(cached_hist.keys()) >= needed:
        print(f"[info] using cached histories (age {age_h:.1f}h, {len(cached_hist)} stocks)")
        histories = {k: [(int(a), float(b)) for a, b in v] for k, v in cached_hist.items()}
    else:
        print(f"[info] fetching up to 15y histories for {len(needed)} stocks (USD-adjusted, tiered downsampling)…")
        # Seed anti-freeze : amorcer avec les histoires précédentes (HIST_CACHE
        # même périmé, sinon celles embarquées dans tradfi_cache.json). Combiné
        # au checkpoint par chunk, un run tué (SIGKILL/deadline) laisse quand
        # même un HIST_CACHE COMPLET et frais → le run suivant réutilise et
        # atteint l'écriture du cache principal. Voir fetch_stock_histories().
        hist_seed = {}
        if cached_hist:
            hist_seed = {k: [(int(a), float(b)) for a, b in v] for k, v in cached_hist.items()}
        elif prev_cache and prev_cache.get("histories"):
            for _s, _ph in prev_cache["histories"].items():
                if _ph and len(_ph) >= 10:
                    hist_seed[_s] = [(int(a), float(b)) for a, b in _ph]
        histories = fetch_stock_histories(sorted(needed), fx_rates=fx_rates,
                                          checkpoint=str(HIST_CACHE), seed=hist_seed)
        # Gap-fill histories from prev tradfi_cache.json (embedded histories)
        prev_hist = prev_cache.get("histories", {}) if prev_cache else {}
        h_filled = 0
        for sym in needed:
            if sym in histories and len(histories[sym]) >= 10:
                continue
            ph = prev_hist.get(sym)
            if ph and len(ph) >= 10:
                histories[sym] = [(int(a), float(b)) for a, b in ph]
                h_filled += 1
        if h_filled:
            print(f"[info] gap-filled {h_filled} stock histories from previous cache")
        try:
            _atomic_write_text(HIST_CACHE, json.dumps({k: [[a, b] for a, b in v] for k, v in histories.items()}))
            print(f"[ok] wrote history cache ({len(histories)} stocks)")
        except Exception as e:
            print(f"[warn] write hist cache: {e}", file=sys.stderr)

    # ─── S&P 500 TREND FILTER (calculé MAINTENANT, après que SPY soit en histories) ───
    print("[info] computing S&P 500 trend filter from fetched SPY history…")
    trend = fetch_sp500_trend(histories)
    print(f"[info] SP500 trend = {trend['mode']} (px={trend['idx_px']}, ma200={trend['ma200']})")

    # ─── HISTORICAL SHARES OUTSTANDING (pour pondération time-varying correcte) ───
    # Cache TTL séparé : les shares ne bougent qu'aux earnings (~1 fois/trimestre)
    # donc 24h de cache est largement suffisant.
    shares_cache_path = HIST_CACHE.with_name("tradfi_shares_history_cache.json")
    shares_hist_all = {}
    shares_age_h = float("inf")
    if shares_cache_path.exists():
        try:
            shares_age_h = (time.time() - shares_cache_path.stat().st_mtime) / 3600
            cached = json.load(open(shares_cache_path, "r", encoding="utf-8"))
            if shares_age_h < 24 and set(cached.keys()) >= (needed - {SP500_BENCH_KEY}):
                shares_hist_all = {k: [(int(a), int(b)) for a, b in v] for k, v in cached.items()}
                print(f"[info] using cached shares-history (age {shares_age_h:.1f}h, {len(shares_hist_all)} stocks)")
        except Exception as e:
            print(f"[warn] shares cache load: {e}", file=sys.stderr)
    if not shares_hist_all:
        print(f"[info] fetching historical shares-outstanding for {len(needed)} stocks (yfinance, threaded)…")
        sh_syms = sorted(needed - {SP500_BENCH_KEY})
        shares_hist_all = fetch_shares_history(sh_syms)
        try:
            _atomic_write_text(shares_cache_path,
                               json.dumps({k: [[a, b] for a, b in v] for k, v in shares_hist_all.items()}))
            print(f"[ok] wrote shares-history cache ({len(shares_hist_all)} stocks)")
        except Exception as e:
            print(f"[warn] write shares cache: {e}", file=sys.stderr)

    for s in stats_list:
        s["history"] = compute_narrative_index(s, histories, shares_hist_all=shares_hist_all)

    # ─── MOMENTUM METRICS (rel-SP500 + breadth + signal) ───
    augment_with_momentum_metrics(stats_list, histories)
    stats_list = compute_composite(stats_list)

    covered = set()
    for s in stats_list:
        for t in s["tokens"]:
            covered.add(t["symbol"])

    # Denominator for the coverage badge = unique tickers actually assigned to a
    # sector (NOT len(STOCKS), which also counts ~50 orphan metadata entries that
    # are defined but never placed in a narrative — those would understate the
    # badge, e.g. show 759/843 when only ~793 tickers are really tracked).
    defined_in_narratives = set()
    for cfg in NARRATIVES.values():
        defined_in_narratives.update(cfg.get("tickers", []))

    # ─── HISTOIRES EMBARQUÉES pour recalcul JS dynamique par région ───
    # On downsample encore côté output (daily récent + hebdo au-delà de 6 mois)
    # pour limiter la taille du JSON. ~260 points × 581 tickers ≈ 2 Mo gzippé.
    def _slim_history(h, max_pts=260):
        if not h:
            return []
        if len(h) <= max_pts:
            return [[int(ts), round(px, 4)] for ts, px in h]
        # keep every 2nd point if too long
        step = max(1, len(h) // max_pts)
        return [[int(ts), round(px, 4)] for ts, px in h[::step]]

    embedded_histories = {
        sym: _slim_history(hist) for sym, hist in histories.items() if hist
    }
    print(f"[info] embedding histories of {len(embedded_histories)} stocks into output JSON")

    # ── Historique LONG de SPY (33 ans) pour le graphe régimes ZEN/ALPHA ──
    spy_longhist = []
    try:
        import yfinance as yf
        print("[info] fetching SPY full history (max, ~32 ans depuis 1993) for regime chart…")
        t = yf.Ticker("SPY")
        long_df = t.history(period="max", interval="1d", auto_adjust=False)
        raw = []
        if long_df is not None and not long_df.empty and "Close" in long_df.columns:
            for idx, row in long_df.iterrows():
                try:
                    v = row["Close"]
                    if v is None or (hasattr(v, '__len__') and len(v)==0):
                        continue
                    v_scalar = float(v)
                    if v_scalar > 0:
                        raw.append((int(idx.timestamp()), v_scalar))
                except Exception:
                    continue
        if len(raw) > 200:
            cutoff = time.time() - 730 * 86400
            old    = [p for p in raw if p[0] < cutoff]
            recent = [p for p in raw if p[0] >= cutoff]
            old_weekly = old[::5]
            spy_longhist = [[ts, round(px, 4)] for ts, px in old_weekly + recent]
            print(f"[ok] SPY max: {len(raw)} raw → {len(spy_longhist)} pts (downsampled)")
        else:
            print(f"[warn] SPY max returned only {len(raw)} points", file=sys.stderr)
    except Exception as e:
        print(f"[warn] SPY max fetch failed: {e}", file=sys.stderr)

    # Fallback : si le fetch dédié 33 ans a échoué (rate-limit Yahoo après le batch),
    # réutiliser la SPY history des histories (15 ans). Mieux que rien pour le
    # régime breakdown ALPHA/ZEN — couvre 2 cycles bear (2020, 2022) et 2 bull.
    if not spy_longhist and histories.get("SPY"):
        spy_15y = sorted(histories["SPY"], key=lambda p: p[0])
        spy_longhist = [[int(ts), round(float(px), 4)] for ts, px in spy_15y if px and px > 0]
        print(f"[info] SPY longhist fallback to 15y history ({len(spy_longhist)} pts) — yfinance max fetch failed", file=sys.stderr)
    # Fallback ultime : conserver le longhist du cache précédent si on a vraiment rien
    if not spy_longhist and prev_cache and prev_cache.get("spy_longhist"):
        spy_longhist = prev_cache["spy_longhist"]
        print(f"[info] SPY longhist reused from previous cache ({len(spy_longhist)} pts)", file=sys.stderr)

    # ── Ampleur du marché dans le temps (jauge dashboard) — sur histories complètes ──
    breadth_series, breadth_now = compute_breadth_history(stats_list, histories, _breadth_key_tradfi)
    print(f"[ok] breadth_history: {len(breadth_series)} pts, now={breadth_now}")

    # ══════════════════════════════════════════════════════════════════════
    # GARDE-FOU ANTI-CACHE-VIDE (incident 2026-07-31, [[feedback_no_recurrence_safeguard]])
    # ──────────────────────────────────────────────────────────────────────
    # Ce jour-là : DNS mort ("Could not resolve host: query2.finance.yahoo.com")
    # → 0/812 quotes, budget quotes épuisé, gap-fill impossible (cache précédent
    # tronqué par un hard-kill) → le script a quand même ÉCRASÉ le bon cache par
    # 39 secteurs à 0 token / perf nulles. Côté site : "+0.0%" partout, ce qui se
    # lit comme "marché plat" et non comme "données manquantes".
    #
    # Règle : on n'écrase JAMAIS un cache sain par un cache dégradé. Si le
    # résultat du run est vide (ou effondré vs le précédent) ET que le cache sur
    # disque est encore sain, on abandonne l'écriture et on sort en erreur — le
    # freshness-watchdog relancera, et la page reste sur des données un peu
    # anciennes (visible via le badge "il y a Xh") plutôt que sur des zéros.
    # Si le cache sur disque est LUI-MÊME dégradé, on écrit quand même : sinon
    # une seule mauvaise journée figerait le pipeline pour toujours.
    n_covered = len(covered)
    prev_covered = int((prev_cache.get("coverage") or {}).get("stocks_in_narratives") or 0)
    abort, keep_prev = write_guard(n_covered, prev_covered)
    if keep_prev:
        print(f"[ABORT] écriture annulée — {abort}.", file=sys.stderr)
        print(f"[ABORT] le cache existant ({prev_covered} tickers, updated="
              f"{prev_cache.get('updated')}) est CONSERVÉ intact. "
              f"Cause probable : source injoignable (voir les erreurs ci-dessus). "
              f"Le run suivant reprendra.", file=sys.stderr)
        sys.exit(3)
    if abort:
        print(f"[warn] run dégradé ({abort}) mais le cache sur disque l'est aussi "
              f"({prev_covered} tickers) → on écrit quand même pour ne pas figer le pipeline.",
              file=sys.stderr)

    out = {
        "updated":      datetime.now(timezone.utc).isoformat(),
        "news_updated": news_data.get("updated"),
        "articles_scanned": len(macro_news),
        "total_matches": total_matches,
        "trend_filter": trend,
        "coverage": {
            "stocks_in_narratives": len(covered),
            "total_stocks_defined": len(defined_in_narratives),
            "live_pct": coverage_pct,
            "stale_filled": filled_stale,
        },
        "narratives": stats_list,
        "histories":  embedded_histories,
        "spy_symbol": SP500_BENCH_KEY,
        "spy_longhist": spy_longhist,  # 20 ans pour graphe régimes ZEN/ALPHA
        "breadth_history": breadth_series,  # % actions en hausse 30j, moyenne secteurs
        "breadth_now": breadth_now,         # dernier point = valeur d'aiguille jauge
    }
    # Écritures ATOMIQUES : un hard-kill du watchdog en plein write laissait
    # sinon un JSON tronqué, qui cassait le gap-fill du run suivant (2026-07-31).
    _atomic_write_text(OUT_CACHE, json.dumps(out, ensure_ascii=False, indent=2))
    print(f"[ok] wrote {OUT_CACHE} ({n_covered} tickers, {coverage_pct}% frais)")
    # Cache JS live consomme par Narrative_Tracker.html (override de __TRADFI_DATA__ inline)
    _atomic_write_text(OUT_CACHE_JS,
                       "window.__TRADFI_LIVE__=" + json.dumps(out, ensure_ascii=False, separators=(",", ":")) + ";\n")
    print(f"[ok] wrote {OUT_CACHE_JS}")
    # Wrapper LEGER pour la tuile "Mode TradFi" de l'Accueil (file://-safe, MEME
    # source trend_filter que le badge ZEN/ALPHA de TradFi_Tracker). Evite de
    # charger les ~13M du cache complet juste pour 2 champs. Reecrit a chaque run
    # => la tuile reste live et coherente sans re-render de index.Rmd.
    mode_js = CACHE_DIR / "mode_tradfi_live.js"
    mode_tradfi = {
        "mode": trend.get("mode"),
        "dist_pct": trend.get("distance_ma200"),
        "price": trend.get("idx_px"),
        "ma": trend.get("ma200"),
        "perf_30d": trend.get("perf_30d"),
        "ref_asset": "S&P 500",
        "ma_label": "MA200",
        "updated": out.get("updated"),
    }
    _atomic_write_text(mode_js,
                       "window.__MODE_TRADFI_LIVE__=" + json.dumps(mode_tradfi, ensure_ascii=False, separators=(",", ":")) + ";\n")
    print(f"[ok] wrote {mode_js}")
    print("\nTop 10 secteurs (momentum cyclique vs S&P 500):")
    for s in stats_list[:10]:
        rel = s.get("rel_mom_90d")
        br  = s.get("breadth_30d")
        sig = s.get("signal", "?").upper()
        age = s.get("trend_age_days", 0)
        rel_s = f"{rel:+6.1f}%" if rel is not None else "  n/a "
        br_s  = f"{br:4.0f}%"   if br  is not None else " n/a"
        print(f"  #{s['rank']:>2} {s['narrative']:<26} score={s['score']:5.1f}  "
              f"[{sig:4} {age:>3}j]  rel90={rel_s}  breadth={br_s}  "
              f"px={s['price_momentum']:+5.1f}%")


if __name__ == "__main__":
    main()
