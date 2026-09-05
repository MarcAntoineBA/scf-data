#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_unite_actions.py — Garde-fou : un nombre d'actions dans la mauvaise unite
ne sort pas de la collecte, et tout ce qui se dit PAR ACTION avec lui non plus.

CE QU'IL EMPECHE DE REVENIR
Une societe qui depose ses actions en MILLIERS les depose ainsi partout : son
nombre de base confirme son nombre dilue, et son benefice par action confirme
les deux. Les deux controles internes du collecteur — « le BPA multiplie par les
actions doit rendre le resultat net », puis « le compte de base arbitre » — sont
alors aveugles, parce qu'ils comparent deux chiffres faux ENSEMBLE.

Mesure du 05/09/2026, sur le parc publie :
  · AIOT 2021 — 34 571 actions pour 126 M$ de produits, soit 3 644 $ de chiffre
    d'affaires par action sur un titre a 3,04 $. Le compte de base CONFIRMAIT le
    dilue : l'arbitre precedent les sauvait tous les deux.
  · PACK 2017 et 2018 — 995 actions et un benefice par action de 27 801 $. Les
    deux concordent parfaitement : aucun controle interne ne peut les departager.
  · EPAM 2011 — 20 473 actions et AUCUN benefice par action depose : le premier
    controle ne s'executait meme pas.

L'ARBITRE QUI TRANCHE EST EXTERIEUR AUX DEPOTS : le cours cote. Aucune societe
ne se traite a un centieme de son chiffre d'affaires par action.

⚠ ET IL FAUT LE BON COURS. Une premiere version passait `cours`, qui est
l'HISTORIQUE — une serie de couples, disponible pour 801 des 3 790 societes.
Le controle de type echouait en silence et l'arbitre restait muet pour les trois
quarts du parc. C'est `cours_cotation`, issu de la collecte de marche, qui
couvre tout le monde.
"""

import importlib.util
import os
import sys

ICI = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(ICI, "..", "scripts")
sys.path.insert(0, SCRIPTS)


def charger():
    spec = importlib.util.spec_from_file_location(
        "fc_test", os.path.join(SCRIPTS, "fondamentaux_communs.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["fc_test"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def main():
    m = charger()
    echecs = []

    def verifie(quoi, obtenu, attendu):
        ok = obtenu == attendu
        print("  %-58s %-22s %s" % (quoi, str(obtenu)[:22], "✓" if ok else "✗"))
        if not ok:
            echecs.append("%s — obtenu %r, attendu %r" % (quoi, obtenu, attendu))

    print("LES TROIS CAS REELS")

    # AIOT 2021 : une puissance de mille, on corrige.
    e = [{"annee": 2021, "shares_diluted": 34571.0, "shares_basic": 34000.0,
          "net_income": -12766000.0, "eps_diluted": -0.64, "revenue": 125960000.0}]
    m._corriger_unite_actions(e, cours=3.035)
    verifie("AIOT 2021 · les actions passent en unites", e[0]["shares_diluted"], 34571000.0)
    verifie("AIOT 2021 · la correction est declaree", e[0].get("shares_echelle_corrigee"), True)

    # PACK 2017 : actions et BPA concordent, tous deux absurdes → on efface.
    e = [{"annee": 2017, "shares_diluted": 995.0, "shares_basic": 995.0,
          "net_income": 27700000.0, "eps_diluted": 27801.44, "revenue": 209700000.0}]
    m._corriger_unite_actions(e, cours=5.045)
    verifie("PACK 2017 · le nombre d'actions est efface", e[0]["shares_diluted"], None)
    verifie("PACK 2017 · et la raison est ecrite",
            bool(e[0].get("shares_ecarte")), True)

    # EPAM 2011 : aucun BPA depose → le premier controle ne s'execute pas.
    e = [{"annee": 2011, "shares_diluted": 20473.0, "shares_basic": 20473.0,
          "net_income": 44353000.0, "eps_diluted": None, "revenue": 334528000.0}]
    m._corriger_unite_actions(e, cours=113.32)
    verifie("EPAM 2011 · traite malgre l'absence de benefice par action",
            e[0]["shares_diluted"] is None or e[0]["shares_diluted"] > 1e6, True)

    print()
    print("CONTRE-EPREUVES — ce qui ne doit PAS bouger")

    # Une societe normale : Apple 2025, ~15 milliards d'actions, 416 Md$ de CA.
    e = [{"annee": 2025, "shares_diluted": 15004697000.0, "shares_basic": 14948500000.0,
          "net_income": 112010000000.0, "eps_diluted": 7.46, "revenue": 416161000000.0}]
    m._corriger_unite_actions(e, cours=320.0)
    verifie("une societe normale n'est pas touchee",
            e[0]["shares_diluted"], 15004697000.0)

    # Une decote profonde — CA par action a dix fois le cours — reste publiee :
    # le seuil est a CENT fois, precisement pour ne pas toucher a ce cas.
    e = [{"annee": 2024, "shares_diluted": 1000000.0, "shares_basic": 1000000.0,
          "net_income": 1000000.0, "eps_diluted": 1.0, "revenue": 100000000.0}]
    m._corriger_unite_actions(e, cours=10.0)
    verifie("une decote profonde (CA/action = 10x le cours) est preservee",
            e[0]["shares_diluted"], 1000000.0)

    # Sans cours, l'arbitre ne peut rien dire — et ne doit rien casser.
    e = [{"annee": 2021, "shares_diluted": 34571.0, "shares_basic": 34000.0,
          "net_income": -12766000.0, "eps_diluted": -0.64, "revenue": 125960000.0}]
    m._corriger_unite_actions(e, cours=None)
    verifie("sans cours, rien n'est efface", e[0]["shares_diluted"], 34571.0)

    print()
    print("LA FORME DU COURS — le piege qui a rendu l'arbitre muet")
    e = [{"annee": 2021, "shares_diluted": 34571.0, "shares_basic": 34000.0,
          "net_income": -12766000.0, "eps_diluted": -0.64, "revenue": 125960000.0}]
    m._corriger_unite_actions(e, cours=[(1700000000, 2.9), (1780000000, 3.035)])
    verifie("une SERIE de cours est acceptee comme un scalaire",
            e[0]["shares_diluted"], 34571000.0)

    src = open(os.path.join(SCRIPTS, "fetch_sec_fundamentals.py"), encoding="utf-8").read()
    verifie("le collecteur SEC transmet le cours de COTATION",
            "cours_cotation=meta.get(\"cours_cotation\")" in src, True)

    print()
    if echecs:
        print("✗ %d controle(s) en echec :" % len(echecs))
        for x in echecs:
            print("   · %s" % x)
        return 1
    print("✓ tous les controles passent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
