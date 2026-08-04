#!/usr/bin/env python3
"""Fetch P/E data live (DeFiLlama + CoinGecko + StockAnalysis + Multpl).
Ecrit per_data_cache.json + injecte dans Comparaison_PER_Crypto_TradFi.html
entre les markers __PER_DATA_LIVE_START__ / __PER_DATA_LIVE_END__.
"""
# ── Global timeout safeguard (30 min) — auto-tué si bloqué sur un I/O réseau,
#    libère le lock pour le prochain cycle launchd. Sans ça, un script bloqué
#    monopolise indéfiniment le verrou et empêche tous les refresh suivants.
import signal as _signal, sys as _sys
def _global_timeout_handler(signum, frame):
    print(f"[fatal] global timeout (30 min) reached — aborting to free lock for next launchd cycle.", file=_sys.stderr)
    _sys.exit(2)
try:
    _signal.signal(_signal.SIGALRM, _global_timeout_handler)
    _signal.alarm(30 * 60)
except Exception:
    pass

import requests, json, sys, re
from pathlib import Path
from datetime import datetime
from bs4 import BeautifulSoup

# Le cache est centralisé dans ~/Library/Caches/site_crypto_finance/ (les
# autres fetch_*.py utilisent ce dossier ; les symlinks Desktop pointent ici).
# Sans ça, le script écrivait à côté de lui-même (Application Support) alors
# que les symlinks Desktop pointaient vers Library/Caches → cache "périmé"
# permanent côté HTML, alors que la donnée fraîche était orpheline.
CACHE_DIR  = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "per_data_cache.json"
JS_FILE    = CACHE_DIR / "per_data_cache.js"
HTML_FILE  = Path(__file__).parent / "Comparaison_PER_Crypto_TradFi.html"
CACHE_MAX_HOURS = 12

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0"
HEADERS = {"User-Agent": UA}

# ──────────────────────────────────────────────────────────
# Fetchers
# ──────────────────────────────────────────────────────────
def fetch_llama_rev():
    try:
        r = requests.get("https://api.llama.fi/overview/fees?dataType=dailyRevenue&excludeTotalDataChartBreakdown=true&excludeTotalDataChart=true", timeout=30)
        return r.json().get("protocols", []) if r.status_code == 200 else []
    except Exception as e:
        sys.stderr.write(f"[PER] llama_rev fetch failed: {type(e).__name__}: {e}\n")
        return []

def fetch_llama_fees():
    try:
        r = requests.get("https://api.llama.fi/overview/fees?excludeTotalDataChartBreakdown=true&excludeTotalDataChart=true", timeout=30)
        return r.json().get("protocols", []) if r.status_code == 200 else []
    except Exception as e:
        sys.stderr.write(f"[PER] llama_fees fetch failed: {type(e).__name__}: {e}\n")
        return []

