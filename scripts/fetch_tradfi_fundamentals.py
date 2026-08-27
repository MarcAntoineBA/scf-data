#!/usr/bin/env python3
"""TradFi Fundamental Snapshot per Sector.

Computes sector-level weighted averages of fundamental metrics (P/E, P/B, P/S,
Div Yield, ROE, FCF yield, Beta, growth, perf 1Y) from yfinance stock data.

Reuses the NARRATIVES taxonomy from fetch_tradfi.py (same sectors/tickers).

Output:
- tradfi_fundamentals_cache.json : machine-readable
- tradfi_fundamentals_cache.js  : window.__TRADFI_FUNDAMENTALS__ = {...}

Schema :
{
  "updated": "YYYY-MM-DD HH:MM CEST",
  "sources": [...],
  "sectors": [
    {
      "narrative": "...", "icon": "...", "color": "...",
      "n_stocks": 10, "n_with_data": 8, "mcap_total_b": 3500.2,
      "pe_ttm": 28.4, "pe_fwd": 24.1, "pb": 5.2, "ps": 3.8,
      "div_yield": 1.5, "eps_growth_fwd": 12.5, "rev_growth_ttm": 9.8,
      "roe": 22.4, "fcf_yield": 4.1, "beta": 1.05, "perf_1y": 15.3,
      "stocks": [
        {"symbol":"AAPL", "name":"Apple", "mcap_b":3100.0, "currency":"USD",
         "pe_ttm":28.5, "pe_fwd":25.0, "pb":45.2, "ps":7.8,
         "div_yield":0.44, "eps_growth_fwd":12.0, "rev_growth_ttm":8.5,
         "roe":170.0, "fcf_yield":3.4, "beta":1.20, "perf_1y":18.5},
        ...
      ]
    }, ...
  ]
}

Scheduled: launchd every 6h (scf.tradfifund.plist).
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
import sys
import threading
import time
import random
import warnings
import os
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")

# ─── Paths ──────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "tradfi_fundamentals_cache.json"
OUT_JS   = CACHE_DIR / "tradfi_fundamentals_cache.js"
TRACKER_CACHE = CACHE_DIR / "tradfi_cache.json"   # written by fetch_tradfi.py
LOCK_FILE  = CACHE_DIR / "tradfi_fundamentals.lock"
# Cache SQLite yfinance PRIVÉ à ce script (2026-07-31), cf. fetch_tradfi.py.
# Purger le cache PARTAGÉ pendant qu'un autre fetcher yfinance tourne lui fait
# perdre le fichier sous son handle SQLite ouvert → cascade
# OperationalError('no such table: _tz_kv') → 0 ticker récupéré.
YF_CACHE_DIR = Path.home() / "Library" / "Caches" / "py-yfinance-tradfifund"


# ─────────────────────────────────────────────────────────────────────────
# Robustness helpers (singleton lock + yfinance SQLite reset + prev_cache merge)
# Same pattern as fetch_tradfi.py / fetch_narratives.py — see
# project_site_crypto_launchd_pattern.md for rationale.
# ─────────────────────────────────────────────────────────────────────────
def acquire_singleton_lock():
    import fcntl
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print("[lock] another fetch_tradfi_fundamentals instance is running — exiting", file=sys.stderr)
        fd.close()
        return None
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


# Cache yfinance par défaut, PARTAGÉ par les fetchers sans répertoire privé.
# Le purger casse tous les runs concurrents : le garde-fou ci-dessous refuse de
# le faire, quoi qu'on mette dans YF_CACHE_DIR.
YF_SHARED_DIR = Path.home() / "Library" / "Caches" / "py-yfinance"


def _init_yfinance_cache():
    """Isole les caches SQLite de yfinance (tz + cookies + isin) dans
    YF_CACHE_DIR, privé à ce script. À appeler AVANT tout usage de yfinance."""
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
            f.unlink(); removed += 1
        except Exception:
            pass
    if removed:
        print(f"[reset] cleared {removed} yfinance SQLite file(s)", file=sys.stderr)


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
    """Read tradfi_cache.json (price + momentum). Returns:
        {
          'quotes':    {sym: {price, mcap, perf_30d, currency, ...}},
          'momentum':  {sector_name: {score, signal, rank, rel_mom_90d, breadth_30d, trend_age_days, perf_90d_w}},
          'updated':   '...'
        }
    Empty dicts if tracker cache missing — funda will still run but with no
    cross-data. Single source of truth for prices/mcap = tracker."""
    if not TRACKER_CACHE.exists():
        print(f"[warn] {TRACKER_CACHE.name} missing — fundamentals will refetch prices/mcap from yfinance", file=sys.stderr)
        return {"quotes": {}, "momentum": {}, "updated": None}
    try:
        with TRACKER_CACHE.open("r", encoding="utf-8") as f:
            tc = json.load(f)
    except Exception as e:
        print(f"[warn] cannot read {TRACKER_CACHE.name}: {e}", file=sys.stderr)
        return {"quotes": {}, "momentum": {}, "updated": None}
    quotes = {}
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
        for t in n.get("tokens", []):
            sym = t.get("symbol")
            if sym and sym not in quotes:
                quotes[sym] = {
                    "price":     t.get("price"),
                    "price_local": t.get("price_local"),
                    "mcap":      t.get("mcap"),
                    "perf_7d":   t.get("perf_7d"),
                    "perf_30d":  t.get("perf_30d"),
                    "currency":  t.get("currency"),
                    "_stale":    t.get("_stale"),
                    "_source":   t.get("_source"),
                }
    print(f"[info] tracker data loaded: {len(quotes)} quotes, {len(momentum)} sectors (cache mtime: {tc.get('updated')})")
    return {"quotes": quotes, "momentum": momentum, "updated": tc.get("updated")}

# ─── Import sector taxonomy from fetch_tradfi.py ────────────────────────
try:
    from fetch_tradfi import NARRATIVES, detect_currency, fetch_fx_rates, apply_fx
except Exception as e:
    print(f"[fatal] Cannot import from fetch_tradfi: {e}", file=sys.stderr)
    sys.exit(1)

# ─── yfinance fundamentals fetch ────────────────────────────────────────
def _safe(v, maximum=None, positive=False):
    """Convert yfinance value to clean float or None.

    positive=True : a non-positive value is returned as None. Used for ratios
    that are economically meaningless when <= 0 — a negative P/E means the
    company is loss-making (no meaningful earnings multiple) and a negative
    P/B means negative book equity (heavy buybacks). Reporting these as "n/a"
    rather than a negative number avoids polluting the sector aggregate
    (e.g. OKLO pe_fwd=-80 dragging the Nucléaire P/E negative)."""
    try:
        if v is None:
            return None
        f = float(v)
        if f != f or f == float("inf") or f == float("-inf"):
            return None
        if maximum is not None and abs(f) > maximum:
            return None
        if positive and f <= 0:
            return None
        return f
    except Exception:
        return None


# ─── PEA eligibility ────────────────────────────────────────────────────
# The legal test is the COMPANY'S REGISTERED SEAT being in the European
# Economic Area — not where the share happens to be listed. Deriving it from
# the exchange (the shortcut every competitor takes) is wrong in both
# directions: a Paris listing does not make a company European, and a company
# can be European while trading elsewhere.
#
# yfinance's `country` is the address on Yahoo's profile — the OPERATIONAL
# headquarters. That is right far more often than the exchange, and wrong in a
# known, small, enumerable set of cases: companies incorporated in one country
# and run from another. Those get an explicit override below, each with its
# reason, so the exception is auditable rather than folded invisibly into a
# heuristic. When the override list and reality diverge, the fix is one line
# here, not a rewrite.
_EEA_COUNTRIES = {
    # 27 EU member states
    "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czech Republic",
    "Czechia", "Denmark", "Estonia", "Finland", "France", "Germany", "Greece",
    "Hungary", "Ireland", "Italy", "Latvia", "Lithuania", "Luxembourg", "Malta",
    "Netherlands", "Poland", "Portugal", "Romania", "Slovakia", "Slovenia",
    "Spain", "Sweden",
    # EEA, non-EU
    "Iceland", "Liechtenstein", "Norway",
}

# symbol -> (eligible, reason). Reviewed 2026-08-27.
_PEA_OVERRIDES = {
    # Registered in the Netherlands, run from Geneva. Yahoo says Switzerland.
    # This is the textbook case — the competitor documents it as a known error
    # in his own help pages and ships it anyway.
    "STM.PA":  (True,  "STMicroelectronics N.V., société de droit néerlandais (siège opérationnel à Genève)"),
    "STM":     (True,  "STMicroelectronics N.V., société de droit néerlandais"),
    # Dutch-incorporated groups whose Yahoo profile shows the operating country.
    "RACE.MI": (True,  "Ferrari N.V., société de droit néerlandais"),
    "RACE":    (True,  "Ferrari N.V., société de droit néerlandais"),
    "STLAM.MI": (True, "Stellantis N.V., société de droit néerlandais"),
    "STLA":    (True,  "Stellantis N.V., société de droit néerlandais"),
    "AIR.PA":  (True,  "Airbus SE, société de droit néerlandais"),
    # Listed in Amsterdam / Paris but incorporated outside the EEA.
    "CNHI":    (False, "CNH Industrial N.V. a transféré son siège au Royaume-Uni en 2024"),
}


def pea_eligible(symbol, country):
    """(bool | None, reason). None means 'unknown' — never guessed.

    Returning None rather than False when the country is missing matters: a
    company shown as "non éligible" because Yahoo had no address is a wrong
    answer wearing the clothes of a right one. The page renders "—" instead.
    """
    ov = _PEA_OVERRIDES.get(symbol)
    if ov is not None:
        return ov[0], ov[1]
    if not country:
        return None, "pays du siège inconnu"
    if country in _EEA_COUNTRIES:
        return True, f"siège en {country}, dans l'Espace économique européen"
    return False, f"siège en {country}, hors Espace économique européen"


def _cur_fy_glitched(pe_cur, pe_next, pe_ttm):
    """True when Yahoo's current-FY figure (priceEpsCurrentYear / epsCurrentYear)
    is a currency/scale glitch: it dwarfs BOTH the trailing and next-FY P/E (>3x the
    larger sane reference). GMEXICOB.MX 286 vs trailing 17, EVO.ST 130 vs 12,
    HEXA-B.ST 237 vs 10. Used to reject both the glitched forward P/E AND the forward
    EPS growth, since they derive from the same bad epsCurrentYear. Legit high
    multiples (TSLA, ARM, NET, Adani) survive because their next-FY P/E is also high."""
    ref = [x for x in (pe_ttm, pe_next) if x]
    return bool(pe_cur and ref and pe_cur > 3 * max(ref))


def _pick_forward_pe(pe_cur, pe_next, pe_ttm):
    """Forward P/E = info["forwardPE"] = price / next-FY EPS estimate. This is the
    value Yahoo displays on /key-statistics as "Forward P/E" (audit verified 2026-06-03
    on NVDA 17.6, AAPL 32.8, 1810.HK 18.6 — matched live page). Prior implementation
    used priceEpsCurrentYear (current-FY EPS basis) which read systematically higher
    on growth names and did NOT match the linked Yahoo page.
    Fallback to priceEpsCurrentYear (pe_cur) when forwardPE missing/glitched."""
    if pe_next and not _cur_fy_glitched(pe_next, pe_cur, pe_ttm):
        return pe_next
    return pe_cur or pe_next


def bulk_fetch_growth_cy(all_syms, chunk=40):
    """Bulk-fetch Yahoo 'Growth Estimates → Current Year' (both EPS and Revenue)
    for every ticker via yahooquery's earnings_trend module (batched).

    Returns {"eps": {sym: pct}, "rev": {sym: pct}}.

    Done up-front in one pass (20-ish HTTP calls for 800 tickers) so the per-ticker
    yfinance loop doesn't have to make individual quoteSummary?modules=earningsTrend
    calls — which rate-limit hard at 800x. Both values match what Yahoo displays on
    /analysis (Growth Estimates table for EPS, Revenue Estimate "Sales Growth
    (year/est)" for revenue) so audit click-throughs verify on the same page.
    """
    try:
        from yahooquery import Ticker as _YQT
    except ImportError:
        return {"eps": {}, "rev": {}}
    eps_out, rev_out = {}, {}
    for i in range(0, len(all_syms), chunk):
        batch = all_syms[i:i + chunk]
        try:
            et = _YQT(batch, asynchronous=False, validate=False).earnings_trend
        except Exception as e:
            print(f"[warn] bulk earnings_trend chunk {i // chunk + 1} failed: {e}", file=sys.stderr)
            continue
        if not isinstance(et, dict):
            continue
        for sym in batch:
            entry = et.get(sym)
            trends = (entry or {}).get("trend") if isinstance(entry, dict) else None
            if not isinstance(trends, list):
                continue
            for tr in trends:
                if not isinstance(tr, dict) or tr.get("period") != "0y":
                    continue
                # EPS growth (Yahoo "Growth Estimates Current Year")
                g = tr.get("growth")
                try:
                    gv = float(g) if g is not None else None
                except (TypeError, ValueError):
                    gv = None
                if gv is not None and gv == gv:
                    eps_out[sym] = gv * 100
                # Revenue growth (Yahoo "Sales Growth (year/est) Current Year")
                rest = tr.get("revenueEstimate") if isinstance(tr.get("revenueEstimate"), dict) else None
                rg = (rest or {}).get("growth")
                try:
                    rgv = float(rg) if rg is not None else None
                except (TypeError, ValueError):
                    rgv = None
                if rgv is not None and rgv == rgv:
                    rev_out[sym] = rgv * 100
                break
    return {"eps": eps_out, "rev": rev_out}


_CROISSANCES_CACHE = {}
# Le drapeau est SÉPARÉ du contenu, et c'est tout l'objet de la correction : un
# dictionnaire non vide n'est pas un dictionnaire complet. Sans cette
# distinction, un fil arrivant pendant le remplissage repartait avec un
# soixante-quatrième de la carte — mesuré, 328 titres servis au lieu de 774.
_CROISSANCES_PRET = [False]
_CROISSANCES_VERROU = threading.Lock()


def _croissances_marche():
    """Les croissances du chiffre d'affaires, depuis la collecte de marche.

    La source d'origine — la table « Revenue Estimate » de Yahoo, lue par
    yahooquery — rend desormais 401 « Invalid Crumb ». Yahoo l'a fermee, et le
    champ tombait a 56 lignes sur 826.

    La collecte de marche sert la meme famille pour 38 061 societes :
        croissance_ca_pct           constatee, douze derniers mois   97,1 %
        croissance_ca_exercice_pct  attendue, exercice en cours      39,1 %

    Sur les 783 titres du tracker retrouves dans cette collecte, 774 ont la
    croissance constatee et 757 l'attendue.
    """
    if _CROISSANCES_PRET[0]:
        return _CROISSANCES_CACHE
    with _CROISSANCES_VERROU:
        if _CROISSANCES_PRET[0]:
            return _CROISSANCES_CACHE
        return _remplir_croissances()


def _remplir_croissances():
    """Remplit la carte une fois pour toutes. Appelé sous verrou."""
    import glob as _g
    for f in sorted(_g.glob(str(CACHE_DIR / "marche_[0-9]*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        ch = d.get("champs") or []
        i_c = ch.index("croissance_ca_pct") if "croissance_ca_pct" in ch else None
        i_e = (ch.index("croissance_ca_exercice_pct")
               if "croissance_ca_exercice_pct" in ch else None)
        if i_c is None and i_e is None:
            continue
        for sym, a in (d.get("societes") or {}).items():
            _CROISSANCES_CACHE[sym] = (
                a[i_c] if (i_c is not None and i_c < len(a)) else None,
                a[i_e] if (i_e is not None and i_e < len(a)) else None,
            )
    _CROISSANCES_PRET[0] = True
    if _CROISSANCES_CACHE:
        print("[info] croissances du CA : %d societes lues dans la collecte de "
              "marche" % len(_CROISSANCES_CACHE))
    else:
        print("[warn] collecte de marche introuvable : la croissance du chiffre "
              "d'affaires restera vide", file=sys.stderr)
    return _CROISSANCES_CACHE


def fetch_stock_fundamentals(symbol, fx_rates, tracker_quotes=None, growth_cy_map=None):
    """Fetch fundamentals for a single stock via yfinance.

    Price/mcap come from tracker_quotes (tradfi_cache.json) when available — this
    guarantees the funda tab and the tracker tab show the SAME prices/mcap.
    yfinance `.info` is still needed for P/E, P/B, ROE etc. (no other free source).
    growth_cy_map is the pre-fetched {"eps": {...}, "rev": {...}} from yahooquery
    bulk earnings_trend (matches Yahoo /analysis Growth + Revenue Estimate tables).

    Returns dict or None on failure.
    """
    try:
        import yfinance as yf
    except ImportError:
        return None

    tracker_quotes = tracker_quotes or {}
    tq = tracker_quotes.get(symbol) or {}

    # price + mcap from TRACKER (single source of truth)
    # mcap in tradfi_cache.json is already USD-converted; price is USD too.
    mcap_usd_from_tracker = _safe(tq.get("mcap"), maximum=1e15) or 0
    price_usd_from_tracker = _safe(tq.get("price"))

    try:
        t = yf.Ticker(symbol)
    except Exception as e:
        print(f"[warn] {symbol}: Ticker() failed: {e}", file=sys.stderr)
        return None

    # Fallback to fast_info ONLY if tracker had no data for this ticker
    mcap_local_fallback = 0
    if not mcap_usd_from_tracker:
        try:
            fi = t.fast_info
            mcap_local_fallback = _safe(getattr(fi, "market_cap", None), maximum=1e15) or 0
        except Exception:
            pass

    # fundamentals (expensive). Yahoo's quoteSummary endpoint rate-limits hard
    # under sustained concurrent load (HTTP 429), so we retry with backoff +
    # jitter rather than giving up on the first miss — a single-shot .info caused
    # ~57% of tickers to fall back to stale cache (2026-05-28 audit). A sparse
    # dict (<5 keys) is also treated as a throttled response and retried.
    info = {}
    for attempt in range(3):
        try:
            info = t.info or {}
            if len(info) >= 5:
                break
        except Exception as e:
            if attempt == 2:
                print(f"[warn] {symbol}: .info failed after retries: {e}", file=sys.stderr)
        # backoff with jitter before next attempt (skip after last)
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1) + random.uniform(0, 0.8))

    if not info or len(info) < 5:
        return None

    ccy = detect_currency(symbol)

    # Hard caps reflect economically plausible ranges. Values beyond these are
    # almost always Yahoo data errors (e.g. WBD pe_fwd=1828 with mcap=68B
    # leaking into Média & Streaming, HCLTECH.NS ps=215, GMEXICOB.MX ps=80).
    # Anything beyond is dropped at source — bias toward conservatism since
    # the bigger sin is a poisoned sector aggregate, not a missing data point.
    pe_ttm = _safe(info.get("trailingPE"),  maximum=200, positive=True)
    # Yahoo's web "Forward P/E" on /key-statistics = info["forwardPE"] = price /
    # next-FY EPS estimate. Audit 2026-06-03 vs live page : NVDA 17.6, AAPL 32.8,
    # 1810.HK 18.6 — all matched. priceEpsCurrentYear (current-FY basis) was used
    # previously and read systematically higher on growth names (NVDA 24.9 vs
    # Yahoo's 17.6) — kept only as fallback when forwardPE missing or glitched
    # (UK pence / exotic FX cases like BARC.L raw 743). Cap 300 covers extreme
    # cases like TSLA pre-earnings while filtering Yahoo data errors.
    pe_cur = _safe(info.get("priceEpsCurrentYear"), maximum=300, positive=True)
    pe_nxt = _safe(info.get("forwardPE"), maximum=300, positive=True)
    pe_fwd = _pick_forward_pe(pe_cur, pe_nxt, pe_ttm)
    pb     = _safe(info.get("priceToBook"), maximum=100, positive=True)
    ps     = _safe(info.get("priceToSalesTrailing12Months"), maximum=50, positive=True)

    # dividendYield from yfinance is ALREADY in % units (e.g. 1.45 for 1.45%),
    # despite the name. We preserve as-is (% points).
    dy_raw = info.get("dividendYield")
    div_yield = _safe(dy_raw)

    # Forward EPS growth = Yahoo's "Growth Estimates → Current Year" value visible
    # on the /analysis page our audit modal links to (consensus FY-current vs
    # FY-prior reported, computed by Yahoo). Mirrors the prominent table on Yahoo
    # so a click-through shows the same percentage. Fallback when Yahoo's trend
    # endpoint is missing: epsCurrentYear / trailingEps − 1 (TTM-based, the prior
    # methodology — coherent with forward P/E but not directly displayed on /analysis).
    eps_cur = _safe(info.get("epsCurrentYear"))
    eps_ttm = _safe(info.get("trailingEps"))
    gcy = growth_cy_map or {}
    yahoo_eps_growth_cy = _safe((gcy.get("eps") or {}).get(symbol), maximum=500)
    yahoo_rev_growth_cy = _safe((gcy.get("rev") or {}).get(symbol), maximum=500)
    eps_growth_fwd = None
    if (yahoo_eps_growth_cy is not None
            and not _cur_fy_glitched(pe_cur, pe_nxt, pe_ttm)):
        eps_growth_fwd = yahoo_eps_growth_cy
    elif (eps_cur is not None and eps_ttm is not None and eps_ttm > 0
            and not _cur_fy_glitched(pe_cur, pe_nxt, pe_ttm)):
        eps_growth_fwd = (eps_cur / eps_ttm - 1) * 100
    # Revenue growth (forward) = Yahoo "Sales Growth (year/est) Current Year"
    # from /analysis Revenue Estimate table. No fallback to info["revenueGrowth"]:
    # that field is quarterly YoY (different metric, different page) — would silently
    # mismatch the audit link. Better to show "—" than a wrong number.
    # ── La croissance du chiffre d'affaires ──
    # Ce champ s'appelle « ttm » — douze mois glissants — et contenait une
    # ESTIMATION de l'exercice en cours. Le nom mentait avant meme que la source
    # ne meure. Il prend desormais la croissance CONSTATEE, qui est ce que son
    # nom annonce, ce que le libelle de la page laisse attendre, et la mieux
    # servie : 774 titres suivis sur 783 contre 56 par l'ancienne voie.
    _cr = _croissances_marche().get(symbol) or (None, None)
    rev_growth_ttm = _safe(_cr[0], maximum=500)
    rev_growth_fwd = _safe(_cr[1], maximum=500)
    if rev_growth_ttm is None:
        # Faute de mieux, l'ancienne voie — quand elle repond encore.
        rev_growth_ttm = yahoo_rev_growth_cy
    if rev_growth_fwd is None:
        rev_growth_fwd = yahoo_rev_growth_cy
    roe = _safe(info.get("returnOnEquity"))
    if roe is not None:
        roe *= 100
    # Yahoo regularly returns beta near 0 for emerging-market tickers when its
    # benchmark (S&P 500) doesn't fit (Aramco 0.005, BBCA.JK 0.013). These
    # absurd values dragged Pétrole & Gaz beta to 0.197 due to mcap weighting.
    # Drop anything below 0.05 (no real listed equity has beta < 0.05 vs SPY).
    beta = _safe(info.get("beta"), maximum=4)
    if beta is not None and beta < 0.05:
        beta = None

    # FCF yield = freeCashflow / marketCap (use tracker-USD mcap for consistency)
    fcf = _safe(info.get("freeCashflow"))
    # FCF in info is in local currency → convert to USD before dividing
    # apply_fx returns None when FX is missing → treat fcf_usd as unknown (no fallback to raw local)
    fcf_usd = apply_fx(fcf, ccy, fx_rates) if fcf is not None else None
    # mcap source priority: tracker (already USD) > info.marketCap (local→USD) > fast_info fallback (local→USD)
    if mcap_usd_from_tracker:
        mcap_usd = mcap_usd_from_tracker
    else:
        mcap_raw_local = _safe(info.get("marketCap")) or mcap_local_fallback
        mcap_usd = (apply_fx(mcap_raw_local, ccy, fx_rates) or 0) if mcap_raw_local else 0
    fcf_yield = None
    if fcf_usd is not None and mcap_usd and mcap_usd > 0:
        fcf_yield = (fcf_usd / mcap_usd) * 100

    # 52W change as proxy for perf_1y (more reliable than computing from history)
    perf_1y = _safe(info.get("52WeekChange"))
    if perf_1y is not None:
        perf_1y *= 100

    # Use longName for display if available
    name = info.get("longName") or info.get("shortName") or symbol

    # ── CHAMPS DÉJÀ TÉLÉCHARGÉS, JUSQU'ICI JETÉS (2026-08-27) ────────────
    # `t.info` est un dict complet : tout ce qui suit arrive dans la MÊME réponse
    # réseau que les onze ratios historiques. Les extraire coûte zéro requête,
    # zéro seconde, zéro quota — seulement quelques kilo-octets de cache. Ils
    # débloquent la fiche société (marges, santé financière), le screener
    # (classification, PEA) et la note quantitative.
    #
    # Discipline reprise du reste du fichier : un plafond économiquement plausible
    # sur chaque ratio, et « rien » plutôt qu'un chiffre douteux.

    def _pct(key, maximum=None):
        """yfinance rend les marges et taux en FRACTION (0,724 = 72,4 %)."""
        v = _safe(info.get(key), maximum=maximum)
        return v * 100 if v is not None else None

    # Classification. Yahoo publie 11 secteurs et ~145 industries — c'est la
    # taxonomie qui manquait au site, qui n'avait que ses 39 narratifs
    # thématiques (lesquels mêlent secteurs et thèmes, cf. volet D du plan).
    sector_std = (info.get("sector") or "").strip() or None
    industry   = (info.get("industry") or "").strip() or None
    country    = (info.get("country") or "").strip() or None
    pea_ok, pea_why = pea_eligible(symbol, country)

    # Valorisation d'entreprise. EV/EBITDA est le multiple qui reste lisible là
    # où le P/E ne l'est plus : il neutralise la structure de dette et les
    # rachats d'actions qui écrasent les capitaux propres (le P/B d'Apple à 42
    # ne dit pas « 42 fois trop cher », il dit « fonds propres rachetés »).
    ev_ebitda = _safe(info.get("enterpriseToEbitda"), maximum=200)
    ev_rev    = _safe(info.get("enterpriseToRevenue"), maximum=100)
    ev_usd    = apply_fx(_safe(info.get("enterpriseValue"), maximum=1e15), ccy, fx_rates)

    # Marges. Plafond à 1 (100 %) AVANT conversion : une marge au-delà relève de
    # l'erreur de source, pas de l'entreprise exceptionnelle.
    gross_margin = _pct("grossMargins", maximum=1.0)
    op_margin    = _pct("operatingMargins", maximum=1.0)
    net_margin   = _pct("profitMargins", maximum=2.0)   # net > CA possible (cession)
    ebitda_margin = _pct("ebitdaMargins", maximum=1.5)
    roa          = _pct("returnOnAssets", maximum=1.5)

    # Santé financière. `debtToEquity` est DÉJÀ en points de pourcentage chez
    # yfinance (150,5 = 150,5 %) — ne pas le multiplier une seconde fois.
    debt_to_equity = _safe(info.get("debtToEquity"), maximum=2000)
    current_ratio  = _safe(info.get("currentRatio"), maximum=50)
    quick_ratio    = _safe(info.get("quickRatio"), maximum=50)
    total_debt_usd = apply_fx(_safe(info.get("totalDebt"), maximum=1e15), ccy, fx_rates)
    total_cash_usd = apply_fx(_safe(info.get("totalCash"), maximum=1e15), ccy, fx_rates)
    net_debt_usd = None
    if total_debt_usd is not None and total_cash_usd is not None:
        net_debt_usd = total_debt_usd - total_cash_usd
    ebitda_usd = apply_fx(_safe(info.get("ebitda"), maximum=1e15), ccy, fx_rates)
    debt_ebitda = None
    if net_debt_usd is not None and ebitda_usd and ebitda_usd > 0:
        # Dette NETTE sur EBITDA. Le barème du concurrent prend la dette totale ;
        # on garde les deux, le champ dit lequel est lequel.
        debt_ebitda = round(net_debt_usd / ebitda_usd, 3)
    debt_ebitda_brut = None
    if total_debt_usd is not None and ebitda_usd and ebitda_usd > 0:
        debt_ebitda_brut = round(total_debt_usd / ebitda_usd, 3)

    # Distribution. Un payout > 100 % existe (on distribue plus qu'on ne gagne) ;
    # au-delà de 500 % c'est du bruit.
    payout_ratio = _pct("payoutRatio", maximum=5.0)

    # Détention. Sert l'onglet Société de la fiche en attendant le pivot 13F.
    held_inst    = _pct("heldPercentInstitutions", maximum=1.5)
    held_insider = _pct("heldPercentInsiders", maximum=1.0)

    # Croissance TRIMESTRIELLE glissante. Métrique DIFFÉRENTE de
    # `rev_growth_ttm` (qui vient de la table « Revenue Estimate » de la page
    # /analysis) : elle compare le dernier trimestre publié à celui d'un an plus
    # tôt. Elle est stockée sous son propre nom, jamais fondue dans l'autre —
    # c'est la règle déjà posée plus haut dans ce fichier : mieux vaut « — »
    # qu'un nombre juste au mauvais endroit. Elle sert de FILET quand le scraping
    # de /analysis tombe, et la fiche indique alors laquelle des deux elle montre.
    rev_growth_yoy_q = _pct("revenueGrowth", maximum=20.0)
    eps_growth_yoy_q = _pct("earningsGrowth", maximum=20.0)

    employees = _safe(info.get("fullTimeEmployees"), maximum=5e6)
    book_value = _safe(info.get("bookValue"))
    rev_per_share = _safe(info.get("revenuePerShare"))
    revenue_usd = apply_fx(_safe(info.get("totalRevenue"), maximum=1e15), ccy, fx_rates)
    ocf_usd = apply_fx(_safe(info.get("operatingCashflow"), maximum=1e15), ccy, fx_rates)

    # PAS de %CAPEX ici. La tentation était d'écrire (OCF − FCF) / OCF : c'est
    # l'identité comptable, et c'est faux avec CES deux nombres-là. Mesuré le
    # 2026-08-27 sur NVDA : OCF 125,7 Md$, FCF yfinance 46,3 Md$ → la formule
    # rend 63 % de CAPEX, quand le dépôt 10-K en donne ~6 %. Le `freeCashflow`
    # de yfinance retranche davantage que les seuls investissements corporels et
    # ne couvre pas la même période. Deux nombres justes séparément peuvent
    # produire un troisième absurde.
    # Le vrai %CAPEX viendra du XBRL de la SEC
    # (PaymentsToAcquirePropertyPlantAndEquipment sur
    # NetCashProvidedByUsedInOperatingActivities), où les deux termes sont du
    # même exercice et de la même définition.

    return {
        "symbol": symbol,
        "name": name,
        "currency": ccy,
        "mcap_b": round(mcap_usd / 1e9, 2) if mcap_usd else 0,
        "price_usd": price_usd_from_tracker,    # from tracker (None if unavailable)
        "perf_30d": tq.get("perf_30d"),         # from tracker
        "pe_ttm": pe_ttm,
        "pe_fwd": pe_fwd,
        "pb": pb,
        "ps": ps,
        "div_yield": div_yield,
        "eps_growth_fwd": eps_growth_fwd,
        "rev_growth_ttm": rev_growth_ttm,
        "rev_growth_fwd": rev_growth_fwd,
        "roe": roe,
        "fcf_yield": fcf_yield,
        "beta": beta,
        "perf_1y": perf_1y,
        "_mcap_source": "tracker" if mcap_usd_from_tracker else "info_or_fast_info",

        # ── ajouts du 2026-08-27, même réponse réseau ──
        "sector_std": sector_std,
        "industry": industry,
        "country": country,
        "pea": pea_ok,
        "pea_why": pea_why,
        "ev_ebitda": ev_ebitda,
        "ev_rev": ev_rev,
        "ev_usd": round(ev_usd) if ev_usd is not None else None,
        "gross_margin": gross_margin,
        "op_margin": op_margin,
        "net_margin": net_margin,
        "ebitda_margin": ebitda_margin,
        "roa": roa,
        "debt_to_equity": debt_to_equity,
        "current_ratio": current_ratio,
        "quick_ratio": quick_ratio,
        "total_debt_usd": round(total_debt_usd) if total_debt_usd is not None else None,
        "total_cash_usd": round(total_cash_usd) if total_cash_usd is not None else None,
        "net_debt_usd": round(net_debt_usd) if net_debt_usd is not None else None,
        "ebitda_usd": round(ebitda_usd) if ebitda_usd is not None else None,
        "debt_ebitda": debt_ebitda,
        "debt_ebitda_brut": debt_ebitda_brut,
        "payout_ratio": payout_ratio,
        "held_inst": held_inst,
        "held_insider": held_insider,
        "rev_growth_yoy_q": rev_growth_yoy_q,
        "eps_growth_yoy_q": eps_growth_yoy_q,
        "employees": int(employees) if employees is not None else None,
        "book_value": book_value,
        "rev_per_share": rev_per_share,
        "revenue_usd": round(revenue_usd) if revenue_usd is not None else None,
        "ocf_usd": round(ocf_usd) if ocf_usd is not None else None,

    }


# ─── Aggregation helpers ────────────────────────────────────────────────
# Toutes les métriques par action ne sont PAS agrégées. Chaque entrée de cette
# liste produit sept clés par secteur (valeur pondérée, médiane, min, max, n, et
# les deux bornes de winsorisation) : y verser les trente champs du titre ferait
# grossir le cache sans que personne ne lise la médiane sectorielle du nombre
# d'employés. On n'agrège que ce qui sert à SITUER un titre dans son secteur.
_METRIC_KEYS = [
    "pe_ttm", "pe_fwd", "pb", "ps", "div_yield",
    "eps_growth_fwd", "rev_growth_ttm", "roe", "fcf_yield", "beta", "perf_1y",
    # ajouts 2026-08-27
    "ev_ebitda", "gross_margin", "op_margin", "net_margin", "roa",
    "payout_ratio", "debt_ebitda",
]

# Ratios de valorisation : un P/E ou P/B négatif n'a aucun sens économique
# (earnings/book négatifs) et polluerait la moyenne pondérée du secteur.
# EV/EBITDA rejoint la liste pour la même raison : un EBITDA négatif rend le
# multiple ininterprétable, et sa valeur négative tirerait la médiane du secteur
# vers le bas comme si l'entreprise était bon marché.
_POSITIVE_ONLY = {"pe_ttm", "pe_fwd", "pb", "ps", "ev_ebitda"}

# Métriques sensibles aux outliers extrêmes (single-stock blow-ups Yahoo)
# qu'on winsorise (capping aux percentiles p_low/p_high) AVANT moyenne pondérée.
# Sans ça, ONC eps_g=+16970% drague Biotech à +660% au lieu de la médiane +43%.
# Cf rapport d'audit 2026-05-21.
_WINSOR_METRICS = {
    "eps_growth_fwd": (5, 95),
    "rev_growth_ttm": (5, 95),
    "pe_ttm":         (5, 95),
    "pe_fwd":         (5, 95),
    "pb":             (5, 95),
    "ps":             (5, 95),
    "perf_1y":        (5, 95),
    "fcf_yield":      (5, 95),
    "roe":            (5, 95),
    # ajouts 2026-08-27 — mêmes causes, même traitement. `debt_ebitda` en
    # particulier : une société sans dette nette sort un ratio très négatif
    # (trésorerie > dette) qui écraserait la moyenne pondérée du secteur.
    "ev_ebitda":      (5, 95),
    "gross_margin":   (5, 95),
    "op_margin":      (5, 95),
    "net_margin":     (5, 95),
    "roa":            (5, 95),
    "payout_ratio":   (5, 95),
    "debt_ebitda":    (5, 95),
}

# Couverture minimale (n_with_data / n_total) en dessous de laquelle une
# métrique sectorielle est marquée comme insuffisamment échantillonnée.
# Le frontend doit afficher "—" plutôt qu'un chiffre trompeur.
_MIN_COVERAGE = 0.40


def _percentile(values_sorted, p):
    """p in [0,100]. linear interp; values_sorted is sorted ascending."""
    if not values_sorted:
        return None
    if len(values_sorted) == 1:
        return values_sorted[0]
    k = (len(values_sorted) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(values_sorted) - 1)
    frac = k - lo
    return values_sorted[lo] * (1 - frac) + values_sorted[hi] * frac


def _winsorize(pairs, p_low, p_high):
    """pairs = [(value, weight), …]. Returns the same list with values
    clipped to the p_low/p_high percentiles (unweighted; the sample is
    small enough — ~10-40 stocks — that unweighted percentiles work well)."""
    if len(pairs) < 5:
        return pairs, None, None  # too few points to winsorize meaningfully
    vals = sorted(v for v, _ in pairs)
    lo = _percentile(vals, p_low)
    hi = _percentile(vals, p_high)
    if lo is None or hi is None or lo >= hi:
        return pairs, None, None
    return [(min(max(v, lo), hi), w) for v, w in pairs], lo, hi


def aggregate_sector(stocks):
    """Compute mcap-weighted mean of each metric over stocks, with:

      • Winsorisation (5/95) for outlier-sensitive metrics (growth, ratios)
        — single-stock blow-ups don't dominate the sector average anymore.
      • Median computed alongside the weighted mean — flagged in the output
        so the frontend can show the gap and the user can audit.
      • Per-metric coverage: n_with_data is now {metric: count} so the
        frontend can hide cells with <40% sample coverage.
      • Outlier audit: which stocks were clipped, with their raw values.

    Returns (summary_dict, coverage_dict, mcap_total_b, outlier_log).
      summary_dict   : {metric: weighted_mean, metric_median, metric_min, metric_max, metric_n}
      coverage_dict  : {metric: count_with_data}
      mcap_total_b   : float
      outlier_log    : list of {metric, symbol, raw_value, clipped_to}
    """
    out = {}
    coverage = {k: 0 for k in _METRIC_KEYS}
    mcap_total = sum((s["mcap_b"] or 0) for s in stocks)
    n_total = len(stocks)
    outliers = []

    for k in _METRIC_KEYS:
        positive_only = k in _POSITIVE_ONLY
        raw_pairs = []  # (value, weight, symbol)
        for s in stocks:
            v = s.get(k)
            w = (s.get("mcap_b") or 0)
            if v is None or w <= 0:
                continue
            if positive_only and v <= 0:
                continue
            raw_pairs.append((v, w, s.get("symbol")))

        coverage[k] = len(raw_pairs)
        if not raw_pairs:
            out[k] = None
            out[f"{k}_median"] = None
            out[f"{k}_min"] = None
            out[f"{k}_max"] = None
            out[f"{k}_n"] = 0
            continue

        # Median (unweighted): robust central tendency the user can sanity-check
        vals_sorted = sorted(v for v, _, _ in raw_pairs)
        out[f"{k}_median"] = round(_percentile(vals_sorted, 50), 3)
        out[f"{k}_min"] = round(vals_sorted[0], 3)
        out[f"{k}_max"] = round(vals_sorted[-1], 3)
        out[f"{k}_n"] = len(raw_pairs)

        # Winsorize for sensitive metrics
        pairs_vw = [(v, w) for v, w, _ in raw_pairs]
        clipped_pairs, lo, hi = (pairs_vw, None, None)
        if k in _WINSOR_METRICS:
            p_lo, p_hi = _WINSOR_METRICS[k]
            clipped_pairs, lo, hi = _winsorize(pairs_vw, p_lo, p_hi)
            if lo is not None:
                # Log which stocks got clipped
                for (orig_v, w, sym), (clipped_v, _) in zip(raw_pairs, clipped_pairs):
                    if abs(orig_v - clipped_v) > 1e-6:
                        outliers.append({
                            "metric": k,
                            "symbol": sym,
                            "raw_value": round(orig_v, 3),
                            "clipped_to": round(clipped_v, 3),
                        })
        # Expose winsor thresholds so the audit modal can reconcile per-stock
        # contributions to the headline (Σ raw_contrib ≠ headline when winsorised;
        # post-clip contrib = w × clip(v, lo, hi) reproduces headline exactly).
        if lo is not None:
            out[f"{k}_winsor_lo"] = round(lo, 3)
            out[f"{k}_winsor_hi"] = round(hi, 3)

        # Mcap-weighted mean on the winsorized data
        num = sum(v * w for v, w in clipped_pairs)
        den = sum(w for _, w in clipped_pairs)
        if den > 0 and len(clipped_pairs) >= max(3, int(n_total * _MIN_COVERAGE)):
            out[k] = round(num / den, 3)
        else:
            # Insufficient coverage → expose None, frontend renders "—"
            out[k] = None

    return out, coverage, round(mcap_total, 1), outliers


# ─── Main ───────────────────────────────────────────────────────────────
def main():
    # Single-instance lock + yfinance DB reset (defenses from launchd pattern)
    _lock = acquire_singleton_lock()
    if _lock is None:
        sys.exit(0)
    reset_yfinance_cache()
    _init_yfinance_cache()
    prev_cache = load_previous_cache()

    # Load tracker data (single source of truth for prices/mcap + momentum to embed)
    tracker = load_tracker_data()
    tracker_quotes = tracker["quotes"]
    tracker_momentum = tracker["momentum"]

    t0 = time.time()
    # Collect every ticker across all sectors (de-dup)
    all_syms = set()
    for narr, info in NARRATIVES.items():
        for sym in info.get("tickers", []):
            all_syms.add(sym)
    all_syms = sorted(all_syms)
    print(f"[info] {len(all_syms)} unique tickers across {len(NARRATIVES)} sectors")

    # Bulk Yahoo "Growth Estimates Current Year" (matches /analysis page directly).
    # Done up-front so the per-ticker loop doesn't make 800 separate API calls.
    print("[info] bulk-fetching Yahoo Growth Estimates (Current Year)…")
    growth_cy_map = bulk_fetch_growth_cy(all_syms)
    print(f"[info] growth_cy: EPS {len(growth_cy_map.get('eps', {}))}/{len(all_syms)}, "
          f"Revenue {len(growth_cy_map.get('rev', {}))}/{len(all_syms)} tickers")

    # FX rates (same pattern as fetch_tradfi)
    currencies = {detect_currency(s) for s in all_syms}
    print(f"[fx] fetching FX for {sorted(currencies)}")
    fx_rates = fetch_fx_rates(currencies)

    # Fundamentals via ThreadPool (6 concurrent, slow but robust)
    fundamentals = {}
    failed = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(fetch_stock_fundamentals, s, fx_rates, tracker_quotes, growth_cy_map): s
                   for s in all_syms}
        for i, fut in enumerate(as_completed(futures), 1):
            sym = futures[fut]
            try:
                data = fut.result(timeout=45)
            except Exception as e:
                print(f"[warn] {sym}: future exception: {e}", file=sys.stderr)
                data = None
            if data is None:
                failed.append(sym)
            else:
                fundamentals[sym] = data
            if i % 20 == 0:
                elapsed = time.time() - t0
                print(f"[info] progress {i}/{len(all_syms)} ({elapsed:.0f}s elapsed, {len(failed)} failed)")

    print(f"[info] {len(fundamentals)} fundamentals fetched, {len(failed)} failed in {time.time()-t0:.0f}s")

    # ── Fallback: yahooquery bulk endpoints for failed tickers ──
    # yahooquery's summary_detail + key_stats + financial_data are bulk calls
    # (1 HTTP round-trip for 40 tickers). Recovers most yfinance .info fails.
    if failed:
        try:
            from yahooquery import Ticker as _YQT
            print(f"[info] yahooquery fallback for {len(failed)} failed tickers…")
            YQ_CHUNK = 40
            recovered = 0
            for i in range(0, len(failed), YQ_CHUNK):
                batch = failed[i:i + YQ_CHUNK]
                try:
                    yq = _YQT(batch, asynchronous=False, validate=False)
                    sd = yq.summary_detail  if isinstance(yq.summary_detail, dict)  else {}
                    ks = yq.key_stats       if isinstance(yq.key_stats, dict)       else {}
                    fd = yq.financial_data  if isinstance(yq.financial_data, dict)  else {}
                    pr = yq.price           if isinstance(yq.price, dict)           else {}
                    qt = yq.quotes          if isinstance(yq.quotes, dict)          else {}
                    # earnings_trend → Yahoo's "Growth Estimates" table (Current Year row)
                    try:
                        et = yq.earnings_trend if isinstance(yq.earnings_trend, dict) else {}
                    except Exception:
                        et = {}
                except Exception as e:
                    print(f"[warn] yahooquery chunk {i // YQ_CHUNK + 1} failed: {e}", file=sys.stderr)
                    continue
                for sym in batch:
                    try:
                        s = sd.get(sym); k = ks.get(sym); f = fd.get(sym); p = pr.get(sym)
                        q = qt.get(sym) if isinstance(qt.get(sym), dict) else {}
                        if not isinstance(s, dict) and not isinstance(k, dict) and not isinstance(f, dict):
                            continue  # nothing available
                        ccy = detect_currency(sym)

                        def _sf(v, maximum=None, positive=False):
                            try:
                                if v is None: return None
                                x = float(v)
                                if maximum is not None and abs(x) > maximum: return None
                                if positive and x <= 0: return None
                                return x
                            except Exception:
                                return None

                        mcap_local = 0
                        if isinstance(p, dict):
                            mcap_local = _sf(p.get("marketCap")) or 0
                        if not mcap_local and isinstance(s, dict):
                            mcap_local = _sf(s.get("marketCap")) or 0
                        mcap_usd = (apply_fx(mcap_local, ccy, fx_rates) or 0) if mcap_local else 0

                        pe_ttm = _sf((s or {}).get("trailingPE"), 200, positive=True)
                        # Yahoo's web "Forward P/E" on /key-statistics = summaryDetail
                        # or keyStats forwardPE (next-FY EPS basis). priceEpsCurrentYear
                        # (current-FY basis) kept only as fallback. See primary-path note.
                        pe_cur_v = _sf((q or {}).get("priceEpsCurrentYear"), 300, positive=True)
                        pe_nxt_v = (_sf((s or {}).get("forwardPE"), 300, positive=True)
                                    or _sf((k or {}).get("forwardPE"), 300, positive=True))
                        pe_fwd = _pick_forward_pe(pe_cur_v, pe_nxt_v, pe_ttm)
                        pb     = _sf((k or {}).get("priceToBook"), 100, positive=True)
                        ps     = _sf((s or {}).get("priceToSalesTrailing12Months"), 50, positive=True)
                        dy     = _sf((s or {}).get("dividendYield"))
                        if dy is not None and dy < 1 and dy > 0:
                            # yahooquery gives fraction, we want %
                            dy = dy * 100
                        # Forward EPS + Revenue growth — Yahoo "Growth Estimates →
                        # Current Year" (earnings_trend module, period '0y' growth
                        # for EPS, period '0y' revenueEstimate.growth for revenue).
                        # Matches what's displayed on Yahoo /analysis. Fallback for
                        # EPS only: TTM-based epsCurrentYear/epsTrailingTwelveMonths−1.
                        # No fallback for revenue (revenueGrowth = quarterly YoY,
                        # different metric → would mismatch the audit link).
                        eps_cur_v = _sf((q or {}).get("epsCurrentYear"))
                        eps_ttm_v = _sf((q or {}).get("epsTrailingTwelveMonths"))
                        yahoo_g_cy = None
                        yahoo_rg_cy = None
                        try:
                            et_sym = et.get(sym) if isinstance(et, dict) else None
                            trends = (et_sym or {}).get("trend") if isinstance(et_sym, dict) else None
                            if isinstance(trends, list):
                                for tr in trends:
                                    if isinstance(tr, dict) and tr.get("period") == "0y":
                                        g = _sf(tr.get("growth"))
                                        if g is not None:
                                            yahoo_g_cy = g * 100
                                        rest = tr.get("revenueEstimate") if isinstance(tr.get("revenueEstimate"), dict) else None
                                        rgv = _sf((rest or {}).get("growth"))
                                        if rgv is not None:
                                            yahoo_rg_cy = rgv * 100
                                        break
                        except Exception:
                            pass
                        eps_g = None
                        if (yahoo_g_cy is not None and abs(yahoo_g_cy) <= 500
                                and not _cur_fy_glitched(pe_cur_v, pe_nxt_v, pe_ttm)):
                            eps_g = yahoo_g_cy
                        elif (eps_cur_v is not None and eps_ttm_v is not None and eps_ttm_v > 0
                                and not _cur_fy_glitched(pe_cur_v, pe_nxt_v, pe_ttm)):
                            eps_g = (eps_cur_v / eps_ttm_v - 1) * 100
                        rev_g = yahoo_rg_cy if (yahoo_rg_cy is not None and abs(yahoo_rg_cy) <= 500) else None
                        roe   = _sf((f or {}).get("returnOnEquity"))
                        if roe is not None: roe *= 100
                        beta  = _sf((s or {}).get("beta"), 4) or _sf((k or {}).get("beta"), 4)
                        if beta is not None and beta < 0.05:
                            beta = None
                        fcf   = _sf((f or {}).get("freeCashflow"))
                        fcf_yield = None
                        if fcf is not None and mcap_local and mcap_local > 0:
                            fcf_yield = (fcf / mcap_local) * 100
                        perf_1y = _sf((k or {}).get("52WeekChange"))
                        if perf_1y is not None: perf_1y *= 100

                        # Skip if literally zero useful metrics
                        if all(v is None for v in [pe_ttm, pe_fwd, pb, ps, dy, roe, eps_g, rev_g, fcf_yield]):
                            continue

                        name = (p or {}).get("longName") or (p or {}).get("shortName") or sym

                        fundamentals[sym] = {
                            "symbol": sym,
                            "name": name,
                            "currency": ccy,
                            "mcap_b": round(mcap_usd / 1e9, 2) if mcap_usd else 0,
                            "pe_ttm": pe_ttm,
                            "pe_fwd": pe_fwd,
                            "pb": pb,
                            "ps": ps,
                            "div_yield": dy,
                            "eps_growth_fwd": eps_g,
                            "rev_growth_ttm": rev_g,
                            "roe": roe,
                            "fcf_yield": fcf_yield,
                            "beta": beta,
                            "perf_1y": perf_1y,
                        }
                        recovered += 1
                    except Exception as e:
                        print(f"[warn] yahooquery {sym}: {e}", file=sys.stderr)
                time.sleep(1)
            # Remove recovered from failed list
            failed = [s for s in failed if s not in fundamentals]
            print(f"[info] yahooquery recovered {recovered} tickers, {len(failed)} still failing")
        except ImportError:
            print("[info] yahooquery not installed — skipping fallback", file=sys.stderr)

    if failed:
        print(f"[info] final failed: {failed[:20]}{'...' if len(failed) > 20 else ''}")

    # ── Gap-fill from previous funda cache (last known good fundamentals) ──
    prev_funda = {}
    for s in (prev_cache.get("sectors", []) if prev_cache else []):
        for st in s.get("stocks", []):
            sym = st.get("symbol")
            if sym:
                prev_funda[sym] = st
    stale_filled = 0
    for sym in all_syms:
        if sym in fundamentals:
            continue
        prev = prev_funda.get(sym)
        if not prev:
            continue
        # Refresh price/mcap from current tracker_quotes if available
        tq = tracker_quotes.get(sym, {})
        merged = {**prev, "_stale_funda": True}
        if tq.get("price") is not None:
            merged["price_usd"] = tq["price"]
        if tq.get("mcap"):
            merged["mcap_b"] = round(tq["mcap"] / 1e9, 2)
            merged["_mcap_source"] = "tracker"
        if tq.get("perf_30d") is not None:
            merged["perf_30d"] = tq["perf_30d"]
        fundamentals[sym] = merged
        stale_filled += 1
    if stale_filled:
        print(f"[info] gap-filled {stale_filled} stocks from previous funda cache (marked _stale_funda=true)")

    # Aggregate per sector + INJECT momentum from tracker
    sectors_out = []
    total_outliers = []
    for narrative, meta in NARRATIVES.items():
        tickers = meta.get("tickers", [])
        stocks = [fundamentals[s] for s in tickers if s in fundamentals]
        summary, coverage, mcap_total_b, sector_outliers = aggregate_sector(stocks)
        # Tag outliers with the sector for global reporting
        for o in sector_outliers:
            o["sector"] = narrative
            total_outliers.append(o)
        # Anomaly guard: warn loudly when the total mcap is implausible
        # ($110T is roughly the global equity market — anything past that
        # is almost certainly an FX bug like the IDR fiasco).
        if mcap_total_b > 200_000:
            print(f"[ANOMALY] {narrative}: mcap_total_b={mcap_total_b:.0f} B (>200,000B implausible — likely FX bug for non-USD tickers)", file=sys.stderr)
        # ── Combien de societes derriere une metrique de ce secteur ? ──
        # Ce nombre valait `max(coverage.values())` : l'effectif de la metrique
        # la MIEUX servie, presente comme l'effectif du secteur. Mesure : « Big
        # Tech & Electronique » annoncait 13 alors que la croissance du chiffre
        # d'affaires n'y etait connue que pour TROIS titres.
        #
        # Un maximum repond a « combien au mieux », un effectif a « combien ».
        # Les confondre flatte systematiquement la couverture, jamais l'inverse.
        #
        # On publie donc la MEDIANE — ce que vaut une metrique typique — et on
        # garde le maximum et le minimum sous leurs propres noms. La mediane
        # plutot que la moyenne : une seule metrique tres bien servie ne doit pas
        # relever le chiffre a elle seule, c'est le defaut qu'on corrige.
        _cov = sorted(coverage.values()) if coverage else []
        if _cov:
            _m = len(_cov) // 2
            n_median_coverage = (_cov[_m] if len(_cov) % 2
                                 else (_cov[_m - 1] + _cov[_m]) // 2)
        else:
            n_median_coverage = 0
        n_max_coverage = _cov[-1] if _cov else 0
        n_min_coverage = _cov[0] if _cov else 0
        # Cross-merge: momentum metrics from the tracker for this sector
        mom = tracker_momentum.get(narrative, {})
        sectors_out.append({
            "narrative": narrative,
            "icon": meta.get("icon", ""),
            "color": meta.get("color", "#9ca3af"),
            "n_stocks": len(tickers),
            "n_with_data": n_median_coverage,
            "n_couverture_max": n_max_coverage,
            "n_couverture_min": n_min_coverage,
            "coverage": coverage,         # {metric: count_with_data} for frontend gating
            "mcap_total_b": mcap_total_b,
            **summary,
            # ── Momentum (from tradfi_cache.json — same source as Tracker tab) ──
            "momentum": {k: v for k, v in mom.items() if v is not None} if mom else {},
            "stocks": stocks,
        })

    # Sort by mcap desc for default display
    sectors_out.sort(key=lambda s: (s.get("mcap_total_b") or 0), reverse=True)

    payload = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M %Z") or
                   datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fetch_ts": int(time.time()),
        "sources": [
            "tradfi_cache.json (price + mcap + perf_30d + momentum) — single source of truth, written by fetch_tradfi.py",
            "yfinance .info per ticker (trailingPE, priceEpsCurrentYear=Yahoo web "
            "'Forward P/E', priceToBook, priceToSalesTrailing12Months, dividendYield, "
            "revenueGrowth, returnOnEquity, freeCashflow, beta, 52WeekChange) + "
            "growth_estimates['0y'] = Yahoo /analysis 'Growth Estimates Current Year' "
            "for forward EPS growth (fallback: epsCurrentYear/trailingEps−1)",
            "yahooquery summary_detail/key_stats/financial_data/quotes/earnings_trend fallback",
            "FX conversion via yfinance 5y daily close on {CCY}USD=X pairs (mcap fallback only)",
        ],
        "tracker_cache_updated": tracker.get("updated"),
        "n_sectors": len(sectors_out),
        "n_tickers_total": len(all_syms),
        "n_tickers_fetched": len(fundamentals),
        "n_tickers_failed": len(failed),
        "n_stale_filled": stale_filled,
        # Cross-sector outlier audit: every stock value clipped by p5/p95
        # winsorisation. Useful for frontend "audit" tooltips and for
        # human review (which tickers Yahoo is most flaky on).
        "outliers": sorted(
            total_outliers,
            key=lambda o: abs(o.get("raw_value", 0) - o.get("clipped_to", 0)),
            reverse=True,
        )[:50],
        "methodology": {
            "weight": "mcap-weighted mean (S&P 500-style)",
            "outlier_handling": "winsorisation 5th/95th percentile on growth, ratios, perf, fcf_yield, roe",
            "min_coverage_for_cell": _MIN_COVERAGE,
            "safe_caps": {"pe_ttm": 200, "pe_fwd": 300, "pb": 100, "ps": 50, "beta_max": 4, "beta_min_drop": 0.05},
            "median_alongside_mean": "every metric exposes <metric>_median, _min, _max, _n for audit",
        },
        "sectors": sectors_out,
    }

    # Write JSON
    with OUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[ok] wrote {OUT_JSON}")

    # Write JS (for CORS-safe loading in file:// context)
    with OUT_JS.open("w", encoding="utf-8") as f:
        f.write("window.__TRADFI_FUNDAMENTALS__=" +
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n")
    print(f"[ok] wrote {OUT_JS}")

    # Summary
    print("\n[summary] top 10 sectors by mcap:")
    for s in sectors_out[:10]:
        pe = s["pe_ttm"]
        pe_s = f"{pe:.1f}x" if pe else "—"
        print(f"  {s['narrative']:<32} mcap={s['mcap_total_b']:>7.0f}B P/E={pe_s:<7} n={s['n_with_data']}/{s['n_stocks']}")


if __name__ == "__main__":
    main()
