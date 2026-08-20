#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_registre_ecrits.py — Garde-fou de « répondre n'est pas travailler ».

CE QU'IL EMPÊCHE DE REVENIR
La panne du 2026-08-04, vue seulement le 2026-08-20 : dix-sept caches publiés figés
pendant seize jours derrière un bilan à 26/26 OK. Le mécanisme, en trois temps :

  1. la collecte tourne sur un serveur neuf ; on lui RESTITUE le cache précédent
     (c'est sa base de fusion, et c'est juste) — mais daté de l'instant de la copie ;
  2. la moitié des collecteurs décide de travailler en regardant l'âge de ce cache
     (« moins de 4 h : rien à faire ») : la garde se referme, à chaque passage ;
  3. le collecteur sort en SUCCÈS sans avoir rien écrit, et l'index de fraîcheur
     redate ses sorties comme fraîches — le site sert alors la copie gelée en
     croyant servir la plus récente.

Trois promesses, dont aucune ne se voit dans un diff, donc chacune est vérifiée ici :

  A. un cache restitué retrouve son ÂGE VÉRITABLE, pas la date de sa copie ;
  B. un collecteur qui aboutit sans écrire ne date RIEN (et il est nommé) ;
  C. le registre se fusionne d'une cadence à l'autre au lieu de s'écraser.

