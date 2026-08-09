#!/usr/bin/env python3
"""Volume short hors bourse (FINRA) — onglet Structure de marché, partie TradFi.

FINRA publie chaque jour, ticker par ticker, le volume marqué « short » sur les
transactions déclarées à ses TRF. Environ 12 000 titres par fichier, 540 Ko.

╔══════════════════════════════════════════════════════════════════════════════╗
║ CE QUE CE RATIO MESURE — ET LE CONTRESENS À NE PAS FAIRE                     ║
╚══════════════════════════════════════════════════════════════════════════════╝
Le fichier ne couvre QUE le volume HORS BOURSE : marchés B, Q et N sont les
facilités de déclaration (TRF) de FINRA, pas les bourses elles-mêmes. Le
« TotalVolume » n'est donc pas le volume consolidé, et ce module ne prétend pas
mesurer la part du hors-bourse dans le marché — il faudrait le consolidé pour
cela, que FINRA ne publie pas ici.

SURTOUT : un ratio short élevé NE VEUT PAS DIRE baissier. Un internalisateur qui
facilite l'achat d'un client se retrouve mécaniquement vendeur, et marque la
transaction « short ». Le ratio tourne donc structurellement autour de 50 %, et
il resterait à 50 % dans un marché parfaitement haussier. Toute l'information
est dans l'ÉCART à cette normale — d'où le z-score publié à côté du niveau, et
jamais le niveau seul.

Les volumes sont FRACTIONNAIRES depuis que les courtiers exécutent des fractions
d'action : 176779.078848 n'est pas une coquille, et arrondir à l'entier fausse
l'agrégat sur les petites capitalisations.

Rattrapage : au premier passage, on remonte 60 jours ouvrés pour que la série
naisse déjà lisible. Ensuite, seuls les jours manquants sont téléchargés — un
fichier par jour, jamais deux fois.

Sorties (~/Library/Caches/site_crypto_finance/) :
  tradfi_short_cache.json + .js     window.__TRADFI_SHORT__

On ne conserve AUCUN fichier brut : 60 jours pèseraient 32 Mo pour une série
agrégée de quelques kilo-octets, et le verrou de 25 Mo l'interdirait de toute façon.
"""
import json
import statistics
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "tradfi_short_cache.json"
OUT_JS = CACHE_DIR / "tradfi_short_cache.js"

BASE = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{}.txt"
UA = {"User-Agent": "Mozilla/5.0 (compatible; SiteCryptoFinance/1.0)"}

RATTRAPAGE = 60          # jours ouvrés remontés au premier passage
PLANCHER_VOL = 500_000   # volume hors bourse minimal pour figurer au palmarès
TOP = 15


def jour_ouvre(d):
    return d.weekday() < 5


def telecharger(d):
    """Renvoie (ratio_agrege, n_titres, [(ticker, ratio, volume)]) ou None."""
    url = BASE.format(d.strftime("%Y%m%d"))
    try:
        brut = urllib.request.urlopen(
            urllib.request.Request(url, headers=UA), timeout=90).read().decode("utf-8", "replace")
    except Exception:                                             # noqa: BLE001
        return None                                               # férié, ou pas encore publié
    court = total = 0.0
    lignes = []
    for ligne in brut.splitlines()[1:]:
        p = ligne.split("|")
        if len(p) < 5:
            continue
        try:
            s, t = float(p[2]), float(p[4])
        except ValueError:
            continue
        if t <= 0:
            continue
        court += s
        total += t
        lignes.append((p[1], s / t * 100, t))
    if total <= 0:
        return None
    return court / total * 100, len(lignes), lignes


def ecrire(path, body):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body)
    tmp.replace(path)


def main():
    ancien = {}
    if OUT_JSON.exists():
        try:
            ancien = json.loads(OUT_JSON.read_text())
        except Exception:                                         # noqa: BLE001
            pass
    serie = {r[0]: r[1] for r in ancien.get("serie", [])}
    connus = set(serie)

    # Fenêtre à couvrir. FINRA publie avec un jour de décalage : on part d'hier.
    fin = date.today() - timedelta(days=1)
    voulus, d, n = [], fin, 0
    while n < RATTRAPAGE and (fin - d).days < RATTRAPAGE * 2:
        if jour_ouvre(d):
            voulus.append(d)
            n += 1
        d -= timedelta(days=1)

    manquants = [x for x in voulus if x.strftime("%Y-%m-%d") not in connus]
    # Sans ce plafond, une longue interruption ferait télécharger cent fichiers
    # d'un coup et dépasser le temps imparti au collecteur.
    manquants = manquants[:RATTRAPAGE if not connus else 10]
    print(f"  {len(connus)} jour(s) déjà en série · {len(manquants)} à télécharger")

    dernier_detail = ancien.get("palmares")
    dernier_jour = ancien.get("jour")
    for d in sorted(manquants):
        r = telecharger(d)
        if r is None:
            continue
        ratio, n_titres, lignes = r
        cle = d.strftime("%Y-%m-%d")
        serie[cle] = round(ratio, 3)
        if dernier_jour is None or cle >= dernier_jour:
            dernier_jour = cle
            liquides = [x for x in lignes if x[2] >= PLANCHER_VOL]
            liquides.sort(key=lambda x: -x[1])
            dernier_detail = {
                "n_titres": n_titres,
                "n_liquides": len(liquides),
                "plus_shortes": [{"t": t, "ratio": round(rr, 2), "vol": int(v)}
                                 for t, rr, v in liquides[:TOP]],
                "moins_shortes": [{"t": t, "ratio": round(rr, 2), "vol": int(v)}
                                  for t, rr, v in liquides[-TOP:][::-1]],
            }
        print(f"  {cle} · ratio {ratio:.2f} % · {n_titres} titres")
        time.sleep(0.3)

    if not serie:
        sys.exit("aucune journée collectée — fichiers laissés intacts")

    lignes_serie = sorted(serie.items())
    vals = [v for _, v in lignes_serie]
    courant = vals[-1]
    mu = sum(vals) / len(vals)
    sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    z = None if sd < 1e-9 else round((courant - mu) / sd, 2)

    payload = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "cdn.finra.org/equity/regsho/daily",
        "jour": lignes_serie[-1][0],
        "ratio": courant,
        "moyenne": round(mu, 3),
        "ecart_type": round(sd, 3),
        "z": z,
        "n_jours": len(vals),
        "serie": [[k, v] for k, v in lignes_serie],
        "palmares": dernier_detail,
        "lecture": {
            "perimetre": "volume HORS BOURSE uniquement (TRF FINRA, marchés B/Q/N) — "
                         "pas le volume consolidé",
            "contresens": "un ratio élevé n'est PAS baissier : un internalisateur qui "
                          "facilite un achat se retrouve vendeur et marque « short ». "
                          "Le ratio tourne structurellement autour de 50 % ; "
                          "l'information est dans l'écart, jamais dans le niveau",
            "mesure": "ratio = Σ volume short ÷ Σ volume total, tous titres confondus",
            "plancher": f"palmarès limité aux titres à plus de {PLANCHER_VOL:,} "
                        f"actions hors bourse, sinon le classement ne montre que du bruit",
            "latence": "FINRA publie avec un jour ouvré de décalage",
        },
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    ecrire(OUT_JSON, body)
    ecrire(OUT_JS, "window.__TRADFI_SHORT__=" + body + ";\n")
    print(f"\n  {len(vals)} jours · ratio {courant:.2f} % (moyenne {mu:.2f}, z {z})"
          f" · {len(body)//1024} Ko")
    return 0


if __name__ == "__main__":
    sys.exit(main())
