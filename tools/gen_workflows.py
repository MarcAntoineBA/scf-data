#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_workflows.py — Écrit les sept workflows de cadence depuis un modèle unique.

POURQUOI GÉNÉRER PLUTÔT QUE FACTORISER
La première version utilisait un workflow « réutilisable » appelé par sept fichiers
courts : plus élégant sur le papier. En pratique, chaque appel échouait AU DÉMARRAGE,
et GitHub ne publie le motif d'un tel échec ni par API ni dans les journaux — seule
l'interface le montre. Diagnostiquer à l'aveugle coûtait un aller-retour complet par
hypothèse.

On supprime donc le mécanisme au lieu de le déboguer : les sept fichiers deviennent
autonomes. La factorisation ne disparaît pas, elle remonte d'un cran — elle vit ici,
dans ce générateur, où elle est vérifiable localement avant tout envoi.

Après modification : relancer ce script, puis committer les fichiers produits.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "..", ".github", "workflows")

# (cadence, expression cron, description lisible, passages, espacement en secondes)
# Minutes VOLONTAIREMENT décalées. La plateforme prévient que les tâches planifiées
# peuvent être retardées, voire abandonnées, aux heures de forte charge — et cite
# explicitement le début de chaque heure. Des expressions en minutes rondes (0, 10,
# 20…) tombent pile au pire moment. Ce décalage ne coûte rien et sort de la cohue.
#
# ── POURQUOI LES CADENCES FINES NE SONT PLUS DES CRON ────────────────────────────
# Les deux premières lignes demandaient 12 et 6 déclenchements par heure. Relevé sur
# les 300 dernières exécutions réelles, tous workflows confondus :
#
#     demandé 12/h → obtenu 1,1/h     (5min)
#     demandé  6/h → obtenu 1,0/h     (10min)
#     demandé  1/h → obtenu 1,1/h     (1h, la seule honorée)
#
# Six workflows distincts, tous plafonnés à exactement un passage par heure. Ce n'est
# pas de la charge aléatoire — les exécutions démarrent sans la moindre attente et
# durent 2 à 4 minutes — c'est un plafond de la plateforme. Les cadences « 5 min » et
# « 10 min » n'ont donc jamais existé : elles tournaient à l'heure depuis le début,
# en silence, pendant que le réglage affichait autre chose.
#
# On cesse de demander plus de réveils, puisque c'est refusé. Le réveil horaire lance
# désormais une exécution qui RESTE au travail et refait une collecte toutes les cinq
# minutes d'elle-même. Le cron redescend à une fois par heure : c'est ce que la
# plateforme accorde, et l'annoncer évite la fiction précédente. C'est aussi une
# sécurité — si un second réveil était un jour honoré, il attendrait la fin du
# premier (voir `concurrency`) et le retard s'accumulerait sans fin.
#
# Les cadences d'une heure et au-delà gardent un seul passage : le cron les honore.
CADENCES = [
    ("5min", "1 * * * *", "toutes les 5 minutes", 10, 300),
    ("10min", "3 * * * *", "toutes les 10 minutes", 5, 600),
    ("1h", "17 * * * *", "toutes les heures", 1, 0),
    ("6h", "19 1,7,13,19 * * *", "toutes les 6 heures", 1, 0),
    ("daily", "37 4 * * *", "une fois par jour", 1, 0),
    ("weekly", "43 4 * * 1", "une fois par semaine", 1, 0),
]

# Pendant la migration, une cadence porte un déclencheur sur l'orchestrateur : GitHub
# met parfois beaucoup plus que prévu à activer les plannings d'un dépôt neuf, et on a
# besoin d'une preuve d'exécution maintenant, pas au bon vouloir du planificateur.
# À retirer une fois la migration validée — sinon chaque retouche de l'orchestrateur
# relancerait un lot complet.
VERIF = ""
CADENCE_VERIF = None   # migration vérifiée : plus de déclencheur de secours

