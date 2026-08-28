# -*- coding: utf-8 -*-
"""Garde-fou : annule toute perte de richesse introduite par le DERNIER commit.

`run_jobs.py collect()` recopie vers le dépôt tout fichier dont le contenu
diffère. Il compare le contenu, PAS la richesse — et ce poste ne collecte qu'une
partie du parc : l'archive d'open interest, le calendrier macro, les icônes des
marchés de prédiction viennent de GitHub Actions, et le dépôt en détient des
versions plus complètes que la copie locale.

Le lanceur signale les pertes de plus d'un tiers sur les fichiers de plus de
cinquante kilooctets, mais il AVERTIT sans EMPÊCHER, au milieu d'un journal de
cinq cents lignes. Ce script est la moitié manquante.

Il tourne APRÈS l'acheminement, sur le commit que celui-ci vient d'écrire, et
restaure tout fichier ayant perdu plus de dix pour cent — dans le dépôt ET dans
le cache local, sans quoi le passage suivant referait la même chose.

Ce qu'il ne touche pas :
  · les paquets de fondamentaux, qui sont la sortie légitime de ce poste ;
  · les bilans et registres de flotte, qui changent de taille par construction.
"""
import os
import shutil
import subprocess
import sys

DEPOT = os.path.expanduser("~/scf-data-work")
CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")
MIENS = ("sec_detail_", "intl_detail_", "sec_fundamentals_index",
         "intl_fundamentals_index")
EXCLUS = ("_fleet_status", "_registre", "_ecrits", "freshness")


def git(*args, binaire=False):
    r = subprocess.run(["git", "-C", DEPOT] + list(args), capture_output=True)
    return r.stdout if binaire else r.stdout.decode("utf-8", "replace")


commit = (sys.argv[1] if len(sys.argv) > 1 else "HEAD").strip()
sujet = git("log", "-1", "--format=%h %s", commit).strip()
print("commit examiné : %s" % sujet)

touches = [l.split("\t")[-1] for l in
           git("show", "--numstat", "--format=", commit).splitlines()
           if l.strip() and "\t" in l]
print("%d fichier(s) touchés" % len(touches))

a_reprendre = []
for f in touches:
    if not f.startswith("cache/"):
        continue
    b = os.path.basename(f)
    if b.startswith(MIENS) or b.startswith(EXCLUS):
        continue
    avant = len(git("show", "%s^:%s" % (commit, f), binaire=True))
    apres = len(git("show", "%s:%s" % (commit, f), binaire=True))
    if avant > 0 and apres < avant * 0.90:
        a_reprendre.append((f, avant, apres))

if not a_reprendre:
    print("✓ aucune perte de richesse — rien à faire.")
    raise SystemExit(0)

print("\n%d fichier(s) ont perdu plus de dix pour cent :" % len(a_reprendre))
for f, a, c in a_reprendre[:10]:
    print("   %-44s %8d → %8d  (%d %%)" % (os.path.basename(f), a, c, 100 * c // a))
if len(a_reprendre) > 10:
    print("   … et %d autres" % (len(a_reprendre) - 10))

chemins = [f for f, _, _ in a_reprendre]
for i in range(0, len(chemins), 100):
    subprocess.run(["git", "-C", DEPOT, "checkout", "%s^" % commit, "--"]
                   + chemins[i:i + 100], check=False)
n = 0
for f, _, _ in a_reprendre:
    src = os.path.join(DEPOT, f)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(CACHE, os.path.basename(f)))
        n += 1
print("\n   %d restaurés dans le dépôt ET dans le cache local" % n)

ko = sum(1 for f, a, _ in a_reprendre
         if not os.path.exists(os.path.join(DEPOT, f))
         or os.path.getsize(os.path.join(DEPOT, f)) < a * 0.99)
print("   contrôle : %s" % ("✓ toutes retrouvées" if ko == 0
                            else "✗ %d incomplètes" % ko))
