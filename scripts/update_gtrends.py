#!/usr/bin/env python3
"""Refresh des caches Google Trends via SerpAPI.

Remplace update_gtrends.R qui depend de gtrendsR (lib R) et tombe sur 429 Google.
SerpAPI passe par leur pool de proxies residentiels => pas de rate-limit cote IP user.

Quota : 250 searches/mois sur le free plan. 22 batches par region par refresh complet.

BUDGET (revu 2026-07-26) — c'est le TTL, pas le planning, qui borne la depense :
  World  : TTL 144h (6j) -> au plus ~5 fetchs/mois           = ~110 recherches
  US/FR  : TTL 336h (14j) -> ~2,2 fetchs/mois chacun         = ~96 recherches
  TOTAL  : ~206/mois pour 250 disponibles.
Le job launchd peut donc etre planifie TOUS LES JOURS sans surcout : le premier
creneau ou le Mac est reveille paie le fetch, les suivants tombent sur le TTL et
coutent 0. C'est ce qui rend la mise a jour robuste au sommeil du Mac (un job
calendaire rate n'est JAMAIS rejoue -> l'ancien creneau unique du lundi 09:00 a
laisse le cache fige 12 jours en juillet 2026).
Les donnees sont MENSUELLES : un fetch hebdo suffit ; les prix live viennent du re-knit.

Sortie (single source) :
  gtrends_cache.json       (52 termes World, mensuel, 2004+ ; 51 termes FR)
  gtrends_cache_meta.json  (metadata du fetch)
gtrends_cache_long.json est conserve pour debug mais non execute par defaut
(SHORT couvre maintenant 2004+ aussi).

Usage:
  python update_gtrends.py            # respect cache_max_h=6h
  python update_gtrends.py --force    # force le re-fetch
  python update_gtrends.py --long     # debug-only: re-fetch le cache long
"""
import argparse
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

KEY_FILE = Path.home() / ".serpapi_key"
# Cache dir TCC-safe (launchd ne peut pas ecrire ~/Desktop sans Full Disk Access).
# Cf. memory project_site_crypto_launchd_pattern.md — symlinks depuis Desktop.
CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOCK_FILE = CACHE_DIR / ".update_gtrends.lock"
CACHE_SHORT = CACHE_DIR / "gtrends_cache.json"
META_SHORT = CACHE_DIR / "gtrends_cache_meta.json"
CACHE_LONG = CACHE_DIR / "gtrends_cache_long.json"
META_LONG = CACHE_DIR / "gtrends_cache_long_meta.json"
CACHE_MAX_H = 6
# TTL du cache WORLD = plafond de dépense, pas un simple confort (2026-07-26).
# Avant : 6 h, donc CHAQUE lancement re-fetchait les 22 batches (22 recherches
# SerpAPI). Le job ne pouvait alors être planifié qu'une seule fois par semaine
# pour tenir dans les 250/mois — et un créneau calendaire raté (Mac endormi le
# lundi 09:00, un job calendaire ne se rejoue JAMAIS) coûtait une semaine entière
# de données : constaté le 26/07 avec un cache figé depuis le 14/07 (12 jours).
# Maintenant : 6 jours. Le job peut être planifié TOUS LES JOURS — le premier
# créneau où le Mac est réveillé fait le fetch, les six suivants tombent sur le
# TTL et ne coûtent RIEN. Une seule dépense hebdomadaire, sept chances de la faire.
# Les données Google Trends sont MENSUELLES : re-fetcher plus souvent n'apporte rien.
WORLD_TTL_H = 144

