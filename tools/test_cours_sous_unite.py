#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_cours_sous_unite.py — Garde-fou : un cours de Johannesburg, Londres ou
Tel-Aviv ne reste pas en centimes quand la référence manque.

CE QU'IL EMPÊCHE DE REVENIR
Trois places cotent en SOUS-UNITÉ : Johannesburg en centimes de rand, Londres en
pence, Tel-Aviv en agorot. Le collecteur corrige déjà — mais seulement s'il a une
RÉFÉRENCE, `mcap / sharesOut`, prise dans l'univers.

Quand l'univers ne porte pas ces deux colonnes, la référence est absente et le
cours reste en centimes **sans que rien ne le signale**.

Mesuré le 30/08/2026 sur cinq paquets de détail :

    12 fiches sur une place à sous-unité
     5 avec un cours au-delà de mille   ← 42 %

    FSR.JO   9 613  pour   96,13 R      SHP.JO  30 700  pour 307,00 R
    MTN.JO  18 830  pour  188,30 R      STJ.L    1 176  pour  11,77 £
    DANE.TA 42 500  pour  425,00 ₪

Confronté au marché : FirstRand cote ~96 R, MTN ~188 R, Shoprite ~307 R. Et sur
la MÊME place, Standard Bank était déjà corrigée à 320,77 R parce qu'elle avait
une référence — deux fiches justes et trois fausses d'un facteur cent dans le
même tableau.

⚠ CE QUE CE TEST PROTÈGE AUTANT QUE LA CORRECTION
Le commentaire du collecteur dit : « On ne se fie pas à une liste de places : une
première mesure par place donnait Londres à 1,00, sa médiane étant diluée par les
cotations secondaires étrangères. C'est l'invariant qui tranche. »

Cette décision reste VRAIE. Le repli par place n'agit QUE là où l'invariant
n'existe pas. Les contre-épreuves ci-dessous vérifient qu'il ne déborde pas :
un cours déjà en unité, un cours en dollars, un cours en euros à 1 850 ne
bougent pas.

Lancement : python3 tools/test_cours_sous_unite.py
"""

import os
import re
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(RACINE, "scripts", "fetch_intl_fundamentals.py")

échecs = []


def verifie(quoi, obtenu, attendu):
    ok = obtenu == attendu
    print("  %-50s %-12s %s" % (quoi, obtenu, "✓" if ok else "✗ attendu " + str(attendu)))
    if not ok:
        échecs.append(quoi)


# La même logique que celle du collecteur, rejouée hors de lui : ce fichier est
# un script de 2 000 lignes qui parle au réseau, on n'en importe pas la fonction.
PLACES_SOUS_UNITE = {
    "ZAR": ("centimes de rand", 1500.0),
    "GBP": ("pence", 1000.0),
    "ILS": ("agorot", 1500.0),
}


def cours_sous_unite(px, ref, devise=None):
    if not (isinstance(px, (int, float)) and px > 0):
        return px, False
    if isinstance(ref, (int, float)) and ref > 0:
        r = px / ref
        return (px / 100.0, True) if 50.0 <= r <= 200.0 else (px, False)
    conv = PLACES_SOUS_UNITE.get((devise or "").upper())
    if conv and px > conv[1]:
        return px / 100.0, True
    return px, False


def main():
    print("LES CINQ CAS RÉELS — mesurés en production")
    for sym, px, dev, attendu in (
            ("FSR.JO", 9613.0, "ZAR", 96.13),
            ("MTN.JO", 18830.0, "ZAR", 188.30),
            ("SHP.JO", 30700.0, "ZAR", 307.00),
            ("STJ.L", 1176.5, "GBP", 11.765),
            ("DANE.TA", 42500.0, "ILS", 425.00)):
        v, _ = cours_sous_unite(px, None, dev)
        verifie("%s %.0f -> unite" % (sym, px), round(v, 3), round(attendu, 3))

    print()
    print("LA RÉFÉRENCE GARDE LA PRIORITÉ — elle mesure, la place suppose")
    v, c = cours_sous_unite(31915.0, 320.77, "ZAR")
    verifie("SBK.JO corrigé par sa référence", round(v, 2), 319.15)
    verifie("  et le drapeau dit que ça a corrigé", c, True)

    print()
    print("CONTRE-ÉPREUVES — ce qui ne doit PAS bouger")
    for lib, px, ref, dev, att, doit in (
            ("cours déjà en rands", 320.77, None, "ZAR", 320.77, False),
            ("cours en dollars", 205.63, None, "USD", 205.63, False),
            ("cours en euros élevé", 1850.0, None, "EUR", 1850.0, False),
            ("petit cours à Londres", 11.77, None, "GBP", 11.77, False),
            ("cours nul", 0, None, "ZAR", 0, False),
            ("cours absent", None, None, "ZAR", None, False)):
        v, c = cours_sous_unite(px, ref, dev)
        verifie(lib, (v, c), (att, doit))

    print()
    print("LE SEUIL — juste au-dessus et juste en dessous")
    v1, c1 = cours_sous_unite(1499.0, None, "ZAR")
    verifie("1 499 ZAR reste (sous le seuil)", c1, False)
    v2, c2 = cours_sous_unite(1501.0, None, "ZAR")
    verifie("1 501 ZAR est corrigé", (round(v2, 2), c2), (15.01, True))

    print()
    print("LE COLLECTEUR — la correction est bien posée")
    code = open(SRC, encoding="utf-8").read()
    verifie("la table des places existe", "PLACES_SOUS_UNITE" in code, True)
    verifie("la devise est passée à la fonction",
            "_cours_sous_unite(px, cours_ref.get(sym), dev)" in code, True)
    verifie("l'invariant reste le chemin prioritaire",
            bool(re.search(r"if isinstance\(ref, \(int, float\)\) and ref > 0:", code)),
            True)

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
