#!/usr/bin/env python3
"""Fetch RSS feeds (crypto + macro) et injecte dans News_Crypto.html.
Reproduit la logique du chunk R fetch_news (max 20 items/feed, dedup par titre, tri par date).
"""
import os
import feedparser
import json, sys, re, time
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "news_cache.json"
HTML_FILE  = Path(os.path.expanduser("~/Desktop/Site_Crypto_Finance/News_Crypto.html"))
# GARDE DE FRAICHEUR — le collecteur ne travaille que si son cache a depasse cet age.
# MESURE DU 21/08/2026, qui a corrige le reglage : le seau 5min passe REELLEMENT
# toutes les 5 minutes (mediane relevee sur fj_news, qui n'a aucune garde), mais
# cette valeur a 1 h ne laissait ecrire qu'UNE fois par heure. Releve des ecritures
# publiees : 05:46, 06:49, 07:49, 08:52, 09:53, 10:57, 11:57, 12:59 — une par heure,
# pendant que la collecte tournait douze fois plus souvent. Le site servait donc de
# l'actualite vieille d'une heure sans qu'aucun passage n'ait echoue.
# A 10 minutes, la garde s'ouvre un passage sur deux : environ 6 ecritures par heure.
# C'est le reglage demande — inutile de descendre a la minute, une depeche ne perd
# pas sa valeur en dix minutes, et vingt flux RSS interroges toutes les cinq minutes
# seraient un gaspillage pour des sources qui ne publient pas plus vite.
# Sans effet sur la machine d'origine : son enveloppe launchd passe --force, qui
# traverse la garde.
CACHE_MAX_HOURS = 10 / 60.0   # 10 minutes

CRYPTO_SOURCES = [
    ("https://cointelegraph.com/rss",                   "CoinTelegraph"),
    ("https://coinacademy.fr/feed/",                    "CoinAcadémie"),
    ("https://cryptoast.fr/feed/",                      "Cryptoast"),
    ("https://www.bfmtv.com/rss/crypto/",               "BFM Crypto"),
    ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
    ("https://decrypt.co/feed",                         "Decrypt"),
    ("https://theblock.co/rss.xml",                     "The Block"),
    ("https://blockworks.co/feed",                      "Blockworks"),
    ("https://bitcoinmagazine.com/.rss/full/",          "Bitcoin Magazine"),
    ("https://www.thedefiant.io/feed",                  "The Defiant"),
]

MACRO_SOURCES = [
    ("https://www.bfmtv.com/rss/economie/",                          "BFM Économie"),
    ("https://www.lemonde.fr/economie/rss_full.xml",                 "Le Monde Éco"),
    ("https://www.lesechos.fr/rss/rss_une_titres.xml",               "Les Echos"),
    ("https://feeds.reuters.com/reuters/businessNews",               "Reuters Business"),
    ("https://feeds.bloomberg.com/markets/news.rss",                 "Bloomberg Markets"),
    ("https://www.ft.com/?format=rss",                               "Financial Times"),
    ("https://www.cnbc.com/id/100003114/device/rss/rss.html",        "CNBC Economy"),
    ("https://www.ecb.europa.eu/rss/press.html",                     "BCE"),
    ("https://www.federalreserve.gov/feeds/press_all.xml",           "Fed Reserve"),
    ("https://www.imf.org/en/News/Rss?dtype=RSS",                    "FMI"),
]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _entry_image(e):
    # Try several common locations for an image url
    if getattr(e, "media_content", None):
        for m in e.media_content:
            if m.get("url"):
                return m["url"]
    if getattr(e, "media_thumbnail", None):
        for m in e.media_thumbnail:
            if m.get("url"):
                return m["url"]
    if getattr(e, "enclosures", None):
        for enc in e.enclosures:
            if enc.get("type", "").startswith("image") and enc.get("href"):
                return enc["href"]
            if enc.get("url"):
                return enc["url"]
    # Inline <img> in summary
    summary = getattr(e, "summary", "") or ""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', summary)
    if m:
        return m.group(1)
    return ""


def _entry_ts(e):
    for k in ("published_parsed", "updated_parsed", "created_parsed"):
        v = getattr(e, k, None)
        if v:
            try:
                return time.mktime(v)
            except Exception:
                pass
    return 0


def fetch_one(url, name, pole):
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": UA})
        items = []
        for e in feed.entries[:20]:
            title = (getattr(e, "title", "") or "").strip()
            if not title:
                continue
            link = getattr(e, "link", "") or "#"
            pub = getattr(e, "published", "") or getattr(e, "updated", "") or ""
            items.append({
                "title": title,
                "link": link,
                "pubDate": pub,
                "img": _entry_image(e),
                "src": name,
                "pole": pole,
                "categories": "",
                "_ts": _entry_ts(e),
            })
        sys.stderr.write(f"[{name}] {len(items)} items\n")
        return items
    except Exception as exc:
        sys.stderr.write(f"[{name}] ERROR: {exc}\n")
        return []


