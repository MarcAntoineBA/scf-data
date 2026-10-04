#!/usr/bin/env python3
"""Cache antifragile pour le chapitre Thèse · Web 3 — La sortie du féodalisme numérique.

Sources (gratuites, sans clé) :
  1. DeFiLlama · /v2/historicalChainTvl — TVL DeFi globale (quotidien)
  2. DeFiLlama · /protocol/{aave-v3, uniswap-v3, lido} — TVL historiques + ETH stakés Lido
  3. DeFiLlama · /tvl/{protocole} — TVL du jour des protocoles du tableau
  4. DeFiLlama · /summary/dexs/uniswap — volume cumulé d'Uniswap depuis 2018
  5. Etherscan · chart/address?output=csv — adresses Ethereum uniques (quotidien)
  6. The Block · tableau de bord gratuit « DEX to CEX spot trade volume » (mensuel)
  7. BundleBear · comptes intelligents ERC-4337 actifs et UserOps (mensuel ;
     la page ne publie qu'une fenêtre de ~2 ans : l'historique est CUMULÉ ici,
     mois par mois, à partir du cache précédent)
  8. DePINscan · appareils recensés et capitalisation des réseaux DePIN
  9. Yahoo · ETH-USD ; CoinGecko · capitalisation DeFi (instantané)
 10. Millésimes affichés (pas d'API) : tableau social Web 2 / Web 3 (chiffres
     publiés par les plateformes, datés), chronologie GameStop 2021.

Règles : repli = DERNIÈRE valeur réellement collectée (cache précédent), mois
en cours (partiel) jamais publié comme un mois plein, horodatage de dernier
succès par source dans meta.sources_last_ok.

Sortie : these_web3_cache.json + .js
Lancé par scf.these_web3.refresh (4×/jour).
"""
import json
import os
import re
import shutil
import sys
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_OUT_OVERRIDE = os.environ.get("THESE_OUT_DIR")  # tests : écrire ailleurs
CACHE_DIR = Path(_OUT_OVERRIDE) if _OUT_OVERRIDE else (
    Path.home() / "Library" / "Caches" / "site_crypto_finance")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "these_web3_cache.json"
OUT_JS   = CACHE_DIR / "these_web3_cache.js"
PREV_JSON = Path(os.environ["THESE_PREV_JSON"]) if os.environ.get("THESE_PREV_JSON") else (
    Path.home() / "Library" / "Caches" / "site_crypto_finance" / "these_web3_cache.json")

UA = "Mozilla/5.0 SiteCryptoFinance-TheseWeb3/1.1"
NOW = datetime.now(timezone.utc)
MOIS_COURANT = NOW.strftime("%Y-%m")

# ════════════════════════════════════════════════════════════════
# DONNÉES DE RÉFÉRENCE (millésimées, sources notées)
# ════════════════════════════════════════════════════════════════

# Protocoles du tableau : identité stable, TVL du jour via DeFiLlama /tvl/{slug}
DEFI_PROTOCOLS = [
    # (slug DeFiLlama, nom affiché, catégorie, fondateurs, gouvernance)
    ("aave",        "Aave",                  "Prêt",                    "Stani Kulechov (2017)",         "DAO Aave"),
    ("lido",        "Lido",                  "Staking liquide",         "Konstantin Lomashuk, Vasiliy Shapovalov (2020)", "DAO Lido"),
    ("morpho",      "Morpho",                "Prêt",                    "Paul Frambot (2021)",           "DAO Morpho"),
    ("eigenlayer",  "EigenLayer",            "Restaking",               "Sreeram Kannan (2023)",         "Jeton EIGEN"),
    ("sky",         "Sky (ex-MakerDAO)",     "Stablecoin adossé",       "Rune Christensen (2014)",       "DAO Sky"),
    ("uniswap",     "Uniswap",               "Échange décentralisé",    "Hayden Adams (2018)",           "DAO Uniswap"),
    ("curve-dex",   "Curve",                 "Échange de stablecoins",  "Michael Egorov (2020)",         "DAO Curve"),
    ("pendle",      "Pendle",                "Marché du rendement",     "TN Lee, Vu Gaba (2021)",        "vePENDLE"),
]

