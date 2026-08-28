#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Efface les grandeurs de MARCHÉ des paquets dont les états ne sont pas en dollars.

POURQUOI

Un déposant étranger coté aux États-Unis publie ses états en monnaie locale —
wons, pesos, réaux — tandis que sa capitalisation et son cours viennent de la
cotation américaine, donc en dollars. Tout ratio qui croise les deux est faux
d'un facteur mille. C'est le défaut qui avait mis Toyota à un P/E de 0,1, et il
a déjà coûté une soirée à ce dépôt.

Le collecteur refuse désormais ces grandeurs à la source. Mais la collecte qui
vient de tourner portait la correction de DEVISE sans encore celle-ci : les
paquets ont donc la bonne monnaie et gardent une capitalisation en dollars.

Ce script applique le même geste aux paquets déjà écrits, sans une requête. Il
ne recalcule rien — il EFFACE, ce qui est précisément le but : mieux vaut une
case vide qu'un multiple faux, parce qu'une case vide se voit.

POURQUOI ON NE CONVERTIT PAS
Le cache de change existe. Sa dernière cotation date du 28 avril, quatre mois
plus tôt, et il ne couvre pas le peso colombien. Une conversion à un taux périmé
serait une fausse précision — plus dangereuse qu'un vide, parce qu'elle a l'air
d'un chiffre.
"""
import glob
import json
import os
import sys

CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")
SITE = os.path.expanduser("~/Site_Crypto_Finance/data")


def main():
    paquets = sorted(glob.glob(os.path.join(CACHE, "sec_detail_[0-9][0-9][0-9].json")))
    if not paquets:
        print("[fatal] aucun paquet sec_detail_NNN.json", file=sys.stderr)
        return 2

    total = touchees = exercices = 0
    devises = {}
    for f in paquets:
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        change = False
        for sym, v in (d.get("societes") or {}).items():
            r = v.get("resume")
            if not isinstance(r, dict):
                continue
            total += 1
            dev = r.get("devise")
            if not dev or dev == "USD":
                continue
            devises[dev] = devises.get(dev, 0) + 1
            touchees += 1
            r["devise_cotation"] = "USD"
            r["devises_alignees"] = False
            # Ce script EFFACE : la fiche peut l'annoncer tel quel. La filière
            # internationale, elle, convertit — d'où ce champ, qui dit lequel
            # des deux traitements a eu lieu au lieu de le déduire.
            r["montants_marche"] = "ecartes"
            r["cours_natif"] = None
            r["cours_natif_le"] = None
            r["cours_source"] = None
            for e in (v.get("exercices") or []):
                if e.get("mcap_estime") is not None or e.get("wacc") is not None:
                    exercices += 1
                e["mcap_estime"] = None
                e["wacc"] = None
                e["ecart_roic_wacc"] = None
            change = True
        if change:
            texte = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(texte)
            jumeau = os.path.join(SITE, os.path.basename(f))
            if os.path.isdir(SITE):
                with open(jumeau, "w", encoding="utf-8") as fh:
                    fh.write(texte)

    print("%d sociétés examinées" % total)
    print("   %d dont les états ne sont pas en dollars" % touchees)
    print("   %d exercice(s) dont la capitalisation et le coût du capital sont effacés"
          % exercices)
    if devises:
        print("   devises : %s"
              % ", ".join("%s %d" % kv for kv in
                          sorted(devises.items(), key=lambda x: -x[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
