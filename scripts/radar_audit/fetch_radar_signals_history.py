#!/usr/bin/env python3
"""
PHASE 0b.1 — Fetch exhaustif des sous-signaux historiques du Radar Crypto.

Sources et profondeur cible :
  • Binance Futures API : Funding BTC / ETH 2019-09 → today
  • DefiLlama : Stablecoin supply 2018+, DeFi TVL 2018+
  • CoinGecko : volume BTC, BTC dominance approximée
  • data.binance.vision : OI / Long-Short / Taker (ZIP CSV daily) — Phase 0b.1 step 2

Tous les outputs sont sauvegardés dans radar_audit/data_raw/<signal>.json
puis consolidés en radar_audit/radar_signals_history.json (daily, normalisé en UTC).

Politique : aucun "skip par paresse". Si une source échoue, on log précisément
et on retry. Validation systématique (gaps, cohérence).
"""
import json
import time
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data_raw"
RAW.mkdir(parents=True, exist_ok=True)


def fetch(url, retries=3, sleep=1.5, timeout=30, headers=None):
    h = {"User-Agent": "Mozilla/5.0 (radar-audit/1.0)"}
    if headers: h.update(headers)
    for attempt in range(retries):
        try:
            req = Request(url, headers=h)
            with urlopen(req, timeout=timeout) as r:
                return r.read()
        except (HTTPError, URLError) as e:
            if attempt == retries - 1:
                raise
            wait = sleep * (2 ** attempt)
            print(f"    retry {attempt+1}/{retries} after {wait}s ({type(e).__name__}: {e})")
            time.sleep(wait)


def fetch_json(url, **kwargs):
    return json.loads(fetch(url, **kwargs))


def _payload_count(data):
    """Nombre d'enregistrements dans un payload raw (liste, ou dict-de-liste
    type CoinGecko market_chart)."""
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for k in ("total_volumes", "market_caps", "prices"):
            v = data.get(k)
            if isinstance(v, list):
                return len(v)
        return sum(len(v) for v in data.values() if isinstance(v, list))
    return 0


def save(name, data):
    """Persiste un payload raw, AVEC garde anti-wipe : ne jamais écraser un
    fichier non-vide par un payload vide. Un flap DNS/réseau transitoire
    (Errno 8 « nodename nor servname provided ») faisait sauvegarder [] par
    fetch_funding et effaçait des années d'historique funding → 2 des 11
    signaux Radar v3 tombaient à None (cache « n/a », score en mode 9/11).
    Sur fetch vide on conserve le fichier précédent et on le logue fort."""
    p = RAW / f"{name}.json"
    new_n = _payload_count(data)
    if new_n == 0 and p.exists():
        try:
            old_n = _payload_count(json.loads(p.read_text()))
        except Exception:
            old_n = 0
        if old_n > 0:
            print(f"    [anti-wipe] {name}: payload vide → conserve l'existant ({old_n} entrées)")
            return p
    p.write_text(json.dumps(data))
    return p


# ─────────────────────────────────────────────────────────────────────────────
# 1) BINANCE FUNDING RATES — pagination par 1000 (8h funding => 3/jour)
#    Endpoint: /fapi/v1/fundingRate
#    Limite : 1000 entrées par appel, donc ~333 jours par appel
# ─────────────────────────────────────────────────────────────────────────────
def fetch_funding(symbol="BTCUSDT"):
    print(f"[FUNDING {symbol}] fetching from Binance Futures…")
    all_data = []
    start = 1568000000000  # 2019-09 (early Binance perp launch)
    end = int(time.time() * 1000)
    cursor = start
    while cursor < end:
        url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&startTime={cursor}&limit=1000"
        try:
            batch = fetch_json(url)
        except Exception as e:
            print(f"    ! échec à cursor {cursor}: {e}")
            break
        if not batch:
            break
        all_data.extend(batch)
        last_t = batch[-1]["fundingTime"]
        if last_t <= cursor:
            break
        cursor = last_t + 1
        time.sleep(0.20)  # courtoisie API
        if len(all_data) % 5000 == 0:
            d = datetime.fromtimestamp(last_t / 1000, tz=timezone.utc).date()
            print(f"    progress: {len(all_data)} entrées, jusqu'au {d}")
    # dédup
    seen = set(); uniq = []
    for r in all_data:
        k = (r["symbol"], r["fundingTime"])
        if k not in seen:
            seen.add(k); uniq.append(r)
    print(f"    → {len(uniq)} funding payments fetchés (3/jour)")
    save(f"funding_{symbol}", uniq)
    return uniq


# ─────────────────────────────────────────────────────────────────────────────
# 2) DEFILLAMA STABLECOIN SUPPLY (total)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_stablecoins():
    print("[STABLECOINS] fetching DefiLlama stablecoincharts/all…")
    data = fetch_json("https://stablecoins.llama.fi/stablecoincharts/all", timeout=60)
    print(f"    → {len(data)} jours de stablecoin supply")
    save("stablecoins_supply", data)
    return data


