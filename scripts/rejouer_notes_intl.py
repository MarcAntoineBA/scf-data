#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rejoue la note des paquets internationaux avec le barème courant.

POURQUOI

Le critère du dividende compte désormais les années SANS BAISSE et non les
années de hausse — parce que c'est ce que mesure le nombre qu'affiche le
concurrent, et que la promesse est de recalculer SA note depuis les dépôts
officiels. Le changement est arrivé dans le collecteur, donc dans les paquets
américains recollectés dans la foulée.

Les dix-neuf mille paquets internationaux, eux, portaient encore l'ancien
barème : le détail de leur note affichait « Années de hausse consécutive » là
où la fiche américaine affiche « Années sans baisse du dividende ». La même
page, deux définitions du même critère selon la nationalité de la société —
exactement ce que `fondamentaux_communs.py` existe pour empêcher.

Une collecte internationale complète demanderait des heures et sept jours de
tranches. Or la note est une valeur DÉRIVÉE : elle se recalcule entièrement à
partir du résumé et des exercices déjà écrits, sans une requête.

⚠ ON N'EN ÉCRIT PAS UNE SECONDE VERSION. Le script importe `note_quantitative`
et `notes_historiques` du collecteur lui-même. Deux copies du même calcul
divergent toujours, et la divergence ne se voit jamais.
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_intl_fundamentals import notes_historiques      # noqa: E402
from fondamentaux_communs import note_quantitative         # noqa: E402

CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")
SITE = os.path.expanduser("~/Site_Crypto_Finance/data")


def main():
    paquets = [f for f in sorted(glob.glob(os.path.join(CACHE, "intl_detail_*.json")))
               if "sync-conflict" not in f]
    if not paquets:
        print("[fatal] aucun paquet intl_detail_*.json", file=sys.stderr)
        return 2

    total = rejouees = montees = descendues = 0
    ecarts = []
    for f in paquets:
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        change = False
        for sym, v in (d.get("societes") or {}).items():
            r = v.get("resume")
            ex = v.get("exercices") or []
            if not isinstance(r, dict) or not ex:
                continue
            total += 1
            avant = (r.get("note_q") or {}).get("note")
            try:
                nq = note_quantitative(r)
                hist = notes_historiques(ex)
            except Exception as e:
                print("[warn] %s : %s" % (sym, e), file=sys.stderr)
                continue
            r["note_q"] = nq
            r["note_historique"] = hist
            rejouees += 1
            change = True
            apres = nq.get("note")
            if isinstance(avant, (int, float)) and isinstance(apres, (int, float)):
                if apres > avant:
                    montees += 1
                elif apres < avant:
                    descendues += 1
                if abs(apres - avant) >= 0.5:
                    ecarts.append((apres - avant, sym, avant, apres))
        if change:
            texte = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(texte)
            if os.path.isdir(SITE):
                with open(os.path.join(SITE, os.path.basename(f)), "w",
                          encoding="utf-8") as fh:
                    fh.write(texte)

    print("%d sociétés internationales · %d notes rejouées" % (total, rejouees))
    print("   %d montent · %d descendent · %d inchangées"
          % (montees, descendues, rejouees - montees - descendues))
    if ecarts:
        print()
        print("   les plus gros écarts :")
        for d_, sym, a, b in sorted(ecarts, key=lambda x: -abs(x[0]))[:8]:
            print("      %-12s %4.1f → %4.1f  (%+.1f)" % (sym, a, b, d_))
    return 0


if __name__ == "__main__":
    sys.exit(main())
