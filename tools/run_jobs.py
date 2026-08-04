#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_jobs.py — Exécute les collecteurs dus pour une cadence donnée. Remplace launchd.

CE QUI CHANGE PAR RAPPORT À launchd, ET POURQUOI
launchd déclenchait un job par créneau, sur une machine qui pouvait dormir : un créneau
manqué n'était jamais rejoué, et rien ne le signalait. Ici, un workflow par cadence
appelle ce script, qui lance TOUS les collecteurs de la cadence. Un runner ne dort pas ;
s'il échoue, la tentative suivante arrive à l'heure dite.

TROIS PRINCIPES
1. Un collecteur qui échoue n'empêche jamais les autres de publier. Chacun est isolé,
   borné dans le temps, et son échec est une ligne du bilan — pas l'arrêt du lot.
2. On ne publie que ce qui a VRAIMENT changé, comparé par contenu. Un horodatage frais
   sur une donnée identique est un mensonge, et c'est exactement ce que faisait l'ancienne
   chaîne quand elle republiait un dépôt à moitié synchronisé.
3. Le bilan (`cache/_fleet_status.json`) est écrit à CHAQUE passage, succès ou échec.
   Une panne muette est pire qu'une panne visible : c'est ce qui a laissé un collecteur
   mort pendant 109 heures sans que personne ne le voie.
"""

import argparse
import concurrent.futures as cf
import filecmp
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
SCRIPTS = os.path.join(ROOT, "scripts")
CACHE_OUT = os.path.join(ROOT, "cache")

# Seuil de séparation entre les deux régimes de publication. 12 fichiers pèsent
# 93 des 114 Mo du parc : versionner ceux-là à chaque passage ferait grossir le dépôt
# sans fin, pour une donnée dont personne ne relira jamais la version d'avant-hier.
# En dessous, l'historique git est au contraire précieux — on voit quelle valeur a
# changé, et quand.
GIT_SIZE_LIMIT = 1_000_000
RELEASE_OUT = os.path.join(ROOT, "release")
MANIFEST = os.path.join(ROOT, "cache_manifest.txt")
JOBS = os.path.join(ROOT, "jobs.json")

# Là où les collecteurs écrivent, tel qu'ils l'ont toujours fait. Sous Linux, ces
# chemins n'ont rien de spécial : ce sont de simples dossiers qu'on crée.
CACHE_DIR = os.path.expanduser("~/Library/Caches/site_crypto_finance")
SITE_DIR = os.path.expanduser("~/Desktop/Site_Crypto_Finance")

# Cadences. Un job tombe dans le PREMIER seau dont le seuil est atteint.
# Le plafond de temps est dimensionné sur le collecteur le plus lent du seau
# (le lot tradfi met ~50 min) : trop court, on tue un collecteur sain en plein
# travail — l'erreur exacte qui a fait perdre des heures de réparation au watchdog.
BUCKETS = [
    ("10min", 96, 8),
    ("30min", 24, 20),
    ("2h", 8, 40),
    ("6h", 3, 50),
    ("12h", 1.5, 60),
    ("daily", 0.9, 70),
    ("weekly", 0.0, 70),
]
PARALLEL = 6


def bucket_of(per_day):
    for name, threshold, _ in BUCKETS:
        if per_day >= threshold:
            return name
    return "weekly"


def timeout_of(bucket):
    return next(t for n, _, t in BUCKETS if n == bucket) * 60


def prepare_env():
    """Recrée l'arborescence que les collecteurs attendent."""
    for d in (CACHE_DIR, SITE_DIR, CACHE_OUT):
        os.makedirs(d, exist_ok=True)
    # Les collecteurs relisent souvent leur propre cache précédent (fusion, historique,
    # préservation en cas d'échec partiel). Sans cette copie, chaque exécution repartirait
    # de zéro et perdrait l'historique accumulé — et un collecteur dont la source est
    # momentanément muette écraserait ses données au lieu de les conserver.
    restored = 0
    for source in (CACHE_OUT, RELEASE_OUT):
        if not os.path.isdir(source):
            continue
        for name in os.listdir(source):
            src = os.path.join(source, name)
            if not os.path.isfile(src):
                continue
            if not os.path.exists(os.path.join(CACHE_DIR, name)):
                shutil.copy2(src, os.path.join(CACHE_DIR, name))
                restored += 1
            # Sur la machine d'origine, plusieurs collecteurs relisent leur cache
            # précédent À CÔTÉ D'EUX, pas dans le dossier des caches (les deux copies
            # y cohabitent depuis toujours). On reproduit cette disposition, sinon ces
            # collecteurs repartiraient de zéro à chaque exécution — en perdant
            # l'historique qu'ils accumulent, sans que rien ne le signale.
            jumeau = os.path.join(SCRIPTS, name)
            if not os.path.exists(jumeau):
                shutil.copy2(src, jumeau)
    return restored


