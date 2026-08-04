#!/usr/bin/env python3
"""Hourly-ish refresh of the 16 FRED macro indicators + Macro Score + 7 sub-indices.

Replicates the R logic from Macro_Trends.Rmd (lines 2182-2391) so that FRED data
is no longer frozen at knit-time. Writes a single JS cache file that injects:

  window.__MACRO_FRED_LIVE__ = {
    tickers:    {...},         # 16 FRED series (dates, values, last, meta)
    score_hist: {dates, values, sp500, sub: {taux, credit, ...}}
    fetch_timestamp, fetch_iso, sources
  }

The Rmd loads this file AFTER its own knit-time fallback, so page-load always
reads the freshest cache. Run by launchd scf.macrofred.refresh (6h).
"""
import json
import sys
import time
import csv
import io
from pathlib import Path
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "macro_fred_cache.json"
OUT_JS   = CACHE_DIR / "macro_fred_cache.js"
CACHE_MAX_HOURS = 5

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) SiteCryptoFinance/1.0"
START_DATE = "2000-01-01"

# ── 16 FRED tickers with metadata (exact mirror of Rmd radar/macro-score config) ──
#   fred_id, display_name, invert, seuil_pct, category, color, short_desc
TICKERS = [
    # id,           name,                    invert, seuil, cat,       color,     short desc
    ("T10Y2Y",      "Yield Curve 10Y-2Y",    False,  5,     "taux",    "#34d399", "Steepening = bull stocks (pente 10y vs 2y)"),
    ("VIXCLS",      "VIX",                   True,   20,    "risque",  "#ef4444", "Volatilite S&P 500 — fear gauge"),
    ("FEDFUNDS",    "Fed Funds Rate",        True,   2,     "taux",    "#f97316", "Taux directeur Fed (politique monetaire)"),
    ("UNRATE",      "Unemployment Rate",     True,   2,     "emploi",  "#f59e0b", "Taux de chomage US"),
    ("BAMLH0A0HYM2","High Yield Spread",     True,   15,    "credit",  "#dc2626", "Spread HY US — risque credit"),
    ("BAA10Y",      "Baa Credit Spread",     True,   10,    "credit",  "#b91c1c", "Spread Moody's Baa Corporate vs 10Y Treasury — proxy stress credit (30 ans d'historique, source Moody's, pas ICE)"),
    ("DGS10",       "10Y Treasury",          True,   3,     "taux",    "#60a5fa", "Taux 10 ans US"),
    ("DTWEXBGS",    "USD Broad Index",       True,   2,     "risque",  "#84cc16", "USD vs paniers principales devises"),
    ("WM2NS",       "M2 Money Supply",       False,  2,     "liquide", "#06b6d4", "Masse monetaire M2"),
    ("CPIAUCSL",    "CPI Inflation",         True,   0.5,   "risque",  "#f87171", "Indice prix consommation US"),
    ("WALCL",       "Fed Balance Sheet",     False,  1,     "liquide", "#22d3ee", "Bilan Federal Reserve"),
    ("UMCSENT",     "Consumer Sentiment",    False,  5,     "conso",   "#a78bfa", "Confiance des menages Michigan"),
    ("RSXFS",       "Retail Sales",          False,  1,     "conso",   "#c084fc", "Ventes au detail US"),
    ("INDPRO",      "Industrial Production", False,  1,     "indus",   "#8b5cf6", "Production industrielle"),
    ("MANEMP",      "Manufacturing Employ",  False,  1,     "emploi",  "#fbbf24", "Emploi manufacturier"),
    ("CFNAI",       "Chicago Fed Activity",  False,  50,    "indus",   "#3b82f6", "Indice activite Chicago Fed"),
    ("DCOILWTICO",  "WTI Crude Oil",         True,   5,     "risque",  "#eab308", "Petrole WTI (stress energetique)"),
]

# Auxiliary series (not in macro score composite). Fetched separately, used for
# specific signals (ex : yield curve probit calibre sur 10Y-3M).
AUXILIARY_FRED = [
    ("T10Y3M", "Yield Curve 10Y-3M (NY Fed probit)"),
]

# Sub-indices: category → list of tickers (same as Rmd)
SUB_INDICES = {
    "taux":    ["FEDFUNDS", "DGS10", "T10Y2Y"],
    "credit":  ["BAMLH0A0HYM2"],
    "emploi":  ["UNRATE", "MANEMP"],
    "conso":   ["UMCSENT", "RSXFS"],
    "indus":   ["INDPRO", "CFNAI"],
    "liquide": ["WM2NS", "WALCL"],
    "risque":  ["VIXCLS", "DCOILWTICO", "DTWEXBGS", "CPIAUCSL"],
}

# Lookup helper
TICKER_META = {t[0]: {"name": t[1], "invert": t[2], "seuil": t[3],
                      "cat": t[4], "color": t[5], "desc": t[6]} for t in TICKERS}


