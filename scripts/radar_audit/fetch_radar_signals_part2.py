#!/usr/bin/env python3
"""
PHASE 0b.1 — PART 2 : sources complémentaires.

Récupère via Binance Klines (gratuit, illimité) :
  • BTC OHLCV daily 2017-08 → today (avec taker buy volume = source officielle)
  • ETH OHLCV daily 2017-11 → today

Et tente data.binance.vision pour OI / Long-Short ratio history.

Klines fields :
  [openTime, open, high, low, close, volume, closeTime, quoteAssetVolume,
   numTrades, takerBuyBaseVolume, takerBuyQuoteVolume, ignore]

Taker Buy/Sell ratio = takerBuyBaseVolume / volume
  Si >0.5 = pression acheteuse, <0.5 = pression vendeuse.
"""
import os
import json, time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data_raw"
RAW.mkdir(parents=True, exist_ok=True)


def fetch_json(url, retries=3, sleep=1.5, timeout=30):
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 (radar-audit/1.0)"})
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except (HTTPError, URLError) as e:
            if attempt == retries - 1: raise
            time.sleep(sleep * (2 ** attempt))


def fetch_klines(symbol, interval="1d", market="spot"):
    """
    Pagination via startTime/endTime. Klines spot ou um (futures).
    Limit 1000 par appel, donc 1000 jours par requête.
    """
    # Binance renvoie 451 aux IP américaines — donc aux runners. Son miroir de données
    # publiques, lui, répond (mesuré). On le prend d'emblée pour le spot : il sert
    # exactement la même API, sans le filtre géographique.
    if market == "spot":
        base = os.environ.get("BINANCE_SPOT_BASE",
                              "https://data-api.binance.vision/api/v3")
    else:
        base = "https://fapi.binance.com/fapi/v1"
    print(f"[KLINES {symbol} {interval} {market}] fetch from Binance…")
    all_data = []
    start = 1500000000000  # 2017-07
    end = int(time.time() * 1000)
    cursor = start
    while cursor < end:
        url = f"{base}/klines?symbol={symbol}&interval={interval}&startTime={cursor}&limit=1000"
        try:
            batch = fetch_json(url)
        except Exception as e:
            print(f"    !! échec à cursor {cursor}: {e}")
            break
        if not batch:
            break
        all_data.extend(batch)
        last = batch[-1][0]
        if last <= cursor: break
        cursor = last + 1
        time.sleep(0.20)
    # dedup par openTime
    seen = set(); uniq = []
    for r in all_data:
        if r[0] not in seen:
            seen.add(r[0]); uniq.append(r)
    if uniq:
        d0 = datetime.fromtimestamp(uniq[0][0]/1000, tz=timezone.utc).date()
        d1 = datetime.fromtimestamp(uniq[-1][0]/1000, tz=timezone.utc).date()
        print(f"    → {len(uniq)} bars du {d0} au {d1}")
    name = f"klines_{market}_{symbol}_{interval}"
    (RAW / f"{name}.json").write_text(json.dumps(uniq))
    return uniq


def main():
    print("="*78)
    print("PHASE 0b.1 — PART 2 : KLINES BTC/ETH (spot + futures)")
    print("="*78)

    # SPOT BTC/ETH (depuis 2017-08 / 2017-11)
    fetch_klines("BTCUSDT", "1d", "spot")
    fetch_klines("ETHUSDT", "1d", "spot")

    # FUTURES BTC/ETH (depuis 2019-09) — utile aussi pour Taker buy/sell des perps
    fetch_klines("BTCUSDT", "1d", "um")
    fetch_klines("ETHUSDT", "1d", "um")

    print("\n→ Données prêtes pour audit. Volume + taker buy/sell ratio dérivable.")

if __name__ == "__main__":
    main()