MODELE = '''name: Collecte {cadence}

# Cadence : {humain}. Fichier GÉNÉRÉ par tools/gen_workflows.py — ne pas éditer
# à la main : modifier le générateur et le relancer, sinon la correction ne vaudra
# que pour cette cadence et divergera silencieusement des six autres.
#
# GitHub n'accorde qu'UN déclenchement planifié par heure et par workflow, quelle que
# soit la fréquence demandée — mesuré sur 300 exécutions. Le cron ci-dessous est donc
# horaire, et la cadence réelle est tenue par la boucle de l'étape « Collecte » :
# {passages} passage(s) par exécution. Voir tools/gen_workflows.py pour le relevé.

on:
  workflow_dispatch:
  schedule:
    - cron: "{cron}"
{verif}
permissions:
  contents: write        # publie les données collectées

# Deux exécutions de la même cadence ne doivent jamais se chevaucher : elles
# écriraient les mêmes fichiers. La nouvelle attend son tour plutôt que d'annuler
# l'autre — une collecte à moitié faite vaut moins qu'une collecte finie.
concurrency:
  group: collecte-{cadence}
  cancel-in-progress: false

jobs:
  collecte:
    runs-on: ubuntu-latest
    timeout-minutes: {plafond}   # filet contre un runner bloqué ; le bornage fin est fait
                           # par collecteur, dans l'orchestrateur. Une cadence à
                           # passages multiples se borne SOUS l'heure : son exécution
                           # doit être finie avant le réveil suivant, faute de quoi
                           # celui-ci attendrait son tour et le retard s'accumulerait.

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Dépendances
        run: pip install --quiet -r requirements.txt

      # Les collecteurs lisent leurs clés dans l'environnement, avec repli sur un
      # fichier du dossier personnel. On alimente les DEUX voies : un collecteur qui
      # ne lirait que le fichier échouerait autrement sans raison visible.
      - name: Clés d'accès
        continue-on-error: true
        env:
          EIA_API_KEY: ${{{{ secrets.EIA_API_KEY }}}}
          SERPAPI_KEY: ${{{{ secrets.SERPAPI_KEY }}}}
          GIE_API_KEY: ${{{{ secrets.GIE_API_KEY }}}}
          LORIS_API_KEY: ${{{{ secrets.LORIS_API_KEY }}}}
        run: |
          [ -n "$EIA_API_KEY" ] && printf '%s' "$EIA_API_KEY" > "$HOME/.eia_api_key"
          [ -n "$SERPAPI_KEY" ] && printf '%s' "$SERPAPI_KEY" > "$HOME/.serpapi_key"
          [ -n "$GIE_API_KEY" ] && printf '%s' "$GIE_API_KEY" > "$HOME/.gie_api_key"
          [ -n "$LORIS_API_KEY" ] && printf '%s' "$LORIS_API_KEY" > "$HOME/.loris_api_key"
          chmod 600 "$HOME"/.eia_api_key "$HOME"/.serpapi_key "$HOME"/.gie_api_key "$HOME"/.loris_api_key || true

      # UNE exécution, {passages} passage(s). Chaque passage vise un instant CALCULÉ
      # depuis le début de l'exécution, jamais « attendre N secondes après le
      # précédent » : sinon la durée de chaque collecte s'ajouterait au rythme et les
      # passages dériveraient d'un quart d'heure sur la fin de l'heure.
      # Un passage en échec n'interrompt pas les suivants — perdre cinq minutes de
      # cotations vaut mieux que perdre le reste de l'heure.
      - name: Collecte
        env:
          GH_TOKEN: ${{{{ github.token }}}}
          FRED_API_KEY: ${{{{ secrets.FRED_API_KEY }}}}
          EIA_API_KEY: ${{{{ secrets.EIA_API_KEY }}}}
          SERPAPI_KEY: ${{{{ secrets.SERPAPI_KEY }}}}
          GIE_API_KEY: ${{{{ secrets.GIE_API_KEY }}}}
          # Cotations SPY de l'indice de sentiment. Cette clé vivait en clair dans deux
          # scripts ; elle a été retirée du code et passe par un secret. Sans elle, le
          # collecteur s'arrête net sur « SPY vide » — un refus franc plutôt qu'un
          # indice calculé sur une donnée manquante.
          TD_API_KEY: ${{{{ secrets.TD_API_KEY }}}}
          # La SEC refuse (403) toute requête sans contact nominatif. La valeur vit
          # dans un secret : l'adresse ne doit pas se trouver dans un dépôt public,
          # où elle serait récoltée dès l'indexation.
          SCF_CONTACT_UA: ${{{{ secrets.SCF_CONTACT_UA }}}}
          # Funding cross-venue de l'onglet Order Flow. Loris n'expose aucun en-tête
          # CORS : l'appel ne peut PAS venir du navigateur, et la clé n'a donc rien à
          # faire dans le HTML publié. Elle ne vit qu'ici.
          LORIS_API_KEY: ${{{{ secrets.LORIS_API_KEY }}}}
        run: |
          PASSAGES={passages}
          ESPACEMENT={espacement}
          DEBUT=$SECONDS
          ECHECS=0

          for p in $(seq 1 $PASSAGES); do
            CIBLE=$(( (p - 1) * ESPACEMENT ))
            ECOULE=$(( SECONDS - DEBUT ))
            if [ $CIBLE -gt $ECOULE ]; then
              echo "— attente de $(( CIBLE - ECOULE ))s avant le passage $p"
              sleep $(( CIBLE - ECOULE ))
            fi

            echo "::group::passage $p/$PASSAGES (à $(( (SECONDS - DEBUT) / 60 )) min)"
            if tools/une_passe.sh {cadence}; then
              echo "passage $p terminé"
            else
              ECHECS=$(( ECHECS + 1 ))
              echo "::warning::passage $p en échec — la boucle continue"
            fi
            echo "::endgroup::"
          done

          echo "$(( PASSAGES - ECHECS ))/$PASSAGES passage(s) menés à bien "\\
               "en $(( (SECONDS - DEBUT) / 60 )) min"

          # L'exécution ne tombe QUE si tout a échoué : un passage perdu est un
          # incident, la totalité est une panne — et seule la seconde doit teinter
          # l'historique en rouge, sinon plus personne ne regarde les alertes.
          if [ $ECHECS -eq $PASSAGES ]; then
            echo "::error::aucun passage n'a abouti"
            exit 1
          fi
'''