def _sans_chemin_perso(msg):
    """Retire le dossier personnel des messages d'erreur avant publication.

    Le bilan est publié dans un dépôt PUBLIC, et un message d'échec cite volontiers
    le chemin complet du fichier fautif — donc le nom du compte de la machine. C'est
    arrivé au premier essai réel : « univers introuvable (/Users/<compte>/…) ».
    Le message reste lisible, il perd juste ce qu'il n'avait pas à dire.
    """
    return (msg or "").replace(os.path.expanduser("~"), "~")


def run_one(job, timeout):
    script = os.path.join(SCRIPTS, job["script"])
    if not os.path.exists(script):
        return dict(job=job["id"], ok=False, secs=0, code=None, why="script absent")

    cmd = (["bash", script] if script.endswith(".sh")
           else [sys.executable, script])
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=SCRIPTS, capture_output=True, text=True,
                           timeout=timeout, env=os.environ.copy())
        secs = round(time.time() - t0, 1)
        ok = p.returncode == 0
        why = "" if ok else (p.stderr or p.stdout or "").strip().splitlines()[-1:] or [""]
        return dict(job=job["id"], ok=ok, secs=secs, code=p.returncode,
                    why="" if ok else _sans_chemin_perso(str(why[0]))[:200])
    except subprocess.TimeoutExpired:
        return dict(job=job["id"], ok=False, secs=round(time.time() - t0, 1),
                    code=None, why=f"dépassement du plafond ({timeout//60} min)")
    except Exception as e:
        return dict(job=job["id"], ok=False, secs=round(time.time() - t0, 1),
                    code=None, why=_sans_chemin_perso(f"{type(e).__name__}: {e}")[:200])


