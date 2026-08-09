#!/usr/bin/env python3
"""Vérifie les agrégations du collecteur Loris sur données synthétiques.

On contrôle quatre comportements qu'un simple passage en réel ne peut pas montrer
(il n'y a qu'un point) et dont une régression serait silencieuse :
  1. seuls les seaux CLOS sont agrégés — le seau courant reste dehors ;
  2. la valeur agrégée est bien la MÉDIANE des points du seau ;
  3. la rétention coupe les points trop vieux ;
  4. une venue apparue en cours de route est ajoutée en fin d'index, sans
     décaler les lignes déjà écrites.
"""
import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Dossier jetable : le test ne doit RIEN écrire dans le dépôt ni dans les caches
# réels — sinon le lancer abîmerait l'historique qu'il est censé protéger.
TMP = Path(tempfile.mkdtemp(prefix="loris_rollup_"))

# Chemin résolu depuis CE fichier, pas depuis le dossier personnel : le test doit
# tourner à l'identique sur un runner, où ~/Desktop n'existe pas.
CIBLE = Path(__file__).resolve().parent.parent / "scripts" / "fetch_loris_funding.py"
spec = importlib.util.spec_from_file_location("loris", CIBLE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

H5, H1, HD = TMP / "5m.json", TMP / "1h.json", TMP / "1d.json"
now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
ok = True


def check(label, got, want):
    global ok
    bon = got == want
    ok &= bon
    print(f"  [{'OK ' if bon else 'ÉCHEC'}] {label}: obtenu {got} · attendu {want}")


def ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 1. deux heures closes (hier) + l'heure courante, 4 points chacune ────────
# Heure A : BTC 1,0 / 2,0 / 3,0 / 10,0  -> médiane 2,5  (la moyenne vaudrait 4,0 :
# le test échouerait si quelqu'un remplaçait la médiane par une moyenne)
base = now - timedelta(days=1)
for i, val in enumerate([1.0, 2.0, 3.0, 10.0]):
    m.hist_append(H5, None, ts(base + timedelta(minutes=5 * i)),
                  {"binance": {"BTC": val, "ETH": val / 2}})
for i, val in enumerate([4.0, 4.0, 6.0, 6.0]):
    m.hist_append(H5, None, ts(base + timedelta(hours=1, minutes=5 * i)),
                  {"binance": {"BTC": val, "ETH": val / 2}})
# heure COURANTE : ne doit jamais être agrégée
for i in range(3):
    m.hist_append(H5, None, ts(now + timedelta(minutes=5 * i)),
                  {"binance": {"BTC": 99.0, "ETH": 99.0}})

n = m.rollup(H5, H1, None, lambda t: t[:13] + ":00:00Z")
h1 = json.loads(H1.read_text())
check("heures agrégées (le seau courant exclu)", n, 2)
check("médiane heure A (et non la moyenne 4.0)", h1["rows"][0][1][0], 2.5)
check("médiane heure B", h1["rows"][1][1][0], 5.0)
check("médiane ETH heure A", h1["rows"][0][2][0], 1.25)
check("aucune ligne à 99 (heure courante)",
      any(99.0 in (r[1] or []) for r in h1["rows"]), False)

# ── 2. relance : idempotence ────────────────────────────────────────────────
n2 = m.rollup(H5, H1, None, lambda t: t[:13] + ":00:00Z")
check("relance n'ajoute rien", n2, 0)
check("toujours 2 heures", len(json.loads(H1.read_text())["rows"]), 2)

# ── 3. venue apparue en cours de route ──────────────────────────────────────
m.hist_append(H5, None, ts(now + timedelta(minutes=20)),
              {"binance": {"BTC": 5.0, "ETH": 5.0}, "lighter": {"BTC": 7.0, "ETH": 7.0}})
h5 = json.loads(H5.read_text())
check("venue ajoutée en fin d'index", h5["venues"], ["binance", "lighter"])
premiere = h5["rows"][0]
check("ligne ancienne non décalée (binance toujours en 0)", premiere[1][0], 1.0)
check("ligne ancienne plus courte que l'index", len(premiere[1]) < len(h5["venues"]), True)

# ── 4. rétention ────────────────────────────────────────────────────────────
m.hist_append(H5, timedelta(hours=6), ts(now + timedelta(minutes=25)),
              {"binance": {"BTC": 1.0, "ETH": 1.0}})
restant = json.loads(H5.read_text())["rows"]
check("points de plus de 6 h purgés", all(r[0] >= ts(now - timedelta(hours=6))
                                          for r in restant), True)

# ── 5. jour depuis l'heure ──────────────────────────────────────────────────
nd = m.rollup(H1, HD, None, lambda t: t[:10] + "T00:00:00Z")
hd = json.loads(HD.read_text())
check("un jour clos agrégé", nd, 1)
check("médiane du jour (2.5 et 5.0 -> 3.75)", hd["rows"][0][1][0], 3.75)

print("\n" + ("TOUS LES CONTRÔLES PASSENT" if ok else "*** RÉGRESSION ***"))
sys.exit(0 if ok else 1)
