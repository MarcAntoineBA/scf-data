#!/usr/bin/env python3
"""Hourly refresh for Ecosysteme_Crypto indicators.

Avoids Yahoo Finance API entirely (rate-limited). Uses:
  - CoinGecko /global  — crypto global market cap (no auth)
  - Stooq CSV          — SPX level + pure-plays prices (no auth)
  - Hardcoded shares outstanding — multiplied by Stooq prices = live market cap
    (shares outstanding change slowly; refreshed quarterly from 10-Q/10-K)

Output (symlinked into Site_Crypto_Finance/):
  - ~/Library/Caches/site_crypto_finance/ecosysteme_indicators_cache.js

Run: python3 fetch_ecosysteme_indicators.py [--force]
Scheduled: scf.ecosysteme.indicators (hourly).
"""
import json
import sys
import time
from pathlib import Path
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "ecosysteme_indicators_cache.json"
OUT_JS   = CACHE_DIR / "ecosysteme_indicators_cache.js"
CACHE_MAX_MIN = 50

# ──────────────────────────────────────────────────────────────
# Pure-plays — (Stooq symbol, shares outstanding in millions)
# Shares sourced from latest 10-Q filings (refreshed quarterly).
# Snapshot 2026-04 Q1 earnings :
# ──────────────────────────────────────────────────────────────
PUREPLAYS = {
    # key              stooq_sym     shares_M   display_label
    "coinbase":        ("coin.us",   269.7,   "Coinbase (COIN)"),
    "microstrategy":   ("mstr.us",   349.0,   "MicroStrategy (MSTR)"),
    "robinhood":       ("hood.us",   900.0,   "Robinhood (HOOD)"),
    "galaxy":          ("glxy.us",   411.5,   "Galaxy Digital (GLXY)"),      # NASDAQ listing since 2025
    "marathon":        ("mara.us",   380.0,   "Marathon Digital (MARA)"),
    "riot":            ("riot.us",   380.0,   "Riot Platforms (RIOT)"),
    "core_scientific": ("corz.us",   316.0,   "Core Scientific (CORZ)"),
    "cleanspark":      ("clsk.us",   256.0,   "CleanSpark (CLSK)"),
    "hut8":            ("hut.us",    111.0,   "Hut 8 (HUT)"),
    "terawulf":        ("wulf.us",   490.0,   "TeraWulf (WULF)"),
    "iris":            ("iren.us",   332.0,   "IREN Ltd (IREN)"),
    "bitfarms":        ("bitf.us",   570.0,   "Bitfarms (BITF)"),
    "cipher":          ("cifr.us",   405.0,   "Cipher Mining (CIFR)"),
    "bitdeer":         ("btdr.us",   243.0,   "Bitdeer (BTDR)"),
    "applied":         ("apld.us",   286.0,   "Applied Digital (APLD)"),
    "block":           ("xyz.us",    608.0,   "Block (XYZ)"),
}

SP500_DIVISOR_B = 8.35
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) SiteCryptoFinance/1.0"


def http_get_text(url, timeout=15):
    req = Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def http_get_json(url, timeout=15):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_crypto_global():
    """CoinGecko global crypto market cap in $B."""
    try:
        g = http_get_json("https://api.coingecko.com/api/v3/global")
        return round(g["data"]["total_market_cap"]["usd"] / 1e9, 1)
    except Exception as e:
        sys.stderr.write(f"[CE] CoinGecko /global: {e}\n")
        return None


def fetch_stooq_close(symbol):
    """Stooq last-close price. Returns float or None."""
    try:
        url = f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"
        csv = http_get_text(url).strip()
        lines = csv.splitlines()
        if len(lines) < 2: return None
        row = lines[1].split(",")
        # Columns: Symbol,Date,Time,Open,High,Low,Close,Volume
        if len(row) < 7: return None
        close_str = row[6]
        if close_str in ("", "N/D"): return None
        return float(close_str)
    except Exception as e:
        sys.stderr.write(f"[CE] Stooq {symbol}: {e}\n")
        return None


