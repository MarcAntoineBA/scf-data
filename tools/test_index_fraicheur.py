#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LE GARDE-FOU DE L'INDEX DE FRAÎCHEUR.

CE QU'IL EMPÊCHE (mesuré le 05/09/2026)
`index_fraicheur.py` date chaque cache pour que le site puisse ARBITRER entre la
copie déployée et celle de la branche de collecte : le plus frais gagne. Il
essaie trois sources, dans cet ordre de confiance — le CONTENU du cache, le
mtime du FICHIER, l'heure du PASSAGE. C'est un bon ordre, à une condition : que
la première marche.

Elle ne marchait pas. Mesuré sur les 2 874 caches de ce parc, **172 seulement —
six pour cent — étaient datés par leur contenu.** Les 2 702 autres retombaient
sur le mtime, c'est-à-dire sur la date de la COPIE. Or une copie est toujours
jeune : elle vient d'être écrite. L'index certifiait donc « frais » des caches
figés depuis des jours, et l'arbitrage préférait systématiquement le
déploiement — servant 8,3 jours quand la branche en avait 5,6 heures.

TROIS CAUSES, TOUTES SILENCIEUSES :

  1. `genere_le` ne figurait dans AUCUNE des deux listes de champs. C'est le
     champ que datent les collecteurs récents. Le chercher n'a jamais échoué :
     on ne le cherchait pas.
  2. Une date nue suffixée « UTC » — « 2026-08-29 10:15 UTC » — était refusée
     comme ambiguë. Elle nomme pourtant son horloge en toutes lettres. Les
     témoins de `marche` et d'`actionnariat` en portent une.
  3. Le PREMIER champ mal formé faisait abandonner toute la recherche, par un
     `return None` là où il fallait passer au suivant. Un cache correctement
     daté plus bas restait muet à cause d'un champ plus haut.

Après correction : **1 936 caches sur 2 874, soit soixante-sept pour cent.**

    python3 tools/test_index_fraicheur.py
"""

import datetime
import glob
import importlib.util
import os
import sys
import tempfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = os.path.join(RACINE, "tools", "index_fraicheur.py")
CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")

# Ce que le parc atteignait avant correction, et ce qu'il doit garder. On borne
# par le BAS : le jour où un collecteur cesse de dater ses sorties, la part
# baisse et le contrôle le dit, au lieu de laisser l'index retomber en silence
# sur des dates de copie.
PART_MINIMALE = 0.55

echecs = []


def v(ok, titre, detail=""):
    print("  %s %s%s" % ("✓" if ok else "✗", titre, "" if ok else " — " + detail))
    if not ok:
        echecs.append(titre)


def charger():
    spec = importlib.util.spec_from_file_location("ix", MODULE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def ecrire(contenu):
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    fh.write(contenu)
    fh.close()
    return fh.name


def main():
    ix = charger()
    print("══ index de fraîcheur ══")

    print("\n[1] Les champs que datent réellement les collecteurs sont lus")
    # Chacun de ces trois a existé dans le parc et n'était pas lu.
    cas = [
        ('{"genere_le":"2026-09-05T13:53:43Z","jetons":{}}', "genere_le en ISO"),
        ('{"generated_at":"2026-09-05T13:53:43Z","x":1}', "generated_at en ISO"),
        ('{"updated":"2026-08-29 10:15 UTC","x":1}', "date nue suffixée UTC"),
        ('{"source_updated":"2026-09-05T10:00:00Z","x":1}', "source_updated"),
    ]
    for contenu, nom in cas:
        p = ecrire(contenu)
        try:
            v(ix.horodatage_contenu(p) is not None, "lit %s" % nom)
        finally:
            os.unlink(p)

    print("\n[2] Un champ illisible ne clôt pas la recherche")
    # ⚠ DÉFAUT RÉEL : le `return None` du premier champ sans fuseau abandonnait
    # tout, même quand le cache portait plus bas une date parfaitement formée.
    p = ecrire('{"as_of":"29/08/2026","genere_le":"2026-09-05T13:53:43Z","x":1}')
    try:
        v(ix.horodatage_contenu(p) is not None,
          "une date ambiguë en tête n'efface pas une date valide plus bas")
    finally:
        os.unlink(p)

    print("\n[3] Une date vraiment ambiguë reste refusée")
    # Le contrôle doit mordre dans les DEUX sens : accepter plus largement ne
    # doit pas revenir à tout accepter. Une date nue sans horloge nommée ne
    # permet pas de comparer deux machines, et retomber sur le mtime est alors
    # le comportement JUSTE.
    p = ecrire('{"updated":"2026-08-29 10:15","x":1}')
    try:
        v(ix.horodatage_contenu(p) is None,
          "une date sans fuseau ni mention d'horloge est refusée")
    finally:
        os.unlink(p)

    print("\n[4] Le parc reste majoritairement daté par son contenu")
    fichiers = sorted(glob.glob(os.path.join(CACHE, "*.js"))
                      + glob.glob(os.path.join(CACHE, "*.json")))
    if not fichiers:
        print("  (parc local absent — contrôle sauté)")
    else:
        ok = sum(1 for p in fichiers if ix.horodatage_contenu(p))
        part = ok / len(fichiers)
        v(part >= PART_MINIMALE,
          "au moins %d %% des caches portent leur propre date" % (PART_MINIMALE * 100),
          "%d/%d = %.0f %%" % (ok, len(fichiers), part * 100))
        print("      (%d caches sur %d, soit %.0f %%)" % (ok, len(fichiers), part * 100))

    print("\n[5] La date lue est celle de la DONNÉE, pas celle du fichier")
    # Le cœur du défaut : un cache ancien fraîchement recopié doit garder sa
    # date ancienne. Sans quoi l'arbitrage préfère toujours la copie la plus
    # récemment écrite, qui n'est pas la donnée la plus fraîche.
    p = ecrire('{"genere_le":"2020-01-01T00:00:00Z","x":1}')
    try:
        os.utime(p, None)          # le fichier vient d'être touché
        t = ix.horodatage_contenu(p)
        an = datetime.datetime.fromisoformat(str(t).replace("Z", "+00:00")).year \
            if isinstance(t, str) else datetime.datetime.utcfromtimestamp(t).year
        v(an == 2020, "un cache de 2020 recopié aujourd'hui reste daté de 2020",
          "année lue : %s" % an)
    finally:
        os.unlink(p)

    print("\n%s" % ("TOUT PASSE." if not echecs else "%d CONTRÔLE(S) EN ÉCHEC." % len(echecs)))
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
