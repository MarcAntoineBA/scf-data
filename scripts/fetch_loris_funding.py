#!/usr/bin/env python3
"""Funding perpétuels cross-venue — onglet Order Flow, partie crypto.

Source unique : `api.loris.tools/funding` (clé gratuite). Un seul appel HTTP rend
les 42 venues suivies, dont la majorité sont des DEX perp (Hyperliquid, Lighter,
Paradex, extended, edgeX, Aster, Vest, Pacifica, GRVT, Bluefin, Ethereal,
Variational, Reya…). Aucune source gratuite ne couvre les DEX aussi largement —
c'est la raison d'être de ce collecteur.

╔══════════════════════════════════════════════════════════════════════════════╗
║ PIÈGE D'UNITÉ — à lire avant toute modification                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
`funding_rates` est en POINTS DE BASE RAMENÉS À 8 HEURES. Ce n'est ni un APR, ni
le taux de l'intervalle natif de la venue.

    APR % = bp8h × 3 × 365 ÷ 100        (= bp8h × 10,95)

`funding_intervals` donne l'intervalle de règlement NATIF (1, 4 ou 8 h). Il sert à
savoir QUAND le funding tombe et à reconstituer le taux par règlement :

    taux par règlement (bp) = bp8h × intervalle ÷ 8

Il ne sert JAMAIS à annualiser. Annualiser par l'intervalle double-compte et
affiche RISEX à 200 % d'APR au lieu de 25 %.

Vérification par recoupement primaire (2026-08-09) : Hyperliquid ETH vaut `1.000`
chez Loris, et `api.hyperliquid.xyz/info metaAndAssetCtxs` renvoie 0,0000125/h,
soit exactement 1,0000 bp ramené à 8 h. Binance BTC : 0,965 chez Loris contre
0,9627 bp/8h sur `fapi.binance.com/fapi/v1/premiumIndex`. L'hypothèse « bp par
intervalle natif » est donc écartée par la mesure, pas par la lecture de la doc
(qui, elle, ne précise rien).

Le cache conserve TOUJOURS le bp8h brut à côté de l'APR : si le facteur devait
changer, l'historique resterait recalculable sans être rejeté.

╔══════════════════════════════════════════════════════════════════════════════╗
║ Ce que le palier gratuit donne — et ne donne pas                            ║
╚══════════════════════════════════════════════════════════════════════════════╝
Seul `/funding` répond. `/funding/historical`, `/funding/settlement`, `/charts`,
open interest, liquidations, profondeur, volume, options, metrics, RWA et HIP-3/4
renvoient 403 `required_tier: dev` (79 $/mois). BTC et ETH uniquement.

D'où l'historique fabriqué ici : un snapshot toutes les 5 minutes, agrégé en trois
paliers. Au bout d'un mois la granularité dépasse ce que le palier payant vend
(il plafonne à 30 jours de profondeur).

Sorties (~/Library/Caches/site_crypto_finance/) :
  orderflow_funding_cache.json + .js   window.__OF_FUNDING__ — instantané enrichi
  orderflow_funding_hist_5m.json       pas de 5 min, 7 jours glissants  (~1,0 Mo)
  orderflow_funding_hist_1h.json       médiane horaire, 90 jours        (~1,1 Mo)
  orderflow_funding_hist_1d.json       médiane + extrêmes, sans limite  (~0,2 Mo/an)

Les trois historiques restent sous le mégaoctet du seuil `GIT_SIZE_LIMIT`, donc
versionnés : on veut pouvoir relire quelle valeur a bougé et quand.

Résilience : fusion préservante par venue (une venue absente garde sa dernière
valeur, marquée `stale` avec son `asof`), jamais d'écrasement par du vide, écriture
atomique. Si la source ne répond pas du tout, le script sort en erreur SANS toucher
aux fichiers — un refus franc plutôt qu'un cache vidé.

Charge : 1 requête HTTP par passage, contre un plafond mesuré à 30/min. La cadence
5 min consomme 0,7 % du quota.
"""
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://api.loris.tools/funding"
BP8H_TO_APR = 3 * 365 / 100          # 10,95 — cf. l'encadré PIÈGE D'UNITÉ

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_JSON = CACHE_DIR / "orderflow_funding_cache.json"
CACHE_JS = CACHE_DIR / "orderflow_funding_cache.js"
HIST = {
    "5m": CACHE_DIR / "orderflow_funding_hist_5m.json",
    "1h": CACHE_DIR / "orderflow_funding_hist_1h.json",
    "1d": CACHE_DIR / "orderflow_funding_hist_1d.json",
}
RETENTION = {"5m": timedelta(days=7), "1h": timedelta(days=90), "1d": None}

