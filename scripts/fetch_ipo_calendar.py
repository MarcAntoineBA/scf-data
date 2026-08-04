#!/usr/bin/env python3
"""Fetch upcoming IPOs via l'API publique Nasdaq.

Source : https://api.nasdaq.com/api/ipo/calendar?date=YYYY-MM
Free, no API key. Couvre IPO listées sur NYSE / NASDAQ / NYSE American.

Injecte un bloc JS `window.__IPO_LIVE__` dans News_Crypto.html.
"""
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "ipo_calendar_cache.json"
JS_CACHE = CACHE_DIR / "ipo_live.js"
HTML_FILE = Path.home() / "Desktop/Site_Crypto_Finance/News_Crypto.html"
CACHE_MAX_HOURS = 12

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
API = "https://api.nasdaq.com/api/ipo/calendar"

# Heuristic : entreprises connues / large cap / fintech / IA → impact high
# Liste libre, à enrichir au fil du temps
HIGH_IMPACT_KEYWORDS = [
    "spacex", "stripe", "klarna", "databricks", "discord", "shein",
    "circle", "kraken", "consensys", "openai", "anthropic", "ramp",
    "canva", "plaid", "chime", "instacart", "reddit", "arm holdings",
    "rivian", "mobileye", "snowflake", "palantir",
]
MED_IMPACT_KEYWORDS = ["ai", "artificial intelligence", "data", "fintech", "robotics",
                       "biotech", "quantum", "cyber", "cloud"]

# SPAC patterns à exclure (calendrier Nasdaq dominé par les SPAC)
SPAC_NAME_PATTERNS = ("acquisition corp", "acquisition i corp", "acquisition ii",
                      "acquisition iii", "acquisition iv", "acquisition v",
                      "acquisition vi", "merger corp", "blank check")


def is_spac(name, sym):
    """Heuristic SPAC detection : nom 'Acquisition Corp' ou ticker se terminant en U."""
    name_l = (name or "").lower()
    if any(p in name_l for p in SPAC_NAME_PATTERNS):
        return True
    # Ticker ending in 'U' (units) is a strong SPAC signal
    if sym and len(sym) >= 4 and sym.endswith("U"):
        return True
    return False


def fetch_month(date_str):
    """Récupère les IPO d'un mois donné (format YYYY-MM)."""
    try:
        r = requests.get(
            f"{API}?date={date_str}",
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=20,
        )
        if r.status_code != 200:
            sys.stderr.write(f"[IPO] {date_str}: HTTP {r.status_code}\n")
            return {}
        return r.json()
    except Exception as e:
        sys.stderr.write(f"[IPO] {date_str}: {type(e).__name__}: {e}\n")
        return {}


def parse_date(s):
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def classify_impact(name):
    name_l = name.lower()
    if any(kw in name_l for kw in HIGH_IMPACT_KEYWORDS):
        return "high"
    if any(kw in name_l for kw in MED_IMPACT_KEYWORDS):
        return "med"
    return "low"


