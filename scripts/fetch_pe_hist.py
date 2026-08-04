#!/usr/bin/env python3
"""Fetch P/E TTM historique pour Mag7 + China AI tickers + S&P 500.
Ecrit pe_hist_cache.json — charge en runtime par Bulle_IA.html et Comparaison_PER.html.
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

import yfinance as yf
import json, sys, warnings, urllib.request, re
from pathlib import Path
from datetime import datetime

warnings.filterwarnings('ignore')

# curl_cffi : bypass Yahoo 429 (stdlib requests bloque depuis mai 2026,
# cf project_yahoo_curl_cffi_required). Sans, les fetches yf.Ticker(...).info
# echouent silencieusement et le cache reste figé.
try:
    from curl_cffi import requests as _cr
    _sess = _cr.Session(impersonate='chrome120')
except Exception:
    _sess = None

def fetch_sp500_pe_hist():
    """Scrape multpl.com pour obtenir le P/E historique mensuel du S&P 500.
    Retourne {'dates': ['YYYY-MM-DD', ...], 'pe': [29.92, ...]} trié chronologiquement."""
    try:
        req = urllib.request.Request(
            "https://www.multpl.com/s-p-500-pe-ratio/table/by-month",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        html = urllib.request.urlopen(req, timeout=20).read().decode()
        rows_html = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
        MONTHS = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
                  'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
        pts = []
        for row in rows_html:
            dm = re.search(r'<td>\s*([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})\s*</td>', row)
            if not dm: continue
            mon, day, yr = dm.group(1), int(dm.group(2)), int(dm.group(3))
            after = row[dm.end():]
            vm = re.search(r'(\d+\.\d+)', after)
            if not vm: continue
            pe = float(vm.group(1))
            if not (5 < pe < 200): continue
            iso = f"{yr:04d}-{MONTHS[mon]:02d}-{day:02d}"
            pts.append((iso, pe))
        pts.sort()  # chronologique
        if len(pts) < 10:
            sys.stderr.write(f'[S&P 500 PE] Only {len(pts)} pts — suspicious\n')
            return None
        sys.stderr.write(f'[S&P 500 PE] {len(pts)} monthly pts, last={pts[-1]}\n')
        return {'dates': [p[0] for p in pts], 'pe': [p[1] for p in pts]}
    except Exception as e:
        sys.stderr.write(f'[S&P 500 PE] err: {e}\n')
        return None

CACHES_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHES_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHES_DIR / "pe_hist_cache.json"
JS_FILE = CACHES_DIR / "pe_hist_cache.js"
HTML_FILES = [
    Path(__file__).parent / "Bulle_IA.html",
    Path(__file__).parent / "Comparaison_PER_Crypto_TradFi.html",
]
CACHE_MAX_HOURS = 20  # refresh ~1x/jour

def inject_into_html(payload):
    """Remplace le bloc entre les markers __PE_HIST_LIVE_START__ / __PE_HIST_LIVE_END__."""
    import re
    pattern = re.compile(r"// __PE_HIST_LIVE_START__.*?// __PE_HIST_LIVE_END__", re.DOTALL)
    new_block = (
        "// __PE_HIST_LIVE_START__\n"
        "window.__PE_HIST_LIVE__ = " + json.dumps(payload, separators=(',',':')) + ";\n"
        "// __PE_HIST_LIVE_END__"
    )
    for html_file in HTML_FILES:
        if not html_file.exists():
            sys.stderr.write(f'[PE Hist] {html_file} introuvable, skip\n'); continue
        html = html_file.read_text()
        if not pattern.search(html):
            sys.stderr.write(f'[PE Hist] markers absents dans {html_file.name}, skip\n'); continue
        html2 = pattern.sub(new_block, html)
        html_file.write_text(html2)
        sys.stderr.write(f'[PE Hist] Injected into {html_file.name}\n')

TICKERS = ['NVDA','MSFT','AAPL','GOOG','AMZN','META','TSLA',
           'BABA','BIDU','0700.HK','1810.HK']

def write_outputs(payload):
    """Write both pe_hist_cache.json and pe_hist_cache.js to Caches dir
    (where Desktop symlinks point). HTML loads .js via document.write."""
    with open(CACHE_FILE, 'w') as f:
        json.dump(payload, f)
    with open(JS_FILE, 'w') as f:
        f.write("window.__PE_HIST_LIVE__ = " + json.dumps(payload, separators=(',',':')) + ";\n")
    sys.stderr.write(f'[PE Hist] Wrote {CACHE_FILE.name} + {JS_FILE.name}\n')

def fetch():
    force = '--force' in sys.argv[1:]

    # Preserve nvda_pe_hist (568+ daily pts since 2015) from previous cache —
    # the historical chart needs it and recomputing daily is expensive.
    prev_nvda_pe_hist = None
    prev_updated = None
    if CACHE_FILE.exists():
        try:
            prev_payload = json.load(open(CACHE_FILE))
            prev_nvda_pe_hist = prev_payload.get('nvda_pe_hist')
            prev_updated = prev_payload.get('updated')
        except Exception:
            pass

    # Freshness based on the in-payload `updated` field (real yfinance fetch time),
    # NOT file mtime — the fast path below also writes the file, so mtime would
    # be bumped every run and the cache would appear perpetually fresh.
    if not force and prev_updated:
        try:
            age_h = (datetime.now() - datetime.fromisoformat(prev_updated)).total_seconds() / 3600
        except Exception:
            age_h = None
        if age_h is not None and age_h < CACHE_MAX_HOURS:
            print(f"[PE Hist] Cache fresh ({age_h:.1f}h, updated={prev_updated})", file=sys.stderr)
            payload = prev_payload
            # Refresh S&P 500 P/E independamment (multpl.com est leger a scraper)
            sp500 = fetch_sp500_pe_hist()
            if sp500:
                payload['sp500_pe_hist'] = sp500
                sys.stderr.write(f'[PE Hist] S&P 500 refreshed ({len(sp500["dates"])} pts)\n')
            write_outputs(payload)
            inject_into_html(payload)
            return payload

    print("[PE Hist] Fetching from Yahoo Finance...", file=sys.stderr)
    result = {}
    for sym in TICKERS:
        try:
            t = yf.Ticker(sym, session=_sess) if _sess else yf.Ticker(sym)
            use_yf_pe = sym in ('BABA','BIDU','0700.HK','1810.HK')
            info = None
            hist = t.history(start='2020-01-01', interval='1wk')
            if hist is None or len(hist) < 10:
                sys.stderr.write(f'{sym}: no price history\n'); continue
            # Append latest daily close pour avoir un point "today" (la barre weekly
            # courante n'est pas encore close tant que vendredi n'est pas passe)
            try:
                daily = t.history(period='5d', interval='1d')
                if daily is not None and len(daily) > 0:
                    last_dt = daily.index[-1]
                    last_px = daily['Close'].iloc[-1]
                    if last_dt > hist.index[-1]:
                        hist.loc[last_dt] = daily.iloc[-1]
            except Exception as e:
                sys.stderr.write(f'{sym}: daily append failed: {e}\n')
            closes = hist['Close']
            rd, rp = [], []
            if use_yf_pe:
                if info is None:
                    info = t.info
                current_eps = info.get('trailingEps', 0)
                if not current_eps or current_eps <= 0:
                    # Fallback BIDU : yfinance trailingEps bugge (-0.15) car ADR USD
                    # mais EPS reported CNY → on calcule TTM manuel + FX CNY/USD.
                    if sym == 'BIDU':
                        try:
                            ed = t.get_earnings_dates(limit=80)
                            eps_data = ed[['Reported EPS']].dropna().sort_index() if ed is not None else None
                            if eps_data is None or len(eps_data) < 4:
                                sys.stderr.write(f'{sym}: no earnings fallback\n'); continue
                            fx_tk = yf.Ticker('CNY=X', session=_sess) if _sess else yf.Ticker('CNY=X')
                            fx_hist = fx_tk.history(start='2020-01-01', interval='1wk')
                            fx_closes = fx_hist['Close'] if fx_hist is not None and len(fx_hist) > 0 else None
                            eps_vals = eps_data['Reported EPS'].values
                            eps_dates = [d.date() for d in eps_data.index]
                            ttm = [(eps_dates[i], sum(eps_vals[i-3:i+1])) for i in range(3, len(eps_vals))]
                            for dt, price in closes.items():
                                ds = dt.date()
                                ttm_cny = None
                                for j in range(len(ttm)-1, -1, -1):
                                    if ttm[j][0] <= ds: ttm_cny = ttm[j][1]; break
                                if ttm_cny is None or ttm_cny <= 0: continue
                                fx = 7.0
                                if fx_closes is not None:
                                    for fd, fv in fx_closes.items():
                                        if fd.date() <= ds and float(fv) > 0: fx = float(fv)
                                ttm_usd = ttm_cny / fx
                                pe = round(float(price) / ttm_usd, 1)
                                if 0 < pe < 1000:
                                    rd.append(str(ds)); rp.append(pe)
                        except Exception as e:
                            sys.stderr.write(f'{sym} fallback error: {e}\n'); continue
                    else:
                        sys.stderr.write(f'{sym}: no trailingEps\n'); continue
                else:
                    for dt, price in closes.items():
                        pe = round(float(price) / current_eps, 1)
                        if 0 < pe < 1000:
                            rd.append(str(dt.date())); rp.append(pe)
            else:
                ed = t.get_earnings_dates(limit=80)
                if ed is None or 'Reported EPS' not in ed.columns:
                    sys.stderr.write(f'{sym}: no earnings data\n'); continue
                eps_data = ed[['Reported EPS']].dropna().sort_index()
                if len(eps_data) < 4:
                    sys.stderr.write(f'{sym}: only {len(eps_data)} EPS pts\n'); continue
                eps_vals = eps_data['Reported EPS'].values
                eps_dates = [d.date() for d in eps_data.index]
                ttm = []
                for i in range(3, len(eps_vals)):
                    ttm.append((eps_dates[i], sum(eps_vals[i-3:i+1])))
                for dt, price in closes.items():
                    ds = dt.date()
                    eps = None
                    for j in range(len(ttm)-1, -1, -1):
                        if ttm[j][0] <= ds:
                            eps = ttm[j][1]; break
                    if eps and eps > 0:
                        pe = round(float(price) / eps, 1)
                        if 0 < pe < 1000:
                            rd.append(str(ds)); rp.append(pe)
            # ── Point courant = trailingPE Yahoo (champ trailingPE de yfinance) ──
            # Cohérence + vérifiabilité : c'est EXACTEMENT le P/E affiché sur la page
            # Yahoo key-statistics et lu par l'onglet Analyse Fondamentale (même source).
            # On remplace le dernier point (reconstruit en EPS reportés) par ce trailingPE
            # live, pour que la carte Bulle IA = la valeur vérifiable sur Yahoo.
            try:
                if info is None:
                    info = t.info or {}
                tpe = info.get('trailingPE')
                if tpe and 0 < float(tpe) < 5000 and rp:
                    rp[-1] = round(float(tpe), 1)
            except Exception as e:
                sys.stderr.write(f'{sym}: trailingPE override failed: {e}\n')
            if len(rd) > 5:
                result[sym] = {'dates': rd, 'pe': rp}
                sys.stderr.write(f'{sym}: {len(rd)} pts, last={rp[-1]}x\n')
        except Exception as e:
            sys.stderr.write(f'{sym} error: {e}\n')

    # ── GARDE ANTI-WIPE ──────────────────────────────────────────────────
    # Si TOUS les tickers ont échoué (ex. Yahoo "Too Many Requests"), NE PAS
    # écraser un bon cache existant avec un all_pe_hist vide. On préserve la
    # dernière version connue (les données restent affichées, juste pas plus
    # fraîches). Sans ce garde, un rate-limit transforme la page en cache vide.
    if not result:
        prev_all = (prev_payload.get('all_pe_hist') if 'prev_payload' in dir() and isinstance(prev_payload, dict) else None)
        if prev_all:
            sys.stderr.write('[PE Hist] 0 ticker fetché (rate-limit ?) — cache précédent PRÉSERVÉ (pas d\'écrasement)\n')
            payload = dict(prev_payload)
            sp500_keep = fetch_sp500_pe_hist()
            if sp500_keep:
                payload['sp500_pe_hist'] = sp500_keep
            write_outputs(payload)
            inject_into_html(payload)
            return payload
        sys.stderr.write('[PE Hist] 0 ticker fetché ET aucun cache précédent — abandon sans écrire\n')
        return None

    sp500 = fetch_sp500_pe_hist()
    # `all_pe_hist` is the field name expected by Bulle_IA.html / Comparaison_PER.html
    payload = {'updated': datetime.now().isoformat(), 'all_pe_hist': result}
    if sp500:
        payload['sp500_pe_hist'] = sp500
    # Preserve daily NVDA history (used by the historical modal chart) — the
    # current weekly fetch doesn't recompute it, so we carry it forward and
    # extend it with today's NVDA point if newer.
    if prev_nvda_pe_hist and 'NVDA' in result:
        nvda_w = result['NVDA']
        last_d, last_p = nvda_w['dates'][-1], nvda_w['pe'][-1]
        if prev_nvda_pe_hist['dates'] and last_d > prev_nvda_pe_hist['dates'][-1]:
            prev_nvda_pe_hist['dates'].append(last_d)
            prev_nvda_pe_hist['pe'].append(last_p)
        payload['nvda_pe_hist'] = prev_nvda_pe_hist
    write_outputs(payload)
    print(f"[PE Hist] Wrote {len(result)} tickers + S&P 500 ({len(sp500['dates']) if sp500 else 0} pts)", file=sys.stderr)
    inject_into_html(payload)
    return payload

if __name__ == '__main__':
    fetch()