def http_get_text(url, timeout=15, max_retries=5):
    """GET avec retry exponentiel : indispensable pour survivre aux cold-boots
    launchd ou coupures DNS transitoires (Errno 8, nodename nor servname provided)."""
    import time
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/csv,*/*"})
    last_err = None
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            # Retry seulement sur erreurs réseau (DNS, timeout, refused)
            if any(k in msg for k in ("nodename", "servname", "timed out", "refused", "unreachable", "name or service")):
                wait = 5 * (2 ** attempt)  # 5, 10, 20, 40, 80s
                sys.stderr.write(f"[FRED] network retry {attempt+1}/{max_retries} after {wait}s: {e}\n")
                time.sleep(wait)
                continue
            raise
    raise last_err


def fetch_fred(fred_id, start=START_DATE):
    """Fetch a single FRED series via l'API officielle.

    Note : fred.stlouisfed.org/graph/fredgraph.csv (CSV public, no-auth) est
    devenu inaccessible depuis ~mai 2026. On utilise l'API authentifiée
    (clé gratuite) via _fred_helpers."""
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _fred_helpers import fetch_fred as _fred_api
    obs = _fred_api(fred_id, start=start)
    if obs is None:
        return None
    return obs["dates"], obs["values"]


def fetch_yahoo_daily(symbol, period1=946684800, label=None):
    """Fetch DAILY closes pour n'importe quel symbole Yahoo Finance.
    period1 = 2000-01-01 par defaut. Utilise pour BTC-USD, ETH-USD, GLD, GC=F, etc."""
    try:
        period2 = int(time.time())
        # URL-encode le symbole (BTC-USD, GC=F, etc.)
        sym_enc = symbol.replace("=", "%3D")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_enc}?period1={period1}&period2={period2}&interval=1d"
        txt = http_get_text(url, timeout=25)
        d = json.loads(txt)
        r = d.get("chart", {}).get("result", [{}])[0]
        ts = r.get("timestamp", [])
        close = r.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        if not ts:
            sys.stderr.write(f"[YAHOO] {symbol}: no timestamps\n")
            return [], []
        out_d, out_v = [], []
        for t, c in zip(ts, close):
            if c is None:
                continue
            out_d.append(datetime.fromtimestamp(t).strftime("%Y-%m-%d"))
            out_v.append(round(float(c), 4))
        return out_d, out_v
    except Exception as e:
        sys.stderr.write(f"[YAHOO] {symbol}: {e}\n")
        return [], []


def fetch_sp500_daily():
    """Fetch S&P 500 DAILY closes depuis 2000 pour overlay sur le FCI daily.
    Utilise period1/period2 au lieu de range=10y pour contourner la limite Yahoo."""
    try:
        # 2000-01-01 00:00:00 UTC = 946684800 ; now = aujourd'hui
        period1 = 946684800
        period2 = int(time.time())
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?period1={period1}&period2={period2}&interval=1d"
        txt = http_get_text(url, timeout=25)
        d = json.loads(txt)
        r = d.get("chart", {}).get("result", [{}])[0]
        ts = r.get("timestamp", [])
        close = r.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        if not ts:
            return [], []
        out_d, out_v = [], []
        for t, c in zip(ts, close):
            if c is None:
                continue
            out_d.append(datetime.fromtimestamp(t).strftime("%Y-%m-%d"))
            out_v.append(round(float(c), 2))
        return out_d, out_v
    except Exception as e:
        sys.stderr.write(f"[SP500-DAILY] {e}\n")
        return [], []


def fetch_sp500_history():
    """Fetch S&P 500 weekly/daily closes via Yahoo Finance chart endpoint (no auth).
    Utilise http_get_text (avec retry DNS) pour être robuste au boot."""
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?interval=1wk&range=20y"
        txt = http_get_text(url, timeout=20)
        d = json.loads(txt)
        r = d.get("chart", {}).get("result", [{}])[0]
        ts = r.get("timestamp", [])
        close = r.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        if not ts:
            return {}, {}
        out_d, out_v = [], []
        for t, c in zip(ts, close):
            if c is None:
                continue
            out_d.append(datetime.fromtimestamp(t).strftime("%Y-%m-%d"))
            out_v.append(round(float(c), 2))
        return out_d, out_v
    except Exception as e:
        sys.stderr.write(f"[SP500] {e}\n")
        return [], []


def compute_macro_score_series(series_by_ticker):
    """Macro Score avec seuils GLISSANTS (percentile P75 sur fenetre 10 ans).

    Pour chaque indicateur, calcule le pourcentage de variation sur 91 jours a chaque
    point de la grille mensuelle, puis utilise le P75 des |variations| sur les 120 derniers
    mois comme seuil de declenchement bull/bear (plutot qu'un seuil fixe arbitraire).

    Retourne :
      {
        dates:       [YYYY-MM-DD, ...],                           # grille mensuelle
        values:      [score 0-100, ...],                          # score composite
        sub:         {cat: [score_cat 0-100, ...], ...},          # 7 sous-indices
        thresholds:  {
          method:       "rolling-percentile",
          window_months: 120,
          percentile:    75,
          description:   "Seuil dynamique P75 des |Δ%| 91j sur 10 ans glissants",
          current:      {ticker: {p75, fixed_legacy, last_chg_pct, state}},  # pour audit
          history:      {ticker: [{date, p25, p50, p75, p90, chg_pct, state}, ...]},
        },
        contributions: [{ticker, weight, state, chg_pct, threshold}, ...]   # au dernier point
      }
    """
    # ── 1. Parse des series ──
    parsed = {}
    for tk, data in series_by_ticker.items():
        if not data:
            continue
        dates, values = data
        try:
            pairs = [(datetime.strptime(d, "%Y-%m-%d"), v) for d, v in zip(dates, values)]
            parsed[tk] = sorted(pairs)
        except Exception as e:
            sys.stderr.write(f"[MS] parse {tk}: {e}\n")
            continue

    if not parsed:
        return {"dates": [], "values": [], "sub": {}, "thresholds": {}, "contributions": []}

    # ── 2. Grille mensuelle 2005 → 1er du mois courant + point "aujourd'hui" ──
    # FIX 2026-05-16 : ajoute un point en date d'aujourd'hui en bout de grille pour
    # synchroniser la valeur affichée avec la recomputation live JS de Macro_Trends.html.
    # Avant ce fix, la dernière valeur du __MACRO_SCORE_HIST__ datait du 1er du mois
    # (cache mensuel) — divergence avec la valeur "Conditions Actuelles" calculée
    # côté client. Conséquence : Bulle_IA utilisait une valeur stale (~17 jours de retard).
    start = datetime(2005, 1, 1)
    month_start_today = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    grid = []
    d = start
    while d <= month_start_today:
        grid.append(d)
        if d.month == 12:
            d = d.replace(year=d.year + 1, month=1)
        else:
            d = d.replace(month=d.month + 1)
    # Ajout du point "aujourd'hui" si on n'est pas le 1er du mois
    real_today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if real_today > month_start_today:
        grid.append(real_today)

    def sample_at(pairs, target_date, lookback_days=91):
        cur = None
        prev_target = target_date - timedelta(days=lookback_days)
        prev = None
        for dt, v in pairs:
            if dt <= target_date:
                cur = v
            if dt <= prev_target:
                prev = v
            elif dt > target_date:
                break
        return cur, prev

    # ── 3. Pré-calcul : pour chaque ticker, la serie des |Δ%| 91j sur la grille ──
    # C'est indispensable pour calculer le P75 glissant sans look-ahead bias.
    chg_history = {tk: [] for tk in TICKER_META}   # [(date, chg_pct signed), ...]
    for tk, meta in TICKER_META.items():
        if tk not in parsed:
            continue
        for target in grid:
            cur, prev = sample_at(parsed[tk], target)
            if cur is None or prev is None:
                chg_history[tk].append((target, None))
                continue
            denom = max(abs(prev), 0.01)
            chg_pct = (cur - prev) / denom * 100
            chg_history[tk].append((target, chg_pct))

    # ── 4. Helper pour P25/P50/P75/P90 sans numpy ──
    def pct(sorted_vals, p):
        """Percentile P (0-100) d'une liste triée. Interpolation linéaire."""
        if not sorted_vals:
            return None
        k = (len(sorted_vals) - 1) * (p / 100.0)
        f = int(k)
        c = min(f + 1, len(sorted_vals) - 1)
        if f == c:
            return sorted_vals[f]
        return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)

    WINDOW_MONTHS = 120     # 10 ans glissants
    PERCENTILE    = 75      # un |Δ| en haut du quartile = signal
    MIN_OBS       = 24      # au moins 2 ans pour calculer un seuil

    # ── 5. Score, sous-scores, et stockage de l'audit trail ──
    score_dates = []
    score_values = []
    sub_series = {cat: [] for cat in SUB_INDICES}
    threshold_hist = {tk: [] for tk in TICKER_META}
    last_snapshot = []   # contributions au dernier point

    for i, target in enumerate(grid):
        bull = bear = neutre = total = 0
        sub_counts = {cat: {"bull": 0, "bear": 0, "neutre": 0, "total": 0} for cat in SUB_INDICES}
        date_str = target.strftime("%Y-%m-%d")
        is_last = (i == len(grid) - 1)
        for tk, meta in TICKER_META.items():
            if tk not in parsed:
                continue
            # Valeur de variation au point courant
            if i >= len(chg_history[tk]):
                continue
            _, chg_pct = chg_history[tk][i]
            if chg_pct is None:
                continue

            # Fenêtre glissante des |Δ%| sur les 120 derniers mois (exclusive du point actuel pour éviter look-ahead)
            lo = max(0, i - WINDOW_MONTHS)
            window = [abs(c) for (_, c) in chg_history[tk][lo:i] if c is not None]
            if len(window) >= MIN_OBS:
                w = sorted(window)
                p25 = pct(w, 25)
                p50 = pct(w, 50)
                p75 = pct(w, 75)
                p90 = pct(w, 90)
                seuil = p75
                seuil_source = "rolling"
            else:
                # Fallback seuil legacy pour les tout premiers points
                p25 = p50 = p75 = p90 = None
                seuil = meta["seuil"]
                seuil_source = "fixed"

            # Classification bull/bear/neutre avec seuil glissant
            if abs(chg_pct) > seuil:
                up = chg_pct > 0
                is_bull = (up and not meta["invert"]) or ((not up) and meta["invert"])
                if is_bull:
                    bull += 1
                    sub_counts[meta["cat"]]["bull"] += 1
                    state = "bull"
                else:
                    bear += 1
                    sub_counts[meta["cat"]]["bear"] += 1
                    state = "bear"
            else:
                neutre += 1
                sub_counts[meta["cat"]]["neutre"] += 1
                state = "neutre"
            total += 1
            sub_counts[meta["cat"]]["total"] += 1

            # Stockage historique léger : un point tous les 3 mois (sinon cache explose)
            if i % 3 == 0 and p75 is not None:
                threshold_hist[tk].append({
                    "date": date_str,
                    "p25": round(p25, 3), "p50": round(p50, 3),
                    "p75": round(p75, 3), "p90": round(p90, 3),
                    "chg_pct": round(chg_pct, 3),
                    "state": state,
                })

            # Snapshot au dernier point pour l'audit en live
            if is_last:
                last_snapshot.append({
                    "ticker":       tk,
                    "name":         meta["name"],
                    "cat":          meta["cat"],
                    "invert":       meta["invert"],
                    "chg_pct":      round(chg_pct, 3),
                    "threshold":    round(seuil, 3) if seuil is not None else None,
                    "threshold_src": seuil_source,
                    "fixed_legacy": meta["seuil"],
                    "p25": round(p25, 3) if p25 is not None else None,
                    "p50": round(p50, 3) if p50 is not None else None,
                    "p75": round(p75, 3) if p75 is not None else None,
                    "p90": round(p90, 3) if p90 is not None else None,
                    "state":        state,
                    "contribution": (+1 if state == "bull" else (-1 if state == "bear" else 0)),
                })

        if total == 0:
            continue
        score = max(0, min(100, round((bull - bear) / total * 50 + 50)))
        score_dates.append(date_str)
        score_values.append(score)
        for cat, counts in sub_counts.items():
            if counts["total"] == 0:
                sub_series[cat].append(None)
            else:
                s = max(0, min(100, round((counts["bull"] - counts["bear"]) / counts["total"] * 50 + 50)))
                sub_series[cat].append(s)

    # ── 6. Resume "current" : snapshot de chaque ticker au dernier point ──
    current = {row["ticker"]: {
        "chg_pct":       row["chg_pct"],
        "threshold":     row["threshold"],
        "threshold_src": row["threshold_src"],
        "fixed_legacy":  row["fixed_legacy"],
        "p25": row["p25"], "p50": row["p50"], "p75": row["p75"], "p90": row["p90"],
        "state":         row["state"],
    } for row in last_snapshot}

    thresholds = {
        "method":         "rolling-percentile",
        "window_months":  WINDOW_MONTHS,
        "percentile":     PERCENTILE,
        "min_observations": MIN_OBS,
        "description":    "Seuil dynamique P75 des |Δ%| 91j sur 10 ans glissants (exclut le point courant)",
        "formula":        "bull/bear si |Δ%_91j| > P75(|Δ%|_120mois) — sinon neutre",
        "current":        current,
        "history":        threshold_hist,
    }

    return {
        "dates":         score_dates,
        "values":        score_values,
        "sub":           sub_series,
        "thresholds":    thresholds,
        "contributions": last_snapshot,
    }


# ════════════════════════════════════════════════════════════════════════════════
#  CADRE INVESTISSEUR : Growth / Inflation / Financial Conditions + Regime
#  (framework Bridgewater All Weather + Sahm Rule + yield curve probit)
# ════════════════════════════════════════════════════════════════════════════════

# Mapping (ticker, sign) — sign = +1 si "valeur haute = force dans cette dimension"
# Growth  : +1 = indicateur favorable a la croissance quand il monte
# Inflation : +1 = indicateur favorable a la pression inflationniste quand il monte
# FinCond : +1 = indicateur favorable aux conditions financieres (risk-on) quand il monte
INVESTOR_DIMS = {
    "growth": [
        ("INDPRO",   +1),   # production industrielle
        ("RSXFS",    +1),   # retail sales
        ("MANEMP",   +1),   # emploi manu
        ("CFNAI",    +1),   # Chicago Fed activity (deja compose de 85 series)
        ("UNRATE",   -1),   # chomage haut = mauvais pour croissance
        ("UMCSENT",  +1),   # confiance menages
    ],
    "inflation": [
        ("CPIAUCSL",    +1),   # inflation headline
        ("DCOILWTICO",  +1),   # petrole = cost push
        ("DTWEXBGS",    -1),   # USD fort = disinflation import
        ("WM2NS",       +1),   # M2 = monnaie en circulation
        ("WALCL",       +0.5), # bilan Fed (QE = inflationniste, poids reduit)
    ],
    "fin_conditions": [
        ("BAMLH0A0HYM2", -1),  # HY spread = stress credit
        ("VIXCLS",       -1),  # volatilite actions
        ("T10Y2Y",       +1),  # pente positive = sain
        ("FEDFUNDS",     -1),  # politique restrictive = mauvais risk
        ("DGS10",        -1),  # taux longs hauts = conditions tendues
    ],
}


def yoy_change(pairs, target_date):
    """Variation YoY (%) : (V(t) - V(t-365j)) / |V(t-365j)|."""
    cur = None
    prev = None
    prev_target = target_date - timedelta(days=365)
    for dt, v in pairs:
        if dt <= target_date:
            cur = v
        if dt <= prev_target:
            prev = v
        elif dt > target_date:
            break
    if cur is None or prev is None:
        return None
    denom = max(abs(prev), 0.01)
    return (cur - prev) / denom * 100


def compute_investor_dimensions(series_by_ticker, grid, parsed):
    """Calcule Growth / Inflation / FinCond scores a chaque point mensuel.

    Methodologie :
      1. Pour chaque indicateur : YoY % change a chaque point de la grille
      2. Z-score glissant sur fenetre 120 mois (exclut le point courant = no look-ahead)
      3. Applique le sign (+/-) specifique a chaque dimension
      4. Moyenne des z-scores de la dimension = score brut de la dimension
      5. Normalisation : z -> [0, 100] via CDF gaussienne, recentree sur 50

    Retourne pour chaque dimension : {dates, values, momentum, raw_z}
    """
    import math

    def norm_cdf(z):
        # CDF gaussienne, approximation Abramowitz & Stegun
        if z is None:
            return None
        t = 1 / (1 + 0.2316419 * abs(z))
        d = 0.3989423 * math.exp(-z * z / 2)
        p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
        return 1 - p if z > 0 else p

    # 1. Pour chaque ticker utilise dans les dimensions : YoY series sur la grille
    yoy_history = {}
    all_tickers = set()
    for dim_tickers in INVESTOR_DIMS.values():
        for tk, _ in dim_tickers:
            all_tickers.add(tk)

    for tk in all_tickers:
        if tk not in parsed:
            continue
        yoy_history[tk] = []
        for target in grid:
            yoy = yoy_change(parsed[tk], target)
            yoy_history[tk].append((target, yoy))

    # 2. Pour chaque dimension et chaque point : z-scores + agregation
    dims_out = {name: {"dates": [], "values": [], "z_raw": [],
                       "components": []}  # dernier point : detail par ticker
                for name in INVESTOR_DIMS}

    WINDOW = 120
    MIN_OBS = 24

    for i, target in enumerate(grid):
        date_str = target.strftime("%Y-%m-%d")
        is_last = (i == len(grid) - 1)
        for dim_name, dim_tickers in INVESTOR_DIMS.items():
            z_signed = []
            comps = []
            for tk, sign in dim_tickers:
                if tk not in yoy_history:
                    continue
                if i >= len(yoy_history[tk]):
                    continue
                _, yoy_cur = yoy_history[tk][i]
                if yoy_cur is None:
                    continue
                # Fenetre glissante pour z-score (exclut le point courant)
                lo = max(0, i - WINDOW)
                window = [y for (_, y) in yoy_history[tk][lo:i] if y is not None]
                if len(window) < MIN_OBS:
                    continue
                mu = sum(window) / len(window)
                var = sum((x - mu) ** 2 for x in window) / len(window)
                sd = var ** 0.5
                if sd < 1e-9:
                    continue
                z = (yoy_cur - mu) / sd
                # Cap at +-3 pour robustesse (evite outliers COVID). Choix justifie :
                # 3 sigma = P(|z|>3) < 0.3% sous normalite --> cap seulement les
                # chocs statistiquement extremes (mars 2020, oct 2008).
                z_capped = max(-3, min(3, z))
                was_capped = (abs(z) > 3)
                z_signed_val = sign * z_capped
                z_signed.append(z_signed_val)
                if is_last:
                    comps.append({
                        "ticker":   tk,
                        "yoy":      round(yoy_cur, 3),
                        "mu":       round(mu, 3),
                        "sigma":    round(sd, 3),
                        "z":        round(z_capped, 3),
                        "z_raw":    round(z, 3),
                        "capped":   was_capped,
                        "sign":     sign,
                        "z_signed": round(z_signed_val, 3),
                        "window_months": len(window),
                    })

            if z_signed:
                z_avg = sum(z_signed) / len(z_signed)
                # Normalisation : z -> 0-100 via CDF gaussienne centree sur 50
                score = round(norm_cdf(z_avg) * 100)
                dims_out[dim_name]["dates"].append(date_str)
                dims_out[dim_name]["values"].append(score)
                dims_out[dim_name]["z_raw"].append(round(z_avg, 3))
                if is_last:
                    dims_out[dim_name]["components"] = comps

    # 3. Momentum (acceleration/deceleration) : score - moyenne 6 derniers mois
    for dim_name in dims_out:
        vals = dims_out[dim_name]["values"]
        momo = []
        for i, v in enumerate(vals):
            lo = max(0, i - 6)
            w = vals[lo:i]
            if len(w) < 3:
                momo.append(None)
            else:
                momo.append(round(v - sum(w) / len(w), 2))
        dims_out[dim_name]["momentum"] = momo

    return dims_out


def classify_regimes(growth_dim, inflation_dim, buffer=0.25):
    """Classifie chaque mois dans l un des 4 regimes Bridgewater All Weather.

    Logique : basee sur le momentum (acceleration) de growth et inflation.
      Si |g_momo| < buffer ET |i_momo| < buffer : regime "Transition" (zone tampon)
      Sinon : (g_momo >= 0) × (i_momo >= 0) --> 4 quadrants

      Reflation   : G+ / I-  (growth up, inflation down)  --> best pour actions
      Expansion   : G+ / I+  (growth up, inflation up)    --> stocks + commodities
      Stagflation : G- / I+  (growth down, inflation up)  --> gold + TIPS, worst stocks
      Deflation   : G- / I-  (growth down, inflation down)--> long bonds
      Transition  : |G| < buffer ET |I| < buffer           --> zone de basculement

    Le buffer evite les flip-flops sur la frontiere exacte (momo = 0.01 vs 0.02).
    Si on est dans le buffer, on reprend le regime precedent (sticky) pour
    refleter la persistence naturelle des regimes macro.
    """
    g_vals = growth_dim.get("momentum", [])
    i_vals = inflation_dim.get("momentum", [])
    g_dates = growth_dim.get("dates", [])
    i_dates = inflation_dim.get("dates", [])

    i_map = dict(zip(i_dates, i_vals))
    regimes = []
    last_clean_regime = None   # dernier regime hors buffer
    for k, d in enumerate(g_dates):
        gm = g_vals[k] if k < len(g_vals) else None
        im = i_map.get(d)
        if gm is None or im is None:
            regimes.append({"date": d, "regime": "unknown", "g_momo": gm, "i_momo": im, "in_buffer": False})
            continue
        in_buffer = (abs(gm) < buffer and abs(im) < buffer)
        if in_buffer and last_clean_regime is not None:
            # Sticky : on reste dans le dernier regime hors-buffer
            r = last_clean_regime
        elif gm >= 0 and im < 0:
            r = "Reflation"
        elif gm >= 0 and im >= 0:
            r = "Expansion"
        elif gm < 0 and im >= 0:
            r = "Stagflation"
        else:
            r = "Deflation"
        if not in_buffer:
            last_clean_regime = r
        regimes.append({"date": d, "regime": r, "g_momo": gm, "i_momo": im, "in_buffer": in_buffer})
    return regimes


def compute_sahm_rule(unrate_pairs, grid):
    """Sahm Rule (Claudia Sahm 2019, Brookings / Fed governor 2024) :

      sahm_t = MA3(UNRATE)_t - min( MA3(UNRATE)_{t-12..t-1} )
      Si sahm_t >= 0.5 % --> recession probable en cours

    Retourne pour chaque date de la grille : (date, sahm_value, recession_flag).
    """
    if not unrate_pairs:
        return []

    # UNRATE est mensuel --> on prend la valeur la plus recente pour chaque point
    out = []
    ma3_hist = []   # liste chronologique (date, ma3)

    for i, target in enumerate(grid):
        # UNRATE observee <= target
        cur = None
        for dt, v in unrate_pairs:
            if dt <= target:
                cur = v
            else:
                break
        if cur is None:
            out.append({"date": target.strftime("%Y-%m-%d"), "sahm": None, "recession": False, "unrate_ma3": None, "unrate_12m_min": None})
            continue

        # MA3 glissante : derniere et 2 valeurs mensuelles precedentes
        # (approximation : on prend les 3 dernieres valeurs UNRATE <= target)
        recent = []
        idx = 0
        for dt, v in unrate_pairs:
            if dt > target:
                break
            recent.append(v)
            idx += 1
        if len(recent) < 3:
            out.append({"date": target.strftime("%Y-%m-%d"), "sahm": None, "recession": False, "unrate_ma3": None, "unrate_12m_min": None})
            continue
        ma3 = sum(recent[-3:]) / 3.0
        ma3_hist.append(ma3)

        # min sur les 12 dernieres MA3 (exclut le point courant)
        if len(ma3_hist) < 4:
            out.append({"date": target.strftime("%Y-%m-%d"), "sahm": None, "recession": False, "unrate_ma3": round(ma3, 3), "unrate_12m_min": None})
            continue
        lo = max(0, len(ma3_hist) - 13)
        window = ma3_hist[lo:-1]  # exclut le dernier (= ma3 courant)
        if not window:
            out.append({"date": target.strftime("%Y-%m-%d"), "sahm": None, "recession": False, "unrate_ma3": round(ma3, 3), "unrate_12m_min": None})
            continue
        min_12m = min(window)
        sahm = ma3 - min_12m
        out.append({
            "date": target.strftime("%Y-%m-%d"),
            "sahm": round(sahm, 3),
            "unrate_ma3": round(ma3, 3),
            "unrate_12m_min": round(min_12m, 3),
            "recession": sahm >= 0.5,
        })
    return out


def compute_yield_curve_proba(t10y3m_pairs, grid, t10y2y_pairs=None):
    """Probabilite de recession a 12 mois — probit Estrella-Mishkin (NY Fed).

    Formule canonique du papier original (Estrella & Mishkin 1998, NBER WP5279) :
      spread_3m_avg = moyenne mobile 3 mois du spread 10Y-3M (T10Y3M)
      z             = 0.5333 - 0.6330 * spread_3m_avg
      P(recession in 12M) = phi(-z)        (i.e. phi(-0.5333 + 0.6330 * spread))
    Source : table 4 du papier, modele M5 (monthly, 1972-1995, re-estime par NY Fed).

    Calibration de controle (empiriquement observee chez NY Fed) :
      spread = +3.0 --> P ~= 1%   (courbe tres pentue, expansion)
      spread = +1.0 --> P ~= 11%
      spread =  0.0 --> P ~= 30%
      spread = -0.5 --> P ~= 48%
      spread = -1.0 --> P ~= 66%
      spread = -2.0 --> P ~= 89%

    Retourne (date, spread, spread_3m_avg, recession_proba_pct).
    Fallback : si T10Y3M absent, calcule un probit approximatif sur T10Y2Y (moins
    precis mais mieux que rien). On indique la source utilisee dans l output.
    """
    import math

    def norm_cdf(z):
        t = 1 / (1 + 0.2316419 * abs(z))
        d = 0.3989423 * math.exp(-z * z / 2)
        p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
        return 1 - p if z > 0 else p

    use_3m = t10y3m_pairs is not None and len(t10y3m_pairs) > 0
    pairs = t10y3m_pairs if use_3m else (t10y2y_pairs or [])

    # Coefficients Estrella-Mishkin 1998 (pour 10Y-3M)
    # Coefficients secondaires pour 10Y-2Y (ajustement empirique a partir des NBER dates)
    # Sur 1976-2023, une regression logistique avec 10Y-2Y donne des coefficients similaires
    # mais avec un intercept legerement different pour matcher les 11 recessions observees.
    if use_3m:
        alpha, beta = 0.5333, -0.6330     # Estrella-Mishkin canonique
        source = "Estrella-Mishkin (1998), 10Y-3M avec moyenne 3m"
    else:
        alpha, beta = 0.25, -0.80          # fallback approximatif pour 10Y-2Y
        source = "fallback approximatif 10Y-2Y (T10Y3M indisponible)"

    # 1. Build a chronological list of (date, value) from pairs for MA computation
    pairs_sorted = sorted(pairs)

    out = []
    for target in grid:
        # Valeur courante = derniere observation <= target
        cur = None
        values_last_3m = []
        three_m_ago = target - timedelta(days=92)
        for dt, v in pairs_sorted:
            if dt > target:
                break
            cur = v
            if dt >= three_m_ago:
                values_last_3m.append(v)
        if cur is None:
            out.append({"date": target.strftime("%Y-%m-%d"), "spread": None,
                        "spread_3m_avg": None, "recession_proba": None, "source": source})
            continue
        # Moyenne mobile 3 mois (fallback sur valeur courante si <5 obs)
        if len(values_last_3m) >= 5:
            spread_avg = sum(values_last_3m) / len(values_last_3m)
        else:
            spread_avg = cur
        z = alpha + beta * spread_avg
        # Probit : P = Phi(-z) dans la formulation Estrella-Mishkin
        # (equivalent a 1 - Phi(z), i.e. si spread eleve => z positif => P faible)
        p = 1 - norm_cdf(z)
        out.append({
            "date":            target.strftime("%Y-%m-%d"),
            "spread":          round(cur, 3),
            "spread_3m_avg":   round(spread_avg, 3),
            "recession_proba": round(p * 100, 1),
            "source":          source,
        })
    return out


# ════════════════════════════════════════════════════════════════════════════════
#  DAILY FINANCIAL CONDITIONS INDEX (FCI) — sous-indice quotidien
#  Utilise uniquement les 7 series FRED disponibles en frequence daily,
#  pour un signal "temps reel" de stress financier (SVB mars 2023, COVID, etc.).
#  PAS d equivalent Growth / Inflation en daily (ces series sont mensuelles).
# ════════════════════════════════════════════════════════════════════════════════

DAILY_FCI_COMPONENTS = [
    # (ticker, sign) — sign = direction favorable aux conditions de marche
    # sign -1 : hausse de la valeur = conditions financieres qui se tendent (stress)
    # sign +1 : hausse = conditions qui se detendent
    ("VIXCLS",       -1),   # volatilite actions
    ("BAMLH0A0HYM2", -1),   # HY spread (stress credit)
    ("T10Y2Y",       +1),   # pente 10Y-2Y (plus elle est positive plus c est sain)
    ("T10Y3M",       +1),   # pente 10Y-3M (idem)
    ("DGS10",        -1),   # taux 10 ans (taux hauts = conditions plus tendues)
    ("DTWEXBGS",     -1),   # USD fort = pression sur corporate EM & dette dollar
    ("DCOILWTICO",   -1),   # petrole haut = choc cost-push
]

def compute_daily_financial_conditions(series_by_ticker, aux_series, lookback_days=750):
    """Daily Financial Conditions Index : composite z-scores des 7 series daily.

    Methodologie :
      1. Aligne toutes les series sur les jours ouvres (business days) FRED
      2. Pour chaque jour et chaque indicateur, calcule la variation 21 jours (~ 1 mois boursier)
      3. Z-score glissant sur 750 jours ouvres (~ 3 ans) -- exclut le jour courant (no look-ahead)
      4. Applique le sign, moyenne les z-scores signes
      5. Normalise via CDF gaussienne --> score 0-100
         > 50 = conditions au-dessus de la moyenne (detendues / risk-on)
         < 50 = conditions sous la moyenne (tendues / risk-off)
         < 25 = stress eleve (P75 historique des conditions tendues)
         > 75 = conditions tres detendues (euphorie potentielle)

    Stress label :
      >= 70 : Detendu   (risk-on)
      50-70 : Neutre    (normal)
      30-50 : Vigilant  (tensions)
      15-30 : Stresse   (conditions difficiles)
      < 15  : Crise     (choc financier en cours)

    Retourne un dict avec :
      dates, values, stress_labels, components (snapshot last day), methodology
    """
    import math

    def norm_cdf(z):
        t = 1 / (1 + 0.2316419 * abs(z))
        d = 0.3989423 * math.exp(-z * z / 2)
        p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
        return 1 - p if z > 0 else p

    # ── 1. Combine series_by_ticker (16 FRED) + aux_series (T10Y3M) ──
    combined = {}
    for tk, data in series_by_ticker.items():
        if data:
            combined[tk] = data
    for tk, data in (aux_series or {}).items():
        if data:
            combined[tk] = data

    # Parse dates
    parsed = {}
    for tk, (dates, values) in combined.items():
        try:
            pairs = [(datetime.strptime(d, "%Y-%m-%d"), v) for d, v in zip(dates, values)]
            parsed[tk] = sorted(pairs)
        except Exception:
            continue

    # ── 2. Grille quotidienne sur l'intersection des dates disponibles ──
    # On prend l'union de toutes les dates, puis on garde les jours ouvres
    all_dates = set()
    for tk, _ in DAILY_FCI_COMPONENTS:
        if tk not in parsed:
            continue
        for dt, _ in parsed[tk]:
            all_dates.add(dt)
    if not all_dates:
        return {}
    sorted_dates = sorted(all_dates)
    # On garde seulement les jours ouvres (Lun-Ven)
    grid = [d for d in sorted_dates if d.weekday() < 5]
    if len(grid) < lookback_days + 30:
        return {}

    # ── 3. Pour chaque (ticker, date grille), valeur last-observed + variation 21j ──
    CHG_LOOKBACK = 21  # ~ 1 mois boursier

    def sample_at(pairs, target):
        cur = None
        for dt, v in pairs:
            if dt <= target:
                cur = v
            else:
                break
        return cur

    # Pré-compute : variation 21j pour chaque ticker sur chaque date de la grille
    chg_history = {tk: [] for tk, _ in DAILY_FCI_COMPONENTS}
    for i, target in enumerate(grid):
        # Date d'il y a 21 jours ouvres
        ref_idx = max(0, i - CHG_LOOKBACK)
        target_ref = grid[ref_idx]
        for tk, _ in DAILY_FCI_COMPONENTS:
            if tk not in parsed:
                chg_history[tk].append(None)
                continue
            cur = sample_at(parsed[tk], target)
            prev = sample_at(parsed[tk], target_ref)
            if cur is None or prev is None:
                chg_history[tk].append(None)
                continue
            denom = max(abs(prev), 0.01)
            chg = (cur - prev) / denom * 100
            chg_history[tk].append(chg)

    # ── 4. Z-score glissant sur lookback_days pour chaque point ──
    # Puis agregation signee + CDF gaussienne
    out_dates, out_values, out_labels = [], [], []
    components_snapshot = {}

    def stress_label(v):
        if v >= 70: return "Détendu"
        if v >= 50: return "Neutre"
        if v >= 30: return "Vigilant"
        if v >= 15: return "Stressé"
        return "Crise"

    for i, target in enumerate(grid):
        z_signed = []
        comps_today = []
        for tk, sign in DAILY_FCI_COMPONENTS:
            chg = chg_history[tk][i]
            if chg is None:
                continue
            # Fenetre glissante (exclut le point courant)
            lo = max(0, i - lookback_days)
            window = [c for c in chg_history[tk][lo:i] if c is not None]
            if len(window) < 60:  # au moins 3 mois d'historique
                continue
            mu = sum(window) / len(window)
            var = sum((x - mu) ** 2 for x in window) / len(window)
            sd = var ** 0.5
            if sd < 1e-9:
                continue
            z = (chg - mu) / sd
            z_cap = max(-3, min(3, z))
            z_signed_val = sign * z_cap
            z_signed.append(z_signed_val)
            # Snapshot au dernier jour
            if i == len(grid) - 1:
                comps_today.append({
                    "ticker":   tk,
                    "chg_21d":  round(chg, 3),
                    "mu":       round(mu, 3),
                    "sigma":    round(sd, 3),
                    "z":        round(z_cap, 3),
                    "sign":     sign,
                    "z_signed": round(z_signed_val, 3),
                    "contribution_to_score": round((sign * z_cap) / len(DAILY_FCI_COMPONENTS), 3),
                })

        if not z_signed:
            continue
        z_avg = sum(z_signed) / len(z_signed)
        score = round(norm_cdf(z_avg) * 100)
        out_dates.append(target.strftime("%Y-%m-%d"))
        out_values.append(score)
        out_labels.append(stress_label(score))
        if i == len(grid) - 1:
            components_snapshot = comps_today

    return {
        "dates":           out_dates,
        "values":          out_values,
        "stress_labels":   out_labels,
        "components":      components_snapshot,
        "last_value":      out_values[-1] if out_values else None,
        "last_label":      out_labels[-1] if out_labels else None,
        "last_date":       out_dates[-1] if out_dates else None,
        "methodology": {
            "frequency":      "Jours ouvres (Lun-Ven)",
            "chg_lookback":   f"{CHG_LOOKBACK} jours ouvres (~1 mois)",
            "rolling_window": f"{lookback_days} jours ouvres (~3 ans)",
            "aggregation":    "Moyenne des z-scores signes -> CDF gaussienne -> 0-100",
            "thresholds":     "Detendu >= 70 | Neutre 50-70 | Vigilant 30-50 | Stresse 15-30 | Crise < 15",
            "components":     [{"ticker": tk, "sign": sg} for tk, sg in DAILY_FCI_COMPONENTS],
            "no_lookahead":   "Fenetre glissante exclut le jour courant",
        },
    }


def backtest_sp500_by_regime(regimes, sp500_on_grid):
    """Statistiques de performance S&P 500 par regime (sur les donnees disponibles).

    Pour chaque regime : avg monthly return, hit rate (mois positifs), max drawdown,
    nombre de mois. Aide l investisseur a savoir comment le SP500 se comporte dans
    chaque regime historiquement.
    """
    # Monthly returns
    rets = []
    for i in range(1, len(sp500_on_grid)):
        p0 = sp500_on_grid[i - 1]
        p1 = sp500_on_grid[i]
        if p0 is None or p1 is None or p0 == 0:
            rets.append(None)
        else:
            rets.append((p1 - p0) / p0 * 100)
    rets = [None] + rets   # align length

    buckets = {r: {"rets": [], "count": 0} for r in ["Reflation", "Expansion", "Stagflation", "Deflation", "unknown"]}
    for k, reg in enumerate(regimes):
        name = reg["regime"]
        if name not in buckets:
            continue
        if k < len(rets) and rets[k] is not None:
            buckets[name]["rets"].append(rets[k])
            buckets[name]["count"] += 1

    out = {}
    for name, b in buckets.items():
        r = b["rets"]
        if not r:
            out[name] = {"count": 0, "avg_monthly": None, "hit_rate": None, "total": None, "annualized": None}
            continue
        avg = sum(r) / len(r)
        hit = sum(1 for x in r if x > 0) / len(r) * 100
        # Compounded return si on avait ete investi a chaque mois du regime
        prod = 1.0
        for x in r:
            prod *= (1 + x / 100)
        total = (prod - 1) * 100
        # Rendement annualise (approximation)
        if len(r) >= 12:
            annual = ((prod ** (12 / len(r))) - 1) * 100
        else:
            annual = None
        out[name] = {
            "count":       len(r),
            "avg_monthly": round(avg, 2),
            "hit_rate":    round(hit, 1),
            "total":       round(total, 1),
            "annualized":  round(annual, 2) if annual is not None else None,
        }
    return out


def align_sp500_on_grid(sp500_dates, sp500_values, score_dates):
    """For each date in score_dates, find the closest SP500 close (last-observed)."""
    if not sp500_dates:
        return [None] * len(score_dates)
    pairs = sorted(zip(sp500_dates, sp500_values))
    out = []
    idx = 0
    last_val = None
    for sd in score_dates:
        while idx < len(pairs) and pairs[idx][0] <= sd:
            last_val = pairs[idx][1]
            idx += 1
        out.append(last_val)
    return out


def is_fresh():
    if not OUT_JSON.exists():
        return False
    age_h = (time.time() - OUT_JSON.stat().st_mtime) / 3600
    return age_h < CACHE_MAX_HOURS


# ──────────────────────────────────────────────────────────────────────────
# Compact indicators cache (variation board pour l'Accueil).
# Dérivé du même `payload` que macro_fred_cache.js → fraîcheur garantie, ~10 KB.
# window.__MACRO_INDICATORS__ = {fetch, cats:{cat:{label, items:[...]}}}
# Chaque item : value, mh (résampling mensuel 36), dh/dt/dh0/freq (historique à
# la fréquence native, cf. INDIC_DH_*), tail (rallonge Yahoo, cf. INDIC_TAIL_*),
# d1/d3/d12 + p1/p3/p12, lo12/hi12 (range 12m), pctile (niveau OU croissance YoY
# pour les niveaux tendanciels), pmode. Couleur/sens gérés côté accueil.js.
# ──────────────────────────────────────────────────────────────────────────
OUT_INDIC_JSON = CACHE_DIR / "macro_indicators_cache.json"
OUT_INDIC_JS   = CACHE_DIR / "macro_indicators_cache.js"
INDIC_ORDER  = ["taux", "credit", "emploi", "conso", "indus", "liquide", "risque"]
INDIC_CATLBL = {"taux": "Taux", "credit": "Crédit", "emploi": "Emploi",
                "conso": "Conso", "indus": "Industrie", "liquide": "Liquidité",
                "risque": "Risque"}
# Niveaux tendanciels : percentile calculé sur la CROISSANCE YoY (pas le niveau,
# qui ne fait que monter → percentile non informatif).
INDIC_TREND = {"CPIAUCSL", "WM2NS", "WALCL", "RSXFS", "INDPRO", "MANEMP"}

# ── Résolution NATIVE des mini-graphes de l'Accueil (2026-07-28) ──────────────
# `mh` (36 points, dernière valeur de chaque mois) jetait la finesse des séries
# FRED quotidiennes : VIX, WTI, 10Y, courbe 10Y-2Y et HY spread sont publiées
# TOUS LES JOURS depuis 2000, WALCL/WM2NS toutes les semaines. On ajoute `dh` =
# historique à la fréquence native sur la MÊME fenêtre 3 ans que `mh` (≈780 pts
# quotidiens, ≈156 hebdo). `mh` reste émis pour tout le monde : c'est la donnée
# des séries mensuelles ET le repli si `dh` manque (cache d'une version
# antérieure, série discontinuée). Dates encodées en offsets de jours depuis
# `dh0` plutôt qu'en ISO → ~2× plus compact, reconstruction triviale côté JS.
INDIC_DH_DAYS    = 1095   # 3 ans, aligné sur les 36 mois de mh (cartes comparables)
INDIC_DH_MIN_PTS = 60     # < 60 pts sur 3 ans = série mensuelle → mh suffit
INDIC_DH_MAX_GAP = 20     # écart médian (jours) au-delà duquel on considère mensuel
# Séries masquées du board Accueil (cf. EXCLUDE dans accueil.js) : pas de `dh`,
# inutile de gonfler le cache de 17 Ko pour des cartes qui ne sont pas affichées.
# Si elles y reviennent un jour : retirer d'ici ET de EXCLUDE, rien d'autre.
INDIC_DH_SKIP    = {"BAA10Y", "DTWEXBGS"}

# ── Rallonge temps réel Yahoo (2026-07-28) ────────────────────────────────────
# FRED publie ces 3 séries avec 2 à 8 jours de retard (constaté le 28/07 : VIX et
# 10Y arrêtés au 24/07, WTI au 20/07 alors que le baril avait fait 84 → 92 → 80
# entre-temps). On colle les jours postérieurs au dernier point FRED depuis
# Yahoo, REBASÉS sur le ratio médian FRED/Yahoo des 60 derniers jours communs.
# Sans rebase : ^VIX est identique à VIXCLS (écart médian mesuré 0.00 %) et ^TNX
# colle à DGS10 (−0.10 %), mais CL=F (front-month) cote ~3 % sous le spot Cushing
# de DCOILWTICO → marche d'escalier visible sur la courbe. Le rebase préserve le
# niveau de la série FRED et n'emprunte à Yahoo que la VARIATION récente.
INDIC_TAIL_YAHOO       = {"VIXCLS": "^VIX", "DGS10": "^TNX", "DCOILWTICO": "CL=F"}
INDIC_TAIL_MIN_OVERLAP = 20          # < 20 jours communs = base non fiable → pas de rallonge
INDIC_TAIL_RATIO_BAND  = (0.5, 2.0)  # garde-fou échelle (mauvais symbole, yield ×10…) → rejet
INDIC_TAIL_MAX_GAP_DAYS = 45         # au-delà : FRED est en panne, pas en retard → pas de rallonge


def _indic_parse(ds):
    return datetime.strptime(ds[:10], "%Y-%m-%d").date()


def _indic_val_at(dates, vals, target):
    for i in range(len(dates) - 1, -1, -1):
        if _indic_parse(dates[i]) <= target:
            return vals[i]
    return vals[0]


def _indic_monthly(dates, vals, nmax=36):
    o = {}
    for ds, v in zip(dates, vals):
        o[ds[:7]] = v
    ks = sorted(o)[-nmax:]
    return [round(o[k], 4) for k in ks]


def _indic_pctile(arr, x):
    n = len(arr)
    if n < 2:
        return 50
    return round(100 * sum(1 for v in arr if v <= x) / n)


def _indic_tail_yahoo(tid, dates, vals):
    """Colle la queue Yahoo (jours postérieurs au dernier point FRED), rebasée.

    Renvoie (dates, vals, meta). Série renvoyée INCHANGÉE et meta=None dès que
    quoi que ce soit cloche (symbole non mappé, Yahoo muet, overlap trop court,
    ratio hors bande, rien de plus récent) : la carte retombe alors sur du FRED
    pur, jamais sur une donnée bricolée.
    """
    sym = INDIC_TAIL_YAHOO.get(tid)
    if not sym:
        return dates, vals, None
    try:
        yd, yv = fetch_yahoo_daily(sym, period1=int(time.time()) - 200 * 86400)
    except Exception as e:
        sys.stderr.write(f"[TAIL] {tid} ({sym}): fetch KO {e}\n")
        return dates, vals, None
    if not yd:
        return dates, vals, None
    ymap = dict(zip(yd, yv))
    # Base = médiane FRED/Yahoo sur les 60 derniers jours de recouvrement.
    common = [(v, ymap[d]) for d, v in zip(dates, vals) if d in ymap and ymap[d]][-60:]
    if len(common) < INDIC_TAIL_MIN_OVERLAP:
        sys.stderr.write(f"[TAIL] {tid} ({sym}): {len(common)} jours communs "
                         f"(< {INDIC_TAIL_MIN_OVERLAP}) — rallonge REFUSÉE\n")
        return dates, vals, None
    ratios = sorted(f / y for f, y in common)
    r = ratios[len(ratios) // 2]
    if not (INDIC_TAIL_RATIO_BAND[0] < r < INDIC_TAIL_RATIO_BAND[1]):
        sys.stderr.write(f"[TAIL] {tid} ({sym}): base ×{r:.3f} hors bande "
                         f"{INDIC_TAIL_RATIO_BAND} — rallonge REFUSÉE\n")
        return dates, vals, None
    add = [(d, ymap[d]) for d in yd if d > dates[-1]]
    if not add:
        return dates, vals, None
    # La rallonge comble un RETARD de publication (2 à 8 jours), pas une panne.
    # Au-delà de 45 jours, la série FRED est cassée/discontinuée : on refuse de
    # la maquiller avec des semaines de données Yahoo, la carte doit montrer le
    # trou pour que le watchdog et l'œil le voient.
    gap = (_indic_parse(add[-1][0]) - _indic_parse(dates[-1])).days
    if gap > INDIC_TAIL_MAX_GAP_DAYS:
        sys.stderr.write(f"[TAIL] {tid} ({sym}): FRED en retard de {gap}j "
                         f"(> {INDIC_TAIL_MAX_GAP_DAYS}) — rallonge REFUSÉE, "
                         f"série probablement en panne côté FRED\n")
        return dates, vals, None
    nd = list(dates) + [d for d, _ in add]
    nv = list(vals) + [round(v * r, 4) for _, v in add]
    sys.stderr.write(f"[TAIL] {tid}: +{len(add)}j depuis {sym} "
                     f"(base ×{r:.4f}) → {add[-1][0]}\n")
    return nd, nv, {"sym": sym, "n": len(add), "base": round(r, 4),
                    "from": dates[-1], "upto": add[-1][0]}


def _indic_native(tid, dates, vals):
    """Historique à la fréquence NATIVE de la série sur la fenêtre INDIC_DH_DAYS.

    Renvoie {dh0, dt, dh, freq} ou None si la série est mensuelle (aucun gain
    face à `mh`) ou masquée du board.
    """
    if tid in INDIC_DH_SKIP:
        return None
    start = _indic_parse(dates[-1]) - timedelta(days=INDIC_DH_DAYS)
    sel = [(_indic_parse(d), v) for d, v in zip(dates, vals) if _indic_parse(d) >= start]
    if len(sel) < INDIC_DH_MIN_PTS:
        return None
    gaps = sorted((sel[i][0] - sel[i - 1][0]).days for i in range(1, len(sel)))
    med = gaps[len(gaps) // 2]
    if med > INDIC_DH_MAX_GAP:
        return None
    d0 = sel[0][0]
    return {"dh0": d0.isoformat(),
            "dt": [(d - d0).days for d, _ in sel],
            "dh": [round(v, 3) for _, v in sel],
            "freq": "quotidien" if med <= 4 else "hebdo"}


def build_indicators_cache(payload):
    """Construit le cache compact des indicateurs (variation) pour l'Accueil."""
    tickers = payload.get("tickers", {})
    items_src = list(tickers.values()) if isinstance(tickers, dict) else list(tickers)
    cats = {k: {"label": INDIC_CATLBL[k], "items": []} for k in INDIC_ORDER}

    def pct(a, b):
        return round((a - b) / abs(b) * 100, 2) if b else None

    for v in items_src:
        vals = v.get("values") or []
        dates = v.get("dates") or []
        if len(vals) < 13 or len(dates) != len(vals):
            continue
        tid = v.get("id")
        cat = v.get("cat", "risque")
        inv = bool(v.get("invert"))
        # Rallonge Yahoo AVANT tout calcul : value, Δ1M/3M/1Y, range 12m et
        # percentile portent alors sur la donnée du jour, pas sur du J-8.
        dates, vals, tail = _indic_tail_yahoo(tid, dates, vals)
        last = vals[-1]
        lastd = _indic_parse(dates[-1])
        mh = _indic_monthly(dates, vals, 36)
        nat = _indic_native(tid, dates, vals)
        v1 = _indic_val_at(dates, vals, lastd - timedelta(days=30))
        v3 = _indic_val_at(dates, vals, lastd - timedelta(days=91))
        v12 = _indic_val_at(dates, vals, lastd - timedelta(days=365))
        win = [vv for dd, vv in zip(dates, vals)
               if _indic_parse(dd) >= lastd - timedelta(days=365)]
        if len(win) < 2:
            win = vals[-12:]
        lo, hi = min(win), max(win)
        if tid in INDIC_TREND:
            mo = {}
            for ds, vv in zip(dates, vals):
                mo[ds[:7]] = vv
            mk = sorted(mo)
            growth = []
            for i in range(12, len(mk)):
                prev = mo[mk[i - 12]]
                if prev:
                    growth.append((mo[mk[i]] - prev) / abs(prev) * 100)
            g_now = growth[-1] if growth else 0
            pr = _indic_pctile(growth, g_now)
            if inv:
                pr = 100 - pr
            pmode = "Δ12m"
        else:
            pr = _indic_pctile(vals, last)
            if inv:
                pr = 100 - pr
            pmode = "niveau"
        cats.setdefault(cat, {"label": cat, "items": []})
        item = {
            "id": tid, "name": v.get("name", tid), "cat": cat,
            "value": round(last, 4), "date": dates[-1], "invert": inv,
            "desc": v.get("desc", ""), "mh": mh,
            "d1": round(last - v1, 4), "d3": round(last - v3, 4),
            "d12": round(last - v12, 4),
            "p1": pct(last, v1), "p3": pct(last, v3), "p12": pct(last, v12),
            "lo12": round(lo, 4), "hi12": round(hi, 4),
            "pctile": pr, "pmode": pmode,
        }
        if nat:
            item.update(nat)          # dh0 + dt + dh + freq (résolution native)
        if tail:
            item["tail"] = tail       # {sym, n, base, from, upto} — traçabilité
        cats[cat]["items"].append(item)

    out = {"fetch": payload.get("fetch_iso"), "cats": cats}
    OUT_INDIC_JSON.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    compact = json.dumps(out, separators=(",", ":"), ensure_ascii=False)
    OUT_INDIC_JS.write_text(
        "(function(){window.__MACRO_INDICATORS__=" + compact + ";})();",
        encoding="utf-8"
    )
    items = [it for c in out["cats"].values() for it in c["items"]]
    nat = [it for it in items if it.get("dh")]
    # Garde-fou anti-régression silencieuse : ces 5 séries SONT quotidiennes chez
    # FRED. Si l'une repasse en 36 points mensuels, la carte reste affichée (repli
    # `mh` côté accueil.js) et personne ne voit rien — d'où l'alerte explicite sur
    # stderr, que le watchdog freshness remonte.
    expected = {"T10Y2Y", "VIXCLS", "DGS10", "BAMLH0A0HYM2", "DCOILWTICO"}
    missing = expected - {it["id"] for it in nat}
    if missing:
        sys.stderr.write(
            f"[WARN] résolution quotidienne PERDUE sur {sorted(missing)} — repli sur "
            f"36 points mensuels. Vérifier que FRED renvoie bien l'historique complet "
            f"(INDIC_DH_MIN_PTS / INDIC_DH_MAX_GAP).\n")
    sys.stderr.write(
        f"[OUT] Wrote {OUT_INDIC_JS} ({len(items)} indicateurs, "
        f"{OUT_INDIC_JS.stat().st_size // 1024} KB) · "
        f"{len(nat)} en résolution native ("
        + ", ".join(f"{it['id']} {len(it['dh'])}pts/{it['freq']}" for it in nat)
        + ")\n"
    )
    return out


def main():
    force = "--force" in sys.argv
    if is_fresh() and not force:
        sys.stderr.write(f"[FRED] Cache fresh (<{CACHE_MAX_HOURS}h). Use --force.\n")
        return

    sys.stderr.write("[FRED] Fetching 16 macro indicators...\n")

    tickers_out = {}
    series_raw = {}
    for t in TICKERS:
        tk = t[0]
        meta = TICKER_META[tk]
        result = fetch_fred(tk)
        if result is None:
            sys.stderr.write(f"[FRED] {tk}: FAILED\n")
            continue
        dates, values = result
        series_raw[tk] = (dates, values)
        last = values[-1] if values else None
        tickers_out[tk] = {
            "id":     tk,
            "name":   meta["name"],
            "color":  meta["color"],
            "desc":   meta["desc"],
            "invert": meta["invert"],
            "seuil":  meta["seuil"],
            "cat":    meta["cat"],
            "dates":  dates,
            "values": values,
            "last":   last,
            "last_date": dates[-1] if dates else None,
        }
        sys.stderr.write(f"[FRED] {tk:15} {len(dates):5} pts · last {dates[-1] if dates else 'N/A'} = {last}\n")
        time.sleep(0.3)  # gentle with FRED

    # ── Fetch des series auxiliaires (pas dans le score composite) ──
    aux_series = {}
    for aux_id, aux_name in AUXILIARY_FRED:
        result = fetch_fred(aux_id)
        if result is None:
            sys.stderr.write(f"[FRED-AUX] {aux_id}: FAILED (optionnel)\n")
            continue
        aux_series[aux_id] = result
        sys.stderr.write(f"[FRED-AUX] {aux_id:15} {len(result[0]):5} pts — {aux_name}\n")
        time.sleep(0.3)

    sys.stderr.write("[SP500] Fetching S&P 500 history...\n")
    sp_d, sp_v = fetch_sp500_history()
    sys.stderr.write(f"[SP500] {len(sp_d)} points\n")

    sys.stderr.write("[MS] Computing Macro Score + sub-indices...\n")
    score = compute_macro_score_series(series_raw)
    score["sp500"] = align_sp500_on_grid(sp_d, sp_v, score["dates"])
    sys.stderr.write(f"[MS] {len(score['dates'])} monthly points · last score: {score['values'][-1] if score['values'] else 'N/A'}\n")

    # ── Cadre investisseur : Growth / Inflation / FinCond + regime + Sahm + yield curve ──
    sys.stderr.write("[INV] Computing investor framework (Growth / Inflation / FinCond + regime + Sahm + YC)...\n")
    # On reutilise le parsed de compute_macro_score_series, donc on recalcule ici (leger)
    parsed_inv = {}
    for tk, data in series_raw.items():
        if not data:
            continue
        dates, values = data
        try:
            pairs = [(datetime.strptime(d, "%Y-%m-%d"), v) for d, v in zip(dates, values)]
            parsed_inv[tk] = sorted(pairs)
        except Exception:
            continue
    # Meme grille mensuelle que compute_macro_score_series + point aujourd'hui (FIX 2026-05-16)
    grid_inv = []
    d = datetime(2005, 1, 1)
    month_start_inv = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while d <= month_start_inv:
        grid_inv.append(d)
        d = d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)
    # Ajout point "aujourd'hui" pour cohérence avec Macro_Trends live
    real_today_inv = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if real_today_inv > month_start_inv:
        grid_inv.append(real_today_inv)

    # Parse T10Y3M (serie auxiliaire, pas dans parsed_inv qui est limite a TICKERS)
    t10y3m_parsed = None
    if "T10Y3M" in aux_series:
        try:
            ad, av = aux_series["T10Y3M"]
            t10y3m_parsed = sorted([(datetime.strptime(d, "%Y-%m-%d"), v) for d, v in zip(ad, av)])
        except Exception as e:
            sys.stderr.write(f"[YC] Parse T10Y3M: {e}\n")

    dims = compute_investor_dimensions(series_raw, grid_inv, parsed_inv)
    regimes = classify_regimes(dims["growth"], dims["inflation"])
    sahm = compute_sahm_rule(parsed_inv.get("UNRATE", []), grid_inv)
    yc = compute_yield_curve_proba(t10y3m_parsed, grid_inv, t10y2y_pairs=parsed_inv.get("T10Y2Y", []))
    # SP500 aligned sur la grille regime pour backtest
    sp500_on_grid = align_sp500_on_grid(sp_d, sp_v, [r["date"] for r in regimes])
    regime_stats = backtest_sp500_by_regime(regimes, sp500_on_grid)

    last_reg = regimes[-1] if regimes else None
    last_sahm = next((x for x in reversed(sahm) if x.get("sahm") is not None), None)
    # ── Daily Financial Conditions Index (sous-indice quotidien) ──
    sys.stderr.write("[FCI] Computing Daily Financial Conditions Index...\n")
    daily_fci = compute_daily_financial_conditions(series_raw, aux_series)
    if daily_fci and daily_fci.get("last_value") is not None:
        sys.stderr.write(f"[FCI] last_value={daily_fci['last_value']} ({daily_fci['last_label']}) on {daily_fci['last_date']} · {len(daily_fci['dates'])} daily points\n")
        # Fetch daily S&P 500 pour overlay pedagogique sur le FCI
        sys.stderr.write("[SP500-DAILY] Fetching daily S&P 500 for FCI overlay...\n")
        sp_dd, sp_dv = fetch_sp500_daily()
        sys.stderr.write(f"[SP500-DAILY] {len(sp_dd)} daily points\n")
        # Aligne via forward-fill sur la grille FCI daily
        if sp_dd:
            sp_aligned = align_sp500_on_grid(sp_dd, sp_dv, daily_fci["dates"])
            daily_fci["sp500"] = sp_aligned
            n_ok = sum(1 for v in sp_aligned if v is not None)
            sys.stderr.write(f"[SP500-DAILY] Aligned {n_ok}/{len(sp_aligned)} FCI dates with S&P 500\n")

        # Fetch actifs additionnels pour backtest multi-asset : BTC, ETH, Gold
        sys.stderr.write("[ASSETS] Fetching BTC / ETH / Gold for backtest...\n")
        daily_fci["assets"] = {"SP500": daily_fci.get("sp500", [])}
        # BTC-USD (disponible depuis ~2014-09-17)
        btc_d, btc_v = fetch_yahoo_daily("BTC-USD")
        if btc_d:
            daily_fci["assets"]["BTC"] = align_sp500_on_grid(btc_d, btc_v, daily_fci["dates"])
            sys.stderr.write(f"[ASSETS] BTC : {len(btc_d)} points, premier={btc_d[0]}\n")
        # ETH-USD (disponible depuis ~2017-11)
        time.sleep(0.3)
        eth_d, eth_v = fetch_yahoo_daily("ETH-USD")
        if eth_d:
            daily_fci["assets"]["ETH"] = align_sp500_on_grid(eth_d, eth_v, daily_fci["dates"])
            sys.stderr.write(f"[ASSETS] ETH : {len(eth_d)} points, premier={eth_d[0]}\n")
        # Gold : GLD ETF (depuis 2004-11) -- plus stable que futures GC=F
        time.sleep(0.3)
        gld_d, gld_v = fetch_yahoo_daily("GLD")
        if gld_d:
            daily_fci["assets"]["GOLD"] = align_sp500_on_grid(gld_d, gld_v, daily_fci["dates"])
            sys.stderr.write(f"[ASSETS] GOLD (GLD) : {len(gld_d)} points, premier={gld_d[0]}\n")
    else:
        sys.stderr.write("[FCI] Insufficient data for daily computation\n")

    last_yc = next((x for x in reversed(yc) if x.get("spread") is not None), None)
    sys.stderr.write(
        f"[INV] regime={last_reg['regime'] if last_reg else 'N/A'} · "
        f"Sahm={last_sahm['sahm'] if last_sahm else 'N/A'} · "
        f"YC_spread={last_yc['spread'] if last_yc else 'N/A'} "
        f"(3m_avg={last_yc.get('spread_3m_avg') if last_yc else 'N/A'}, "
        f"proba_rec_12m={last_yc['recession_proba'] if last_yc else 'N/A'}%, "
        f"source={last_yc.get('source','') if last_yc else ''})\n"
    )

    investor_framework = {
        "dimensions":   dims,          # {growth, inflation, fin_conditions} : {dates, values, momentum, z_raw, components}
        "regimes":      regimes,       # [{date, regime, g_momo, i_momo}, ...]
        "sahm_rule":    sahm,          # [{date, sahm, unrate_ma3, unrate_12m_min, recession}, ...]
        "yield_curve":  yc,            # [{date, t10y2y, recession_proba}, ...]
        "regime_stats": regime_stats,  # {Reflation: {count, avg_monthly, ...}, ...}
        "daily_fci":    daily_fci,     # Daily Financial Conditions : {dates, values, stress_labels, components, methodology}
        "methodology": {
            "growth":    "Moyenne des z-scores YoY (fenetre 120 mois glissants) de INDPRO, RSXFS, MANEMP, CFNAI, -UNRATE, UMCSENT; puis CDF gaussienne -> 0-100",
            "inflation": "Moyenne des z-scores YoY de CPIAUCSL, DCOILWTICO, -DTWEXBGS, WM2NS, +0.5*WALCL; puis CDF gaussienne -> 0-100",
            "fin_cond":  "Moyenne des z-scores YoY de -BAMLH0A0HYM2, -VIXCLS, T10Y2Y, -FEDFUNDS, -DGS10; puis CDF gaussienne -> 0-100",
            "regime":    "Classification 4 quadrants base sur le momentum (score - MA6) de growth et inflation : Reflation (G+/I-), Expansion (G+/I+), Stagflation (G-/I+), Deflation (G-/I-). Zone tampon |momo|<0.25 : sticky sur le regime precedent (evite les flip-flops sur bruit).",
            "sahm":      "MA3(UNRATE) - min(MA3(UNRATE) sur 12 mois precedents). Signal de recession si >= 0.5%. Source : Sahm (2019), Brookings Institution",
            "yield_curve": "P(recession 12m) = 1 - phi(0.5333 - 0.6330*spread_3m_avg) ; probit Estrella-Mishkin (1998, NBER WP5279) calibre pour le spread 10Y-3M avec moyenne mobile 3 mois. Fallback 10Y-2Y si T10Y3M absent.",
        },
        "references": [
            "Dalio R. (2017) 'Principles' / Bridgewater All Weather framework",
            "Sahm C. (2019) 'Direct Stimulus Payments to Individuals' Brookings",
            "Estrella A., Mishkin F. (1998) 'Predicting U.S. Recessions' NY Fed",
            "Chicago Fed National Activity Index (CFNAI) methodology",
        ],
    }

    payload = {
        "tickers":      tickers_out,
        "score_hist":   score,
        "investor":     investor_framework,
        "sub_indices":  SUB_INDICES,
        "fetch_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M CEST"),
        "fetch_iso":       datetime.now().isoformat(),
        "n_tickers_ok":    len(tickers_out),
        "sources": {
            "fred":  "https://fred.stlouisfed.org/graph/fredgraph.csv (no auth, CSV)",
            "sp500": "Yahoo Finance /v8/finance/chart/^GSPC (no auth)",
        },
    }

    # GARDE-FOU ANTI-VIDE (2026-06-29) : si trop peu de séries FRED ont été récupérées
    # (panne réseau / FRED down / rate-limit), NE RIEN ÉCRIRE — sinon on écrase les bons
    # caches existants par des tableaux vides et le widget Accueil « Macro · Indicateurs
    # US » s'affiche incomplet/vide. On préserve la dernière version saine et on sort en
    # erreur (péremption alors visible via le watchdog freshness, qui relancera). Les 16
    # séries proviennent du même endpoint FRED → elles réussissent/échouent en groupe,
    # donc un total sous la moitié = panne systémique, pas une série isolée discontinuée.
    MIN_TICKERS_OK = 8
    if payload["n_tickers_ok"] < MIN_TICKERS_OK:
        sys.stderr.write(
            f"[ABORT] {payload['n_tickers_ok']}/16 FRED récupérés (< {MIN_TICKERS_OK}) — "
            f"écriture ANNULÉE pour préserver les caches sains existants "
            f"(macro_fred_cache.*, macro_indicators_cache.*). Réessai au prochain cycle.\n"
        )
        sys.exit(1)

    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    sys.stderr.write(f"[OUT] Wrote {OUT_JSON} ({OUT_JSON.stat().st_size // 1024} KB)\n")

    compact = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    # Shallow-merge pattern so knit-time fallback isn't dropped if this refresh is partial.
    OUT_JS.write_text(
        "(function(){var fresh=" + compact + ";"
        "window.__MACRO_FRED_LIVE__=fresh;"
        "})();",
        encoding="utf-8"
    )
    sys.stderr.write(f"[OUT] Wrote {OUT_JS}\n")

    # Cache compact des indicateurs (variation) pour l'Accueil — gardé pour ne
    # JAMAIS casser le run principal si la dérivation échoue.
    try:
        build_indicators_cache(payload)
    except Exception as e:
        sys.stderr.write(f"[WARN] indicators cache build failed: {e}\n")

    sys.stderr.write(f"[DONE] {payload['n_tickers_ok']}/16 FRED · last fetch {payload['fetch_timestamp']}\n")


if __name__ == "__main__":
    main()