# Réseaux DePIN suivis (identifiants DePINscan)
DEPIN_NETWORKS = [
    # (p_id DePINscan, nom affiché, unité)
    ("heliumiot",  "Helium · IoT",   "hotspots"),
    ("dimo",       "DIMO",           "véhicules connectés"),
    ("geodnet",    "GEODNET",        "stations GNSS"),
    ("weatherxm",  "WeatherXM",      "stations météo"),
    ("hivemapper", "Hivemapper",     "dashcams"),
    ("filecoin",   "Filecoin",       "fournisseurs de stockage"),
    ("akash",      "Akash",          "fournisseurs de calcul"),
]

# Social Web 2 vs Web 3 — chiffres publiés par les plateformes, datés.
# users_mn = None quand aucun chiffre fiable n'est publié (vide > chiffre plausible).
SOCIAL_WEB2_VS_WEB3 = [
    # (plateforme, type, users_mn, libellé daté, propriétaire des données, monétisation, résistance censure)
    ("Facebook",   "Web 2", 3065, "3,07 Md mensuels (fin 2023, dernier chiffre publié par Meta)",
     "Meta",        "Publicité ciblée",      "Aucune"),
    ("Instagram",  "Web 2", 3000, "3 Md mensuels (Meta, sept. 2025)",
     "Meta",        "Publicité + commerce",  "Aucune"),
    ("YouTube",    "Web 2", 2000, "> 2 Md connectés par mois (Google, 2019)",
     "Google",      "Publicité + abonnements", "Faible"),
    ("TikTok",     "Web 2", 1000, "> 1 Md mensuels (TikTok, sept. 2021)",
     "ByteDance",   "Publicité + commerce",  "Aucune"),
    ("X (Twitter)", "Web 2", 600, "≈ 600 M mensuels (X, 2024)",
     "X Corp.",     "Publicité + abonnements", "Faible"),
    ("Farcaster",  "Web 3",  1.05, "≈ 1 M d'identifiants enregistrés (avr. 2025)",
     "Utilisateur", "Pourboires + jetons",   "Forte"),
    ("Lens",       "Web 3",  None, "n. d. (aucun chiffre récent publié)",
     "Utilisateur", "Pourboires + jetons",   "Forte"),
    ("Paragraph (ex-Mirror)", "Web 3", None, "n. d.",
     "Utilisateur", "Abonnements + articles en NFT", "Forte"),
]

# GameStop / Robinhood — chronologie vérifiée (rapport SEC d'octobre 2021, FINRA)
GAMESTOP_TIMELINE = [
    ("2021-01-22", "Le short squeeze de GameStop s'emballe, porté par WallStreetBets"),
    ("2021-01-27", "GME clôture à 347,51 $ (+1 745 % depuis le 1er janvier)"),
    ("2021-01-28", "La chambre de compensation (NSCC) exige ≈ 3,7 Md$ de garanties à Robinhood"),
    ("2021-01-28", "Robinhood bloque l'ACHAT de GME et d'une dizaine d'autres titres — la vente reste permise"),
    ("2021-02-18", "Audition du PDG de Robinhood, Vlad Tenev, devant la Chambre des représentants"),
    ("2021-06-30", "FINRA inflige à Robinhood une amende record de 70 M$ (pannes, informations trompeuses)"),
]

# ENS — noms enregistrés cumulés (Dune · ethereumnameservice/ens). NON revérifié le 2026-10-04 :
# aucune source ouverte à jour trouvée ; conservé tel quel et signalé.
ENS_REGISTRATIONS_MN = [
    (2017, 0.05), (2018, 0.12), (2019, 0.20), (2020, 0.40),
    (2021, 1.20), (2022, 2.80), (2023, 2.95), (2024, 3.20),
    (2025, 3.65),
]

# ════════════════════════════════════════════════════════════════
# OUTILS HTTP
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
    return json.loads(http_get_text(url, timeout=timeout, max_retries=max_retries))


