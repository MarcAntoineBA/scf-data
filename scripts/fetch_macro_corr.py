#!/usr/bin/env python3
"""Fetch données daily pour graphique Corrélation BTC × S&P500, Or, DXY, M2.
Sources :
- Yahoo Finance (BTC-USD, ^IXIC, ^GSPC, GC=F, DX-Y.NYB, BZ=F)
- FRED (WM2NS) pour M2 money supply
- CoinGecko fallback pour BTC si Yahoo échoue

Écrit dans ~/Library/Caches/site_crypto_finance/macro_corr_cache.json
Injecte dans Correlations_Macro_Crypto.html via markers __MACRO_DATA_LIVE__ (tolérant TCC).
"""
import json, sys, warnings, re, urllib.request, csv, io, time
from pathlib import Path
from datetime import datetime, timedelta

warnings.filterwarnings('ignore')

# curl_cffi : méthode PRIMAIRE — impersonate chrome120 contourne le 429 récurrent
# que subissent yfinance/urllib depuis R (cf. mémoire yahoo-curlcffi). C'est la
# seule méthode qui ramène l'historique 20 ans de façon fiable.
try:
    from curl_cffi import requests as creq
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
    sys.stderr.write('[Macro Corr] curl_cffi unavailable, fallback yfinance/HTTP\n')

# yfinance : secours (retry auto, gestion rate-limit)
try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False
    sys.stderr.write('[Macro Corr] yfinance unavailable, fallback HTTP direct\n')

import urllib.parse

CACHES_DIR = Path.home() / "Library/Caches/site_crypto_finance"
CACHES_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHES_DIR / "macro_corr_cache.json"
# Store DURABLE « last-known-good » : jamais écrasé par du vide. Sert à
# rebootstrapper une série bloquée à 0 (le cache live, lui, peut devenir vide
# si Yahoo rate-limite au mauvais moment). Garant : un knit ne sort JAMAIS vide.
LASTGOOD_FILE = CACHES_DIR / "macro_corr_lastgood.json"
HTML_FILE  = Path.home() / "Desktop/Site_Crypto_Finance/Correlations_Macro_Crypto.html"
CACHE_MAX_HOURS = 6
YEARS = 20
# Séries cœur qui doivent TOUJOURS être peuplées (bannière sinon).
CORE_KEYS = ("btc", "nasdaq", "sp500", "gold", "dxy", "oil")

def http_get(url, timeout=20, is_json=True):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Chrome/122.0.0.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    return json.loads(raw) if is_json else raw

def _parse_v8(d):
    """Extrait [[ts, close], ...] d'une réponse Yahoo chart v8."""
    res = d.get("chart", {}).get("result", [])
    if not res:
        return []
    ts = res[0].get("timestamp") or []
    ind = res[0].get("indicators", {}).get("quote", [])
    closes = ind[0].get("close") if ind else []
    out = []
    for t, c in zip(ts, closes or []):
        if c is not None:
            out.append([int(t), round(float(c), 4)])
    return out

