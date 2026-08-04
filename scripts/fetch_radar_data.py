#!/usr/bin/env python3
"""Fetch ALL radar signal data (Binance, CoinGecko, FNG, stablecoins, BTC hist).

Ecrit dans ~/Library/Caches/site_crypto_finance/ :
  - radar_cache.json : payload brut
  - radar_cache.js   : surcharge les window.__BINANCE_*__, __CG_*__, etc.
                        chargé par index.html via <script src> avec cache-bust.

Lancé par launchd (scf.radardata) toutes les 4h.
"""
import requests, json, sys, time, re
from pathlib import Path
from datetime import datetime

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "radar_cache.json"
CACHE_JS   = CACHE_DIR / "radar_cache.js"
CACHE_MAX_HOURS = 4

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0"
HEADERS = {"User-Agent": UA}

def log(msg):
    sys.stderr.write(f"[RADAR] {msg}\n")

# ──────────────────────────────────────────────────────────
# Fetchers — Binance Futures
# ──────────────────────────────────────────────────────────
def fetch_json(url, timeout=15, retries=1, backoff=3):
    """Generic fetch returning parsed JSON or None. Retries on 429/5xx."""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 or r.status_code >= 500:
                wait = backoff * (attempt + 1)
                log(f"  {r.status_code} on {url[:50]}... retry in {wait}s ({attempt+1}/{retries+1})")
                time.sleep(wait)
                continue
            log(f"  {r.status_code} on {url[:50]}...")
            return None
        except Exception as e:
            log(f"  FAIL {url[:50]}... → {e}")
    return None

def fetch_binance_fr(symbol="BTCUSDT"):
    return fetch_json(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}")

def fetch_binance_oi(symbol="BTCUSDT"):
    return fetch_json(f"https://fapi.binance.com/futures/data/openInterestHist?symbol={symbol}&period=1h&limit=48")

def fetch_binance_ls():
    return fetch_json("https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=4h&limit=12")

def fetch_binance_taker():
    return fetch_json("https://fapi.binance.com/futures/data/takerlongshortRatio?symbol=BTCUSDT&period=1h&limit=24")

# ──────────────────────────────────────────────────────────
# Fetchers — CoinGecko
# ──────────────────────────────────────────────────────────
def fetch_cg_global():
    return fetch_json("https://api.coingecko.com/api/v3/global")

def fetch_cg_coins():
    d = fetch_json("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=100&sparkline=false&price_change_percentage=1h%2C24h%2C7d%2C30d%2C1y")
    if d and isinstance(d, list):
        return [c for c in d if c.get("id") != "noon"]
    return None

# ──────────────────────────────────────────────────────────
# Fetchers — Stablecoins (DefiLlama) — replicate R processing
# ──────────────────────────────────────────────────────────
def fetch_stablecoins():
    raw = fetch_json("https://stablecoins.llama.fi/stablecoins?includePrices=true")
    if not raw or "peggedAssets" not in raw:
        return None
    total_now = 0.0
    total_30d = 0.0
    top = []
    for s in raw["peggedAssets"]:
        mc_now = float((s.get("circulating") or {}).get("peggedUSD", 0) or 0)
        mc_30d = float((s.get("circulatingPrevMonth") or {}).get("peggedUSD", 0) or 0)
        if mc_now > 1e8:
            total_now += mc_now
            total_30d += mc_30d
            top.append({
                "name": s.get("name", ""),
                "symbol": s.get("symbol", ""),
                "mcap": round(mc_now / 1e9, 2),
                "mcap_30d": round(mc_30d / 1e9, 2),
            })
    change_30d = round((total_now - total_30d) / total_30d * 100, 2) if total_30d > 0 else 0
    return {
        "totalNow": round(total_now / 1e9, 2),
        "total30d": round(total_30d / 1e9, 2),
        "change30d": change_30d,
        "top": top,
    }

# ──────────────────────────────────────────────────────────
# Fetchers — DeFi TVL
# ──────────────────────────────────────────────────────────
def fetch_defi_tvl():
    d = fetch_json("https://api.llama.fi/v2/historicalChainTvl")
    if d and isinstance(d, list) and len(d) > 60:
        return d[-60:]
    return d

