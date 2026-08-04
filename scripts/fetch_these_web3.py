#!/usr/bin/env python3
"""Cache antifragile pour le chapitre Thèse · Web 3 — La sortie du féodalisme numérique.

Sources auditables :
  1. DeFiLlama · /v2/historicalChainTvl — TVL DeFi globale (live daily)
  2. DeFiLlama · /protocol/aave-v3 — TVL Aave V3 (live)
  3. DeFiLlama · /protocol/uniswap — TVL Uniswap (live)
  4. CoinGecko · /coins/{id}/market_chart — ETH historique (live)
  5. CoinGecko · /global/decentralized_finance_defi — DeFi marketcap (live)
  6. Etherscan + Lens + ENS + DePIN networks — hardcoded officiels
  7. CryptoSlate · UAW (Unique Active Wallets) — hardcoded
  8. GameStop/Robinhood timeline 2021 (hardcoded historique)

Sortie : these_web3_cache.json + .js
Lancé par scf.these_web3.refresh (4×/jour).
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
OUT_JSON = CACHE_DIR / "these_web3_cache.json"
OUT_JS   = CACHE_DIR / "these_web3_cache.js"

UA = "Mozilla/5.0 SiteCryptoFinance-TheseWeb3/1.0"

# ════════════════════════════════════════════════════════════════
# HARDCODED DATASETS — sources officielles, mises à jour annuellement
# ════════════════════════════════════════════════════════════════

# Comptes Ethereum — adresses uniques cumulées fin d'année (millions)
# Source : https://etherscan.io/chart/address (snapshot annuel, MAJ manuelle)
# Réf. ICO boom 2017 (~21 M), DeFi summer 2020 (~135 M).
ETH_ADDRESSES_MN = [
    (2016,   1.2), (2017,  21.0), (2018,  49.0), (2019,  83.0),
    (2020, 135.0), (2021, 182.0), (2022, 221.0), (2023, 250.0),
    (2024, 295.0), (2025, 320.0),
]

# Lens Protocol — profils créés (cumulatif, fin d'année)
# Source : https://lens.xyz/stats + Dune dashboards
LENS_PROFILES = [
    (2022,  100_000), (2023,  300_000), (2024,  550_000), (2025,  780_000),
]

# Farcaster — daily active users (Q4)
# Source : https://dune.com/farcaster_team/farcaster-overview
FARCASTER_DAU = [
    (2023,  15_000), (2024,  60_000), (2025, 240_000),
]

# DePIN — réseaux et capacité installée (snapshot 2025)
# Source : Messari "State of DePIN 2024" + sites officiels Helium / Filecoin / Render.
# NB Helium IoT 1 M = cumul historique déployé (Helium Foundation), pas actifs (~370–400 K).
# NB Filecoin SPs : ~3 600 storage providers actifs (Filecoin Foundation dashboards).
DEPIN_NETWORKS = [
    # (name, nodes_or_units, metric_label, mcap_musd_2025)
    ("Helium · IoT",       1_000_000, "hotspots déployés (cumulés)", 180),
    ("Helium · Mobile",      120_000, "hotspots 5G",                  60),
    ("Render Network",        15_000, "GPU rendering nodes",        3500),
    ("Filecoin",               3_600, "storage providers actifs",   3200),
    ("Akash Network",          1_500, "compute nodes",               650),
    ("Hivemapper",           200_000, "dashcams mappers",            280),
    ("DIMO",                 230_000, "véhicules connectés",          90),
    ("WeatherXM",             12_000, "stations météo",               45),
]

# ENS — noms enregistrés cumulatifs (Etherscan + ens.domains)
# Source : https://dune.com/makoto/ens
ENS_REGISTRATIONS_MN = [
    (2017, 0.05), (2018, 0.12), (2019, 0.20), (2020, 0.40),
    (2021, 1.20), (2022, 2.80), (2023, 2.95), (2024, 3.20),
    (2025, 3.65),
]

# Account abstraction (ERC-4337) — UserOps par mois (millions)
# Source : https://www.bundlebear.com
ERC4337_USEROPS_MN = [
    ("2023-Q1", 0.02), ("2023-Q2", 0.10), ("2023-Q3", 0.30), ("2023-Q4", 0.50),
    ("2024-Q1", 1.20), ("2024-Q2", 2.80), ("2024-Q3", 4.50), ("2024-Q4", 6.20),
    ("2025-Q1", 8.50), ("2025-Q2",11.20), ("2025-Q3",14.30), ("2025-Q4",17.80),
]

# Top DeFi protocols par TVL (Md$, snapshot fin 2025)
# Source : https://defillama.com
TOP_DEFI_PROTOCOLS = [
    # (name, category, tvl_bn_usd, founders, governance)
    ("Aave",            "Lending",      18.5, "Stani Kulechov (2017)",  "DAO Aave"),
    ("Lido",            "Liquid staking", 32.0, "Vasily Shapovalov",     "DAO Lido"),
    ("EigenLayer",      "Restaking",     14.2, "Sreeram Kannan",        "DAO Eigen"),
    ("Uniswap",         "DEX",            6.8, "Hayden Adams (2018)",   "DAO UNI"),
    ("MakerDAO/Sky",    "CDP/Stablecoin",10.5, "Rune Christensen (2014)","DAO SKY"),
    ("Curve",           "Stableswap",     2.4, "Michael Egorov",        "DAO CRV"),
    ("Pendle",          "Yield trading",  5.8, "TN Lee, Vu Gaba",       "DAO PENDLE"),
    ("Morpho",          "Lending",        4.2, "Paul Frambot",          "DAO Morpho"),
]

# Comparaison sociale Web 2 vs Web 3 (utilisateurs/contrôle des données)
SOCIAL_WEB2_VS_WEB3 = [
    # (platform, type, users_mn, data_owner, monetisation, censorship_resist)
    ("Facebook",   "Web 2", 3050, "Meta Inc.",   "Vente data + pubs",    "Aucun"),
    ("Twitter/X",  "Web 2",  600, "X Corp.",     "Pubs + Premium",       "Faible"),
    ("TikTok",     "Web 2", 1700, "ByteDance",   "Pubs + commerce",      "Aucun"),
    ("Instagram",  "Web 2", 2400, "Meta Inc.",   "Pubs + creator fund",  "Aucun"),
    ("YouTube",    "Web 2", 2700, "Google",      "Pubs + monetisation",  "Faible"),
    ("Lens",       "Web 3",    1, "Utilisateur", "Tokenisation + tip",   "Total"),
    ("Farcaster",  "Web 3",    1, "Utilisateur", "Tokenisation + tip",   "Total"),
    ("Mirror",     "Web 3", 0.05, "Utilisateur", "Mint NFT articles",    "Total"),
]

# Timeline GameStop / Robinhood Janvier 2021 — gouvernance et censure
GAMESTOP_TIMELINE = [
    (datetime(2021, 1, 22), "GameStop short squeeze · WSB"),
    (datetime(2021, 1, 27), "GME atteint $347 (+1700% YTD)"),
    (datetime(2021, 1, 28), "Robinhood bloque l'achat de GME · seul vente autorisé"),
    (datetime(2021, 1, 29), "Plainte collective vs Robinhood + Citadel"),
    (datetime(2021, 2,  1), "Audition Congress · Vlad Tenev (CEO Robinhood)"),
    (datetime(2021, 6,  1), "Robinhood paie $70M (FINRA) record d'amende historique"),
]

# Volume on-chain DEX vs CEX (% du volume crypto total)
# Source : The Block (https://www.theblock.co/data/decentralized-finance/dex-non-custodial)
DEX_VOLUME_SHARE = [
    (2020,  1.5), (2021,  6.8), (2022,  7.2), (2023, 11.5),
    (2024, 14.2), (2025, 16.8),
]

# ════════════════════════════════════════════════════════════════
# FETCH HELPERS
# ════════════════════════════════════════════════════════════════

def http_get_text(url, timeout=25, max_retries=4, accept="application/json,*/*"):
    req = Request(url, headers={"User-Agent": UA, "Accept": accept})
    last_err = None
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="ignore")
        except HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                time.sleep(15 * (2 ** attempt)); continue
            if 500 <= e.code < 600 and attempt < max_retries - 1:
                time.sleep(5 * (2 ** attempt)); continue
            raise
        except (URLError, ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(5 * (2 ** attempt))
    raise last_err if last_err else RuntimeError("retries exhausted")


def http_get_json(url, timeout=25, max_retries=4):
    txt = http_get_text(url, timeout=timeout, max_retries=max_retries)
    return json.loads(txt)


def fetch_defillama_total_tvl():
    """https://api.llama.fi/v2/historicalChainTvl — TVL DeFi globale (Md$, daily)."""
    url = "https://api.llama.fi/v2/historicalChainTvl"
    try:
        data = http_get_json(url, timeout=30)
    except Exception as e:
        sys.stderr.write(f"[DEFILLAMA total] {e}\n"); return None
    if not isinstance(data, list) or len(data) == 0:
        return None
    dates, values = [], []
    for row in data:
        ts = row.get("date")
        tvl = row.get("tvl")
        if ts is None or tvl is None: continue
        try:
            d = datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")
            dates.append(d); values.append(float(tvl) / 1e9)
        except (TypeError, ValueError):
            continue
    if not dates: return None
    # Échantillonnage hebdomadaire pour réduire la taille
    keep_d, keep_v = [], []
    for i, d in enumerate(dates):
        if i == 0 or i == len(dates) - 1 or i % 7 == 0:
            keep_d.append(d); keep_v.append(round(values[i], 2))
    return {"dates": keep_d, "values": keep_v,
            "source_url": "https://defillama.com",
            "unit": "Md$"}


