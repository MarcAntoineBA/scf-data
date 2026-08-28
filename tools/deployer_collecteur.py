#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deployer_collecteur.py — Le retour manquant : dépôt → machine.

`import_scripts.py` descend les collecteurs de la machine vers le dépôt, et le dit
sans ambiguïté : « Sens UNIQUE : machine → dépôt. Jamais l'inverse. » C'était vrai
tant que le dépôt n'était qu'une vitrine. Il ne l'est plus : on y travaille, et ce
travail doit REDESCENDRE — sinon il n'existe nulle part. Le 28/08/2026, sept
collecteurs étaient dans ce cas ; la page Valorisation L1 a perdu un framework
entier pendant que sa version complète dormait dans le dépôt, et le bandeau de la
page a accusé la source.

POURQUOI CE N'EST PAS UN `cp`
Publier ABÎME volontairement les fichiers, et il faut défaire exactement ces
blessures-là avant de les rendre à la machine :

  · le contact nominatif — « CapitalAntifragile research (<adresse>) » — devient une
    lecture d'environnement avec un repli neutre. Sur un runner GitHub, le secret
    remplit la variable ; sur le Mac, la variable n'existe pas. Le collecteur
    perdrait son identité, et la SEC répond 403 sans elle.
  · les valeurs de repli des clés d'API sont VIDÉES (une vraie clé FRED a failli
    partir en public par cette porte).
  · le chemin du compte, l'interpréteur figé du Mac, l'étiquette launchd.

Rendre le fichier publié tel quel réparerait la fonctionnalité en cassant la
collecte.

COMMENT
On aligne les deux versions sur leur forme NORMALISÉE — celle où les réécritures de
publication n'existent plus. Là où les deux disent la même chose, on garde la ligne
de la MACHINE : c'est le même code, avec ses secrets intacts. Là où elles diffèrent
vraiment, on prend celle du dépôt : c'est le travail à déployer.

CE QUI L'ARRÊTE (aucun déploiement n'est « forcé »)
  · le dépôt a PERDU du code que la machine avait — la dérive va dans les deux sens,
    et écraser vaudrait régression ;
  · un marqueur sensible de la machine ne se retrouve pas dans le résultat et n'a
    pas pu être restitué ;
  · le résultat ne compile pas.
Un refus laisse la machine intacte et dit pourquoi.

Lancement :
  python3 tools/deployer_collecteur.py --a-blanc          # tout ce qui est en retard, sans écrire
  python3 tools/deployer_collecteur.py fetch_tradfi.py    # un fichier nommé
  python3 tools/deployer_collecteur.py --tous             # tout ce qui est en retard
"""

import difflib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time

ICI = os.path.dirname(os.path.abspath(__file__))
MACHINE = os.path.expanduser("~/Library/Application Support/SiteCryptoFinance")
MIROIR = os.path.join(ICI, "..", "scripts")

# On réutilise le détecteur : re-normaliser « presque pareil » ici finirait par
# diverger, et les deux outils cesseraient de parler du même écart.
_spec = importlib.util.spec_from_file_location("derive_collecteurs",
                                               os.path.join(ICI, "derive_collecteurs.py"))
_der = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_der)

# ── LES MARQUEURS QU'ON N'A PAS LE DROIT DE PERDRE ───────────────────────────
MARQUEURS = [
    ("adresse de contact", re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')),
    ("clé littérale", re.compile(r'(?:api[_-]?key|token|secret|password|bearer)\s*[:=]\s*'
                                 r'["\'][A-Za-z0-9_\-]{16,}["\']', re.I)),
    ("repli de clé", re.compile(r'os\.environ\.get\(\s*["\'][A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD)'
                                r'[A-Z_]*["\']\s*,\s*["\'][^"\']{8,}["\']')),
    ("repli de clé (shell)", re.compile(r'\$\{[A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z_]*:-[^}]{8,}\}')),
    ("chemin du compte", re.compile(r'/Users/[A-Za-z0-9._-]+/')),
    ("interpréteur", re.compile(r'/Library/Frameworks/Python\.framework|/opt/anaconda3'
                                r'|/opt/homebrew/bin/python|/usr/local/bin/python')),
    ("étiquette launchd", re.compile(r'com\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+')),
]

# Les réécritures de publication, dans le sens machine → dépôt : elles servent à
# reconnaître, dans le fichier publié, la ligne qui vient de celle de la machine.
EMAIL_DANS_CHAINE = re.compile(r'(?<![\w.])[fFrRbB]{0,2}"[^"\n]*[\w.+-]+@[\w-]+\.[\w.]+[^"\n]*"')
CONTACT_PY = 'os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research")'
CONTACT_SH = '"${SCF_CONTACT_UA:-CapitalAntifragile research}"'
REPLI_PY = re.compile(r'(os\.environ\.get\(\s*["\'][A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD)'
                      r'[A-Z_]*["\']\s*,\s*)["\'][A-Za-z0-9_\-]{16,}["\']')
REPLI_SH = re.compile(r'(\$\{[A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z_]*:-)[A-Za-z0-9_\-]{16,}(\})')
INTERPRETEUR = re.compile(r'/Library/Frameworks/Python\.framework/Versions/3\.\d+/bin/python3'
                          r'|/usr/local/bin/python3|/opt/homebrew/bin/python3'
                          r'|/opt/anaconda3/bin/python3?|/opt/miniconda3/bin/python3?')


def publier(ligne, compte, shell):
    """La ligne de la machine, telle que la publication l'aurait écrite."""
    ligne = EMAIL_DANS_CHAINE.sub(CONTACT_SH if shell else CONTACT_PY, ligne)
    ligne = REPLI_PY.sub(r'\1""', ligne)
    ligne = REPLI_SH.sub(r"\1\2", ligne)
    ligne = INTERPRETEUR.sub("python3", ligne)
    if compte:
        ligne = re.sub(r"com\.%s\." % re.escape(compte), "scf.", ligne)
        ligne = ligne.replace("/Users/%s/" % compte, "$HOME/" if shell else "~/")
        i = ligne.find("#")
        if i >= 0:
            ligne = ligne[:i] + re.sub(r"\b%s\b" % re.escape(compte), "l'auteur",
                                       ligne[i:], flags=re.I)
    return ligne


