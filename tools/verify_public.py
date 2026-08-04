#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_public.py — Vérifie ce que le SITE PUBLIC sert vraiment, fichier par fichier.

Le bilan des cadences dit qu'un collecteur a réussi. Il ne dit pas que la donnée est
arrivée jusqu'au visiteur : entre les deux il y a la publication, la fonction, les
deux origines et le repli. C'est ce dernier maillon que ce contrôle mesure — et c'est
le seul qui compte pour quelqu'un qui consulte le site.

Trois verdicts par fichier :
  branche / piece-jointe → servi depuis le dépôt de collecte (le Mac n'y est pour rien)
  deploiement            → REPLI : le collecteur n'a pas encore réussi dans le cloud,
                           on sert la copie du dernier déploiement (donnée du Mac)
  absent                 → ni l'un ni l'autre : la page n'aura rien

Chaque requête contourne le cache : sans ça on mesure ce que le cache a gardé de la
version d'AVANT, et on conclut à tort.
"""
import collections
import concurrent.futures as cf
import os
import sys
import time
import urllib.request

BASE = "https://site-crypto-finance.pages.dev"
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def sonder(nom):
    url = f"{BASE}/{nom}?cb={int(time.time() * 1000)}"
    req = urllib.request.Request(url, headers={"User-Agent": "verif-publication"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            origine = r.headers.get("x-scf-origin") or "sans-fonction"
            taille = len(r.read())
            return nom, origine, taille
    except Exception as e:
        return nom, f"erreur:{type(e).__name__}", 0


def main():
    manifeste = [l.strip() for l in open(os.path.join(ROOT, "cache_manifest.txt"))
                 if l.strip()]
    print(f"Contrôle de {len(manifeste)} fichiers servis par {BASE}\n")

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        resultats = list(ex.map(sonder, manifeste))

    par_origine = collections.Counter(o for _, o, _ in resultats)
    for origine, n in par_origine.most_common():
        print(f"  {n:3}  {origine}")

    replis = [(n, t) for n, o, t in resultats if o == "deploiement"]
    if replis:
        print(f"\n{len(replis)} fichier(s) encore servis depuis le repli "
              f"(collecteur pas encore réussi dans le cloud) :")
        for nom, _ in sorted(replis)[:15]:
            print(f"    {nom}")

    casses = [(n, o) for n, o, t in resultats if o.startswith("erreur") or t == 0]
    if casses:
        print(f"\n{len(casses)} fichier(s) EN ÉCHEC :")
        for nom, o in casses[:10]:
            print(f"    {nom:38} {o}")

    depuis_cloud = par_origine.get("branche", 0) + par_origine.get("piece-jointe", 0)
    print(f"\n{depuis_cloud}/{len(manifeste)} fichiers servis depuis le dépôt de collecte "
          f"({100 * depuis_cloud / len(manifeste):.0f} %)")
    return 0 if not casses else 1


if __name__ == "__main__":
    sys.exit(main())
