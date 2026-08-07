#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
index_fraicheur.py — Date CHAQUE cache publié, un par un.

POURQUOI CE FICHIER EXISTE — LA PANNE DU 2026-08-07
Le site arbitre entre deux origines (la collecte, le déploiement) et sert la plus
fraîche. Jusqu'ici il tranchait sur un BATTEMENT GLOBAL : « la cadence rapide a-t-elle
tourné récemment ? ». Mesuré ce jour à 16 h 45 locale, la réponse était oui — 21 min —
et le site servait donc la copie de la collecte pour TOUS les fichiers. Or le Narrative
Tracker et le TradFi Tracker vivaient dans la cadence 6 h : leur donnée avait 6 h 07,
et une copie de 40 minutes dormait dans le déploiement, à un repli de distance.
L'arbitrage n'a pas hésité : il n'a jamais posé la question. Il mesurait la santé de la
FLOTTE, pas l'âge du FICHIER demandé.

C'est la forme exacte que reprend cette panne à chaque récidive, et la raison pour
laquelle accélérer une cadence ne la referme jamais : passer un collecteur de 6 h à 1 h
réduit la fenêtre, il ne rétablit pas la mesure. Tant qu'un seul chiffre répond pour
139 fichiers, le prochain fichier lent se figera dans le même silence.

CE QUE PRODUIT CE MODULE
`cache/_fichiers.json` — un dictionnaire { nom de fichier : "AAAA-MM-JJTHH:MM:SSZ" }.
Le déploiement en publie un jumeau (`_deploy_fichiers.json`, écrit par
deploy_public_wrangler.sh). La fonction /data/ compare alors, pour LE fichier demandé,
son âge de chaque côté.

DEUX MESURES, DANS CET ORDRE — ET POURQUOI PAS UNE SEULE
1. L'horodatage écrit DANS le fichier. C'est celui que la page affiche au visiteur :
   arbitrer dessus, c'est arbitrer sur ce qu'il verra. Mais le parc n'a pas de
   vocabulaire commun (`updated`, `updated_at`, `updated_at_unix`, `generated_at`,
   `ts_fetched`…) et beaucoup de dates sont écrites SANS fuseau. Une date nue vaut
   deux heures d'écart en été selon la machine qui l'a écrite — assez pour inverser
   un arbitrage à 25 minutes de marge. On ne retient donc que les instants sans
   ambiguïté : epoch, ou ISO portant Z / un décalage explicite. Mesuré sur le parc :
   48 fichiers sur 110.
2. À défaut, le PASSAGE qui a produit le fichier. Chaque côté sait quand son
   collecteur a tourné, en UTC, sans rien à deviner. Couvre les 62 restants.

Une seule des deux ne suffit pas : la première est juste mais incomplète, la seconde
est complète mais aveugle à un collecteur qui tourne sans plus rien écrire. Ensemble,
tout fichier a une date, et les fichiers qui comptent ont la BONNE.

LA PROPRIÉTÉ QUI REND LA COMPARAISON HONNÊTE
Les deux côtés appliquent la MÊME règle, dans le MÊME ordre, au MÊME contenu. Le choix
du champ est heuristique — un cache peut porter plusieurs dates — mais peu importe
qu'il soit « le bon » dans l'absolu : ce qui compte est que les deux côtés choisissent
le même, sinon on comparerait deux grandeurs différentes et l'arbitrage serait un
tirage au sort déguisé. Toute retouche ici doit être reportée à l'identique dans
deploy_public_wrangler.sh, et inversement — c'est ce que vérifie tools/test_index_fraicheur.py.