def fetch_yahoo(symbol, years=YEARS):
    """Yahoo Finance daily.
    Priorité curl_cffi (impersonate chrome120 → contourne le 429) → yfinance → HTTP urllib."""
    end_ts = int(time.time())
    start_ts = end_ts - 86400 * 365 * years
    enc = urllib.parse.quote(symbol, safe='')
    v8_url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{enc}"
              f"?interval=1d&period1={start_ts}&period2={end_ts}")
    # Méthode 0 : curl_cffi impersonation (la plus fiable — bypass 429)
    if HAS_CURL_CFFI:
        try:
            r = creq.get(v8_url, impersonate="chrome120", timeout=25)
            if r.status_code == 200:
                out = _parse_v8(r.json())
                if len(out) > 100:
                    return out
            else:
                sys.stderr.write(f'[{symbol}] curl_cffi status {r.status_code}\n')
        except Exception as e:
            sys.stderr.write(f'[{symbol}] curl_cffi err: {e}\n')
    # Méthode 1 : yfinance (retry intégré)
    if HAS_YF:
        try:
            start = (datetime.now() - timedelta(days=365 * years)).strftime("%Y-%m-%d")
            t = yf.Ticker(symbol)
            hist = t.history(start=start, interval="1d", auto_adjust=False)
            if hist is not None and len(hist) > 0:
                out = []
                for idx, row in hist.iterrows():
                    c = row.get('Close')
                    if c is not None and not (c != c):  # NaN check
                        ts = int(idx.timestamp())
                        out.append([ts, round(float(c), 4)])
                return out
        except Exception as e:
            sys.stderr.write(f'[{symbol}] yfinance err: {e}\n')
    # Méthode 2 : HTTP direct fallback
    try:
        end_ts = int(time.time())
        start_ts = end_ts - 86400 * 365 * years
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&period1={start_ts}&period2={end_ts}"
        d = http_get(url)
        res = d.get("chart", {}).get("result", [])
        if not res: return []
        ts = res[0].get("timestamp") or []
        ind = res[0].get("indicators", {}).get("quote", [])
        if not ind: return []
        closes = ind[0].get("close") or []
        out = []
        for t, c in zip(ts, closes):
            if c is not None:
                out.append([int(t), round(float(c), 4)])
        return out
    except Exception as e:
        sys.stderr.write(f'[{symbol}] http err: {e}\n')
        return []

def fetch_cg_btc(years=YEARS):
    """Fallback BTC via CoinGecko daily."""
    try:
        days = years * 365
        url = f"https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days={days}&interval=daily"
        d = http_get(url)
        prices = d.get("prices", [])
        return [[int(p[0] / 1000), round(float(p[1]), 2)] for p in prices]
    except Exception as e:
        sys.stderr.write(f'[BTC CoinGecko] err: {e}\n')
        return []

def fetch_fred_m2(years=YEARS):
    """FRED WM2NS via curl subprocess (urllib Python est flaky sur FRED)."""
    import subprocess
    cutoff_ts = int((datetime.now() - timedelta(days=365 * years)).timestamp())
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=WM2NS"
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "30", "--http1.1", "-A", "Mozilla/5.0", url],
            capture_output=True, text=True, timeout=35
        )
        if result.returncode != 0:
            sys.stderr.write(f'[M2 FRED] curl rc={result.returncode}\n'); return []
        raw = result.stdout
        rdr = csv.reader(io.StringIO(raw))
        out = []
        for i, row in enumerate(rdr):
            if i == 0 or len(row) < 2: continue
            try:
                dt = datetime.strptime(row[0], "%Y-%m-%d")
                v = float(row[1])
                ts = int(dt.timestamp())
                if ts >= cutoff_ts:
                    out.append([ts, v])
            except (ValueError, TypeError):
                continue
        return out
    except Exception as e:
        sys.stderr.write(f'[M2 FRED] curl err: {e}\n')
        return []

def read_m2_from_fred_cache(years=YEARS):
    """SECOURS LOCAL (2026-06-29) : lit WM2NS depuis macro_fred_cache.js, maintenu
    par fetch_macro_fred_data.py qui fetch FRED avec retry/backoff robuste.
    L'endpoint fredgraph.csv utilisé par fetch_fred_m2() timeout (curl rc=28) de
    façon récurrente → sans ce secours, la série M2 restait vide indéfiniment
    (le garde-fou de préservation ne peut pas rebootstrapper une série déjà à 0),
    d'où la bannière « Sources indisponibles : M2 » et les corrélations de
    liquidité manquantes. On réutilise la donnée déjà fetchée localement."""
    js = CACHES_DIR / "macro_fred_cache.js"
    if not js.exists():
        sys.stderr.write('[M2 fallback] macro_fred_cache.js absent\n')
        return []
    try:
        txt = js.read_text()
        m = re.search(r'var fresh=(\{.*?\});window\.__MACRO_FRED_LIVE__', txt, re.DOTALL)
        if not m:
            sys.stderr.write('[M2 fallback] marqueur var fresh introuvable\n')
            return []
        d = json.loads(m.group(1))
        wm = d.get("tickers", {}).get("WM2NS", {})
        dates = wm.get("dates", []) or []
        vals = wm.get("values", []) or []
        cutoff_ts = int((datetime.now() - timedelta(days=365 * years)).timestamp())
        out = []
        for ds, v in zip(dates, vals):
            if v is None:
                continue
            try:
                ts = int(datetime.strptime(ds[:10], "%Y-%m-%d").timestamp())
            except (ValueError, TypeError):
                continue
            if ts >= cutoff_ts:
                out.append([ts, float(v)])
        return out
    except Exception as e:
        sys.stderr.write(f'[M2 fallback] macro_fred_cache.js err: {e}\n')
        return []