def dedup_sort(items):
    seen, out = set(), []
    for it in items:
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    out.sort(key=lambda x: x.get("_ts", 0), reverse=True)
    for it in out:
        it.pop("_ts", None)
    return out


def inject_into_html(crypto, macro, all_items):
    if not HTML_FILE.exists():
        sys.stderr.write(f"[News] {HTML_FILE} not found, skip\n")
        return
    try:
        html = HTML_FILE.read_text()
    except PermissionError as e:
        sys.stderr.write(f"[News] HTML read blocked by TCC (ok from launchd): {e}\n")
        return
    new_block = (
        "<!-- __NEWS_LIVE_START__ -->\n"
        "<script>\n"
        "var NEWS_CRYPTO=" + json.dumps(crypto, ensure_ascii=False) + ";\n"
        "var NEWS_MACRO="  + json.dumps(macro,  ensure_ascii=False) + ";\n"
        "var NEWS_ALL="    + json.dumps(all_items, ensure_ascii=False) + ";\n"
        "window.__NEWS_UPDATED__=" + json.dumps(datetime.now().strftime("%d/%m/%Y %H:%M")) + ";\n"
        "</script>\n"
        "<!-- __NEWS_LIVE_END__ -->"
    )
    pattern = re.compile(r"<!-- __NEWS_LIVE_START__ -->.*?<!-- __NEWS_LIVE_END__ -->", re.DOTALL)
    if pattern.search(html):
        html2 = pattern.sub(new_block, html)
        sys.stderr.write("[News] Markers found, replacing\n")
    else:
        # First-time: replace the original R-injected NEWS_CRYPTO/MACRO/ALL block
        # The R chunk emits 3 lines: var NEWS_CRYPTO=...; var NEWS_MACRO=...; var NEWS_ALL=...
        # We replace from first <script>var NEWS_CRYPTO= to closing </script> just after NEWS_ALL.
        legacy = re.compile(
            r"<script>var NEWS_CRYPTO=.*?</script>\s*<script>var NEWS_MACRO=.*?</script>\s*<script>var NEWS_ALL=.*?</script>",
            re.DOTALL,
        )
        if legacy.search(html):
            html2 = legacy.sub(new_block, html, count=1)
            sys.stderr.write("[News] First-time injection (replaced legacy R block)\n")
        else:
            sys.stderr.write("[News] Cannot locate insertion point, abort\n")
            return
    try:
        HTML_FILE.write_text(html2)
        sys.stderr.write(f"[News] Injected: {len(crypto)} crypto + {len(macro)} macro into {HTML_FILE.name}\n")
    except PermissionError as e:
        sys.stderr.write(f"[News] HTML write blocked by TCC (ok from launchd): {e}\n")


def main():
    if CACHE_FILE.exists() and "--force" not in sys.argv:
        age_h = (datetime.now().timestamp() - CACHE_FILE.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS:
            sys.stderr.write(f"[News] Cache fresh ({age_h:.1f}h)\n")
            return

    jobs = [(u, n, "crypto") for u, n in CRYPTO_SOURCES] + [(u, n, "macro") for u, n in MACRO_SOURCES]
    all_items = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(fetch_one, u, n, p) for u, n, p in jobs]
        for f in as_completed(futs):
            all_items.extend(f.result())

    crypto = dedup_sort([i for i in all_items if i["pole"] == "crypto"])
    macro  = dedup_sort([i for i in all_items if i["pole"] == "macro"])
    all_d  = dedup_sort(all_items)

    payload = {
        "crypto": crypto, "macro": macro, "all": all_d,
        "updated": datetime.now().isoformat(),
    }
    with open(CACHE_FILE, "w") as f:
        json.dump(payload, f, ensure_ascii=False)
    # Wrapper JS chargeable via <script src> (necessaire pour file:// car fetch JSON est bloque par CORS)
    JS_CACHE = CACHE_DIR / "news_cache.js"
    with open(JS_CACHE, "w") as f:
        f.write("window.__NEWS_LIVE__=" + json.dumps(payload, ensure_ascii=False) + ";\n")
    sys.stderr.write(f"[News] Cache: {len(crypto)} crypto + {len(macro)} macro\n")
    inject_into_html(crypto, macro, all_d)


if __name__ == "__main__":
    main()