def normalize(raw_data):
    """Convertit les rows Nasdaq en events au format du calendrier."""
    out = []
    seen = set()
    today = datetime.now()
    for raw in raw_data:
        data = raw.get("data") or {}
        # On scanne 'upcoming', 'priced' (récents), et 'filed' (en attente)
        for section in ("upcoming", "priced", "filed"):
            sec = data.get(section) or {}
            rows = sec.get("rows") if isinstance(sec, dict) else None
            if not rows:
                continue
            for row in rows:
                name = (row.get("companyName") or "").strip()
                sym = (row.get("proposedTickerSymbol") or row.get("symbol") or "").strip()
                d_str = (row.get("expectedPriceDate") or row.get("pricedDate")
                         or row.get("filedDate") or "").strip()
                deal = row.get("dealStatus") or section.capitalize()
                price_range = row.get("proposedSharePrice") or ""
                deal_size = row.get("dollarValueOfSharesOffered") or ""
                exchange = row.get("proposedExchange") or "NASDAQ"

                if not name or not d_str:
                    continue
                # Skip SPACs (Nasdaq calendar dominé par eux)
                if is_spac(name, sym):
                    continue
                parsed = parse_date(d_str)
                if not parsed:
                    continue
                # Lookback : 30j pour priced (IPO récente reste visible),
                # forward : 120j max
                if parsed < today - timedelta(days=30):
                    continue
                if parsed > today + timedelta(days=120):
                    continue

                key = (name.lower(), parsed.strftime("%Y-%m-%d"))
                if key in seen:
                    continue
                seen.add(key)

                impact = classify_impact(name)

                sub_parts = [exchange]
                if sym:
                    sub_parts.append(sym)
                if price_range:
                    sub_parts.append(f"prix {price_range}")
                if deal_size:
                    sub_parts.append(deal_size)
                sub_parts.append(deal)
                sub = " · ".join(sub_parts)

                # IPO listées en pré-market US ~9:30 ET (13:30 UTC)
                iso = parsed.strftime("%Y-%m-%dT13:30")

                # Construis URL recherche Nasdaq pour le ticker
                if sym:
                    url = f"https://www.nasdaq.com/market-activity/stocks/{sym.lower()}"
                else:
                    url = "https://www.nasdaq.com/market-activity/ipos"

                out.append({
                    "date": iso,
                    "name": f"{name} — IPO",
                    "sub": sub,
                    "impact": impact,
                    "cat": "ipo",
                    "btcHist": "",
                    "btcDir": "neut",
                    "note": f"Source : Nasdaq IPO calendar. Status : {deal}.",
                    "url": url,
                })
    out.sort(key=lambda x: x["date"])
    return out


# ════════════════════════════════════════════════════════════════════════════
# IPO CHINOISES À VENIR — Eastmoney (ajout 2026-07-28)
# ────────────────────────────────────────────────────────────────────────────
# Le calendrier ci-dessus ne couvre que NYSE/NASDAQ. C'est précisément cet angle
# mort qui a laissé passer CXMT (688825.SS), cotée le 27/07/2026 et devenue la
# 1re capitalisation chinoise (~465 Md$) : rien, nulle part, ne l'annonçait.
# Source : datacenter Eastmoney, gratuite et sans clé. Donne la date de
# SOUSCRIPTION (申购) et, quand elle est fixée, la date de COTATION.
# ════════════════════════════════════════════════════════════════════════════
EM_IPO_API = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
              "?sortColumns=APPLY_DATE&sortTypes=-1&pageSize=50&pageNumber=1"
              "&reportName=RPTA_APP_IPOAPPLY&columns=ALL&source=WEB&client=WEB")


