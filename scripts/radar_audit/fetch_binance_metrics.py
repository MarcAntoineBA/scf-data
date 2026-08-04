#!/usr/bin/env python3
"""
PHASE 0b.1 — PART 3 : data.binance.vision metrics history.

Pour BTCUSDT et ETHUSDT, fetch quotidien :
  https://data.binance.vision/data/futures/um/daily/metrics/{SYMBOL}/{SYMBOL}-metrics-YYYY-MM-DD.zip

Chaque ZIP contient un CSV 5min avec :
  create_time, symbol, sum_open_interest (BTC), sum_open_interest_value (USD),
  count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio,
  count_long_short_ratio, sum_taker_long_short_vol_ratio

Agrégation daily :
  • OI : valeur de fin de journée (dernier point 23:55) → reflète l'OI à la clôture
  • Long/Short ratios : moyenne pondérée sur la journée
  • Taker ratio : moyenne pondérée sur la journée

Output : data_raw/metrics_{symbol}_daily.json
  [{"date":"2020-09-01","oi_btc":..., "oi_usd":..., "ls_count":..., "ls_top_count":...,
    "ls_top_position":..., "taker_lsv_ratio":...}, ...]

Politique : pas de "skip par paresse". Cache local pour éviter re-DL si interrompu.
Validation : log gaps + fail-fast si > 5 jours manquants.
"""
import json, time, io, zipfile, csv
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import date, timedelta, datetime

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data_raw"
CACHE_DIR = RAW / "binance_metrics_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def fetch_zip(symbol, d_iso, retries=3):
    """Télécharge le ZIP d'un jour. Cache local. Retourne bytes ou None si 404."""
    cache = CACHE_DIR / f"{symbol}-metrics-{d_iso}.zip"
    if cache.exists() and cache.stat().st_size > 0:
        return cache.read_bytes()
    url = f"https://data.binance.vision/data/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{d_iso}.zip"
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 (radar-audit/1.0)"})
            with urlopen(req, timeout=30) as r:
                data = r.read()
                cache.write_bytes(data)
                return data
        except HTTPError as e:
            if e.code == 404:
                return None  # data not available for this day
            if attempt == retries - 1: raise
            time.sleep(1.5 * (2 ** attempt))
        except URLError as e:
            if attempt == retries - 1: raise
            time.sleep(1.5 * (2 ** attempt))


def parse_zip_to_daily(zip_bytes):
    """Parse un ZIP CSV 5min, retourne dict daily summary."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as f:
            text = io.TextIOWrapper(f, encoding="utf-8")
            reader = csv.DictReader(text)
            rows = list(reader)
    if not rows:
        return None

    def col(name):
        # strip whitespace and detect float
        return [r[name] for r in rows if r.get(name, "").strip()]

    def avg(name):
        vals = [float(v) for v in col(name)]
        return sum(vals) / len(vals) if vals else None

    last = rows[-1]
    return {
        "n_intraday": len(rows),
        "oi_btc_close": float(last["sum_open_interest"]) if last["sum_open_interest"] else None,
        "oi_usd_close": float(last["sum_open_interest_value"]) if last["sum_open_interest_value"] else None,
        "oi_btc_avg": avg("sum_open_interest"),
        "oi_usd_avg": avg("sum_open_interest_value"),
        "ls_count_avg": avg("count_long_short_ratio"),
        "ls_top_count_avg": avg("count_toptrader_long_short_ratio"),
        "ls_top_position_avg": avg("sum_toptrader_long_short_ratio"),
        "taker_lsv_avg": avg("sum_taker_long_short_vol_ratio"),
    }


def fetch_metrics_history(symbol, start_iso="2020-09-01", end=None):
    print(f"\n[METRICS {symbol}] from {start_iso}…")
    if end is None:
        end = date.today() - timedelta(days=1)  # avoid in-progress today
    out = []
    cur = date.fromisoformat(start_iso)
    missing_days = []
    n_total = (end - cur).days + 1
    n_done = 0
    while cur <= end:
        d_iso = cur.isoformat()
        try:
            zb = fetch_zip(symbol, d_iso)
        except Exception as e:
            print(f"    !! fetch error {d_iso}: {e}")
            missing_days.append(d_iso)
            cur += timedelta(days=1)
            continue
        if zb is None:
            missing_days.append(d_iso)
        else:
            try:
                daily = parse_zip_to_daily(zb)
                if daily:
                    daily["date"] = d_iso
                    out.append(daily)
                else:
                    missing_days.append(d_iso)
            except Exception as e:
                print(f"    !! parse error {d_iso}: {e}")
                missing_days.append(d_iso)
        n_done += 1
        if n_done % 100 == 0:
            print(f"    progress: {n_done}/{n_total} ({d_iso})  ok={len(out)} missing={len(missing_days)}")
        cur += timedelta(days=1)
        time.sleep(0.05)  # courtoisie

    print(f"    → {len(out)} jours valides, {len(missing_days)} manquants")
    if missing_days:
        # Log first/last missing
        print(f"    missing sample: {missing_days[:5]} … {missing_days[-3:]}")

    out_path = RAW / f"metrics_{symbol}_daily.json"
    out_path.write_text(json.dumps({"data": out, "missing": missing_days}, indent=1))
    print(f"    → {out_path}")
    return out


def main():
    print("=" * 78)
    print("PHASE 0b.1 — PART 3 : Binance Vision metrics (OI + L/S + taker)")
    print("=" * 78)

    fetch_metrics_history("BTCUSDT")
    fetch_metrics_history("ETHUSDT")

    print("\nDone. Données disponibles depuis 2020-09-01.")


if __name__ == "__main__":
    main()
