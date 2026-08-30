#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_roe_minoritaires.py — Garde-fou : le ROE ne rapporte pas un résultat part du
groupe à des capitaux qui contiennent les intérêts minoritaires.

CE QU'IL EMPÊCHE DE REVENIR
`net_income` est le résultat **part du groupe** — le fichier porte
`net_income_total` à côté, preuve que la distinction est faite. Mais `equity`
inclut les **intérêts minoritaires** quand la source ne sert pas
`totalCommonEquity`.

Le ROE divisait donc un numérateur net de minoritaires par un dénominateur qui
les contient : sous-estimé, d'autant plus que la filiale non détenue est grosse.

Mesuré le 30/08/2026 sur cinq paquets de détail :

    228 derniers exercices examinés
    134 portent des intérêts minoritaires
     47 calculaient le ROE sur les capitaux TOTAUX   ← 35 %

    3994.T      ROE  3,91 %  au lieu de  4,99 %   (minoritaires 27,4 % du bilan)
    000619.SZ   ROE −3,36 %  au lieu de −4,23 %   (minoritaires 19,4 %)
    1304.SR     ROE  9,86 %  au lieu de 11,40 %   (minoritaires 15,8 %)

⚠ LES DEUX PIÈGES QUE CE TEST VERROUILLE

1. **Ne pas retrancher deux fois.** Quand `equity_part_groupe` existe, `equity`
   EST déjà la part du groupe. Retrancher encore compterait les minoritaires
   deux fois — le dépôt a déjà payé cette erreur sur la reconstruction du passif,
   et la garde qu'il a écrite alors est réutilisée telle quelle.

2. **Ne pas mélanger un solde et une moyenne.** Les capitaux propres du ROE sont
   une MOYENNE de deux exercices. Retrancher les minoritaires d'une seule année
   creuserait un écart artificiel de la taille de leur variation annuelle.

⚠ Le ROIC et le ROCE ne doivent PAS être touchés : ils rapportent un résultat
d'exploitation, AVANT répartition entre groupe et minoritaires, à un capital
investi qui doit rester total. Seul le ROE mélange deux périmètres.

Lancement : python3 tools/test_roe_minoritaires.py
"""

import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(RACINE, "scripts", "fetch_intl_fundamentals.py")

échecs = []


def verifie(quoi, obtenu, attendu):
    ok = obtenu == attendu
    print("  %-54s %-12s %s" % (quoi, obtenu, "✓" if ok else "✗ attendu " + str(attendu)))
    if not ok:
        échecs.append(quoi)


def moy(serie, i):
    """La même moyenne glissante que le collecteur."""
    cur = serie[i]
    if cur is None:
        return None
    if i == 0:
        return cur
    prev = serie[i - 1]
    return cur if prev is None else (cur + prev) / 2.0


def roe(net_income, equity_serie, mi_serie, i, equity_part_groupe=None):
    """La logique du collecteur, rejouée hors de lui."""
    cp = moy(equity_serie, i)
    mi = moy(mi_serie, i)
    if equity_part_groupe is not None or not mi:
        mi = 0.0
    cpg = (cp - mi) if (cp and mi) else cp
    return round(100.0 * net_income / cpg, 2) if (cpg and cpg > 0) else None


def main():
    print("LE CAS RÉEL — 3994.T, minoritaires à 27 % du bilan")
    # equity total 1000 -> dont 274 de minoritaires ; resultat part du groupe 36
    eq = [1000.0, 1000.0]
    mi = [274.0, 274.0]
    verifie("ROE sur capitaux TOTAUX (l'ancien, faux)",
            round(100 * 36.0 / 1000.0, 2), 3.6)
    verifie("ROE sur capitaux PART DU GROUPE (le juste)",
            roe(36.0, eq, mi, 1), 4.96)

    print()
    print("PIÈGE 1 — ne pas retrancher deux fois")
    # equity vaut DEJA la part du groupe : equity_part_groupe est renseigne.
    verifie("equity déjà part du groupe → aucun retranchement",
            roe(36.0, [726.0, 726.0], [274.0, 274.0], 1, equity_part_groupe=726.0),
            4.96)
    verifie("  (et non 36/(726−274) = 7,96 %, qui compterait deux fois)",
            roe(36.0, [726.0, 726.0], [274.0, 274.0], 1, equity_part_groupe=726.0) != 7.96,
            True)

    print()
    print("PIÈGE 2 — moyenne contre solde de clôture")
    # Les minoritaires passent de 200 a 300 : la moyenne vaut 250.
    eq2 = [900.0, 1100.0]     # moyenne 1000
    mi2 = [200.0, 300.0]      # moyenne 250
    verifie("les deux termes sont moyennés", roe(40.0, eq2, mi2, 1), 5.33)
    verifie("  et non le solde de clôture (40/(1000−300) = 5,71)",
            roe(40.0, eq2, mi2, 1) != 5.71, True)

    print()
    print("CONTRE-ÉPREUVES — ce qui ne doit PAS changer")
    verifie("sans minoritaires, le ROE est inchangé",
            roe(50.0, [1000.0, 1000.0], [None, None], 1), 5.0)
    verifie("minoritaires à zéro, inchangé",
            roe(50.0, [1000.0, 1000.0], [0.0, 0.0], 1), 5.0)
    verifie("capitaux négatifs → pas de ROE",
            roe(50.0, [-100.0, -100.0], [10.0, 10.0], 1), None)
    verifie("minoritaires plus grands que les capitaux → pas de ROE",
            roe(50.0, [100.0, 100.0], [200.0, 200.0], 1), None)

    print()
    print("LE COLLECTEUR — la correction est bien posée")
    code = open(SRC, encoding="utf-8").read()
    verifie("cp_groupe est calculé", "cp_groupe" in code, True)
    verifie("les minoritaires passent par _moy",
            '_moy("interets_minoritaires_bilan", i)' in code, True)
    verifie("la garde equity_part_groupe est reprise",
            'e.get("equity_part_groupe") is not None or not _mi' in code, True)
    verifie("le ROIC n'est PAS touché",
            'e["roic"] = _pct(e["nopat"], ci)' in code, True)
    verifie("le ROCE n'est PAS touché",
            'e["roce"] = _pct(e["operating_income"], ce)' in code, True)

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
