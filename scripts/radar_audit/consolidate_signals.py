#!/usr/bin/env python3
"""
PHASE 0b.1 — Consolidation de tous les signaux fetchés en un dataset unique daily.

Lit les fichiers data_raw/*.json et produit radar_signals_history.json :
  [
    {
      "date": "2020-09-01",
      "ts": 1598918400,
      "btc_close": ...,
      "btc_high": ..., "btc_low": ...,
      "btc_open": ..., "btc_vol": ...,
      "btc_taker_buy_vol_spot": ..., "btc_taker_buy_ratio_spot": ...,
      "eth_close": ...,
      "btc_eth_ratio": ...,             # proxy dominance
      "fng": ...,                       # Fear & Greed
      "funding_btc_8h": ..., "funding_btc_daily_avg": ...,
      "funding_eth_8h": ..., "funding_eth_daily_avg": ...,
      "stables_total_usd": ..., "stables_growth_30d": ...,
      "tvl_total_usd": ..., "tvl_growth_30d": ...,
      "oi_btc_usd": ..., "oi_btc_close_usd": ..., "oi_growth_7d": ..., "oi_growth_30d": ...,
      "ls_count_btc": ..., "ls_top_count_btc": ..., "ls_top_pos_btc": ...,
      "taker_lsv_btc": ...,             # taker long/short volume ratio depuis perp
      # Forward returns (calculés à partir des prix BTC)
      "ret_btc_7": ..., "ret_btc_30": ..., "ret_btc_90": ...,
    },
    ...
  ]

Toutes les sources sont alignées sur des dates UTC (jour entier).
Forward fill pour les séries qui n'ont pas tous les jours (ex: stables/TVL si gaps).
"""
import json
import math
from pathlib import Path
from datetime import datetime, timezone, date, timedelta

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data_raw"
OUT = ROOT / "radar_signals_history.json"


def utc_day(ts_ms_or_s):
    """Renvoie 'YYYY-MM-DD' en UTC."""
    if ts_ms_or_s > 1e12:
        ts = ts_ms_or_s / 1000
    else:
        ts = ts_ms_or_s
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def load_klines(symbol="BTCUSDT", market="spot"):
    """Retourne dict {date_iso : {open, high, low, close, vol, taker_buy_base, taker_buy_quote}}."""
    p = RAW / f"klines_{market}_{symbol}_1d.json"
    if not p.exists(): return {}
    arr = json.loads(p.read_text())
    out = {}
    for k in arr:
        d = utc_day(k[0])
        out[d] = {
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "vol": float(k[5]),
            "quote_vol": float(k[7]),
            "n_trades": int(k[8]),
            "taker_buy_base": float(k[9]),
            "taker_buy_quote": float(k[10]),
        }
        out[d]["taker_buy_ratio"] = out[d]["taker_buy_base"] / out[d]["vol"] if out[d]["vol"] > 0 else None
    return out


def load_funding(symbol):
    """Retourne {date_iso : {avg_funding, last_funding, n_payments}}."""
    p = RAW / f"funding_{symbol}.json"
    if not p.exists(): return {}
    arr = json.loads(p.read_text())
    by_day = {}
    for r in arr:
        d = utc_day(r["fundingTime"])
        rate = float(r["fundingRate"])
        by_day.setdefault(d, []).append(rate)
    out = {}
    for d, rates in by_day.items():
        out[d] = {
            "avg_funding": sum(rates) / len(rates),
            "last_funding": rates[-1],
            "n_payments": len(rates),
            "daily_funding": sum(rates),  # approximation : 3 paiements 8h
        }
    return out


def load_fng():
    """Charge F&G alternative.me /tmp/fng_full.json."""
    import os
    fng_p = Path("/tmp/fng_full.json")
    if not fng_p.exists():
        return {}
    arr = json.loads(fng_p.read_text())["data"]
    out = {}
    for r in arr:
        d = utc_day(int(r["timestamp"]))
        out[d] = int(r["value"])
    return out


def load_stables():
    p = RAW / "stablecoins_supply.json"
    if not p.exists(): return {}
    arr = json.loads(p.read_text())
    out = {}
    for r in arr:
        ts = int(r["date"])
        d = utc_day(ts)
        # totalCirculatingUSD est un dict {peggedUSD: ...}
        v = r.get("totalCirculatingUSD", {})
        if isinstance(v, dict):
            total = sum(float(x) for x in v.values() if x is not None)
        else:
            total = float(v) if v else None
        out[d] = total
    return out


def load_tvl():
    p = RAW / "defi_tvl.json"
    if not p.exists(): return {}
    arr = json.loads(p.read_text())
    out = {}
    for r in arr:
        d = utc_day(int(r["date"]))
        out[d] = float(r["tvl"])
    return out


def load_metrics(symbol):
    p = RAW / f"metrics_{symbol}_daily.json"
    if not p.exists(): return {}
    raw = json.loads(p.read_text())
    out = {}
    for r in raw["data"]:
        out[r["date"]] = {
            "oi_btc": r.get("oi_btc_close"),
            "oi_usd": r.get("oi_usd_close"),
            "oi_btc_avg": r.get("oi_btc_avg"),
            "oi_usd_avg": r.get("oi_usd_avg"),
            "ls_count": r.get("ls_count_avg"),
            "ls_top_count": r.get("ls_top_count_avg"),
            "ls_top_pos": r.get("ls_top_position_avg"),
            "taker_lsv": r.get("taker_lsv_avg"),
        }
    return out


