#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_garde_temoins.py — Garde-fou : on ne publie pas un cache qui a perdu des
valeurs qu'il portait la fois d'avant.

CE QU'IL EMPÊCHE DE REVENIR
Le 2026-08-30, `tradfi_fundamentals_cache.js` est passé de 690 à 624 cours en
deux heures. NVIDIA, Oracle, Qualcomm, Samsung et quarante banques ont perdu
leur cotation, et la fiche société n'affichait plus qu'un tiret à la place du
cours.

Les deux gardes existantes ont dit oui, toutes les deux :

    garde de POIDS     93 % du poids conservé (1 037 → 962 Ko)  → laisse passer
    garde de FRAÎCHEUR horodatage plus récent (11:45 → 14:18)   → laisse passer

Elles regardent l'ENVELOPPE. Aucune ne regarde ce qu'il y a dedans. C'est par ce
trou-là qu'une collecte à moitié en échec publie quand même — et la cause était
un repli `yahooquery` qui réécrivait la société avec quinze champs au lieu de
cinquante-deux.

CE QUE LA GARDE FAIT
Elle relit l'ancienne et la nouvelle version, en extrait les mêmes TÉMOINS, et
refuse la publication si un témoin présent avant a disparu après.

⚠ ON NE COMPARE PAS « LA RICHESSE ». Un compte de champs non nuls protège aussi
la donnée fausse : un fichier entièrement rempli de zéros passerait haut la
main. On compare des valeurs NOMMÉES dont on sait ce qu'elles doivent valoir —
« NVIDIA a-t-elle un cours ? » se vérifie, « le remplissage a-t-il baissé de
8 % ? » ne se juge pas.

⚠ UN GARDE QUI REFUSE TOUT EST AUSSI INUTILE QU'UN GARDE QUI NE REFUSE RIEN.
D'où les contre-épreuves ci-dessous, qui pèsent autant que le cas réel : une
version identique, une version plus riche et un fichier hors table doivent
passer sans broncher.

Lancement : python3 tools/test_garde_temoins.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_jobs  # noqa: E402

échecs = []


def verifie(quoi, obtenu, attendu):
    ok = obtenu == attendu
    print("  %-46s %-16s %s" % (quoi, obtenu, "✓" if ok else "✗ attendu " + str(attendu)))
    if not ok:
        échecs.append(quoi)


def cache(chemin, cours_par_symbole):
    """Un cache minimal, à la forme réelle : sectors[].stocks[].

    Les symboles dont le cours vaut None sont présents mais sans cotation —
    c'est exactement l'état dans lequel le repli laissait NVIDIA."""
    stocks = [{"symbol": s, "name": s, "price_usd": p}
              for s, p in cours_par_symbole.items()]
    d = {"updated": "2026-08-30 12:00", "sectors": [{"narrative": "T", "stocks": stocks}]}
    with open(chemin, "w", encoding="utf-8") as h:
        h.write("window.__TRADFI_FUNDAMENTALS__=" + json.dumps(d) + ";")


def main():
    base = "/tmp/_test_temoins"
    os.makedirs(base, exist_ok=True)

    # Un parc assez grand pour que le seuil de 5 % ait un sens : sous cinquante
    # entrées la garde s'abstient volontairement, un petit fichier bougeant trop.
    riche = {"NVDA": 228.0, "MSFT": 513.0, "AAPL": 319.0}
    riche.update({"T%03d" % i: 10.0 + i for i in range(97)})       # 100 cours

    pauvre = dict(riche)
    pauvre["NVDA"] = None                                          # le témoin nommé
    for i in range(20):
        pauvre["T%03d" % i] = None                                 # 79 cours restants

    p_riche = os.path.join(base, "tradfi_fundamentals_cache_riche.js")
    p_pauvre = os.path.join(base, "tradfi_fundamentals_cache_pauvre.js")
    p_hors = os.path.join(base, "un_cache_inconnu.js")
    cache(p_riche, riche)
    cache(p_pauvre, pauvre)
    cache(p_hors, pauvre)

    print("LECTURE DES TÉMOINS")
    t_riche = run_jobs._temoins_de(p_riche)
    t_pauvre = run_jobs._temoins_de(p_pauvre)
    verifie("la version riche se lit", t_riche is not None, True)
    verifie("elle compte ses cours", t_riche.get("_compte:cours"), 100)
    verifie("elle voit le témoin NVDA", "cours NVDA" in t_riche, True)
    verifie("la version pauvre a perdu NVDA", "cours NVDA" in t_pauvre, False)

    print()
    print("LE CAS RÉEL — publier la version pauvre par-dessus la riche")
    perdus = run_jobs._temoins_perdus(p_pauvre, p_riche)
    verifie("elle REFUSE", bool(perdus), True)
    verifie("elle nomme le témoin disparu",
            any("NVDA" in x for x in perdus), True)
    verifie("elle chiffre l'effondrement",
            any("100" in x and "79" in x for x in perdus), True)

    print()
    print("CONTRE-ÉPREUVES — ce qui ne doit PAS être refusé")
    verifie("une version identique passe",
            bool(run_jobs._temoins_perdus(p_riche, p_riche)), False)
    verifie("une version PLUS RICHE passe",
            bool(run_jobs._temoins_perdus(p_riche, p_pauvre)), False)
    verifie("un fichier hors table passe",
            bool(run_jobs._temoins_perdus(p_hors, p_riche)), False)
    verifie("un fichier illisible s'abstient",
            run_jobs._temoins_de(os.path.join(base, "absent.js")), None)

    print()
    print("LE SEUIL — le bruit passe, l'effondrement non")
    petite = dict(riche)
    for i in range(3):
        petite["T%03d" % i] = None                                 # 97/100, −3 %
    p_petite = os.path.join(base, "tradfi_fundamentals_cache_petite.js")
    cache(p_petite, petite)
    verifie("une perte de 3 % passe",
            bool(run_jobs._temoins_perdus(p_petite, p_riche)), False)

    grosse = dict(riche)
    for i in range(10):
        grosse["T%03d" % i] = None                                 # 90/100, −10 %
    p_grosse = os.path.join(base, "tradfi_fundamentals_cache_grosse.js")
    cache(p_grosse, grosse)
    verifie("une perte de 10 % est refusée",
            bool(run_jobs._temoins_perdus(p_grosse, p_riche)), True)

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
