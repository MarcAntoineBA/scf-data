#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_cours_devise_locale.py — Garde-fou : écarter les MULTIPLES d'un déposant en
monnaie locale n'autorise pas à lui retirer son COURS.

CE QU'IL EMPÊCHE DE REVENIR
Quand une société déposant à la SEC publie ses états en devise locale, tout ce
qui met en rapport une capitalisation en dollars et un bilan en monnaie locale
est faux. Le collecteur les écarte — `mcap_estime`, `wacc`, `ecart_roic_wacc` —
et c'est le bon choix : mieux vaut une case vide qu'un multiple faux.

Mais l'écartement emportait aussi `cours_natif`, qui n'y était pour rien. **Un
cours n'est pas un rapport** : il est libellé dans sa propre devise de cotation,
il ne se mélange à rien, et il ne devient faux dans aucune monnaie.

Mesuré sur la production le 30/08/2026 :

    163 déposants SEC hors dollar
    163 d'entre eux SANS AUCUN COURS   ← cent pour cent

British American Tobacco, LG Display, Sibanye, Ambev, Banco Santander Brasil
n'affichaient aucun cours, alors que le jeu de marché les sert : BTI 56,13 $,
LPL 3,35 $, SBSW 12,09 $, HMY 20,23 $.

⚠ OÙ SE TROUVAIT LE VERROU
Pas dans le bloc qui pose `cours_natif = None` — celui-là est sans effet, puisque
le cours est réécrit plus bas. Le vrai verrou était une branche qui SAUTAIT tout
le renseignement du cours quand `devises_alignees` valait faux. Chercher le
symptôme là où il s'écrit plutôt que là où il se décide fait perdre une heure.

⚠ CE TEST VÉRIFIE AUSSI L'INTÉGRITÉ DU BLOC
Une première version du correctif remplaçait en prime un commentaire d'en-tête,
et sa découpe par bornes flottantes a emporté les deux lignes qui appellent
`_dernier_cours` : le cours n'était plus renseigné DU TOUT. D'où les contrôles
de présence ci-dessous.

Lancement : python3 tools/test_cours_devise_locale.py
"""

import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(RACINE, "scripts", "fetch_sec_fundamentals.py")

échecs = []


def verifie(quoi, obtenu, attendu):
    ok = obtenu == attendu
    print("  %-56s %-10s %s" % (quoi, obtenu, "✓" if ok else "✗ attendu " + str(attendu)))
    if not ok:
        échecs.append(quoi)


def main():
    code = open(SRC, encoding="utf-8").read()

    print("LE VERROU A SAUTÉ")
    verifie("la branche qui sautait le cours a disparu",
            'if bati["resume"].get("devises_alignees") is False:\n'
            '            bati["resume"]["cours_source"] = None' in code, False)
    verifie("le cours est renseigné sans condition",
            "LE COURS EST RENSEIGNÉ DANS TOUS LES CAS" in code, True)

    print()
    print("L'INTÉGRITÉ DU BLOC — rien n'a été emporté par la découpe")
    verifie("l'appel à _dernier_cours est présent",
            code.count("_dernier_cours(cours.get(sym))"), 1)
    verifie("le repli sur la cotation de l'univers est présent",
            code.count('bati["resume"]["cours_source"] = "univers"'), 1)
    verifie("la source « tracker » est toujours posée",
            '"tracker" if bati["resume"]["cours_natif"] is not None else None' in code,
            True)
    verifie("cours_source reste écrit (4 sites)", code.count("cours_source"), 4)

    print()
    print("CE QUI DOIT RESTER ÉCARTÉ — on n'a pas ouvert trop grand")
    verifie("mcap_estime est toujours écarté",
            'e["mcap_estime"] = None' in code, True)
    verifie("wacc est toujours écarté", 'e["wacc"] = None' in code, True)
    verifie("ecart_roic_wacc est toujours écarté",
            'e["ecart_roic_wacc"] = None' in code, True)
    verifie("le drapeau montants_marche reste posé",
            'resume["montants_marche"] = "ecartes"' in code, True)
    verifie("devises_alignees reste posé à faux",
            'resume["devises_alignees"] = False' in code, True)

    print()
    print("AUCUNE CONVERSION N'A ÉTÉ INTRODUITE")
    # La filière SEC ne porte pas de table de taux : en introduire une en
    # douce serait pire que le défaut d'origine.
    verifie("pas de _en_devise_etats côté SEC",
            "_en_devise_etats" in code, False)

    print()
    print("LE CORRECTIF ALTMAN TIENT TOUJOURS")
    verifie("la devise est testée avant de scorer",
            'if (devise or "USD") == "USD":' in code, True)

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