def lire(chemin):
    return io.open(chemin, encoding="utf-8", errors="replace").read()


def paires(brut, compte):
    """(lignes normalisées, lignes d'origine) — de MÊME longueur, indice par indice.

    C'est toute la mécanique du déploiement : on compare sur la gauche, on recolle
    avec la droite. Le jour où les deux listes cessent de se correspondre, le
    fichier produit serait un collage arbitraire — d'où le contrôle appelant.
    """
    norm = _der.normaliser(brut, compte, garder_import_os=True)
    orig = [l.rstrip() for l in brut.split("\n")]
    while orig and not orig[-1]:
        orig.pop()
    return norm, orig


IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{4,}\b")
BRUIT = {"return", "import", "print", "range", "value", "false", "class", "while",
         "lambda", "except", "append", "format", "float", "round"}


def code_perdu(brut_m, brut_r, compte):
    """Identifiants que la machine porte et que le dépôt ne connaît nulle part.

    Le témoin est l'IDENTIFIANT, pas la ligne : une reformulation le garde, une
    suppression l'emporte. On retire d'abord des lignes tout ce qui est un MARQUEUR
    — adresse, clé, chemin du compte : la publication les efface par construction et
    la fusion les restitue. Les compter comme du code perdu bloquerait tout
    déploiement pour une raison qui n'en est pas une.
    """
    perdus = []
    for ligne in difflib.ndiff(brut_m.splitlines(), brut_r.splitlines()):
        if not ligne.startswith("- "):
            continue
        code = ligne[2:].split("#")[0]
        for _, motif in MARQUEURS:
            code = motif.sub(" ", code)
        if compte:
            code = re.sub(r"\b%s\b" % re.escape(compte), " ", code, flags=re.I)
        for ident in IDENT.findall(code):
            if ident in BRUIT or ident in perdus:
                continue
            if not re.search(r"\b%s\b" % re.escape(ident), brut_r):
                perdus.append(ident)
    return perdus


def fusionner(brut_m, brut_r, compte, shell):
    """Le travail du dépôt, sur les lignes de la machine partout où c'est le même code."""
    nm, im = paires(brut_m, compte)
    nr, ir = paires(brut_r, compte)
    if len(nm) != len(im) or len(nr) != len(ir):
        return None, [], ["alignement rompu (normalisation non bijective)"]

    sortie = []
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, nm, nr,
                                                      autojunk=False).get_opcodes():
        if op == "equal":
            sortie += im[a1:a2]          # même code : la MACHINE, secrets intacts
        else:
            sortie += ir[b1:b2]          # vrai écart : le travail du dépôt

    # Filet : un marqueur de la machine perdu en route est restitué s'il est
    # reconnaissable, et bloque le déploiement sinon.
    restitutions, refus = [], []
    for etiquette, motif in MARQUEURS:
        for trouve in sorted(set(motif.findall(brut_m))):
            if trouve in "\n".join(sortie):
                continue
            restitue = False
            for ligne in brut_m.split("\n"):
                if trouve not in ligne:
                    continue
                cible = publier(ligne.rstrip(), compte, shell).strip()
                places = [i for i, l in enumerate(sortie) if l.strip() == cible]
                if len(places) == 1:
                    sortie[places[0]] = ligne.rstrip()
                    restitutions.append("%s → %s" % (etiquette, ligne.strip()[:84]))
                    restitue = True
                    break
            if not restitue and trouve not in "\n".join(sortie):
                refus.append("%s introuvable après fusion : %s" % (etiquette, trouve[:60]))
    return sortie, restitutions, refus