# Categories Google Trends : 0 = All, 7 = Finance
BATCHES_SHORT = [
    {"name": "crypto-majors",  "kws": ["Bitcoin", "Ethereum", "Solana"],                                              "cat": 0, "group": "crypto"},
    {"name": "crypto-bullrun", "kws": ["Bull run crypto", "Bittensor"],                                               "cat": 0, "group": "crypto"},
    {"name": "crypto-defi",    "kws": ["Aave crypto", "Chainlink crypto"],                                            "cat": 0, "group": "crypto"},
    {"name": "crypto-hype",    "kws": ["Hyperliquid"],                                                                "cat": 0, "group": "crypto"},
    {"name": "crypto-stable",  "kws": ["stablecoin"],                                                                 "cat": 0, "group": "crypto"},
    {"name": "crypto-narratives", "kws": ["Decentralized AI", "Real world asset"],                                    "cat": 0, "group": "crypto"},
    {"name": "macro-crash",    "kws": ["stock market crash", "recession", "market crash", "financial crisis", "bear market"], "cat": 7, "group": "macro"},
    {"name": "macro-refuges",  "kws": ["buy gold", "treasury bonds", "safe haven", "hedge fund"],                     "cat": 7, "group": "macro"},
    {"name": "macro-credit",   "kws": ["layoffs", "bank run", "margin call", "credit crunch", "debt crisis"],         "cat": 7, "group": "macro"},
    {"name": "macro-panique",  "kws": ["sell stocks", "market bottom", "market bubble", "black monday", "panic selling"], "cat": 7, "group": "macro"},
    {"name": "macro-vix",      "kws": ["VIX"],                                                                        "cat": 7, "group": "macro"},
    {"name": "macro-fomo",     "kws": ["buy US stocks", "buy chinese stocks"],                                        "cat": 7, "group": "macro"},
    # Sentiment Index — bullish terms (ajoutes 2026-05-20 pour le Sentiment Marche 0-100)
    # "Bull run" isole car ecrase a 0 dans un batch avec "buy stocks" (qui pic a 100).
    {"name": "sent-bull-run",  "kws": ["Bull run"],                                                                    "cat": 7, "group": "macro"},
    {"name": "sent-bull-fin",  "kws": ["buy stocks", "bull market", "how to invest", "invest now"],                   "cat": 7, "group": "macro"},
    {"name": "sent-bull-life", "kws": ["become millionaire", "financial freedom"],                                     "cat": 0, "group": "macro"},
    # Sentiment Index — capitulation au passe (signal LEADING de bottom)
    {"name": "sent-capitul",   "kws": ["stock market crashed"],                                                        "cat": 7, "group": "macro"},
    {"name": "ai-bubble",      "kws": ["AI bubble"],                                                                  "cat": 0, "group": "ai"},
    # Crise sociale : signaux profonds de stress societal / collapse / despair (groupes par echelle de volume)
    {"name": "crypto-despair", "kws": ["bitcoin is dead", "ethereum is dead", "self custody"],                         "cat": 0, "group": "crypto"},
    {"name": "crisis-economic",     "kws": ["can't afford food", "can't afford rent", "AI unemployment"],              "cat": 0, "group": "crise"},
    {"name": "crisis-civil-war",    "kws": ["civil war"],                                                              "cat": 0, "group": "crise"},
    {"name": "crisis-democracy",    "kws": ["democracy is dead"],                                                      "cat": 0, "group": "crise"},
    {"name": "crisis-existential",  "kws": ["meaning of life", "why live"],                                            "cat": 0, "group": "crise"},
]

BATCHES_LONG = [
    {"name": "long-majors",   "kws": ["Bitcoin", "Ethereum", "Solana"],                "cat": 0, "group": "crypto"},
    {"name": "long-bullrun",  "kws": ["Bull run crypto", "Bittensor"],                 "cat": 0, "group": "crypto"},
    {"name": "long-defi",     "kws": ["Aave crypto", "Chainlink crypto", "stablecoin"],"cat": 0, "group": "crypto"},
    {"name": "long-hype",     "kws": ["Hyperliquid"],                                  "cat": 0, "group": "crypto"},
]

