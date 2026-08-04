#!/usr/bin/env python3
"""Fetch le VRAI poids des Magnificent 7 dans le S&P 500 (serie hebdo).

Methodologie IDENTIQUE a celle affichee sur Bulle_IA.html et au calcul R statique inline :
  - pour chaque Mag7 : mcap_t = adj_close_t x shares_outstanding actuelles
  - S&P 500 mcap_t   = ^GSPC_t x diviseur free-float 8.3B (officiel, stable 2024-2026)
  - poids_t          = somme_Mag7_mcap_t / S&P_mcap_t x 100

shares_outstanding deduites de market_cap / last_price (point courant exact, conforme
au KPI R statique). Drift historique pre-2024 : +/-5-10% pour les titres ayant rachete/
dilue des actions depuis 2020 (caveat documente sur la page).

ANCIENNE methode (jusqu'au 2026-06-08) : proxy prix XLK/SPY x calibration 27.6% -> FAUX :
  XLK = secteur IT (exclut AMZN/GOOG/META/TSLA, 4 des 7) et un ratio de prix d'ETF n'est
  pas un poids -> surevaluait a ~45% au lieu de ~36%. Cf memoire project_mag7_weight_real_mcap.

Ecrit mag7_hist_cache.json + mag7_hist_cache.js (charge directement par Bulle_IA.html).
"""
import yfinance as yf
import json, sys, warnings, os
from pathlib import Path
from datetime import datetime

warnings.filterwarnings('ignore')

CACHE_DIR  = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "mag7_hist_cache.json"
CACHE_JS   = CACHE_DIR / "mag7_hist_cache.js"
CACHE_MAX_HOURS = 4

MAG7 = ['NVDA', 'MSFT', 'AAPL', 'GOOG', 'AMZN', 'META', 'TSLA']
SP500_DIVISOR = 8.3e9  # diviseur free-float officiel S&P 500 (stable 2024-2026)


def _close_series(df):
    """Extrait une serie Close 1-D depuis un DataFrame yfinance (multi ou simple)."""
    c = df['Close']
    if hasattr(c, 'columns'):
        c = c.iloc[:, 0]
    return c


def _shares_outstanding(ticker):
    """shares = market_cap / last_price (point courant exact, comme le KPI R statique)."""
    fi = yf.Ticker(ticker).fast_info
    mc = fi.get('market_cap') or fi.get('marketCap')
    px = fi.get('last_price') or fi.get('lastPrice')
    if not mc or not px or px <= 0:
        raise RuntimeError(f"{ticker}: market_cap/last_price indisponible (mc={mc}, px={px})")
    return mc / px


def fetch_mag7_weight():
    import pandas as pd

    # Shares outstanding actuelles par ticker (point courant exact)
    shares = {}
    for tk in MAG7:
        shares[tk] = _shares_outstanding(tk)

    # mcap hebdo par ticker = adj_close x shares actuelles
    mcap_cols = {}
    for tk in MAG7:
        h = yf.download(tk, start='2020-01-01', interval='1wk', progress=False)
        if h is None or len(h) == 0:
            raise RuntimeError(f"{tk}: historique hebdo vide")
        c = _close_series(h)
        mcap_cols[tk] = c * shares[tk]

    mag7_mcap = pd.DataFrame(mcap_cols).dropna()
    mag7_sum = mag7_mcap.sum(axis=1)  # somme des 7 mcaps par semaine

    # S&P 500 mcap = ^GSPC x diviseur
    gspc = yf.download('^GSPC', start='2020-01-01', interval='1wk', progress=False)
    gspc_c = _close_series(gspc)
    sp_mcap = gspc_c * SP500_DIVISOR

    # Aligne sur dates communes
    common = mag7_sum.index.intersection(sp_mcap.index)
    weight = (mag7_sum.loc[common] / sp_mcap.loc[common] * 100.0)

    # Point "today" : barre weekly courante pas encore close -> append daily
    try:
        mag7_today = 0.0
        for tk in MAG7:
            d = yf.download(tk, period='5d', interval='1d', progress=False)
            dc = _close_series(d)
            if len(dc):
                mag7_today += float(dc.iloc[-1]) * shares[tk]
        gd = yf.download('^GSPC', period='5d', interval='1d', progress=False)
        gdc = _close_series(gd)
        if mag7_today > 0 and len(gdc):
            last_d = gdc.index[-1]
            w_today = mag7_today / (float(gdc.iloc[-1]) * SP500_DIVISOR) * 100.0
            if last_d > common[-1]:
                weight.loc[last_d] = w_today
            else:
                weight.iloc[-1] = w_today  # remplace la derniere barre par le close du jour
    except Exception as e:
        sys.stderr.write(f"[Mag7] daily append failed: {e}\n")

    weight = weight.sort_index()
    pcts = [round(float(v), 1) for v in weight.values]
    dates = [d.strftime('%Y-%m-%d') for d in weight.index]
    return pcts, dates


def write_js_cache(payload):
    js = "window.__MAG7_HIST_LIVE__=" + json.dumps(payload, separators=(",", ":")) + ";\n"
    _tmpjs = str(CACHE_JS) + ".tmp"
    with open(_tmpjs, "w") as f:
        f.write(js)
    os.replace(_tmpjs, CACHE_JS)
    sys.stderr.write(f"[Mag7] Wrote {CACHE_JS.name}\n")


def main():
    if CACHE_FILE.exists() and CACHE_JS.exists() and "--force" not in sys.argv:
        age_h = (datetime.now().timestamp() - CACHE_FILE.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS:
            sys.stderr.write(f"[Mag7] Cache fresh ({age_h:.1f}h)\n")
            return

    sys.stderr.write("[Mag7] Fetching real Mag7 weight (mcap / S&P 500 mcap)...\n")
    pcts, dates = fetch_mag7_weight()
    sys.stderr.write(f"[Mag7] {len(pcts)} pts, {pcts[0]}% -> peak {max(pcts)}% -> {pcts[-1]}%\n")

    # Sanity check : le vrai poids Mag7 est ~30-45%. Au-dela = bug methodo (ancien proxy XLK/SPY).
    if not (15.0 <= pcts[-1] <= 50.0):
        sys.stderr.write(f"[Mag7] WARN: dernier poids {pcts[-1]}% hors plage plausible 15-50% — cache NON ecrit\n")
        sys.exit(1)

    payload = {"pcts": pcts, "dates": dates, "updated": datetime.now().isoformat()}
    # Ecriture ATOMIQUE (tmp + os.replace) : un render R qui lit pendant l'ecriture
    # ne doit JAMAIS tomber sur un fichier tronque -> seed mag7_hist vide -> concentration gelee.
    _tmp = str(CACHE_FILE) + ".tmp"
    with open(_tmp, "w") as f:
        json.dump(payload, f)
    os.replace(_tmp, CACHE_FILE)
    sys.stderr.write(f"[Mag7] Wrote cache {CACHE_FILE.name}\n")
    write_js_cache(payload)


if __name__ == "__main__":
    main()