def _iso(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")


def _weekly(dates, values, nd=2):
    keep_d, keep_v = [], []
    n = len(dates)
    idx = sorted(set([0] + list(range(n - 1, -1, -7))))
    for i in idx:
        keep_d.append(dates[i]); keep_v.append(round(values[i], nd))
    return keep_d, keep_v


# ════════════════════════════════════════════════════════════════
# COLLECTE
# ════════════════════════════════════════════════════════════════

def fetch_defillama_total_tvl():
    try:
        data = http_get_json("https://api.llama.fi/v2/historicalChainTvl", timeout=30)
    except Exception as e:
        sys.stderr.write(f"[DEFILLAMA total] {e}\n"); return None
    dates, values = [], []
    for row in data if isinstance(data, list) else []:
        ts, tvl = row.get("date"), row.get("tvl")
        if ts is None or tvl is None: continue
        dates.append(_iso(ts)); values.append(float(tvl) / 1e9)
    if not dates: return None
    d, v = _weekly(dates, values)
    return {"dates": d, "values": v, "source_url": "https://defillama.com", "unit": "Md$"}


def fetch_defillama_protocol(slug, label):
    """TVL historique d'un protocole + dernier relevé des jetons détenus."""
    try:
        data = http_get_json(f"https://api.llama.fi/protocol/{slug}", timeout=40)
    except Exception as e:
        sys.stderr.write(f"[DEFILLAMA {slug}] {e}\n"); return None
    if not isinstance(data, dict): return None
    dates, values = [], []
    for row in data.get("tvl") or []:
        if not isinstance(row, dict): continue
        ts, tvl = row.get("date"), row.get("totalLiquidityUSD")
        if ts is None or tvl is None: continue
        dates.append(_iso(ts)); values.append(float(tvl) / 1e9)
    if not dates: return None
    d, v = _weekly(dates, values, 3)
    out = {"dates": d, "values": v, "label": label,
           "source_url": f"https://defillama.com/protocol/{slug}", "unit": "Md$"}
    tk = data.get("tokens") or []
    if tk and isinstance(tk[-1], dict):
        out["tokens_last"] = {k: round(float(x), 2) for k, x in (tk[-1].get("tokens") or {}).items()
                              if isinstance(x, (int, float))}
        out["tokens_date"] = _iso(tk[-1].get("date", time.time()))
    return out


def fetch_protocols_tvl():
    rows = []
    for slug, name, cat, founders, gov in DEFI_PROTOCOLS:
        try:
            v = float(http_get_text(f"https://api.llama.fi/tvl/{slug}", timeout=20, max_retries=2))
        except Exception as e:
            sys.stderr.write(f"[DEFILLAMA tvl {slug}] {e}\n"); v = None
        rows.append({"slug": slug, "name": name, "cat": cat, "founders": founders, "gov": gov,
                     "tvl_bn": round(v / 1e9, 2) if v else None})
        time.sleep(0.3)
    if sum(1 for r in rows if r["tvl_bn"]) < len(rows) // 2:
        return None
    return rows


def fetch_uniswap_volume():
    try:
        d = http_get_json("https://api.llama.fi/summary/dexs/uniswap"
                          "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true", timeout=30)
        tot = float(d["totalAllTime"])
        return {"total_bn": round(tot / 1e9), "d30_bn": round(float(d.get("total30d") or 0) / 1e9, 1),
                "date": NOW.date().isoformat(), "source_url": "https://defillama.com/protocol/uniswap"}
    except Exception as e:
        sys.stderr.write(f"[DEFILLAMA uniswap volume] {e}\n"); return None


def fetch_yahoo_eth():
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/ETH-USD"
           f"?period1=1510185600&period2={int(time.time()) + 86400}&interval=1d")
    try:
        data = http_get_json(url, timeout=20)
        r = data["chart"]["result"][0]
        ts = r["timestamp"]; close = r["indicators"]["quote"][0]["close"]
    except Exception as e:
        sys.stderr.write(f"[YF ETH-USD] {e}\n"); return None
    dates, values = [], []
    for t, v in zip(ts, close):
        if v is None: continue
        dates.append(_iso(t)); values.append(float(v))
    if not dates: return None
    d, v = _weekly(dates, values)
    return {"dates": d, "values": v, "source_url": "https://finance.yahoo.com/quote/ETH-USD/", "unit": "USD"}


def fetch_defi_marketcap():
    try:
        data = http_get_json("https://api.coingecko.com/api/v3/global/decentralized_finance_defi", timeout=20)
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


