#!/usr/bin/env python3
"""Positionnement COT — onglet Structure de marché, partie TradFi.

La CFTC publie chaque vendredi la répartition des positions ouvertes par TYPE
d'intervenant. C'est le seul panneau réellement DIRECTIONNEL de la moitié
actions : le gamma dit comment les teneurs de marché vont réagir, le COT dit qui
est positionné dans quel sens.

Jeu retenu : « Traders in Financial Futures » (yw9f-hn96), qui découpe en trois
catégories lisibles là où le jeu historique se contente de commercial /
non-commercial :

  dealer          — teneurs de marché, banques. Contrepartie, pas opinion : leur
                    net est le miroir de la demande des autres, pas un pari.
  asset manager   — fonds de pension, assureurs, gérants indiciels. Argent lent,
                    directionnel, qui tourne peu.
  leveraged funds — hedge funds, CTA. Argent rapide, celui qui fait les extrêmes.

╔══════════════════════════════════════════════════════════════════════════════╗
║ DEUX PIÈGES DE LECTURE, PORTÉS DANS LE CACHE                                 ║
╚══════════════════════════════════════════════════════════════════════════════╝
1. LATENCE STRUCTURELLE. Le rapport paraît le vendredi mais photographie le
   MARDI précédent. Trois jours de marché séparent toujours la donnée de sa
   publication — et davantage quand un jour férié décale la parution. Le champ
   `arrete` porte la date d'arrêté, `publie` celle de parution ; c'est `arrete`
   que la page doit afficher.

2. UN NET BRUT NE SE COMPARE À RIEN. « Les leveraged funds sont short de 335 894
   contrats » ne dit rien sans échelle : ni contre leur propre histoire, ni entre
   marchés de tailles différentes. On publie donc le net EN PART DE L'OPEN
   INTEREST, et son z-score sur trois ans glissants. Un z de −2 dit « rarement vu
   aussi short » ; le chiffre brut, lui, ne dit rien.

Les spreads sont EXCLUS du net : une position de spread n'est pas une opinion
directionnelle, l'inclure gonflerait les deux côtés sans rien signifier.

Sorties (~/Library/Caches/site_crypto_finance/) :
  tradfi_cot_cache.json + .js     window.__TRADFI_COT__

Cadence hebdomadaire : la source ne publie qu'une fois par semaine, la relire
plus souvent ne ferait que republier le même contenu.
"""
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "tradfi_cot_cache.json"
OUT_JS = CACHE_DIR / "tradfi_cot_cache.js"

API = "https://publicreporting.cftc.gov/resource/yw9f-hn96.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; SiteCryptoFinance/1.0)"}

# Codes de contrat, pas noms de marché : la CFTC RENOMME ses marchés au fil du
# temps (« E-MINI S&P 500 » puis « E-MINI S&P 500 STOCK INDEX » sous le même code
# 13874A). Filtrer sur le nom perdrait l'historique au premier renommage.
MARCHES = [
    ("13874A", "S&P 500",      "actions"),
    ("20974+", "Nasdaq 100",   "actions"),
    ("12460+", "Dow Jones",    "actions"),
    ("043602", "UST 10 ans",   "taux"),
    ("020601", "UST 30 ans",   "taux"),
    ("042601", "UST 2 ans",    "taux"),
    ("1170E1", "VIX",          "volatilité"),
    ("098662", "Dollar index", "devises"),
]

CATEGORIES = [
    ("dealer", "dealer_positions_long_all", "dealer_positions_short_all"),
    ("asset_manager", "asset_mgr_positions_long", "asset_mgr_positions_short"),
    ("leveraged", "lev_money_positions_long", "lev_money_positions_short"),
]

SEMAINES = 160          # ~3 ans de rapports hebdomadaires

# ╔════════════════════════════════════════════════════════════════════════════╗
# ║ ÂGE MAXIMAL TOLÉRÉ DE LA DATE D'ARRÊTÉ                                     ║
# ╚════════════════════════════════════════════════════════════════════════════╝
# Le rapport photographie le mardi et paraît le vendredi. Juste AVANT la parution
# du vendredi, le plus récent rapport disponible est donc celui du mardi 10 jours
# plus tôt : dix jours d'écart sont NORMAUX et ne doivent rien déclencher. Un jour
# férié qui décale la parution ajoute deux ou trois jours. Au-delà de quatorze, ce
# n'est plus la latence de la source : soit elle a cessé de publier, soit ce
# collecteur ramène une valeur figée. On sort alors en ERREUR — les fichiers sont
# tout de même écrits, mais le passage est compté en échec, l'index de fraîcheur
# ne redate pas ces sorties et la panne devient visible au lieu de rester muette.
AGE_MAX_JOURS = 14


def get(url, essais=3):
    dernier = None
    for n in range(1, essais + 1):
        try:
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=90).read())
        except Exception as e:                                    # noqa: BLE001
            dernier = e
            if n < essais:
                time.sleep(8 * n)
    raise RuntimeError(f"CFTC injoignable : {dernier}")


