"""Fetcher DeFi Stablecoin Engagement — % de la supply stable engagee en DeFi.

Methodologie (apres audit 2026-06-03) :
1. Top 60 protocoles stable-heavy par TVL (Lending / CDP / Yield / Basis Trading / RWA / etc.)
2. Pour chacun, fetcher /protocol/<slug> et agreger chainTvls.<chain>.tokensInUsd
   (skip les chaines virtuelles -borrowed etc qui sont des liabilities)
3. Per protocole, somme stables (regex USD|DAI|GHO|LUSD|FRAX|PYUSD|EUR|RLUSD|BUIDL|USYC|CRVUSD)
4. Forward-fill par protocole pour aligner historique (sinon dernier jour artificiellement bas)
5. Ratio = DeFi stable TVL / Total stable supply (DefiLlama /stablecoincharts)

Sortie : ~/Desktop/Site_Crypto_Finance/defi_engagement_cache.js
expose window.__DEFI_ENGAGEMENT__ = {
  current: {date, ratio_pct, defi_b, total_b, dry_b, percentile},
  history: [{d, u, defi, total}, ...]  (downsampled to ~600 pts),
  btc_hist: [{d, p}, ...]  pour overlay
}
"""
import os
import json
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OUT_JS = os.path.expanduser("~/Desktop/Site_Crypto_Finance/defi_engagement_cache.js")

STABLE_CATEGORIES = {
    "Lending", "CDP", "Yield Aggregator", "Basis Trading",
    "RWA", "Yield", "Algo-Stables", "Synthetics", "Insurance",
}
STABLE_RE = re.compile(r"USD|DAI|GHO|LUSD|FRAX|PYUSD|EUR|RLUSD|BUIDL|USYC|CRVUSD|MUSD|FEUSD|USDH", re.I)
VIRTUAL_SUFFIXES = ("-borrowed", "-staking", "-pool2", "-treasury", "-vesting")
MIN_TVL = 200e6
TOP_N = 60
HTTP_TIMEOUT = 25
INTER_REQ_SLEEP = 0.06


def http_json(url, retries=3, sleep_between=2.0):
    last_err = None
    for i in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 SiteCryptoFinance"})
            with urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return json.load(r)
        except (HTTPError, URLError, json.JSONDecodeError, TimeoutError) as e:
            last_err = e
            time.sleep(sleep_between * (i + 1))
    print(f"[defi_engagement] failed {url}: {last_err}", file=sys.stderr)
    return None


def is_real_chain(name):
    if not name or name == "borrowed":
        return False
    return not any(name.endswith(s) for s in VIRTUAL_SUFFIXES)


def is_stable_sym(sym):
    return bool(STABLE_RE.search(sym))


def sum_pt_stables(pt):
    toks = pt.get("tokens") or {}
    s = 0.0
    for tk, v in toks.items():
        sym = tk.split(":")[-1]
        try:
            vf = float(v)
        except (TypeError, ValueError):
            continue
        if vf <= 0:
            continue
        if is_stable_sym(sym):
            s += vf
    return s