def fetch_defillama_protocol(slug, label):
    """https://api.llama.fi/protocol/{slug} — TVL d'un protocole."""
    url = f"https://api.llama.fi/protocol/{slug}"
    try:
        data = http_get_json(url, timeout=30)
    except Exception as e:
        sys.stderr.write(f"[DEFILLAMA {slug}] {e}\n"); return None
    if not isinstance(data, dict): return None
    tvl_arr = data.get("tvl") or []
    if not isinstance(tvl_arr, list) or len(tvl_arr) == 0:
        return None
    dates, values = [], []
    for row in tvl_arr:
        ts = row.get("date") if isinstance(row, dict) else None
        tvl = row.get("totalLiquidityUSD") if isinstance(row, dict) else None
        if ts is None or tvl is None: continue
        try:
            d = datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")
            dates.append(d); values.append(float(tvl) / 1e9)
        except (TypeError, ValueError):
            continue
    if not dates: return None
    # Hebdo
    keep_d, keep_v = [], []
    for i, d in enumerate(dates):
        if i == 0 or i == len(dates) - 1 or i % 7 == 0:
            keep_d.append(d); keep_v.append(round(values[i], 3))
    return {"dates": keep_d, "values": keep_v, "label": label,
            "source_url": f"https://defillama.com/protocol/{slug}",
            "unit": "Md$"}


