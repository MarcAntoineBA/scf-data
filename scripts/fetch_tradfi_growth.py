#!/usr/bin/env python3
"""Fetch Revenue Growth YoY pour les tickers TradFi via yfinance.
Calcule depuis les revenus trimestriels : (revenue_4Q_last - revenue_4Q_prev) / revenue_4Q_prev.
Injecte dans Comparaison_PER_Crypto_TradFi.html via markers __TRADFI_GROWTH__.
"""
import yfinance as yf
import json, sys, warnings, re
from pathlib import Path
from datetime import datetime

warnings.filterwarnings('ignore')

CACHES_DIR = Path.home() / "Library/Caches/site_crypto_finance"
CACHES_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHES_DIR / "tradfi_growth_cache.json"
HTML_FILE  = Path.home() / "Desktop/Site_Crypto_Finance/Comparaison_PER_Crypto_TradFi.html"
CACHE_MAX_HOURS = 12

TICKERS = {
    # Mapping affichage → ticker yfinance
    'Apple': 'AAPL', 'NVIDIA': 'NVDA', 'Microsoft': 'MSFT',
    'Tesla': 'TSLA', 'Alphabet': 'GOOGL', 'Meta': 'META',
    'Amazon': 'AMZN',
    'Alibaba': 'BABA', 'Tencent': 'TCEHY', 'Baidu': 'BIDU'
}

def compute_growth(ticker):
    """Revenue Growth TTM = (revenu derniers 4Q) / (revenu 4Q précédents) - 1"""
    try:
        t = yf.Ticker(ticker)
        # Trimestriels (plus robuste)
        qfin = t.quarterly_financials
        if qfin is not None and 'Total Revenue' in qfin.index:
            rev = qfin.loc['Total Revenue'].dropna().sort_index(ascending=False)
            if len(rev) >= 8:
                last4 = rev.iloc[:4].sum()
                prev4 = rev.iloc[4:8].sum()
                if prev4 > 0:
                    return round(100 * (last4 - prev4) / prev4, 1)
        # Fallback : annual
        afin = t.financials
        if afin is not None and 'Total Revenue' in afin.index:
            rev = afin.loc['Total Revenue'].dropna().sort_index(ascending=False)
            if len(rev) >= 2:
                if rev.iloc[1] > 0:
                    return round(100 * (rev.iloc[0] - rev.iloc[1]) / rev.iloc[1], 1)
        # Fallback : info
        info = t.info
        g = info.get('revenueGrowth')
        if g is not None:
            return round(100 * float(g), 1)
    except Exception as e:
        sys.stderr.write(f'{ticker} err: {e}\n')
    return None

def fetch():
    if CACHE_FILE.exists():
        age_h = (datetime.now().timestamp() - CACHE_FILE.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS:
            sys.stderr.write(f'[TradFi Growth] Cache fresh ({age_h:.1f}h)\n')
            payload = json.load(open(CACHE_FILE))
            inject_into_html(payload)
            return payload

    sys.stderr.write('[TradFi Growth] Fetching live data...\n')
    result = {}
    for name, ticker in TICKERS.items():
        g = compute_growth(ticker)
        if g is not None:
            result[name] = g
            sys.stderr.write(f'{name} ({ticker}): {g}%\n')
        else:
            sys.stderr.write(f'{name} ({ticker}): no growth data\n')

    payload = {'updated': datetime.now().isoformat(), 'data': result}
    with open(CACHE_FILE, 'w') as f:
        json.dump(payload, f)
    sys.stderr.write(f'[TradFi Growth] Wrote {len(result)} tickers\n')
    inject_into_html(payload)
    return payload

def inject_into_html(payload):
    try:
        if not HTML_FILE.exists():
            sys.stderr.write(f'[TradFi Growth] {HTML_FILE} introuvable\n'); return
        html = HTML_FILE.read_text()
        new_block = (
            "// __TRADFI_GROWTH_START__\n"
            "window.__TRADFI_GROWTH__ = " + json.dumps(payload, separators=(',',':')) + ";\n"
            "// __TRADFI_GROWTH_END__"
        )
        pattern = re.compile(r"// __TRADFI_GROWTH_START__.*?// __TRADFI_GROWTH_END__", re.DOTALL)
        if not pattern.search(html):
            sys.stderr.write('[TradFi Growth] markers absents, skip\n'); return
        html2 = pattern.sub(new_block, html)
        HTML_FILE.write_text(html2)
        sys.stderr.write(f'[TradFi Growth] Injected into {HTML_FILE.name}\n')
    except (PermissionError, OSError) as e:
        sys.stderr.write(f'[TradFi Growth] HTML injection skipped (TCC): {e}\n')
        sys.stderr.write('[TradFi Growth] Browser will fetch cache JSON via symlink instead\n')

if __name__ == '__main__':
    fetch()