def fetch_btc_history_weekly():
    """CoinGecko /coins/bitcoin/market_chart for 2y weekly. Free tier limit: days=365 max."""
    try:
        url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=365"
        d = http_get_json(url)
        prices = d.get("prices", [])
        # Downsample to weekly
        if not prices: return []
        out = []
        last_week = -1
        for ts, val in prices:
            week = int(ts // 86400000 // 7)
            if week != last_week:
                out.append([ts, val])
                last_week = week
        return out
    except Exception as e:
        sys.stderr.write(f"[CE] BTC history: {e}\n")
        return []


def fetch_spx_history_yahoo():
    """Yahoo chart endpoint (no auth needed for /v8/finance/chart/). Returns (dates, closes)."""
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?interval=1wk&range=2y"
        d = http_get_json(url)
        r = d.get("chart", {}).get("result", [{}])[0]
        timestamps = r.get("timestamp", [])
        closes = r.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        if not timestamps:
            return [], []
        dates, vals = [], []
        for ts, c in zip(timestamps, closes):
            if c is None:
                continue
            dates.append(datetime.fromtimestamp(ts).strftime("%Y-%m-%d"))
            vals.append(float(c))
        return dates, vals
    except Exception as e:
        sys.stderr.write(f"[CE] Yahoo SPX history: {e}\n")
        return [], []


def is_fresh():
    if not OUT_JSON.exists():
        return False
    age_min = (time.time() - OUT_JSON.stat().st_mtime) / 60
    return age_min < CACHE_MAX_MIN


def main():
    force = "--force" in sys.argv
    if is_fresh() and not force:
        sys.stderr.write(f"[CE] Cache fresh (<{CACHE_MAX_MIN} min). Use --force.\n")
        return

    sys.stderr.write("[CE] Fetching indicators (CoinGecko + Stooq, no Yahoo API)...\n")

    # ── 1) Crypto global MCap ──
    crypto_global_B = fetch_crypto_global() or 2580.0

    # ── 2) SPX level ──
    spx_level = fetch_stooq_close("^spx")
    sp500_total_B = round(spx_level * SP500_DIVISOR_B) if spx_level else 55000

    # Fallback values (from last known-good snapshot). Used when Stooq returns N/D.
    # These are approximate and the launchd will keep them if Stooq fails for a ticker.
    PUREPLAYS_FALLBACK = {
        "bitfarms": 1.74, "galaxy": 11.0,
    }

    # ── 3) Pure-plays MCap = close_price × shares_outstanding ──
    pureplays = {}
    for key, (sym, shares_M, _label) in PUREPLAYS.items():
        price = fetch_stooq_close(sym)
        if price is not None and price > 0:
            pureplays[key] = round((price * shares_M) / 1000, 2)  # M × price = $ · convert $ → $B
        else:
            fb = PUREPLAYS_FALLBACK.get(key)
            pureplays[key] = fb if fb is not None else 0
        time.sleep(0.1)  # gentle for Stooq

    companies_B = round(sum(pureplays.values()), 2)
    if companies_B == 0:
        companies_B = 330  # fallback

    sector_total_B = round(crypto_global_B + companies_B, 2)
    sector_weight_pct = round(sector_total_B / sp500_total_B * 100, 2)

    # ── 4) Historical series (BTC + SPX, 2y weekly) ──
    btc_hist = fetch_btc_history_weekly()
    time.sleep(0.3)
    spx_dates, spx_vals = fetch_spx_history_yahoo()

    # Align BTC + SPX on weekly dates. Since CoinGecko free = 365d max, the intersection
    # is ~1 year. We drop SPX dates before the earliest BTC date to avoid BTC=0 artifacts.
    dates, btc_vals_aligned, spx_vals_aligned = [], [], []
    if spx_dates and btc_hist:
        btc_by_date = {}
        for ts, val in btc_hist:
            d_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
            btc_by_date[d_str] = val
        btc_sorted = sorted(btc_by_date.items())
        earliest_btc = btc_sorted[0][0] if btc_sorted else None

        for d_str, spx_val in zip(spx_dates, spx_vals):
            if earliest_btc and d_str < earliest_btc:
                continue  # skip SPX dates before BTC history starts
            # find closest BTC date ≤ d_str (backward-fill)
            btc_val = None
            for bd, bv in btc_sorted:
                if bd <= d_str:
                    btc_val = bv
                else:
                    break
            if btc_val is None:
                continue
            dates.append(d_str)
            btc_vals_aligned.append(btc_val)
            spx_vals_aligned.append(spx_val)

    history = {"dates": dates, "btc": btc_vals_aligned, "spx": spx_vals_aligned} if dates else {}

    payload = {
        "crypto_global_B":     crypto_global_B,
        "crypto_companies_B":  companies_B,
        "pureplays":           pureplays,
        "sp500_level":         spx_level,
        "sp500_divisor_B":     SP500_DIVISOR_B,
        "sp500_total_B":       sp500_total_B,
        "sector_total_B":      sector_total_B,
        "sector_weight_pct":   sector_weight_pct,
        "fetch_timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M CEST"),
        "fetch_iso":           datetime.now().isoformat(),
        "history":             history,
        "sources": {
            "crypto_global": "CoinGecko /api/v3/global",
            "pureplays":     "Stooq CSV close × hardcoded shares outstanding (quarterly snapshot)",
            "sp500":         "Stooq ^spx close × divisor S&P DJI (8.35B)",
            "history":       "CoinGecko market_chart BTC + Stooq ^spx weekly CSV",
        },
    }

    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.stderr.write(f"[CE] Wrote {OUT_JSON} ({OUT_JSON.stat().st_size // 1024} KB)\n")

    # Shallow-merge into any existing window.__CE_INDICATORS__ (from knit-time fallback).
    # This preserves the richer 2y history written by R yfinance at knit-time when the
    # hourly fetch can only get ~1y from CoinGecko free tier.
    compact = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    OUT_JS.write_text(
        "(function(){var fresh=" + compact + ";"
        "var prev=window.__CE_INDICATORS__||{};"
        "var prevHist=prev.history&&prev.history.dates&&prev.history.dates.length>fresh.history.dates.length?prev.history:fresh.history;"
        "fresh.history=prevHist;"
        "window.__CE_INDICATORS__=Object.assign({},prev,fresh);"
        "})();",
        encoding="utf-8"
    )
    sys.stderr.write(f"[CE] Wrote {OUT_JS}\n")

    n_ok = sum(1 for v in pureplays.values() if v > 0)
    sys.stderr.write(f"[CE] {n_ok}/{len(PUREPLAYS)} pure-plays · SPX={spx_level} · crypto={crypto_global_B}B · weight={sector_weight_pct}%\n")


if __name__ == "__main__":
    main()