def fetch_eth_addresses():
    """Adresses Ethereum uniques cumulées (Etherscan, CSV public) → fin de mois."""
    try:
        t = http_get_text("https://etherscan.io/chart/address?output=csv", timeout=40, accept="text/csv,*/*")
    except Exception as e:
        sys.stderr.write(f"[Etherscan] {e}\n"); return None
    bym = OrderedDict()
    last = None
    for line in t.splitlines()[1:]:
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 3: continue
        try:
            d = datetime.strptime(parts[0], "%m/%d/%Y").date().isoformat(); v = float(parts[2])
        except ValueError:
            continue
        bym[d[:7]] = (d, v); last = (d, v)
    if not last or last[1] < 1e8:
        return None
    pts = list(bym.values())
    return {"dates": [d for d, _ in pts], "values_mn": [round(v / 1e6, 2) for _, v in pts],
            "last_date": last[0], "last_mn": round(last[1] / 1e6, 1),
            "source_url": "https://etherscan.io/chart/address"}


def fetch_dex_cex():
    url = ("https://data.tbstat.com/dashboard/"
           "openfinance_dexnoncustodial_dextocexspottradevolumeupdated_monthly_others.json")
    try:
        d = http_get_json(url, timeout=30)
        series = d["Series"]
        if isinstance(series, str):
            import ast
            series = ast.literal_eval(series)
        data = next(iter(series.values()))["Data"]
    except Exception as e:
        sys.stderr.write(f"[TheBlock dex/cex] {e}\n"); return None
    pts = []
    for r in data:
        m = _iso(r["Timestamp"])[:7]
        if m >= MOIS_COURANT:      # mois en cours = partiel → jamais publié
            continue
        v = float(r["Result"])
        if not pts and v < 0.5:    # la série démarre à zéro avant la couverture réelle
            continue
        pts.append((m, round(v, 2)))
    if len(pts) < 24: return None
    return {"months": [m for m, _ in pts], "values": [v for _, v in pts],
            "definition": "volume au comptant des échanges décentralisés (DEX) en % de celui des plateformes centralisées (CEX)",
            "source_url": "https://www.theblock.co/data/decentralized-finance/dex-non-custodial/dex-to-cex-spot-trade-volume"}


def _rsc_payload(html):
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html, re.S)
    return "".join(json.loads('"' + c + '"') for c in chunks)


def _rsc_array_after(s, title):
    m = re.search(re.escape(title), s)
    if not m: return None
    k = s.find('"data":[', m.end())
    if k < 0: return None
    i = k + len('"data":'); depth = 0
    for j in range(i, len(s)):
        if s[j] == "[": depth += 1
        elif s[j] == "]":
            depth -= 1
            if depth == 0:
                return json.loads(s[i:j + 1])
    return None


def fetch_bundlebear():
    url = "https://www.bundlebear.com/erc4337-overview/all/month"
    try:
        s = _rsc_payload(http_get_text(url, timeout=40, accept="text/html,*/*"))
        acc = _rsc_array_after(s, "Monthly Active Smart Accounts")
        ops = _rsc_array_after(s, "Monthly Sucessful UserOps") or _rsc_array_after(s, "Monthly Successful UserOps")
    except Exception as e:
        sys.stderr.write(f"[BundleBear] {e}\n"); return None
    if not acc or "NUM_ACCOUNTS" not in acc[0]:
        return None
    a, o, n = defaultdict(int), defaultdict(int), defaultdict(set)
    for r in acc:
        a[r["DATE"][:7]] += int(r["NUM_ACCOUNTS"]); n[r["DATE"][:7]].add(r["CHAIN"])
    for r in ops or []:
        if "NUM_USEROPS" in r: o[r["DATE"][:7]] += int(r["NUM_USEROPS"])
    months = sorted(m for m in a if m < MOIS_COURANT)
    return {m: {"accounts": a[m], "userops": o.get(m), "chains": len(n[m])} for m in months}


