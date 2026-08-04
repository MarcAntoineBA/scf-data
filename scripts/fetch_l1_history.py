#!/usr/bin/env python3
"""Backfill deep historical time series (daily, multi-year) for the 15 L1 blockchains.

Sources (all free, no auth required):
  - Yahoo Finance chart API v8 (via curl_cffi, impersonate=chrome120) — daily close
    + volume per token. → price, volume. MCap computed as close × current
    circulating_supply (read from l1_valuation_cache).
    (Migré 2026-06-17 depuis CryptoCompare /histoday, devenu 401 — clé API exigée.
     Même bascule Yahoo que fetch_narratives.py : cf. yf_crypto_symbol_map / UNI7083.)
  - DeFiLlama /v2/historicalChainTvl/{chain} — daily TVL, full history from chain inception.

Writes to ~/Library/Caches/site_crypto_finance/ :
  - l1_history_cache.json
  - l1_history_cache.js  (window.__L1_HISTORY_LIVE__ = {...};)

Run manually: python3 fetch_l1_history.py [--force] [--daily]
  --daily : keep daily granularity (default is weekly downsample, lighter file)

Run by launchd (scf.l1history) daily.
"""
import json
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
HIST_JSON = CACHE_DIR / "l1_history_cache.json"
HIST_JS   = CACHE_DIR / "l1_history_cache.js"
LIVE_JSON = CACHE_DIR / "l1_valuation_cache.json"
HIST_MAX_HOURS = 24
HIST_DAYS = 2200   # ~6 ans de fenêtre demandée à Yahoo (interval=1d)

# Hard launch dates — on filtre tout point antérieur (garde-fou anti-données
# pré-listing bogues que Yahoo peut renvoyer pour un ticker numéroté réutilisé).
LAUNCH_DATE_MS = {
    "HYPE": int(datetime(2024, 11, 29, tzinfo=timezone.utc).timestamp() * 1000),
    "TAO":  int(datetime(2023, 3,  14, tzinfo=timezone.utc).timestamp() * 1000),
    "SUI":  int(datetime(2023, 5,   3, tzinfo=timezone.utc).timestamp() * 1000),
    "APT":  int(datetime(2022, 10, 19, tzinfo=timezone.utc).timestamp() * 1000),
    "TON":  int(datetime(2022, 8,  15, tzinfo=timezone.utc).timestamp() * 1000),
}

# token, symbole Yahoo vérifié, chaîne DeFiLlama, nom (pour le fallback search Yahoo).
# Symboles numérotés (SUI20947, APT21794, TON11419, HYPE32196, TAO22974) : Yahoo
# désambiguïse les collisions de ticker par suffixe ; le {SYM}-USD nu est mort ou
# pointe vers un autre token. Vérifiés via chart API le 2026-06-17.
TOKENS = [
    # token,  yahoo_sym,        defillama_chain,   name
    ("BTC",  "BTC-USD",        "Bitcoin",         "Bitcoin"),
    ("ETH",  "ETH-USD",        "Ethereum",        "Ethereum"),
    ("SOL",  "SOL-USD",        "Solana",          "Solana"),
    ("BNB",  "BNB-USD",        "BSC",             "BNB"),
    ("XRP",  "XRP-USD",        "XRPL",            "XRP"),
    ("ADA",  "ADA-USD",        "Cardano",         "Cardano"),
    ("AVAX", "AVAX-USD",       "Avalanche",       "Avalanche"),
    ("DOT",  "DOT-USD",        "Polkadot",        "Polkadot"),
    ("NEAR", "NEAR-USD",       "Near",            "NEAR Protocol"),
    ("SUI",  "SUI20947-USD",   "Sui",             "Sui"),
    ("APT",  "APT21794-USD",   "Aptos",           "Aptos"),
    ("TON",  "TON11419-USD",   "TON",             "Toncoin"),
    ("TRX",  "TRX-USD",        "Tron",            "TRON"),
    ("HYPE", "HYPE32196-USD",  "hyperliquid-l1",  "Hyperliquid"),
    ("TAO",  "TAO22974-USD",   "Bittensor",       "Bittensor"),
]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) SiteCryptoFinance/1.0"


