#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_sentinelle_relance.py — Recaler un seuil d'AFFICHAGE ne retarde pas la
RELANCE de la collecte.

CE QU'IL EMPÊCHE DE REVENIR
Le 30/09/2026, les seuils du bandeau « Données datées de Xh » ont été relevés
sur ce que voit réellement le visiteur (le bandeau s'affichait 5 à 6 h par jour
sans aucune panne). La sentinelle relançait la collecte à « seuil − 2 h » : le
même relèvement aurait repoussé le rattrapage des narratifs de 9 h à 18 h. Elle
relance désormais dès qu'une chaîne saine aurait dû rafraîchir le bloc.

Lancer : python3 tools/test_sentinelle_relance.py   (aucun accès réseau)
"""
import importlib.util
import os
import sys

ICI = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "sentinelle_publique", os.environ.get("SCF_SENTINELLE", os.path.join(ICI, "sentinelle_publique.py")))
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)

échecs = []


def verifie(quoi, obtenu, attendu):
    ok = obtenu == attendu
    print("  %-60s %-8s %s" % (quoi, obtenu, "✓" if ok else "✗ attendu " + str(attendu)))
    if not ok:
        échecs.append(quoi)


# (bloc, ancien seuil, nouveau seuil, cadence cloud d'après jobs.json au 30/09/2026)
BLOCS = [("comparateur", 8, 12, "1h"), ("indices", 12, 12, "1h"), ("fondamentaux", 11, 28, "6h"),
         ("narratifs", 11, 20, "6h"), ("historiques", 22, 22, "1h"), ("fiches crypto", 14, 30, "6h"),
         ("capture crypto", 11, 30, "6h"), ("calendrier", 28, 28, "6h"), ("secteurs", 28, 32, "daily")]


def main():
    if not hasattr(sp, "seuil_relance"):
        print("✗ pas de seuil_relance : la relance suit encore « seuil − 2 h »")
        return 1
    print("LE CAS RÉEL")
    verifie("narratifs (6h), seuil 20 : relance à 8,5 h, pas à 18 h", sp.seuil_relance(20, {"6h"}), 8.5)
    verifie("secteurs (quotidien), seuil 32 : relance à 26,5 h", sp.seuil_relance(32, {"daily"}), 26.5)
    print()
    print("AUCUN BLOC RELANCÉ PLUS TARD QU'AVANT (à 30 min près : quotidien 26 → 26,5 h)")
    for nom, ancien, nouveau, cad in BLOCS:
        avant = ancien - sp.MARGE_RATTRAPAGE_H
        apres = sp.seuil_relance(nouveau, {cad})
        verifie("%s : %g h → %g h" % (nom, avant, apres), apres <= avant + 0.5, True)
    print()
    print("SANS CADENCE CONNUE : L'ANCIENNE RÈGLE")
    verifie("cadence inconnue", sp.seuil_relance(20, {"mensuel"}), 18.0)
    verifie("aucune cadence", sp.seuil_relance(20, set()), 18.0)
    verifie("seuil plus court que la cadence : le seuil gagne", sp.seuil_relance(8, {"daily"}), 6.0)
    print()
    print("LA COPIE DE SECOURS = LA PAGE")
    verifie("copie de secours : secteurs à 32 h",
            dict((l, t) for l, _, t in sp.BANDEAU_SECOURS).get("secteurs"), 32.0)
    print()
    if échecs:
        print("✗ %d contrôle(s) en échec :" % len(échecs))
        for e in échecs:
            print("   · %s" % e)
        return 1
    print("✓ tous les contrôles passent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