# ──────────────────────────────────────────────────────────
# Fetchers — Fear & Greed (merged CMC + alternative.me + BTC price)
# ──────────────────────────────────────────────────────────
def fetch_btc_history(now_ts):
    """BTC daily close 2018+ via curl_cffi (Yahoo bloque requests stdlib → 429, cf
    mémoire project_yahoo_curl_cffi_required). CRITIQUE pour __BTC_HIST__ → signaux
    radar LONG TERME (200MA, valorisation). Fallback : cache persistant.
    Retourne [{t:epoch_s, p:float}, ...]."""
    cache = CACHE_DIR / "btc_hist_radar.json"
    try:
        from curl_cffi import requests as cr
        s = cr.Session(impersonate="chrome120")
        r = s.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?interval=1d&period1=1514764800&period2={now_ts}",
            timeout=30,
        )
        res = r.json()["chart"]["result"][0]
        ts = res["timestamp"]
        cl = res["indicators"]["quote"][0]["close"]
        pts = [{"t": int(t), "p": round(c, 2)} for t, c in zip(ts, cl) if c is not None]
        if len(pts) > 100:
            try:
                cache.write_text(json.dumps(pts))
            except Exception:
                pass
            return pts
        raise ValueError("too few points")
    except Exception as e:
        log(f"  BTC curl_cffi échec ({e}) → fallback cache")
        try:
            return json.loads(cache.read_text())
        except Exception:
            return []


def fetch_fng_merged():
    """Fear & Greed = SOURCE UNIQUE CMC (coinmarketcap), choix user 2026-05-29 pour
    concordance totale du site. Avant : merge alternative.me (2018+) + CMC (2023+) ->
    2 methodologies, valeurs divergentes (alt.me ~23 vs CMC ~33). CMC couvre ~2023+.
    Retourne (fng_data, btc_prices). btc_prices (Yahoo) reste fourni pour __BTC_HIST__."""
    now_ts = int(time.time())

    # 1) CMC Fear & Greed (btcPrice natif inclus)
    cmc_raw = fetch_json(f"https://api.coinmarketcap.com/data-api/v3/fear-greed/chart?start=1514764800&end={now_ts}")
    merged = []
    if cmc_raw and "data" in cmc_raw and "dataList" in cmc_raw["data"]:
        for item in cmc_raw["data"]["dataList"]:
            merged.append({
                "score": int(item.get("score", 0)),
                "name": item.get("name", ""),
                "timestamp": str(item.get("timestamp", 0)),
                "btcPrice": str(item.get("btcPrice", "0")),
                "btcVolume": str(item.get("btcVolume", "0")),
            })

    # 2) BTC price history via curl_cffi (Yahoo 429 sur requests stdlib). CRITIQUE :
    #    sans __BTC_HIST__, les signaux radar LONG TERME (200MA, valorisation) ne firent
    #    PAS sur l'Accueil → divergence vs Indicateur. curl_cffi = concordance.
    btc_prices = fetch_btc_history(now_ts)
    log(f"  BTC history: {len(btc_prices)} points ({'OK' if len(btc_prices) > 365 else 'WARN: too few for valuation!'})")

    if not merged:
        return None, (btc_prices or None)
    fng_data = {"data": {"dataList": merged}}
    return fng_data, btc_prices

# ──────────────────────────────────────────────────────────
# Fetchers — Altcoin Season
# ──────────────────────────────────────────────────────────
def fetch_altseason():
    now_ts = int(time.time())
    return fetch_json(f"https://api.coinmarketcap.com/data-api/v3/altcoin-season/chart?start=1514764800&end={now_ts}")

# ──────────────────────────────────────────────────────────
# Fetchers — Top 100 US Stocks (Yahoo Finance)
# ──────────────────────────────────────────────────────────
STOCK_TICKERS = [
    "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","SPCX","BRK-B","AVGO","LLY",
    "JPM","V","WMT","MA","UNH","XOM","COST","HD","PG","JNJ",
    "NFLX","ABBV","CRM","BAC","ORCL","CVX","MRK","KO","AMD","PEP",
    "TMO","LIN","ADBE","ACN","MCD","CSCO","ABT","WFC","IBM","PM",
    "GE","NOW","ISRG","INTU","CAT","DIS","TMUS","TXN","QCOM","AMGN",
    "DHR","SPGI","PFE","AXP","BKNG","RTX","UBER","LOW","BLK","HON",
    "UNP","DE","GS","NEE","AMAT","ADP","SYK","ARM","PLTR","ETN",
    "CB","LRCX","GILD","REGN","VRTX","ADI","BSX","PANW","SCHW","MMC",
    "FI","MU","KLAC","SNPS","CDNS","SO","CME","CI","SHW","APP",
    "CEG","PGR","ICE","VZ","T","MSI","CRWD","MCO","APH","WELL",
]

