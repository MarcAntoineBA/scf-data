#!/usr/bin/env python3
"""Fil « Actualités » de l'Accueil : squawk FinancialJuice (traduit FR) MIXÉ avec
les Flash News de Coin Académie, trié par heure, écrit dans fj_news_cache.js
(window.__FJ_NEWS__).

Choix de conception :
  - 2026-06-14 — Source #1 = FinancialJuice RSS, MÊME flux que le desk
    (build_context.sec_news_live). Flashs courts horodatés (Fed/géopo/flux/
    surprises) qui bougent le risk-on/off.
  - 2026-07-28 — Source #2 = Coin Académie (« Flash News » du site) via l'API
    WP REST (wp-json/wp/v2/posts). Déjà en FR → aucune traduction. Le pôle
    crypto/macro vient des catégories WP (TradFi/IA = macro, le reste = crypto).
    Les deux flux sont fusionnés et triés par timestamp décroissant ; chaque
    item porte son `src` → le widget affiche le badge de la bonne source.
  - Traduction (FJ seulement) = endpoint Google gratuit SANS clé. Cache de
    traduction persisté (fj_trans_cache.json) -> on ne (re)traduit QUE les
    nouveaux titres : peu d'appels, robuste au rate-limit. Fallback = titre EN
    si la trad échoue (widget jamais vide).
  - RÉSILIENCE (2026-07-28) : une source qui tombe (429 FinancialJuice observé
    quand deux runs s'enchaînent) n'efface PAS ses flashs du fil — on recharge
    l'ancien cache et on reporte ses items (< CARRY_MAX_H heures). On n'écrit
    que si au moins un flux a répondu. Chaque item porte `ts` (epoch absolu) :
    le widget recalcule l'âge à chaque render, donc « il y a 3min » reste vrai
    entre deux fetchs et un item reporté n'affiche jamais un âge périmé.
  - Écriture dans Library/Caches UNIQUEMENT (piège TCC : launchd ne peut pas
    écrire ~/Desktop, cf project_treasury_launchd_tcc_piege). Le repo lit via
    symlink fj_news_cache.js -> Library/Caches (file:// local frais), et
    snapshot_site.sh convertit le symlink en fichier réel pour le deploy public.
"""
import html as _html
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

FEED = "https://www.financialjuice.com/feed.ashx?xy=rss"
PREFIX = "FinancialJuice:"          # préfixe à retirer de chaque titre
FJ_MAX = 18                          # flashs FJ conservés (squawk = très verbeux)

CA_API = ("https://coinacademy.fr/wp-json/wp/v2/posts"
          "?per_page={n}&_fields=id,date_gmt,link,title,categories")
CA_MAX = 12                          # flash news Coin Académie conservées
# Catégories WP Coin Académie qui basculent l'item côté « macro » (icône banque).
# 1036 = TradFi, 1197 = Intelligence Artificielle. Tout le reste (bitcoin,
# altcoins, defi, exchanges, régulation crypto, NFT, mining…) reste « crypto ».
CA_MACRO_CATS = {1036, 1197}

MAX_ITEMS = 30                       # taille finale du fil mixé (widget scrollable)
CARRY_MAX_H = 36                     # âge max d'un item reporté depuis l'ancien cache

SRC_FJ = "FinancialJuice"
SRC_CA = "Coin Académie"

HOME = Path.home()
CACHE_DIR = HOME / "Library/Caches/site_crypto_finance"
TRANS_CACHE = CACHE_DIR / "fj_trans_cache.json"

CRYPTO_KW = ("bitcoin", "btc", "crypto", "ethereum", " eth ", "stablecoin",
             "coinbase", "etf", "sec ", "digital asset", "binance", "ripple",
             "xrp", "solana", "blackrock")


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read()


