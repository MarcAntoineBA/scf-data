#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pose le cours manquant sur les paquets déjà collectés, sans réseau.

POURQUOI

Le collecteur SEC prend désormais le cours dans `univers_actions.json` quand la
série du tracker ne couvre pas la société — correction du 28/08/2026, qui a fait
passer la couverture de 9,9 % à 87,6 %. Mais il ne l'applique qu'aux sociétés
qu'il RECOLLECTE, et chaque passage est borné par son budget de temps : sept
tranches ont traité environ trois mille deux cents sociétés, pas la totalité.

Restent 712 sociétés dont le paquet est complet — exercices, ratios, note — mais
dont le cours est vide. Or 709 d'entre elles ont un cours dans
`univers_actions.json`, sur ce disque. Attendre plusieurs jours que la cadence
quotidienne les rattrape reviendrait à laisser l'onglet Valorisation éteint pour
sept cents sociétés alors que la donnée est là.

CE QUE CE SCRIPT NE FAIT PAS

Il ne recalcule RIEN. Ni P/E, ni prix juste, ni note : la fiche les dérive
elle-même du cours, à l'affichage. Il ne fait qu'écrire deux champs, exactement
comme le collecteur les écrirait.

LES GARDES

· on ne touche QUE les paquets où `cours_natif` est vide — jamais on n'écrase un
  cours venu du tracker, plus précisément daté ;
· on n'accepte qu'une cotation en DOLLARS. Les paquets SEC portent des états en
  dollars ; un cours en une autre devise y rencontrerait des montants qui ne
  sont pas les siens, et referait le défaut Toyota à P/E 0,1 ;
· on n'accepte que la cotation PRINCIPALE : le cours d'un certificat de dépôt
  n'est pas celui du titre ;
· on écrit la date de l'univers, pour qu'un cours de six jours ne se confonde
  pas avec un cours du jour.
"""
import glob
import json
import os
import sys

CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")
SITE = os.path.expanduser("~/Site_Crypto_Finance/data")


def main():
    fu = os.path.join(CACHE, "univers_actions.json")
    if not os.path.exists(fu):
        print("[fatal] univers_actions.json absent", file=sys.stderr)
        return 2
    with open(fu, encoding="utf-8") as fh:
        u = json.load(fh)
    jour = str(u.get("updated") or "")[:10] or None

    cotations = {}
    for t in u.get("titres", []):
        sym = t.get("yahoo") or t.get("sa")
        px = t.get("cours")
        if (sym and t.get("principal") and isinstance(px, (int, float)) and px > 0
                and (t.get("devise") or "").upper() == "USD"):
            cotations[sym] = px
    print("univers : %d cotations principales en dollars, datées du %s"
          % (len(cotations), jour))

    total = poses = deja = sans = 0
    for f in sorted(glob.glob(os.path.join(CACHE, "sec_detail_*.json"))):
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
            if isinstance(r.get("cours_natif"), (int, float)):
                deja += 1
                continue
            px = cotations.get(sym)
            if px is None:
                sans += 1
                continue
            r["cours_natif"] = px
            r["cours_natif_le"] = jour
            r["cours_source"] = "univers"
            poses += 1
            change = True
        if change:
            texte = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(texte)
            jumeau = os.path.join(SITE, os.path.basename(f))
            if os.path.isdir(SITE):
                with open(jumeau, "w", encoding="utf-8") as fh:
                    fh.write(texte)

    print("\n%d sociétés examinées" % total)
    print("   %d avaient déjà un cours" % deja)
    print("   %d cours posés depuis l'univers" % poses)
    print("   %d restent sans cours (absentes de l'univers, ou non cotées en dollars)"
          % sans)
    if total:
        print("\n   couverture : %.1f %% → %.1f %%"
              % (100.0 * deja / total, 100.0 * (deja + poses) / total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
