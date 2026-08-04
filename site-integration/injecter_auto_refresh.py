#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
injecter_auto_refresh.py — Ajoute la mise à jour automatique aux pages du site.

Idempotent : relancé, il ne fait rien sur une page déjà équipée. À rejouer après un
nouveau rendu de page, puisque le rendu réécrit le HTML depuis la source.

N'ÉQUIPE QUE LES PAGES QUI AFFICHENT DES DONNÉES. Poser le script sur les 73 pages
ferait tourner une surveillance sur des pages statiques qui n'ont rien à surveiller :
du bruit réseau pour rien, et un mécanisme dont on ne saurait plus s'il sert.

Par défaut : SIMULATION.
"""

import os
import re
import sys

SITE = os.path.expanduser("~/Desktop/Site_Crypto_Finance")
SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_refresh.js")
CIBLE_JS = os.path.join(SITE, "assets", "js", "auto_refresh.js")

# Les fichiers de données du site portent l'une de ces marques dans leur nom.
# La balise `<script>` s'étale souvent sur plusieurs lignes dans ce HTML : chercher
# `<script ... src=` d'un seul tenant ratait la majorité des pages, dont l'accueil.
DONNEES = re.compile(r'src="[^"]*(?:_cache|_live|_light|_alert)[\w.-]*\.js', re.I)
BALISE = '<script src="/assets/js/auto_refresh.js" defer></script>'
MARQUEUR = "auto_refresh.js"


def main():
    appliquer = "--appliquer" in sys.argv[1:]
    if not os.path.isdir(SITE):
        sys.exit(f"Dépôt du site introuvable : {SITE}")

    pages = sorted(f for f in os.listdir(SITE) if f.endswith(".html"))
    equipees, deja, sans_donnees = [], [], 0

    for nom in pages:
        chemin = os.path.join(SITE, nom)
        try:
            html = open(chemin, encoding="utf-8", errors="replace").read()
        except OSError:
            continue

        if not DONNEES.search(html):
            sans_donnees += 1
            continue
        if MARQUEUR in html:
            deja.append(nom)
            continue

        # Juste avant </body> : le script n'a besoin de rien d'autre que du DOM chargé,
        # et le placer en tête retarderait l'affichage pour une fonction d'arrière-plan.
        i = html.rfind("</body>")
        if i == -1:
            continue
        nouveau = html[:i] + "  " + BALISE + "\n" + html[i:]
        if appliquer:
            with open(chemin, "w", encoding="utf-8") as f:
                f.write(nouveau)
        equipees.append(nom)

    if appliquer:
        os.makedirs(os.path.dirname(CIBLE_JS), exist_ok=True)
        with open(CIBLE_JS, "w", encoding="utf-8") as f:
            f.write(open(SOURCE, encoding="utf-8").read())

    prefixe = "" if appliquer else "[simulation] "
    print(f"{prefixe}{len(pages)} pages examinées")
    print(f"  équipées maintenant : {len(equipees)}")
    print(f"  déjà équipées       : {len(deja)}")
    print(f"  sans données        : {sans_donnees}")
    for nom in equipees[:12]:
        print(f"    + {nom}")
    if len(equipees) > 12:
        print(f"    … et {len(equipees) - 12} autres")
    if not appliquer:
        print("\nRien n'a été écrit. Relancer avec --appliquer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