Lancement : python3 tools/test_registre_ecrits.py
"""

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_jobs as rj  # noqa: E402

échecs = []


def verifie(nom, obtenu, attendu):
    if obtenu != attendu:
        échecs.append(f"{nom}\n     attendu : {attendu!r}\n     obtenu  : {obtenu!r}")
    print(f"  {'✓' if obtenu == attendu else '✗'} {nom}")


def main():
    tmp = tempfile.mkdtemp(prefix="registre_")
    cache_out = os.path.join(tmp, "cache")
    release_out = os.path.join(tmp, "release")
    cache_dir = os.path.join(tmp, "caches_machine")
    scripts = os.path.join(tmp, "scripts")
    for d in (cache_out, release_out, cache_dir, scripts):
        os.makedirs(d)

    origines = (rj.CACHE_OUT, rj.RELEASE_OUT, rj.CACHE_DIR, rj.SCRIPTS)
    rj.CACHE_OUT, rj.RELEASE_OUT, rj.CACHE_DIR, rj.SCRIPTS = \
        cache_out, release_out, cache_dir, scripts
    try:
        seize_jours = time.time() - 16 * 86400
        with open(os.path.join(cache_out, "gele_cache.js"), "w") as f:
            f.write("window.X={};")
        with open(os.path.join(cache_out, "neuf_cache.js"), "w") as f:
            f.write("window.Y={};")

        # ── C. Deux cadences écrivent chacune son registre ; la lecture fusionne ──
        rj.ecrire_ecrits("1h", {"gele_cache.js"}, seize_jours)
        rj.ecrire_ecrits("6h", {"autre_cache.json"}, seize_jours)
        fusion = rj.lire_ecrits()
        verifie("C. le registre d'une cadence n'écrase pas celui d'une autre",
                sorted(fusion), ["autre_cache.json", "gele_cache.js"])

        # La date la plus récente l'emporte quand deux cadences écrivent le même
        # fichier : c'est l'âge qu'aurait constaté la machine d'origine.
        rj.ecrire_ecrits("6h", {"gele_cache.js"}, seize_jours + 3600)
        verifie("C. deux cadences sur un même fichier : la plus récente gagne",
                round(rj.lire_ecrits()["gele_cache.js"]), round(seize_jours + 3600))

        # ── A. Un cache restitué retrouve son âge véritable ───────────────────
        rj.ecrire_ecrits("6h", set(), 0)          # remet le registre 6h à plat
        os.remove(os.path.join(cache_out, rj.ECRITS_PREFIXE + "6h.json"))
        rj.prepare_env(())
        age_gele = (time.time() - os.path.getmtime(
            os.path.join(cache_dir, "gele_cache.js"))) / 3600
        verifie("A. le cache gelé est restitué avec ses 16 jours",
                round(age_gele / 24), 16)
        age_neuf = (time.time() - os.path.getmtime(
            os.path.join(cache_dir, "neuf_cache.js"))) / 3600
        verifie("A. un cache inconnu du registre reste neuf (il l'est vraiment)",
                age_neuf < 1, True)

        # A bis. LA COPIE NE SUFFIT PAS — c'est ce qui a fait échouer le premier
        # correctif. L'index qui dit au site quelle origine servir lit les fichiers
        # du DÉPÔT, pas la copie rendue aux collecteurs : il remesure tout fichier
        # dont la date dépasse celle qu'il avait notée, ce qui est vrai de tous
        # après un clone. Un cache gelé au 4 août restait donc annoncé « il y a
        # 4 minutes ». La source doit être datée, pas seulement sa copie.
        age_depot = (time.time() - os.path.getmtime(
            os.path.join(cache_out, "gele_cache.js"))) / 3600
        verifie("A bis. le fichier du DÉPÔT porte lui aussi ses 16 jours",
                round(age_depot / 24), 16)

        # ── B. Ce qui n'a pas été écrit n'est pas daté ────────────────────────
        debut = time.time()
        time.sleep(0.01)
        with open(os.path.join(cache_dir, "neuf_cache.js"), "w") as f:
            f.write("window.Y={z:1};")           # un collecteur écrit vraiment
        ecrits = rj.ecrits_depuis(debut, {"gele_cache.js", "neuf_cache.js"})
        verifie("B. seul le fichier réellement écrit est retenu",
                sorted(ecrits), ["neuf_cache.js"])
        verifie("B. le cache d'un collecteur muet reste hors du registre",
                "gele_cache.js" in ecrits, False)

        # ── B bis. Muet ≠ en avance ──────────────────────────────────────────
        # Dans une cadence rejouée trois fois par exécution, un collecteur qui a
        # écrit il y a quatre minutes et dont la source n'a rien publié depuis est
        # sain. Le nommer à chaque passage transformerait l'alerte en décor — et
        # c'est un décor qu'on regarde sans voir, donc la panne d'origine.
        emploi = [{"id": "recent", "outputs": ["neuf_cache.js"]},
                  {"id": "gele",   "outputs": ["gele_cache.js"]}]
        vieux = time.time() - 16 * 86400
        os.utime(os.path.join(cache_dir, "gele_cache.js"), (vieux, vieux))
        muets = rj.collecteurs_muets(emploi, {"recent", "gele"}, set(), 2 * 3600)
        verifie("B bis. le collecteur au cache frais n'est pas signalé",
                any(m.startswith("recent") for m in muets), False)
        verifie("B bis. le collecteur au cache gelé est signalé avec son âge",
                [m.split(" (")[0] for m in muets], ["gele"])
        verifie("B bis. l'âge annoncé est celui de la donnée",
                round(float(muets[0].split("(")[1].split(" h")[0]) / 24), 16)
        verifie("B bis. un collecteur qui vient d'écrire n'est jamais muet",
                rj.collecteurs_muets(emploi, {"gele"}, {"gele_cache.js"}, 2 * 3600), [])

        # B ter. Le plancher de deux heures. Plusieurs collecteurs portent une garde
        # interne PLUS LONGUE que la cadence qui les appelle : les news sont réveillées
        # toutes les 5 min et ne se réécrivent qu'à l'heure. Deux tours de cadence
        # suffiraient à les nommer 55 minutes sur 60, et une alerte permanente
        # n'alerte plus personne — c'est ainsi qu'on ne voit plus rien.
        seuil = max(2 * rj.PERIODES["5min"], 7200)
        une_heure = time.time() - 3600
        os.utime(os.path.join(cache_dir, "gele_cache.js"), (une_heure, une_heure))
        verifie("B ter. cadence 5 min, cache d'une heure : on se tait",
                rj.collecteurs_muets(emploi, {"gele"}, set(), seuil), [])
        trois_heures = time.time() - 3 * 3600
        os.utime(os.path.join(cache_dir, "gele_cache.js"), (trois_heures, trois_heures))
        verifie("B ter. cadence 5 min, cache de trois heures : on parle",
                [m.split(" (")[0] for m in
                 rj.collecteurs_muets(emploi, {"gele"}, set(), seuil)], ["gele"])

        # Un fichier réécrit à l'identique COMPTE comme écrit : la donnée a été
        # vérifiée fraîche, elle n'a simplement pas changé. Le distinguer d'un
        # collecteur muet est tout l'objet de la mesure.
        with open(os.path.join(cache_dir, "gele_cache.js"), "w") as f:
            f.write("window.X={};")
        verifie("B. une réécriture à l'identique compte comme une écriture",
                sorted(rj.ecrits_depuis(debut, {"gele_cache.js"})), ["gele_cache.js"])
    finally:
        rj.CACHE_OUT, rj.RELEASE_OUT, rj.CACHE_DIR, rj.SCRIPTS = origines
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if échecs:
        print(f"✗ {len(échecs)} contrôle(s) en échec :")
        for e in échecs:
            print(f"   · {e}")
        return 1
    print("✓ tous les contrôles passent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
