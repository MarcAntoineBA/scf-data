#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_altman_devise.py — Garde-fou : pas de Z d'Altman quand la capitalisation et
le bilan ne sont pas dans la même monnaie.

CE QU'IL EMPÊCHE DE REVENIR
Le Z d'Altman met en rapport la capitalisation et le passif :

    0,6 × capitalisation / dettes

La filière SEC passait `mcap_usd` — en DOLLARS — alors que `liabilities` est
dans la devise de DÉPÔT. Pour une société qui dépose en wons, en pesos ou en
livres, ce terme est multiplié par le taux de change.

Le biais suit le TAUX, et il va dans les DEUX sens. Quand le taux dépasse 1 —
won, yen, peso — le terme est trop petit et le Z trop bas : la fiche accuse.
Quand il est inférieur à 1 — la livre — le Z est au contraire gonflé et la fiche
innocente. Un chiffre faux reste faux dans les deux sens.

Mesuré sur la production le 30/08/2026 : 76 des 163 sociétés SEC déposant hors
dollar publiaient un Z faux —

    LPL   KRW  Z = 0,71      EMA  CAD  Z = 0,62
    NOA   CAD  Z = 1,17      CPAC PEN  Z = 1,56

Sous 1,81, Altman dit « zone de détresse ». Ces quatre-là y étaient mises par une
division mal posée, pas par leur bilan.

⚠ Le Z ne s'effondre pas à zéro : le terme de capitalisation ne pèse que 2,4
points sur un Z de 4,36. Il SE DÉPLACE — ce qui suffit, les seuils étant à 1,81
et 2,99. J'avais d'abord écrit « il s'effondre sous 1,81 » et attendu 3,16 ; la
mesure a rendu 1,96 et corrigé les deux.

POURQUOI ON REFUSE AU LIEU DE CONVERTIR
La filière SEC ne porte aucune table de taux de change : elle a été écrite pour
des déposants américains. Convertir demanderait d'y amener une source de taux,
de la dater et de la faire vivre. En attendant, on ne publie pas un score qu'on
ne sait pas calculer — mais on publie les quatre termes qui ne dépendent pas de
la capitalisation, pour que la fiche montre ce qu'elle sait.

⚠ `_altman_z(cur, None)` rend un détail VIDE : il sort avant tout calcul. Les
quatre termes sont donc recalculés explicitement. Ce test le vérifie, parce que
c'est exactement le genre de promesse qu'on croit tenir sans l'avoir mesurée.

Lancement : python3 tools/test_altman_devise.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fondamentaux_communs import _altman_z  # noqa: E402

échecs = []


def verifie(quoi, obtenu, attendu):
    ok = obtenu == attendu
    print("  %-52s %-14s %s" % (quoi, obtenu, "✓" if ok else "✗ attendu " + str(attendu)))
    if not ok:
        échecs.append(quoi)


# Un bilan sain, volontairement au-dessus du seuil de « zone sûre » (2,99).
BILAN = {
    "assets": 1000.0, "assets_current": 400.0, "liabilities_current": 200.0,
    "liabilities": 500.0, "operating_income": 120.0,
    "retained_earnings": 300.0, "revenue": 900.0,
}


def main():
    print("LA FORMULE ELLE-MÊME")
    z_bon, det = _altman_z(BILAN, 2000.0)
    verifie("un bilan sain, capitalisation cohérente → Z", z_bon, 4.36)
    verifie("les cinq termes sont présents",
            sum(1 for v in det.values() if v is not None), 5)

    print()
    print("LE DÉFAUT — ce qui se passait quand les devises divergent")
    # Le terme de capitalisation vaut 0,6 × capi / dettes : il pèse 2,4 points
    # sur ce bilan. Le Z ne s'effondre donc pas à zéro, il SE DÉPLACE — et c'est
    # bien assez pour changer de verdict, les seuils étant à 1,81 et 2,99.
    for dev, taux, attendu in (("KRW", 1380, 1.96), ("JPY", 157, 1.97),
                               ("PEN", 3.75, 2.6), ("CAD", 1.37, 3.71),
                               ("GBP", 0.79, 4.99)):
        z, _ = _altman_z(BILAN, 2000.0 / taux)
        verifie("passif en %s, capitalisation en USD -> Z" % dev, z, attendu)

    # ⚠ Ce qui compte n'est pas la taille de l'écart, c'est le CHANGEMENT DE
    # VERDICT : le même bilan passe de « zone sûre » à « zone grise ».
    z_krw, _ = _altman_z(BILAN, 2000.0 / 1380)
    verifie("  KRW : « zone sûre » devient « zone grise »",
            (z_bon > 2.99) and (1.81 < z_krw < 2.99), True)

    # ⚠ Et le biais ne va PAS toujours dans le même sens. Les quatre exemples
    # relevés en production partageaient un taux supérieur à 1 ; pour la livre,
    # dont le taux est inférieur à 1, le Z est GONFLÉ — la fiche innocente au
    # lieu d'accuser. Un chiffre faux reste faux dans les deux sens.
    z_gbp, _ = _altman_z(BILAN, 2000.0 / 0.79)
    verifie("  GBP : le biais joue dans l'AUTRE sens (Z gonflé)",
            z_gbp > z_bon, True)

    print()
    print("LA CORRECTION — aucun score, mais les termes connus")
    z_none, det_none = _altman_z(BILAN, None)
    verifie("sans capitalisation, _altman_z ne rend PAS de score", z_none, None)
    verifie("et son détail est VIDE — d'où le recalcul explicite",
            det_none, {})

    # Ce que la filière SEC recalcule désormais elle-même.
    A = BILAN["assets"]
    fr = BILAN["assets_current"] - BILAN["liabilities_current"]
    recalcule = {
        "fonds_de_roulement": round(1.2 * fr / A, 3),
        "reserves": round(1.4 * BILAN["retained_earnings"] / A, 3),
        "resultat_exploitation": round(3.3 * BILAN["operating_income"] / A, 3),
        "capitalisation_sur_dettes": None,
        "rotation": round(1.0 * BILAN["revenue"] / A, 3),
    }
    verifie("les quatre termes indépendants sont calculables",
            sum(1 for v in recalcule.values() if v is not None), 4)
    verifie("  et ils valent ceux de la formule complète",
            all(recalcule[k] == det[k] for k in recalcule if recalcule[k] is not None),
            True)
    verifie("  seul le terme de capitalisation manque",
            recalcule["capitalisation_sur_dettes"], None)

    print()
    print("LE COLLECTEUR — la branche est bien posée")
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "scripts", "fetch_sec_fundamentals.py")
    code = open(src, encoding="utf-8").read()
    verifie("il teste la devise avant de scorer",
            'if (devise or "USD") == "USD":' in code, True)
    verifie("il ne passe plus mcap_usd sans condition",
            code.count("_altman_z(exercices[-1], mcap_usd)"), 1)
    verifie("il recalcule le détail dans l'autre branche",
            '"capitalisation_sur_dettes": None,   # devise non convertible ici' in code,
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
