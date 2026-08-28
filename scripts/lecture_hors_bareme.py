#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Retire la LECTURE des sociétés dont la note est refusée.

POURQUOI

Le barème compte vingt critères. Quand moins de dix sont mesurables — une banque
n'a ni marge brute, ni investissement, ni dette sur EBITDA — le collecteur refuse
de ramener la note : ramener quatre points obtenus sur six prétendrait à une
précision qu'on n'a pas. La fiche affiche alors « non notée », et c'est juste.

Mais la LECTURE, elle, continuait d'être calculée sur la note BRUTE — celle qui
compte les absences comme des zéros. La fiche écrivait donc « non notée » et,
juste à côté, « médiocre » : le refus annulé par l'étiquette qui le suit.

Mesuré le 28/08/2026 : 946 sociétés sur 3 462 (27,3 %) sont dans ce cas. Shell —
249 Md$ — sortait à 1,0/20 sur sept critères mesurables, donc « médiocre ». Ce
n'est pas un jugement sur Shell : c'est le constat que treize de nos vingt
critères ne s'appliquent pas à une compagnie pétrolière intégrée. Alibaba,
Petrobras, KKR, Brookfield, US Bancorp, Nubank étaient logées à la même enseigne.

CE QUE FAIT CE SCRIPT

Le collecteur porte désormais la règle. Ce script l'applique aux paquets déjà
écrits, sans une requête : `lecture` est une valeur DÉRIVÉE du résumé, dont rien
ne dépend en aval — la recalculer hors ligne ne laisse aucune incohérence.
`lecture_bareme_concurrent` reste intacte : elle décrit sa note à lui, pas la
nôtre, et elle a le droit d'être sévère.
"""
import glob
import json
import os
import sys

CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")
SITE = os.path.expanduser("~/Site_Crypto_Finance/data")
MOTIFS = ("sec_detail_[0-9][0-9][0-9].json", "intl_detail_*.json")


def main():
    total = touchees = 0
    exemples = []
    for motif in MOTIFS:
        for f in sorted(glob.glob(os.path.join(CACHE, motif))):
            if "sync-conflict" in f:
                continue
            try:
                with open(f, encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception:
                continue
            change = False
            for sym, v in (d.get("societes") or {}).items():
                nq = (v.get("resume") or {}).get("note_q")
                if not isinstance(nq, dict):
                    continue
                total += 1
                if nq.get("note_ramenee") is not None or nq.get("lecture") is None:
                    continue
                if len(exemples) < 8:
                    exemples.append((sym, nq.get("note"), nq.get("lecture"),
                                     nq.get("criteres_notables")))
                nq["lecture"] = None
                touchees += 1
                change = True
            if change:
                texte = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
                with open(f, "w", encoding="utf-8") as fh:
                    fh.write(texte)
                jumeau = os.path.join(SITE, os.path.basename(f))
                if os.path.isdir(SITE):
                    with open(jumeau, "w", encoding="utf-8") as fh:
                        fh.write(texte)

    print("%d sociétés portent une note" % total)
    print("   %d dont la lecture est retirée — la note était refusée, "
          "la lecture la contredisait" % touchees)
    if exemples:
        print()
        for sym, n, lec, nb in exemples:
            print("      %-10s %4s/20 sur %s critère(s) — était « %s »"
                  % (sym, n, nb, lec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
