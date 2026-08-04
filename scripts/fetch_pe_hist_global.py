#!/usr/bin/env python3
"""Fetch historical P/E ratios for major global indices.

Sources:
  - S&P 500: multpl.com (monthly table, 150+ years of real data)
  - S&P 500 CAPE: multpl.com (Shiller 10Y smoothed)
  - Other indices: computed from ETF dividends + payout ratio methodology:
      PE(t) = Price(t) * Payout_Ratio / Trailing_12M_Dividends(t)
    where Payout_Ratio = current_div_yield * current_PE (assumed stable for indices)

Outputs: pe_hist_global_cache.{json,js}
Run: python3 fetch_pe_hist_global.py [--force]
"""
import json, sys, re, math, time
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.stderr.write("[PE_HIST_GLOBAL] requests/bs4 required\n")
    sys.exit(1)

try:
    import yfinance as yf
except ImportError:
    sys.stderr.write("[PE_HIST_GLOBAL] yfinance required\n")
    sys.exit(1)

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_JSON = CACHE_DIR / "pe_hist_global_cache.json"
CACHE_JS   = CACHE_DIR / "pe_hist_global_cache.js"
CACHE_MAX_HOURS = 24

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HEADERS = {"User-Agent": UA}

# ETFs to compute PE history for. Region tags drive per-region median annotations
# in the chart (Damodaran historical PE benchmarks):
#   USA developed ≈ 16x · Europe developed ≈ 14x · Asia developed ≈ 15x ·
#   Emerging markets ≈ 12x · MSCI World blended ≈ 16x
ETF_PE_LIST = [
    {"ticker": "SPY",  "name": "USA",           "color": "#34d399", "region": "developed_us"},
    {"ticker": "EWJ",  "name": "Japon",         "color": "#5eaff6", "region": "developed_asia"},
    {"ticker": "EWG",  "name": "Allemagne",     "color": "#a78bfa", "region": "developed_eu"},
    {"ticker": "ASHR", "name": "Chine",         "color": "#ff4d6a", "region": "emerging"},
    {"ticker": "INDY", "name": "Inde",          "color": "#f59e0b", "region": "emerging"},
    {"ticker": "EWU",  "name": "Royaume-Uni",   "color": "#9ba1b8", "region": "developed_eu"},
    {"ticker": "EWY",  "name": "Corée du Sud",  "color": "#10b981", "region": "emerging"},
    {"ticker": "EWZ",  "name": "Brésil",        "color": "#fbbf24", "region": "emerging"},
    {"ticker": "EWQ",  "name": "France",        "color": "#fb923c", "region": "developed_eu"},
    {"ticker": "EWC",  "name": "Canada",        "color": "#6366f1", "region": "developed_us"},
    {"ticker": "URTH", "name": "MSCI World",    "color": "#c084fc", "region": "global"},
]

# Historical median P/E per region (Damodaran NYU long-term references)
REGION_MEDIANS = {
    "developed_us":   {"label": "Médiane USA développé ≈ 16x",   "value": 16.0, "color": "rgba(52,211,153,0.30)"},
    "developed_eu":   {"label": "Médiane Europe ≈ 14x",          "value": 14.0, "color": "rgba(167,139,250,0.30)"},
    "developed_asia": {"label": "Médiane Asie développée ≈ 15x", "value": 15.0, "color": "rgba(94,175,246,0.30)"},
    "emerging":       {"label": "Médiane Émergents ≈ 12x",       "value": 12.0, "color": "rgba(251,191,36,0.30)"},
    "global":         {"label": "Médiane MSCI World ≈ 16x",      "value": 16.0, "color": "rgba(192,132,252,0.30)"},
}


def interp_gaps(vals, max_gap=8):
    """Interpole linéairement les trous INTÉRIEURs (None) entre points valides,
    jusqu'à max_gap mois de large (au-delà = vrai manque, on laisse le trou).
    Pas d'extrapolation aux bords. Rend les courbes continues sans inventer
    de longues périodes."""
    out = list(vals)
    idxs = [k for k, v in enumerate(vals) if v is not None]
    if len(idxs) < 2:
        return out
    for a, b in zip(idxs, idxs[1:]):
        if 1 < b - a <= max_gap + 1:
            va, vb = vals[a], vals[b]
            for k in range(a + 1, b):
                out[k] = round(va + (vb - va) * (k - a) / (b - a), 1)
    return out


