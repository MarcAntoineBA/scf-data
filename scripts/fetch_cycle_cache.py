#!/usr/bin/env python3
"""Fetch cycle data: Top7 Dot-Com (1995-2002) vs Magnificent 7 (Nov 2022-now).
Equal-weight indices base 100. Ecrit cycle_cache.json + cycle_cache.js charge
par Bulle_IA.html pour le graphe "Ou en sommes-nous dans le cycle ?".

Suit le pattern mag7_hist_cache / pe_hist_cache : cache dans ~/Library/Caches/
(non-TCC-protege), symlink depuis ~/Desktop/Site_Crypto_Finance/.
"""
import yfinance as yf
import json, sys, warnings
from pathlib import Path
from datetime import datetime

warnings.filterwarnings('ignore')

CACHE_DIR  = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "cycle_cache.json"
CACHE_JS   = CACHE_DIR / "cycle_cache.js"
CACHE_MAX_HOURS = 12

DOTCOM7 = ['CSCO', 'MSFT', 'INTC', 'ORCL', 'QCOM', 'TXN', 'AMAT']
MAG7    = ['NVDA', 'MSFT', 'AAPL', 'GOOG', 'AMZN', 'META', 'TSLA']


def equal_weight_index(tickers, start, end=None):
    kw = {'start': start, 'interval': '1d', 'progress': False, 'auto_adjust': False}
    if end:
        kw['end'] = end
    data = yf.download(tickers, **kw)
    closes = data['Close'].dropna(how='all')
    n = len(tickers)
    idx = [0.0] * len(closes)
    for t in tickers:
        if t in closes.columns:
            s = closes[t].dropna()
            if len(s) == 0:
                continue
            base = float(s.iloc[0])
            for i, d in enumerate(closes.index):
                if d in s.index and base > 0:
                    idx[i] += float(s.loc[d]) / base * 100
    idx = [round(v / n, 2) for v in idx]
    d0 = closes.index[0]
    return {
        'm': [(d - d0).days for d in closes.index],
        'v': idx,
        'dates': [d.strftime('%Y-%m-%d') for d in closes.index],
    }


def write_js_cache(payload):
    js = "window.__CYCLE_LIVE__=" + json.dumps(payload, separators=(",", ":")) + ";\n"
    CACHE_JS.write_text(js)
    sys.stderr.write(f"[Cycle] Wrote {CACHE_JS.name}\n")


def main():
    if CACHE_FILE.exists() and CACHE_JS.exists() and "--force" not in sys.argv:
        age_h = (datetime.now().timestamp() - CACHE_FILE.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS:
            sys.stderr.write(f"[Cycle] Cache fresh ({age_h:.1f}h)\n")
            return

    sys.stderr.write("[Cycle] Fetching DotCom7 (1995-2003)...\n")
    dotcom = equal_weight_index(DOTCOM7, '1995-01-01', '2003-01-01')
    sys.stderr.write(f"[Cycle] DotCom7: {len(dotcom['m'])} pts, last={dotcom['v'][-1]}\n")

    sys.stderr.write("[Cycle] Fetching MAG7 (Nov 2022-now)...\n")
    mag7 = equal_weight_index(MAG7, '2022-11-01')
    sys.stderr.write(f"[Cycle] MAG7: {len(mag7['m'])} pts, last={mag7['v'][-1]}\n")

    payload = {
        "dotcom7": dotcom,
        "mag7": mag7,
        "updated": datetime.now().isoformat(),
    }
    with open(CACHE_FILE, "w") as f:
        json.dump(payload, f)
    sys.stderr.write(f"[Cycle] Wrote {CACHE_FILE.name}\n")
    write_js_cache(payload)


if __name__ == "__main__":
    main()