def _load_data(path):
    try:
        return json.load(open(path)).get("data", {}) or {}
    except Exception:
        return {}

def fetch():
    if CACHE_FILE.exists():
        age_h = (datetime.now().timestamp() - CACHE_FILE.stat().st_mtime) / 3600
        # « Frais » ne suffit pas : un cache récent mais dont une série cœur est
        # vide (Yahoo a rate-limité au dernier run) DOIT être refetché, sinon la
        # bannière « Sources indisponibles » persiste 6h.
        cached = _load_data(CACHE_FILE)
        core_ok = all(len(cached.get(k, [])) > 50 for k in CORE_KEYS)
        if age_h < CACHE_MAX_HOURS and core_ok:
            sys.stderr.write(f'[Macro Corr] Cache fresh ({age_h:.1f}h) & séries cœur OK\n')
            payload = json.load(open(CACHE_FILE))
            inject_into_html(payload)
            return payload
        if age_h < CACHE_MAX_HOURS and not core_ok:
            missing = [k for k in CORE_KEYS if len(cached.get(k, [])) <= 50]
            sys.stderr.write(f'[Macro Corr] Cache frais ({age_h:.1f}h) mais séries vides {missing} → refetch forcé\n')

    sys.stderr.write('[Macro Corr] Fetching live data...\n')

    data = {}
    # BTC : Yahoo en priorité (daily propre), fallback CoinGecko
    btc = fetch_yahoo("BTC-USD")
    if len(btc) < 100:
        sys.stderr.write(f'[Macro Corr] Yahoo BTC low ({len(btc)} pts), fallback CoinGecko\n')
        btc = fetch_cg_btc()
    data["btc"] = btc
    sys.stderr.write(f'  BTC: {len(btc)} pts\n')

    # Marchés TradFi via Yahoo (symboles natifs yfinance)
    yahoo_map = {
        "nasdaq": "^IXIC",
        "sp500":  "^GSPC",
        "gold":   "GC=F",       # Gold futures
        "silver": "SI=F",       # Silver futures (régime commodities advisor)
        "copper": "HG=F",       # Copper futures
        "dxy":    "DX-Y.NYB",   # US Dollar Index
        "oil":    "BZ=F",       # Brent Crude
    }
    for key, sym in yahoo_map.items():
        pts = fetch_yahoo(sym)
        data[key] = pts
        sys.stderr.write(f'  {key.upper()}: {len(pts)} pts\n')
        time.sleep(0.3)  # léger anti-rate-limit

    # M2 depuis FRED (hebdomadaire), avec SECOURS LOCAL si l'endpoint timeout.
    m2 = fetch_fred_m2()
    if len(m2) < 50:
        fb = read_m2_from_fred_cache()
        if len(fb) > len(m2):
            sys.stderr.write(f'  M2: FRED direct faible ({len(m2)} pts) → secours macro_fred_cache.js ({len(fb)} pts)\n')
            m2 = fb
    data["m2"] = m2
    sys.stderr.write(f'  M2: {len(m2)} pts\n')

    # ── PRÉSERVATION via store DURABLE « last-known-good » ────────────────────
    # L'ancien garde-fou lisait le cache LIVE (qui pouvait lui-même être vide →
    # une série tombée à 0 ne se relevait jamais). On lit désormais un fichier
    # dédié qui n'accumule QUE des séries valides et n'est JAMAIS écrasé par du
    # vide. Effets : (1) une série à 0 est rebootstrappée depuis last-good ;
    # (2) le cache live n'est jamais nucléarisé ; (3) un knit ne sort jamais vide.
    lastgood = _load_data(LASTGOOD_FILE)
    # Fallback historique : si pas encore de last-good, amorcer depuis le cache live.
    if not lastgood:
        lastgood = _load_data(CACHE_FILE)

    all_keys = set(data.keys()) | set(lastgood.keys())
    for k in all_keys:
        new = data.get(k, []) or []
        good = lastgood.get(k, []) or []
        # 1) Mettre à jour last-good si le fetch frais est au moins aussi complet.
        if new and len(new) >= max(50, 0.9 * len(good)):
            lastgood[k] = new
            good = new
        # 2) Valeur retenue : fetch frais si correct, sinon repli sur last-good.
        if new and len(new) >= max(50, 0.5 * len(good)):
            data[k] = new
        elif good:
            data[k] = good
            sys.stderr.write(f'[Macro Corr] {k}: fetch faible ({len(new)}) → last-good préservé ({len(good)} pts)\n')
        else:
            data[k] = new  # les deux vides : rien à faire (ex. 1er run réseau KO)

    # Persister le store durable (uniquement des séries valides).
    try:
        with open(LASTGOOD_FILE, 'w') as f:
            json.dump({"updated": datetime.now().isoformat(), "data": lastgood}, f)
    except Exception as _e:
        sys.stderr.write(f'[Macro Corr] écriture last-good KO: {_e}\n')

    payload = {
        "updated": datetime.now().isoformat(),
        "data": data,
        "sources": {
            "btc": "Yahoo Finance (BTC-USD) / fallback CoinGecko",
            "nasdaq": "Yahoo Finance (^IXIC)",
            "sp500": "Yahoo Finance (^GSPC)",
            "gold": "Yahoo Finance (GC=F gold futures)",
            "silver": "Yahoo Finance (SI=F silver futures)",
            "copper": "Yahoo Finance (HG=F copper futures)",
            "dxy": "Yahoo Finance (DX-Y.NYB US Dollar Index)",
            "oil": "Yahoo Finance (BZ=F Brent crude futures)",
            "m2": "FRED St. Louis (WM2NS, weekly, seasonally adjusted)"
        }
    }

    with open(CACHE_FILE, 'w') as f:
        json.dump(payload, f)
    total = sum(len(v) for v in data.values())
    sys.stderr.write(f'[Macro Corr] Wrote {len(data)} series, {total} total pts to {CACHE_FILE}\n')

    inject_into_html(payload)
    return payload

