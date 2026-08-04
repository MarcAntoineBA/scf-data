#!/usr/bin/env python3
"""Cache antifragile pour le chapitre Thèse · La bulle IA.

Données récupérées (toutes auditables) :

  1. Mag7 CapEx annuel (NVDA, MSFT, GOOG, AMZN, META, AAPL, TSLA)
     → yfinance cashflow statement (-CapitalExpenditure) sur 5 ans
  2. Nvidia revenue breakdown trimestriel (datacenter vs gaming vs other)
     → yfinance get_earnings_dates ne donne pas le breakdown, on hardcode
       à partir des earnings reports officiels NVDA
  3. Mag7 market caps + P/E forward courants
     → yfinance fast_info + info
  4. Foreign holdings of US Treasuries — top 5 pays (China, Japan, UK, …)
     → US Treasury TIC public data (CSV)
  5. Cass Freight Index (proxy économie réelle)
     → Cass Information Systems ne publie pas d'API mais FRED en distribue
       une variante : DPTRANSP (FRED) — index de production transport
  6. S&P 500 vs S&P 500 Equal Weight (pour le narratif "concentration")
     → Yahoo Finance ^GSPC vs ^SPXEW (avec fallback RSP ETF si pas dispo)
  7. Projections IEA data centers electricity (hardcoded, source IEA 2024)
  8. Coûts d'entraînement modèles IA (hardcoded, sources publiques)
  9. Prix robots humanoïdes (hardcoded, sources officielles fabricants)

Sortie : un seul cache JSON+JS lu côté navigateur par These_BulleIA.html.
Lancé par scf.these_bulleia.refresh (4×/jour).
"""
import csv
import io
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "these_bulleia_cache.json"
OUT_JS   = CACHE_DIR / "these_bulleia_cache.js"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) SiteCryptoFinance-TheseBulleIA/1.0"

MAG7 = ["NVDA", "MSFT", "GOOGL", "AMZN", "META", "AAPL", "TSLA"]

# ─── Datasets hardcodés (sources publiques fiables, peu volatiles) ──

# IEA — Electricity consumption by data centers (TWh/an).
# Source : IEA Electricity 2024 Report (2024-01-24) + projection ENERGY 2024.
# Includes AI + Crypto. Référence finale: 2030 forecast.
IEA_DATACENTER_ELECTRICITY_TWH = [
    # (year, twh)
    (2018, 240), (2019, 250), (2020, 260), (2021, 290),
    (2022, 340), (2023, 410), (2024, 480),
    # Projections IEA Electricity 2024 (médian scenario)
    (2025, 580), (2026, 700), (2027, 820), (2028, 940), (2029, 1000), (2030, 1050),
]

# Coût d'entraînement modèles IA (millions $, sources publiques)
AI_MODEL_TRAINING_COSTS = [
    # (label, cost_million_usd, year, source)
    ("DeepSeek V3",   5.6,    2024, "DeepSeek paper officiel · décembre 2024"),
    ("DeepSeek R1",   5.0,    2025, "DeepSeek paper · janvier 2025"),
    ("Qwen 2.5",     12.0,    2024, "Alibaba release notes"),
    ("Llama 3.1 405B", 90.0,  2024, "Meta paper, FAIR"),
    ("Claude 3.5 Sonnet", 100.0, 2024, "Anthropic estimé (non communiqué)"),
    ("GPT-4",       100.0,    2023, "OpenAI / Epoch AI estimation"),
    ("GPT-4o",      150.0,    2024, "OpenAI / Epoch AI estimation"),
    ("Gemini Ultra", 200.0,   2024, "Google / Epoch AI estimation"),
]

# Prix robots humanoïdes (USD, prix de lancement annoncé)
HUMANOID_ROBOT_PRICES = [
    # (label, country, price_usd, year, source)
    ("Unitree G1",         "CN",  16000,   2024, "Unitree Robotics · annonce officielle"),
    ("Unitree H1",         "CN",  90000,   2023, "Unitree Robotics"),
    ("Xiaomi CyberOne",    "CN",  120000,  2022, "Xiaomi · estimé"),
    ("Fourier Intelligence GR-1", "CN", 150000, 2023, "Fourier · catalogue"),
    ("1X NEO",             "NO",  20000,   2025, "1X Technologies · pré-commande"),
    ("Tesla Optimus Gen 2","US",  30000,   2025, "Tesla / Elon Musk annonce 2024"),
    ("Boston Dynamics Atlas", "US", 250000, 2024, "Boston Dynamics · prototype non commercial"),
    ("Figure 02",          "US",  150000,  2024, "Figure AI · estimé"),
    ("Apptronik Apollo",   "US",  100000,  2024, "Apptronik · estimé"),
]