def fetch_yahoo_eth():
    """ETH-USD daily depuis 2017 via Yahoo Finance (CoinGecko free tier ne donne
    plus que 365 jours). Échantillonnage weekly pour limiter la taille."""
    # 2017-11-09 (inception YF ETH-USD) à 2030
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/ETH-USD"
           "?period1=1510185600&period2=1893456000&interval=1d")
    try:
        data = http_get_json(url, timeout=20)
        r = data["chart"]["result"][0]
        ts = r["timestamp"]
        close = r["indicators"]["quote"][0]["close"]
    except Exception as e:
        sys.stderr.write(f"[YF ETH-USD] {e}\n"); return None
    dates, values = [], []
    for t, v in zip(ts, close):
        if v is None: continue
        d = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        dates.append(d); values.append(round(float(v), 2))
    if not dates: return None
    # Sous-échantillonnage weekly
    keep_d, keep_v = [], []
    for i, d in enumerate(dates):
        if i == 0 or i == len(dates) - 1 or i % 7 == 0:
            keep_d.append(d); keep_v.append(values[i])
    return {"dates": keep_d, "values": keep_v,
            "source_url": "https://finance.yahoo.com/quote/ETH-USD/",
            "unit": "USD"}


def fetch_defi_marketcap():
    """CoinGecko · DeFi global market cap."""
    url = "https://api.coingecko.com/api/v3/global/decentralized_finance_defi"
    try:
        data = http_get_json(url, timeout=20)
    except Exception as e:
        sys.stderr.write(f"[CG defi] {e}\n"); return None
    d = data.get("data") or {}
    if not d: return None
    return {
        "defi_market_cap_usd": float(d.get("defi_market_cap", "0") or 0),
        "eth_market_cap_usd":  float(d.get("eth_market_cap", "0") or 0),
        "defi_to_eth_ratio":   float(d.get("defi_to_eth_ratio", "0") or 0),
        "trading_volume_24h":  float(d.get("trading_volume_24h", "0") or 0),
        "defi_dominance":      float(d.get("defi_dominance", "0") or 0),
        "top_coin_name":       d.get("top_coin_name", ""),
        "top_coin_defi_dominance": float(d.get("top_coin_defi_dominance", "0") or 0),
        "source_url": "https://www.coingecko.com/en/categories/decentralized-finance-defi",
    }


