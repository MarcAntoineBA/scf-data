#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Déduit le total des dettes manquant des paquets internationaux, sans réseau.

POURQUOI

Beaucoup de sociétés n'étiquettent jamais un total de passif : leur bilan détaille
les postes sans en donner la somme. Mesuré le 28/08/2026 : 1 772 sociétés
internationales sur 19 430 — 9,1 % — sortaient sans total des dettes, dont 1 757
dont l'actif ET les capitaux propres étaient pourtant déposés.

Le coût était silencieux. Sans total des dettes, le Z d'Altman perd son quatrième
terme, « capitalisation sur dettes », qui vaut à lui seul 1,4 point en médiane.
Or le Z était publié quand même, amputé d'un cinquième de sa formule, puis comparé
aux seuils d'Altman — 1,81 « détresse », 2,99 « sûre » — comme s'il était complet.
Les cinq termes étant positifs pour une société saine, l'amputation ne se trompe
que dans un sens : celui qui accuse.

CE QU'IL FAIT

Actif = dettes + capitaux propres + intérêts minoritaires + capitaux mezzanine
est une IDENTITÉ comptable. On la retourne. Ce n'est pas une estimation : c'est
une soustraction entre deux chiffres déposés par la société elle-même, et le
résultat est marqué `liabilities_reconstruit` pour rester distinguable.

Puis le Z d'Altman est recalculé — par la fonction du collecteur, importée telle
quelle, pour qu'il n'existe jamais deux formules qui pourraient diverger.

POURQUOI PAS UNE COLLECTE

Une collecte internationale complète demande des heures et des dizaines de
milliers de requêtes pour rapporter des chiffres qu'on a déjà. Le collecteur porte
désormais la même déduction pour ses passages futurs ; ce script rattrape le parc
existant. Aucune donnée n'est inventée, seulement une soustraction posée.
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fondamentaux_communs import _altman_z                # noqa: E402

CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")
SITE = os.path.expanduser("~/Site_Crypto_Finance/data")


def main():
    paquets = sorted(glob.glob(os.path.join(CACHE, "intl_detail_*.json")))
    if not paquets:
        print("[fatal] aucun paquet intl_detail_*.json", file=sys.stderr)
        return 2

    total = deduites = z_gagnes = z_perdus = z_changes = 0
    bascules = []
    for f in paquets:
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        change = False
        for sym, v in (d.get("societes") or {}).items():
            ex = v.get("exercices") or []
            r = v.get("resume")
            if not ex or not isinstance(r, dict):
                continue
            total += 1
            touche = False
            for e in ex:
                if (e.get("liabilities") is None and e.get("assets") is not None
                        and e.get("equity") is not None):
                    e["liabilities"] = (e["assets"] - e["equity"]
                                        - (e.get("interets_minoritaires_bilan") or 0.0)
                                        - (e.get("capitaux_mezzanine") or 0.0))
                    e["liabilities_reconstruit"] = True
                    touche = True
            if not touche:
                continue
            deduites += 1
            avant = r.get("altman_z")
            z, detail = _altman_z(ex[-1], ex[-1].get("mcap_estime"))
            r["altman_z"] = z
            r["altman_detail"] = detail
            change = True
            a_avant = isinstance(avant, (int, float))
            a_apres = isinstance(z, (int, float))
            if a_apres and not a_avant:
                z_gagnes += 1
            elif a_avant and not a_apres:
                z_perdus += 1
            elif a_avant and a_apres and abs(z - avant) > 0.01:
                z_changes += 1
                # Un changement de CAMP est le seul qui change ce que le lecteur
                # comprend : 1,81 et 2,99 sont les deux frontières d'Altman.
                if (avant < 2.99) != (z < 2.99) or (avant < 1.81) != (z < 1.81):
                    if len(bascules) < 10:
                        bascules.append((sym, avant, z))
        if change:
            texte = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(texte)
            if os.path.isdir(SITE):
                with open(os.path.join(SITE, os.path.basename(f)), "w",
                          encoding="utf-8") as fh:
                    fh.write(texte)

    print("%d sociétés internationales examinées" % total)
    print("   %d dont au moins un exercice a retrouvé son total de dettes" % deduites)
    print("   Z d'Altman : %d apparus, %d disparus, %d modifiés"
          % (z_gagnes, z_perdus, z_changes))
    if bascules:
        print()
        print("   dix verdicts qui changent de camp :")
        for s, a, b in bascules:
            print("      %-12s %5.2f → %5.2f" % (s, a, b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