def compile_ok(chemin, shell):
    if shell:
        r = subprocess.run(["bash", "-n", chemin], capture_output=True, text=True)
    else:
        r = subprocess.run([sys.executable, "-m", "py_compile", chemin],
                           capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or "").strip()[-300:]


def deployer(nom, compte, a_blanc):
    chemin_m = os.path.join(MACHINE, nom)
    chemin_r = os.path.join(MIROIR, nom)
    shell = nom.endswith(".sh")
    print("\n── %s" % nom)
    if not os.path.exists(chemin_m):
        print("   absent de la machine — ce collecteur ne tourne pas ici, rien à déployer")
        return None
    if not os.path.exists(chemin_r):
        print("   absent du dépôt — rien à déployer")
        return None
    brut_m, brut_r = lire(chemin_m), lire(chemin_r)

    perdus = code_perdu(brut_m, brut_r, compte)
    if perdus:
        print("   REFUS — le dépôt a perdu du code présent sur la machine :")
        for ident in perdus[:6]:
            print("      · %s" % ident)
        print("   Les deux versions ont divergé : à reprendre à la main, pas à écraser.")
        return False

    sortie, restitutions, refus = fusionner(brut_m, brut_r, compte, shell)
    if sortie is None or refus:
        print("   REFUS — %s" % (refus[0] if refus else "fusion impossible"))
        for r in refus[1:4]:
            print("           %s" % r)
        return False
    for r in restitutions:
        print("   restitué  %s" % r)

    texte = "\n".join(sortie) + "\n"
    if texte.rstrip("\n") == brut_m.rstrip("\n"):
        print("   déjà à jour")
        return None

    provisoire = os.path.join("/tmp", "deploiement_" + nom)
    io.open(provisoire, "w", encoding="utf-8").write(texte)
    ok, erreur = compile_ok(provisoire, shell)
    if not ok:
        print("   REFUS — le résultat ne compile pas : %s" % erreur)
        return False

    n_av, n_ap = len(brut_m.rstrip("\n").split("\n")), len(sortie)
    if a_blanc:
        print("   (à blanc) déploierait : %d → %d lignes" % (n_av, n_ap))
        return None

    sauvegarde = chemin_m + ".SAUV_avant_deploiement_" + time.strftime("%Y%m%d_%H%M")
    io.open(sauvegarde, "w", encoding="utf-8").write(brut_m)
    io.open(chemin_m, "w", encoding="utf-8").write(texte)
    print("   DÉPLOYÉ   %d → %d lignes  (sauvegarde : %s)"
          % (n_av, n_ap, os.path.basename(sauvegarde)))
    return True


def main():
    a_blanc = "--a-blanc" in sys.argv
    tous = "--tous" in sys.argv
    noms = [a for a in sys.argv[1:] if not a.startswith("--")]

    if not os.path.isdir(MACHINE):
        print("Dossier des collecteurs de la machine introuvable : %s" % MACHINE)
        print("Ce déploiement se lance sur le Mac, ou sur un poste qui le reçoit par Syncthing.")
        return 1

    compte = _der.detecter_compte()
    if not noms:
        if not (tous or a_blanc):
            print(__doc__.strip().split("Lancement :")[-1].strip())
            return 1
        r = subprocess.run([sys.executable, os.path.join(ICI, "derive_collecteurs.py"), "--json"],
                           capture_output=True, text=True, timeout=300)
        noms = [c["nom"] for c in json.loads(r.stdout.strip().splitlines()[-1])["en_avance"]]
        if not noms:
            print("Rien en retard : ce qui tourne est déjà ce qui est écrit.")
            return 0
        print("En retard sur le dépôt : %s" % ", ".join(noms))

    faits = refuses = 0
    for nom in noms:
        r = deployer(nom, compte, a_blanc)
        faits += 1 if r is True else 0
        refuses += 1 if r is False else 0

    print("\n%d déployé(s), %d refusé(s)%s" % (faits, refuses, " — à blanc" if a_blanc else ""))
    if refuses:
        print("Un refus n'est pas un échec du script : c'est une divergence qui demande")
        print("une décision. La machine est intacte.")
    return 1 if refuses else 0


if __name__ == "__main__":
    sys.exit(main())
