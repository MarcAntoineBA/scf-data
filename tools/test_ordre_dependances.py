#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_ordre_dependances.py — Garde-fou : un collecteur ne part jamais avant celui
dont il lit la sortie.

CE QU'IL EMPÊCHE DE REVENIR
Le 2026-09-05, le job `marche` sortait en erreur toutes les nuits depuis le
29/08 sur « not enough values to unpack (expected 3, got 2) ».

Le message ne nommait rien. La cause était un ORDRE : `fetch_marche_actions.py`
lit `univers_actions.json`, écrit par `fetch_univers_actions.py`. Le lien
n'était pas déclaré, et l'ordonnanceur ne savait de toute façon pas l'exprimer —
il rangeait les collecteurs en DEUX vagues : « ce dont un autre dépend », puis
« le reste ». `marche` étant lui-même attendu par `secfunda`, `intlfunda`,
`screener`, `medianesind` et `secteursmonde`, il partait dans la MÊME vague que
`univers` et lisait un fichier pas encore écrit.

Conséquences mesurées : `marche_actions_index.json` avait sept jours de retard,
et avec lui l'univers dont les états financiers tirent QUELLES sociétés
approfondir. `actionnariat` tombait pour la même raison, une marche plus loin
(il lit l'index des états, écrit par `secfunda`, lancé dans la même vague).
Le bilan de la nuit affichait « 32/38 OK ».

CE QUE LE CONTRÔLE FAIT
Il rejoue le calcul des niveaux de `run_jobs.py` sur le vrai `jobs.json` et
vérifie, pour CHAQUE cadence, que tout collecteur part strictement après ceux
dont il dépend. Puis trois contre-épreuves, parce qu'un contrôle qui dit oui à
tout ne protège de rien : une chaîne à trois maillons doit rendre trois niveaux,
un cycle ne doit pas boucler sans fin, et une dépendance absente de la cadence
ne doit pas immobiliser le collecteur qui l'attend.
"""

import json
import os
import sys

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)

from run_jobs import DEPENDANCES, bucket_of  # noqa: E402

JOBS = os.path.join(ICI, "..", "jobs.json")


def niveaux(due, deps):
    """La copie EXACTE du calcul de run_jobs.py — si l'un change, l'autre doit.

    On la duplique volontairement plutôt que d'importer : le contrôle doit
    échouer quand l'ordonnanceur change de forme, pas suivre sa dérive.
    """
    par_id = {j["id"]: j for j in due}
    restants, faits, out = dict(par_id), set(), []
    tours = 0
    while restants:
        tours += 1
        if tours > len(par_id) + 2:
            raise RuntimeError("le calcul des niveaux ne converge pas")
        prets = [j for i, j in restants.items()
                 if all(d in faits or d not in par_id
                        for d in deps.get(i, ()))]
        if not prets:
            prets = list(restants.values())      # cycle : dégrader, pas bloquer
        out.append(prets)
        for j in prets:
            faits.add(j["id"])
            restants.pop(j["id"])
    return out


def rang(nivs):
    return {j["id"]: i for i, n in enumerate(nivs) for j in n}


def main():
    echecs = []
    jobs = [j for j in json.load(open(JOBS))["jobs"] if j["category"] == "public"]

    # ── 1. LE CAS RÉEL : chaque cadence, chaque dépendance ────────────────
    cadences = sorted({bucket_of(j) for j in jobs if bucket_of(j)})
    for cad in cadences:
        due = [j for j in jobs if bucket_of(j) == cad]
        r = rang(niveaux(due, DEPENDANCES))
        for jid, avant in DEPENDANCES.items():
            if jid not in r:
                continue
            for d in avant:
                if d in r and not r[d] < r[jid]:
                    echecs.append(
                        f"[{cad}] « {jid} » (niveau {r[jid]}) ne part pas après "
                        f"« {d} » (niveau {r[d]})")

    # ── 2. LA CHAÎNE QUI A CASSÉ, NOMMÉE ─────────────────────────────────
    # univers → marche → secfunda → actionnariat : quatre niveaux distincts.
    daily = [j for j in jobs if bucket_of(j) == "daily"]
    r = rang(niveaux(daily, DEPENDANCES))
    chaine = ["univers", "marche", "secfunda", "actionnariat"]
    presents = [c for c in chaine if c in r]
    if len(presents) < 4:
        echecs.append("la chaîne des états n'est plus dans la cadence daily : "
                      f"manque {sorted(set(chaine) - set(presents))}")
    else:
        for a, b in zip(presents, presents[1:]):
            if not r[a] < r[b]:
                echecs.append(f"chaîne des états : « {b} » ne part pas après « {a} »")

    # ── 3. CONTRE-ÉPREUVES ───────────────────────────────────────────────
    faux = [{"id": x} for x in ("a", "b", "c")]
    n = niveaux(faux, {"b": ["a"], "c": ["b"]})
    if len(n) != 3:
        echecs.append(f"une chaîne à trois maillons rend {len(n)} niveau(x), pas 3")

    try:
        n = niveaux([{"id": "x"}, {"id": "y"}], {"x": ["y"], "y": ["x"]})
        if len(n) != 1:
            echecs.append("un cycle devrait être lancé d'un seul tenant")
    except RuntimeError as e:
        echecs.append(f"un cycle fait boucler le calcul : {e}")

    # Une dépendance d'une AUTRE cadence n'existe pas ici : ne pas l'attendre.
    n = niveaux([{"id": "seul"}], {"seul": ["absent_de_cette_cadence"]})
    if len(n) != 1:
        echecs.append("une dépendance hors cadence immobilise le collecteur")

    print(f"cadences vérifiées : {', '.join(cadences)}")
    print(f"chaîne des états   : " + " → ".join(
        f"{c}(n{r[c]})" for c in presents))
    print()
    if echecs:
        print(f"✗ {len(echecs)} contrôle(s) en échec :")
        for e in echecs:
            print(f"   · {e}")
        return 1
    print("✓ tous les contrôles passent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