# La clé vit dans un secret du dépôt, jamais dans le code : le dépôt scf-data est
# PUBLIC. Deux voies alimentées par le workflow (variable d'environnement et fichier
# du dossier personnel) — on lit les deux, sinon une panne de l'une passe pour une
# panne de la source.
KEY_FILE = Path.home() / ".loris_api_key"

# Loris ne dit pas si une venue est un carnet centralisé ou un DEX. La distinction
# porte pourtant la lecture entière du module (les DEX paient-ils plus cher que les
# CEX ?), donc elle est tenue ici, explicitement. Toute venue inconnue est classée
# DEX — la doc de Loris annonce 32 DEX sur 43 venues, c'est le pari le moins faux —
# et signalée dans `coverage.unclassified` pour être arbitrée à la main.
CEX = {
    "binance", "bybit", "okx", "bitget", "bingx", "gateio", "kucoin", "mexc",
    "huobi", "cryptocom", "phemex", "coinbase", "coinbaseus", "woofipro",
}
PREDICTION = {"kalshi"}
KNOWN_DEX = {
    "aster", "bluefin", "bullet", "decibel", "edgex", "ethereal", "extended",
    "grvt", "hibachi", "hotstuff", "hyena", "hyperliquid", "kinetiq", "lighter",
    "nado", "ondo", "pacifica", "paradex", "paragon", "phoenix", "qfex", "reya",
    "risex", "tradexyz", "variational", "vest", "zo",
}


def kind_of(venue_id):
    if venue_id in CEX:
        return "CEX"
    if venue_id in PREDICTION:
        return "PRED"
    return "DEX"


def read_key():
    key = (os.environ.get("LORIS_API_KEY") or "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    return key


def fetch(key, tries=3):
    """Un seul appel, trois tentatives. Le 429 est respecté à la lettre : le
    serveur renvoie `retry_after`, et forcer le passage ferait bannir la clé."""
    last = None
    for n in range(1, tries + 1):
        try:
            r = requests.get(API, headers={"X-Api-Key": key}, timeout=25)
            if r.status_code == 429:
                wait = int(r.headers.get("retry-after") or 3)
                print(f"  429 — pause {wait}s (tentative {n}/{tries})")
                time.sleep(wait + 1)
                last = "429"
                continue
            r.raise_for_status()
            data = r.json()
            reste = r.headers.get("x-ratelimit-remaining")
            if reste is not None:
                print(f"  quota restant sur la minute : {reste}")
            return data
        except Exception as e:                                    # noqa: BLE001
            last = e
            print(f"  échec {n}/{tries} : {e}")
            time.sleep(2 * n)
    raise RuntimeError(f"source injoignable après {tries} tentatives : {last}")


def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:                                             # noqa: BLE001
        return default


def write_atomic(path, body):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body)
    tmp.replace(path)


# ─────────────────────────────────────────────────────────────────────────────
# Historique : format indexé
#
# Une ligne = [timestamp, [bp8h BTC par venue], [bp8h ETH par venue]], les tableaux
# étant alignés sur `venues`, écrit UNE fois en tête. Stocker le nom de la venue à
# chaque point multiplierait le poids par huit pour zéro information.
#
# Une venue qui apparaît en cours de route est AJOUTÉE en fin d'index : les lignes
# anciennes restent plus courtes et le lecteur comble par `null`. Réindexer
# l'historique à chaque nouvelle venue le réécrirait en entier — et une erreur de
# décalage y serait invisible.
# ─────────────────────────────────────────────────────────────────────────────
def hist_append(path, retention, ts, per_venue, symbols=("BTC", "ETH")):
    h = load(path, {"venues": [], "rows": []})
    venues = h["venues"]
    index = {v: i for i, v in enumerate(venues)}
    for vid in per_venue:
        if vid not in index:
            index[vid] = len(venues)
            venues.append(vid)

    row = [ts]
    for sym in symbols:
        arr = [None] * len(venues)
        for vid, vals in per_venue.items():
            arr[index[vid]] = vals.get(sym)
        row.append(arr)

    rows = [r for r in h["rows"] if r[0] != ts]     # rejouer un passage n'ajoute rien
    rows.append(row)
    rows.sort(key=lambda r: r[0])

    if retention is not None:
        limite = (datetime.now(timezone.utc) - retention).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [r for r in rows if r[0] >= limite]

    h["venues"], h["rows"] = venues, rows
    h["unit"] = "bp8h"
    h["updated"] = ts
    write_atomic(path, json.dumps(h, separators=(",", ":")))
    return len(rows)