# FENETRE = 2007-01-01 -> aujourd'hui (~19,5 ans). CRITIQUE : Google Trends renvoie
# du MENSUEL pour une fenetre <= ~22 ans, mais bascule en ANNUEL (1 pt/an) au-dela.
# L'ancien start fige "2004-01-01" a fait grossir la fenetre au-dela de ~22,5 ans debut
# juillet 2026 -> Google est passe en annuel -> cache max=2026-01-01 -> onglet + sentiment
# Bulle_IA bloques sur "Janvier 2026". 2007 preserve tout l'historique utile (le pic GFC
# oct-2008 de "financial crisis"/"recession"/"bear market" ; 2004-2006 est vide pour ces
# termes : max 7-21, jamais un pic all-time) tout en restant MENSUEL avec ~2,5 ans de marge
# sous le seuil. La garde _is_annual() (run_batches) refuse d'ecraser un cache mensuel si
# Google resserre encore le seuil. cf memory project_gtrends_serpapi (regression 2026-07).
TIME_RANGE_SHORT = "2007-01-01 {today}"
TIME_RANGE_LONG = "2007-01-01 {today}"

# ── Filtres par pays (refondu 2026-05-13 soir) ─────────────────────────────
# US a tous les 52 termes, FR 51 (memes batches que WORLD).
# Pour FR, on traduit chaque keyword via EN_TO_FR ; les termes universels
# (Bitcoin, hedge fund, VIX, self custody...) gardent leur forme anglaise.
# CN et HK supprimes (signal pas assez fiable, cf user 2026-05-13).
# Refresh bi-mensuel via TTL 336h (=14j) sur regions, launchd Mon+Thu touche
# les regions une fois sur deux le lundi.

# Mapping EN -> FR pour la region France. Cle = keyword World (EN), valeur = keyword FR.
EN_TO_FR = {
    # Crypto : marques globales gardent EN
    "Bitcoin": "Bitcoin", "Ethereum": "Ethereum", "Solana": "Solana",
    "Bull run crypto": "bull run crypto", "Bittensor": "Bittensor",
    "Aave crypto": "Aave", "Chainlink crypto": "Chainlink",
    "Hyperliquid": "Hyperliquid", "stablecoin": "stablecoin",
    "Decentralized AI": "IA décentralisée", "Real world asset": "RWA",
    # Crypto despair
    "bitcoin is dead": "bitcoin mort", "ethereum is dead": "ethereum mort",
    "self custody": "self custody",
    # Macro fear : traduits sauf anglicismes universels (hedge fund, VIX)
    "stock market crash": "krach boursier",
    "recession": "récession",
    "market crash": "effondrement marché",
    "financial crisis": "crise financière",
    "bear market": "marché baissier",
    "buy gold": "acheter or",
    "treasury bonds": "obligations d'État",
    "safe haven": "valeur refuge",
    "hedge fund": "hedge fund",
    "layoffs": "licenciements",
    "bank run": "panique bancaire",
    "margin call": "appel de marge",
    "credit crunch": "credit crunch",
    "debt crisis": "crise de la dette",
    "sell stocks": "vendre actions",
    "market bottom": "creux marché",
    "market bubble": "bulle financière",
    "black monday": "lundi noir",
    "panic selling": "vente panique",
    "VIX": "VIX",
    "buy US stocks": "acheter actions américaines",
    "buy chinese stocks": "acheter actions chinoises",
    # Sentiment Index — bullish
    "Bull run": "bull run",
    "buy stocks": "acheter actions",
    "bull market": "marché haussier",
    "how to invest": "comment investir",
    "invest now": "investir maintenant",
    "become millionaire": "devenir millionnaire",
    "financial freedom": "liberté financière",
    # Sentiment Index — capitulation. NB: distinct de "stock market crash" -> "krach boursier"
    # (sinon collision FR : 2 batches ecrivent la meme clef -> lignes dupliquees + FR=51 termes).
    "stock market crashed": "effondrement boursier",
    # AI
    "AI bubble": "bulle IA",
    # Crise sociale
    "AI unemployment": "chômage IA",
    "can't afford food": "vie chère",
    "can't afford rent": "loyer trop cher",
    "civil war": "guerre civile",
    "democracy is dead": "fin de la démocratie",
    "meaning of life": "sens de la vie",
    "why live": "pourquoi vivre",
}

