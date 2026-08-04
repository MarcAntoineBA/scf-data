#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_deps.py — Vérifie que requirements.txt couvre tout ce que les collecteurs importent.

Un module oublié ne se voit qu'à l'exécution, et seulement pour le collecteur qui
s'en sert : `feedparser` manquait, et seul le fil de news est tombé — les 68 autres
collecteurs n'y voyaient rien. Ce contrôle rend l'oubli visible AVANT l'envoi.

Sortie non nulle si un module manque, pour pouvoir être branché sur un workflow.
"""
import ast
import glob
import os
import sys

ALIAS = {"bs4": "beautifulsoup4", "yaml": "pyyaml", "PIL": "pillow",
         "dateutil": "python_dateutil", "sklearn": "scikit_learn"}

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))


def main():
    std = set(sys.stdlib_module_names)
    fichiers = (glob.glob(os.path.join(ROOT, "scripts", "*.py"))
                + glob.glob(os.path.join(ROOT, "scripts", "*", "*.py")))
    locaux = {os.path.basename(f)[:-3] for f in fichiers}

    utilises = set()
    for f in fichiers:
        try:
            arbre = ast.parse(open(f, encoding="utf-8", errors="replace").read())
        except SyntaxError as e:
            print(f"  ! {os.path.basename(f)} illisible : {e}")
            continue
        for n in ast.walk(arbre):
            noms = ([a.name.split(".")[0] for a in n.names] if isinstance(n, ast.Import)
                    else [n.module.split(".")[0]] if isinstance(n, ast.ImportFrom)
                    and n.level == 0 and n.module else [])
            utilises.update(m for m in noms if m not in std and m not in locaux)

    declares = {l.split(">")[0].split("=")[0].split("#")[0].strip().lower().replace("-", "_")
                for l in open(os.path.join(ROOT, "requirements.txt"))
                if l.strip() and not l.startswith("#")}

    manquants = sorted(m for m in utilises
                       if ALIAS.get(m, m).lower().replace("-", "_") not in declares)

    print(f"{len(utilises)} modules externes utilisés · {len(declares)} déclarés")
    if manquants:
        for m in manquants:
            print(f"  MANQUANT : {m} (paquet « {ALIAS.get(m, m)} »)")
        return 1
    print("requirements.txt couvre tout ce qui est importé")
    return 0


if __name__ == "__main__":
    sys.exit(main())
