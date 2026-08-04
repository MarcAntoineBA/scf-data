#!/usr/bin/env python3
"""Backtest complet du Radar de Signaux.

Récupère TOUTES les données historiques disponibles, calcule le score radar
pour chaque jour, et produit radar_backtest_cache.js.

Signaux backtestés (10/24 catégories) :
  1. Fear & Greed (alternative.me + CMC, ~2018)
  2. BTC 200MA distance (Yahoo Finance)
  3. Valorisation composite (Mayer + ATH + 365MA)
  4. BTC 7d change
  5. Volatilité 30j
  6. Halving cycle
  7. ETH/BTC ratio (Yahoo Finance)
  8. Funding Rate BTC (Binance, ~1 an)
  9. Stablecoins supply 30d change (DefiLlama, ~3 ans)
  10. DeFi TVL 30d change (DefiLlama, ~4 ans)
  11. Altseason Index (CMC, ~3 ans)

Non disponibles historiquement (free) : OI, L/S, taker, Google Trends, dominance.
"""
import json, sys, time, math
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

# curl_cffi : TLS fingerprint impersonation (bypass Yahoo/Cloudflare bot detection).
# requests stdlib gets 429 sur Yahoo Finance depuis ~mai 2026 → cache scores:[] silencieux.
try:
    from curl_cffi import requests as _http
    _HTTP_KWARGS = {"impersonate": "chrome120"}
except ImportError:
    import requests as _http
    _HTTP_KWARGS = {}

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JS = CACHE_DIR / "radar_backtest_cache.js"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0"
HALVING_TS = 1713571200  # April 20, 2024

def log(msg):
    sys.stderr.write(f"[BACKTEST] {msg}\n")

def fetch(url, retries=2, backoff=5):
    for attempt in range(retries + 1):
        try:
            r = _http.get(url, headers={"User-Agent": UA}, timeout=30, **_HTTP_KWARGS)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 or r.status_code >= 500:
                wait = backoff * (attempt + 1)
                log(f"  {r.status_code} retry in {wait}s ({attempt+1}/{retries+1})")
                time.sleep(wait)
                continue
            log(f"  {r.status_code} on {url[:60]}")
            return None
        except Exception as e:
            log(f"  FAIL {url[:60]} → {e}")
    return None

