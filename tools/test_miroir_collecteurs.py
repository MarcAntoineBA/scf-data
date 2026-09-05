#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_miroir_collecteurs.py — Rend VISIBLE la dérive entre `scripts/` (le miroir publié,
celui que les runners exécutent) et les collecteurs vivants de la machine de l'auteur.

POURQUOI CE CONTRÔLE EXISTE

`scripts/` n'est pas une source : c'est un MIROIR GÉNÉRÉ par `tools/import_scripts.py`,
dans un seul sens (machine → dépôt). Rien ne garantit qu'il soit à jour. Quand un
collecteur est corrigé d'un côté et pas re-miroité, le nuage continue de tourner
L'ANCIENNE VERSION : il publie des données à l'ancienne forme, à l'heure, sans une
seule erreur, sans un seul job rouge. La panne est parfaitement muette — c'est
exactement pour ça qu'elle dure des semaines.

LE CAS QUI A COÛTÉ DE LA DONNÉE FAUSSE (tout ce qui suit est mesuré le 05/09/2026)

`fetch_narratives_fundamentals.py` porte, dans le dépôt du site (~/Desktop/…), une
version du 05/09/2026 01:20 de 1 827 lignes, 58 mentions de la valeur pleinement
diluée, avec une fonction `garantir_offre_coherente` qui interdit de publier une
capitalisation supérieure à cette valeur. Le chemin d'import (~/Library/Application
Support/…) et le miroir publié portent tous deux la version du 04/06/2026, 1 387
lignes, 29 mentions, sans cette fonction : 440 lignes de travail sur la FDV, correctif
compris, ne sont jamais arrivées dans le nuage.

Ce que ça donne côté lecteur, compté dans le cache servi
`narratives_fundamentals_cache.js` daté du 04/09/2026 16:56, 247 jetons :
  · 22 jetons publient une capitalisation SUPÉRIEURE à leur propre valeur pleinement
    diluée. Monero en tête : 9,942 Md$ contre 9,762 Md$, soit un rapport de 1,0184.
    C'est arithmétiquement impossible — la capitalisation, c'est l'offre EN CIRCULATION
    × le prix ; la FDV, l'offre TOTALE × le même prix : le rapport ne peut pas dépasser 1.
    Suivent Zebec (1,0158), Invesco USTB (1,0145), SPX6900 (1,0088)… jusqu'à Bitcoin
    (1,0017) et Ethereum (1,0005).
  · 2 jetons affichent une offre en circulation au-delà de 100 % : Kinesis Silver
    à 101,3 %, Invesco USTB à 100,6 %.
Aucune erreur, aucun job rouge, aucune alerte de fraîcheur : le collecteur tourne à
l'heure, il tourne simplement l'ancien code.

TROIS COPIES VIVANTES, PAS DEUX — ET C'EST LE CŒUR DU PIÈGE

Un contrôle qui ne comparerait que le miroir au chemin d'import NE VERRAIT PAS ce
cas-là : ces deux-là sont identiques, au bit près. Le correctif est dans une troisième
copie. Mesuré ici même, `fetch_narratives.py` existe en trois longueurs (2 150, 2 209
et 2 317 lignes selon le dossier), et aucune des trois ne se sait périmée. Le contrôle
compare donc le miroir à TOUTES les copies vivantes déclarées, pas seulement à celle
que l'import lit — sinon il rendrait un rapport vert sur le seul cas qui a coûté de la
donnée fausse.

CE QUE FAIT LE CONTRÔLE

Il rejoue sur chaque collecteur les réécritures EXACTES de `tools/import_scripts.py`
— chemins personnels, étiquettes launchd, adresse de contact, valeurs de repli
secrètes, ajout d'`import os` — puis compare au fichier du miroir. Ces réécritures sont
IMPORTÉES du module, jamais recopiées « à l'identique » ici : deux copies d'une même
règle finissent par diverger, et un contrôle qui compare avec des règles approchantes
signale 100 % de faux positifs, puis se fait désarmer.

CE QU'IL NE FAIT PAS

Il ne corrige rien et ne touche à aucun collecteur. Il ne prétend pas non plus savoir
quel côté a raison : il dit de COMBIEN et dans quel SENS chaque copie diverge (lignes
de chaque côté, date de dernière écriture), et l'humain tranche.

