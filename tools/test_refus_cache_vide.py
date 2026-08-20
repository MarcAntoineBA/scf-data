#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_refus_cache_vide.py — Garde-fou : on ne remplace pas une donnée par son absence.

CE QU'IL EMPÊCHE DE REVENIR
Le 2026-08-20 à 22 h 37, deux collectes forcées à treize minutes d'intervalle ont
valu un refus de service de la source de capitalisations (tarif gratuit, plafond
d'appels). Le collecteur a poursuivi sa route et écrit un cache SANS AUCUNE
capitalisation, par-dessus un cache complet. Conséquences en chaîne : le rendu de la
page est mort sur « moins d'un élément » en cherchant le meilleur P/S, et s'il avait
survécu il aurait publié quinze N/A sous un horodatage tout frais — le même mensonge
que celui qu'on venait de corriger côté collecte, par une autre porte.

Un cache de quatre heures vaut infiniment mieux qu'un cache vide daté de maintenant.

Lancement : python3 tools/test_refus_cache_vide.py
"""

import importlib.util
import os
import sys

CHEMIN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "scripts", "fetch_l1_valuation.py")
spec = importlib.util.spec_from_file_location("fetch_l1_valuation", CHEMIN)
fl1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fl1)

échecs = []


def verifie(nom, obtenu, attendu):
    if obtenu != attendu:
        échecs.append(f"{nom}\n     attendu : {attendu!r}\n     obtenu  : {obtenu!r}")
    print(f"  {'✓' if obtenu == attendu else '✗'} {nom}")


def main():
    # Le cas réel : la source n'a rien rendu, le cache précédent était complet.
    verifie("zéro capitalisation sur un cache complet : refus",
            fl1.doit_refuser(0, 15), True)
    # Le même, sans cache précédent connu (première collecte d'une machine neuve) :
    # écrire quinze N/A resterait une publication vide, on refuse aussi.
    verifie("zéro capitalisation sans cache précédent : refus",
            fl1.doit_refuser(0, 0), True)
    # Un plafond atteint en cours de route : la moitié du parc disparaît d'un coup.
    verifie("la moitié du parc perdue d'un coup : refus",
            fl1.doit_refuser(6, 15), True)
    # Ce qui n'est PAS une panne et doit passer :
    verifie("collecte complète : on écrit", fl1.doit_refuser(15, 15), False)
    verifie("un token manquant : on écrit", fl1.doit_refuser(14, 15), False)
    verifie("un token de moins sur un tout petit parc : on écrit",
            fl1.doit_refuser(2, 4), False)
    verifie("première collecte réussie, sans passé : on écrit",
            fl1.doit_refuser(15, 0), False)
    # Et le refus doit VOYAGER : main() rend 1, le lanceur doit le propager.
    source = open(CHEMIN, encoding="utf-8").read()
    verifie("le code de sortie porte le refus jusqu'au lanceur",
            "sys.exit(main() or 0)" in source, True)

    print()
    if échecs:
        print(f"✗ {len(échecs)} contrôle(s) en échec :")
        for e in échecs:
            print(f"   · {e}")
        return 1
    print("✓ tous les contrôles passent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