def http_get_json(url, timeout=30, retries=4):
    """GET JSON with retries on transient errors (DNS, network) — backoff 5/15/45/90s.

    Why: launchd fires at 03:30 right after sleep wake; DNS often not ready yet,
    causing 'nodename nor servname provided' on the first attempt for every URL.
    Utilisé pour DeFiLlama (Yahoo passe par curl_cffi, cf. _yf_chart_daily).
    """
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    backoff = [5, 15, 45, 90]
    last_exc = None
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (URLError, OSError) as e:
            last_exc = e
            msg = str(e)
            is_transient = ("nodename" in msg or "Temporary failure" in msg
                            or "timed out" in msg or "Connection" in msg)
            if attempt < retries - 1 and is_transient:
                wait = backoff[min(attempt, len(backoff) - 1)]
                sys.stderr.write(f"[HIST] retry {attempt+1}/{retries} in {wait}s ({msg})\n")
                time.sleep(wait)
                continue
            raise
    raise last_exc


# ─── Yahoo Finance (curl_cffi) ──────────────────────────────────────────────
# Yahoo bloque requests/urllib stdlib (429) depuis mai 2026 ; curl_cffi
# impersonate=chrome120 obligatoire (cf. project_yahoo_curl_cffi_required).

def _yf_chart_daily(sess, yf_sym, p1, p2):
    """Yahoo v8 chart → [(ts_ms, close, volume_usd), ...] daily, ou []."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}"
           f"?period1={p1}&period2={p2}&interval=1d")
    try:
        r = sess.get(url, timeout=25)
        res = (r.json().get("chart", {}).get("result") or [None])[0]
        if not res or not res.get("timestamp"):
            return []
        ts = res["timestamp"]
        q  = (res.get("indicators", {}).get("quote", [{}])[0] or {})
        closes = q.get("close", []) or []
        vols   = q.get("volume", []) or []
        rows = []
        for i, t in enumerate(ts):
            c = closes[i] if i < len(closes) else None
            if c is None or c <= 0:
                continue
            v = vols[i] if i < len(vols) else None
            rows.append((int(t) * 1000, float(c), float(v) if v else 0.0))
        return rows
    except Exception as e:
        sys.stderr.write(f"[HIST] Yahoo chart {yf_sym}: {e}\n")
        return []


def _yf_search_usd(sess, name):
    """Résout un nom en symboles Yahoo '-USD' candidats (fallback si le symbole
    mémorisé a été renuméroté par Yahoo). cf. fetch_narratives._yf_search_usd_symbols."""
    from urllib.parse import quote
    try:
        r = sess.get(f"https://query1.finance.yahoo.com/v1/finance/search"
                     f"?q={quote(name)}&quotesCount=8&newsCount=0", timeout=15)
        return [x.get("symbol") for x in r.json().get("quotes", [])
                if str(x.get("symbol", "")).endswith("-USD")]
    except Exception:
        return []


def price_ok(last, ref):
    """Garde anti-collision : un vrai token a un dernier close à 0.5×–2× du prix live.
    Une collision de ticker (Yahoo sert un autre coin) diverge de plusieurs ordres
    de grandeur → rejetée."""
    return bool(ref) and bool(last) and 0.5 <= (last / ref) <= 2.0


def fetch_yahoo_histoday(sess, yf_sym, name, ref_price, p1, p2):
    """Daily price+volume depuis Yahoo. Essaie le symbole vérifié, puis un fallback
    par recherche de nom (gère un ticker renuméroté) avec garde anti-collision.

    Renvoie (rows, symbole_utilisé) ou (None, None).
    rows = [(ts_ms, close, volume_usd), ...]  — même forme que l'ancien fetcher CC.
    """
    tried = []
    for cand in (yf_sym,):
        if not cand or cand in tried:
            continue
        tried.append(cand)
        rows = _yf_chart_daily(sess, cand, p1, p2)
        if rows and (ref_price is None or price_ok(rows[-1][1], ref_price)):
            return rows, cand
        time.sleep(0.12)
    # Fallback : recherche Yahoo par nom (uniquement si on a un prix de référence
    # pour valider qu'on n'attrape pas une collision).
    if ref_price:
        for cand in _yf_search_usd(sess, name)[:6]:
            if cand in tried:
                continue
            tried.append(cand)
            rows = _yf_chart_daily(sess, cand, p1, p2)
            if rows and price_ok(rows[-1][1], ref_price):
                sys.stderr.write(f"[HIST] {name}: fallback search → {cand}\n")
                return rows, cand
            time.sleep(0.12)
    return None, None


def fetch_defillama_tvl_history(chain_slug):
    url = f"https://api.llama.fi/v2/historicalChainTvl/{chain_slug}"
    try:
        data = http_get_json(url)
        if not isinstance(data, list):
            return []
        return [(int(d["date"]) * 1000, float(d["tvl"]))
                for d in data if d.get("tvl") is not None]
    except HTTPError as e:
        if e.code == 404:
            return []
        sys.stderr.write(f"[HIST] DeFiLlama TVL {chain_slug}: HTTP {e.code}\n")
        return []
    except Exception as e:
        sys.stderr.write(f"[HIST] DeFiLlama TVL {chain_slug}: {e}\n")
        return []


def downsample_weekly(series, value_index=1):
    """series = [(ts_ms, value, ...), ...]. Returns weekly-averaged [[ts_ms, val], ...].
    Keeps first value of each week bucket for the timestamp, averages the value."""
    if not series:
        return []
    buckets = {}
    for row in series:
        ts = row[0]
        val = row[value_index] if len(row) > value_index else row[1]
        if val is None:
            continue
        day = ts // 86400000
        bucket = day // 7 * 7
        buckets.setdefault(bucket, []).append(val)
    return [[b * 86400000, sum(vals) / len(vals)]
            for b, vals in sorted(buckets.items())]


def daily_series(series, value_index=1):
    """Keep daily granularity, just reformat."""
    if not series:
        return []
    return [[row[0], row[value_index]] for row in series
            if len(row) > value_index and row[value_index] is not None]


def load_live_supply():
    """Return (supply_map, price_map) from the live valuation cache (if available).
    supply_map = {token: circ_supply} ; price_map = {token: price_usd} (réf garde anti-collision)."""
    if not LIVE_JSON.exists():
        return {}, {}
    try:
        data = json.loads(LIVE_JSON.read_text())
        toks = data.get("tokens", {})
        supply = {tok: v.get("circ_supply") for tok, v in toks.items()}
        price  = {tok: v.get("price_usd") for tok, v in toks.items()}
        return supply, price
    except Exception:
        return {}, {}


def is_fresh():
    if not HIST_JSON.exists():
        return False
    age_hours = (time.time() - HIST_JSON.stat().st_mtime) / 3600
    return age_hours < HIST_MAX_HOURS


def main():
    force = "--force" in sys.argv
    keep_daily = "--daily" in sys.argv
    if is_fresh() and not force:
        sys.stderr.write("[HIST] Cache fresh (<24h). Use --force to refresh.\n")
        return

    try:
        from curl_cffi import requests as creq
    except ImportError:
        sys.stderr.write("[HIST] curl_cffi indisponible (pip3 install curl_cffi) — "
                         "on garde le cache existant.\n")
        sys.exit(2)

    sys.stderr.write(f"[HIST] Fetching {'daily' if keep_daily else 'weekly'} history "
                     f"(15 L1s, Yahoo ~{HIST_DAYS//365}y window)...\n")
    sess = creq.Session(impersonate="chrome120")
    p2 = int(time.time())
    p1 = p2 - HIST_DAYS * 86400
    supply_map, price_map = load_live_supply()
    out = {}

    for tok, yf_sym, dl_chain, name in TOKENS:
        sys.stderr.write(f"[HIST] {tok} (YF={yf_sym}, DL={dl_chain})... ")

        ref = price_map.get(tok)
        raw, sym_used = fetch_yahoo_histoday(sess, yf_sym, name, ref, p1, p2)
        if raw is None:
            sys.stderr.write("no Yahoo data\n")
            out[tok] = {"token": tok, "mcap_history": [], "price_history": [],
                        "volume_history": [], "tvl_history": [], "inception_iso": None,
                        "source_cc": "unavailable", "source_tvl": "unavailable"}
            continue

        # Filtre anti pré-listing (ticker numéroté réutilisé)
        launch_ms = LAUNCH_DATE_MS.get(tok)
        if launch_ms:
            raw = [r for r in raw if r[0] >= launch_ms]
            if not raw:
                sys.stderr.write("no post-launch data\n")
                continue

        # MCap = price × circ_supply courant (proxy ; les variations de supply
        # historiques restent faibles → fidèle à ~5-10% pour la tendance).
        circ = supply_map.get(tok)
        if circ and circ > 0:
            mcap_series = [(ts, close * circ) for ts, close, _ in raw]
        else:
            mcap_series = []

        # Downsample or keep daily
        if keep_daily:
            entry_price  = daily_series([(ts, close) for ts, close, _ in raw])
            entry_mcap   = daily_series(mcap_series) if mcap_series else []
            entry_volume = daily_series([(ts, vol)   for ts, _, vol in raw])
        else:
            entry_price  = downsample_weekly([(ts, close) for ts, close, _ in raw])
            entry_mcap   = downsample_weekly(mcap_series) if mcap_series else []
            entry_volume = downsample_weekly([(ts, vol)   for ts, _, vol in raw])

        # TVL history
        tvl_raw = fetch_defillama_tvl_history(dl_chain)
        if keep_daily:
            entry_tvl = daily_series(tvl_raw)
        else:
            entry_tvl = downsample_weekly(tvl_raw)

        inception = (datetime.fromtimestamp(raw[0][0] / 1000, tz=timezone.utc).date().isoformat()
                     if raw else None)

        n_years = len(raw) / 365.0
        sys.stderr.write(f"YF={sym_used} {len(raw)}d (~{n_years:.1f}y) + TVL {len(tvl_raw)}d\n")

        out[tok] = {
            "token":          tok,
            "cc_fsym":        tok,          # alias legacy (ex-CryptoCompare fsym)
            "cc_tsym":        "USD",        # alias legacy
            "defillama":      dl_chain,
            "price_history":  entry_price,
            "mcap_history":   entry_mcap,
            "volume_history": entry_volume,
            "tvl_history":    entry_tvl,
            "inception_iso":  inception,
            "source_cc":      f"Yahoo Finance chart {sym_used}",
            "source_tvl":     f"defillama.com historicalChainTvl/{dl_chain}" if tvl_raw else "unavailable",
        }

        time.sleep(0.2)   # pacing poli entre tokens

    payload = {
        "tokens":      out,
        "universe":    [t[0] for t in TOKENS],
        "updated":     datetime.now().strftime("%d/%m/%Y %H:%M"),
        "updated_iso": datetime.now().isoformat(),
        "granularity": "daily" if keep_daily else "weekly (downsampled)",
        "window":      f"jusqu'à ~{HIST_DAYS//365} ans de daily selon disponibilité Yahoo Finance",
        "sources": {
            "price_volume":       "Yahoo Finance chart API v8 (daily, via curl_cffi)",
            "mcap_computation":   "price × circulating_supply (from l1_valuation_cache live)",
            "tvl":                "DeFiLlama /v2/historicalChainTvl/{chain} (full history)",
        }
    }

    # Garde-fou anti-écrasement : on compte les tokens avec un PRICE history utilisable
    # (price vient de Yahoo et ne dépend pas du cache valuation, contrairement à mcap).
    n_price = sum(1 for v in out.values() if len(v.get("price_history", [])) > 10)
    n_complete = sum(1 for v in out.values() if len(v.get("mcap_history", [])) > 10)

    # Si TOUT a échoué (DNS down au réveil launchd, Yahoo 429, etc.), ne pas écraser
    # un cache valide existant par un payload vide — on garde l'ancien.
    if n_price == 0 and HIST_JSON.exists() and HIST_JSON.stat().st_size > 50_000:
        sys.stderr.write(f"[HIST] ABORT write: 0/{len(TOKENS)} tokens fetched and existing cache is non-empty. "
                         f"Keeping previous cache intact.\n")
        sys.exit(2)

    with open(HIST_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    sys.stderr.write(f"[HIST] Wrote {HIST_JSON} ({HIST_JSON.stat().st_size // 1024} KB)\n")

    js_payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    HIST_JS.write_text(f"window.__L1_HISTORY_LIVE__={js_payload};", encoding="utf-8")
    sys.stderr.write(f"[HIST] Wrote {HIST_JS}\n")

    sys.stderr.write(f"[HIST] Done: {n_price}/{len(TOKENS)} tokens with price, "
                     f"{n_complete}/{len(TOKENS)} with mcap\n")


if __name__ == "__main__":
    main()