SORTIES
  0 — miroir conforme, ou comparaison impossible (voir plus bas)
  1 — au moins un CONTENU DIFFÉRENT, c'est-à-dire :
        · une copie du chemin d'import qui diffère du miroir ;
        · ou une copie HORS chemin d'import qui diffère du miroir ET qui est PLUS
          RÉCENTE que le chemin d'import — le mécanisme exact du cas Monero : le
          correctif existe, l'import ne le voit pas, le nuage publie l'ancien.
      Une copie hors chemin d'import PLUS ANCIENNE est signalée sans faire échouer :
      elle peut n'être qu'une branche morte, et on n'invente pas un verdict.

Un collecteur présent dans le miroir et absent de toutes les copies est NORMAL : il est
né dans le nuage. Ce n'est pas une erreur, c'est une ligne d'inventaire.

POURQUOI IL SORT EN SUCCÈS SUR UN RUNNER LINUX

Les dossiers sources n'existent que sur le Mac de l'auteur. Sur un runner, il n'y a
rien à comparer : le contrôle le DIT et sort en succès, au lieu d'échouer partout. Un
contrôle qui échoue partout est un contrôle qu'on finit par retirer de la chaîne — et
on perd alors aussi le jour où il avait raison.
"""

import difflib
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RACINE = os.path.normpath(os.path.join(HERE, ".."))
MIROIR = os.path.join(RACINE, "scripts")

# Les règles de réécriture viennent du module d'import LUI-MÊME. Si son interface
# change (renommage de `rewrite`, de `SRC`…), on veut une panne FRANCHE ici plutôt
# qu'une comparaison qui continue avec des règles devinées et accuse le miroir à tort.
sys.path.insert(0, HERE)
try:
    import import_scripts as imp
except Exception as e:                                   # noqa: BLE001
    print("Impossible d'importer tools/import_scripts.py : %s" % e, file=sys.stderr)
    print("Sans ses règles de réécriture, toute comparaison serait fausse.", file=sys.stderr)
    sys.exit(1)

for attendu in ("SRC", "SITE", "rewrite", "needs_os_import", "add_os_import",
                "with_local_modules", "NEVER", "SECRET_HINT", "ACCOUNT"):
    if not hasattr(imp, attendu):
        print("tools/import_scripts.py n'expose plus « %s » : le contrôle ne peut pas "
              "rejouer ses règles à l'identique, il refuse de deviner." % attendu,
              file=sys.stderr)
        sys.exit(1)

# Les emplacements où vit un collecteur, DÉCLARÉS, jamais devinés par balayage du
# disque : une heuristique qui ratisse le Bureau ramènerait des sauvegardes datées
# (`*.bak_…`) et des exports, et noierait le signal. La première entrée est la seule
# que `import_scripts.py` lit — d'où la colonne « chemin d'import » du rapport.
COPIES = [
    ("machine (chemin d'import)", imp.SRC, True),
    ("bureau — dépôt du site", imp.SITE, False),
    ("bureau/scripts/SiteCryptoFinance", os.path.join(imp.SITE, "scripts",
                                                      "SiteCryptoFinance"), False),
]


# ── Normalisation ────────────────────────────────────────────────────────────────
def normaliser(nom, brut):
    """Applique au texte d'une copie vivante ce que l'import lui aurait appliqué.

    Retourne (texte, refus). `refus` non vide = l'import REFUSERAIT ce fichier
    aujourd'hui. C'est une information de premier ordre : un miroir figé sur un
    collecteur refusé n'est pas un oubli, c'est un blocage, et il se règle autrement.
    """
    if imp.SECRET_HINT.search(brut):
        return None, "ressemble à un secret en dur (l'import le refuse)"
    texte = imp.rewrite(brut, nom.endswith(".sh"))
    reste = re.search(re.escape(imp.ACCOUNT), texte)
    if reste:
        return None, ("chemin personnel résiduel ligne %d (l'import le refuse)"
                      % (texte[:reste.start()].count("\n") + 1))
    if not nom.endswith(".sh") and imp.needs_os_import(texte):
        rustine = imp.add_os_import(texte)
        if rustine is None:
            return None, "réécriture sans `import os` possible (l'import le refuse)"
        texte = rustine
    return texte, ""


def lire(chemin):
    return open(chemin, encoding="utf-8", errors="replace").read()


def ecarts(avant, apres):
    """Combien de lignes il faudrait ajouter/retirer au miroir pour rejoindre la copie."""
    plus = moins = 0
    for ligne in difflib.unified_diff(avant.splitlines(), apres.splitlines(),
                                      n=0, lineterm=""):
        if ligne.startswith("+") and not ligne.startswith("+++"):
            plus += 1
        elif ligne.startswith("-") and not ligne.startswith("---"):
            moins += 1
    return plus, moins


def date_miroir(nom):
    """Date de dernière écriture du fichier du miroir.

    On demande d'abord à git : dans une copie de travail fraîchement clonée ou
    basculée, la date du SYSTÈME DE FICHIERS est celle du checkout — la même pour
    tous les fichiers — et ferait croire que le miroir vient d'être régénéré alors
    qu'il est figé depuis des semaines. C'est le même piège que le registre des
    écritures, où le clone rajeunissait tous les caches d'un coup. La date du dernier
    commit qui touche le fichier, elle, ne ment pas. Repli sur le mtime si git est
    absent ou si le fichier n'est pas encore suivi — et on DIT lequel des deux on
    affiche, pour qu'une date de checkout ne passe jamais pour une date d'écriture.
    """
    try:
        out = subprocess.run(
            ["git", "-C", RACINE, "log", "-1", "--format=%ct", "--", "scripts/" + nom],
            capture_output=True, text=True, timeout=20)
        if out.returncode == 0 and out.stdout.strip().isdigit():
            return int(out.stdout.strip()), "dernier commit"
    except Exception:                                     # noqa: BLE001
        pass
    return int(os.path.getmtime(os.path.join(MIROIR, nom))), "mtime, hors git"


def jour(ts):
    return time.strftime("%d/%m/%Y", time.localtime(ts))


def attendus_du_miroir():
    """Les collecteurs que l'import DEVRAIT emporter, calculés comme le fait `main()`.

    Sans ce calcul, tout `.py` de la machine hors périmètre public (advisor,
    portefeuille, souveraineté…) serait signalé « absent du miroir » : des dizaines de
    fausses alertes qui noieraient les vraies. On réutilise la même liste d'exclusion
    et le même parcours des modules que les collecteurs s'importent entre eux.
    """
    chemin = os.path.join(RACINE, "jobs.json")
    if not os.path.exists(chemin):
        return None
    jobs = json.load(open(chemin))["jobs"]
    voulus = sorted({j["script"] for j in jobs
                     if j.get("category") == "public" and j.get("script")})
    return {n for n in imp.with_local_modules(voulus)
            if n.endswith(".py") and not imp.NEVER.search(n)}


# ── Contrôle ─────────────────────────────────────────────────────────────────────
def main():
    montrer_diff = "--diff" in sys.argv[1:]

    presentes = [(lib, d, est_import) for lib, d, est_import in COPIES if os.path.isdir(d)]
    if not os.path.isdir(imp.SRC):
        print("Comparaison IMPOSSIBLE : le chemin d'import des collecteurs est absent.")
        print("  attendu : %s" % imp.SRC)
        print("  Ce dossier n'existe que sur la machine de l'auteur ; sur un runner,")
        print("  il n'y a rien à comparer. Le miroir n'est donc ni validé ni infirmé.")
        for lib, d, _ in COPIES[1:]:
            if os.path.isdir(d):
                print("  (autre copie tout de même présente : %s — %s)" % (lib, d))
        return 0

    if not os.path.isdir(MIROIR):
        print("scripts/ introuvable dans %s — dépôt incomplet." % RACINE, file=sys.stderr)
        return 1

    for lib, d, _ in COPIES:
        if not os.path.isdir(d):
            # Une copie déclarée mais absente n'a PAS été contrôlée : le dire, sinon
            # le rapport laisse croire à une couverture qu'il n'a pas.
            print("note : copie déclarée absente, non contrôlée — %s (%s)" % (lib, d))

    miroir = sorted(f for f in os.listdir(MIROIR)
                    if f.endswith(".py") and os.path.isfile(os.path.join(MIROIR, f)))

    conformes, divergents, nes_nuage, refuses = [], [], [], []

    for nom in miroir:
        contenu_miroir = lire(os.path.join(MIROIR, nom))
        ts_import = None
        etats = []
        for lib, dossier, est_import in presentes:
            chemin = os.path.join(dossier, nom)
            if not os.path.isfile(chemin):
                continue
            ts = int(os.path.getmtime(chemin))
            if est_import:
                ts_import = ts
            attendu, refus = normaliser(nom, lire(chemin))
            if refus:
                refuses.append((nom, lib, refus))
                continue
            # `len(splitlines())` et non `count("\n") + 1` : un fichier terminé par
            # un saut de ligne — c'est-à-dire tous — comptait une ligne vide de trop,
            # et le rapport annonçait 1 388 lignes là où `wc -l` en voit 1 387. Un
            # chiffre qu'on ne peut pas recouper à la main est un chiffre qu'on ne croit pas.
            etats.append({
                "lib": lib, "est_import": est_import, "ts": ts,
                "lignes": len(attendu.splitlines()),
                "identique": attendu == contenu_miroir,
                "texte": attendu,
            })

        if not etats:
            if not any(os.path.isfile(os.path.join(d, nom)) for _, d, _ in presentes):
                nes_nuage.append(nom)
            continue

        ts_mir, origine = date_miroir(nom)
        for e in etats:
            e["plus"], e["moins"] = ((0, 0) if e["identique"]
                                     else ecarts(contenu_miroir, e["texte"]))
            # Le mécanisme du cas Monero, exprimé en une condition datée et
            # falsifiable : une copie hors chemin d'import, différente du miroir et
            # PLUS RÉCENTE que ce que l'import lit — le correctif existe, l'import ne
            # le verra jamais, le nuage publiera l'ancien code jusqu'à ce qu'on le dise.
            e["hors_chemin_recent"] = (not e["est_import"] and not e["identique"]
                                       and ts_import is not None and e["ts"] > ts_import)

        if all(e["identique"] for e in etats):
            conformes.append(nom)
            continue

        divergents.append({
            "nom": nom, "l_mir": len(contenu_miroir.splitlines()),
            "ts_mir": ts_mir, "origine": origine, "etats": etats,
            "bloquant": any((e["est_import"] and not e["identique"])
                            or e["hors_chemin_recent"] for e in etats),
            "diff": (difflib.unified_diff(
                contenu_miroir.splitlines(),
                max((e for e in etats if not e["identique"]),
                    key=lambda e: e["ts"])["texte"].splitlines(),
                "miroir/" + nom, "copie la plus récente/" + nom, n=1, lineterm="")),
        })

    absents_miroir = []
    attendus = attendus_du_miroir()
    if attendus is not None:
        for nom in sorted(attendus - set(miroir)):
            if os.path.exists(os.path.join(imp.SRC, nom)):
                absents_miroir.append(nom)

    # ── Rapport ──────────────────────────────────────────────────────────────
    compares = len(conformes) + len(divergents)
    bloquants = [d for d in divergents if d["bloquant"]]
    signales = [d for d in divergents if not d["bloquant"]]

    print("MIROIR DES COLLECTEURS — scripts/ comparé aux copies vivantes, "
          "règles d'import rejouées")
    print("  miroir    %s" % MIROIR)
    for lib, d, est_import in presentes:
        n = sum(1 for f in miroir if os.path.isfile(os.path.join(d, f)))
        print("  copie     %-34s %-3d fichier(s) du miroir présent(s)   %s"
              % (lib, n, d))
    print("  %d fichier(s) .py dans le miroir · %d comparable(s) à au moins une copie"
          % (len(miroir), compares))
    # Les lignes affichées sont celles du texte APRÈS réécriture d'import : c'est le
    # seul comptage comparable au miroir. Il peut dépasser `wc -l` d'une unité quand
    # la réécriture ajoute `import os`. On le dit, sinon le premier recoupement à la
    # main fait passer tout le rapport pour faux.
    print("  (lignes comptées après réécriture d'import ; +1 vs `wc -l` "
          "si `import os` a été ajouté)")
    print("  %d conforme(s) partout · %d divergent(s) dont %d BLOQUANT(s) · "
          "%d né(s) dans le nuage · %d refusé(s) à l'import · %d attendu(s) mais absent(s)"
          % (len(conformes), len(divergents), len(bloquants), len(nes_nuage),
             len(refuses), len(absents_miroir)))

    def afficher(d):
        print("  ✗ %s" % d["nom"])
        print("      %-34s %5d l.   %s (%s)"
              % ("miroir publié", d["l_mir"], jour(d["ts_mir"]), d["origine"]))
        for e in sorted(d["etats"], key=lambda e: (not e["est_import"], e["lib"])):
            if e["identique"]:
                verdict = "identique au miroir"
            else:
                delta = e["lignes"] - d["l_mir"]
                verdict = ("copie en avance de %d l." % delta if delta > 0 else
                           "miroir en avance de %d l." % -delta if delta < 0 else
                           "même longueur, contenu différent")
                verdict += "  → %d à ajouter / %d à retirer au miroir" % (e["plus"],
                                                                         e["moins"])
            if e["hors_chemin_recent"]:
                verdict += "   ⚠ PLUS RÉCENTE QUE LE CHEMIN D'IMPORT"
            print("      %-34s %5d l.   %s   %s"
                  % (e["lib"], e["lignes"], jour(e["ts"]), verdict))
        if montrer_diff:
            lignes = list(d["diff"])
            for ligne in lignes[:40]:
                print("        | %s" % ligne)
            if len(lignes) > 40:
                print("        | … %d ligne(s) de diff en plus" % (len(lignes) - 40))

    if bloquants:
        print("\nCONTENU DIFFÉRENT, BLOQUANT — le nuage tourne un autre code "
              "que le plus récent :")
        for d in sorted(bloquants, key=lambda d: -max(abs(e["lignes"] - d["l_mir"])
                                                      for e in d["etats"])):
            afficher(d)

    if signales:
        print("\nCONTENU DIFFÉRENT, SIGNALÉ SANS BLOQUER — copies hors chemin d'import "
              "PLUS ANCIENNES que lui (branche morte possible, on ne tranche pas) :")
        for d in sorted(signales, key=lambda d: -max(abs(e["lignes"] - d["l_mir"])
                                                     for e in d["etats"])):
            afficher(d)

    if absents_miroir:
        print("\nATTENDUS MAIS ABSENTS DU MIROIR — le job existe, le code n'est pas publié :")
        for nom in absents_miroir:
            print("  ! %s" % nom)

    if refuses:
        print("\nREFUSÉS À L'IMPORT — cette copie ne PEUT pas alimenter le miroir :")
        for nom, lib, why in refuses:
            print("  ! %-42s %-34s %s" % (nom, lib, why))

    if nes_nuage:
        print("\nNÉS DANS LE NUAGE — absents de toutes les copies, légitime, "
              "pas une erreur (%d) :" % len(nes_nuage))
        for nom in nes_nuage:
            print("  · %s" % nom)

    # Garde-fou du contrôle LUI-MÊME. Si la normalisation cassait (règle renommée,
    # compte différent, encodage), chaque fichier paraîtrait divergent et le rapport
    # accuserait le miroir à tort. 100 % de divergence sur un échantillon non trivial
    # n'est pas un miroir périmé : c'est ce contrôle qui est en panne.
    if compares >= 5 and not conformes:
        print("\n⚠ CONTRÔLE SUSPECT : %d fichiers comparés, AUCUN conforme." % compares)
        print("  La normalisation ne reproduit sans doute plus les règles d'import ;")
        print("  vérifier tools/import_scripts.py avant d'accuser le miroir.")

    if bloquants:
        print("\nÉCHEC : %d collecteur(s) publient un code plus ancien que la dernière "
              "version écrite." % len(bloquants))
        print("  Ordre des opérations : porter la bonne version dans %s,"
              % imp.SRC)
        print("  puis `python3 tools/import_scripts.py` — l'import ne lit QUE ce "
              "dossier-là, jamais le Bureau.")
        return 1

    print("\nOK : aucun collecteur ne publie un code plus ancien que sa dernière "
          "version écrite (%d comparé(s))." % compares)
    return 0


if __name__ == "__main__":
    sys.exit(main())