def nombre(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def traiter(code, libelle, famille):
    q = urllib.parse.urlencode({
        "$where": f"cftc_contract_market_code='{code}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": SEMAINES,
    })
    lignes = get(f"{API}?{q}")
    if not lignes:
        return None

    series = {c: [] for c, _, _ in CATEGORIES}
    dates, oi_series = [], []
    for r in lignes:
        oi = nombre(r.get("open_interest_all"))
        if not oi:
            continue
        dates.append(r.get("report_date_as_yyyy_mm_dd", "")[:10])
        oi_series.append(oi)
        for cat, kl, ks in CATEGORIES:
            lo, sh = nombre(r.get(kl)), nombre(r.get(ks))
            # Le net EN PART DE L'OPEN INTEREST, jamais en contrats bruts : c'est
            # la seule forme comparable dans le temps et entre marchés.
            series[cat].append(None if lo is None or sh is None else (lo - sh) / oi * 100)

    if not dates:
        return None

    cats = {}
    for cat, _, _ in CATEGORIES:
        vals = [v for v in series[cat] if v is not None]
        if not vals:
            continue
        courant = vals[0]
        mu = sum(vals) / len(vals)
        sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        # Un z-score sans dispersion n'a pas de sens : on le tait plutôt que de
        # diviser par un epsilon et de publier un nombre inventé.
        z = None if sd < 1e-9 else (courant - mu) / sd
        precedent = vals[1] if len(vals) > 1 else None
        cats[cat] = {
            "net_pct_oi": round(courant, 3),
            "z_3a": None if z is None else round(z, 2),
            "variation": None if precedent is None else round(courant - precedent, 3),
            "min_3a": round(min(vals), 3), "max_3a": round(max(vals), 3),
            "n": len(vals),
            # Série renversée : la page lit du plus ancien au plus récent.
            "serie": [None if v is None else round(v, 3) for v in series[cat][::-1]],
        }

    return {
        "libelle": libelle,
        "famille": famille,
        "code": code,
        "arrete": dates[0],
        "open_interest": int(oi_series[0]),
        "dates": dates[::-1],
        "categories": cats,
    }


def ecrire(path, body):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body)
    tmp.replace(path)


def main():
    ancien = {}
    if OUT_JSON.exists():
        try:
            ancien = json.loads(OUT_JSON.read_text()).get("marches", {})
        except Exception:                                         # noqa: BLE001
            pass

    res, rates = {}, []
    for code, libelle, famille in MARCHES:
        try:
            r = traiter(code, libelle, famille)
            if not r:
                raise RuntimeError("aucune ligne")
            r["stale"] = False
            res[libelle] = r
            lev = r["categories"].get("leveraged", {})
            print(f"  {libelle:<14} arrêté {r['arrete']} · OI {r['open_interest']:>10,}"
                  f" · leveraged {lev.get('net_pct_oi'):>7} % (z {lev.get('z_3a')})")
        except Exception as e:                                    # noqa: BLE001
            rates.append(f"{libelle} : {type(e).__name__} {e}")
            if libelle in ancien:
                g = dict(ancien[libelle])
                g["stale"] = True
                res[libelle] = g
                print(f"  {libelle:<14} ÉCHEC — valeur précédente conservée")
        time.sleep(0.4)          # la CFTC n'aime pas les rafales

    if not any(not v.get("stale") for v in res.values()):
        sys.exit("aucun marché frais — fichiers laissés intacts")

    frais = [v["arrete"] for v in res.values() if not v.get("stale")]
    arrete = max(frais) if frais else None

    # L'âge est PORTÉ DANS LE FICHIER, pas seulement testé ici : la page peut ainsi
    # dire elle-même « donnée d'il y a n jours » sans refaire le calcul de calendrier,
    # et un lecteur peut vérifier la fraîcheur sans lire ce script.
    age = None
    if arrete:
        age = (datetime.now(timezone.utc).date()
               - datetime.strptime(arrete, "%Y-%m-%d").date()).days

    payload = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "publicreporting.cftc.gov — Traders in Financial Futures",
        "arrete": arrete,
        "age_jours": age,
        "lecture": {
            "latence": "rapport publié le vendredi, arrêté le MARDI précédent — "
                       "afficher la date d'arrêté, pas celle de publication",
            "mesure": "net = (longs − shorts) ÷ open interest × 100, spreads exclus "
                      "car une position de spread n'est pas une opinion directionnelle",
            "z": "z-score du net sur 160 rapports (~3 ans) — un net brut ne se "
                 "compare ni à son histoire ni aux autres marchés",
            "categories": {
                "dealer": "teneurs de marché : contrepartie, pas opinion",
                "asset_manager": "argent lent et directionnel",
                "leveraged": "hedge funds et CTA : l'argent rapide, celui des extrêmes",
            },
        },
        "marches": res,
        "echecs": rates,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    ecrire(OUT_JSON, body)
    ecrire(OUT_JS, "window.__TRADFI_COT__=" + body + ";\n")
    print(f"\n  {len(res)} marchés · arrêté {payload['arrete']} · {len(body)//1024} Ko")
    for r in rates:
        print(f"  échec : {r}")

    if age is not None and age > AGE_MAX_JOURS:
        return (f"arrêté {payload['arrete']} — {age} jours, au-delà des "
                f"{AGE_MAX_JOURS} tolérés : la source ne publie plus ou ce "
                f"collecteur sert une valeur figée")
    return 0


if __name__ == "__main__":
    sys.exit(main())