def collect(manifest):
    """Range les fichiers du manifeste qui ont RÉELLEMENT changé, selon leur poids.

    La comparaison se fait par CONTENU, jamais par date : un collecteur réécrit
    souvent un fichier identique (données inchangées depuis la veille), et se fier
    à la date de modification produirait une publication à chaque passage — du bruit
    qui noie les vrais changements dans l'historique.

    Petits fichiers → `cache/`, versionnés par git.
    Gros fichiers   → `release/`, publiés en pièces jointes remplacées sur place.
    Le comparant reste le même dans les deux cas : la copie précédente, où qu'elle soit.
    """
    os.makedirs(RELEASE_OUT, exist_ok=True)
    small, big, absent = [], [], []
    for name in manifest:
        src = os.path.join(CACHE_DIR, name)
        if not os.path.exists(src):
            absent.append(name)
            continue
        heavy = os.path.getsize(src) >= GIT_SIZE_LIMIT
        dst = os.path.join(RELEASE_OUT if heavy else CACHE_OUT, name)

        # Un fichier peut changer de camp (il grossit avec l'historique qu'il accumule) :
        # on nettoie l'ancienne place, sinon le site continuerait de lire une copie
        # figée pendant que la nouvelle est publiée ailleurs.
        stale = os.path.join(CACHE_OUT if heavy else RELEASE_OUT, name)
        if os.path.exists(stale):
            os.remove(stale)

        if os.path.exists(dst) and filecmp.cmp(src, dst, shallow=False):
            continue
        shutil.copy2(src, dst)
        (big if heavy else small).append(name)
    return small, big, absent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True, choices=[b[0] for b in BUCKETS])
    ap.add_argument("--only", help="un seul job, pour tester (étiquette exacte)")
    ap.add_argument("--list", action="store_true", help="affiche la répartition et sort")
    args = ap.parse_args()

    jobs = [j for j in json.load(open(JOBS))["jobs"] if j["category"] == "public"]

    if args.list:
        for name, _, mins in BUCKETS:
            sel = [j for j in jobs if bucket_of(j["per_day"]) == name]
            print(f"{name:7} {len(sel):3} collecteurs  (plafond {mins} min)")
            for j in sorted(sel, key=lambda j: -j["per_day"]):
                print(f"          {j['per_day']:6.1f}/j  {j['id']:26} {j['script']}")
        return 0

    due = [j for j in jobs if bucket_of(j["per_day"]) == args.bucket]
    if args.only:
        due = [j for j in jobs if j["id"] == args.only]
        if not due:
            print(f"Aucun job « {args.only} »", file=sys.stderr)
            return 1

    manifest = [l.strip() for l in open(MANIFEST) if l.strip()] if os.path.exists(MANIFEST) else []
    restored = prepare_env()
    timeout = timeout_of(args.bucket)

    print(f"cadence « {args.bucket} » · {len(due)} collecteurs · plafond {timeout//60} min "
          f"· {restored} cache(s) restauré(s)\n")

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        results = list(ex.map(lambda j: run_one(j, timeout), due))
    elapsed = round(time.time() - t0, 1)

    small, big, absent = collect(manifest)
    changed = small + big

    ko = [r for r in results if not r["ok"]]
    for r in sorted(results, key=lambda r: (r["ok"], -r["secs"])):
        mark = "✓" if r["ok"] else "✗"
        print(f"{mark} {r['secs']:6.1f}s  {r['job']:28} {r['why']}")

    print(f"\n{len(results)-len(ko)}/{len(results)} collecteurs OK en {elapsed}s "
          f"· {len(changed)} fichier(s) modifié(s) : {len(small)} versionné(s), "
          f"{len(big)} en pièce jointe")
    if changed:
        print("  " + ", ".join(changed[:12]) + (" …" if len(changed) > 12 else ""))
    if absent:
        # Un fichier attendu par le site que personne ne produit : ni erreur bruyante
        # ni silence — la page servirait une donnée figée sans que rien ne l'indique.
        print(f"  {len(absent)} fichier(s) du manifeste jamais produit(s) : "
              + ", ".join(absent[:8]) + (" …" if len(absent) > 8 else ""))

    # Bilan cumulatif : on garde l'état des cadences qui n'ont pas tourné cette fois-ci,
    # sinon chaque passage effacerait la vue d'ensemble du parc.
    status_path = os.path.join(CACHE_OUT, "_fleet_status.json")
    status = {}
    if os.path.exists(status_path):
        try:
            status = json.load(open(status_path))
        except (json.JSONDecodeError, OSError):
            status = {}
    status.setdefault("buckets", {})[args.bucket] = dict(
        ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        total=len(results), ok=len(results) - len(ko), secs=elapsed,
        changed=len(changed), versionnes=len(small), pieces_jointes=len(big),
        absents=len(absent), failed=[dict(job=r["job"], why=r["why"]) for r in ko])
    status["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(status_path, "w") as f:
        json.dump(status, f, indent=1, ensure_ascii=False)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"### {args.bucket} — {len(results)-len(ko)}/{len(results)} OK · "
                    f"{len(changed)} fichier(s) modifié(s) · {elapsed}s\n\n")
            if ko:
                f.write("| Collecteur en échec | Raison |\n|---|---|\n")
                for r in ko:
                    f.write(f"| {r["job"]} | {r['why'][:120]} |\n")

    # Toujours 0 : l'échec d'un collecteur ne doit pas empêcher la publication des
    # autres. Les échecs se lisent dans le bilan, qui est fait pour ça.
    return 0


if __name__ == "__main__":
    sys.exit(main())

# migration : declencheur temporaire (rejoue une collecte reelle apres correction)
# relance apres correction du verrou