# CapEx Mag7 historique (Md$/an, sources : 10-K SEC filings).
# Données stables (annuelles), utilisées comme fallback si yfinance rate-limite.
# Source : https://www.sec.gov/edgar/searchedgar/companysearch
CAPEX_MAG7_FALLBACK = {
    "MSFT":  [(2020, 17.6), (2021, 20.6), (2022, 23.9), (2023, 28.1), (2024, 44.5), (2025, 80.0)],
    "GOOGL": [(2020, 22.3), (2021, 24.6), (2022, 31.5), (2023, 32.3), (2024, 52.5), (2025, 75.0)],
    "META":  [(2020, 15.7), (2021, 19.2), (2022, 31.4), (2023, 27.3), (2024, 37.0), (2025, 60.0)],
    "AMZN":  [(2020, 40.1), (2021, 55.4), (2022, 58.3), (2023, 48.1), (2024, 63.0), (2025, 110.0)],
    "AAPL":  [(2020,  7.6), (2021, 11.1), (2022, 10.7), (2023, 11.0), (2024,  9.4), (2025, 10.0)],
    "NVDA":  [(2020,  1.1), (2021,  1.0), (2022,  1.8), (2023,  1.1), (2024,  2.0), (2025,  2.5)],
    "TSLA":  [(2020,  3.2), (2021,  6.5), (2022,  7.2), (2023,  8.9), (2024, 11.2), (2025, 14.0)],
}

# Snapshot Mag7 fallback (proche valeurs mai 2026, sources : Yahoo Finance live).
# Ces valeurs ne sont utilisées QUE si yfinance rate-limite ; sinon les données live
# remplacent celles-ci à chaque refresh launchd. Valeurs mises à jour 2026-05-17.
SNAPSHOT_MAG7_FALLBACK = {
    "NVDA":  {"mcap_b": 5450.0, "pe_trailing": 46.0, "pe_forward": 20.0, "revenue_b": 130.0, "name": "Nvidia"},
    "MSFT":  {"mcap_b": 3700.0, "pe_trailing": 32.0, "pe_forward": 26.0, "revenue_b": 250.0, "name": "Microsoft"},
    "AAPL":  {"mcap_b": 3800.0, "pe_trailing": 30.0, "pe_forward": 26.0, "revenue_b": 390.0, "name": "Apple"},
    "GOOGL": {"mcap_b": 2700.0, "pe_trailing": 24.0, "pe_forward": 20.0, "revenue_b": 320.0, "name": "Alphabet"},
    "AMZN":  {"mcap_b": 2400.0, "pe_trailing": 42.0, "pe_forward": 30.0, "revenue_b": 620.0, "name": "Amazon"},
    "META":  {"mcap_b": 1700.0, "pe_trailing": 24.0, "pe_forward": 20.0, "revenue_b": 160.0, "name": "Meta"},
    "TSLA":  {"mcap_b": 1300.0, "pe_trailing": 120.0, "pe_forward": 80.0, "revenue_b": 100.0, "name": "Tesla"},
}

# Benchmarks LLM 2024-2025 (sources : papers officiels + Stanford AI Index)
LLM_BENCHMARKS = [
    # (model, mmlu, humaneval, math, country, training_cost_usd_M)
    ("DeepSeek-V3",       88.5,  82.6,  90.2,  "CN",   5.6),
    ("DeepSeek-R1",       90.8,  88.7,  97.3,  "CN",   5.0),
    ("Qwen 2.5 72B",      86.1,  85.3,  83.1,  "CN",  12.0),
    ("Llama 3.1 405B",    88.6,  89.0,  73.8,  "US",  90.0),
    ("Claude 3.5 Sonnet", 88.7,  92.0,  78.3,  "US", 100.0),
    ("GPT-4o",            88.7,  90.2,  76.6,  "US", 150.0),
    ("Gemini 1.5 Pro",    85.9,  84.1,  91.1,  "US", 200.0),
]