def fetch_cn_ipos():
    try:
        r = requests.get(EM_IPO_API, timeout=25,
                         headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"})
        if r.status_code != 200:
            sys.stderr.write(f"[IPO] Eastmoney HTTP {r.status_code}\n")
            return []
        rows = ((r.json().get("result") or {}).get("data")) or []
    except Exception as e:
        sys.stderr.write(f"[IPO] Eastmoney: {type(e).__name__}: {e}\n")
        return []

    today = datetime.now()
    out = []
    for row in rows:
        code = (row.get("SECURITY_CODE") or "").strip()
        name = (row.get("SECURITY_NAME_ABBR") or "").strip()
        listing = (row.get("LISTING_DATE") or "")[:10]
        apply_d = (row.get("APPLY_DATE") or "")[:10]
        d_str = listing or apply_d
        if not code or not d_str:
            continue
        try:
            parsed = datetime.strptime(d_str, "%Y-%m-%d")
        except ValueError:
            continue
        if parsed < today - timedelta(days=30) or parsed > today + timedelta(days=120):
            continue
        # Place déduite du code : 688 = STAR, 300/301 = ChiNext, 8xx/920 = BSE.
        if code.startswith("688"):
            place, sfx = "SSE STAR Market", ".SS"
        elif code.startswith(("300", "301")):
            place, sfx = "Shenzhen ChiNext", ".SZ"
        elif code.startswith(("8", "920")):
            place, sfx = "Beijing Stock Exchange", ".BJ"
        elif code.startswith("6"):
            place, sfx = "Shanghai", ".SS"
        else:
            place, sfx = "Shenzhen", ".SZ"

        etape = "cotation" if listing else "souscription"
        sub = " · ".join([place, code + sfx, f"étape : {etape}"])
        # Ouverture des marchés chinois : 9h30 heure de Pékin = 01:30 UTC.
        iso = parsed.strftime("%Y-%m-%dT01:30")
        out.append({
            "date": iso,
            "name": f"{name or code} — IPO Chine",
            "sub": sub,
            # Les IPO chinoises du STAR Market sont le canal de cotation des
            # champions technologiques (semi-conducteurs, IA) : impact par défaut
            # relevé pour cette place, qui est celle qu'on a manquée.
            "impact": "high" if code.startswith("688") else "med",
            "cat": "ipo",
            "btcHist": "", "btcDir": "neut",
            "note": f"Source : Eastmoney (calendrier IPO Chine). Étape : {etape}.",
            "url": f"https://quote.eastmoney.com/{'sh' if sfx == '.SS' else 'sz'}{code}.html",
        })
    sys.stderr.write(f"[IPO] Chine : {len(out)} événements\n")
    return out


def inject_into_html(events):
    if not HTML_FILE.exists():
        sys.stderr.write(f"[IPO] {HTML_FILE} not found\n")
        return
    try:
        html = HTML_FILE.read_text()
    except PermissionError as e:
        sys.stderr.write(f"[IPO] HTML read blocked by TCC (ok from launchd): {e}\n")
        return

    block = (
        "// __IPO_LIVE_START__\n"
        "  window.__IPO_LIVE__ = " +
        json.dumps(events, ensure_ascii=False, separators=(",", ":")) + ";\n"
        "  window.__IPO_UPDATED__ = " +
        json.dumps(datetime.now().strftime("%d/%m/%Y %H:%M")) + ";\n"
        "  // __IPO_LIVE_END__"
    )
    pat = re.compile(r"// __IPO_LIVE_START__.*?// __IPO_LIVE_END__", re.DOTALL)
    if pat.search(html):
        html2 = pat.sub(block, html)
        sys.stderr.write("[IPO] Replacing JS block\n")
    else:
        m = re.search(r"(\s*)var\s+MACRO_EVENTS\s*=\s*\[", html)
        if not m:
            sys.stderr.write("[IPO] Cannot find MACRO_EVENTS marker, abort\n")
            return
        idx = m.start()
        html2 = html[:idx] + "\n  " + block + "\n" + html[idx:]
        sys.stderr.write("[IPO] First-time JS injection before MACRO_EVENTS\n")
    try:
        HTML_FILE.write_text(html2)
        sys.stderr.write(f"[IPO] Injected {len(events)} events\n")
    except PermissionError as e:
        sys.stderr.write(f"[IPO] HTML write blocked by TCC (ok from launchd): {e}\n")


def main():
    if CACHE_FILE.exists() and "--force" not in sys.argv:
        age_h = (datetime.now().timestamp() - CACHE_FILE.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS:
            sys.stderr.write(f"[IPO] Cache fresh ({age_h:.1f}h)\n")
            return

    today = datetime.now()
    raw = []
    # Scan : mois précédent (priced récents) + courant + 3 suivants
    base_y, base_m = today.year, today.month
    for offset in range(-1, 4):
        m = base_m + offset
        y = base_y + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        date_str = f"{y:04d}-{m:02d}"
        data = fetch_month(date_str)
        if data:
            raw.append(data)

    events = normalize(raw)
    events.extend(fetch_cn_ipos())
    events.sort(key=lambda x: x["date"])
    sys.stderr.write(f"[IPO] {len(events)} events normalized\n")

    payload = {"events": events, "updated": datetime.now().isoformat()}
    with open(CACHE_FILE, "w") as f:
        json.dump(payload, f, ensure_ascii=False)

    # Wrapper JS loadable via <script src> (resilient au blocage TCC sur News_Crypto.html)
    updated_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    with open(JS_CACHE, "w") as f:
        f.write(
            "window.__IPO_LIVE__=" + json.dumps(events, ensure_ascii=False, separators=(",", ":")) + ";\n"
            "window.__IPO_UPDATED__=" + json.dumps(updated_str) + ";\n"
        )
    sys.stderr.write(f"[IPO] JS cache: {len(events)} events\n")

    inject_into_html(events)


if __name__ == "__main__":
    main()
