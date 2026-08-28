#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
derive_collecteurs.py — Ce qui TOURNE est-il bien ce qui est ÉCRIT ?

LE PIÈGE QU'IL FERME
`scripts/` est un MIROIR GÉNÉRÉ. import_scripts.py le dit en toutes lettres : « Sens
UNIQUE : machine → dépôt. Jamais l'inverse. » Éditer un collecteur ici ne change donc
RIEN à ce que launchd exécute — et ça ne se voit nulle part. Le fichier est bien
modifié, la modification est bien commitée, elle part bien sur GitHub. Elle n'arrive
simplement jamais sur la machine qui collecte.

CE QUE ÇA A COÛTÉ, LE 28/08/2026
Le framework « captation de valeur » (584 lignes) a été écrit dans le miroir. Lancé à
la main, il a produit un cache complet à 14 h 07 : la page était juste. À 23 h 30, le
launchd des 4 h a lancé le VRAI collecteur — celui du 20 août, qui ignore la captation
— et l'a réécrit sans elle. Le bandeau « Feed health » a alors accusé DefiLlama, qui
répondait parfaitement, et proposé de relancer ce même binaire périmé : le geste de
réparation aurait ré-effacé la donnée à chaque essai.

Ce que ce contrôle regarde n'est PAS le contenu des collectes — c'est le code qui les
produit. Un cache peut être frais, daté, complet, et néanmoins produit par un binaire
d'il y a huit jours.

LECTURE DU VERDICT
  · MIROIR EN AVANCE  → du travail écrit ici n'a jamais atteint la machine. C'est le
                        cas grave : la panne est déjà armée, elle attend le prochain
                        passage du planificateur.
  · MIROIR EN RETARD  → bénin. La machine a évolué, le dépôt ne le sait pas encore :
                        `python3 tools/import_scripts.py` remet à niveau.

Le contrôle tourne indifféremment sur le Mac (où il compare pour de vrai) ou sur un
poste qui reçoit `Application Support` par Syncthing.