def fetch_cg_markets():
    ids = "ethereum,uniswap,hyperliquid,curve-dao-token,binancecoin,aave,sky,jupiter-exchange-solana,gmx,havven,dydx-chain"
    r = requests.get(f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={ids}&order=market_cap_desc&per_page=20&sparkline=false", timeout=30)
    return r.json() if r.status_code == 200 else []

def fetch_pe_tradfi(slug):
    try:
        r = requests.get(f"https://stockanalysis.com/stocks/{slug}/", headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        # Find the row whose first cell starts with "PE Ratio"
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if not tds:
                    continue
                label = tds[0].get_text(strip=True)
                if label.startswith("PE Ratio") and len(tds) > 1:
                    raw = tds[1].get_text(strip=True)
                    val = re.sub(r"[^0-9.]", "", raw)
                    return float(val) if val else None
        return None
    except Exception as e:
        sys.stderr.write(f"[{slug}] error: {e}\n")
        return None

def fetch_sp500_pe():
    try:
        r = requests.get("https://www.multpl.com/s-p-500-pe-ratio", headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return 28.5
        soup = BeautifulSoup(r.text, "html.parser")
        cur = soup.find(id="current-value")
        if not cur:
            return 28.5
        val = re.sub(r"[^0-9.]", "", cur.get_text())
        return float(val) if val else 28.5
    except Exception:
        return 28.5

# ──────────────────────────────────────────────────────────
# Build crypto rows
# ──────────────────────────────────────────────────────────
PROTOS = [
    {"name":"Ethereum",   "sym":"ETH",  "cg":"eth",  "rev":None,                    "fees":r"^Ethereum$",   "mech":"EIP-1559 burn"},
    {"name":"Uniswap",    "sym":"UNI",  "cg":"uni",  "rev":"Uniswap V3",            "fees":None,            "mech":"Fee switch partiel"},
    {"name":"Hyperliquid","sym":"HYPE", "cg":"hype", "rev":"Hyperliquid Perps",     "fees":None,            "mech":"Assist. Fund burn"},
    {"name":"Curve",      "sym":"CRV",  "cg":"crv",  "rev":r"^Curve DEX$",          "fees":None,            "mech":"50% frais (veCRV)"},
    {"name":"BNB Chain",  "sym":"BNB",  "cg":"bnb",  "rev":None,                    "fees":r"^BSC$",        "mech":"Auto-burn trim."},
    {"name":"Aave",       "sym":"AAVE", "cg":"aave", "rev":"Aave V3",               "fees":None,            "mech":"$50M/an DAO"},
    {"name":"Sky",        "sym":"SKY",  "cg":"sky",  "rev":"Sky Lending",           "fees":None,            "mech":"DSR + governance"},
    {"name":"Jupiter",    "sym":"JUP",  "cg":"jup",  "rev":"Jupiter Aggregator",    "fees":None,            "mech":"50% rev lock 3 ans"},
    {"name":"GMX",        "sym":"GMX",  "cg":"gmx",  "rev":"GMX V2 Perps",          "fees":None,            "mech":"27% frais -> rachat"},
    {"name":"Synthetix",  "sym":"SNX",  "cg":"snx",  "rev":"Synthetix",             "fees":None,            "mech":"100% frais (2026)"},
    {"name":"dYdX",       "sym":"DYDX", "cg":"dydx", "rev":"dYdX V4",               "fees":None,            "mech":"75% frais"},
]

TRADFI_LIST = [
    ("aapl",  "Apple",     "7 Magnifiques"),
    ("nvda",  "NVIDIA",    "7 Magnifiques"),
    ("msft",  "Microsoft", "7 Magnifiques"),
    ("tsla",  "Tesla",     "7 Magnifiques"),
    ("googl", "Alphabet",  "7 Magnifiques"),
    ("meta",  "Meta",      "7 Magnifiques"),
    ("amzn",  "Amazon",    "7 Magnifiques"),
    ("baba",  "Alibaba",   "BATX"),
    ("tcehy", "Tencent",   "BATX"),
    ("bidu",  "Baidu",     "BATX"),
]

def sum_rev(pattern, df):
    total = 0.0
    found = False
    rgx = re.compile(pattern, re.IGNORECASE)
    for p in df:
        if rgx.search(p.get("name", "")):
            v = p.get("total1y")
            if v is not None:
                total += float(v)
                found = True
    return total if found else None

def build_crypto(llama_rev, llama_fees, cg):
    rows = []
    cg_by_sym = {c.get("symbol", "").lower(): c for c in cg}
    for p in PROTOS:
        rev_1y = None
        if p["rev"]:
            rev_1y = sum_rev(p["rev"], llama_rev)
        elif p["fees"] and llama_fees:
            rev_1y = sum_rev(p["fees"], llama_fees)
        if not rev_1y or rev_1y <= 0:
            continue
        cr = cg_by_sym.get(p["cg"].lower())
        if not cr:
            continue
        mcap = cr.get("market_cap") or 0
        fdv  = cr.get("fully_diluted_valuation") or mcap
        if not mcap or mcap <= 0:
            continue
        if not fdv or fdv <= 0:
            fdv = mcap
        circ_pct = round(100 * mcap / fdv)
        pe_mc  = round(mcap / rev_1y, 1)
        pe_fdv = round(fdv  / rev_1y, 1)
        if pe_mc <= 0 or pe_mc > 5000:
            continue
        rows.append({
            "name": p["name"], "sym": p["sym"],
            "mcap_b": round(mcap/1e9, 1), "fdv_b": round(fdv/1e9, 1),
            "circ_pct": circ_pct, "rev_m": round(rev_1y/1e6),
            "pe_mc": pe_mc, "pe_fdv": pe_fdv, "mech": p["mech"]
        })
    return rows

def build_tradfi():
    rows = []
    for slug, name, cat in TRADFI_LIST:
        pe = fetch_pe_tradfi(slug)
        if pe is None:
            sys.stderr.write(f"[{slug}] no PE\n")
            continue
        rows.append({"name": name, "pe": pe, "cat": cat})
        sys.stderr.write(f"[{slug}] {name}: {pe}x\n")
    return rows

# ──────────────────────────────────────────────────────────
# Inject into HTML
# ──────────────────────────────────────────────────────────
def inject_into_html(payload):
    if not HTML_FILE.exists():
        sys.stderr.write(f"[PER] {HTML_FILE} not found, skip injection\n")
        return
    html = HTML_FILE.read_text()
    new_block = (
        "// __PER_DATA_LIVE_START__\n"
        "window.__PER_DATA_LIVE__ = " + json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + ";\n"
        "// __PER_DATA_LIVE_END__"
    )
    pattern = re.compile(r"// __PER_DATA_LIVE_START__.*?// __PER_DATA_LIVE_END__", re.DOTALL)
    if not pattern.search(html):
        sys.stderr.write("[PER] markers not found in HTML, skip\n")
        return
    html2 = pattern.sub(new_block, html)
    HTML_FILE.write_text(html2)
    sys.stderr.write(f"[PER] Injected into {HTML_FILE.name}\n")

# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────
def main():
    if CACHE_FILE.exists():
        age_h = (datetime.now().timestamp() - CACHE_FILE.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS and "--force" not in sys.argv:
            sys.stderr.write(f"[PER] Cache fresh ({age_h:.1f}h)\n")
            return

    sys.stderr.write("[PER] Fetching DeFiLlama revenue...\n")
    llama_rev = fetch_llama_rev()
    sys.stderr.write(f"[PER] {len(llama_rev)} revenue entries\n")

    sys.stderr.write("[PER] Fetching DeFiLlama fees...\n")
    llama_fees = fetch_llama_fees()
    sys.stderr.write(f"[PER] {len(llama_fees)} fees entries\n")

    sys.stderr.write("[PER] Fetching CoinGecko markets...\n")
    cg = fetch_cg_markets()
    sys.stderr.write(f"[PER] {len(cg)} CG entries\n")

    # ── If ALL external sources failed (DNS down, network outage, API blocks),
    #    bail out gracefully WITHOUT overwriting the previous cache. The site
    #    will keep showing stale data — better than empty/zero values.
    if not llama_rev and not llama_fees and not cg:
        sys.stderr.write("[PER] All external sources failed (DNS/network ?) — keeping previous cache untouched.\n")
        sys.exit(0)

    crypto = build_crypto(llama_rev, llama_fees, cg)
    sys.stderr.write(f"[PER] Built {len(crypto)} crypto rows\n")

    # If crypto build returned nothing AND we have a previous cache, keep
    # the prev crypto rows (so partial failures don't wipe legitimate data).
    if not crypto and CACHE_FILE.exists():
        try:
            prev = json.load(open(CACHE_FILE, "r"))
            if prev.get("crypto"):
                crypto = prev["crypto"]
                sys.stderr.write(f"[PER] crypto build empty — reusing {len(crypto)} prev rows\n")
        except Exception:
            pass

    sys.stderr.write("[PER] Fetching TradFi PEs...\n")
    try:
        tradfi = build_tradfi()
    except Exception as e:
        sys.stderr.write(f"[PER] tradfi build failed: {type(e).__name__}: {e}\n")
        tradfi = []
    sys.stderr.write(f"[PER] Built {len(tradfi)} tradfi rows\n")
    if not tradfi and CACHE_FILE.exists():
        try:
            prev = json.load(open(CACHE_FILE, "r"))
            if prev.get("tradfi"):
                tradfi = prev["tradfi"]
                sys.stderr.write(f"[PER] tradfi build empty — reusing {len(tradfi)} prev rows\n")
        except Exception:
            pass

    try:
        sp500_pe = fetch_sp500_pe()
    except Exception as e:
        sys.stderr.write(f"[PER] sp500_pe fetch failed: {type(e).__name__}: {e}\n")
        sp500_pe = None
    sys.stderr.write(f"[PER] S&P 500 P/E: {sp500_pe}\n")
    if sp500_pe is None and CACHE_FILE.exists():
        try:
            prev = json.load(open(CACHE_FILE, "r"))
            sp500_pe = prev.get("sp500_pe")
        except Exception:
            pass

    payload = {
        "crypto": crypto,
        "tradfi": tradfi,
        "sp500_pe": sp500_pe,
        "updated": datetime.now().strftime("%d/%m/%Y %H:%M")
    }
    with open(CACHE_FILE, "w") as f:
        json.dump(payload, f, ensure_ascii=False)
    sys.stderr.write(f"[PER] Wrote cache to {CACHE_FILE}\n")

    # Compagnon .js consommé par la page via document.write — permet l'override
    # live sans re-render du HTML.
    try:
        with open(JS_FILE, "w") as f:
            f.write("window.__PER_DATA_LIVE__ = ")
            json.dump(payload, f, ensure_ascii=False)
            f.write(";\n")
        sys.stderr.write(f"[PER] Wrote JS sibling to {JS_FILE}\n")
    except Exception as e:
        sys.stderr.write(f"[PER] failed to write JS sibling: {e}\n")

    inject_into_html(payload)

if __name__ == "__main__":
    main()