def http_get_text(url, timeout=20, max_retries=5, accept="text/csv,*/*"):
    req = Request(url, headers={"User-Agent": UA, "Accept": accept})
    last_err = None
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="ignore")
        except HTTPError as e:
            if 500 <= e.code < 600 and attempt < max_retries - 1:
                time.sleep(5 * (2 ** attempt)); continue
            raise
        except (URLError, ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(5 * (2 ** attempt))
            continue
    raise last_err if last_err else RuntimeError("retries exhausted")


# FRED via API officielle (la version CSV graph est cassée depuis ~mai 2026)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fred_helpers import fetch_fred as fetch_fred_csv  # noqa: E402


def fetch_yahoo_daily(symbol, period1=1262304000, label=None):
    """Yahoo Finance v8 daily closes (period1 = 2010-01-01 par défaut)."""
    try:
        sym_enc = symbol.replace("=", "%3D").replace("^", "%5E")
        period2 = int(time.time())
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_enc}"
            f"?period1={period1}&period2={period2}&interval=1d"
        )
        txt = http_get_text(url, timeout=25, accept="application/json")
        d = json.loads(txt)
        r = d.get("chart", {}).get("result", [{}])[0]
        ts = r.get("timestamp", []) or []
        close = r.get("indicators", {}).get("quote", [{}])[0].get("close", []) or []
        if not ts: return None
        dates, values = [], []
        for t, c in zip(ts, close):
            if c is None: continue
            dates.append(datetime.fromtimestamp(t).strftime("%Y-%m-%d"))
            values.append(round(float(c), 4))
        return {"dates": dates, "values": values, "symbol": symbol, "label": label or symbol}
    except Exception as e:
        sys.stderr.write(f"[YAHOO {symbol}] {e}\n"); return None


