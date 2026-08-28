#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Achemine vers le dépôt le travail d'une collecte lancée à la main.

POURQUOI CE SCRIPT EXISTE (28/08/2026)

`run_jobs.py` fait deux choses : il LANCE les collecteurs d'une cadence, puis il
ACHEMINE leurs sorties vers le dépôt. Quand on relance un collecteur seul — pour
corriger un défaut, comme la collecte SEC de ce jour — la première moitié est
inutile et la seconde manque. On acheminait donc à la main, avec un `cp`, et on
oubliait la moitié des règles.

Ce jour-là, l'oubli s'est vu : les 512 paquets SEC publiés portaient encore
l'ancienne collecte. NetEase y affichait 112 milliards de chiffre d'affaires en
« dollars » — c'étaient des yuans. Le travail d'une nuit entière restait invisible
en ligne, et personne ne pouvait le savoir en regardant le site.

CE QU'IL FAIT, ET DANS CET ORDRE
  1. `collect()` de run_jobs — la MÊME fonction, donc la même garde anti-fonte
     (un fichier qui perd plus d'un tiers de son poids n'est pas publié) ;
  2. l'index de fraîcheur `_fichiers.json`, redaté pour les seuls fichiers
     réellement écrits — sans lui la page annonce « cache indisponible », et
     c'est un défaut qu'on a déjà payé ;
  3. rien d'autre. Il ne commite pas, il ne pousse pas : ces deux gestes-là
     restent à la main, parce qu'ils sont irréversibles côté public.

Usage :
    python3 tools/acheminer.py                 # tout le manifeste
    python3 tools/acheminer.py sec_detail      # les fichiers dont le nom contient ceci
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import index_fraicheur                                   # noqa: E402
import run_jobs                                          # noqa: E402


def main():
    motif = sys.argv[1] if len(sys.argv) > 1 else ""
    with open(run_jobs.MANIFEST, encoding="utf-8") as fh:
        manifest = [l.strip() for l in fh
                    if l.strip() and not l.strip().startswith("#")]
    if motif:
        manifest = [n for n in manifest if motif in n]
        print("filtre « %s » : %d fichier(s) du manifeste" % (motif, len(manifest)))
    if not manifest:
        print("[fatal] aucun fichier à acheminer", file=sys.stderr)
        return 2

    t0 = time.time()
    small, big, absent, retires, fondus = run_jobs.collect(manifest)
    ecrits = set(small) | set(big)

    print("%d fichier(s) acheminé(s) en %.1f s : %d versionné(s), %d en pièce jointe"
          % (len(ecrits), time.time() - t0, len(small), len(big)))
    if absent:
        print("  %d absent(s) du cache local : %s%s"
              % (len(absent), ", ".join(absent[:8]), " …" if len(absent) > 8 else ""))
    if fondus:
        # Le refus est le comportement voulu, mais il doit se VOIR : c'est en
        # laissant cet avertissement au milieu de cinq cents lignes de journal
        # qu'on avait publié 93 caches appauvris sans s'en apercevoir.
        print("  ! %d fichier(s) ont FONDU d'un tiers ou plus et n'ont PAS été "
              "publiés — la copie du dépôt reste :" % len(fondus))
        for f in fondus[:10]:
            print("      %s" % f)

    # L'index de fraîcheur ne connaît que ce qu'on vient d'écrire : redater un
    # fichier qu'on n'a pas touché le ferait passer pour frais, ce qui est
    # exactement le mensonge que cet index existe pour supprimer.
    quand = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    chemin = os.path.join(run_jobs.CACHE_OUT, "_fichiers.json")
    n = index_fraicheur.ecrire(chemin, [run_jobs.CACHE_OUT, run_jobs.RELEASE_OUT],
                               quand, ecrits)
    print("index de fraîcheur : %s fichier(s) datés" % n)
    print()
    print("Rien n'est commité ni poussé. À faire à la main, depuis ~/scf-data-work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
