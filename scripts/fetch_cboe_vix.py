#!/usr/bin/env python3
"""Structure par terme du VIX — onglet Structure de marché, partie TradFi.

Le Cboe publie l'historique quotidien de ses indices de volatilité en CSV nu,
sans clé ni quota, depuis 1990 pour le VIX. Quatre échéances plus deux indices
dérivés suffisent à lire le PRIX DE LA COUVERTURE — l'angle qui manque au gamma
(comment les dealers réagiront) et au COT (qui est positionné).

  VIX9D   9 jours    la peur immédiate
  VIX     30 jours   la référence
  VIX3M   3 mois     l'ancrage
  VIX6M   6 mois     le fond de marché
  SKEW               le prix relatif des puts hors de la monnaie
  VVIX               la volatilité du VIX lui-même

╔══════════════════════════════════════════════════════════════════════════════╗
║ CE QUE LA PENTE DIT                                                          ║
╚══════════════════════════════════════════════════════════════════════════════╝
En temps normal la courbe est en CONTANGO : VIX3M > VIX. Assurer six mois coûte
plus cher qu'assurer un mois, comme toute assurance. Quand la courbe s'INVERSE —
VIX au-dessus de VIX3M — le marché paie plus cher pour se couvrir tout de suite
que pour se couvrir dans trois mois : c'est le signe d'un stress qu'on croit
passager mais qu'on ne veut pas subir. L'inversion est rare et ne dure pas.

    pente = VIX3M ÷ VIX     > 1 contango (calme)   < 1 backwardation (stress)

Le NIVEAU du VIX ne dit presque rien seul : 18 est bas dans un régime nerveux et
haut dans un régime calme. On publie donc le percentile sur un an et sur cinq
ans à côté de chaque valeur, jamais la valeur seule.

Sorties (~/Library/Caches/site_crypto_finance/) :
  tradfi_vix_cache.json + .js     window.__TRADFI_VIX__

Les historiques complets pèsent 1,4 Mo cumulés. On n'écrit que les valeurs
courantes, les percentiles et UN AN de série quotidienne pour le graphe — le
reste ne servirait qu'à alourdir la page.
"""
import json
import statistics
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "tradfi_vix_cache.json"
OUT_JS = CACHE_DIR / "tradfi_vix_cache.js"

BASE = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{}_History.csv"
UA = {"User-Agent": "Mozilla/5.0 (compatible; SiteCryptoFinance/1.0)"}

INDICES = [
    ("VIX9D", "9 jours", "la peur immédiate"),
    ("VIX", "30 jours", "la référence"),
    ("VIX3M", "3 mois", "l'ancrage"),
    ("VIX6M", "6 mois", "le fond de marché"),
    ("SKEW", "—", "prix relatif des puts hors de la monnaie"),
    ("VVIX", "—", "volatilité du VIX lui-même"),
]

JOURS_AN = 252
SERIE_PUBLIEE = 252