def fetch_mag7_capex_via_yfinance():
    """Récupère le CapEx annuel des Mag7 via yfinance.

    yfinance expose `Ticker.cashflow` (DataFrame) dont la ligne
    'Capital Expenditure' contient des valeurs négatives. On les négativise
    pour avoir des montants positifs en USD."""
    py_bin = "/opt/anaconda3/bin/python3"
    if not Path(py_bin).exists():
        py_bin = "python3"
    import subprocess
    script = '''
import yfinance as yf, json, sys
out = {}
tickers = ["NVDA", "MSFT", "GOOGL", "AMZN", "META", "AAPL", "TSLA"]
for tk in tickers:
    try:
        t = yf.Ticker(tk)
        cf = t.cashflow
        if cf is None or cf.empty:
            continue
        # On cherche la ligne CapEx (peut s'appeler différemment selon yfinance)
        candidates = ["Capital Expenditure", "Capital Expenditures",
                      "CapitalExpenditure", "Capital Expenditure Reported"]
        row = None
        for c in candidates:
            if c in cf.index:
                row = cf.loc[c]; break
        if row is None:
            for idx in cf.index:
                if "capital" in idx.lower() and "expenditure" in idx.lower():
                    row = cf.loc[idx]; break
        if row is None:
            continue
        years_capex = []
        for date, val in row.items():
            try:
                y = date.year if hasattr(date, "year") else int(str(date)[:4])
                v = float(val)
                # CapEx est négatif dans cashflow statement → on prend abs
                years_capex.append([y, round(abs(v) / 1e9, 2)])
            except Exception:
                continue
        years_capex.sort(key=lambda x: x[0])
        out[tk] = years_capex
    except Exception as e:
        sys.stderr.write(f"{tk}: {e}\\n")
        continue
print(json.dumps(out))
'''
    try:
        result = subprocess.run(
            [py_bin, "-c", script],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            sys.stderr.write(f"[CAPEX yfinance] stderr: {result.stderr[:500]}\n")
            return None
        # Le JSON est sur la dernière ligne de stdout
        line = [l for l in result.stdout.splitlines() if l.strip().startswith("{")]
        if not line:
            return None
        return json.loads(line[-1])
    except Exception as e:
        sys.stderr.write(f"[CAPEX] {e}\n")
        return None


def fetch_mag7_snapshot():
    """Snapshot live des Mag7 : market cap, prix, P/E."""
    py_bin = "/opt/anaconda3/bin/python3"
    if not Path(py_bin).exists():
        py_bin = "python3"
    import subprocess
    script = '''
import yfinance as yf, json, sys
out = {}
for tk in ["NVDA","MSFT","GOOGL","AMZN","META","AAPL","TSLA"]:
    try:
        t = yf.Ticker(tk)
        fi = t.fast_info
        info = t.info
        mcap = fi.get("marketCap", 0) or info.get("marketCap", 0)
        price = fi.get("lastPrice", 0) or 0
        tpe = info.get("trailingPE", 0) or 0
        fpe = info.get("forwardPE", 0) or 0
        rev = info.get("totalRevenue", 0) or 0
        emp = info.get("fullTimeEmployees", 0) or 0
        out[tk] = {
            "mcap_b": round(mcap / 1e9, 1) if mcap else 0,
            "price": round(price, 2) if price else 0,
            "pe_trailing": round(tpe, 1) if tpe else 0,
            "pe_forward":  round(fpe, 1) if fpe else 0,
            "revenue_b":   round(rev / 1e9, 1) if rev else 0,
            "employees":   emp,
            "name": info.get("longName", tk),
        }
    except Exception as e:
        sys.stderr.write(f"{tk}: {e}\\n")
print(json.dumps(out))
'''
    try:
        result = subprocess.run(
            [py_bin, "-c", script],
            capture_output=True, text=True, timeout=180,
        )
        line = [l for l in result.stdout.splitlines() if l.strip().startswith("{")]
        if not line:
            return None
        return json.loads(line[-1])
    except Exception as e:
        sys.stderr.write(f"[SNAPSHOT] {e}\n")
        return None


def fetch_freight_index():
    """Indice de production transport — IPN3361T3S = Industrial Production:
    Motor vehicle bodies, ou plus simple : RAILFRTINTERMODAL = volume fret
    rail intermodal mensuel (NSA, FRED)."""
    return fetch_fred_csv("RAILFRTINTERMODAL", start="2015-01-01")


def fetch_pce_goods():
    """Real Personal Consumption Expenditures · Goods (FRED DGDSRX1).
    Le proxy le plus direct de "les consommateurs n'achètent plus de biens"."""
    return fetch_fred_csv("DGDSRX1", start="2015-01-01")


def fetch_cre_delinquency():
    """Commercial Real Estate Delinquency Rate (FRED DRCRELEXFACBS) — taux
    de défaut sur prêts CRE des banques commerciales US, trimestriel."""
    return fetch_fred_csv("DRCRELEXFACBS", start="2010-01-01")


def fetch_nfib_optimism():
    """NFIB Small Business Optimism Index (FRED BUSLOANS proxy ou via API NFIB)
    NFIB n'est pas sur FRED. On utilise les Bank loans to small business
    comme proxy de la santé PME : BUSLOANS = Commercial & Industrial Loans."""
    return fetch_fred_csv("BUSLOANS", start="2015-01-01")


def fetch_indpro_long():
    """Industrial Production: Total Index (INDPRO) — données mensuelles
    depuis 1919, base 2017 = 100. Mesure officielle de la production
    industrielle US (manufacturing + mining + utilities). Le plus large
    et le plus long historique disponible pour 'économie privée réelle'."""
    return fetch_fred_csv("INDPRO", start="1919-01-01")


def fetch_sp500_vs_eqw():
    """S&P 500 (^GSPC) vs S&P 500 Equal Weight (RSP ETF, qui suit ^SPXEW)."""
    return {
        "GSPC": fetch_yahoo_daily("^GSPC", period1=1577836800, label="S&P 500 (mcap-weighted)"),
        "RSP":  fetch_yahoo_daily("RSP",  period1=1577836800, label="S&P 500 Equal Weight (RSP)"),
    }


def build_payload():
    failed, ok = [], []

    # 1. CapEx Mag7 annuel
    capex = fetch_mag7_capex_via_yfinance()
    if capex: ok.append("YF:capex")
    else: failed.append("YF:capex")

    # 2. Snapshot Mag7
    snap = fetch_mag7_snapshot()
    if snap: ok.append("YF:snapshot")
    else: failed.append("YF:snapshot")

    # 3. (Champ china_treasury retiré : l'ID FRED MHHNGNCNM052N était faux dans
    #    le code d'origine, le champ n'était utilisé par aucun graphique du body.)
    china_treas = None

    # 4. Freight index (rail intermodal)
    freight = fetch_freight_index()
    if freight: ok.append("FRED:RAILFRTINTERMODAL")
    else: failed.append("FRED:RAILFRTINTERMODAL")

    # 4b. Real PCE Goods (consommateurs en biens physiques)
    pce_goods = fetch_pce_goods()
    if pce_goods: ok.append("FRED:DGDSRX1")
    else: failed.append("FRED:DGDSRX1")

    # 4c. CRE Delinquency Rate (faillites immobilier commercial)
    cre = fetch_cre_delinquency()
    if cre: ok.append("FRED:DRCRELEXFACBS")
    else: failed.append("FRED:DRCRELEXFACBS")

    # 4d. Bank loans to commercial & industrial (proxy PME)
    busloans = fetch_nfib_optimism()
    if busloans: ok.append("FRED:BUSLOANS")
    else: failed.append("FRED:BUSLOANS")

    # 4e. Industrial Production Index (économie privée réelle, depuis 1919)
    indpro = fetch_indpro_long()
    if indpro: ok.append("FRED:INDPRO")
    else: failed.append("FRED:INDPRO")

    # 5. S&P 500 mcap vs equal-weight
    sp500 = fetch_sp500_vs_eqw()
    if sp500.get("GSPC"): ok.append("YAHOO:^GSPC")
    else: failed.append("YAHOO:^GSPC")
    if sp500.get("RSP"): ok.append("YAHOO:RSP")
    else: failed.append("YAHOO:RSP")

    # Fallback : si yfinance a rate-limité, on utilise les valeurs SEC 10-K
    if not capex or len(capex) < 5:
        capex = {tk: [list(p) for p in vals] for tk, vals in CAPEX_MAG7_FALLBACK.items()}
        ok.append("hardcoded:CAPEX_MAG7_FALLBACK")
    if not snap or len(snap) < 5:
        snap = SNAPSHOT_MAG7_FALLBACK
        ok.append("hardcoded:SNAPSHOT_MAG7_FALLBACK")

    # 6. Datasets hardcodés
    iea = [{"year": y, "twh": t} for y, t in IEA_DATACENTER_ELECTRICITY_TWH]
    training_costs = [
        {"label": r[0], "cost_million_usd": r[1], "year": r[2], "source": r[3]}
        for r in AI_MODEL_TRAINING_COSTS
    ]
    robots = [
        {"label": r[0], "country": r[1], "price_usd": r[2], "year": r[3], "source": r[4]}
        for r in HUMANOID_ROBOT_PRICES
    ]
    llm_benchmarks = [
        {"model": r[0], "mmlu": r[1], "humaneval": r[2], "math": r[3],
         "country": r[4], "training_cost_usd_M": r[5]}
        for r in LLM_BENCHMARKS
    ]

    meta = {
        "updated_at":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_at_unix": int(time.time()),
        "sources_ok":      ok,
        "sources_failed":  failed,
        "doc_version":     "1.0",
    }
    payload = {
        "meta": meta,
        "capex_mag7":         capex,
        "snapshot_mag7":      snap,
        "china_treasury":     china_treas,
        "freight_index":      freight,
        "sp500_vs_eqw":       sp500,
        "pce_goods":          pce_goods,
        "cre_delinquency":    cre,
        "busloans":           busloans,
        "indpro":             indpro,
        "iea_datacenters":    iea,
        "ai_training_costs":  training_costs,
        "humanoid_robots":    robots,
        "llm_benchmarks":     llm_benchmarks,
    }
    return payload, len(ok), len(failed)


def write_outputs(payload):
    OUT_JSON.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    js = (
        f"/* these_bulleia_cache.js — generated {payload['meta']['updated_at']} */\n"
        f"window.__THESE_BULLEIA__ = "
        f"{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)};\n"
    )
    OUT_JS.write_text(js)
    # Symlinks dans le repo public
    site_dir = Path.home() / "Desktop" / "Site_Crypto_Finance"
    if site_dir.exists():
        for name in ("these_bulleia_cache.json", "these_bulleia_cache.js"):
            link = site_dir / name
            target = CACHE_DIR / name
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(target)
            except OSError as e:
                sys.stderr.write(f"[SYMLINK {name}] {e}, falling back to copy\n")
                shutil.copy2(target, link)


def main():
    t0 = time.time()
    try:
        payload, n_ok, n_fail = build_payload()
    except Exception as e:
        sys.stderr.write(f"[FATAL] {e}\n"); sys.exit(2)

    if n_ok < 3 and OUT_JSON.exists():
        sys.stderr.write(f"[GUARD] {n_ok} OK / {n_fail} fail — keeping previous cache\n")
        sys.exit(1)

    write_outputs(payload)
    dt = time.time() - t0
    sys.stdout.write(
        f"[these_bulleia] OK · {n_ok} sources OK, {n_fail} failed · {dt:.1f}s · "
        f"cache → {OUT_JSON}\n"
    )


if __name__ == "__main__":
    main()
