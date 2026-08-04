"""Fetcher TradFi Allocation Index — % patrimoine financier menages US en actions.

Methodologie RIGOUREUSE (revision 2026-06-03 apres double audit) :
Utilise le % PUBLIE par la Fed (BOGZ1FL153064486Q), c'est l'indicateur authoritative
Philosophical Economics / Hussman avec look-through complet vers mutual funds
+ pension equity holdings. Tenter de reproduire le ratio en divisant les series
dollar Z.1 brutes donne des chiffres ABERRANTS (1945 = 52.8% au lieu de 22.5%
publie) parce que le numerateur dollar Z.1 inclut des indirects que le denominateur
n'a pas dans la meme proportion. CONCLUSION : on prend le % publie tel quel, et
les KPIs dollar sont du CONTEXTE non-reproducible (avec caveat explicite).

Series :
  - BOGZ1FL153064486Q : % publie (numerateur ratio, AUTHORITATIVE)
  - BOGZ1FL153081105Q : equity holdings $ (CONTEXTE)
  - TFAABSHNO         : total financial assets $ (CONTEXTE)
  - Multpl.com Shiller : S&P 500 mensuel 1871+ (overlay)

Sortie : ~/Desktop/Site_Crypto_Finance/tradfi_allocation_cache.js
expose window.__TRADFI_ALLOCATION__ = {
  current: {date, ratio_pct, equity_t, total_t, percentile, implied_10y_pct},
  history: [{d, u, eq, tot}, ...]  (trimestriel 1945+),
  sp500_hist: [{d, p}, ...]  (mensuel 1945+)
}
"""
import html as html_module
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from _fred_helpers import fetch_fred

OUT_JS = os.path.expanduser("~/Desktop/Site_Crypto_Finance/tradfi_allocation_cache.js")


def to_ts(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())


def lookup_by_date(series, target_date):
    """Trouve la valeur exacte (meme date) ou la plus proche dans serie FRED."""
    if not series:
        return None
    target = to_ts(target_date)
    best = None
    best_diff = float("inf")
    for d, v in zip(series["dates"], series["values"]):
        if v in (None, ".", ""):
            continue
        diff = abs(to_ts(d) - target)
        if diff < best_diff:
            best_diff = diff
            try:
                best = float(v)
            except ValueError:
                pass
    return best


def fetch_multpl_sp500():
    url = "https://www.multpl.com/s-p-500-historical-prices/table/by-month"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html_text = urlopen(req, timeout=20).read().decode("utf-8", errors="replace")
    m = re.search(r'<table[^>]*id="datatable"[^>]*>(.*?)</table>', html_text, re.S)
    if not m:
        return []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S)
    out = []
    for r in rows:
        cells = re.findall(r"<td[^>]*>([^<]+)</td>", r)
        if len(cells) < 2:
            continue
        date_str = cells[0].strip()
        try:
            dt = datetime.strptime(date_str, "%b %d, %Y")
        except ValueError:
            try:
                dt = datetime.strptime(date_str, "%B %d, %Y")
            except ValueError:
                continue
        cell_text = html_module.unescape(cells[1])
        v_raw = re.sub(r"[^\d.\-]", "", cell_text)
        if not v_raw or v_raw in (".", "-"):
            continue
        try:
            v = float(v_raw)
        except ValueError:
            continue
        if v <= 0 or v > 1e6:
            continue
        out.append({"d": int(dt.timestamp()), "p": v})
    out.sort(key=lambda x: x["d"])
    return out


