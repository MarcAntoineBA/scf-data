#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rejoue le regroupement des détenteurs sur l'actionnariat déjà collecté.

POURQUOI

La première collecte a lu dix-neuf mille documents en cinquante-huit minutes.
Trois règles lui manquaient, et elles se voient à l'œil nu sur les fiches :

  · « The Vanguard Group » figurait à 0,00 % à côté de la vraie ligne Vanguard.
    Un déclarant qui repasse sous les 5 % dépose un dernier document à zéro : ce
    n'est pas une détention, c'est une sortie ;
  · la clé de normalisation ne mordait pas sur l'article initial — « THE
    VANGUARD GROUP » et « Vanguard Capital Management » tombaient dans deux
    clés différentes, et la même maison apparaissait deux fois ;
  · chez AvalonBay, « Vanguard Portfolio Management » (8,69 %) et « Vanguard
    Capital Management » (7,64 %) s'ADDITIONNAIENT dans le total déclaré. Ce ne
    sont pas deux détentions : c'est la même, vue par deux entités du groupe.

Ces trois règles ne demandent aucune donnée nouvelle : chaque enregistrement
conserve le nom complet de son déclarant. On rejoue donc le regroupement sur ce
qui est écrit, plutôt que de refaire une heure de requêtes.

⚠ On IMPORTE `_cle_detenteur` du collecteur. Une seconde copie de cette fonction
divergerait, et la divergence produirait des regroupements différents entre la
collecte quotidienne et ce rattrapage — c'est-à-dire une fiche qui change de
contenu selon qui l'a écrite en dernier.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_actionnariat import _cle_detenteur              # noqa: E402

CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")
SORTIE = os.path.join(CACHE, "actionnariat_cache.json")
SORTIE_JS = os.path.join(CACHE, "actionnariat_cache.js")
# ⚠ LA RACINE DU SITE, PAS SON DOSSIER data/. Les caches PLATS y vivent — c est
# la que  va les chercher, avec un nom nu. Seuls les paquets decoupes
# par empreinte habitent data/. Ecrire au mauvais endroit produit un 404 muet :
# la carte s affiche, annonce qu aucun detenteur n a declare, et personne ne voit
# que le fichier existe a trois centimetres de la.
SITE = os.path.expanduser("~/Site_Crypto_Finance")


def main():
    if not os.path.exists(SORTIE):
        print("[fatal] %s absent" % SORTIE, file=sys.stderr)
        return 2
    with open(SORTIE, encoding="utf-8") as fh:
        doc = json.load(fh)
    soc = doc.get("societes") or {}

    n_avant = sum(v.get("n") or 0 for v in soc.values())
    zeros = fusions = 0
    neuf = {}
    for sym, bloc in soc.items():
        par_cle = {}
        for x in (bloc.get("detenteurs") or []):
            p = x.get("part_pct")
            if not isinstance(p, (int, float)) or p <= 0:
                zeros += 1
                continue
            cle = _cle_detenteur(x.get("detenteur"))
            vu = par_cle.get(cle)
            if vu is None:
                par_cle[cle] = x
                continue
            fusions += 1
            # La PLUS GRANDE part : deux entités d'une même maison déclarent la
            # même détention, pas deux détentions qui s'ajoutent.
            if p > vu["part_pct"] or (p == vu["part_pct"]
                                      and (x.get("depose_le") or "")
                                      > (vu.get("depose_le") or "")):
                par_cle[cle] = x
        lignes = sorted(par_cle.values(), key=lambda y: -(y.get("part_pct") or 0))
        if not lignes:
            continue
        neuf[sym] = {
            "detenteurs": lignes,
            "n": len(lignes),
            "part_declaree_pct": round(sum(y["part_pct"] for y in lignes), 2),
            "dernier_depot": max((y.get("depose_le") or "") for y in lignes),
        }

    doc["societes"] = neuf
    texte = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    with open(SORTIE, "w", encoding="utf-8") as fh:
        fh.write(texte)
    with open(SORTIE_JS, "w", encoding="utf-8") as fh:
        fh.write("window.__ACTIONNARIAT__=" + texte + ";")
    if os.path.isdir(SITE):
        for nom, contenu in ((os.path.basename(SORTIE), texte),
                             (os.path.basename(SORTIE_JS),
                              "window.__ACTIONNARIAT__=" + texte + ";")):
            with open(os.path.join(SITE, nom), "w", encoding="utf-8") as fh:
                fh.write(contenu)

    n_apres = sum(v["n"] for v in neuf.values())
    print("%d sociétés · %d détenteur(s) avant, %d après"
          % (len(neuf), n_avant, n_apres))
    print("   %d déclaration(s) à 0 %% écartée(s) — des sorties, pas des détentions"
          % zeros)
    print("   %d doublon(s) de maison fusionné(s) sur la plus grande part" % fusions)
    print("   %s : %d Ko" % (os.path.basename(SORTIE),
                             os.path.getsize(SORTIE) // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
