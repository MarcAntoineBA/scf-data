#!/usr/bin/env python3
"""Fetch 20-year historical prices for global equity indices.

Uses yfinance to get monthly closing prices for each index ticker.
Computes base-100 cumulative performance series.

Outputs:
  - global_hist_cache.json  (monthly prices + base100 series)
  - global_hist_cache.js    (window.__GLOBAL_HIST__ = {...};)

Run: python3 fetch_global_hist.py [--force]
"""
import json, sys, math, time
from pathlib import Path
from datetime import datetime, timedelta

try:
    import yfinance as yf
except ImportError:
    sys.stderr.write("[GLOBAL_HIST] yfinance not installed.\n")
    sys.exit(1)

CACHE_DIR  = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_JSON = CACHE_DIR / "global_hist_cache.json"
CACHE_JS   = CACHE_DIR / "global_hist_cache.js"
CACHE_MAX_HOURS = 24  # Daily refresh is enough for monthly data

# Tickers to fetch — use ETFs for consistency (most have 15-20 years of data)
INDICES = [
    {"key": "USA",           "ticker": "SPY",  "name": "S&P 500",       "color": "#34d399"},
    {"key": "Chine",         "ticker": "MCHI", "name": "CSI 300",        "color": "#ff4d6a"},
    {"key": "Inde",          "ticker": "INDA", "name": "Nifty 50",       "color": "#f59e0b"},
    {"key": "Japon",         "ticker": "EWJ",  "name": "Nikkei 225",     "color": "#5eaff6"},
    {"key": "Allemagne",     "ticker": "EWG",  "name": "DAX 40",         "color": "#a78bfa"},
    {"key": "France",        "ticker": "EWQ",  "name": "CAC 40",         "color": "#fb923c"},
    {"key": "Royaume-Uni",   "ticker": "EWU",  "name": "FTSE 100",       "color": "#9ba1b8"},
    {"key": "Corée du Sud",  "ticker": "EWY",  "name": "KOSPI",          "color": "#10b981"},
    {"key": "Brésil",        "ticker": "EWZ",  "name": "Bovespa",        "color": "#fbbf24"},
    {"key": "Canada",        "ticker": "EWC",  "name": "TSX Composite",  "color": "#6366f1"},
    {"key": "Australie",     "ticker": "EWA",  "name": "ASX 200",        "color": "#ec4899"},
    {"key": "Taïwan",        "ticker": "EWT",  "name": "TAIEX",          "color": "#14b8a6"},
    {"key": "Hong Kong",     "ticker": "EWH",  "name": "Hang Seng",      "color": "#e11d48"},
    {"key": "MSCI World",    "ticker": "URTH", "name": "MSCI World",     "color": "#a78bfa"},
]

START_DATE = "2005-01-01"


def clean_nan(obj):
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_nan(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def fetch_monthly_prices(ticker, start=START_DATE):
    """Fetch monthly closing prices from yfinance."""
    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(start=start, interval="1mo")
        if hist is None or hist.empty:
            return [], []
        dates = [d.strftime("%Y-%m") for d in hist.index]
        prices = [round(float(p), 2) for p in hist["Close"]]
        return dates, prices
    except Exception as e:
        sys.stderr.write(f"  [{ticker}] error: {e}\n")
        return [], []


def compute_base100(prices):
    """Convert price series to base-100 cumulative performance."""
    if not prices or prices[0] is None or prices[0] <= 0:
        return []
    base = prices[0]
    return [round(p / base * 100, 1) if p and p > 0 else None for p in prices]


def main():
    if CACHE_JSON.exists() and "--force" not in sys.argv:
        age_h = (datetime.now().timestamp() - CACHE_JSON.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS:
            sys.stderr.write(f"[GLOBAL_HIST] Cache fresh ({age_h:.1f}h). Use --force.\n")
            return

    sys.stderr.write("[GLOBAL_HIST] Fetching 20-year monthly data...\n")

    data = {}
    for idx in INDICES:
        sys.stderr.write(f"  {idx['key']} ({idx['ticker']})...")
        dates, prices = fetch_monthly_prices(idx["ticker"])
        base100 = compute_base100(prices)
        data[idx["key"]] = {
            "ticker": idx["ticker"],
            "name": idx["name"],
            "color": idx["color"],
            "dates": dates,
            "prices": prices,
            "base100": base100,
        }
        sys.stderr.write(f" {len(dates)} months\n")
        time.sleep(0.3)

    # Build a unified date axis (union of all dates)
    all_dates = sorted(set(d for v in data.values() for d in v["dates"]))
    sys.stderr.write(f"  Unified timeline: {len(all_dates)} months ({all_dates[0] if all_dates else '?'} to {all_dates[-1] if all_dates else '?'})\n")

    # Align all series to the unified timeline
    for key, v in data.items():
        date_map = dict(zip(v["dates"], v["base100"]))
        v["aligned_base100"] = [date_map.get(d) for d in all_dates]
        # Also compute since common start (first date where ALL series have data)
        price_map = dict(zip(v["dates"], v["prices"]))
        v["aligned_prices"] = [price_map.get(d) for d in all_dates]

    payload = clean_nan({
        "dates": all_dates,
        "series": {k: {"name": v["name"], "ticker": v["ticker"], "color": v["color"],
                        "base100": v["aligned_base100"]}
                   for k, v in data.items()},
        "start": all_dates[0] if all_dates else None,
        "end": all_dates[-1] if all_dates else None,
        "updated": datetime.now().strftime("%d/%m/%Y %H:%M"),
    })

    with open(CACHE_JSON, "w") as f:
        json.dump(payload, f, ensure_ascii=False)
    sys.stderr.write(f"[GLOBAL_HIST] Wrote {CACHE_JSON}\n")

    js_payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    CACHE_JS.write_text(f"window.__GLOBAL_HIST__={js_payload};")
    sys.stderr.write(f"[GLOBAL_HIST] Wrote {CACHE_JS}\n")


if __name__ == "__main__":
    main()