def rollup(src_path, dst_path, retention, tronque):
    """Agrège le palier fin dans le palier supérieur : médiane par seau, sur les
    seaux CLOS uniquement. Agréger le seau courant publierait une médiane calculée
    sur trois points à 00:05 puis douze à 00:55 — la même heure changerait de
    valeur sous les yeux du lecteur."""
    src = load(src_path, {"venues": [], "rows": []})
    if not src["rows"]:
        return 0
    venues = src["venues"]
    courant = tronque(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    seaux = {}
    for r in src["rows"]:
        b = tronque(r[0])
        if b == courant:
            continue
        seaux.setdefault(b, []).append(r)

    dst = load(dst_path, {"venues": [], "rows": []})
    dv = dst["venues"]
    dindex = {v: i for i, v in enumerate(dv)}
    for v in venues:
        if v not in dindex:
            dindex[v] = len(dv)
            dv.append(v)

    existants = {r[0] for r in dst["rows"]}
    ajoutes = 0
    for b, lignes in sorted(seaux.items()):
        if b in existants:
            continue
        row = [b]
        for si in (1, 2):
            arr = [None] * len(dv)
            for vi, vid in enumerate(venues):
                vals = [l[si][vi] for l in lignes
                        if len(l[si]) > vi and l[si][vi] is not None]
                if vals:
                    arr[dindex[vid]] = round(statistics.median(vals), 4)
            row.append(arr)
        dst["rows"].append(row)
        ajoutes += 1

    dst["rows"].sort(key=lambda r: r[0])
    if retention is not None:
        limite = (datetime.now(timezone.utc) - retention).strftime("%Y-%m-%dT%H:%M:%SZ")
        dst["rows"] = [r for r in dst["rows"] if r[0] >= limite]
    dst["venues"], dst["unit"], dst["updated"] = dv, "bp8h", courant
    write_atomic(dst_path, json.dumps(dst, separators=(",", ":")))
    return ajoutes


def stats_for(venues, sym):
    """Statistiques transversales. La MÉDIANE, pas la moyenne : une venue naissante
    à +200 % d'APR déplacerait la moyenne de plusieurs points et ferait mentir le
    « régime de funding » que ce chiffre est censé résumer."""
    vals = [(v["id"], v["apr"][sym]) for v in venues if v["apr"].get(sym) is not None]
    if not vals:
        return None
    xs = sorted(x for _, x in vals)
    n = len(xs)
    med = statistics.median(xs)
    sd = statistics.pstdev(xs) if n > 1 else 0.0
    haut = max(vals, key=lambda t: t[1])
    bas = min(vals, key=lambda t: t[1])

    def med_of(kind):
        s = [v["apr"][sym] for v in venues
             if v["kind"] == kind and v["apr"].get(sym) is not None]
        return round(statistics.median(s), 3) if s else None

    return {
        "n": n,
        "median": round(med, 3),
        "mean": round(sum(xs) / n, 3),
        "sd": round(sd, 3),
        "min": round(xs[0], 3), "max": round(xs[-1], 3),
        "spread": round(xs[-1] - xs[0], 3),
        "negatifs": len([x for x in xs if x < 0]),
        "median_cex": med_of("CEX"), "median_dex": med_of("DEX"),
        "haut": {"id": haut[0], "apr": round(haut[1], 3)},
        "bas": {"id": bas[0], "apr": round(bas[1], 3)},
        # Écart brut entre les deux extrêmes : ce qu'un long sur la venue la moins
        # chère financé par un short sur la plus chère capterait AVANT frais,
        # exécution et risque de contrepartie. Ce n'est pas un rendement net, et le
        # libellé de la page doit le dire.
        "arbitrage_brut": round(haut[1] - bas[1], 3),
    }


def main():
    key = read_key()
    if not key:
        sys.exit("LORIS_API_KEY absente (ni environnement, ni ~/.loris_api_key)")

    print("Loris /funding …")
    d = fetch(key)
    ts = d.get("timestamp") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fr, ivs = d.get("funding_rates", {}), d.get("funding_intervals", {})
    symbols = d.get("symbols", ["BTC", "ETH"])
    noms = {e["name"]: e.get("display", e["name"].upper())
            for e in d.get("exchanges", {}).get("exchange_names", [])}
    if not fr:
        sys.exit("réponse sans funding_rates — fichiers laissés intacts")

    # Fusion préservante : on part de l'état publié, on écrase venue par venue.
    ancien = load(CACHE_JSON, {})
    precedent = {v["id"]: v for v in ancien.get("venues", [])}

    venues, vivants, per_venue = [], 0, {}
    for vid, vals in fr.items():
        bp = {s: vals.get(s) for s in symbols}
        iv = ivs.get(vid, {})
        interval = next((iv[s] for s in symbols if iv.get(s)), None)
        if any(v is not None for v in bp.values()):
            vivants += 1
            per_venue[vid] = bp
            venues.append({
                "id": vid,
                "name": noms.get(vid, vid.upper()),
                "kind": kind_of(vid),
                "interval": interval,
                "bp8h": {s: bp[s] for s in symbols},
                "apr": {s: (None if bp[s] is None else round(bp[s] * BP8H_TO_APR, 3))
                        for s in symbols},
                "stale": False,
                "asof": ts,
            })
        elif vid in precedent:
            # Venue muette ce coup-ci : on garde sa dernière valeur connue, marquée.
            # L'effacer ferait clignoter la page à chaque hoquet de la source.
            garde = dict(precedent[vid])
            garde["stale"] = True
            venues.append(garde)

    if vivants == 0:
        sys.exit("aucune venue ne renvoie de funding — fichiers laissés intacts")

    venues.sort(key=lambda v: -(v["apr"].get("BTC") if v["apr"].get("BTC") is not None
                                else -9e9))

    inconnues = sorted(set(fr) - CEX - PREDICTION - KNOWN_DEX)
    payload = {
        "updated": ts,
        "updated_ts": int(time.time()),
        "source": "api.loris.tools/funding",
        "unite": {
            "brut": "points de base ramenés à 8 h",
            "facteur_apr": round(BP8H_TO_APR, 4),
            "formule_apr": "APR% = bp8h × 3 × 365 ÷ 100",
            "formule_par_reglement": "bp par règlement = bp8h × intervalle ÷ 8",
        },
        "symbols": symbols,
        "venues": venues,
        "stats": {s: stats_for(venues, s) for s in symbols},
        "coverage": {
            "annoncees": len(fr),
            "vivantes": vivants,
            "conservees_stale": len(venues) - vivants,
            "muettes": sorted(v for v, x in fr.items()
                              if all(x.get(s) is None for s in symbols)),
            "unclassified": inconnues,
        },
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    write_atomic(CACHE_JSON, body)
    write_atomic(CACHE_JS, "window.__OF_FUNDING__=" + body + ";\n")

    n5 = hist_append(HIST["5m"], RETENTION["5m"], ts, per_venue, symbols)
    n1h = rollup(HIST["5m"], HIST["1h"], RETENTION["1h"],
                 lambda t: t[:13] + ":00:00Z")
    n1d = rollup(HIST["1h"], HIST["1d"], RETENTION["1d"],
                 lambda t: t[:10] + "T00:00:00Z")

    st = payload["stats"].get("BTC") or {}
    print(f"  {vivants}/{len(fr)} venues vivantes"
          f" · médiane BTC {st.get('median')} %"
          f" · écart {st.get('arbitrage_brut')} pts"
          f" · {st.get('negatifs')} en négatif")
    print(f"  historique : {n5} points à 5 min · +{n1h} heures · +{n1d} jours")
    if inconnues:
        print(f"  À CLASSER (traitées en DEX) : {', '.join(inconnues)}")


if __name__ == "__main__":
    main()