def clean_nan(obj):
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_nan(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


# ──────────────────────────────────────────────────────────
# S&P 500 real PE from multpl.com
# ──────────────────────────────────────────────────────────
def fetch_multpl_table(url, label):
    sys.stderr.write(f"  {label} (multpl.com)...")
    dates, vals = [], []
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            sys.stderr.write(f" HTTP {r.status_code}\n")
            return dates, vals
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if not table:
            sys.stderr.write(" no table\n")
            return dates, vals
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            try:
                dt = datetime.strptime(tds[0].get_text(strip=True), "%b %d, %Y")
                val = float(re.sub(r"[^0-9.]", "", tds[1].get_text(strip=True)))
                if 0 < val < 500:
                    dates.append(dt.strftime("%Y-%m"))
                    vals.append(round(val, 2))
            except (ValueError, IndexError):
                continue
        if dates and dates[0] > dates[-1]:
            dates.reverse()
            vals.reverse()
        sys.stderr.write(f" {len(dates)} months\n")
    except Exception as e:
        sys.stderr.write(f" error: {e}\n")
    return dates, vals


# ──────────────────────────────────────────────────────────
# Compute PE history from ETF dividends (dividend-based method)
#
# Method:
#   payout_ratio = current_annual_dividend / current_EPS
#   EPS(t) = trailing_12m_dividends(t) / payout_ratio
#   PE(t) = price(t) / EPS(t) = price(t) * payout_ratio / trailing_12m_divs(t)
#
# Assumptions:
#   - Payout ratio is relatively stable for broad index ETFs
#   - Dividends are a reasonable proxy for earnings trajectory
# ──────────────────────────────────────────────────────────
def fetch_weekly_prices(ticker, start_year=2005):
    """Fetch weekly close prices via yfinance. Returns (dates, prices).

    Dates are normalized to ISO-week format 'YYYY-Www' so every ETF aligns
    on the same weekly bucket regardless of which weekday yfinance anchors
    each ticker to (some are Saturday-aligned, others Monday-aligned).
    Within a week, if multiple bars exist we keep the latest close.
    """
    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(start=f"{start_year}-01-01", interval="1wk", auto_adjust=False)
        if hist is None or hist.empty:
            return [], []
        # Bucket by ISO year-week, keep the last close per week
        bucket = {}  # iso_key -> (sort_ts, close)
        for idx, row in hist.iterrows():
            close = float(row["Close"])
            if close <= 0 or math.isnan(close):
                continue
            iso_year, iso_week, _ = idx.isocalendar()
            iso_key = f"{iso_year:04d}-W{iso_week:02d}"
            ts = idx.timestamp()
            prev = bucket.get(iso_key)
            if prev is None or ts > prev[0]:
                bucket[iso_key] = (ts, close)
        dates = sorted(bucket.keys())
        prices = [round(bucket[k][1], 2) for k in dates]
        return dates, prices
    except Exception as e:
        sys.stderr.write(f" [weekly_prices error: {e}]")
        return [], []


def compute_pe_from_dividends(ticker, name, start_year=2005):
    sys.stderr.write(f"  {name} ({ticker})...")
    try:
        tk = yf.Ticker(ticker)
        info = tk.info or {}

        # Get current PE and dividend yield to compute payout ratio
        current_pe = info.get("trailingPE")
        current_div_yield = None
        for dy_key in ["trailingAnnualDividendYield", "dividendYield", "yield"]:
            v = info.get(dy_key)
            if v and v > 0:
                current_div_yield = v if v < 1 else v / 100
                break

        # Fallback: compute from annual dividend rate / price
        if not current_div_yield:
            ann_div = info.get("trailingAnnualDividendRate")
            price = info.get("regularMarketPrice") or info.get("previousClose")
            if ann_div and price and price > 0:
                current_div_yield = ann_div / price

        if not current_pe or not current_div_yield or current_div_yield <= 0:
            sys.stderr.write(f" no PE or DivY (PE={current_pe}, DY={current_div_yield})\n")
            return None

        payout_ratio = current_div_yield * current_pe
        sys.stderr.write(f" payout={payout_ratio:.2f}...")

        # Get historical dividends
        divs = tk.dividends
        if divs is None or len(divs) < 4:
            sys.stderr.write(" not enough dividends\n")
            return None

        # Get monthly prices
        hist = tk.history(start=f"{start_year}-01-01", interval="1mo")
        if hist is None or hist.empty:
            sys.stderr.write(" no price history\n")
            return None

        # Build trailing 12M dividend at each month
        div_by_month = defaultdict(float)
        for dt_idx, amt in divs.items():
            ym = dt_idx.strftime("%Y-%m")
            div_by_month[ym] += float(amt)

        price_months = [(idx.strftime("%Y-%m"), float(row["Close"])) for idx, row in hist.iterrows()]

        month_data = []
        for ym, price in price_months:
            if price <= 0:
                continue
            year = int(ym[:4])
            month = int(ym[5:7])
            trail_div = 0.0
            for m_offset in range(12):
                m = month - m_offset
                y = year
                while m <= 0:
                    m += 12
                    y -= 1
                lookup = f"{y:04d}-{m:02d}"
                trail_div += div_by_month.get(lookup, 0.0)
            if trail_div > 0:
                month_data.append((ym, price, trail_div))

        if len(month_data) < 24:
            sys.stderr.write(" too few months with divs\n")
            return None

        ym_to_divs = {ym: td for ym, _, td in month_data}

        pe_dates = []
        pe_values = []
        eps_values = []
        eps_growth_values = []

        for ym, price, trail_div in month_data:
            pe = price * payout_ratio / trail_div
            if pe < 1 or pe > 300:
                continue
            eps = trail_div / payout_ratio
            year = int(ym[:4]) - 1
            month_prev = ym[5:7]
            ym_prev = f"{year:04d}-{month_prev}"
            prev_divs = ym_to_divs.get(ym_prev)
            eps_g = None
            if prev_divs and prev_divs > 0:
                eps_g = (trail_div / prev_divs - 1) * 100
                if eps_g > 500 or eps_g < -90:
                    eps_g = None
            pe_dates.append(ym)
            pe_values.append(round(pe, 1))
            eps_values.append(round(eps, 3))
            eps_growth_values.append(round(eps_g, 1) if eps_g is not None else None)

        latest_eps_growth = None
        for v in reversed(eps_growth_values):
            if v is not None:
                latest_eps_growth = v
                break

        # Calibration anchor
        if pe_values and current_pe and pe_values[-1] > 0:
            scale = current_pe / pe_values[-1]
            if 0.1 < scale < 10:
                pe_values = [round(v * scale, 1) for v in pe_values]
                eps_values = [round(v / scale, 3) for v in eps_values]

        # ── Outlier clipping [3, 100] ──
        # Wide enough to capture real crisis valuations: S&P 500 hit ~70x in 2009
        # (post-GFC EPS collapse) and ~46x at the 2002 dot-com unwind. Only
        # exclude truly impossible values that are dividend-timing artifacts.
        clipped = 0
        for i in range(len(pe_values)):
            if pe_values[i] is None:
                continue
            if pe_values[i] < 3 or pe_values[i] > 100:
                pe_values[i] = None
                clipped += 1

        # Keep a copy of raw (clipped but unsmoothed) values for the chart
        pe_raw = list(pe_values)

        # ── 3-month rolling median (smoothed view) ──
        # Absorbs single-month dividend timing spikes. Both raw and smoothed
        # series are exposed; the chart can display whichever the user picks.
        smoothed = []
        for i in range(len(pe_values)):
            window = []
            for j in (i-1, i, i+1):
                if 0 <= j < len(pe_values) and pe_values[j] is not None:
                    window.append(pe_values[j])
            if not window:
                smoothed.append(None)
            else:
                window.sort()
                smoothed.append(round(window[len(window)//2], 1))
        pe_values = smoothed

        # Re-anchor last point on live (smoothing can shift the tail)
        if pe_values and current_pe and pe_values[-1] is not None:
            pe_values[-1] = round(current_pe, 1)
        if pe_raw and current_pe and pe_raw[-1] is not None:
            pe_raw[-1] = round(current_pe, 1)

        # Comble les trous intérieurs courts (dividendes irréguliers, mois clippés) →
        # courbe lissée continue. pe_raw garde ses trous (vue brute).
        pe_values = interp_gaps(pe_values)

        # ── Reliability gate ──
        # Bumped to 15% (was 10%) after widening the clip band — fewer false
        # positives. ETFs whose payout_ratio shifted dramatically (Japon EWJ
        # post-Abenomics) still get voided.
        clip_pct = clipped / max(1, len(pe_values))
        if clip_pct > 0.15:
            sys.stderr.write(f" [PE unreliable ({clip_pct*100:.0f}% clipped) — voiding PE series]")
            pe_values = [None] * len(pe_values)
            pe_raw = [None] * len(pe_raw)
            eps_values = [None] * len(eps_values)
            eps_growth_values = [None] * len(eps_growth_values)
            latest_eps_growth = None

        sys.stderr.write(f" clipped={clipped}")

        price_dates, price_values = fetch_weekly_prices(ticker, start_year=start_year)

        sys.stderr.write(f" {len(pe_dates)} months PE, {len(price_dates)} weeks prices, last={pe_values[-1] if pe_values else 'na'}\n")

        if len(pe_dates) < 12 and len(price_dates) < 24:
            return None

        return {
            "pe_dates": pe_dates,
            "pe": pe_values,         # smoothed (3-month rolling median)
            "pe_raw": pe_raw,        # raw clipped values, no smoothing
            "eps": eps_values,
            "eps_growth": eps_growth_values,
            "current_eps_growth": latest_eps_growth,
            "price_dates": price_dates,
            "prices": price_values,
            "method": "dividend_payout_anchored",
            "payout_ratio": round(payout_ratio, 3),
            "current_pe": round(current_pe, 1),
        }

    except Exception as e:
        sys.stderr.write(f" error: {e}\n")
        return None


def main():
    if CACHE_JSON.exists() and "--force" not in sys.argv:
        age_h = (datetime.now().timestamp() - CACHE_JSON.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS:
            sys.stderr.write(f"[PE_HIST_GLOBAL] Cache fresh ({age_h:.1f}h).\n")
            return

    sys.stderr.write("[PE_HIST_GLOBAL] Fetching real P/E history...\n")

    # S&P 500 trailing P/E (real monthly data from multpl.com — 150+ years)
    sp_dates, sp_pes = fetch_multpl_table(
        "https://www.multpl.com/s-p-500-pe-ratio/table/by-month", "S&P 500 P/E")

    # S&P 500 CAPE (Shiller 10Y smoothed)
    cape_dates, cape_pes = fetch_multpl_table(
        "https://www.multpl.com/shiller-pe/table/by-month", "S&P 500 CAPE")

    # Compute PE history for all ETFs using dividend method
    etf_pe_series = {}
    for etf_info in ETF_PE_LIST:
        result = compute_pe_from_dividends(etf_info["ticker"], etf_info["name"])
        if result:
            etf_pe_series[etf_info["name"]] = {
                "ticker": etf_info["ticker"],
                "color": etf_info["color"],
                "region": etf_info["region"],
                "pe_dates": result["pe_dates"],
                "pe": result["pe"],
                "pe_raw": result.get("pe_raw"),
                "price_dates": result["price_dates"],
                "prices": result["prices"],
                "eps": result["eps"],
                "eps_growth": result["eps_growth"],
                "current_eps_growth": result["current_eps_growth"],
                "method": result["method"],
                "payout_ratio": result["payout_ratio"],
                "current_pe_live": result["current_pe"],
            }
        time.sleep(0.3)

    # CRITICAL: refuse to write a degenerate cache. If both the SP500 series AND
    # all ETF series came back empty (typical: DNS / network outage during the
    # cron run), keep the previous good cache instead of overwriting with zeros.
    # Without this guard, a single transient failure permanently disables the
    # P/E history charts until the next 24h tick.
    if len(sp_dates) == 0 and len(etf_pe_series) == 0:
        sys.stderr.write(
            "[PE_HIST_GLOBAL] All sources failed (likely network outage). "
            "Refusing to overwrite previous cache. Exiting non-zero so launchd "
            "can log it; previous cache preserved.\n"
        )
        sys.exit(2)

    # Merge with previous good cache when this run lost ETFs to transient errors.
    # A DNS blip mid-loop (yfinance times out for half the list, recovers for the
    # rest) used to silently overwrite the cache with the surviving subset and
    # leave the chart showing 4 markets instead of 10. Now: for any ETF that the
    # current run failed to produce, keep its previous series if available.
    prev_indices = {}
    if CACHE_JSON.exists():
        try:
            prev = json.load(open(CACHE_JSON))
            prev_indices = prev.get("indices") or {}
        except Exception:
            prev_indices = {}
    expected_names = {e["name"] for e in ETF_PE_LIST}
    fresh_names = set(etf_pe_series.keys())
    missing = expected_names - fresh_names
    recovered = 0
    for name in missing:
        if name in prev_indices:
            etf_pe_series[name] = prev_indices[name]
            recovered += 1
    if recovered:
        sys.stderr.write(
            f"[PE_HIST_GLOBAL] {recovered} ETF series carried over from previous cache "
            f"(this run failed to fetch them — likely transient network).\n"
        )

    # Note : l'indice USA reste sur la même base que les 16 autres pays (reconstruction
    # par dividendes ancrée sur le P/E trailing live de l'ETF), pour que les indices
    # soient comparables entre eux. La série GAAP réelle (multpl) est exposée séparément
    # via "sp500_multpl" et n'est utilisée QUE comme benchmark du graphe par secteur.

    payload = clean_nan({
        "sp500_multpl": {"dates": sp_dates, "pe": sp_pes, "name": "S&P 500 (multpl.com)",
                         "color": "#34d399", "source": "multpl.com", "method": "real_trailing_PE"},
        "sp500_cape": {"dates": cape_dates, "pe": cape_pes, "name": "S&P 500 CAPE (Shiller)",
                       "color": "#c084fc", "source": "multpl.com", "method": "shiller_10Y_avg"},
        "indices": etf_pe_series,
        "region_medians": REGION_MEDIANS,
        "updated": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "methodology": "PE computed from ETF Price * Payout_Ratio / Trailing_12M_Dividends. "
                       "Pipeline: scale anchor on live PE → clip [3,100] → 3-month rolling median → re-anchor. "
                       "Both raw (pe_raw) and smoothed (pe) series exposed. "
                       "Series with >15% clipped points are voided. Limitation: dividend-based "
                       "reconstruction overstates PE for low-payout markets (US tech) and understates "
                       "for high-payout markets (European utilities). Real EPS data requires paid feeds."
    })

    with open(CACHE_JSON, "w") as f:
        json.dump(payload, f, ensure_ascii=False)
    sys.stderr.write(f"[PE_HIST_GLOBAL] Wrote {CACHE_JSON}\n")

    js_payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    CACHE_JS.write_text(f"window.__PE_HIST_GLOBAL__={js_payload};")
    sys.stderr.write(f"[PE_HIST_GLOBAL] Wrote {CACHE_JS}\n")

    n_etf = len(etf_pe_series)
    sys.stderr.write(f"[PE_HIST_GLOBAL] Done: SP500={len(sp_dates)} months, CAPE={len(cape_dates)}, "
                     f"{n_etf} ETF PE series via dividends\n")


if __name__ == "__main__":
    main()
