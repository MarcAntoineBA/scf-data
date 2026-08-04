#!/usr/bin/env python3
"""Fetch historique weekly des P/E crypto via DefiLlama.
Calcule P/E = mcap / (revenue_annualise) pour les protocoles avec buyback prouve.
Injecte dans Comparaison_PER_Crypto_TradFi.html via les markers __CRYPTO_PE_HIST__.
"""
import json, sys, warnings, time
from pathlib import Path
from datetime import datetime, timedelta
import urllib.request

warnings.filterwarnings('ignore')

# Cache écrit dans Library/Caches (accessible depuis LaunchAgent sans TCC)
# Desktop/crypto_pe_hist_cache.json est un symlink vers ce fichier
CACHES_DIR = Path.home() / "Library/Caches/site_crypto_finance"
CACHES_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHES_DIR / "crypto_pe_hist_cache.json"
# HTML : injection optionnelle — skip silencieusement si path pas accessible (LaunchAgent TCC)
HTML_FILE = Path.home() / "Desktop/Site_Crypto_Finance/Comparaison_PER_Crypto_TradFi.html"
CACHE_MAX_HOURS = 6  # refresh 4x/jour

# Protocoles suivis : slug DefiLlama + coin DefiLlama (coingecko:X)
# Pour mcap, on utilise price DefiLlama × current supply (approximation)
PROTOCOLS = [
    {"name": "Sky",         "llama": "sky",         "coin": "coingecko:maker",                     "supply": 881000,      "color": "#34d399"},
    {"name": "Aave",        "llama": "aave",        "coin": "coingecko:aave",                      "supply": 15100000,    "color": "#5eaff6"},
    {"name": "Hyperliquid", "llama": "hyperliquid", "coin": "coingecko:hyperliquid",               "supply": 333900000,   "color": "#50F0C0"},
    {"name": "Jupiter",     "llama": "jupiter",     "coin": "coingecko:jupiter-exchange-solana",   "supply": 3000000000,  "color": "#fbbf24"},
]

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def fetch_llama_revenue(slug):
    """Revenue quotidienne historique d'un protocole DefiLlama.
    Retourne [(timestamp_ms, revenue_usd), ...]"""
    try:
        url = f"https://api.llama.fi/summary/fees/{slug}?dataType=dailyRevenue"
        d = http_get(url)
        if "totalDataChart" in d and d["totalDataChart"]:
            # DefiLlama renvoie timestamps en SECONDES → convertir en millisecondes
            return [(int(t) * 1000, float(v)) for t, v in d["totalDataChart"] if v]
    except Exception as e:
        sys.stderr.write(f'{slug} llama err: {e}\n')
    return []

def fetch_llama_prices(coin, supply, years=5, period="1w"):
    """Prix depuis DefiLlama Coins API, converti en mcap via supply actuel.
    period='1w' (weekly) ou '1d' (daily).
    L'API plafonne le span a ~1000 weekly / ~500 daily.
    Pour daily, si le start est trop ancien pour un coin recent, l'API renvoie 400.
    On utilise donc years=2 pour daily (couvre ~500j sans depasser l'histoire du coin).
    """
    try:
        # Daily : fenetre plus courte (2 ans) pour respecter l'API
        eff_years = years if period == "1w" else min(years, 2)
        start = int(datetime.now().timestamp()) - eff_years * 365 * 86400
        if period == "1w":
            span = min(eff_years * 52, 1000)
        else:
            span = min(eff_years * 365, 500)
        url = f"https://coins.llama.fi/chart/{coin}?start={start}&period={period}&span={span}"
        d = http_get(url)
        coins_data = d.get("coins", {})
        if coin not in coins_data: return []
        prices = coins_data[coin].get("prices", [])
        return [[int(p["timestamp"]) * 1000, float(p["price"]) * supply] for p in prices if p.get("price")]
    except Exception as e:
        sys.stderr.write(f'{coin} llama price err ({period}): {e}\n')
    return []

def compute_pe_series(mcaps, revenues_daily, min_spacing_days=6):
    """Calcule P/E = mcap / (somme revenue 30j * 365/30).
    min_spacing_days : espacement mini entre deux points (6 pour weekly, 0 pour daily).
    """
    if not mcaps or not revenues_daily:
        return {"dates": [], "pe": []}
    rev_by_day = {}
    for ts_ms, v in revenues_daily:
        day = ts_ms - (ts_ms % 86400000)
        rev_by_day[day] = rev_by_day.get(day, 0) + v
    dates, pes = [], []
    last_ts = 0
    spacing_ms = min_spacing_days * 86400000
    for ts_ms, mcap in mcaps:
        if ts_ms - last_ts < spacing_ms:
            continue
        last_ts = ts_ms
        cutoff = ts_ms - 30 * 86400000
        rev_30d = sum(v for day, v in rev_by_day.items() if cutoff <= day <= ts_ms)
        if rev_30d <= 0 or mcap <= 0:
            continue
        annualized = rev_30d * (365 / 30)
        pe = mcap / annualized
        if 0 < pe < 500:
            d = datetime.fromtimestamp(ts_ms / 1000).strftime('%Y-%m-%d')
            dates.append(d)
            pes.append(round(pe, 1))
    return {"dates": dates, "pe": pes}