# ════════════════════════════════════════════════════════════════
# PAYLOAD BUILDER
# ════════════════════════════════════════════════════════════════

def build_payload():
    ok, failed = [], []

    defi_tvl = fetch_defillama_total_tvl()
    if defi_tvl: ok.append("DefiLlama:total_tvl")
    else: failed.append("DefiLlama:total_tvl")

    aave_tvl = fetch_defillama_protocol("aave-v3", "Aave V3")
    if aave_tvl: ok.append("DefiLlama:aave-v3")
    else: failed.append("DefiLlama:aave-v3")

    uni_tvl = fetch_defillama_protocol("uniswap-v3", "Uniswap V3")
    if uni_tvl: ok.append("DefiLlama:uniswap-v3")
    else: failed.append("DefiLlama:uniswap-v3")

    lido_tvl = fetch_defillama_protocol("lido", "Lido")
    if lido_tvl: ok.append("DefiLlama:lido")
    else: failed.append("DefiLlama:lido")

    eth_px = fetch_yahoo_eth()
    if eth_px: ok.append("YF:ETH-USD")
    else: failed.append("YF:ETH-USD")

    defi_snap = fetch_defi_marketcap()
    if defi_snap: ok.append("CoinGecko:defi_global")
    else: failed.append("CoinGecko:defi_global")

    meta = {
        "updated_at":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_at_unix": int(time.time()),
        "sources_ok":      ok,
        "sources_failed":  failed,
        "doc_version":     "1.0",
    }
    payload = {
        "meta": meta,
        "defi_tvl_total":  defi_tvl,
        "aave_tvl":        aave_tvl,
        "uniswap_tvl":     uni_tvl,
        "lido_tvl":        lido_tvl,
        "eth_price":       eth_px,
        "defi_snapshot":   defi_snap,
        "eth_addresses":   [{"year": y, "addresses_mn": v} for y, v in ETH_ADDRESSES_MN],
        "lens_profiles":   [{"year": y, "profiles": v} for y, v in LENS_PROFILES],
        "farcaster_dau":   [{"year": y, "dau": v} for y, v in FARCASTER_DAU],
        "depin_networks":  [{"name": n, "units": u, "metric": m, "mcap_musd": mc}
                            for n, u, m, mc in DEPIN_NETWORKS],
        "ens_registrations": [{"year": y, "registered_mn": v} for y, v in ENS_REGISTRATIONS_MN],
        "erc4337_useops":  [{"period": p, "useops_mn": v} for p, v in ERC4337_USEROPS_MN],
        "top_defi_protocols": [{"name": n, "cat": c, "tvl_bn": t, "founders": f, "gov": g}
                               for n, c, t, f, g in TOP_DEFI_PROTOCOLS],
        "social_compare":  [{"platform": p, "type": t, "users_mn": u,
                             "data_owner": o, "monetisation": m, "censorship_resist": cr}
                            for p, t, u, o, m, cr in SOCIAL_WEB2_VS_WEB3],
        "gamestop_timeline": [{"date": d.strftime("%Y-%m-%d"), "event": e}
                              for d, e in GAMESTOP_TIMELINE],
        "dex_volume_share": [{"year": y, "pct": v} for y, v in DEX_VOLUME_SHARE],
    }
    return payload, len(ok), len(failed)


def write_outputs(payload):
    OUT_JSON.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    js = (
        f"/* these_web3_cache.js — generated {payload['meta']['updated_at']} */\n"
        f"window.__THESE_WEB3__ = "
        f"{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)};\n"
    )
    OUT_JS.write_text(js)
    site_dir = Path.home() / "Desktop" / "Site_Crypto_Finance"
    if site_dir.exists():
        for name in ("these_web3_cache.json", "these_web3_cache.js"):
            link = site_dir / name
            target = CACHE_DIR / name
            try:
                if link.is_symlink() or link.exists(): link.unlink()
                link.symlink_to(target)
            except OSError:
                shutil.copy2(target, link)


def main():
    t0 = time.time()
    try:
        payload, n_ok, n_fail = build_payload()
    except Exception as e:
        sys.stderr.write(f"[FATAL] {e}\n"); sys.exit(2)
    write_outputs(payload)
    dt = time.time() - t0
    sys.stdout.write(f"[these_web3] OK · {n_ok} sources, {n_fail} failed · {dt:.1f}s\n")


if __name__ == "__main__":
    main()
