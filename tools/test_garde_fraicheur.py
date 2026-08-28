#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_garde_fraicheur.py — Garde-fou : on ne remonte pas le temps en publiant.

CE QU'IL EMPÊCHE DE REVENIR
Le 2026-08-28, trois collecteurs SEC — initiés, rachats, gérants 13F — publiaient
une donnée du 16 août. Douze jours de retard sur l'onglet Société de la fiche.

Les collecteurs n'étaient pas en cause : lancés sur le PC, ils rendaient 298
sociétés en 85 s, et le runner les produisait chaque nuit — ses commits du 23, 24,
25 et 26 août contiennent bien leurs six fichiers. La cause était un acheminement
lancé depuis le PC : ce poste ne fait pas tourner tous les collecteurs, plusieurs
sources lui étant fermées, donc ses caches vieillissent pendant que ceux du runner
avancent. Sa copie du 16 août a écrasé la version fraîche.

La garde existante ne regardait que la TAILLE — elle refuse un fichier de plus de
50 Ko qui perd plus d'un tiers, et elle a déjà servi. Mais un fichier PÉRIMÉ de
même poids passait en silence : collecte réussie, commit réussi, douze jours
perdus sans un mot.

CE QUE LA GARDE FAIT MAINTENANT
Quand les deux versions portent un horodatage interne lisible, on refuse celle qui
est plus ancienne. Sans horodatage des deux côtés on ne devine pas : la garde de
poids reste seule juge, comme avant.

Lancement : python3 tools/test_garde_fraicheur.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_jobs                                                    # noqa: E402

échecs = []

# (nom, contenu local, contenu du dépôt, sort attendu)
CAS = [
    # Le cas réel du 28 août : la copie du poste a douze jours de retard.
    ("perime.json", '{"updated":"2026-08-16T05:00:00Z","x":1}',
     '{"updated":"2026-08-28T16:00:00Z","x":1}', "refusé"),

    # Le cas ordinaire : une collecte fraîche doit passer, évidemment.
    ("frais.json", '{"updated":"2026-08-28T16:00:00Z","x":1}',
     '{"updated":"2026-08-16T05:00:00Z","x":1}', "publié"),

    # Sans horodatage, on ne devine pas : la garde se tait et laisse celle du
    # poids juger seule. Une garde qui devine refuse un jour à tort.
    ("sans_date.json", '{"x":2}', '{"x":1}', "publié"),

    # ── Les deux formats du dépôt, dans le même fichier ────────────────────
    # « 2026-08-28 11:29 UTC » et « 2026-08-28T16:00:00Z » cohabitent selon le
    # collecteur, et l'espace (0x20) se classe AVANT le « T » (0x54).
    #
    # ⚠ LE MÊME JOUR DES DEUX CÔTÉS, sinon le cas ne prouve rien : la date
    # départage avant qu'on atteigne le séparateur. C'est à date égale que le
    # piège mord — ci-dessous, sans normalisation, 18 h paraîtrait plus VIEUX
    # que 5 h du matin du même jour, et la sortie neuve serait refusée à tort.
    ("melange_neuf.json", '{"updated":"2026-08-28 18:00 UTC","x":1}',
     '{"updated":"2026-08-28T05:00:00Z","x":1}', "publié"),
    ("melange_vieux.json", '{"updated":"2026-08-28 05:00 UTC","x":1}',
     '{"updated":"2026-08-28T16:00:00Z","x":1}', "refusé"),
]


def verifie(nom, obtenu, attendu):
    if obtenu != attendu:
        échecs.append(f"{nom}\n     attendu : {attendu!r}\n     obtenu  : {obtenu!r}")
    print(f"  {'✓' if obtenu == attendu else '✗'} {nom} — {obtenu}")


def main():
    # On travaille dans un dossier temporaire : `collect()` écrit vraiment, et
    # un test qui touche au cache réel du poste vaut moins que pas de test.
    base = tempfile.mkdtemp(prefix="garde_fraicheur_")
    src_dir = os.path.join(base, "cache_local")
    out_dir = os.path.join(base, "depot")
    rel_dir = os.path.join(base, "release")
    for d in (src_dir, out_dir, rel_dir):
        os.makedirs(d)

    ancien = (run_jobs.CACHE_DIR, run_jobs.CACHE_OUT, run_jobs.RELEASE_OUT)
    run_jobs.CACHE_DIR, run_jobs.CACHE_OUT, run_jobs.RELEASE_OUT = src_dir, out_dir, rel_dir
    try:
        for nom, local, depot, _ in CAS:
            with open(os.path.join(src_dir, nom), "w", encoding="utf-8") as fh:
                fh.write(local)
            with open(os.path.join(out_dir, nom), "w", encoding="utf-8") as fh:
                fh.write(depot)

        small, big, absent, retires, fondus, perimes = run_jobs.collect(
            [c[0] for c in CAS])
        publies = set(small) | set(big)
        refuses = {p.split(" ")[0] for p in perimes}

        print("La garde de fraîcheur :")
        for nom, _, _, attendu in CAS:
            obtenu = ("refusé" if nom in refuses
                      else "publié" if nom in publies else "inchangé")
            verifie(nom, obtenu, attendu)

        # Un refus muet est à peine mieux qu'une garde absente : c'est en
        # laissant un avertissement au milieu de cinq cents lignes qu'on avait
        # publié 93 caches appauvris sans s'en apercevoir. Le message doit
        # nommer le fichier ET les deux dates.
        print()
        print("Le message du refus :")
        msg = perimes[0] if perimes else ""
        verifie("nomme le fichier", "oui" if "perime.json" in msg else "non", "oui")
        verifie("montre les deux dates",
                "oui" if ("2026-08-28" in msg and "2026-08-16" in msg) else "non", "oui")
    finally:
        run_jobs.CACHE_DIR, run_jobs.CACHE_OUT, run_jobs.RELEASE_OUT = ancien
        shutil.rmtree(base, ignore_errors=True)

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
