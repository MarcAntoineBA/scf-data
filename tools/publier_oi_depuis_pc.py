#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Publie l'archive d'open interest depuis le PC, sans passer par le lanceur.

POURQUOI CET OUTIL PLUTÔT QUE `tools/une_passe.sh`
Le rapatriement initial — six ans, cinq cents actifs — ne tient pas dans les
soixante-dix minutes d'un passage de la cadence quotidienne. Il se fait donc
depuis le PC, en plusieurs heures, puis il faut publier le résultat.

Or `une_passe.sh` publie la liste `.publish_list` écrite par l'orchestrateur, et
celle-ci ment quand elle est produite sur le PC : mesuré le 2026-08-27, elle
déclarait 169 fichiers modifiés pour un job qui n'en écrit que deux, parce qu'elle
compare le cache local à un repère `release/` périmé. La publier écraserait le
parc avec de vieux états.

On ne pousse donc QUE les sorties de ce collecteur-ci, et on reprend telles quelles
les trois règles du lanceur :

  1. REFUS FRANC SI UN FICHIER FOND. Un fichier qui perd un tiers de son poids a
     perdu sa base de fusion. Ici ce serait un actif qui repart de zéro : trois
     ans d'archive remplacés par trente jours, sans une erreur nulle part.
  2. L'INDEX DE FRAÎCHEUR PART DANS LE MÊME COMMIT. `cache/_fichiers.json` est ce
     que lit la fonction Cloudflare pour arbitrer entre la collecte et la copie
     déployée. Pousser des données fraîches en laissant l'index sur l'ancienne
     date, c'est publier du frais que le site croira périmé.
  3. `reset` SUR origin/main AVANT CHAQUE ESSAI. Sept cadences GitHub poussent en
     permanence ; sans ce recalage, notre commit annulerait les leurs.

`oi_hist_index.json` est RECONSTRUIT ici, à partir des fichiers réellement publiés
plutôt que recopié depuis le cache : l'index doit décrire ce qui est en ligne, et
non ce que la dernière collecte locale avait sous la main. Le site s'en sert pour
éteindre les fenêtres profondes qu'un actif ne peut pas honorer — un index en
avance sur les données proposerait des fenêtres vides.

Usage :
  python3 tools/publier_oi_depuis_pc.py            # publie
  python3 tools/publier_oi_depuis_pc.py --a-blanc  # dit ce qui partirait
"""
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
CACHE = os.path.join(HOME, "Library", "Caches", "site_crypto_finance")
CLONE = os.path.join(HOME, ".cache", "scf_oi_publication")
DEPOT = "https://github.com/MarcAntoineBA/scf-data.git"
A_BLANC = "--a-blanc" in sys.argv


def lancer(cmd, cwd=None, minutes=10):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=minutes * 60)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "dépassement du délai"


def taille_saine(source, reference):
    """Règle du lanceur, reprise telle quelle : un fichier qui perd un tiers de son
    poids a perdu sa base de fusion, et le publier effacerait de l'historique."""
    if not os.path.exists(reference):
        return True
    a, b = os.path.getsize(reference), os.path.getsize(source)
    return a == 0 or b * 100 // a >= 67


def index_des_fichiers(noms):
    """L'index décrit ce qui PART, pas ce que le cache local contient."""
    act = []
    for nom in noms:
        try:
            d = json.load(open(os.path.join(CACHE, nom)))
        except (OSError, ValueError):
            continue
        act.append({"s": d["s"], "b": d["b"], "t0": d["t0"], "n": d["n"],
                    "vus": d.get("vus", d["n"]), "k": d["k"],
                    "debut": d["debut"], "fin": d["fin"],
                    "complet": bool(d.get("complet"))})
    act.sort(key=lambda x: -x["n"])
    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pas": 3600,
        "n_actifs": len(act),
        "n_complets": sum(1 for x in act if x["complet"]),
        "profondeur_max_h": max((x["n"] for x in act), default=0),
        "src": "data.binance.vision futures/um/daily/metrics",
        "note": "oi[i] × k = open interest notionnel en dollars à t0 + i × 3600 s "
                "(UTC). Le point horaire T vaut le relevé 5 min de T−5 min, ce qui "
                "le rend identique au point de futures/data/openInterestHist.",
        "actifs": act,
    }


