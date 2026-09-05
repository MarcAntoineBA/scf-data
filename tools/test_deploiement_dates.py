#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LE GARDE-FOU DES DATES DU DÉPLOIEMENT.

CE QU'IL EMPÊCHE (mesuré le 05/09/2026)
`deploy_public_wrangler.sh` écrit `_deploy_fichiers.json`, l'index qui dit au
site l'âge de chaque fichier du DÉPLOIEMENT. Le site le compare à l'âge du même
fichier dans la branche de collecte et sert le plus frais. Tout repose donc sur
une seule chose : que cet index dise la date de la DONNÉE, pas celle de la copie.

Il disait celle de la copie. Deux causes, et la seconde avait été explicitement
interdite par un commentaire du script lui-même :

  1. `cp -R` sans `-p` ne préserve pas les mtimes : les 749 fichiers du stage
     portaient l'heure de la copie, à la seconde près (659 d'entre eux sur trois
     secondes consécutives) ;
  2. l'appel `ix.dater(chemin_du_stage, defaut)` essaie le CONTENU, puis le
     mtime du fichier qu'on lui donne — la COPIE — et seulement ensuite le
     défaut. Le défaut, pourtant calculé deux lignes plus haut sur la vraie
     source, n'était JAMAIS atteint : zéro fichier sur 749. Le commentaire des
     lignes 282-283 disait mot pour mot ce qu'il ne fallait pas faire ; la ligne
     287 le faisait.

Mesuré : **620 fichiers sur 749 étaient rajeunis.** `per_history_cache.js` de
3 125 heures — cent trente jours — `screener_index.js` de 193 heures, et quatre
caches SEC de huit jours. Le site servait donc `screener_index.js` du 28 août
pendant que la branche avait celui du 5 septembre, sous un en-tête certifiant
« deploiement=23min ».

⚠ LE MÉCANISME AVAIT ÉTÉ CONSTRUIT APRÈS LES PANNES DES 06 ET 07/08 POUR
EMPÊCHER EXACTEMENT CELA — qu'une origine « qui répond » passe pour « à jour ».
Il certifiait lui-même une donnée de huit jours et demi comme vieille de vingt-
huit minutes. Et la suite de tests passait : son unique contrôle vérifiait que
le script IMPORTE le module et publie le fichier, jamais ce qu'il y écrit.

    python3 tools/test_deploiement_dates.py
"""

import datetime
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = os.path.join(RACINE, "tools", "index_fraicheur.py")
SCRIPT = os.path.expanduser(
    "~/Library/Application Support/SiteCryptoFinance/deploy_public_wrangler.sh")

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


def main():
    print("══ dates du déploiement ══")

    print("\n[1] La copie de staging préserve les dates")
    if not os.path.exists(SCRIPT):
        print("  (script de déploiement absent de cette machine — contrôle sauté)")
    else:
        src = open(SCRIPT, encoding="utf-8", errors="ignore").read()
        m = re.search(r'cp\s+(-\S+)\s+"\$SRC/public"\s+"\$STAGE"', src)
        v(bool(m) and "p" in (m.group(1) if m else ""),
          "la copie vers le stage porte -p",
          "options trouvées : %s" % (m.group(1) if m else "aucune"))

        print("\n[2] L'index ne date jamais la COPIE")
        # ⚠ C'est le défaut lui-même : `dater()` relit le mtime du chemin qu'on
        # lui passe. Le passer sur le stage revient à dater la copie, quoi qu'en
        # dise le commentaire au-dessus.
        v("ix.dater(os.path.join(stage" not in src,
          "le script n'appelle pas dater() sur un chemin du stage")
        v("ix.horodatage_contenu(os.path.join(stage" in src,
          "il lit le contenu, puis retombe sur la date de la SOURCE")

    print("\n[3] Une copie fraîche d'un vieux cache reste vieille")
    # Le contrôle de fond, rejoué sur de vrais fichiers : c'est la propriété que
    # tout le reste sert. Un cache de 2020 recopié à l'instant doit se dater de
    # 2020, que sa date vienne de son contenu ou du mtime de sa source.
    ix = charger()
    with tempfile.TemporaryDirectory() as d:
        caches = os.path.join(d, "caches")
        stage = os.path.join(d, "stage")
        os.makedirs(caches)
        os.makedirs(stage)
        vieux = time.time() - 8.5 * 86400          # huit jours et demi

        # (a) un cache SANS date interne : seule sa source peut le dater
        for base in (caches, stage):
            with open(os.path.join(base, "muet_cache.json"), "w") as fh:
                fh.write('{"donnees":[1,2,3]}')
        os.utime(os.path.join(caches, "muet_cache.json"), (vieux, vieux))
        # le stage vient d'être écrit : son mtime est « maintenant »

        # (b) un cache AVEC date interne
        contenu = '{"genere_le":"2020-01-01T00:00:00Z","x":1}'
        for base in (caches, stage):
            with open(os.path.join(base, "date_cache.json"), "w") as fh:
                fh.write(contenu)

        def iso(t):
            return datetime.datetime.fromtimestamp(
                t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def index_du_deploiement(nom):
            """La règle CORRIGÉE, telle que le script l'applique."""
            source = os.path.join(caches, nom)
            quand = os.path.getmtime(source if os.path.exists(source)
                                     else os.path.join(stage, nom))
            return ix.horodatage_contenu(os.path.join(stage, nom)) or iso(quand)

        def index_ancien(nom):
            """La règle FAUTIVE, pour montrer que le contrôle mord."""
            source = os.path.join(caches, nom)
            quand = os.path.getmtime(source if os.path.exists(source)
                                     else os.path.join(stage, nom))
            return ix.dater(os.path.join(stage, nom), iso(quand))

        neuf = index_du_deploiement("muet_cache.json")
        age_h = (time.time() - datetime.datetime.strptime(
            neuf, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc).timestamp()) / 3600.0
        v(age_h > 200,
          "un cache muet de huit jours et demi se date à huit jours et demi",
          "âge rendu : %.1f h" % age_h)

        v(index_du_deploiement("date_cache.json").startswith("2020"),
          "un cache daté de 2020 se date de 2020",
          index_du_deploiement("date_cache.json"))

        print("\n[4] Le contrôle mord : la règle fautive doit échouer ici")
        # ⚠ Un contrôle qu'on n'a pas fait échouer ne protège rien. On rejoue la
        # règle d'avant sur les mêmes fichiers et on EXIGE qu'elle rajeunisse.
        ancien = index_ancien("muet_cache.json")
        age_ancien = (time.time() - datetime.datetime.strptime(
            ancien, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc).timestamp()) / 3600.0
        v(age_ancien < 1,
          "la règle d'avant rajeunissait bien le cache muet (elle est donc "
          "bien la cause, et ce contrôle la détecte)",
          "âge rendu par l'ancienne règle : %.1f h" % age_ancien)

    print("\n%s" % ("TOUT PASSE." if not echecs
                    else "%d CONTRÔLE(S) EN ÉCHEC." % len(echecs)))
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