def charger(nom):
    """Renvoie [(date, clôture)] trié du plus ancien au plus récent.

    Les fichiers n'ont pas tous la même forme : VIX porte OPEN/HIGH/LOW/CLOSE,
    SKEW et VVIX une seule colonne de valeur. On prend donc la DERNIÈRE colonne
    numérique de chaque ligne plutôt qu'un index fixe, qui casserait sur deux
    fichiers sur six.
    """
    txt = urllib.request.urlopen(
        urllib.request.Request(BASE.format(nom), headers=UA), timeout=90
    ).read().decode("utf-8", "replace")
    out = []
    for ligne in txt.splitlines()[1:]:
        p = [x.strip() for x in ligne.split(",")]
        if len(p) < 2:
            continue
        try:
            d = datetime.strptime(p[0], "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        val = None
        for x in reversed(p[1:]):
            try:
                val = float(x)
                break
            except ValueError:
                continue
        if val is not None and val > 0:
            out.append((d, val))
    out.sort()
    return out


def percentile(vals, x):
    if not vals:
        return None
    return round(sum(1 for v in vals if v <= x) / len(vals) * 100, 1)


def main():
    ancien = {}
    if OUT_JSON.exists():
        try:
            ancien = json.loads(OUT_JSON.read_text()).get("indices", {})
        except Exception:                                         # noqa: BLE001
            pass

    res, rates, series = {}, [], {}
    for nom, echeance, role in INDICES:
        try:
            s = charger(nom)
            if not s:
                raise RuntimeError("série vide")
            series[nom] = s
            vals = [v for _, v in s]
            courant = vals[-1]
            an = vals[-JOURS_AN:]
            cinq = vals[-JOURS_AN * 5:]
            res[nom] = {
                "echeance": echeance, "role": role,
                "valeur": round(courant, 2),
                "jour": s[-1][0],
                "variation": round(courant - vals[-2], 2) if len(vals) > 1 else None,
                "pct_1a": percentile(an, courant),
                "pct_5a": percentile(cinq, courant),
                "min_1a": round(min(an), 2), "max_1a": round(max(an), 2),
                "depuis": s[0][0],
                "stale": False,
            }
            print(f"  {nom:<6} {courant:>7.2f}  au {s[-1][0]}"
                  f"  · percentile 1 an {res[nom]['pct_1a']:>5} %"
                  f"  · {len(s)} séances depuis {s[0][0]}")
        except Exception as e:                                    # noqa: BLE001
            rates.append(f"{nom} : {type(e).__name__} {e}")
            if nom in ancien:
                g = dict(ancien[nom])
                g["stale"] = True
                res[nom] = g
                print(f"  {nom:<6} ÉCHEC — valeur précédente conservée")

    if not any(not v.get("stale") for v in res.values()):
        sys.exit("aucun indice frais — fichiers laissés intacts")

    pente = None
    if "VIX" in res and "VIX3M" in res and res["VIX"]["valeur"]:
        pente = round(res["VIX3M"]["valeur"] / res["VIX"]["valeur"], 4)

    # Série publiée : un an, sur les dates du VIX. Aligner sur une base commune
    # évite qu'un jour férié propre à un indice décale les courbes entre elles.
    courbe = []
    if "VIX" in series:
        dates = [d for d, _ in series["VIX"]][-SERIE_PUBLIEE:]
        index = {n: dict(s) for n, s in series.items()}
        for d in dates:
            courbe.append([d] + [index.get(n, {}).get(d) for n, _, _ in INDICES])

    payload = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "cdn.cboe.com/api/global/us_indices/daily_prices",
        "jour": res.get("VIX", {}).get("jour"),
        "indices": res,
        "pente_3m_1m": pente,
        "regime": None if pente is None else ("contango" if pente > 1 else "backwardation"),
        "colonnes_courbe": ["date"] + [n for n, _, _ in INDICES],
        "courbe": courbe,
        "lecture": {
            "pente": "pente = VIX3M ÷ VIX. Au-dessus de 1, contango : assurer trois "
                     "mois coûte plus cher qu'assurer un mois, c'est la normale. "
                     "En dessous, backwardation : le marché paie plus cher pour se "
                     "couvrir tout de suite — stress qu'on croit passager",
            "niveau": "le niveau du VIX seul ne dit presque rien : 18 est bas dans un "
                      "régime nerveux et haut dans un régime calme. D'où les "
                      "percentiles à 1 an et 5 ans publiés à côté de chaque valeur",
            "latence": "clôtures quotidiennes — la valeur du jour paraît après séance",
        },
        "echecs": rates,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    for path, contenu in ((OUT_JSON, body), (OUT_JS, "window.__TRADFI_VIX__=" + body + ";\n")):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(contenu)
        tmp.replace(path)

    print(f"\n  pente VIX3M/VIX {pente} → {payload['regime']}"
          f" · courbe {len(courbe)} séances · {len(body)//1024} Ko")
    for r in rates:
        print(f"  échec : {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