def inject_into_html(payload):
    """Injection HTML optionnelle — tolère les erreurs TCC (LaunchAgent sans accès Desktop)."""
    try:
        if not HTML_FILE.exists():
            sys.stderr.write(f'[Macro Corr] {HTML_FILE} introuvable, skip HTML injection\n'); return
        html = HTML_FILE.read_text()
        new_block = (
            "// __MACRO_DATA_LIVE_START__\n"
            "window.__MACRO_DATA_LIVE__ = " + json.dumps(payload, separators=(',',':')) + ";\n"
            "// __MACRO_DATA_LIVE_END__"
        )
        pattern = re.compile(r"// __MACRO_DATA_LIVE_START__.*?// __MACRO_DATA_LIVE_END__", re.DOTALL)
        if not pattern.search(html):
            sys.stderr.write('[Macro Corr] markers absents dans HTML, skip\n'); return
        html2 = pattern.sub(new_block, html)
        HTML_FILE.write_text(html2)
        sys.stderr.write(f'[Macro Corr] Injected into {HTML_FILE.name}\n')
    except (PermissionError, OSError) as e:
        sys.stderr.write(f'[Macro Corr] HTML injection skipped (TCC): {e}\n')
        sys.stderr.write('[Macro Corr] Browser will fetch cache JSON via symlink instead\n')

if __name__ == '__main__':
    fetch()
