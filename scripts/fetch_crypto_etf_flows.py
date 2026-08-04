#!/usr/bin/env python3
"""
Crypto ETF Flows Fetcher - Farside Investors
=============================================
Recupere les flows journaliers des ETFs spot crypto US (BTC + ETH + SOL + HYPE).
Source : https://farside.co.uk (acces via curl_cffi pour bypass Cloudflare).

Output JSON structure :
{
  "ts_fetched": "2026-05-07T08:55:00Z",
  "btc": {
    "tickers": ["IBIT", "FBTC", ...],
    "issuers": {"IBIT": "BlackRock", ...},   # vide pour BTC (Farside ne les liste pas)
    "fees": {"IBIT": "0.25%", ...},
    "launch_date": "2024-01-11",
    "rows": [
      {"date": "2024-01-11", "flows": {"IBIT": 111.7, "FBTC": 227.0, ...}, "total": 655.3},
      ...
    ]
  },
  "eth": {...},
  "sol": {...}
}

Toutes les valeurs en millions USD (convention Farside).
- "-" -> null (pas de data)
- "(X.Y)" -> -X.Y (negatif)
- "X.Y" -> X.Y (positif)

Refresh recommande : 1x/jour apres cloture US (>= 22:00 UTC).
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    print("[ERR] curl_cffi non installe. pip3 install curl_cffi", file=sys.stderr)
    sys.exit(1)

import socket


def wait_for_network(host: str = "farside.co.uk", max_wait: int = 120) -> bool:
    """Attend que le DNS/reseau soit up avant de fetcher.

    launchd (calendaire ET les watchdogs StartInterval+RunAtLoad) se rejoue DES le
    reveil du Mac, souvent AVANT que le Wi-Fi/DNS soit reconnecte -> chaque fetch
    leve NameResolutionError et n'ecrit rien (cache fige silencieusement). Cette
    garde evite de bruler les retries [0,30,60,120] pendant la fenetre DNS-morte
    du reveil. Backoff 3->15s, on continue quand meme apres max_wait (le prochain
    cycle horaire reessaiera). Cf. [[project_freshness_watchdog]] incident 2026-07-20.
    """
    waited, delay = 0, 3
    while waited < max_wait:
        try:
            socket.getaddrinfo(host, 443)
            return True
        except OSError:
            time.sleep(delay)
            waited += delay
            delay = min(delay + 3, 15)
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        print(f"[net][WARN] {host} injoignable apres {max_wait}s — on tente quand meme",
              file=sys.stderr)
        return False


# ─── Config ────────────────────────────────────────────────────────────
ASSETS = {
    "btc": {
        "url": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
        "label": "Bitcoin",
        "price_symbol": "BTC-USD",
        "coingecko_id": "bitcoin",
        "max_supply": 21_000_000,
    },
    "eth": {
        "url": "https://farside.co.uk/ethereum-etf-flow-all-data/",
        "label": "Ethereum",
        "price_symbol": "ETH-USD",
        "coingecko_id": "ethereum",
        "max_supply": None,  # pas de cap dur
    },
    "sol": {
        "url": "https://farside.co.uk/sol/",
        "label": "Solana",
        "price_symbol": "SOL-USD",
        "coingecko_id": "solana",
        "max_supply": None,  # inflationnaire (~1.5%/an, decroissant)
    },
    "hype": {
        # Hyperliquid spot ETFs lances le 12 mai 2026 (BHYP Bitwise + THYP 21Shares).
        # URL Farside : /hyp/ (et non /hype/, attention).
        "url": "https://farside.co.uk/hyp/",
        "label": "Hyperliquid",
        # ATTENTION : sur Yahoo, "HYPE-USD" = Supreme Finance (memecoin a $0.000005).
        # Le vrai Hyperliquid est "HYPE32196-USD" (suffixe CoinMarketCap-style).
        "price_symbol": "HYPE32196-USD",
        "coingecko_id": "hyperliquid",
        "max_supply": 1_000_000_000,  # 1B HYPE supply max (genesis cap)
    },
}

# Fallback metadata (Farside ne donne issuers/fees que pour ETH ; on remplit BTC + SOL).
# Les fees sont les TER officiels publies par les emetteurs.
FALLBACK_META = {
    "btc": {
        "issuers": {
            "IBIT": "BlackRock", "FBTC": "Fidelity", "BITB": "Bitwise",
            "ARKB": "ARK / 21Shares", "BTCO": "Invesco", "EZBC": "Franklin",
            "BRRR": "Valkyrie", "HODL": "VanEck", "BTCW": "WisdomTree",
            "MSBT": "Hashdex", "GBTC": "Grayscale", "BTC":  "Grayscale Mini",
        },
        "fees": {
            "IBIT": "0.25%", "FBTC": "0.25%", "BITB": "0.20%", "ARKB": "0.21%",
            "BTCO": "0.25%", "EZBC": "0.19%", "BRRR": "0.25%", "HODL": "0.25%",
            "BTCW": "0.25%", "MSBT": "0.25%", "GBTC": "1.50%", "BTC":  "0.15%",
        },
    },
    "sol": {
        "issuers": {
            "BSOL": "Bitwise", "VSOL": "VanEck", "FSOL": "Fidelity",
            "TSOL": "21Shares", "SOEZ": "Franklin", "GSOL": "Grayscale",
        },
        # Fees deja fournis par Farside pour SOL, mais on garde un fallback
        "fees": {
            "BSOL": "0.20%", "VSOL": "0.30%", "FSOL": "0.25%",
            "TSOL": "0.21%", "SOEZ": "0.19%", "GSOL": "0.35%",
        },
    },
    "hype": {
        # 3 ETFs HYPE listes (BHYP Bitwise + THYP 21Shares + HYPG Grayscale Staking,
        # ce dernier lance le 3 juin 2026 sur Nasdaq avec le fee US le plus bas)
        "issuers": {
            "BHYP": "Bitwise", "THYP": "21Shares", "HYPG": "Grayscale",
        },
        # TER officiels (source HypurrIntel cross-check)
        "fees": {
            "BHYP": "0.34%", "THYP": "0.30%", "HYPG": "0.29%",
        },
    },
}

# ─── CoinGecko : circulating supply ───────────────────────────────────
def fetch_circulating_supply(coingecko_id: str) -> float | None:
    """Recupere le circulating supply via CoinGecko /coins/markets."""
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = "?vs_currency=usd&ids=" + coingecko_id
    try:
        r = curl_requests.get(url + params, impersonate="chrome120", timeout=20)
        if r.status_code != 200:
            print(f"[supply][ERR] {coingecko_id}: HTTP {r.status_code}", file=sys.stderr)
            return None
        j = r.json()
        if not j:
            return None
        cs = j[0].get("circulating_supply")
        if cs is None:
            return None
        print(f"[supply] {coingecko_id}: {cs:,.0f} circulating")
        return float(cs)
    except Exception as e:
        print(f"[supply][ERR] {coingecko_id}: {e}", file=sys.stderr)
        return None

# ─── Fetch prix Yahoo (close journalier) ──────────────────────────────
YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"

def fetch_price_history(symbol: str) -> dict:
    """
    Recupere les close daily Yahoo pour le symbol (ex: 'BTC-USD').
    Range = max disponible. Retourne dict { 'YYYY-MM-DD': close, ... }.
    """
    url = f"{YAHOO_BASE}{symbol}?range=5y&interval=1d"
    try:
        r = curl_requests.get(url, impersonate="chrome120", timeout=30)
        if r.status_code != 200:
            print(f"[price][ERR] {symbol}: HTTP {r.status_code}", file=sys.stderr)
            return {}
        j = r.json()
        result = j.get("chart", {}).get("result", [])
        if not result:
            return {}
        timestamps = result[0].get("timestamp", []) or []
        closes = (result[0].get("indicators", {}).get("quote", [{}])[0].get("close")) or []
        out = {}
        for ts, c in zip(timestamps, closes):
            if c is None:
                continue
            d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            out[d] = round(float(c), 2)
        print(f"[price] {symbol}: {len(out)} closes")
        return out
    except Exception as e:
        print(f"[price][ERR] {symbol}: {e}", file=sys.stderr)
        return {}


def fetch_price_history_intraday(symbol: str) -> dict:
    """
    Recupere les close HORAIRES Yahoo (interval=1h, range=60d) pour densifier la
    courbe de prix sur les fenetres courtes (ex: HYPE qui n'a que ~20j d'historique
    ETF -> 20 points en daily, mais ~480 points en horaire). Cle = datetime ISO
    minute 'YYYY-MM-DDTHH:MM' (UTC). Retourne {} si indisponible.
    """
    url = f"{YAHOO_BASE}{symbol}?range=60d&interval=1h"
    try:
        r = curl_requests.get(url, impersonate="chrome120", timeout=30)
        if r.status_code != 200:
            print(f"[price_h][ERR] {symbol}: HTTP {r.status_code}", file=sys.stderr)
            return {}
        j = r.json()
        result = j.get("chart", {}).get("result", [])
        if not result:
            return {}
        timestamps = result[0].get("timestamp", []) or []
        closes = (result[0].get("indicators", {}).get("quote", [{}])[0].get("close")) or []
        out = {}
        for ts, c in zip(timestamps, closes):
            if c is None:
                continue
            d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")
            out[d] = round(float(c), 4)
        print(f"[price_h] {symbol}: {len(out)} closes horaires")
        return out
    except Exception as e:
        print(f"[price_h][ERR] {symbol}: {e}", file=sys.stderr)
        return {}

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "crypto_etf_cache.json"
# Pattern site : on ecrit aussi le .js dans Library/Caches (TCC-safe pour launchd)
# et on attend que Desktop ait un symlink vers ce fichier (cf. macro_fred_cache.js,
# narratives_cache.js). Ecrire directement dans Desktop echoue depuis launchd
# car les scripts dans ~/Library/Application Support/ n'ont pas l'autorisation TCC
# d'ecrire sur le Desktop sans Full Disk Access.
SITE_JS = CACHE_DIR / "crypto_etf_cache.js"

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


# ─── Parsing helpers ──────────────────────────────────────────────────
def parse_date(s: str) -> str | None:
    """'11 Jan 2024' -> '2024-01-11'."""
    m = re.match(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", s.strip())
    if not m:
        return None
    day, mon_s, year = m.groups()
    mon = MONTHS.get(mon_s[:3])
    if mon is None:
        return None
    return f"{int(year):04d}-{mon:02d}-{int(day):02d}"


def parse_value(s: str) -> float | None:
    """Farside: '-' -> None, '(X.Y)' -> -X.Y, 'X.Y' -> X.Y, 'X,Y' -> X*1000+Y."""
    s = s.strip().replace(",", "")  # 1,234.5 -> 1234.5
    if s in ("", "-", "—"):
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def extract_rows(html: str) -> list[list[str]]:
    """Extrait toutes les <tr>...</tr> en listes de cellules text."""
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        cells = [strip_html(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.DOTALL)]
        out.append(cells)
    return out


def parse_table(html: str) -> dict:
    """
    Identifie 1) la ligne tickers, 2) la ligne issuers (optionnelle), 3) la ligne fees,
    4) les lignes de data, et construit la structure normalisee.
    """
    rows = extract_rows(html)
    # Trouve la 1ere ligne contenant un ticker reconnaissable (tout-uppercase 3-5 lettres)
    # OU la ligne avec en 1ere cell "Date" (BTC) ou la ligne juste apres "Date" si elle existe
    ticker_row_idx = None
    issuer_row_idx = None
    fee_row_idx = None
    data_rows = []

    # 1ere passe : trouve la ligne avec date pattern
    for i, r in enumerate(rows):
        if r and re.match(r"\d{1,2}\s+\w+\s+\d{4}", r[0] if r else ""):
            data_rows.append((i, r))
    if not data_rows:
        return {"error": "no data rows found"}

    first_data_idx = data_rows[0][0]

    # 2eme passe : retrouve les en-tetes en remontant depuis first_data_idx
    # On cherche dans les ~6 lignes precedentes
    for i in range(max(0, first_data_idx - 6), first_data_idx):
        cells = rows[i]
        if not cells:
            continue
        # Detection ticker row : majoritairement tickers (uppercase 3-5)
        # Seuil bas (>=2) pour supporter les nouvelles pages avec peu d'ETFs (ex: HYPE -> BHYP+THYP).
        n_ticker_cells = sum(1 for c in cells if re.fullmatch(r"[A-Z]{3,5}", c))
        if n_ticker_cells >= 2:
            ticker_row_idx = i
        # Issuer row : contient "Blackrock", "Fidelity", etc. (mots avec majuscule)
        n_issuer_cells = sum(1 for c in cells if c and c[0].isupper() and " " not in c.strip("()") and len(c) > 3 and not re.fullmatch(r"[A-Z]{3,5}", c) and "%" not in c)
        if n_issuer_cells >= 3 and ticker_row_idx is None:
            issuer_row_idx = i
        # Fee row
        if cells and cells[0].lower() == "fee":
            fee_row_idx = i

    if ticker_row_idx is None:
        # BTC : la ligne header est "Date | IBIT | FBTC | ..." dans une seule passe
        for i in range(max(0, first_data_idx - 4), first_data_idx):
            if rows[i] and rows[i][0] == "Date":
                ticker_row_idx = i
                break

    if ticker_row_idx is None:
        return {"error": "ticker row not found"}

    raw_header = rows[ticker_row_idx]
    # Identifier les colonnes ticker (exclure "Date", "Total", celles vides)
    n_cols = len(raw_header)
    ticker_cols = []
    for ci, h in enumerate(raw_header):
        if h in ("Date", "Total", ""):
            continue
        ticker_cols.append((ci, h))
    tickers = [t for _, t in ticker_cols]

    # Issuer mapping si dispo
    issuers = {}
    if issuer_row_idx is not None:
        issuer_row = rows[issuer_row_idx]
        for ci, t in ticker_cols:
            if ci < len(issuer_row):
                v = issuer_row[ci]
                if v:
                    issuers[t] = v

    # Fee mapping si dispo
    fees = {}
    if fee_row_idx is not None:
        fee_row = rows[fee_row_idx]
        for ci, t in ticker_cols:
            if ci < len(fee_row):
                v = fee_row[ci]
                if v:
                    fees[t] = v

    # Data rows : parse + normalise
    parsed_rows = []
    # Total col : scan TOUS les rows d'entete (issuer/ticker/etc.) car ETH met "Total"
    # dans la ligne issuer, BTC dans la ligne ticker, SOL dans une ligne separee.
    total_col_idx = None
    for hi in range(max(0, first_data_idx - 6), first_data_idx):
        for ci, h in enumerate(rows[hi] if hi < len(rows) else []):
            if h.lower() == "total":
                total_col_idx = ci
                break
        if total_col_idx is not None:
            break

    date_col_idx = 0  # par defaut col 0
    # ETH/SOL : la 1ere col est vide dans l'en-tete, mais data commence par date
    # Donc date_col_idx = 0 fonctionne pour tous

    for _, r in data_rows:
        if len(r) < n_cols:
            # Pad avec None
            r = r + [""] * (n_cols - len(r))
        date_iso = parse_date(r[date_col_idx])
        if date_iso is None:
            continue
        flows = {}
        for ci, t in ticker_cols:
            if ci < len(r):
                flows[t] = parse_value(r[ci])
        total = parse_value(r[total_col_idx]) if total_col_idx is not None and total_col_idx < len(r) else None
        # Filtre : si toutes les valeurs sont None, on skip (ligne future "07 May 2026 - - -")
        if all(v is None for v in flows.values()) and total in (None, 0.0):
            continue
        parsed_rows.append({"date": date_iso, "flows": flows, "total": total})

    # Tri par date
    parsed_rows.sort(key=lambda x: x["date"])

    return {
        "tickers": tickers,
        "issuers": issuers,
        "fees": fees,
        "launch_date": parsed_rows[0]["date"] if parsed_rows else None,
        "rows": parsed_rows,
    }


# ─── Fetcher principal ────────────────────────────────────────────────
def fetch_one(asset_key: str, cfg: dict) -> dict:
    """Fetch Farside avec retry resilient (DNS/network/Cloudflare 5xx)."""
    url = cfg["url"]
    print(f"[fetch] {asset_key.upper()}: {url}")
    # Retry 4 tentatives avec backoff progressif. Couvre DNS hiccups + Cloudflare 5xx transients.
    backoffs = [0, 30, 60, 120]  # secondes
    last_err = None
    for attempt, wait in enumerate(backoffs, start=1):
        if wait:
            print(f"[fetch] {asset_key}: retry {attempt}/{len(backoffs)} apres {wait}s", file=sys.stderr)
            time.sleep(wait)
        try:
            # Deux façons de demander la page, essayées dans cet ordre. MESURÉ, et
            # contre-intuitif : depuis une adresse résidentielle, l'usurpation TLS passe
            # et la requête nue se fait jeter ; depuis une IP de datacenter (les runners),
            # c'est l'INVERSE — une empreinte Chrome venant d'un serveur est plus suspecte
            # qu'un client honnête. L'ancien code abandonnait dès le premier 4xx, ce qui
            # aurait fait échouer les flux ETF à chaque exécution dans le cloud.
            import _cloud_sources as _cs
            html = _cs.guarded_html(url, impersonate_first=True)
            parsed = parse_table(html)
            if "error" in parsed:
                print(f"[parse][ERR] {asset_key}: {parsed['error']}", file=sys.stderr)
                return parsed
            print(f"[fetch] {asset_key.upper()}: {len(parsed['rows'])} rows, "
                  f"{len(parsed['tickers'])} ETFs, since {parsed['launch_date']} "
                  f"(voie {_cs.LAST_SOURCE.get('guarded_html')})")
            return parsed
        except Exception as e:
            last_err = str(e)
            print(f"[fetch][WARN] {asset_key}: {e}, tentative {attempt}/{len(backoffs)}", file=sys.stderr)
    print(f"[fetch][ERR] {asset_key}: epuise apres {len(backoffs)} tentatives : {last_err}", file=sys.stderr)
    return {"error": last_err or "unknown"}


def merge_rows(prev_rows, new_rows):
    """
    Fusionne l'historique deja en cache (prev_rows) avec le fetch courant (new_rows),
    UNION par date. Indispensable car Farside sert certaines pages (/hyp/, /sol/) en
    FENETRE GLISSANTE (~2-3 semaines) : sans fusion, chaque run ECRASE les rows et on
    perd l'historique accumule (bug decouvert 2026-07-16 : HYPE reduit a 13j alors que
    les ETF tradent depuis mi-mai). Les valeurs du fetch courant font autorite pour les
    dates qu'il couvre (chiffres Farside parfois revises J+1) ; les dates plus anciennes,
    absentes du fetch courant, sont conservees depuis le cache.
    """
    if not prev_rows:
        return new_rows
    by_date = {}
    for r in prev_rows:
        d = r.get("date")
        if d:
            by_date[d] = r
    for r in new_rows:  # le fetch courant ecrase la date correspondante (valeurs revisees)
        d = r.get("date")
        if d:
            by_date[d] = r
    return sorted(by_date.values(), key=lambda x: x["date"])


def main(force: bool = False) -> int:
    # Garde anti-course « reveil du Mac » : attend que le DNS soit up avant de
    # bruler les retries Farside dans le vide (cache fige silencieusement sinon).
    wait_for_network("farside.co.uk", max_wait=120)

    # Charge l'ancien cache pour fallback en cas d'echec de fetch secondaire
    # (Yahoo / CoinGecko peuvent rate-limiter -> sans fallback on perd la donnee).
    prev_cache = {}
    if OUT_JSON.exists():
        try:
            with open(OUT_JSON) as f:
                prev_cache = json.load(f)
        except Exception as e:
            print(f"[main][WARN] ancien cache non parsable : {e}", file=sys.stderr)

    payload = {
        "ts_fetched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "farside.co.uk",
    }
    ok_count = 0
    for key, cfg in ASSETS.items():
        prev_pack = prev_cache.get(key, {}) if isinstance(prev_cache, dict) else {}
        result = fetch_one(key, cfg)
        if "error" not in result:
            ok_count += 1
            # UNION de l'historique : ne JAMAIS perdre les dates deja vues meme si
            # Farside sert desormais une fenetre glissante plus courte (cf. merge_rows).
            merged_rows = merge_rows(prev_pack.get("rows", []), result.get("rows", []))
            result["rows"] = merged_rows
            if merged_rows:
                result["launch_date"] = merged_rows[0]["date"]
            # Union des tickers (un ETF sorti de la fenetre glissante garde sa colonne)
            prev_tk = prev_pack.get("tickers", []) or []
            for t in prev_tk:
                if t not in result.get("tickers", []):
                    result.setdefault("tickers", []).append(t)
            # Merge fallback metadata (issuers + fees) si Farside n'a pas fourni
            fb = FALLBACK_META.get(key, {})
            if not result.get("issuers"):
                result["issuers"] = fb.get("issuers", {})
            else:
                merged = dict(fb.get("issuers", {}))
                merged.update(result["issuers"])
                result["issuers"] = merged
            if not result.get("fees"):
                result["fees"] = fb.get("fees", {})
            else:
                merged_f = dict(fb.get("fees", {}))
                merged_f.update(result["fees"])
                result["fees"] = merged_f
            # Price history Yahoo (BTC-USD / ETH-USD / SOL-USD) avec fallback ancien
            ph = fetch_price_history(cfg["price_symbol"])
            if ph:
                result["price_history"] = ph
            elif prev_pack.get("price_history"):
                result["price_history"] = prev_pack["price_history"]
                print(f"[main] {key}: price_history STALE conserve depuis ancien cache", file=sys.stderr)
            # Price history HORAIRE (60j) pour densifier les courbes sur fenetres courtes
            phh = fetch_price_history_intraday(cfg["price_symbol"])
            if phh:
                result["price_history_h"] = phh
            elif prev_pack.get("price_history_h"):
                result["price_history_h"] = prev_pack["price_history_h"]
                print(f"[main] {key}: price_history_h STALE conserve depuis ancien cache", file=sys.stderr)
            # Circulating supply via CoinGecko avec fallback ancien
            cs = fetch_circulating_supply(cfg["coingecko_id"])
            if cs:
                result["circulating_supply"] = cs
            elif prev_pack.get("circulating_supply"):
                result["circulating_supply"] = prev_pack["circulating_supply"]
                print(f"[main] {key}: circulating_supply STALE conserve depuis ancien cache", file=sys.stderr)
            if cfg.get("max_supply"):
                result["max_supply"] = cfg["max_supply"]
            # Fraicheur PAR ACTIF : ce pack vient d'etre rafraichi depuis Farside.
            # Permet de detecter qu'UN actif est gele meme si le ts_fetched GLOBAL
            # (et donc le mtime du cache) reste frais grace aux autres actifs OK.
            result["asset_ts"] = payload["ts_fetched"]
            result["stale"] = False
        else:
            # Farside a echoue pour CET actif : on conserve l'ancien pack mais on le
            # MARQUE gele (stale) en preservant son ancien asset_ts -> jamais presente
            # comme frais alors que ses flux ne bougent plus (ex. /sol/ ou /hyp/ renommes).
            if prev_pack and "rows" in prev_pack:
                print(f"[main][WARN] {key}: Farside fail, pack GELE depuis ancien cache (stale)", file=sys.stderr)
                result = dict(prev_pack)
                result["stale"] = True
                result.setdefault("asset_ts", prev_cache.get("ts_fetched"))
            else:
                result["stale"] = True
                result["asset_ts"] = None
        payload[key] = result
        time.sleep(1.0)  # politesse vis a vis de Farside

    if ok_count == 0:
        print("[main][ERR] aucun fetch OK, on conserve l'ancien cache si dispo", file=sys.stderr)
        # Garde ancien cache si dispo
        if OUT_JSON.exists() and not force:
            return 1
        # Sinon ecrit quand meme un cache vide pour debug
    # Bilan fraicheur par actif : log proeminent si un actif est gele (detection
    # de la perte silencieuse d'un seul actif, BTC/ETH/SOL/HYPE).
    stale_assets = [k for k in ASSETS
                    if isinstance(payload.get(k), dict) and payload[k].get("stale")]
    if stale_assets:
        print(f"[main][WARN] actifs GELES (flux non rafraichis ce run) : {', '.join(stale_assets)}",
              file=sys.stderr)
    # Ecrit JSON
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    # Genere le .js (window.__CRYPTO_ETF_LIVE__ = ... ;)
    with open(SITE_JS, "w", encoding="utf-8") as f:
        f.write("window.__CRYPTO_ETF_LIVE__=")
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";")
    print(f"[main] OK -> {OUT_JSON} ({OUT_JSON.stat().st_size // 1024} KB) + {SITE_JS}")
    return 0


if __name__ == "__main__":
    force = "--force" in sys.argv
    sys.exit(main(force=force))