def main():
    print(f"[defi_engagement] fetch /protocols...", file=sys.stderr)
    all_protos = http_json("https://api.llama.fi/protocols")
    if not all_protos:
        print("[defi_engagement] ERROR: /protocols failed", file=sys.stderr)
        sys.exit(1)

    candidates = []
    for p in all_protos:
        slg = p.get("slug")
        cat = p.get("category", "")
        if not slg or cat not in STABLE_CATEGORIES:
            continue
        try:
            tvl = float(p.get("tvl") or 0)
        except (TypeError, ValueError):
            continue
        if tvl < MIN_TVL:
            continue
        candidates.append((slg, p.get("name", slg), cat, tvl))
    candidates.sort(key=lambda x: -x[3])
    candidates = candidates[:TOP_N]
    print(f"[defi_engagement] {len(candidates)} stable-heavy candidates >${MIN_TVL/1e6:.0f}M TVL",
          file=sys.stderr)

    proto_series = {}     # slug -> list[(date, stable_value)]
    defi_stable_total = 0
    proto_details = []    # for visibility

    for i, (slug, name, cat, tvl) in enumerate(candidates):
        pj = http_json(f"https://api.llama.fi/protocol/{slug}")
        if not pj:
            time.sleep(INTER_REQ_SLEEP)
            continue
        # Build per-chain sparse stable series
        chain_series = {}  # chain -> sorted list of (date, stable_value)
        cts = pj.get("chainTvls") or {}
        for cn, cd in cts.items():
            if not is_real_chain(cn):
                continue
            tiu = cd.get("tokensInUsd") or []
            if not tiu:
                continue
            pts = []
            for pt in tiu:
                try:
                    d = int(pt.get("date") or 0)
                except (TypeError, ValueError):
                    continue
                if d <= 0:
                    continue
                stab = sum_pt_stables(pt)
                if stab > 0:
                    pts.append((d, stab))
            if pts:
                pts.sort(key=lambda x: x[0])
                chain_series[cn] = pts
        # Fallback : single-chain / pas chainTvls -> root tokensInUsd
        if not chain_series:
            tiu = pj.get("tokensInUsd") or []
            pts = []
            for pt in tiu:
                try:
                    d = int(pt.get("date") or 0)
                except (TypeError, ValueError):
                    continue
                if d <= 0:
                    continue
                stab = sum_pt_stables(pt)
                if stab > 0:
                    pts.append((d, stab))
            if pts:
                pts.sort(key=lambda x: x[0])
                chain_series["_root"] = pts
        if not chain_series:
            time.sleep(INTER_REQ_SLEEP)
            continue
        # Forward-fill each chain to common date range, then sum
        all_dates = sorted({d for pts in chain_series.values() for d, _ in pts})
        proto_dense = [0.0] * len(all_dates)
        for cn, pts in chain_series.items():
            pdates = [p[0] for p in pts]
            pvals = [p[1] for p in pts]
            j = 0
            last_v = 0.0
            for k, gd in enumerate(all_dates):
                while j < len(pdates) and pdates[j] <= gd:
                    last_v = pvals[j]
                    j += 1
                if j > 0:  # chain has started reporting
                    proto_dense[k] += last_v
        proto_series[slug] = list(zip(all_dates, proto_dense))
        latest_v = proto_dense[-1]
        defi_stable_total += latest_v
        proto_details.append((name, cat, latest_v))
        time.sleep(INTER_REQ_SLEEP)

    proto_details.sort(key=lambda x: -x[2])
    print(f"[defi_engagement] DeFi stable TVL snapshot: ${defi_stable_total/1e9:.2f}B "
          f"({len(proto_series)} protocols with stable composition)", file=sys.stderr)
    print(f"[defi_engagement] top 5:", file=sys.stderr)
    for nm, cat, v in proto_details[:5]:
        print(f"   {nm:30s} ({cat[:18]:18s}) ${v/1e9:.2f}B", file=sys.stderr)

    # Total stable supply history (DefiLlama /stablecoincharts/all)
    print(f"[defi_engagement] fetch total stable supply history...", file=sys.stderr)
    tot_resp = http_json("https://stablecoins.llama.fi/stablecoincharts/all")
    if not tot_resp:
        print("[defi_engagement] ERROR: stablecoincharts failed", file=sys.stderr)
        sys.exit(1)
    tot_history = []
    for pt in tot_resp:
        try:
            d = int(pt.get("date") or 0)
            v = float(pt.get("totalCirculatingUSD", {}).get("peggedUSD") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if d > 0 and v > 0:
            tot_history.append((d, v))
    tot_history.sort(key=lambda x: x[0])
    # SAFEGUARD (bug 2026-07-27, cf. fetch_moneyflow.drop_partial_tail) : DefiLlama
    # écrit le point du JOUR EN COURS au fil de son indexation → tôt le matin UTC il
    # ne vaut qu'une fraction du total (mesuré : 122 Md$ au lieu de 306 Md$). Ici il
    # gonflerait le ratio DeFi/total de 12 % à ~30 %. La masse stable ne bouge jamais
    # de 10 % en un jour → tout point de queue hors bande est incomplet, on le jette.
    dropped = 0
    while len(tot_history) >= 2 and dropped < 3:
        prev = tot_history[-2][1]
        if prev > 0 and abs(tot_history[-1][1] / prev - 1) > 0.10:
            print(f"[defi_engagement] point partiel jeté : {tot_history[-1][0]} = "
                  f"${tot_history[-1][1]/1e9:.2f}B vs ${prev/1e9:.2f}B", file=sys.stderr)
            tot_history.pop()
            dropped += 1
        else:
            break
    print(f"[defi_engagement] total supply: {len(tot_history)} pts, "
          f"last ${tot_history[-1][1]/1e9:.2f}B",
          file=sys.stderr)

    # Build aggregate utilization history with FORWARD-FILL
    all_dates_set = set()
    for slug, pts in proto_series.items():
        for d, _ in pts:
            all_dates_set.add(d)
    all_dates = sorted(all_dates_set)
    if not all_dates:
        print("[defi_engagement] ERROR: no aggregate dates", file=sys.stderr)
        sys.exit(1)
    # findInterval-style per protocol forward-fill
    agg = [0.0] * len(all_dates)
    for slug, pts in proto_series.items():
        pdates = [p[0] for p in pts]
        pvals = [p[1] for p in pts]
        j = 0
        last_v = 0
        for i, gd in enumerate(all_dates):
            while j < len(pdates) and pdates[j] <= gd:
                last_v = pvals[j]
                j += 1
            if j > 0:  # protocol has started reporting
                agg[i] += last_v

    # Join with total supply on closest date
    util_hist = []
    th_dates = [d for d, _ in tot_history]
    th_vals = [v for _, v in tot_history]
    for i, gd in enumerate(all_dates):
        defi_v = agg[i]
        if defi_v < 1e8:
            continue
        # closest th date
        idx = min(range(len(th_dates)), key=lambda k: abs(th_dates[k] - gd))
        tot_v = th_vals[idx]
        if tot_v <= 0:
            continue
        util_hist.append({
            "d": gd,
            "u": round(defi_v / tot_v * 100, 3),
            "defi": int(round(defi_v)),
            "total": int(round(tot_v)),
        })
    # Downsample to ~600 points
    if len(util_hist) > 600:
        step = (len(util_hist) + 599) // 600
        util_hist = util_hist[::step]
    print(f"[defi_engagement] util_hist: {len(util_hist)} pts, latest u={util_hist[-1]['u']:.2f}%",
          file=sys.stderr)

    # BTC overlay (Yahoo) — kept for backward compat
    print(f"[defi_engagement] fetch BTC overlay (Yahoo)...", file=sys.stderr)
    start_ts = util_hist[0]["d"]
    btc_pts = []
    try:
        yurl = (f"https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD"
                f"?interval=1d&period1={start_ts}&period2={int(time.time())}")
        ydata = http_json(yurl)
        if ydata:
            r = ydata["chart"]["result"][0]
            ts = r["timestamp"]
            cl = r["indicators"]["quote"][0]["close"]
            for i, t in enumerate(ts):
                if cl[i] is not None:
                    btc_pts.append({"d": int(t), "p": round(float(cl[i]), 2)})
    except Exception as e:
        print(f"[defi_engagement] WARN BTC fetch: {e}", file=sys.stderr)
    print(f"[defi_engagement] btc_pts: {len(btc_pts)}", file=sys.stderr)

    # ── ETH price history (Yahoo, free) — courbe de reference overlay sur l'engagement ──
    eth_pts = []
    try:
        yurle = (f"https://query1.finance.yahoo.com/v8/finance/chart/ETH-USD"
                 f"?interval=1d&period1={start_ts}&period2={int(time.time())}")
        ydatae = http_json(yurle)
        if ydatae:
            re_ = ydatae["chart"]["result"][0]
            tse = re_["timestamp"]
            cle = re_["indicators"]["quote"][0]["close"]
            for i, t in enumerate(tse):
                if cle[i] is not None:
                    eth_pts.append({"d": int(t), "p": round(float(cle[i]), 2)})
    except Exception as e:
        print(f"[defi_engagement] WARN ETH fetch: {e}", file=sys.stderr)
    print(f"[defi_engagement] eth_pts: {len(eth_pts)}", file=sys.stderr)

    # ── Total crypto mcap proxy via ^CMC200 (Yahoo, free) ──
    # ^CMC200 = CoinMarketCap 200 index (top 200 cryptos par mcap, niveau d'index
    # libellé en USD ~ proportionnel à TOTAL). Meilleur proxy gratuit pour la
    # direction du marché crypto global / TOTAL2 (vrai TOTAL2 requiert CoinGecko
    # Pro API). User-requested 2026-06-04 pour remplacer BTC sur DeFi Engagement.
    print(f"[defi_engagement] fetch ^CMC200 (proxy TOTAL crypto)...", file=sys.stderr)
    cmc200_pts = []
    try:
        yurl2 = (f"https://query1.finance.yahoo.com/v8/finance/chart/%5ECMC200"
                 f"?interval=1d&period1={start_ts}&period2={int(time.time())}")
        ydata2 = http_json(yurl2)
        if ydata2:
            r2 = ydata2["chart"]["result"][0]
            ts2 = r2["timestamp"]
            cl2 = r2["indicators"]["quote"][0]["close"]
            for i, t in enumerate(ts2):
                if cl2[i] is not None:
                    cmc200_pts.append({"d": int(t), "p": round(float(cl2[i]), 2)})
    except Exception as e:
        print(f"[defi_engagement] WARN CMC200 fetch: {e}", file=sys.stderr)
    print(f"[defi_engagement] cmc200_pts: {len(cmc200_pts)}", file=sys.stderr)

    last = util_hist[-1]
    sorted_u = sorted(p["u"] for p in util_hist)
    pct = 100.0 * sum(1 for v in sorted_u if v <= last["u"]) / len(sorted_u)

    payload = {
        "current": {
            "date": last["d"],
            "ratio_pct": last["u"],
            "defi_b": round(last["defi"] / 1e9, 3),
            "total_b": round(last["total"] / 1e9, 3),
            "dry_b": round((last["total"] - last["defi"]) / 1e9, 3),
            "percentile": round(pct, 1),
        },
        "history": util_hist,
        "btc_hist": btc_pts,
        "eth_hist": eth_pts,
        "cmc200_hist": cmc200_pts,
        "generated_at": int(time.time()),
        "source": "DefiLlama /protocols + /protocol/<slug>.chainTvls.<chain>.tokensInUsd + /stablecoincharts/all",
        "methodology": (f"top {TOP_N} stable-heavy protocols (TVL>${MIN_TVL/1e6:.0f}M) in "
                       f"{sorted(STABLE_CATEGORIES)}, cross-chain aggregation, forward-fill per protocol"),
    }
    js = "/* Auto-generated by fetch_defi_engagement.py — do not edit. */\n"
    js += "(function(){var d=" + json.dumps(payload, separators=(",", ":"))
    js += ";window.__DEFI_ENGAGEMENT__=d;})();\n"
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write(js)
    import os
    print(f"[defi_engagement] OK -> {OUT_JS} ({os.path.getsize(OUT_JS)/1024:.1f} KB)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
