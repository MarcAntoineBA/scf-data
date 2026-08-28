#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rattrape sur les paquets internationaux ce qui se corrige SANS réseau.

POURQUOI CE SCRIPT, ET POURQUOI PAS UNE COLLECTE

Les collecteurs portent désormais six correctifs. Côté américain, une collecte
complète de cent minutes les applique aux 3 856 sociétés d'un coup. Côté
international, l'univers fait 19 495 sociétés et la source ne se parcourt qu'en
sept jours, une tranche par jour : les correctifs mettraient une semaine à couvrir
le parc, et pendant cette semaine la fiche montrerait deux définitions du même
chiffre selon le jour où la société a été collectée.

Deux des correctifs ne demandent AUCUNE donnée nouvelle. Ils se rejouent sur ce
qui est déjà écrit :

  1. LE TOTAL DES DETTES. Actif = dettes + capitaux propres + intérêts
     minoritaires + capitaux mezzanine est une identité comptable. Mesuré le
     28/08/2026 : 1 772 sociétés sur 19 430 (9,1 %) sortaient sans total de
     passif, dont 1 757 dont l'actif ET les capitaux propres étaient déposés.
     On retourne l'identité. Ce n'est pas une estimation, c'est une soustraction
     entre deux chiffres déposés par la société elle-même.

  2. LE Z D'ALTMAN. Il était publié avec quatre termes sur cinq, puis comparé aux
     seuils d'Altman — 1,81 « détresse », 2,99 « sûre » — comme s'il était
     complet. Les cinq termes étant positifs pour une société saine, l'amputation
     ne se trompe que dans un sens : celui qui accuse. Mesuré : 1 557 fiches
     internationales dans ce cas, deux fois plus que côté américain.
     La règle est désormais « les cinq termes ou rien », et on la rejoue ici.

CE QU'ON NE RATTRAPE PAS ICI, ET POURQUOI

Les quatre autres correctifs — nombre d'actions à la mauvaise échelle, valeurs
logiquement impossibles — modifient des chiffres de BASE dont dépendent ensuite
les marges, les rendements et les croissances. Les rejouer à moitié laisserait un
paquet incohérent : une marge brute calculée sur un chiffre d'affaires effacé.
Ceux-là attendent la collecte, qui recalcule tout dans le bon ordre. Ils
concernent 92 exercices sur 96 170 — 0,1 % — contre 1 557 fiches pour l'Altman.
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fondamentaux_communs import _altman_z                # noqa: E402

CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")
SITE = os.path.expanduser("~/Site_Crypto_Finance/data")
TERMES = ("fonds_de_roulement", "reserves", "resultat_exploitation",
          "capitalisation_sur_dettes", "rotation")


def main():
    paquets = [f for f in sorted(glob.glob(os.path.join(CACHE, "intl_detail_*.json")))
               if "sync-conflict" not in f]
    if not paquets:
        print("[fatal] aucun paquet intl_detail_*.json", file=sys.stderr)
        return 2

    total = dettes = z_nes = z_retires = z_bouges = 0
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
            if touche:
                dettes += 1

            # Le Z se rejoue pour TOUTES les sociétés, pas seulement celles dont
            # les dettes viennent de changer : les 1 557 fiches amputées le sont
            # le plus souvent par un autre terme, et la règle « cinq ou rien »
            # n'était appliquée nulle part sur ce parc.
            avant = r.get("altman_z")
            z, detail = _altman_z(ex[-1], ex[-1].get("mcap_estime"))
            if z == avant and (r.get("altman_detail") or {}) == detail:
                if touche:
                    change = True
                continue
            r["altman_z"] = z
            r["altman_detail"] = detail
            change = True
            a, b = isinstance(avant, (int, float)), isinstance(z, (int, float))
            if b and not a:
                z_nes += 1
            elif a and not b:
                z_retires += 1
            elif a and b and abs(z - avant) > 0.01:
                z_bouges += 1
                if ((avant < 2.99) != (z < 2.99) or (avant < 1.81) != (z < 1.81)) \
                        and len(bascules) < 8:
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
    print("   %d ont retrouvé un total de dettes par l'identité comptable" % dettes)
    print("   Z d'Altman : %d apparus, %d RETIRÉS parce qu'amputés, %d recalculés"
          % (z_nes, z_retires, z_bouges))
    if bascules:
        print()
        print("   verdicts qui changent de camp :")
        for s, a, b in bascules:
            print("      %-14s %6.2f → %6.2f" % (s, a, b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