def growth(series, dates, lookback):
    """Retourne pour chaque date la croissance % vs date - lookback days."""
    out = {}
    sorted_dates = sorted(series.keys())
    idx_by_date = {d: i for i, d in enumerate(sorted_dates)}
    for d in sorted_dates:
        i = idx_by_date[d]
        if i < lookback: continue
        prev_d = sorted_dates[i - lookback]
        prev = series.get(prev_d)
        cur = series.get(d)
        if prev and cur and prev > 0:
            out[d] = (cur - prev) / prev * 100
    return out


def main():
    print("=" * 78)
    print("CONSOLIDATION DATASET RADAR SIGNALS")
    print("=" * 78)

    btc_spot = load_klines("BTCUSDT", "spot")
    btc_um = load_klines("BTCUSDT", "um")
    eth_spot = load_klines("ETHUSDT", "spot")
    print(f"BTC spot klines: {len(btc_spot)} jours")
    print(f"BTC futures klines: {len(btc_um)} jours")
    print(f"ETH spot klines: {len(eth_spot)} jours")

    funding_btc = load_funding("BTCUSDT")
    funding_eth = load_funding("ETHUSDT")
    print(f"Funding BTC: {len(funding_btc)} jours")
    print(f"Funding ETH: {len(funding_eth)} jours")

    fng = load_fng()
    print(f"F&G: {len(fng)} jours")

    stables = load_stables()
    tvl = load_tvl()
    print(f"Stablecoins: {len(stables)} jours")
    print(f"TVL: {len(tvl)} jours")

    metrics_btc = load_metrics("BTCUSDT")
    metrics_eth = load_metrics("ETHUSDT")
    print(f"Metrics BTC: {len(metrics_btc)} jours")
    print(f"Metrics ETH: {len(metrics_eth)} jours")

    # Growth rates (30j) pour stables et TVL
    stables_g30 = growth(stables, sorted(stables.keys()), 30)
    tvl_g30 = growth(tvl, sorted(tvl.keys()), 30)
    stables_g7 = growth(stables, sorted(stables.keys()), 7)
    tvl_g7 = growth(tvl, sorted(tvl.keys()), 7)
    print(f"Stables growth 30d: {len(stables_g30)} jours")
    print(f"TVL growth 30d: {len(tvl_g30)} jours")

    # Master date range : 2020-09-01 (start metrics) → today
    # Mais on garde tout depuis 2018 si dispo (FNG + stables)
    all_dates = set(btc_spot.keys()) | set(funding_btc.keys()) | set(fng.keys()) | set(stables.keys())
    start = "2018-02-01"
    end = max(all_dates) if all_dates else date.today().isoformat()
    cur = date.fromisoformat(start)
    end_d = date.fromisoformat(end)

    # Build daily dataset
    rows = []
    while cur <= end_d:
        d = cur.isoformat()
        bs = btc_spot.get(d)
        if not bs:
            cur += timedelta(days=1)
            continue
        es = eth_spot.get(d, {})
        bu = btc_um.get(d, {})
        fb = funding_btc.get(d, {})
        fe = funding_eth.get(d, {})
        mb = metrics_btc.get(d, {})

        row = {
            "date": d,
            "btc_close": bs.get("close"),
            "btc_open": bs.get("open"),
            "btc_high": bs.get("high"),
            "btc_low": bs.get("low"),
            "btc_vol": bs.get("vol"),
            "btc_taker_buy_ratio_spot": bs.get("taker_buy_ratio"),
            "eth_close": es.get("close"),
            "btc_eth_ratio": (bs.get("close") / es.get("close")) if es.get("close") else None,
            "fng": fng.get(d),
            "funding_btc_avg": fb.get("avg_funding"),
            "funding_btc_daily": fb.get("daily_funding"),
            "funding_eth_avg": fe.get("avg_funding"),
            "stables_usd": stables.get(d),
            "stables_growth_30d": stables_g30.get(d),
            "stables_growth_7d": stables_g7.get(d),
            "tvl_usd": tvl.get(d),
            "tvl_growth_30d": tvl_g30.get(d),
            "tvl_growth_7d": tvl_g7.get(d),
            "oi_btc_usd_close": mb.get("oi_usd"),
            "oi_btc_usd_avg": mb.get("oi_usd_avg"),
            "ls_count_btc": mb.get("ls_count"),
            "ls_top_count_btc": mb.get("ls_top_count"),
            "ls_top_position_btc": mb.get("ls_top_pos"),
            "taker_lsv_btc_perp": mb.get("taker_lsv"),
        }
        rows.append(row)
        cur += timedelta(days=1)

    # Ajout des forward returns BTC
    closes = [r["btc_close"] for r in rows]
    for i, r in enumerate(rows):
        for h in [7, 30, 90]:
            if i + h < len(rows) and rows[i + h]["btc_close"]:
                r[f"ret_btc_{h}"] = (rows[i + h]["btc_close"] - r["btc_close"]) / r["btc_close"] * 100
            else:
                r[f"ret_btc_{h}"] = None

    print()
    print(f"Dataset final: {len(rows)} jours du {rows[0]['date']} au {rows[-1]['date']}")

    # Validation gaps : combien de jours ont chacun des signaux ?
    keys = [k for k in rows[0].keys() if k != "date"]
    print()
    print("=" * 78)
    print("COVERAGE PAR SIGNAL")
    print("=" * 78)
    for k in keys:
        n_avail = sum(1 for r in rows if r.get(k) is not None)
        pct = n_avail / len(rows) * 100
        bar = "█" * int(pct // 5)
        print(f"  {k:30s} : {n_avail:5d}/{len(rows)} ({pct:5.1f}%) {bar}")

    OUT.write_text(json.dumps(rows))
    print()
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