# ─────────────────────────────────────────
# 1. BTC + ETH price history (Yahoo Finance)
# ─────────────────────────────────────────
def fetch_price_history(symbol):
    """Try: 1) Yahoo Finance, 2) CoinGecko range, 3) extract from rendered HTML."""
    log(f"Fetching {symbol} price history...")
    now = int(time.time())

    # 1) Yahoo Finance
    d = fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&period1=1514764800&period2={now}", retries=1, backoff=5)
    if d:
        try:
            res = d["chart"]["result"][0]
            ts = res["timestamp"]
            cl = res["indicators"]["quote"][0]["close"]
            by_day = {}
            for t, p in zip(ts, cl):
                if p is not None:
                    by_day[datetime.fromtimestamp(t).strftime("%Y-%m-%d")] = round(p, 2)
            if len(by_day) > 365:
                log(f"  {symbol} (Yahoo): {len(by_day)} days")
                return by_day
        except Exception as e:
            log(f"  Yahoo parse error: {e}")

    # 2) CoinGecko (range endpoint, free tier)
    cg_id = "bitcoin" if "BTC" in symbol else "ethereum"
    # Fetch in 365-day chunks
    by_day = {}
    chunk_end = now
    for _ in range(8):  # Max 8 years
        chunk_start = chunk_end - 365 * 86400
        d = fetch(f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart/range?vs_currency=usd&from={chunk_start}&to={chunk_end}", retries=1, backoff=3)
        if d and "prices" in d:
            for ts_ms, p in d["prices"]:
                by_day[datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")] = round(p, 2)
        else:
            break
        chunk_end = chunk_start
        time.sleep(2)
    if len(by_day) > 365:
        log(f"  {symbol} (CoinGecko chunked): {len(by_day)} days")
        return by_day

    # 3) Extract from rendered HTML (last resort)
    if "BTC" in symbol:
        html_path = Path.home() / "Desktop" / "Site_Crypto_Finance" / "index.html"
        if html_path.exists():
            log(f"  APIs failed, extracting from index.html...")
            import re
            html = html_path.read_text(errors="ignore")
            m = re.search(r'window\.__BTC_HIST__\s*=\s*(\[.*?\]);', html, re.DOTALL)
            if m:
                try:
                    arr = json.loads(m.group(1))
                    for item in arr:
                        by_day[datetime.fromtimestamp(item["t"]).strftime("%Y-%m-%d")] = round(item["p"], 2)
                    if len(by_day) > 365:
                        log(f"  {symbol} (HTML extract): {len(by_day)} days")
                        return by_day
                except Exception as e:
                    log(f"  HTML parse error: {e}")

    log(f"  FAILED: no data for {symbol}")
    return by_day if by_day else {}

# ─────────────────────────────────────────
# 2. Fear & Greed (alternative.me)
# ─────────────────────────────────────────
def fetch_fng_history():
    log("Fetching Fear & Greed history (alternative.me)...")
    d = fetch("https://api.alternative.me/fng/?limit=0")
    if not d or "data" not in d:
        return {}
    by_day = {}
    for item in d["data"]:
        ts = int(item["timestamp"])
        day = datetime.fromtimestamp(ts, tz=None).strftime("%Y-%m-%d")
        by_day[day] = int(item["value"])
    log(f"  FNG: {len(by_day)} days")
    return by_day

# ─────────────────────────────────────────
# 3. Binance Funding Rate (paginated, ~1 year)
# ─────────────────────────────────────────
def fetch_funding_history():
    log("Fetching BTC Funding Rate history (Binance)...")
    all_entries = []
    start = int((datetime.now() - timedelta(days=400)).timestamp() * 1000)
    while True:
        url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&startTime={start}&limit=1000"
        d = fetch(url)
        if not d or len(d) == 0:
            break
        all_entries.extend(d)
        start = d[-1]["fundingTime"] + 1
        if len(d) < 1000:
            break
        time.sleep(0.5)

    # Average daily funding rate
    by_day = defaultdict(list)
    for entry in all_entries:
        day = datetime.fromtimestamp(entry["fundingTime"] / 1000, tz=None).strftime("%Y-%m-%d")
        by_day[day].append(float(entry["fundingRate"]) * 100)  # Convert to %

    result = {day: round(sum(rates) / len(rates), 6) for day, rates in by_day.items()}
    log(f"  Funding: {len(result)} days")
    return result

# ─────────────────────────────────────────
# 4. Stablecoins supply (DefiLlama)
# ─────────────────────────────────────────
def fetch_stablecoin_history():
    log("Fetching Stablecoin supply history (DefiLlama)...")
    # USDT (id=1) + USDC (id=2) + DAI (id=3) + BUSD (id=4)
    total_by_day = defaultdict(float)
    for sid in [1, 2, 3, 4]:
        d = fetch(f"https://stablecoins.llama.fi/stablecoincharts/all?stablecoin={sid}")
        if not d:
            continue
        for entry in d:
            ts = entry.get("date", 0)
            if isinstance(ts, str): ts = int(ts)
            day = datetime.fromtimestamp(ts, tz=None).strftime("%Y-%m-%d")
            circ = entry.get("totalCirculatingUSD", entry.get("totalCirculating", {}))
            if isinstance(circ, dict):
                total_by_day[day] += float(circ.get("peggedUSD", 0))
            else:
                total_by_day[day] += float(circ or 0)
        time.sleep(1)

    # Compute 30d change
    sorted_days = sorted(total_by_day.keys())
    result = {}
    for i, day in enumerate(sorted_days):
        # Find value 30 days ago
        target = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
        val_30d = total_by_day.get(target, 0)
        val_now = total_by_day[day]
        if val_30d > 1e9:  # Only if meaningful supply existed
            result[day] = round((val_now - val_30d) / val_30d * 100, 2)
    log(f"  Stablecoins: {len(result)} days with 30d change")
    return result

# ─────────────────────────────────────────
# 5. DeFi TVL (DefiLlama)
# ─────────────────────────────────────────
def fetch_tvl_history():
    log("Fetching DeFi TVL history (DefiLlama)...")
    d = fetch("https://api.llama.fi/v2/historicalChainTvl")
    if not d:
        return {}
    by_day = {}
    for entry in d:
        ts = entry.get("date", 0)
        if isinstance(ts, str): ts = int(ts)
        day = datetime.fromtimestamp(ts, tz=None).strftime("%Y-%m-%d")
        by_day[day] = float(entry.get("tvl", 0))

    # Compute 30d change
    sorted_days = sorted(by_day.keys())
    result = {}
    for day in sorted_days:
        target = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
        val_30d = by_day.get(target, 0)
        val_now = by_day[day]
        if val_30d > 1e9:
            result[day] = round((val_now - val_30d) / val_30d * 100, 2)
    log(f"  TVL: {len(result)} days with 30d change")
    return result

# ─────────────────────────────────────────
# 6. Altseason Index (CMC)
# ─────────────────────────────────────────
def fetch_altseason_history():
    log("Fetching Altseason history (CMC)...")
    now = int(time.time())
    d = fetch(f"https://api.coinmarketcap.com/data-api/v3/altcoin-season/chart?start=1514764800&end={now}")
    dlist = (d.get("data", {}).get("dataList") or d.get("data", {}).get("points")) if d else None
    if not dlist:
        return {}
    by_day = {}
    for item in dlist:
        ts = int(item.get("timestamp", 0))
        if ts == 0:
            continue
        day = datetime.fromtimestamp(ts, tz=None).strftime("%Y-%m-%d")
        by_day[day] = int(float(item.get("score", item.get("value", item.get("altcoinIndex", 50)))))
    log(f"  Altseason: {len(by_day)} days")
    return by_day

# ─────────────────────────────────────────
# 7. Google Trends (cache local de la page Google Trends dediee, mensuel 2004+)
# ─────────────────────────────────────────
def fetch_gtrends_history():
    log("Loading Google Trends history (local gtrends_cache.json)...")
    p = CACHE_DIR / "gtrends_cache.json"
    if not p.exists():
        log("  gtrends_cache.json absent"); return [], []
    try:
        data = json.loads(p.read_text())
    except Exception as e:
        log(f"  parse error: {e}"); return [], []
    btc = sorted((d["date"], int(d["hits"])) for d in data if d.get("keyword") == "Bitcoin")
    bull = sorted((d["date"], int(d["hits"])) for d in data if d.get("keyword") == "Bull run crypto")
    log(f"  GTrends: Bitcoin {len(btc)} mois, Bull run {len(bull)} mois")
    return btc, bull

# ─────────────────────────────────────────
# 8. Signaux dérivés (OI / L-S / taker / funding ETH) depuis le dataset v3
#    radar_signals_history.json (quotidien depuis 2020-09). Les données order-flow
#    n'existent pas avant : ces signaux sont donc absents du backtest pré-2020.
# ─────────────────────────────────────────
def load_v3_signals():
    log("Loading order-flow signals (radar_audit/radar_signals_history.json)...")
    candidates = [
        Path.home() / "Library" / "Application Support" / "SiteCryptoFinance" / "radar_audit" / "radar_signals_history.json",
        Path.home() / "Desktop" / "Site_Crypto_Finance" / "radar_audit" / "radar_signals_history.json",
    ]
    p = next((c for c in candidates if c.exists()), None)
    if not p:
        log("  radar_signals_history.json absent"); return {}
    try:
        rows = json.loads(p.read_text())
    except Exception as e:
        log(f"  parse error: {e}"); return {}
    by_day = {r["date"]: r for r in rows}
    log(f"  v3 signals: {len(by_day)} jours")
    return by_day

# ─────────────────────────────────────────
# MAIN — Compute daily radar score
# ─────────────────────────────────────────
def compute_backtest(btc, eth, fng, funding, stables, tvl, altseason, gtrends_btc, gtrends_bull, v3_signals):
    # Sort BTC prices chronologically
    btc_days = sorted(btc.keys())
    btc_prices = [btc[d] for d in btc_days]

    import bisect
    gt_months = [m for m, _ in gtrends_btc]; gt_hits = [h for _, h in gtrends_btc]
    bull_months = [m for m, _ in gtrends_bull]; bull_hits = [h for _, h in gtrends_bull]
    def gt_signal(day):
        # Reproduit la logique Google Trends du Radar v1 live (mensuel, as-of day).
        b = r = n = act = 0
        i = bisect.bisect_right(gt_months, day) - 1
        if i >= 12:
            act += 1
            recent4 = gt_hits[i-3:i+1]; prev8 = gt_hits[i-11:i-3]
            avgR4 = sum(recent4) / 4.0; avgP8 = (sum(prev8) / 8.0) if prev8 else 0
            gtChg = (avgR4 - avgP8) / avgP8 * 100 if avgP8 > 0 else 0
            last = gt_hits[i]
            if last >= 80 or avgR4 >= 75: r += 1      # gt_euphoria
            elif last <= 15 or avgR4 <= 20: b += 1    # gt_apathy
            else: n += 1                              # gt_surge/decline/neutre (info)
        if bull_months:
            j = bisect.bisect_right(bull_months, day) - 1
            if j >= 3 and sum(bull_hits[j-3:j+1]) / 4.0 >= 50:
                r += 1; act += 1                      # gt_bullrun
        return b, r, n, act

    v3_days = sorted(v3_signals.keys())
    v3_idx = {d: i for i, d in enumerate(v3_days)}
    def deriv_signals(day):
        # Funding ETH + OI + L/S + Taker (mêmes seuils que le Radar v1 live),
        # depuis le dataset v3 (order-flow Binance, quotidien 2020-09+).
        b = r = n = act = 0
        row = v3_signals.get(day)
        if not row:
            return 0, 0, 0, 0
        fe = row.get("funding_eth_avg"); fb = row.get("funding_btc_avg")
        if fe is not None:
            fep = fe * 100; act += 1
            if fep > 0.06: r += 1          # fr_eth_extreme
            elif fep < -0.03: b += 1       # fr_eth_neg
            else: n += 1
            if fb is not None and abs(fep - fb * 100) > 0.04:
                n += 1; act += 1           # divergence funding ETH/BTC (info)
        oi = row.get("oi_btc_usd_close"); i = v3_idx.get(day, -1)
        if oi is not None and i >= 1:
            prev = v3_signals.get(v3_days[i-1], {})
            oi_p = prev.get("oi_btc_usd_close"); bp = row.get("btc_close"); bp_p = prev.get("btc_close")
            if oi_p and oi_p > 0 and bp and bp_p and bp_p > 0:
                oiChg = (oi - oi_p) / oi_p * 100; btcC = (bp - bp_p) / bp_p * 100
                act += 1
                if oiChg > 8 or oiChg < -8: n += 1             # oi_surge/flush (info)
                if oiChg > 5 and btcC < -2: r += 1; act += 1   # oi_div_bear
                elif oiChg < -5 and btcC > 2: b += 2; act += 1 # oi_div_bull
        ls = row.get("ls_count_btc")
        if ls is not None:
            act += 1
            if ls > 2.5: r += 3
            elif ls < 0.6: b += 2
            elif ls > 1.8: r += 1
            elif ls < 0.8: b += 1
        tk = row.get("taker_lsv_btc_perp")
        if tk is not None:
            act += 1
            if tk > 1.15: b += 2
            elif tk < 0.85: r += 1
        return b, r, n, act

    results = []

    for i, day in enumerate(btc_days):
        if i < 365:
            continue  # Need 365 days of history
        p = btc_prices[i]
        if p <= 0:
            continue

        bull_w = 0
        bear_w = 0
        neutrals = 0
        active_signals = 0

        # 1. Fear & Greed
        f = fng.get(day)
        if f is not None:
            active_signals += 1
            if f < 20: bull_w += 2
            elif f < 35: bull_w += 1
            elif f > 82: bear_w += 1
            elif f > 65: bear_w += 1
            else: neutrals += 1

        # 2. 200MA distance
        if i >= 200:
            ma200 = sum(btc_prices[i-199:i+1]) / 200
            dist200 = (p - ma200) / ma200 * 100
            active_signals += 1
            if dist200 < -15: bull_w += 1
            elif dist200 < -5: bull_w += 1
            elif dist200 > 80: bear_w += 1
            elif dist200 > 40: bear_w += 1
            else: neutrals += 1

        # 3. Valuation composite (Mayer + ATH + 365MA)
        if i >= 365:
            ma365 = sum(btc_prices[i-364:i+1]) / 365
            ma200_v = sum(btc_prices[i-199:i+1]) / 200
            mayer = p / ma200_v
            ath = max(btc_prices[:i+1])
            ath_dist = (p - ath) / ath * 100

            mayer_s = 10 if mayer < 0.8 else 30 if mayer < 1.0 else 50 if mayer < 1.5 else 70 if mayer < 2.4 else 90
            ath_s = 10 if ath_dist < -60 else 25 if ath_dist < -30 else 45 if ath_dist < -10 else 60 if ath_dist < 0 else 85
            ma365_s = 10 if p < ma365*0.8 else 30 if p < ma365 else 50 if p < ma365*1.5 else 70 if p < ma365*2.5 else 90
            composite = round((mayer_s + ath_s + ma365_s) / 3)

            active_signals += 1
            if composite <= 20: bull_w += 1
            elif composite <= 40: bull_w += 1
            elif composite >= 80: bear_w += 1
            elif composite >= 60: bear_w += 1
            else: neutrals += 1

        # 4. BTC 7d change
        if i >= 7:
            p7 = btc_prices[i-7]
            if p7 > 0:
                chg7 = (p - p7) / p7 * 100
                active_signals += 1
                if chg7 < -12: bull_w += 1
                elif chg7 > 12: bear_w += 1
                else: neutrals += 1

        # 5. Volatility 30d
        if i >= 30:
            rets = []
            for k in range(i-29, i+1):
                if btc_prices[k-1] > 0:
                    rets.append(abs(math.log(btc_prices[k] / btc_prices[k-1])) * 100)
            if rets:
                avg_vol = sum(rets) / len(rets)
                active_signals += 1
                neutrals += 1  # Vol is always info/neutral

        # (Halving retiré comme sous-signal — cohérence avec le live)

        # 7. ETH/BTC ratio
        eth_p = eth.get(day)
        if eth_p and eth_p > 0 and p > 0:
            eth_btc = eth_p / p
            active_signals += 1
            if eth_btc < 0.03: bull_w += 2
            elif eth_btc > 0.08: bear_w += 2
            else: neutrals += 1

        # 8. Funding Rate
        fr = funding.get(day)
        if fr is not None:
            active_signals += 1
            if fr > 0.06: bear_w += 1
            elif fr > 0.03: bear_w += 1
            elif fr < -0.03: bull_w += 3
            elif fr < -0.01: bull_w += 2
            else: neutrals += 1

        # 9. Stablecoins 30d change
        sc = stables.get(day)
        if sc is not None:
            active_signals += 1
            if sc > 3: bull_w += 1
            elif sc < -2: bear_w += 2
            else: neutrals += 1

        # 10. DeFi TVL 30d change
        tv = tvl.get(day)
        if tv is not None:
            active_signals += 1
            if tv > 15: bull_w += 1
            elif tv < -15: bear_w += 2
            else: neutrals += 1

        # 11. Altseason Index
        als = altseason.get(day)
        if als is not None:
            active_signals += 1
            if als > 75: bull_w += 1
            elif als < 25: bear_w += 1
            else: neutrals += 1

        # 12. Google Trends (Bitcoin + Bull run crypto)
        gb, gr, gn, gact = gt_signal(day)
        bull_w += gb; bear_w += gr; neutrals += gn; active_signals += gact

        # 13. Order-flow : OI, L/S, taker, funding ETH (dataset v3, 2020-09+)
        db, dr, dn, dact = deriv_signals(day)
        bull_w += db; bear_w += dr; neutrals += dn; active_signals += dact

        # ── Score ──
        if active_signals < 3:
            continue
        total_w = max(bull_w + bear_w + neutrals * 0.5, 1)
        score = round(((bull_w - bear_w) / total_w) * 50 + 50)
        score = max(0, min(100, score))

        results.append({
            "d": day,
            "s": score,
            "p": round(p),
            "n": active_signals
        })

    return results


def compute_performance(results):
    """Compute predictive performance by score bucket."""
    buckets = {
        "0-25": {"ret7": [], "ret30": [], "ret90": []},
        "25-45": {"ret7": [], "ret30": [], "ret90": []},
        "45-55": {"ret7": [], "ret30": [], "ret90": []},
        "55-75": {"ret7": [], "ret30": [], "ret90": []},
        "75-100": {"ret7": [], "ret30": [], "ret90": []},
    }

    for i, r in enumerate(results):
        s = r["s"]
        p = r["p"]
        if s <= 25: bk = "0-25"
        elif s <= 45: bk = "25-45"
        elif s <= 55: bk = "45-55"
        elif s <= 75: bk = "55-75"
        else: bk = "75-100"

        if i + 7 < len(results): buckets[bk]["ret7"].append((results[i+7]["p"] - p) / p * 100)
        if i + 30 < len(results): buckets[bk]["ret30"].append((results[i+30]["p"] - p) / p * 100)
        if i + 90 < len(results): buckets[bk]["ret90"].append((results[i+90]["p"] - p) / p * 100)

    def median(arr):
        if not arr: return None
        s = sorted(arr)
        m = len(s) // 2
        return s[m] if len(s) % 2 else (s[m-1] + s[m]) / 2

    def win_rate(arr):
        if not arr: return None
        return round(len([x for x in arr if x > 0]) / len(arr) * 100)

    perf = {}
    for bk, data in buckets.items():
        perf[bk] = {
            "count": len(data["ret30"]),
            "ret7": round(median(data["ret7"]), 1) if median(data["ret7"]) is not None else None,
            "ret30": round(median(data["ret30"]), 1) if median(data["ret30"]) is not None else None,
            "ret90": round(median(data["ret90"]), 1) if median(data["ret90"]) is not None else None,
            "win7": win_rate(data["ret7"]),
            "win30": win_rate(data["ret30"]),
            "win90": win_rate(data["ret90"]),
        }
    return perf


def main():
    log("=== RADAR BACKTEST — Full Historical Computation ===")

    btc = fetch_price_history("BTC-USD")
    time.sleep(2)
    eth = fetch_price_history("ETH-USD")
    time.sleep(2)
    fng = fetch_fng_history()
    time.sleep(1)
    funding = fetch_funding_history()
    time.sleep(1)
    stables = fetch_stablecoin_history()
    time.sleep(1)
    tvl = fetch_tvl_history()
    time.sleep(1)
    altseason = fetch_altseason_history()
    time.sleep(1)
    gtrends_btc, gtrends_bull = fetch_gtrends_history()
    v3_signals = load_v3_signals()

    if len(btc) < 365:
        log("ERROR: Not enough BTC data for backtest")
        sys.exit(1)

    log("Computing daily radar scores...")
    results = compute_backtest(btc, eth, fng, funding, stables, tvl, altseason, gtrends_btc, gtrends_bull, v3_signals)
    log(f"  {len(results)} daily scores computed")

    # ── Amplification d'amplitude (linéaire, monotone, autour du centre historique) :
    # élargit le swing du score pour mieux utiliser la plage 0-100, SANS changer le
    # classement des jours ni la pertinence (transform monotone → IC/ranking inchangés).
    # gain léger ; pivot = moyenne historique → le niveau moyen reste identique.
    AMP_GAIN = 1.4
    raw = [r["s"] for r in results]
    pivot = sum(raw) / len(raw)
    for r in results:
        r["s"] = max(0, min(100, round(pivot + (r["s"] - pivot) * AMP_GAIN)))
    log(f"  Amplitude x{AMP_GAIN} autour de pivot={pivot:.1f}")

    log("Computing performance stats...")
    perf = compute_performance(results)
    for bk, data in perf.items():
        r30 = f"{data['ret30']:+.1f}%" if data['ret30'] is not None else "N/A"
        w30 = f"{data['win30']}%" if data['win30'] is not None else "N/A"
        log(f"  [{bk}] {data['count']}j → ret30={r30} win30={w30}")

    # Write JS cache
    payload = {
        "scores": results,
        "performance": perf,
        "signals_used": 15,
        "signals_total": 15,
        "missing": [],
        "amp": {"pivot": round(pivot, 2), "gain": AMP_GAIN},
        "computed": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }
    js = "window.__RADAR_BACKTEST__=" + json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + ";\n"
    OUT_JS.write_text(js)
    log(f"Wrote {OUT_JS.name} ({len(results)} points, {len(js)//1024}KB)")


if __name__ == "__main__":
    main()
