#!/usr/bin/env python3
"""Extract Google Trends "AI bubble" series for the Bulle_IA sentiment sub-index.

Source: gtrends_cache.json (alimente par update_gtrends.py via SerpAPI Mon+Thu,
cf memory project_gtrends_serpapi.md). Le keyword "AI bubble" est deja present
dans le cache SerpAPI (group=ai, mensuel, 2004+, normalise 0..100 par Google).

Output:
  gtrends_hype_cache.json + gtrends_hype_cache.js (window.__GTRENDS_LIVE__)
  ecrits dans ~/Library/Caches/site_crypto_finance/, format {dates, values, updated, source}
  identique a l'ancienne version Wikipedia-pageviews pour que les consumers
  (Bulle_IA.Rmd ligne ~620 + sentiment scoring JS) n'aient rien a changer.

Si gtrends_cache.json manque ou ne contient pas "AI bubble", on conserve le
cache hype precedent (pas d'overwrite avec donnees vides).
"""
import json, sys
from pathlib import Path
from datetime import datetime

CACHES_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHES_DIR.mkdir(parents=True, exist_ok=True)
SOURCE_CACHE = CACHES_DIR / "gtrends_cache.json"
CACHE_FILE   = CACHES_DIR / "gtrends_hype_cache.json"
JS_FILE      = CACHES_DIR / "gtrends_hype_cache.js"

KEYWORD = "AI bubble"


def extract_from_serpapi_cache():
    if not SOURCE_CACHE.exists():
        raise RuntimeError(f"source cache absent : {SOURCE_CACHE}")
    rows = json.loads(SOURCE_CACHE.read_text())
    ai_rows = [r for r in rows if r.get("keyword") == KEYWORD]
    if not ai_rows:
        raise RuntimeError(f"keyword '{KEYWORD}' absent du cache SerpAPI")
    # COHERENCE avec l'onglet Google_Trends : on exclut le mois EN COURS (partiel/sous-
    # estime par Google), exactement comme Google_Trends.Rmd (filtre gt_cur_month). Sans
    # ca, le sentiment Bulle_IA afficherait le mois courant deflate (ex juillet=39) tandis
    # que l'onglet montre le dernier mois COMPLET (ex juin=100) -> incoherence entre les
    # deux pages qui lisent pourtant la meme serie. cf project_bulle_ia_sentiment_source.
    cur_month = datetime.now().strftime("%Y-%m")
    ai_rows = [r for r in ai_rows if r["date"][:7] != cur_month]
    if not ai_rows:
        raise RuntimeError(f"keyword '{KEYWORD}' : plus de points apres exclusion du mois courant")
    ai_rows.sort(key=lambda r: r["date"])
    dates  = [r["date"] for r in ai_rows]
    values = [int(r["hits"]) for r in ai_rows]
    sys.stderr.write(
        f"[GTrends-AIbubble] {len(values)} pts, {dates[0]} -> {dates[-1]}, "
        f"last={values[-1]}, max={max(values)}\n"
    )
    return {"dates": dates, "values": values}


def write_outputs(payload):
    with open(CACHE_FILE, "w") as f:
        json.dump(payload, f)
    with open(JS_FILE, "w") as f:
        f.write("window.__GTRENDS_LIVE__ = " + json.dumps(payload, separators=(",", ":")) + ";\n")
    sys.stderr.write(f"[GTrends-AIbubble] Wrote {CACHE_FILE.name} + {JS_FILE.name}\n")


def main():
    try:
        payload = extract_from_serpapi_cache()
    except Exception as e:
        sys.stderr.write(f"[GTrends-AIbubble] extract failed: {e}\n")
        if CACHE_FILE.exists():
            sys.stderr.write("[GTrends-AIbubble] keeping previous cache\n")
            return 0
        sys.exit(1)
    payload["updated"] = datetime.now().isoformat()
    payload["source"]  = "google_trends_serpapi:AI bubble"
    write_outputs(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
