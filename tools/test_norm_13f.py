#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_norm_13f.py — Garde-fou : le rapprochement d'un émetteur 13F avec un ticker
de l'univers ne doit pas être cassé par la normalisation des noms.

CE QU'IL EMPÊCHE DE REVENIR
Un dépôt 13F désigne une LIGNE DE TITRE, pas une société : « ALPHABET INC (CAP
STK CL A) », « TAIWAN SEMICONDUCTOR MANUFAC (SPONSORED ADS) ». L'univers, lui,
nomme la société : « Alphabet Inc. ». Le rapprochement se fait par raison sociale
normalisée — et la normalisation avait deux défauts.

⚠ 1. LE RETRAIT DES SUFFIXES MANGEAIT L'INTÉRIEUR DES MOTS

       s.replace(" CORP", " ")   sur « NVIDIA CORPORATION »
                                 donnait « NVIDIA ORATION »

    « CORPORATION » contient « CORP », et `str.replace` ne connaît pas les
    frontières de mots. Même chose pour « CO » dans « COM » : « LAM RESEARCH
    CORP (COM NEW) » devenait « LAM RESEARCH M NEW ». Ces noms ne pouvaient plus
    correspondre à rien, et le rapprochement échouait EN SILENCE.

⚠ 2. LA CATÉGORIE DE TITRE RESTAIT ATTACHÉE

    « ALPHABET INC (CAP STK CL A) » → « ALPHABET CAP STK », qui ne rencontre
    jamais « ALPHABET ».

MESURÉ contre les 1 296 sociétés de l'univers, sur les 60 lignes d'encombrement :

    ancienne normalisation   21 / 60 rapprochées   (35 %)
    NOUVELLE                 33 / 60               (55 %)

    +12 sociétés, dont Alphabet (18 gérants), NVIDIA (12), Lam Research (8),
    Costco, Goldman Sachs, Snowflake, Intuitive Surgical.

Le ticker est la CLÉ qui relie une ligne 13F à une fiche société : sans lui, une
liste « portées par les gérants » ne serait cliquable qu'à un tiers.

⚠ CE QUI RESTE UNE LIMITE, ET QUI N'EST PAS UN DÉFAUT
45 % restent non rapprochés. Ce sont pour l'essentiel des ETF (SPDR S&P 500), des
sociétés hors univers (SpaceX, non cotée) et des émetteurs nommés autrement. Le
collecteur le documente déjà dans ses `lacunes`. Normaliser plus fort ne les
trouverait pas — cela créerait des faux rapprochements, ce qui est pire.

Lancement : python3 tools/test_norm_13f.py
"""

import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(RACINE, "scripts", "fetch_13f.py")

échecs = []


def verifie(quoi, obtenu, attendu):
    ok = obtenu == attendu
    print("  %-50s %-24s %s" % (quoi, obtenu, "✓" if ok else "✗ attendu " + str(attendu)))
    if not ok:
        échecs.append(quoi)


def charger_norm():
    """On extrait `_norm` du collecteur sans importer le module : il parle au
    réseau au chargement, et un test ne doit pas déposer de requête."""
    src = open(SRC, encoding="utf-8").read()
    i = src.find("_SUFFIXES_13F")
    j = src.find("def main()")
    assert i > 0 and j > i, "bornes de _norm introuvables"
    ns = {"re": re}
    exec(src[i:j], ns)
    return ns["_norm"]


def main():
    norm = charger_norm()

    print("LE BUG DES MOTS ENTIERS — le plus discret des deux")
    verifie("NVIDIA CORPORATION", norm("NVIDIA CORPORATION"), "NVIDIA")
    verifie("  (et non « NVIDIA ORATION »)",
            norm("NVIDIA CORPORATION") != "NVIDIA ORATION", True)
    verifie("LAM RESEARCH CORP (COM NEW)",
            norm("LAM RESEARCH CORP (COM NEW)"), "LAM RESEARCH")
    verifie("  (et non « LAM RESEARCH M NEW »)",
            norm("LAM RESEARCH CORP (COM NEW)") != "LAM RESEARCH M NEW", True)

    print()
    print("LA CATÉGORIE DE TITRE — un 13F nomme un titre, pas une société")
    verifie("ALPHABET INC (CAP STK CL A)",
            norm("ALPHABET INC (CAP STK CL A)"), "ALPHABET")
    verifie("ALPHABET INC (CAP STK CL C)",
            norm("ALPHABET INC (CAP STK CL C)"), "ALPHABET")
    verifie("  les deux classes se rejoignent",
            norm("ALPHABET INC (CAP STK CL A)") == norm("ALPHABET INC (CAP STK CL C)"),
            True)

    print()
    print("LES DEUX CÔTÉS DOIVENT SE RENCONTRER")
    for cote_13f, cote_univers in (
            ("NVIDIA CORPORATION", "NVIDIA Corporation"),
            ("ALPHABET INC (CAP STK CL A)", "Alphabet Inc."),
            ("COSTCO WHOLESALE CORPORATION", "Costco Wholesale"),
            ("GOLDMAN SACHS GROUP INC", "Goldman Sachs"),
            ("INTUITIVE SURGICAL INC (COM NEW)", "Intuitive Surgical"),
            ("SNOWFLAKE INC (COM SHS)", "Snowflake")):
        verifie("« %s »" % cote_13f[:40], norm(cote_13f), norm(cote_univers))

    print()
    print("CONTRE-ÉPREUVES — la normalisation ne doit pas TOUT écraser")
    # Deux sociétés distinctes ne doivent pas se confondre : c'est le risque
    # d'une normalisation trop agressive, et un faux rapprochement est pire
    # qu'une absence de rapprochement.
    verifie("Apple ≠ Applied Materials",
            norm("APPLE INC") != norm("APPLIED MATLS INC"), True)
    verifie("Bank of America ≠ Bank of NY Mellon",
            norm("BANK OF AMERICA CORP") != norm("BANK OF NEW YORK MELLON CORP"), True)
    verifie("un nom vide reste vide", norm(""), "")
    verifie("un nom absent ne casse pas", norm(None), "")

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
