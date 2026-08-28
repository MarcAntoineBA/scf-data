#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rejouer_calculs_sec.py — Propage un correctif de CALCUL aux paquets SEC,
                         sans réseau et sans reconstruire ce qu'on ne sait pas refaire.

POURQUOI IL EST CIBLÉ, ET NON GÉNÉRAL

Le jumeau international reconstruit le résumé entier : là-bas, `construire_resume`
ne dépend que des exercices. Côté SEC, le même bloc lit une capitalisation, une
devise déduite en cours de route et une variable de module — le sortir en fin de
session serait un refactor risqué pour un gain nul.

Ce script ne touche donc QUE ce que les correctifs de calcul modifient :

  · les taux de croissance, recalculés depuis les séries par action déjà
    stockées — c'est là que vivent la base infinitésimale et la traversée de
    zéro ;
  · les ratios sortis de la bande de plausibilité, écartés ;
  · la note, qui se déduit des deux précédents ;
  · la note historique, calculée sur les mêmes exercices.

Tout le reste du résumé est laissé intact. Un rejeu qui ne reconstruit pas ne
peut pas perdre une clé — c'est le défaut que le jumeau international a failli
commettre, où quatre champs venus de la collecte disparaissaient sur les 19 495
sociétés.

CE QU'IL NE REMPLACE PAS

Les correctifs de VOCABULAIRE — une étiquette XBRL qu'on ne demandait pas — ne
se rejouent pas : la donnée n'est pas dans le paquet, il faut retourner la
chercher. Seule une collecte les applique.

Lancement :
    python3 scripts/rejouer_calculs_sec.py --essai    # mesure sans rien écrire
    python3 scripts/rejouer_calculs_sec.py            # écrit
"""

import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_sec_fundamentals import historique_note            # noqa: E402
from fondamentaux_communs import (                            # noqa: E402
    _croissances, ecarter_ratios_degeneres, note_quantitative,
)

CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")

# (clé de la croissance, champ par action de l'exercice)
SERIES = (("ca", "ca_par_action"), ("eps", "eps_diluted"),
          ("fcf", "fcf_par_action"), ("ocf", "ocf_par_action"), ("div", "dps"))


def main():
    essai = "--essai" in sys.argv
    t0 = time.time()
    paquets = [f for f in sorted(glob.glob(os.path.join(CACHE, "sec_detail_[0-9][0-9][0-9].json")))
               if "sync-conflict" not in f]
    if not paquets:
        print("[fatal] aucun paquet sec_detail_NNN.json", file=sys.stderr)
        return 2

    horodatage = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    total = touchees = 0
    retirees = {c: 0 for c, _ in SERIES}
    ecartes = 0
    montees = descendues = 0
    mouvements = []

    for f in paquets:
        try:
            with open(f, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        change = False
        for sym, v in (doc.get("societes") or {}).items():
            ex = v.get("exercices") or []
            r = v.get("resume")
            if not ex or not isinstance(r, dict):
                continue
            total += 1
            avant = (r.get("note_q") or {}).get("note_ramenee")

            # ── Les croissances, depuis les séries déjà stockées ──
            neuves = {}
            for cle, champ in SERIES:
                serie = [(e.get("annee"), e.get(champ)) for e in ex]
                neuves[cle] = _croissances(serie)
                anc = ((r.get("croissances") or {}).get(cle) or {})
                for fen in ("1a", "5a", "10a"):
                    if anc.get(fen) is not None and neuves[cle].get(fen) is None:
                        retirees[cle] += 1
            r["croissances"] = neuves

            # ── Les ratios hors bande ──
            ecartes += ecarter_ratios_degeneres(r)

            # ── Ce qui s'en déduit ──
            r["note_q"] = note_quantitative(r)
            try:
                r["note_historique"] = historique_note(ex)
            except Exception as e:
                print("[warn] %s : note historique : %s" % (sym, e), file=sys.stderr)
            r["calculs_rejoues_le"] = horodatage

            apres = (r.get("note_q") or {}).get("note_ramenee")
            if isinstance(avant, (int, float)) and isinstance(apres, (int, float)):
                if apres > avant:
                    montees += 1
                elif apres < avant:
                    descendues += 1
                if abs(apres - avant) >= 2:
                    mouvements.append((round(apres - avant, 1), sym, avant, apres))
            touchees += 1
            change = True

        if change and not essai:
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))

    print("[ok] %d société(s) lues, %d rejouée(s) — %.1f s"
          % (total, touchees, time.time() - t0))
    print("[ok] taux retirés (base infinitésimale ou traversée de zéro) : %s"
          % ", ".join("%s %d" % (k, n) for k, n in sorted(retirees.items())))
    print("[ok] ratios hors bande écartés : %d" % ecartes)
    print("[ok] notes ramenées : %d en hausse, %d en baisse" % (montees, descendues))
    if mouvements:
        mouvements.sort()
        print("[ok] les plus gros mouvements :")
        for d, sym, a, b in (mouvements[:3] + mouvements[-3:]):
            print("      %-8s %.1f → %.1f  (%+.1f)" % (sym, a, b, d))
    if essai:
        print()
        print("[essai] RIEN N'A ÉTÉ ÉCRIT.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