def fetch():
    # Cache check
    if CACHE_FILE.exists():
        age_h = (datetime.now().timestamp() - CACHE_FILE.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS:
            sys.stderr.write(f"[Crypto PE Hist] Cache fresh ({age_h:.1f}h)\n")
            payload = json.load(open(CACHE_FILE))
            inject_into_html(payload)
            return payload

    sys.stderr.write("[Crypto PE Hist] Fetching live data...\n")
    result_w = {}   # weekly (backward compat)
    result_d = {}   # daily (nouveau)
    for p in PROTOCOLS:
        sys.stderr.write(f'{p["name"]}: fetching...\n')
        revenues = fetch_llama_revenue(p["llama"])
        time.sleep(0.3)
        mcaps_w = fetch_llama_prices(p["coin"], p["supply"], period="1w")
        time.sleep(0.3)
        mcaps_d = fetch_llama_prices(p["coin"], p["supply"], period="1d")
        time.sleep(0.3)
        if revenues and (mcaps_w or mcaps_d):
            series_w = compute_pe_series(mcaps_w, revenues, min_spacing_days=6) if mcaps_w else {"dates":[],"pe":[]}
            series_d = compute_pe_series(mcaps_d, revenues, min_spacing_days=0) if mcaps_d else {"dates":[],"pe":[]}
            if series_w["dates"]:
                result_w[p["name"]] = {**series_w, "color": p["color"], "points": len(series_w["dates"])}
            if series_d["dates"]:
                result_d[p["name"]] = {**series_d, "color": p["color"], "points": len(series_d["dates"])}
            sys.stderr.write(f'{p["name"]}: weekly={len(series_w["dates"])}, daily={len(series_d["dates"])}\n')
        else:
            sys.stderr.write(f'{p["name"]}: missing data (rev={len(revenues)}, mcaps_w={len(mcaps_w)}, mcaps_d={len(mcaps_d)})\n')

    payload = {"updated": datetime.now().isoformat(), "data": result_w, "data_daily": result_d}
    with open(CACHE_FILE, 'w') as f:
        json.dump(payload, f)
    sys.stderr.write(f"[Crypto PE Hist] Wrote weekly={len(result_w)} / daily={len(result_d)} protocols to {CACHE_FILE}\n")
    inject_into_html(payload)
    return payload

def inject_into_html(payload):
    """Injecte via les markers — tolérant aux erreurs TCC (LaunchAgent sans accès Desktop)."""
    import re
    try:
        if not HTML_FILE.exists():
            sys.stderr.write(f'[Crypto PE Hist] {HTML_FILE} introuvable, skip\n'); return
        html = HTML_FILE.read_text()
        new_block = (
            "// __CRYPTO_PE_HIST_START__\n"
            "window.__CRYPTO_PE_HIST__ = " + json.dumps(payload, separators=(',',':')) + ";\n"
            "// __CRYPTO_PE_HIST_END__"
        )
        pattern = re.compile(r"// __CRYPTO_PE_HIST_START__.*?// __CRYPTO_PE_HIST_END__", re.DOTALL)
        if not pattern.search(html):
            sys.stderr.write('[Crypto PE Hist] markers absents, skip\n'); return
        html2 = pattern.sub(new_block, html)
        HTML_FILE.write_text(html2)
        sys.stderr.write(f'[Crypto PE Hist] Injected into {HTML_FILE.name}\n')
    except (PermissionError, OSError) as e:
        sys.stderr.write(f'[Crypto PE Hist] HTML injection skipped (TCC or OS): {e}\n')
        sys.stderr.write('[Crypto PE Hist] Browser will fetch cache JSON via symlink instead\n')

if __name__ == '__main__':
    # --inject-only : relit le cache existant et re-injecte le HTML sans refetch
    if '--inject-only' in sys.argv:
        if CACHE_FILE.exists():
            payload = json.load(open(CACHE_FILE))
            inject_into_html(payload)
        else:
            sys.stderr.write('[Crypto PE Hist] --inject-only : cache manquant\n')
    else:
        fetch()