def main():
    noms = sorted(os.path.basename(f) for f in glob.glob(CACHE + "/oi_hist_*.json")
                  if not f.endswith("oi_hist_index.json"))
    if not noms:
        print("aucun fichier d'archive dans le cache", file=sys.stderr)
        return 1
    poids = sum(os.path.getsize(os.path.join(CACHE, n)) for n in noms)
    idx = index_des_fichiers(noms)
    print("%d actifs · %.1f Mo · profondeur maximale %d jours · %d complets"
          % (len(noms), poids / 1048576, idx["profondeur_max_h"] // 24, idx["n_complets"]))
    if A_BLANC:
        for a in idx["actifs"][:8]:
            print("   %-16s %5d j  %s → %s" % (a["s"], a["n"] // 24, a["debut"], a["fin"]))
        print("(à blanc — rien n'est poussé)")
        return 0

    if not os.path.isdir(os.path.join(CLONE, ".git")):
        os.makedirs(os.path.dirname(CLONE), exist_ok=True)
        code, sortie = lancer(["git", "clone", "--depth", "1", "-b", "main", DEPOT, CLONE],
                              minutes=20)
        if code != 0:
            print("clone impossible : " + sortie[-300:], file=sys.stderr)
            return 1

    code, _ = lancer(["git", "fetch", "-q", "--depth", "1", "origin", "main"], CLONE, 10)
    if code != 0:
        print("fetch impossible", file=sys.stderr)
        return 1

    for essai in range(1, 7):
        lancer(["git", "reset", "-q", "--hard", "origin/main"], CLONE, 5)
        dossier = os.path.join(CLONE, "cache")
        pousses = []
        for nom in noms:
            src, dst = os.path.join(CACHE, nom), os.path.join(dossier, nom)
            if not taille_saine(src, dst):
                print("REFUS %s : fond d'un tiers ou plus — rien n'est poussé" % nom,
                      file=sys.stderr)
                return 1
            with open(src, "rb") as a, open(dst, "wb") as b:
                b.write(a.read())
            pousses.append("cache/" + nom)

        with open(os.path.join(dossier, "oi_hist_index.json"), "w", encoding="utf-8") as f:
            json.dump(idx, f, separators=(",", ":"), ensure_ascii=False)
        pousses.append("cache/oi_hist_index.json")

        # L'index de fraîcheur part dans le MÊME commit que les données.
        chemin = os.path.join(dossier, "_fichiers.json")
        try:
            fr = json.load(open(chemin))
            for nom in noms + ["oi_hist_index.json"]:
                fr[nom] = idx["updated"]
            with open(chemin, "w", encoding="utf-8") as f:
                json.dump(fr, f, indent=0, sort_keys=True, ensure_ascii=False)
            pousses.append("cache/_fichiers.json")
        except (OSError, ValueError) as e:
            print("  index de fraîcheur illisible (%s) — les données partent quand même" % e)

        lancer(["git", "config", "user.name", "collecte-pc"], CLONE, 1)
        lancer(["git", "config", "user.email", "collecte@users.noreply.github.com"], CLONE, 1)
        lancer(["git", "add"] + pousses, CLONE, 5)
        code, _ = lancer(["git", "diff", "--cached", "--quiet"], CLONE, 3)
        if code == 0:
            print("déjà publié à l'identique")
            return 0
        lancer(["git", "commit", "-q", "-m",
                "archive open interest : %d actifs, jusqu'à %d jours d'historique"
                % (len(noms), idx["profondeur_max_h"] // 24)], CLONE, 5)
        code, sortie = lancer(["git", "push", "-q", "origin", "HEAD:main"], CLONE, 15)
        if code == 0:
            print("publié : %d fichier(s)" % len(pousses))
            return 0
        print("  publication concurrente — nouvel essai (%d)" % essai)
        time.sleep(essai * 4)
    print("échec de publication après 6 essais", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