def fetch_stock_data():
    """Fetch top 100 US stock prices & changes via Yahoo spark API, market caps via yfinance."""
    all_data = {}
    batch_size = 15
    for i in range(0, len(STOCK_TICKERS), batch_size):
        batch = STOCK_TICKERS[i:i + batch_size]
        syms = ",".join(batch)
        url = f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={syms}&range=ytd&interval=1d"
        data = fetch_json(url, timeout=15, retries=2, backoff=3)
        if not data:
            continue
        for sym in batch:
            obj = data.get(sym)
            if not obj:
                continue
            closes = obj.get("close", [])
            if not closes:
                # Try nested spark response format
                try:
                    resp = obj["response"][0]
                    closes = resp["indicators"]["quote"][0]["close"]
                except (KeyError, IndexError, TypeError):
                    continue
            closes = [c for c in closes if c is not None]
            prev_close = obj.get("chartPreviousClose")  # Dec 31 close for YTD range
            if len(closes) < 2:
                continue
            cur = closes[-1]
            n = len(closes)
            d1 = round((cur / closes[n - 2] - 1) * 100, 2)
            d5 = round((cur / closes[max(0, n - 6)] - 1) * 100, 2) if n >= 6 else d1
            m1 = round((cur / closes[max(0, n - 22)] - 1) * 100, 2) if n >= 23 else round((cur / closes[0] - 1) * 100, 2)
            ytd = round((cur / prev_close - 1) * 100, 2) if prev_close and prev_close > 0 else round((cur / closes[0] - 1) * 100, 2)
            all_data[sym] = {"t": sym, "p": round(cur, 2), "d1": d1, "d5": d5, "m1": m1, "ytd": ytd}
        if i + batch_size < len(STOCK_TICKERS):
            time.sleep(0.5)

    # Market caps via yfinance (optional, best-effort)
    try:
        import yfinance as yf
        log(f"  Fetching market caps for {len(all_data)} stocks via yfinance...")
        for sym in list(all_data.keys()):
            try:
                t = yf.Ticker(sym)
                mc = t.fast_info.get("marketCap", 0)
                if mc and mc > 0:
                    all_data[sym]["mc"] = round(mc / 1e9)
            except Exception:
                pass
    except ImportError:
        log("  yfinance not installed — skipping market caps")

    results = list(all_data.values())
    log(f"  Got {len(results)}/{len(STOCK_TICKERS)} stocks")
    return results if results else None

# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────
def main():
    if CACHE_FILE.exists() and CACHE_JS.exists() and "--force" not in sys.argv:
        age_h = (datetime.now().timestamp() - CACHE_FILE.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS:
            log(f"Cache fresh ({age_h:.1f}h)")
            return

    log("=== Fetching radar signals ===")

    # Binance
    log("Binance funding BTC...")
    fr_btc = fetch_binance_fr("BTCUSDT")
    log("Binance funding ETH...")
    fr_eth = fetch_binance_fr("ETHUSDT")
    log("Binance OI BTC...")
    oi_btc = fetch_binance_oi("BTCUSDT")
    log("Binance OI ETH...")
    oi_eth = fetch_binance_oi("ETHUSDT")
    log("Binance L/S ratio...")
    ls = fetch_binance_ls()
    log("Binance taker ratio...")
    taker = fetch_binance_taker()

    # CoinGecko (sleep between to avoid rate limit)
    log("CoinGecko global...")
    cg_global = fetch_cg_global()
    time.sleep(2)
    log("CoinGecko top 100...")
    cg_coins = fetch_cg_coins()
    time.sleep(2)

    # Stablecoins + TVL
    log("Stablecoins (DefiLlama)...")
    stables = fetch_stablecoins()
    log("DeFi TVL...")
    tvl = fetch_defi_tvl()

    # Fear & Greed + BTC history
    log("Fear & Greed (CMC + alternative.me + Yahoo BTC)...")
    fng_data, btc_hist = fetch_fng_merged()

    # Altseason
    log("Altcoin Season (CMC)...")
    altseason = fetch_altseason()

    # Top 100 US Stocks
    log("Top 100 US Stocks (Yahoo Finance)...")
    stock_data = fetch_stock_data()

    # Ecosystem map live market caps
    log("Ecosystem market caps (yfinance)...")
    eco_mcap = {}
    eco_tickers = {
        "NVDA":"nvidia","MSFT":"azure","AAPL":"apple","GOOGL":"gcp","AMZN":"aws",
        "META":"meta","TSLA":"tesla","BABA":"alibaba","0700.HK":"tencent",
        "BIDU":"baidu","1810.HK":"xiaomi","ASML":"asml","000660.KS":"skhynix",
        "005930.KS":"samsung","TSM":"tsmc","AMD":"amd","AVGO":"broadcom",
        "INTC":"intel","ORCL":"oracle","9984.T":"softbank","CRWV":"coreweave",
        "PLTR":"palantir","ARM":"arm","QCOM":"qualcomm","MRVL":"marvell","0981.HK":"smic",
    }
    # Currency conversion for non-USD tickers
    fx_div = {"000660.KS":1380,"005930.KS":1380,"9984.T":155,"0700.HK":7.8,"1810.HK":7.8,"0981.HK":7.8}
    try:
        import yfinance as yf
        for tk, nid in eco_tickers.items():
            try:
                t = yf.Ticker(tk)
                mc = t.fast_info.get("marketCap", 0)
                if mc and mc > 0:
                    mc_usd = mc / fx_div.get(tk, 1)
                    eco_mcap[nid] = round(mc_usd / 1e9)
            except Exception:
                pass
        # Private companies (last known valuations)
        for nid, val in {"openai":300,"anthropic":61,"xai":50,"bytedance":220,"huawei":80,
                         "databricks":62,"scaleai":14,"hf":4.5,"cerebras":4,"groq":2.8,
                         "cohere":5.5,"perplexity":9,"mistral":6,"deepseek":2}.items():
            if nid not in eco_mcap:
                eco_mcap[nid] = val
        log(f"  Got {len(eco_mcap)} ecosystem mcaps")
    except ImportError:
        log("  yfinance not available — skipping ecosystem mcaps")

    # Charge les valeurs du run précédent pour le MERGE (durcissement 2026-05-29).
    # Avant : un fetch échoué (429, blip réseau) -> global droppé -> cache amputé.
    # Si TOUS les fetchs échouaient (cas réel 13:41), radar_cache.js tombait à 2
    # vars sans __CG_COINS__ -> Radar v1 Indicateur bloqué en "ANALYSE EN COURS".
    # Désormais : un fetch vide CONSERVE la valeur précédente (le cache ne peut
    # plus se vider). Même pattern que fetch_global_markets / fetch_l1_valuation.
    prev_data = {}
    try:
        if CACHE_FILE.exists():
            _pj = json.loads(CACHE_FILE.read_text())
            if isinstance(_pj.get("data"), dict):
                prev_data = _pj["data"]
    except Exception as e:
        log(f"  prev cache illisible ({e}) — merge désactivé ce run")

    fetched = {
        "__BINANCE_FR__": fr_btc, "__BINANCE_FR_ETH__": fr_eth,
        "__BINANCE_OI__": oi_btc, "__BINANCE_OI_ETH__": oi_eth,
        "__BINANCE_LS__": ls, "__BINANCE_TAKER__": taker,
        "__CG_GLOBAL__": cg_global, "__CG_COINS__": cg_coins,
        "__STABLE_DATA__": stables, "__DEFI_TVL__": tvl,
        "__CMC_FNG_DATA__": fng_data, "__ALTSEASON_DATA__": altseason,
        "__BTC_HIST__": btc_hist, "__STOCK_DATA__": stock_data,
        "__ECO_MCAP__": eco_mcap if eco_mcap else None,
    }

    def non_empty(d):
        return d is not None and d != [] and d != {}

    # Merge : valeur fraîche si non-vide, sinon valeur précédente, sinon rien.
    final = {}
    for varname, data in fetched.items():
        if non_empty(data):
            final[varname] = data
        elif non_empty(prev_data.get(varname)):
            final[varname] = prev_data[varname]
            log(f"  KEEP {varname} (fetch vide → valeur précédente conservée)")
        else:
            log(f"  SKIP {varname} (vide + aucun précédent — fallback R)")

    parts = [f"window.{v}={json.dumps(d, separators=(',', ':'), ensure_ascii=False)}"
             for v, d in final.items()]

    js_content = "// Auto-generated by fetch_radar_data.py — " + datetime.now().strftime("%d/%m/%Y %H:%M") + "\n"
    js_content += ";\n".join(parts) + ";\n"
    js_content += f'window.__RADAR_UPDATED__="{datetime.now().strftime("%d/%m/%Y %H:%M")}";\n'

    CACHE_JS.write_text(js_content)
    log(f"Wrote {CACHE_JS.name} ({len(parts)} variables, {len(js_content)//1024}KB)")

    # On persiste les données (pas juste un compteur) pour servir de fallback merge.
    payload = {"updated": datetime.now().strftime("%d/%m/%Y %H:%M"), "vars": len(parts), "data": final}
    CACHE_FILE.write_text(json.dumps(payload))

if __name__ == "__main__":
    main()
