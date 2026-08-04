#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_scripts.py — Copie les collecteurs de la machine d'origine vers `scripts/`,
en les rendant publiables et portables. Sens UNIQUE : machine → dépôt. Jamais l'inverse.

DEUX PROBLÈMES À RÉGLER EN MÊME TEMPS

1. Le dépôt est public. Les scripts contiennent le chemin du compte de l'utilisateur
   (« /Users/<compte>/… ») dans 31 fichiers. Publier ça révèle une identité pour rien.

2. Les runners tournent sous Linux, où « /Users/<compte> » n'existe pas.

Une même réécriture résout les deux : le chemin absolu devient un chemin relatif au
dossier personnel (`expanduser("~/…")` en Python, `$HOME/…` en shell). Sur le Mac le
comportement est rigoureusement identique ; sur un runner, il pointe vers /home/runner,
qu'on aura créé. Aucun script de la machine n'est modifié : ce dossier est un MIROIR
GÉNÉRÉ, régénérable à tout moment.

TOUT RÉSIDU EST SIGNALÉ, JAMAIS AVALÉ. Un fichier qui contient encore un chemin absolu
après réécriture est refusé et listé : mieux vaut un import incomplet qu'une fuite
silencieuse. C'est le même principe que le verrou anti-secrets du script de déploiement
— on vérifie AVANT de publier, parce qu'après, c'est public.
"""

import json
import os
import re
import shutil
import sys

SRC = os.path.expanduser("~/Library/Application Support/SiteCryptoFinance")
HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "..", "scripts")
JOBS = os.path.join(HERE, "..", "jobs.json")

HOME = os.path.expanduser("~")          # /Users/<compte>
ACCOUNT = os.path.basename(HOME)

# Le compte n'apparaît pas que dans les chemins : les collecteurs citent leur étiquette
# launchd (« com.<compte>.treasury ») dans leurs commentaires d'en-tête. C'est la même
# fuite d'identité, sous une autre forme — on la neutralise en gardant le nom du job,
# qui est l'information utile.
LABEL_RULE = (rf"com\.{re.escape(ACCOUNT)}\.", "scf.")

# La SEC EXIGE un User-Agent nominatif avec une adresse de contact, sinon elle renvoie
# 403. Quatre collecteurs portent donc en clair le nom et l'e-mail de l'utilisateur.
# On ne peut ni les supprimer (la source refuserait) ni les publier (adresse récoltée
# par les robots dès l'indexation) : la valeur passe par l'environnement, alimentée
# par un secret GitHub, avec un repli neutre qui garde le script exécutable en local.
EMAIL_IN_STRING = re.compile(r'"[^"\n]*[\w.+-]+@[\w-]+\.[\w.]+[^"\n]*"')
CONTACT_ENV_PY = 'os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research")'
CONTACT_ENV_SH = '"${SCF_CONTACT_UA:-CapitalAntifragile research}"'

# Résidus de sessions de travail : chemins de dossiers temporaires éphémères laissés
# dans des listes de candidats. Ils n'existent plus, la ligne ne sert plus à rien, et
# elle porte le nom du compte. On retire l'entrée entière — les autres candidats et
# la variable d'environnement dédiée assurent déjà le repli.
SCRATCH_ENTRY = re.compile(r'^[ \t]*"/private/tmp/[^"\n]*",[ \t]*\n', re.M)

# Réécritures, appliquées dans l'ordre. Le shell et Python n'ont pas la même syntaxe
# pour « mon dossier personnel », d'où deux jeux de règles.
PY_RULES = [
    (rf'"{re.escape(HOME)}/([^"]*)"', r'os.path.expanduser("~/\1")'),
    (rf"'{re.escape(HOME)}/([^']*)'", r"os.path.expanduser('~/\1')"),
    # Chemin nu dans une f-string ou une concaténation : plus rare, traité au cas par cas.
    (re.escape(HOME) + r"/", "~/"),
]
SH_RULES = [
    (re.escape(HOME) + r"/", "$HOME/"),
    (re.escape(HOME) + r"\b", "$HOME"),
    # Interpréteur figé du Mac → celui du système hôte.
    (r"/Library/Frameworks/Python\.framework/Versions/3\.\d+/bin/python3", "python3"),
    (r"/usr/local/bin/python3|/opt/homebrew/bin/python3", "python3"),
]

# Les scripts qui n'ont rien à faire dans un dépôt public, quelle que soit leur
# catégorie dans jobs.json : ils portent des données personnelles ou des secrets.
NEVER = re.compile(r"portefeuille|advisor|souv|savoir|grind|carte\.sh|links_runner|"
                   r"deploy_public|snapshot_site|fast_publish|sync_prod|wake_kicker|"
                   r"watchdog|syncthing|siteserver")

# Un import ne doit jamais emporter un secret par accident.
SECRET_HINT = re.compile(
    r"(?:api[_-]?key|token|secret|password|bearer)\s*[:=]\s*[\"'][A-Za-z0-9_\-]{16,}[\"']",
    re.I)

# Le piège que la règle ci-dessus ne voyait PAS : une clé placée en valeur par défaut
# d'une lecture d'environnement — `os.environ.get("FRED_API_KEY", "1410…")`. La forme
# est rassurante (« ça vient de l'environnement »), mais le littéral est bien dans le
# fichier, et une vraie clé FRED a failli partir en public à cause d'elle.
# On vide la valeur par défaut : le script reste exécutable, et l'absence de clé se
# manifeste par un refus franc de la source plutôt que par une fuite.
ENV_DEFAULT_SECRET = re.compile(
    r'(os\.environ\.get\(\s*["\'](?:[A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z_]*)["\']\s*,\s*)'
    r'["\'][A-Za-z0-9_\-]{16,}["\']')


def scrub_prose(text):
    """Retire le nom du compte des COMMENTAIRES (« enregistrée par <compte> le … »).

    Volontairement limité à ce qui suit un `#` : remplacer le mot partout dans le
    fichier risquerait de renommer un identifiant et de casser le script en silence.
    Ce qui échapperait à cette règle reste attrapé par le contrôle final, qui refuse
    le fichier — un import incomplet se voit, une fuite non.
    """
    out = []
    for line in text.split("\n"):
        i = line.find("#")
        if i >= 0:
            line = line[:i] + re.sub(rf"\b{re.escape(ACCOUNT)}\b", "l'auteur",
                                     line[i:], flags=re.I)
        out.append(line)
    return "\n".join(out)


def rewrite(text, is_shell):
    text = ENV_DEFAULT_SECRET.sub(r'\1""', text)
    text = scrub_prose(text)
    text = SCRATCH_ENTRY.sub("", text)
    text = EMAIL_IN_STRING.sub(CONTACT_ENV_SH if is_shell else CONTACT_ENV_PY, text)
    for pat, rep in (SH_RULES if is_shell else PY_RULES):
        text = re.sub(pat, rep, text)
    return re.sub(LABEL_RULE[0], LABEL_RULE[1], text)


def needs_os_import(text):
    """Une réécriture Python introduit `os.path.expanduser` : le module doit être importé."""
    return "os.path.expanduser" in text and not re.search(r"^import os\b|^import os,", text, re.M)


def add_os_import(text):
    """Insère `import os` juste avant le premier import de premier niveau.

    Quatre collecteurs n'importent pas `os` (ils passent par `pathlib`) : la réécriture
    y introduirait un appel à un module absent, donc une panne au tout premier run.
    On l'insère AVANT le premier import plutôt qu'après le dernier — un import peut se
    trouver dans un `try:` ou après du code conditionnel, alors que le premier import
    de colonne zéro marque toujours le haut du fichier, sous l'en-tête et les docstrings.
    """
    m = re.search(r"^(?:import|from)\s+\w", text, re.M)
    if not m:
        return None                      # forme inattendue : on préfère refuser
    return text[:m.start()] + "import os\n" + text[m.start():]


def with_local_modules(names):
    """Complète la liste avec les modules internes que les collecteurs s'importent
    entre eux, en suivant la chaîne jusqu'au bout.

    `jobs.json` ne connaît que les scripts LANCÉS par un job. Or 18 collecteurs
    importent `_fred_helpers`, qui n'est lancé par personne : sans ce parcours, le
    miroir serait syntaxiquement parfait et planterait à l'exécution sur un
    ModuleNotFoundError. On boucle parce qu'un module importé peut lui-même en
    importer un autre.
    """
    seen, queue = set(names), list(names)
    imp = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_]\w*)", re.M)
    while queue:
        cur = queue.pop()
        path = os.path.join(SRC, cur)
        if not cur.endswith(".py") or not os.path.exists(path):
            continue
        for mod in set(imp.findall(open(path, encoding="utf-8", errors="replace").read())):
            cand = mod + ".py"
            if cand not in seen and os.path.exists(os.path.join(SRC, cand)):
                seen.add(cand)
                queue.append(cand)
    return sorted(seen)


def main():
    argv = sys.argv[1:]
    dry = "--dry-run" in argv

    if not os.path.isdir(SRC):
        print(f"Source introuvable : {SRC}", file=sys.stderr)
        return 1
    if not os.path.exists(JOBS):
        print(f"{JOBS} manquant — lancer d'abord tools/inventory.py", file=sys.stderr)
        return 1

    jobs = json.load(open(JOBS))["jobs"]
    wanted = sorted({j["script"] for j in jobs
                     if j["category"] == "public" and j["script"]})
    wanted = with_local_modules(wanted)

    os.makedirs(DEST, exist_ok=True)
    copied, skipped, refused, missing, fixed_imports = [], [], [], [], []

    for name in wanted:
        if NEVER.search(name):
            skipped.append((name, "liste d'exclusion"))
            continue
        src = os.path.join(SRC, name)
        if not os.path.exists(src):
            missing.append(name)
            continue

        raw = open(src, encoding="utf-8", errors="replace").read()

        if SECRET_HINT.search(raw):
            refused.append((name, "ressemble à un secret en dur"))
            continue

        out = rewrite(raw, name.endswith(".sh"))

        # Contrôle final : plus AUCUNE trace du compte, sous aucune forme.
        residue = [m.start() for m in re.finditer(re.escape(ACCOUNT), out)]
        if residue:
            line = out[:residue[0]].count("\n") + 1
            refused.append((name, f"chemin personnel résiduel ligne {line}"))
            continue

        if not name.endswith(".sh") and needs_os_import(out):
            patched = add_os_import(out)
            if patched is None:
                refused.append((name, "réécriture introduit os.path sans `import os`"))
                continue
            out = patched
            fixed_imports.append(name)

        if not dry:
            with open(os.path.join(DEST, name), "w", encoding="utf-8") as f:
                f.write(out)
            shutil.copymode(src, os.path.join(DEST, name))
        copied.append(name)

    # ── Fichiers d'ENTRÉE ────────────────────────────────────────────────────
    # Certains collecteurs ne lisent pas que des sources distantes : ils s'appuient
    # sur un fichier de référence posé à côté d'eux (l'univers d'actions suivi, par
    # exemple). Sans lui, le collecteur démarre et meurt sur « fichier introuvable » —
    # constaté en conditions réelles avec `stock_universe.json`.
    # On n'emporte QUE les entrées : les fichiers du manifeste sont des SORTIES,
    # restaurées ailleurs par l'orchestrateur, et les embarquer ici en ferait deux
    # copies concurrentes de la même donnée.
    manifeste = set()
    chemin_manifeste = os.path.join(HERE, "..", "cache_manifest.txt")
    if os.path.exists(chemin_manifeste):
        manifeste = {l.strip() for l in open(chemin_manifeste) if l.strip()}

    motif = re.compile(r'["\']([\w\-.]+\.(?:json|csv|txt))["\']')
    entrees = set()
    for name in copied:
        chemin = os.path.join(SRC, name)
        if not os.path.exists(chemin):
            continue
        for m in motif.finditer(open(chemin, encoding="utf-8", errors="replace").read()):
            f = m.group(1)
            if f not in manifeste and os.path.exists(os.path.join(SRC, f)):
                entrees.add(f)

    emportees = []
    for f in sorted(entrees):
        contenu = open(os.path.join(SRC, f), encoding="utf-8", errors="replace").read()
        if ACCOUNT.lower() in contenu.lower() or SECRET_HINT.search(contenu):
            refused.append((f, "fichier d'entrée contenant une donnée personnelle"))
            continue
        if not dry:
            shutil.copy2(os.path.join(SRC, f), os.path.join(DEST, f))
        emportees.append(f)

    print(f"{'[simulation] ' if dry else ''}collecteurs publics : {len(wanted)} attendus")
    if emportees:
        print(f"  entrées   {len(emportees)}   ({', '.join(emportees)})")
    print(f"  importés  {len(copied)}")
    print(f"  écartés   {len(skipped)}   (hors périmètre public)")
    print(f"  REFUSÉS   {len(refused)}   (à traiter à la main)")
    print(f"  absents   {len(missing)}")
    if fixed_imports:
        print(f"  `import os` ajouté dans {len(fixed_imports)} fichier(s) : " + ", ".join(fixed_imports))

    for name, why in refused:
        print(f"    ✗ {name:44} {why}")
    for name in missing:
        print(f"    ? {name:44} introuvable dans la source")
    return 0


if __name__ == "__main__":
    sys.exit(main())