def translate_batches(batches, lang_map):
    """Renvoie une copie de batches avec chaque keyword traduit selon lang_map."""
    return [
        {**b, "kws": [lang_map.get(k, k) for k in b["kws"]]}
        for b in batches
    ]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_api_key():
    if not KEY_FILE.exists():
        log(f"ERREUR : cle SerpAPI manquante a {KEY_FILE}")
        sys.exit(1)
    return KEY_FILE.read_text().strip()


def acquire_lock():
    """Singleton lock via fcntl. Si deja locke -> exit 0 (pas 1, pour ne pas
    marquer le job launchd en echec)."""
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd  # garder ouvert pour le scope de la run
    except BlockingIOError:
        log("Une autre instance tourne deja (lock detenu). Exit 0.")
        sys.exit(0)


def cache_is_fresh(meta_path, max_hours):
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
        ts = datetime.fromisoformat(meta["updated_at"].replace("Z", ""))
        age_h = (datetime.now(timezone.utc).replace(tzinfo=None) - ts).total_seconds() / 3600
        return age_h < max_hours
    except Exception:
        return False


def load_prev_cache(cache_path):
    if not cache_path.exists():
        return []
    try:
        return json.loads(cache_path.read_text())
    except Exception:
        return []


def fetch_serpapi(keywords, timeframe, cat, api_key, geo=""):
    """Retourne la liste timeline_data de SerpAPI, ou None en cas d'erreur.

    geo: code pays ISO ("" = worldwide, "US", "FR", "CN", "HK", ...)
    """
    params = {
        "engine": "google_trends",
        "q": ",".join(keywords),
        "date": timeframe,
        "data_type": "TIMESERIES",
        "tz": "0",
        "api_key": api_key,
    }
    if cat:
        params["cat"] = str(cat)
    if geo:
        params["geo"] = geo
    url = "https://serpapi.com/search.json?" + urlencode(params)
    # Retry : le reseau peut etre instable (timeouts SerpAPI intermittents). 3 tentatives
    # avec backoff court ameliore la probabilite d'un fetch COMPLET (tous les lots OK le
    # meme run -> mois courant coherent sur tous les termes, pas de bord en escalier).
    data = None
    for attempt in range(3):
        try:
            with urlopen(url, timeout=45) as resp:
                data = json.loads(resp.read())
            break
        except Exception as e:
            # TimeoutError, HTTPError, URLError, json.JSONDecodeError, socket.timeout, etc.
            log(f"    HTTP/parse error ({type(e).__name__}, tentative {attempt+1}/3): {e}")
            if attempt < 2:
                time.sleep(4)
    if data is None:
        return None
    md = data.get("search_metadata", {})
    if md.get("status") != "Success":
        log(f"    SerpAPI status={md.get('status')}, error={data.get('error')}")
        return None
    iot = data.get("interest_over_time", {}).get("timeline_data")
    if not iot:
        log("    pas de timeline_data dans la reponse")
        return None
    return iot


def timeline_to_rows(timeline, group):
    """Convertit la timeline SerpAPI -> liste de dicts {date,hits,keyword,group,partial?}.

    On CONSERVE le mois courant incomplet (point marque partial_data=true) : les valeurs
    Google Trends mensuelles sont des MOYENNES (pas des cumuls), donc le mois en cours est
    comparable aux mois complets — c'est la lecture "live" du mois courant, ce que l'on veut
    afficher (on est en juin -> on montre juin). On le tague `partial: true` pour pouvoir le
    labelliser "en cours" cote UI. NB : l'incoherence de fin de serie observee avant venait
    des lots qui ECHOUAIENT (reseau) et restaient au mois precedent via prev_cache, pas du
    point partiel lui-meme ; un fetch complet rend tous les termes coherents au mois courant."""
    rows = []
    for point in timeline:
        ts = int(point.get("timestamp", 0))
        if not ts:
            continue
        is_partial = bool(point.get("partial_data"))
        date_iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        for v in point.get("values", []):
            kw = v.get("query")
            hits = v.get("extracted_value")
            if kw is None or hits is None:
                continue
            row = {"date": date_iso, "hits": int(hits), "keyword": kw, "group": group}
            if is_partial:
                row["partial"] = True
            rows.append(row)
    return rows


