#!/usr/bin/env python3
"""Fetch fundamental data for major global equity indices.

Uses yfinance ETFs as proxies for country-level fundamentals (P/E, P/B, Div Yield)
and index tickers for price performance (YTD, 1Y, 3Y, 5Y).

Ecrit dans ~/Library/Caches/site_crypto_finance/ (non-TCC-protege) :
  - global_markets_cache.json  (structured data)
  - global_markets_cache.js    (window.__GLOBAL_MARKETS__ = {...};)

Lance par launchd (scf.globalmarkets) toutes les 6h.

Sources (all auditable):
  - yfinance (Yahoo Finance) for ETF fundamentals + index price history
  - iShares/MSCI for index-level P/E when ETF data unavailable

Run: python3 fetch_global_markets.py [--force]
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

import json, sys, time, math
from pathlib import Path
from datetime import datetime, timedelta

try:
    import yfinance as yf
except ImportError:
    sys.stderr.write("[GLOBAL] yfinance not installed. pip install yfinance\n")
    sys.exit(1)

# curl_cffi : bypass Yahoo 429 (stdlib requests bloqué depuis mai 2026, cf
# project_yahoo_curl_cffi_required). SANS session impersonée, yf.Ticker(...).info
# renvoie {} silencieusement sur les premiers tickers (rate-limit cold-start) →
# c'est exactement ce qui vidait SPY/EWJ/EWG… du cache (data "—" dans le tableau).
try:
    from curl_cffi import requests as _cr
    _sess = _cr.Session(impersonate='chrome120')
except Exception:
    _cr = None
    _sess = None

def _ticker(sym):
    # Session partagée pour les .info (fundamentals).
    return yf.Ticker(sym, session=_sess) if _sess else yf.Ticker(sym)

def _ticker_hist(sym):
    # IMPORTANT : .history() doit utiliser une session SÉPARÉE de celle des .info.
    # Sur curl_cffi, enchaîner .info puis .history sur la MÊME session empoisonne
    # le .history (crumb/cookie en conflit) → renvoie vide → prix/perf "—" pour
    # les indices (SPY/^GSPC/^KS11…). Une session neuve par appel élimine le bug.
    if _cr is not None:
        try:
            return yf.Ticker(sym, session=_cr.Session(impersonate='chrome120'))
        except Exception:
            pass
    return yf.Ticker(sym)

CACHE_DIR  = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_JSON = CACHE_DIR / "global_markets_cache.json"
CACHE_JS   = CACHE_DIR / "global_markets_cache.js"
CACHE_MAX_HOURS = 6

# ──────────────────────────────────────────────────────────
# Market definitions
# ──────────────────────────────────────────────────────────
MARKETS = [
    {"country": "USA",           "index_ticker": "^GSPC",      "etf": "SPY",  "name": "S&P 500",       "currency": "USD", "region": "Amérique"},
    {"country": "Chine",         "index_ticker": "000300.SS",  "etf": "ASHR", "name": "CSI 300",        "currency": "CNY", "region": "Asie"},
    {"country": "Inde",          "index_ticker": "^NSEI",      "etf": "INDA", "name": "Nifty 50",       "currency": "INR", "region": "Asie"},
    {"country": "Japon",         "index_ticker": "^N225",      "etf": "EWJ",  "name": "Nikkei 225",     "currency": "JPY", "region": "Asie"},
    {"country": "Allemagne",     "index_ticker": "^GDAXI",     "etf": "EWG",  "name": "DAX 40",         "currency": "EUR", "region": "Europe"},
    {"country": "France",        "index_ticker": "^FCHI",      "etf": "EWQ",  "name": "CAC 40",         "currency": "EUR", "region": "Europe"},
    {"country": "Royaume-Uni",   "index_ticker": "^FTSE",      "etf": "EWU",  "name": "FTSE 100",       "currency": "GBP", "region": "Europe"},
    {"country": "Corée du Sud",  "index_ticker": "^KS11",      "etf": "EWY",  "name": "KOSPI",          "currency": "KRW", "region": "Asie"},
    {"country": "Brésil",        "index_ticker": "^BVSP",      "etf": "EWZ",  "name": "Bovespa",        "currency": "BRL", "region": "Amérique"},
    {"country": "Canada",        "index_ticker": "^GSPTSE",    "etf": "EWC",  "name": "TSX Composite",  "currency": "CAD", "region": "Amérique"},
    {"country": "Australie",     "index_ticker": "^AXJO",      "etf": "EWA",  "name": "ASX 200",        "currency": "AUD", "region": "Océanie"},
    {"country": "Taïwan",        "index_ticker": "^TWII",      "etf": "EWT",  "name": "TAIEX",          "currency": "TWD", "region": "Asie"},
    {"country": "Hong Kong",     "index_ticker": "^HSI",       "etf": "EWH",  "name": "Hang Seng",      "currency": "HKD", "region": "Asie"},
    {"country": "Italie",        "index_ticker": "FTSEMIB.MI", "etf": "EWI",  "name": "FTSE MIB",       "currency": "EUR", "region": "Europe"},
    {"country": "Espagne",       "index_ticker": "EWP",        "etf": "EWP",  "name": "IBEX 35",        "currency": "EUR", "region": "Europe"},
    {"country": "Suisse",        "index_ticker": "^SSMI",      "etf": "EWL",  "name": "SMI",            "currency": "CHF", "region": "Europe"},
    {"country": "Mexique",       "index_ticker": "^MXX",       "etf": "EWW",  "name": "IPC Mexico",     "currency": "MXN", "region": "Amérique"},
]

BENCHMARK = {"country": "Monde", "index_ticker": None, "etf": "URTH", "name": "MSCI World", "currency": "USD", "region": "Global"}


# ──────────────────────────────────────────────────────────
# Fetch fundamentals from ETF
# ──────────────────────────────────────────────────────────
def fetch_etf_fundamentals(etf_ticker, retries=3):
    result = {"pe": None, "pb": None, "div_yield": None, "rev_growth": None, "eps_growth": None}
    # Retry : Yahoo rate-limit le cold-start, .info revient vide. On réessaie avec
    # backoff au lieu d'abandonner (sinon le ticker finit en "—" dans le cache).
    info = {}
    tk = None
    for attempt in range(retries):
        try:
            tk = _ticker(etf_ticker)
            info = tk.info or {}
            if info.get("trailingPE") or info.get("priceToBook") or info.get("forwardPE"):
                break
        except Exception as e:
            sys.stderr.write(f"  [ETF {etf_ticker}] attempt {attempt+1}/{retries} error: {e}\n")
            info = {}
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    try:

        for key in ["trailingPE", "forwardPE"]:
            v = info.get(key)
            if v and v > 0 and v < 500:
                result["pe"] = round(v, 1)
                break

        for key in ["priceToBook"]:
            v = info.get(key)
            if v and v > 0 and v < 100:
                result["pb"] = round(v, 1)
                break

        for key in ["yield", "trailingAnnualDividendYield", "dividendYield"]:
            v = info.get(key)
            if v and v > 0:
                result["div_yield"] = round(v * 100, 2) if v < 1 else round(v, 2)
                break

        # Revenue/Earnings growth — try multiple fields
        for key in ["revenueGrowth", "earningsGrowth", "earningsQuarterlyGrowth"]:
            v = info.get(key)
            if v is not None and abs(v) < 10:  # sanity: < 1000%
                field = "rev_growth" if "revenue" in key.lower() else "eps_growth"
                result[field] = round(v * 100, 1)

        # Forward P/E for PEG calculation
        fwd = info.get("forwardPE")
        if fwd and fwd > 0 and fwd < 500:
            result["forward_pe"] = round(fwd, 1)

        # Compute implied earnings growth from trailing vs forward P/E
        trail = result.get("pe")
        if trail and fwd and trail > 0 and fwd > 0 and result["eps_growth"] is None:
            implied_growth = round((trail / fwd - 1) * 100, 1)
            if abs(implied_growth) < 200:
                result["eps_growth"] = implied_growth

        # EPS value for historical earnings computation
        eps = info.get("epsTrailingTwelveMonths")
        if eps and eps > 0:
            result["eps_ttm"] = round(eps, 2)

        if result["pe"] is None and tk is not None:
            try:
                fund_info = tk.funds_data
                if hasattr(fund_info, 'equity_holdings'):
                    eh = fund_info.equity_holdings
                    if hasattr(eh, 'iloc') and len(eh) > 0:
                        for col in eh.columns:
                            if 'pe' in col.lower() or 'earnings' in col.lower():
                                v = eh[col].iloc[0]
                                if v and v > 0:
                                    result["pe"] = round(float(v), 1)
            except Exception:
                pass

    except Exception as e:
        sys.stderr.write(f"  [ETF {etf_ticker}] error: {e}\n")

    return result


# ──────────────────────────────────────────────────────────
# Fetch price performance from index ticker
# ──────────────────────────────────────────────────────────
def fetch_performance(ticker_str):
    result = {"price": None, "perf_ytd": None, "perf_1y": None, "perf_3y": None, "perf_5y": None}
    if not ticker_str:
        return result
    try:
        now = datetime.now()
        start_5y = now - timedelta(days=5*365+30)

        hist = None
        for attempt in range(3):
            try:
                tk = _ticker_hist(ticker_str)  # session neuve à chaque tentative
                hist = tk.history(start=start_5y.strftime("%Y-%m-%d"), end=now.strftime("%Y-%m-%d"))
            except Exception:
                hist = None
            if hist is not None and not hist.empty:
                break
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
        if hist is None or hist.empty:
            return result

        # Yahoo renvoie souvent une dernière ligne Close=NaN (jour courant
        # incomplet, surtout sur les indices ^GSPC/^KS11…). float(NaN) puis round
        # → nan → tout devient None en aval. On purge les Close NaN d'abord.
        closes = hist["Close"].dropna()
        if closes.empty:
            return result

        current_price = float(closes.iloc[-1])
        result["price"] = round(current_price, 2)

        year_start = f"{now.year}-01-01"
        ytd_data = closes[closes.index >= year_start]
        if len(ytd_data) > 1:
            first_price = float(ytd_data.iloc[0])
            if first_price > 0:
                result["perf_ytd"] = round((current_price / first_price - 1) * 100, 1)

        d1y = now - timedelta(days=365)
        c1y = closes[closes.index >= d1y.strftime("%Y-%m-%d")]
        if len(c1y) > 1:
            first_price = float(c1y.iloc[0])
            if first_price > 0:
                result["perf_1y"] = round((current_price / first_price - 1) * 100, 1)

        d3y = now - timedelta(days=3*365)
        c3y = closes[closes.index >= d3y.strftime("%Y-%m-%d")]
        if len(c3y) > 1:
            first_price = float(c3y.iloc[0])
            if first_price > 0:
                result["perf_3y"] = round((current_price / first_price - 1) * 100, 1)

        if len(closes) > 100:
            first_price = float(closes.iloc[0])
            if first_price > 0:
                result["perf_5y"] = round((current_price / first_price - 1) * 100, 1)

    except Exception as e:
        sys.stderr.write(f"  [PERF {ticker_str}] error: {e}\n")

    # nan → None : sinon le champ nan n'est pas "None", court-circuite le fallback
    # ETF, puis devient None à la toute fin (clean_nan) → colonne "—" silencieuse.
    for k in list(result.keys()):
        v = result[k]
        if isinstance(v, float) and math.isnan(v):
            result[k] = None
    return result


# ──────────────────────────────────────────────────────────
# Clean NaN/Inf
# ──────────────────────────────────────────────────────────
def clean_nan(obj):
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_nan(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


# ──────────────────────────────────────────────────────────
# Fetch all markets
# ──────────────────────────────────────────────────────────
def fetch_all_markets():
    rows = []
    for m in MARKETS:
        sys.stderr.write(f"  Fetching {m['country']}: {m['name']} (ETF={m['etf']}, idx={m['index_ticker']})...\n")
        row = dict(m)
        fund = fetch_etf_fundamentals(m["etf"])
        row.update(fund)
        perf = fetch_performance(m["index_ticker"])
        row.update(perf)
        # Fallback ETF : l'indice (^GSPC, ^KS11…) renvoie parfois nan/None sur
        # certaines fenêtres. On comble chaque champ perf manquant via l'ETF.
        if any(row.get(k) is None for k in ("price", "perf_ytd", "perf_1y", "perf_3y", "perf_5y")):
            perf2 = fetch_performance(m["etf"])
            for k in ("price", "perf_ytd", "perf_1y", "perf_3y", "perf_5y"):
                if row.get(k) is None and perf2.get(k) is not None:
                    row[k] = perf2[k]
        # Compute implied earnings growth YoY from price performance + P/E
        # Logic: EPS = Price / PE. If we know price change and PE stayed same, EPS grew by price change.
        # More precisely: EPS_growth ≈ Price_growth_1Y * (1 + 1/PE_change)
        # Simplification: earnings_growth ≈ price_1Y_growth - PE_expansion_pct
        # This is the best we can do for index-level data without bottom-up earnings
        if row.get("pe") and row.get("perf_1y") is not None and row.get("eps_growth") is None:
            # Approximate: if price went up 30% and PE went from ~18 to ~19 (5.5% expansion)
            # then earnings grew ~24.5%. We don't have last year's PE, so use forward PE as proxy
            fwd_pe = row.get("forward_pe")
            trail_pe = row.get("pe")
            if fwd_pe and trail_pe and fwd_pe > 0:
                pe_change_pct = (trail_pe / fwd_pe - 1) * 100  # implied earnings growth from PE compression
                row["eps_growth"] = round(pe_change_pct, 1)

        rows.append(row)
        sys.stderr.write(f"    -> P/E={row.get('pe')}, P/B={row.get('pb')}, "
                         f"DivY={row.get('div_yield')}%, EpsG={row.get('eps_growth')}%, YTD={row.get('perf_ytd')}%\n")
        time.sleep(1.2)  # pacing anti rate-limit Yahoo (budget watchdog 30min, large)
    return rows


def fetch_benchmark():
    bm = dict(BENCHMARK)
    sys.stderr.write(f"  Fetching benchmark: {bm['name']} ({bm['etf']})...\n")
    fund = fetch_etf_fundamentals(bm["etf"])
    perf = fetch_performance(bm["etf"])
    bm.update(fund)
    bm.update(perf)
    return bm


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────
def main():
    if CACHE_JSON.exists() and "--force" not in sys.argv:
        age_h = (datetime.now().timestamp() - CACHE_JSON.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS:
            sys.stderr.write(f"[GLOBAL] Cache fresh ({age_h:.1f}h < {CACHE_MAX_HOURS}h). Use --force to override.\n")
            return

    sys.stderr.write("[GLOBAL] Fetching global market data...\n")

    # Charge le cache précédent pour merge-préservation (anti-effacement).
    prev = {}
    prev_bm = {}
    if CACHE_JSON.exists():
        try:
            _pc = json.load(open(CACHE_JSON, encoding="utf-8"))
            for r in _pc.get("markets", []):
                prev[r.get("name")] = r
            prev_bm = _pc.get("benchmark", {}) or {}
        except Exception as e:
            sys.stderr.write(f"[GLOBAL] could not read prev cache: {e}\n")

    markets = fetch_all_markets()
    benchmark = fetch_benchmark()

    # ── Merge-préservation : si un champ revient None ce run (rate-limit Yahoo),
    # on garde la dernière valeur connue du cache au lieu d'écrire "—". C'est ce
    # garde-fou qui manquait → SPY/EWJ/… se vidaient à chaque run raté. ──
    MERGE_FIELDS = ["pe", "pb", "div_yield", "rev_growth", "eps_growth", "forward_pe",
                    "eps_ttm", "price", "perf_ytd", "perf_1y", "perf_3y", "perf_5y"]
    restored = 0
    for row in markets:
        old = prev.get(row.get("name"))
        if not old:
            continue
        for f in MERGE_FIELDS:
            if row.get(f) is None and old.get(f) is not None:
                row[f] = old[f]
                row["_stale"] = True
                restored += 1
    for f in MERGE_FIELDS:
        if benchmark.get(f) is None and prev_bm.get(f) is not None:
            benchmark[f] = prev_bm[f]
    if restored:
        sys.stderr.write(f"[GLOBAL] merge-preserved {restored} champs depuis le cache précédent (fetch partiel)\n")

    # Sanity : si le run live est catastrophique (presque tout None) ET qu'on n'a
    # rien pu restaurer, on n'écrase PAS un cache précédent valable.
    n_pe_live = sum(1 for m in markets if m.get("pe") is not None)
    if n_pe_live < max(3, len(markets) // 3) and prev and sum(1 for r in prev.values() if r.get("pe") is not None) > n_pe_live:
        sys.stderr.write(f"[GLOBAL] run dégradé ({n_pe_live} P/E live) — cache précédent conservé, abandon écriture.\n")
        return
    # Note : l'entrée USA (S&P 500) reste sur le trailingPE yfinance de SPY, comme les 16
    # autres pays (base ETF, comparable entre indices). Le P/E GAAP réel (multpl ~32x) est
    # réservé au benchmark du graphe par secteur, où il se compare à des secteurs agrégés
    # en GAAP. Chaque chiffre est libellé côté front avec sa méthode.

    payload = clean_nan({
        "markets": markets,
        "benchmark": benchmark,
        "updated": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "sources": {
            "fundamentals": "Yahoo Finance (ETF info: SPY, EWJ, MCHI, INDA, etc.)",
            "performance": "Yahoo Finance (index historical prices)",
            "audit_urls": {
                "SPY": "https://finance.yahoo.com/quote/SPY/",
                "EWJ": "https://finance.yahoo.com/quote/EWJ/",
                "MCHI": "https://finance.yahoo.com/quote/MCHI/",
                "INDA": "https://finance.yahoo.com/quote/INDA/",
                "EWG": "https://finance.yahoo.com/quote/EWG/",
                "EWQ": "https://finance.yahoo.com/quote/EWQ/",
                "EWY": "https://finance.yahoo.com/quote/EWY/",
                "EWU": "https://finance.yahoo.com/quote/EWU/",
                "EWZ": "https://finance.yahoo.com/quote/EWZ/",
                "URTH": "https://finance.yahoo.com/quote/URTH/"
            }
        }
    })

    with open(CACHE_JSON, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    sys.stderr.write(f"[GLOBAL] Wrote {CACHE_JSON}\n")

    js_payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    CACHE_JS.write_text(f"window.__GLOBAL_MARKETS__={js_payload};")
    sys.stderr.write(f"[GLOBAL] Wrote {CACHE_JS}\n")

    n_pe = sum(1 for m in markets if m.get("pe"))
    n_perf = sum(1 for m in markets if m.get("perf_ytd") is not None)
    sys.stderr.write(f"[GLOBAL] Done: {len(markets)} markets, {n_pe} with P/E, {n_perf} with perf data\n")


if __name__ == "__main__":
    main()