# ─────────────────────────────────────────────────────────────────────────────
# 3) DEFILLAMA TOTAL TVL
# ─────────────────────────────────────────────────────────────────────────────
def fetch_tvl():
    print("[TVL] fetching DefiLlama charts (all chains)…")
    data = fetch_json("https://api.llama.fi/v2/historicalChainTvl", timeout=60)
    print(f"    → {len(data)} jours de TVL global")
    save("defi_tvl", data)
    return data


# ─────────────────────────────────────────────────────────────────────────────
# 4) BTC VOLUME (CoinGecko market_chart, max range)
#    Note : pour granularité daily sur 365j+, on peut concaténer plusieurs requêtes
#    via paramètre interval=daily ou utiliser yfinance pour long historique.
#    Ici on tente "max" en daily puis fallback yfinance.
# ─────────────────────────────────────────────────────────────────────────────
def fetch_btc_volume():
    print("[BTC VOLUME] CoinGecko market_chart days=max interval=daily…")
    try:
        url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=max&interval=daily"
        data = fetch_json(url, timeout=60)
        vols = data.get("total_volumes", [])
        print(f"    → {len(vols)} jours de volume")
        save("btc_volume", data)
        return data
    except Exception as e:
        print(f"    ! CoinGecko échec: {e}, fallback yfinance")
        try:
            import yfinance as yf
            df = yf.download("BTC-USD", start="2014-01-01", interval="1d", progress=False)
            vols = [[int(t.timestamp()*1000), float(v)] for t, v in df["Volume"].items()]
            data = {"total_volumes": vols, "source": "yfinance"}
            print(f"    → {len(vols)} jours via yfinance")
            save("btc_volume", data)
            return data
        except Exception as e2:
            print(f"    ! yfinance aussi: {e2}")
            return None


# ─────────────────────────────────────────────────────────────────────────────
# 5) BTC DOMINANCE (approximation via marketcap BTC / total mcap)
#    CoinGecko n'expose pas l'historique global gratuit. On utilise :
#    coingecko market_chart pour BTC.market_cap (a long terme)
#    + global mcap dérivé via /coins/markets snapshot (limité)
#    Ou approximation : BTC mcap / (BTC mcap + ETH mcap + 1.5 × somme top-10)
#    Plus pragmatique: utiliser les données BTC dominance de TradingView (DXY scraping)
#    Ici on commence par mcap BTC seul (utile aussi en soi).
# ─────────────────────────────────────────────────────────────────────────────
def fetch_btc_marketcap():
    print("[BTC MARKETCAP] CoinGecko market_chart days=max…")
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=max&interval=daily"
    data = fetch_json(url, timeout=60)
    save("btc_marketcap", data)
    print(f"    → BTC mcap : {len(data.get('market_caps', []))} jours")
    return data


def fetch_eth_marketcap():
    print("[ETH MARKETCAP] CoinGecko market_chart days=max…")
    url = "https://api.coingecko.com/api/v3/coins/ethereum/market_chart?vs_currency=usd&days=max&interval=daily"
    data = fetch_json(url, timeout=60)
    save("eth_marketcap", data)
    print(f"    → ETH mcap : {len(data.get('market_caps', []))} jours")
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("="*78)
    print("PHASE 0b.1 — FETCH RADAR SIGNALS (Phase 1 : sources principales)")
    print("="*78)

    results = {}

    # 1) Funding BTC + ETH
    try:
        results["funding_btc"] = fetch_funding("BTCUSDT")
    except Exception as e:
        print(f"  !! funding BTC fail: {e}")

    try:
        results["funding_eth"] = fetch_funding("ETHUSDT")
    except Exception as e:
        print(f"  !! funding ETH fail: {e}")

    # 2) Stablecoins
    try:
        results["stablecoins"] = fetch_stablecoins()
    except Exception as e:
        print(f"  !! stablecoins fail: {e}")

    # 3) TVL DeFi
    try:
        results["tvl"] = fetch_tvl()
    except Exception as e:
        print(f"  !! tvl fail: {e}")

    # 4) BTC volume + mcap
    try:
        results["btc_volume"] = fetch_btc_volume()
    except Exception as e:
        print(f"  !! btc volume fail: {e}")

    try:
        results["btc_mcap"] = fetch_btc_marketcap()
    except Exception as e:
        print(f"  !! btc mcap fail: {e}")

    try:
        results["eth_mcap"] = fetch_eth_marketcap()
    except Exception as e:
        print(f"  !! eth mcap fail: {e}")

    print()
    print("="*78)
    print("RÉSUMÉ FETCH")
    print("="*78)
    for k, v in results.items():
        n = len(v) if isinstance(v, list) else (len(v.get("total_volumes", v.get("market_caps", []))) if isinstance(v, dict) else "?")
        print(f"  {k:25s} : {n} entrées")

    print()
    print(f"Tous les fichiers raw → {RAW}")
    print("Prochaine étape : data.binance.vision pour OI / L-S / Taker (Phase 0b.1 step 2)")


if __name__ == "__main__":
    main()