Lancement : python3 tools/derive_collecteurs.py [--tout]
Sortie    : 0 si rien n'est en avance, 1 sinon (utilisable en garde-fou).
"""

import collections
import difflib
import io
import json
import os
import re
import sys

MACHINE = os.path.expanduser("~/Library/Application Support/SiteCryptoFinance")
ICI = os.path.dirname(os.path.abspath(__file__))
MIROIR = os.path.join(ICI, "..", "scripts")
JOBS = os.path.join(ICI, "..", "jobs.json")

# Au-delà de ce nombre de lignes que le miroir a en plus, ce n'est plus un écart de
# réécriture : c'est du travail. Cinq lignes, parce que la normalisation ci-dessous
# laisse passer un ou deux résidus (un import ajouté, une ligne de repli).
SEUIL_TRAVAIL = 5


# ── NORMALISATION ────────────────────────────────────────────────────────────
# import_scripts.py réécrit les fichiers en les publiant : chemin du compte, $HOME,
# interpréteur figé du Mac, étiquette launchd, secrets vidés. Comparer sans annuler
# ces réécritures ferait clignoter les 129 collecteurs en permanence — et un contrôle
# qui crie toujours ne dit plus rien. On annule donc TOUT ce qu'import_scripts fait,
# et rien d'autre : ce qui reste est un vrai écart de code.
RE_COMPTE = re.compile(r"/Users/([A-Za-z0-9._-]+)/")
RE_EXPAND_D = re.compile(r'os\.path\.expanduser\(\s*"~/([^"]*)"\s*\)')
RE_EXPAND_S = re.compile(r"os\.path\.expanduser\(\s*'~/([^']*)'\s*\)")
RE_PY_MAC = re.compile(r"/Library/Frameworks/Python\.framework/Versions/3\.\d+/bin/python3"
                       r"|/usr/local/bin/python3|/opt/homebrew/bin/python3"
                       r"|/opt/anaconda3/bin/python3?|/opt/miniconda3/bin/python3?")
RE_CAFFEINATE = re.compile(r"/usr/bin/caffeinate -[a-z]* ")
RE_LABEL = re.compile(r"com\.[A-Za-z0-9._-]+\.")
RE_SECRET_PY = re.compile(r'(os\.environ\.get\(\s*["\'][A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD)'
                          r'[A-Z_]*["\']\s*,\s*)["\'][^"\']*["\']')
RE_SECRET_SH = re.compile(r'(\$\{[A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z_]*:-)[^}]*(\})')
RE_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')
RE_CONTACT = re.compile(r'os\.environ\.get\(\s*"SCF_CONTACT_UA"\s*,\s*"[^"]*"\s*\)'
                        r'|\$\{SCF_CONTACT_UA:-[^}]*\}')


def normaliser(texte, compte):
    if compte:
        texte = re.sub(r"/Users/%s/" % re.escape(compte), "~/", texte)
    texte = RE_COMPTE.sub("~/", texte)
    texte = texte.replace("$HOME/", "~/").replace("${HOME}/", "~/")
    texte = RE_EXPAND_D.sub(r'"~/\1"', texte)
    texte = RE_EXPAND_S.sub(r'"~/\1"', texte)
    texte = RE_PY_MAC.sub("python3", texte)
    texte = RE_CAFFEINATE.sub("", texte)
    texte = RE_LABEL.sub("scf.", texte)
    texte = RE_CONTACT.sub('"<contact>"', texte)
    texte = RE_EMAIL.sub("<contact>", texte)
    texte = RE_SECRET_PY.sub(r'\1""', texte)
    texte = RE_SECRET_SH.sub(r"\1\2", texte)
    if compte:
        texte = re.sub(r"\b%s\b" % re.escape(compte), "l'auteur", texte, flags=re.I)
    lignes = []
    for ligne in texte.split("\n"):
        ligne = ligne.rstrip()
        # `import os` est ajouté par la publication quand elle introduit un
        # expanduser : sa présence d'un seul côté n'est pas un écart de code.
        if ligne == "import os":
            continue
        lignes.append(ligne)
    while lignes and not lignes[-1]:
        lignes.pop()
    return lignes


def lire(chemin):
    try:
        return io.open(chemin, encoding="utf-8", errors="replace").read()
    except OSError:
        return None


def detecter_compte():
    """Le nom du compte se lit dans les fichiers de la machine, pas dans le HOME
    local : ce contrôle doit pouvoir tourner depuis un autre poste que le Mac."""
    comptes = collections.Counter()
    if not os.path.isdir(MACHINE):
        return None
    for nom in os.listdir(MACHINE)[:400]:
        if not (nom.endswith(".py") or nom.endswith(".sh")):
            continue
        texte = lire(os.path.join(MACHINE, nom))
        if texte:
            comptes.update(RE_COMPTE.findall(texte))
    return comptes.most_common(1)[0][0] if comptes else None


def jobs_par_script():
    """id de job → script, pour dire quel planificateur exécute le fichier fautif."""
    try:
        jobs = json.load(io.open(JOBS, encoding="utf-8"))["jobs"]
    except (OSError, ValueError, KeyError):
        return {}
    par_script = collections.defaultdict(list)
    for j in jobs:
        if j.get("script"):
            par_script[j["script"]].append("%s (%s)" % (j["id"], j.get("schedule") or "?"))
    return par_script


def main():
    tout = "--tout" in sys.argv

    if not os.path.isdir(MACHINE):
        print("Le dossier des collecteurs de la machine est introuvable :")
        print("  %s" % MACHINE)
        print("\nCe contrôle a besoin de VOIR ce qui tourne. Il se lance sur le Mac,")
        print("ou sur un poste qui reçoit ce dossier par Syncthing.")
        return 0

    compte = detecter_compte()
    par_script = jobs_par_script()

    identiques, absents_machine, ecarts = 0, [], []
    for nom in sorted(os.listdir(MIROIR)):
        if not (nom.endswith(".py") or nom.endswith(".sh")):
            continue
        brut_machine = lire(os.path.join(MACHINE, nom))
        brut_miroir = lire(os.path.join(MIROIR, nom))
        if brut_machine is None:
            absents_machine.append(nom)
            continue
        a = normaliser(brut_machine, compte)
        b = normaliser(brut_miroir, compte)
        if a == b:
            identiques += 1
            continue
        ajoutees = supprimees = 0
        for ligne in difflib.ndiff(a, b):
            if ligne.startswith("+ "):
                ajoutees += 1
            elif ligne.startswith("- "):
                supprimees += 1
        ecarts.append((nom, len(a), len(b), ajoutees, supprimees))

    en_avance = [e for e in ecarts if e[3] - e[4] >= SEUIL_TRAVAIL]
    en_retard = [e for e in ecarts if e[4] - e[3] >= SEUIL_TRAVAIL]
    mineurs = [e for e in ecarts if e not in en_avance and e not in en_retard]

    print("Collecteurs comparés (machine ↔ miroir du dépôt)")
    print("  compte de la machine     : %s" % (compte or "indéterminé"))
    print("  identiques               : %d" % identiques)
    print("  écarts mineurs           : %d" % len(mineurs))
    print("  miroir en retard         : %d  (bénin — réimporter)" % len(en_retard))
    print("  MIROIR EN AVANCE         : %d" % len(en_avance))

    if en_avance:
        print("\n" + "=" * 72)
        print("ALERTE — du travail écrit dans le dépôt n'a jamais atteint la machine.")
        print("Ces collecteurs tournent dans une version ANCIENNE. Ce qui est écrit ici")
        print("ne s'exécute nulle part, et la page servira ce que l'ancienne version")
        print("sait produire — sans le dire.")
        print("=" * 72)
        for nom, la, lb, plus, moins in sorted(en_avance, key=lambda e: e[4] - e[3]):
            print("\n  %s" % nom)
            print("    machine %d lignes → miroir %d lignes  (+%d / -%d)"
                  % (la, lb, plus, moins))
            for job in par_script.get(nom, []):
                print("    lancé par : %s" % job)
            print("    déployer  : cp \"%s/%s\" \\\n                   \"%s/%s\""
                  % (os.path.relpath(MIROIR, os.getcwd()), nom, MACHINE, nom))

    if en_retard and tout:
        print("\n— Miroir en retard (la machine a évolué, le dépôt l'ignore) —")
        for nom, la, lb, plus, moins in en_retard:
            print("  %-42s machine %d → miroir %d" % (nom, la, lb))
        print("  Remise à niveau : python3 tools/import_scripts.py")

    if mineurs and tout:
        print("\n— Écarts mineurs (à regarder si un doute persiste) —")
        for nom, la, lb, plus, moins in mineurs:
            print("  %-42s +%d / -%d" % (nom, plus, moins))

    if absents_machine:
        print("\n%d script(s) du dépôt absents de la machine — collecte hors Mac ou"
              % len(absents_machine))
        print("script retiré. Ce n'est pas une dérive : rien à déployer.")
        if tout:
            for nom in absents_machine:
                print("  %s" % nom)

    if not en_avance:
        print("\nRien en avance : ce qui tourne est bien ce qui est écrit.")
    return 1 if en_avance else 0


if __name__ == "__main__":
    sys.exit(main())