def merge_with_prev(new_rows, prev_rows, fetched_keywords):
    """Pour les keywords NON fetched ce run, on garde leurs anciennes rows."""
    kept = [r for r in prev_rows if r["keyword"] not in fetched_keywords]
    return kept + new_rows


def _is_annual(rows):
    """True si la serie est en granularite ANNUELLE (1 pt/an) plutot que mensuelle.

    Google Trends bascule mensuel -> annuel au-dela de ~22 ans de fenetre. On detecte
    via l'ecart median entre dates distinctes : ~30j = mensuel, ~365j = annuel. Sert de
    garde anti-regression (voir run_batches) : on refuse d'ecraser un cache mensuel par
    de l'annuel si Google resserre le seuil un jour."""
    ds = sorted({r["date"] for r in rows})
    if len(ds) < 3:
        return False
    gaps = sorted(
        (datetime.fromisoformat(ds[i]) - datetime.fromisoformat(ds[i - 1])).days
        for i in range(1, len(ds))
    )
    return gaps[len(gaps) // 2] > 200  # ecart median > ~6 mois => annuel


def run_batches(batches, time_range_template, api_key, today_str, label, cache_path, meta_path, geo=""):
    timeframe = time_range_template.format(today=today_str)
    geo_label = f" geo={geo}" if geo else ""
    log(f"=== {label}{geo_label} — timeframe={timeframe} — {len(batches)} batches ===")

    prev_rows = load_prev_cache(cache_path)
    all_rows = []
    fetched_kws = set()
    n_ok = 0
    n_err = 0

    for i, b in enumerate(batches, 1):
        log(f"  Batch {i}/{len(batches)}: {b['name']:18s} [{b['group']} | cat={b['cat']}] {', '.join(b['kws'])}")
        timeline = fetch_serpapi(b["kws"], timeframe, b["cat"], api_key, geo=geo)
        if timeline is None:
            log("    => echec, on conserve les anciennes valeurs pour ces kws (prev_cache)")
            n_err += 1
        else:
            rows = timeline_to_rows(timeline, b["group"])
            all_rows.extend(rows)
            fetched_kws.update(b["kws"])
            n_ok += 1
        time.sleep(1.0)  # politesse + cache SerpAPI

    if n_ok == 0:
        log(f"!!! {label}: aucun batch OK, cache non modifie")
        return False

    # Garde anti-regression de granularite : si le fetch revient en ANNUEL (fenetre au-dela
    # du seuil Google ~22 ans) alors que le cache precedent etait MENSUEL, on REFUSE d'ecraser
    # -> on preserve la fraicheur mensuelle et on alerte (il faut reculer le start de
    # TIME_RANGE). Sinon la page retombe sur "dernier mois complet = Janvier". cf 2026-07.
    if _is_annual(all_rows) and prev_rows and not _is_annual(prev_rows):
        log(f"!!! {label}: FETCH ANNUEL detecte (fenetre > seuil Google) — cache MENSUEL "
            f"preserve, PAS d'ecrasement. Reculer le start de TIME_RANGE_SHORT.")
        return False

    merged = merge_with_prev(all_rows, prev_rows, fetched_kws)
    # Dedup defensif par (keyword, date), derniere valeur gagne. Protege contre une
    # collision de traduction EN->FR (2 batches ecrivant la meme clef) qui doublerait
    # les lignes et fausserait les lookbacks % de la heatmap.
    dedup = {}
    for r in merged:
        dedup[(r["keyword"], r["date"])] = r
    merged = list(dedup.values())

    # Bord de fin uniforme : on coupe au dernier mois COMMUN a tous les termes (= min des
    # dates max par terme). Si le fetch est complet, tous les termes atteignent le mois
    # courant -> on montre le mois courant (juin). Si certains lots ont echoue (reseau) et
    # sont restes au mois precedent via prev_cache, le min retombe au mois precedent -> tous
    # les termes affichent le meme dernier mois (mai), sans bord en escalier ni "Actuel"
    # non comparable. Degrade proprement au lieu de melanger juin/mai.
    last_by_kw = {}
    for r in merged:
        k = r["keyword"]
        if k not in last_by_kw or r["date"] > last_by_kw[k]:
            last_by_kw[k] = r["date"]
    if last_by_kw:
        common_max = min(last_by_kw.values())
        merged = [r for r in merged if r["date"] <= common_max]
    # Ecriture atomique
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False))
    tmp.replace(cache_path)

    kws = sorted({r["keyword"] for r in merged})
    groups = sorted({r["group"] for r in merged})
    meta = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "n_keywords": len(kws),
        "n_batches_ok": n_ok,
        "n_batches_err": n_err,
        "groups": groups,
        "source": "serpapi",
    }
    if merged:
        dates = sorted({r["date"] for r in merged})
        meta["date_min"] = dates[0]
        meta["date_max"] = dates[-1]
    meta_tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    meta_tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    meta_tmp.replace(meta_path)
    log(f"=== {label} OK: {len(kws)} kws, {n_ok} batches ok, {n_err} erreurs ===")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore le TTL")
    ap.add_argument("--long", action="store_true", help="debug: re-fetch le cache long (rare)")
    ap.add_argument("--region", choices=["all", "world", "US", "FR"], default="world",
                    help="all=World+US+FR ; world=Worldwide only (default) ; US ou FR")
    args = ap.parse_args()

    do_long = args.long
    if args.region == "all":
        regions_to_run = ["world", "US", "FR"]
    elif args.region in ("US", "FR"):
        regions_to_run = [args.region]
    else:
        regions_to_run = ["world"]

    api_key = load_api_key()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    fd = acquire_lock()
    try:
        for region in regions_to_run:
            if region == "world":
                if not args.force and cache_is_fresh(META_SHORT, WORLD_TTL_H):
                    log(f"Cache WORLD frais (<{WORLD_TTL_H}h = {WORLD_TTL_H // 24}j), "
                        f"skip (0 recherche SerpAPI). --force pour forcer.")
                else:
                    run_batches(BATCHES_SHORT, TIME_RANGE_SHORT, api_key, today_str,
                                "WORLD", CACHE_SHORT, META_SHORT, geo="")
            else:
                cache_p = CACHE_DIR / f"gtrends_cache_{region}.json"
                meta_p = CACHE_DIR / f"gtrends_cache_{region}_meta.json"
                # Region caches : 52 termes (US) / 51 (FR) ; TTL 14j => refresh ~bi-mensuel via Mon+Thu launchd.
                # Quota : 22 batches x 2 regions x ~2 fois/mois ~= 96/mo + World ~190/mo ~= 286/mo (cf docstring : >250).
                if not args.force and cache_is_fresh(meta_p, 24 * 14):
                    log(f"Cache {region} frais (<14j), skip. --force pour forcer.")
                else:
                    # FR utilise les 22 batches World avec keywords traduits, US utilise EN tels quels
                    batches_for_region = translate_batches(BATCHES_SHORT, EN_TO_FR) if region == "FR" else BATCHES_SHORT
                    run_batches(batches_for_region, TIME_RANGE_SHORT, api_key, today_str,
                                region, cache_p, meta_p, geo=region)
        if do_long:
            if not args.force and cache_is_fresh(META_LONG, CACHE_MAX_H * 24):
                log("Cache LONG frais (<6j), skip. --force pour forcer.")
            else:
                run_batches(BATCHES_LONG, TIME_RANGE_LONG, api_key, today_str,
                            "LONG", CACHE_LONG, META_LONG)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass
    log("Done.")


if __name__ == "__main__":
    main()