def fetch_depinscan():
    try:
        t = http_get_text("https://depinscan.io/", timeout=40, accept="text/html,*/*")
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', t, re.S)
        lst = json.loads(m.group(1))["props"]["pageProps"]["projectList"]
    except Exception as e:
        sys.stderr.write(f"[DePINscan] {e}\n"); return None
    by = {x.get("p_id"): x for x in lst}
    rows = []
    for pid, name, unit in DEPIN_NETWORKS:
        x = by.get(pid)
        if not x: continue
        try:
            dev = int(float(x.get("total_devices") or 0))
        except ValueError:
            dev = 0
        if dev <= 0: continue
        try:
            mc = round(float(x.get("market_cap") or 0) / 1e6, 1)
        except ValueError:
            mc = None
        rows.append({"id": pid, "name": name, "units": dev, "metric": unit,
                     "token": x.get("token"), "mcap_musd": mc})
    if len(rows) < 4: return None
    return {"as_of": NOW.date().isoformat(), "rows": rows, "source_url": "https://depinscan.io/"}


# ════════════════════════════════════════════════════════════════
# PAYLOAD
# ════════════════════════════════════════════════════════════════

def build_payload(prev):
    ok, failed = [], []
    last_ok = dict((prev.get("meta") or {}).get("sources_last_ok") or {})
    now_iso = NOW.isoformat(timespec="seconds")

    def mark(name, success):
        (ok if success else failed).append(name)
        if success: last_ok[name] = now_iso

    def keep_prev(key):
        v = prev.get(key)
        if isinstance(v, dict):
            v = dict(v); v["stale"] = True
        return v

    defi_tvl = fetch_defillama_total_tvl(); mark("DefiLlama:total_tvl", bool(defi_tvl))
    defi_tvl = defi_tvl or keep_prev("defi_tvl_total")
    aave_tvl = fetch_defillama_protocol("aave-v3", "Aave V3"); mark("DefiLlama:aave-v3", bool(aave_tvl))
    aave_tvl = aave_tvl or keep_prev("aave_tvl")
    uni_tvl = fetch_defillama_protocol("uniswap-v3", "Uniswap V3"); mark("DefiLlama:uniswap-v3", bool(uni_tvl))
    uni_tvl = uni_tvl or keep_prev("uniswap_tvl")
    lido_tvl = fetch_defillama_protocol("lido", "Lido"); mark("DefiLlama:lido", bool(lido_tvl))
    lido_tvl = lido_tvl or keep_prev("lido_tvl")

    protos = fetch_protocols_tvl(); mark("DefiLlama:tvl_protocoles", bool(protos))
    if protos:
        prev_by = {r.get("slug"): r for r in (prev.get("top_defi_protocols") or []) if isinstance(r, dict)}
        for r in protos:   # protocole isolé en échec : dernière valeur collectée
            if r["tvl_bn"] is None and prev_by.get(r["slug"], {}).get("tvl_bn") is not None:
                r["tvl_bn"] = prev_by[r["slug"]]["tvl_bn"]
        protos_asof = NOW.date().isoformat()
    else:
        protos = prev.get("top_defi_protocols"); protos_asof = prev.get("top_defi_asof")

    univol = fetch_uniswap_volume(); mark("DefiLlama:uniswap_volume", bool(univol))
    univol = univol or keep_prev("uniswap_volume")

    eth_px = fetch_yahoo_eth(); mark("YF:ETH-USD", bool(eth_px))
    eth_px = eth_px or keep_prev("eth_price")
    defi_snap = fetch_defi_marketcap(); mark("CoinGecko:defi_global", bool(defi_snap))
    defi_snap = defi_snap or keep_prev("defi_snapshot")

    addr = fetch_eth_addresses(); mark("Etherscan:address", bool(addr))
    addr = addr or keep_prev("eth_addresses_monthly")

    dexcex = fetch_dex_cex(); mark("TheBlock:dex_cex", bool(dexcex))
    dexcex = dexcex or keep_prev("dex_cex_ratio")

    # ERC-4337 : fusion mois par mois (la source ne publie qu'une fenêtre glissante)
    bb = fetch_bundlebear(); mark("BundleBear:erc4337", bool(bb))
    hist = {}
    pv = prev.get("erc4337_monthly") or {}
    for m, ac, uo in zip(pv.get("months", []), pv.get("accounts_m", []), pv.get("userops_m", [])):
        hist[m] = {"accounts": ac * 1e6 if ac is not None else None,
                   "userops": uo * 1e6 if uo is not None else None}
    for m, r in (bb or {}).items():
        hist[m] = {"accounts": r["accounts"], "userops": r["userops"]}
    erc = None
    if hist:
        ms = sorted(m for m in hist if m < MOIS_COURANT)
        erc = {"months": ms,
               "accounts_m": [round(hist[m]["accounts"] / 1e6, 2) if hist[m]["accounts"] else None for m in ms],
               "userops_m": [round(hist[m]["userops"] / 1e6, 2) if hist[m]["userops"] else None for m in ms],
               "definition": "comptes intelligents ERC-4337 ayant agi dans le mois, somme des réseaux suivis "
                             "(Ethereum, Base, Arbitrum, Optimism, Polygon…) — un même compte actif sur deux "
                             "réseaux compte deux fois",
               "source_url": "https://www.bundlebear.com/erc4337-overview/all",
               "stale": not bool(bb)}

    dep = fetch_depinscan(); mark("DePINscan", bool(dep))
    dep = dep or keep_prev("depin")

    # ── Champs de compatibilité (ancienne page), recalculés sur données réelles ──
    eth_annual = []
    if addr:
        ye = {}
        for d, v in zip(addr["dates"], addr["values_mn"]):
            ye[int(d[:4])] = v
        cur = NOW.year
        eth_annual = [{"year": y, "addresses_mn": round(v, 1)} for y, v in sorted(ye.items()) if y < cur]
    dex_annual = []
    if dexcex:
        by = defaultdict(list)
        for m, v in zip(dexcex["months"], dexcex["values"]):
            by[int(m[:4])].append(v)
        dex_annual = [{"year": y, "pct": round(sum(v) / len(v), 1)} for y, v in sorted(by.items()) if len(v) == 12]
    erc_q = []
    if erc:
        q = defaultdict(list)
        for m, u in zip(erc["months"], erc["userops_m"]):
            if u is not None: q[f"{m[:4]}-Q{(int(m[5:7]) - 1) // 3 + 1}"].append(u)
        erc_q = [{"period": k, "useops_mn": round(sum(v), 1)} for k, v in sorted(q.items()) if len(v) == 3]

    meta = {
        "updated_at":      now_iso,
        "updated_at_unix": int(time.time()),
        "sources_ok":      ok,
        "sources_failed":  failed,
        "sources_last_ok": last_ok,
        "doc_version":     "2.0",
    }
    payload = {
        "meta": meta,
        "defi_tvl_total":  defi_tvl,
        "aave_tvl":        aave_tvl,
        "uniswap_tvl":     uni_tvl,
        "lido_tvl":        lido_tvl,
        "uniswap_volume":  univol,
        "eth_price":       eth_px,
        "defi_snapshot":   defi_snap,
        "eth_addresses_monthly": addr,
        "dex_cex_ratio":   dexcex,
        "erc4337_monthly": erc,
        "depin":           dep,
        "top_defi_protocols": protos,
        "top_defi_asof":   protos_asof,
        "ens_registrations": [{"year": y, "registered_mn": v} for y, v in ENS_REGISTRATIONS_MN],
        "social_compare":  [{"platform": p, "type": t, "users_mn": u, "users_label": lab,
                             "data_owner": o, "monetisation": m, "censorship_resist": cr}
                            for p, t, u, lab, o, m, cr in SOCIAL_WEB2_VS_WEB3],
        "gamestop_timeline": [{"date": d, "event": e} for d, e in GAMESTOP_TIMELINE],
        # ── compatibilité ──
        "eth_addresses":   eth_annual or prev.get("eth_addresses"),
        "dex_volume_share": dex_annual or prev.get("dex_volume_share"),
        "erc4337_useops":  erc_q or prev.get("erc4337_useops"),
        "depin_networks":  (dep or {}).get("rows") or prev.get("depin_networks"),
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
    if _OUT_OVERRIDE:
        return
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
        prev = json.loads(PREV_JSON.read_text()) if PREV_JSON.exists() else {}
    except Exception:
        prev = {}
    try:
        payload, n_ok, n_fail = build_payload(prev)
    except Exception as e:
        import traceback; traceback.print_exc()
        sys.stderr.write(f"[FATAL] {e}\n"); sys.exit(2)
    write_outputs(payload)
    dt = time.time() - t0
    sys.stdout.write(f"[these_web3] OK · {n_ok} sources, {n_fail} failed · {dt:.1f}s\n")


if __name__ == "__main__":
    main()