def main():
    ecrits = []
    for cadence, cron, humain, passages, espacement in CADENCES:
        # Le dernier passage démarre à (passages-1) × espacement ; on lui laisse de
        # quoi finir, puis on borne SOUS l'heure pour ne pas mordre sur le réveil
        # suivant. Une cadence à passage unique garde le filet large d'origine.
        plafond = 120 if passages == 1 else min(55, (passages - 1) * espacement // 60 + 10)
        contenu = MODELE.format(cadence=cadence, cron=cron, humain=humain,
                                passages=passages, espacement=espacement,
                                plafond=plafond,
                                verif=VERIF if cadence == CADENCE_VERIF else "")
        chemin = os.path.join(DEST, f"collect-{cadence}.yml")
        with open(chemin, "w", encoding="utf-8") as f:
            f.write(contenu)
        ecrits.append(os.path.basename(chemin))

    # Le workflow réutilisable n'a plus de raison d'être : le laisser traînerait un
    # fichier mort que quelqu'un croirait actif.
    ancien = os.path.join(DEST, "_collect.yml")
    if os.path.exists(ancien):
        os.remove(ancien)
        print("  retiré : _collect.yml (mécanisme abandonné)")

    print(f"{len(ecrits)} workflows générés : " + ", ".join(ecrits))

    try:
        import yaml
    except ImportError:
        print("  (PyYAML absent : validation ignorée)")
        return 0
    for nom in ecrits:
        chemin = os.path.join(DEST, nom)
        d = yaml.safe_load(open(chemin, encoding="utf-8"))
        job = list(d["jobs"].values())[0]
        assert "steps" in job, f"{nom} : job sans étapes"
        assert d[True] if True in d else d.get("on"), f"{nom} : déclencheurs manquants"
    print("  YAML valide, chaque workflow porte bien ses étapes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