def main():
    # AUTHORITATIVE : % publie Fed (Philosophical Economics indicator)
    print("[tradfi_allocation] Fetch FRED published % (BOGZ1FL153064486Q)...", file=sys.stderr)
    alloc_pct = fetch_fred("BOGZ1FL153064486Q")
    print("[tradfi_allocation] Fetch FRED equity assets $ (BOGZ1FL153081105Q, contexte)...", file=sys.stderr)
    eq_assets = fetch_fred("BOGZ1FL153081105Q")
    print("[tradfi_allocation] Fetch FRED total financial assets $ (TFAABSHNO, contexte)...", file=sys.stderr)
    tot_fin = fetch_fred("TFAABSHNO")

    if not alloc_pct:
        print("[tradfi_allocation] ERROR: missing alloc % data", file=sys.stderr)
        sys.exit(1)

    # Build joined history sur le % PUBLIE (authoritative)
    history = []
    for d, v in zip(alloc_pct["dates"], alloc_pct["values"]):
        if v in (None, ".", ""):
            continue
        try:
            ratio = float(v)
        except ValueError:
            continue
        eq_v = lookup_by_date(eq_assets, d) if eq_assets else None
        tot_v = lookup_by_date(tot_fin, d) if tot_fin else None
        history.append({
            "d": to_ts(d),
            "u": round(ratio, 3),                      # % publie (AUTHORITATIVE)
            "eq": int(round(eq_v)) if eq_v else None,  # $M equity (contexte)
            "tot": int(round(tot_v)) if tot_v else None,  # $M total fin assets (contexte)
        })

    if not history:
        print("[tradfi_allocation] ERROR: empty joined history", file=sys.stderr)
        sys.exit(1)

    last = history[-1]
    sorted_vals = sorted(p["u"] for p in history)
    percentile = 100.0 * sum(1 for v in sorted_vals if v <= last["u"]) / len(sorted_vals)

    print(f"[tradfi_allocation] history: {len(history)} quarters, "
          f"{datetime.fromtimestamp(history[0]['d']).strftime('%Y-Q')}"
          f"{(datetime.fromtimestamp(history[0]['d']).month - 1) // 3 + 1} → "
          f"{datetime.fromtimestamp(last['d']).strftime('%Y-Q')}"
          f"{(datetime.fromtimestamp(last['d']).month - 1) // 3 + 1}",
          file=sys.stderr)
    print(f"[tradfi_allocation] latest: {last['u']:.2f}% (equity ${last['eq']/1e6:.2f}T / total ${last['tot']/1e6:.2f}T) "
          f"- percentile {percentile:.1f}%",
          file=sys.stderr)

    # S&P 500 overlay
    print("[tradfi_allocation] Fetch Shiller S&P 500 via multpl...", file=sys.stderr)
    try:
        sp500 = fetch_multpl_sp500()
    except Exception as e:
        print(f"[tradfi_allocation] WARN multpl failed: {e}", file=sys.stderr)
        sp500 = []
    start_ts = history[0]["d"]
    sp500_filt = [p for p in sp500 if p["d"] >= start_ts]

    # === REGRESSION 10y return predit a partir de l'alloc ===
    # Pour chaque trimestre t : alloc_pct[t] et S&P 500 10 ans plus tard -> return annualise.
    # Fit lineaire simple : return = alpha + beta * alloc
    implied_10y = None
    reg_meta = None
    if sp500_filt:
        # Build sp500 lookup
        sp_dates = [p["d"] for p in sp500_filt]
        sp_vals = [p["p"] for p in sp500_filt]
        def sp_at(ts):
            if ts < sp_dates[0] or ts > sp_dates[-1]:
                return None
            idx = min(range(len(sp_dates)), key=lambda k: abs(sp_dates[k] - ts))
            return sp_vals[idx]
        SEC_10Y = int(10 * 365.25 * 86400)
        xs = []  # alloc
        ys = []  # 10y annualized return
        for p in history:
            sp0 = sp_at(p["d"])
            sp10 = sp_at(p["d"] + SEC_10Y)
            if not sp0 or not sp10 or sp0 <= 0:
                continue
            ann = (sp10 / sp0) ** (1.0 / 10) - 1
            xs.append(p["u"])
            ys.append(ann * 100)  # %
        if len(xs) >= 30:
            n = len(xs)
            mx = sum(xs) / n
            my = sum(ys) / n
            sxx = sum((x - mx) ** 2 for x in xs)
            sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
            beta = sxy / sxx if sxx else 0
            alpha = my - beta * mx
            implied_10y = round(alpha + beta * last["u"], 2)
            # R (correlation coefficient)
            syy = sum((y - my) ** 2 for y in ys)
            r = sxy / ((sxx * syy) ** 0.5) if sxx and syy else 0
            reg_meta = {
                "alpha": round(alpha, 3), "beta": round(beta, 4),
                "r": round(r, 3), "n_obs": n,
                "formula": f"return_10y_annualise = {alpha:.2f} + ({beta:.3f}) * alloc_pct"
            }
            print(f"[tradfi_allocation] regression 10y: r={r:.3f}, n={n}, "
                  f"return_pred @ {last['u']:.1f}% = {implied_10y:+.2f}%/an",
                  file=sys.stderr)

    payload = {
        "current": {
            "date": last["d"],
            "ratio_pct": round(last["u"], 3),
            "equity_t": round(last["eq"] / 1e6, 3) if last["eq"] else None,
            "total_t": round(last["tot"] / 1e6, 3) if last["tot"] else None,
            "percentile": round(percentile, 1),
            "implied_10y_pct": implied_10y,  # rendement S&P 500 annualise predit sur 10y
        },
        "regression": reg_meta,
        "history": history,
        "sp500_hist": sp500_filt,
        "generated_at": int(time.time()),
        "source": "FRED BOGZ1FL153064486Q (% publie AUTHORITATIVE) + BOGZ1FL153081105Q (equity $) + TFAABSHNO (total fin assets $) + multpl.com Shiller",
        "methodology": (
            "% publie Fed Z.1 (indicateur Philosophical Economics 2013, look-through "
            "complet mutual funds + pensions). KPIs dollar = series Z.1 brutes pour "
            "contexte (le ratio direct equity/total_fin ne reproduit PAS le % publie a "
            "cause d'inclusions look-through). Regression 10y forward S&P = predicteur "
            "de rendement long-terme (r affiche)."
        ),
    }

    js = "/* Auto-generated by fetch_tradfi_allocation.py — do not edit. */\n"
    js += "(function(){var d=" + json.dumps(payload, separators=(",", ":"))
    js += ";window.__TRADFI_ALLOCATION__=d;})();\n"
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write(js)
    print(f"[tradfi_allocation] OK -> {OUT_JS} ({os.path.getsize(OUT_JS)/1024:.1f} KB)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