def _stamp(dt):
    """(HH:MM UTC, âge en minutes, epoch) depuis un datetime aware."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    hhmm = dt.astimezone(timezone.utc).strftime("%H:%M")
    age = round((datetime.now(timezone.utc) - dt).total_seconds() / 60)
    return hhmm, age, dt.timestamp()


def fetch_fj():
    """FinancialJuice RSS -> items EN horodatés (traduits plus bas)."""
    raw = _get(FEED).decode("utf-8", "replace")
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        if title.startswith(PREFIX):
            title = title[len(PREFIX):].strip()
        hhmm, age_min, ts = "", None, 0.0
        try:
            hhmm, age_min, ts = _stamp(parsedate_to_datetime(
                (it.findtext("pubDate") or "").strip()))
        except Exception:
            pass
        tl = " " + title.lower() + " "
        items.append({"src": SRC_FJ, "title_en": title[:240], "t": hhmm,
                      "age_min": age_min, "ts": ts,
                      "crypto": any(k in tl for k in CRYPTO_KW),
                      "link": (it.findtext("link") or "").strip()})
    return items[:FJ_MAX]


def fetch_ca():
    """Coin Académie (Flash News) via WP REST -> items déjà en FR."""
    raw = _get(CA_API.format(n=CA_MAX)).decode("utf-8", "replace")
    posts = json.loads(raw)
    items = []
    for p in posts:
        title = _html.unescape(re.sub(r"<[^>]+>", "",
                                      (p.get("title") or {}).get("rendered", ""))).strip()
        if not title:
            continue
        hhmm, age_min, ts = "", None, 0.0
        try:
            # date_gmt est naïf mais toujours UTC côté WordPress.
            hhmm, age_min, ts = _stamp(datetime.fromisoformat(
                p["date_gmt"]).replace(tzinfo=timezone.utc))
        except Exception:
            pass
        cats = set(p.get("categories") or [])
        items.append({"src": SRC_CA, "title": title[:240], "t": hhmm,
                      "age_min": age_min, "ts": ts,
                      "crypto": not (cats & CA_MACRO_CATS),
                      "link": (p.get("link") or "https://coinacademy.fr/").strip()})
    return items


def translate(q, cache):
    """EN -> FR via endpoint Google gratuit. Cache d'abord, fallback EN sinon."""
    if q in cache:
        return cache[q]
    try:
        u = ("https://translate.googleapis.com/translate_a/single?client=gtx"
             "&sl=en&tl=fr&dt=t&q=" + urllib.parse.quote(q))
        d = json.loads(_get(u).decode("utf-8"))
        fr = "".join(seg[0] for seg in d[0] if seg and seg[0]).strip()
        if fr:
            cache[q] = fr
            return fr
    except Exception as e:
        sys.stderr.write(f"[FJ] trad fail: {e}\n")
    return q


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def carry_over(prev_items, src):
    """Items de `src` récupérés de l'ancien cache quand le flux vient d'échouer.
    `ts` étant absolu, on recalcule age_min : jamais d'âge périmé à l'écran."""
    now = datetime.now(timezone.utc).timestamp()
    keep = []
    for it in prev_items or []:
        if it.get("src") != src or not it.get("ts"):
            continue
        age = (now - it["ts"]) / 60.0
        if age > CARRY_MAX_H * 60:
            continue
        out = dict(it)
        out["age_min"] = round(age)
        keep.append(out)
    if keep:
        sys.stderr.write(f"[FJ] {len(keep)} items {src} reportés (flux KO)\n")
    return keep


def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Deux sources indépendantes : l'échec de l'une ne doit pas vider le fil.
    prev = load_json(CACHE_DIR / "fj_news_cache.json", {}).get("items", [])
    fj, ca, carried = [], [], []
    try:
        fj = fetch_fj()
    except Exception as e:
        sys.stderr.write(f"[FJ] financialjuice fail: {e}\n")
        carried += carry_over(prev, SRC_FJ)
    try:
        ca = fetch_ca()
    except Exception as e:
        sys.stderr.write(f"[FJ] coinacademy fail: {e}\n")
        carried += carry_over(prev, SRC_CA)
    if not fj and not ca:
        sys.stderr.write("[FJ] aucune source, cache conservé\n")
        return

    trans = load_json(TRANS_CACHE, {})
    for it in fj:                       # FJ seul a besoin d'une traduction
        it["title"] = translate(it["title_en"], trans)

    # Fil mixé, le plus récent en tête (ts=0 si date illisible -> en fin de fil).
    merged = sorted(fj + ca + carried,
                    key=lambda x: x.get("ts") or 0, reverse=True)[:MAX_ITEMS]
    # ts = epoch absolu -> le widget recalcule l'âge à chaque render (age_min
    # reste écrit pour les anciens rendus qui ne connaissent pas ts).
    out = [{"t": it["t"], "ts": int(it.get("ts") or 0), "age_min": it["age_min"],
            "title": it["title"], "title_en": it.get("title_en", ""),
            "crypto": it["crypto"], "link": it["link"], "src": it["src"]}
           for it in merged]

    payload = {"updated": datetime.now().isoformat(), "n": len(out),
               "sources": {SRC_FJ: sum(1 for i in out if i["src"] == SRC_FJ),
                           SRC_CA: sum(1 for i in out if i["src"] == SRC_CA)},
               "carried": len(carried), "items": out}
    js = "window.__FJ_NEWS__=" + json.dumps(payload, ensure_ascii=False) + ";"
    jsonp = json.dumps(payload, ensure_ascii=False)
    # Library/Caches uniquement : le repo lit via symlink (piège TCC launchd).
    (CACHE_DIR / "fj_news_cache.js").write_text(js)
    (CACHE_DIR / "fj_news_cache.json").write_text(jsonp)

    # Borne la taille du cache de traduction (garde les plus récemment ajoutés)
    if len(trans) > 800:
        trans = {k: trans[k] for k in list(trans)[-500:]}
    try:
        TRANS_CACHE.write_text(json.dumps(trans, ensure_ascii=False))
    except Exception as e:
        sys.stderr.write(f"[FJ] trans cache write fail: {e}\n")

    sys.stderr.write(f"[FJ] {len(out)} flashs ({len(fj)} FJ + {len(ca)} CA live"
                     + (f", {len(carried)} reportés" if carried else "")
                     + f"), {len(trans)} entrées trad\n")


if __name__ == "__main__":
    main()