MERGE PLUTÔT QUE REMPLACEMENT
Un passage ne voit que ses propres fichiers : les gros caches des autres cadences ne
sont même pas dans le clone. Écrire l'index à neuf effacerait leur date et les ferait
passer pour « jamais datés » — donc sans verdict, donc de retour au battement global,
donc à la panne d'origine. On repart de l'index publié et on ne remplace que ce qu'on
a réellement mesuré.
"""

import datetime
import json
import os
import re

# 64 Ko : les caches datent leur travail dans leur en-tête. Au-delà on lirait 25 Mo
# pour retrouver un champ qui figure dans les 200 premiers octets.
TETE = 65536

# Champs portant une EPOCH. Ni fuseau ni format à deviner : c'est la mesure la moins
# ambiguë du parc, donc la première essayée.
CHAMPS_EPOCH = ("generated_at", "updated_at_unix", "updated_unix", "updated_ts",
                "ts_fetched", "ts")
# Champs portant une date ISO. Retenus SEULEMENT s'ils portent Z ou un décalage :
# une date nue ne dit pas de quelle horloge elle vient (cf. en-tête).
CHAMPS_ISO = ("updated_at", "updated", "as_of", "asof")

_AVEC_FUSEAU = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")


def _canonique(epoch):
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def horodatage_contenu(chemin):
    """Instant écrit DANS le cache, en ISO-Z. None = aucune date sans ambiguïté.

    None n'est pas un échec à rattraper : c'est un refus de deviner. L'appelant
    retombe alors sur la date du passage, qui, elle, est toujours connue.
    """
    try:
        with open(chemin, "rb") as f:
            tete = f.read(TETE).decode("utf-8", "replace")
    except OSError:
        return None

    for champ in CHAMPS_EPOCH:
        m = re.search(r'"%s"\s*:\s*(\d{9,13}(?:\.\d+)?)' % champ, tete)
        if m:
            valeur = float(m.group(1))
            # Certains fetchers datent en millisecondes. 10^11 s = an 5138 : au-delà,
            # c'est forcément des millisecondes, jamais une date.
            if valeur > 1e11:
                valeur /= 1000.0
            try:
                return _canonique(valeur)
            except (OverflowError, OSError, ValueError):
                return None

    for champ in CHAMPS_ISO:
        m = re.search(r'"%s"\s*:\s*"([^"]{10,40})"' % champ, tete)
        if m:
            valeur = m.group(1).strip()
            if not _AVEC_FUSEAU.search(valeur):
                return None       # date nue : inutilisable pour comparer deux machines
            try:
                return _canonique(
                    datetime.datetime.fromisoformat(
                        valeur.replace("Z", "+00:00")).timestamp())
            except ValueError:
                return None
    return None


def dater(chemin, defaut):
    """Date d'un cache : son contenu s'il est explicite, sinon celle du passage."""
    return horodatage_contenu(chemin) or defaut


def construire(dossiers, defaut, ancien=None, seulement=None):
    """Index { nom : ISO-Z } pour les caches trouvés dans `dossiers`.

    `ancien`    : l'index déjà publié. Sert de fond de carte — ce qu'on n'a pas mesuré
                  cette fois-ci garde sa date au lieu de disparaître.
    `seulement` : restreint la mise à jour aux fichiers effectivement produits par ce
                  passage. Sans cette borne, un cache qu'aucun collecteur n'a touché
                  hériterait de la date du passage : il paraîtrait frais pour la seule
                  raison qu'on l'a REGARDÉ. C'est précisément le mensonge que cet
                  index existe pour supprimer.
    """
    index = dict(ancien or {})
    for dossier in dossiers:
        if not os.path.isdir(dossier):
            continue
        for nom in os.listdir(dossier):
            if not nom.endswith((".js", ".json")) or nom.startswith("_"):
                continue          # les battements ne se datent pas eux-mêmes
            if seulement is not None and nom not in seulement:
                continue
            index[nom] = dater(os.path.join(dossier, nom), defaut)
    return index


def ecrire(chemin, dossiers, defaut, seulement=None):
    """Écrit l'index fusionné et rend le nombre de fichiers datés."""
    ancien = {}
    if os.path.exists(chemin):
        try:
            with open(chemin, encoding="utf-8") as f:
                chargé = json.load(f)
            if isinstance(chargé, dict):
                ancien = {k: v for k, v in chargé.items() if isinstance(v, str)}
        except (OSError, ValueError):
            ancien = {}           # index illisible : on le reconstruit, sans bruit
    index = construire(dossiers, defaut, ancien, seulement)
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=0, sort_keys=True, ensure_ascii=False)
    return len(index)
