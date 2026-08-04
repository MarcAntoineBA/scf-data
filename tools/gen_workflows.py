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

# (cadence, expression cron, description lisible)
CADENCES = [
    ("10min", "*/10 * * * *", "toutes les 10 minutes"),
    ("30min", "*/30 * * * *", "toutes les 30 minutes"),
    ("2h", "5 */2 * * *", "toutes les 2 heures"),
    ("6h", "15 */6 * * *", "toutes les 6 heures"),
    ("12h", "25 3,15 * * *", "deux fois par jour"),
    ("daily", "35 4 * * *", "une fois par jour"),
    ("weekly", "45 4 * * 1", "une fois par semaine"),
]

# Pendant la migration, une cadence porte un déclencheur sur l'orchestrateur : GitHub
# met parfois beaucoup plus que prévu à activer les plannings d'un dépôt neuf, et on a
# besoin d'une preuve d'exécution maintenant, pas au bon vouloir du planificateur.
# À retirer une fois la migration validée — sinon chaque retouche de l'orchestrateur
# relancerait un lot complet.
VERIF = """  push:
    branches: [main]
    paths: [tools/run_jobs.py]
"""
CADENCE_VERIF = "30min"

MODELE = '''name: Collecte {cadence}

# Cadence : {humain}. Fichier GÉNÉRÉ par tools/gen_workflows.py — ne pas éditer
# à la main : modifier le générateur et le relancer, sinon la correction ne vaudra
# que pour cette cadence et divergera silencieusement des six autres.
#
# GitHub exécute les tâches planifiées « au mieux » : quelques minutes de retard aux
# heures de pointe sont normales. Sans commune mesure avec les 10 à 19 heures de gel
# que produisait une machine endormie.

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
    timeout-minutes: 120   # filet contre un runner bloqué ; le bornage fin est fait
                           # par collecteur, dans l'orchestrateur

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
        run: |
          [ -n "$EIA_API_KEY" ] && printf '%s' "$EIA_API_KEY" > "$HOME/.eia_api_key"
          [ -n "$SERPAPI_KEY" ] && printf '%s' "$SERPAPI_KEY" > "$HOME/.serpapi_key"
          [ -n "$GIE_API_KEY" ] && printf '%s' "$GIE_API_KEY" > "$HOME/.gie_api_key"
          chmod 600 "$HOME"/.eia_api_key "$HOME"/.serpapi_key "$HOME"/.gie_api_key || true

      - name: Collecte
        env:
          FRED_API_KEY: ${{{{ secrets.FRED_API_KEY }}}}
          EIA_API_KEY: ${{{{ secrets.EIA_API_KEY }}}}
          SERPAPI_KEY: ${{{{ secrets.SERPAPI_KEY }}}}
          GIE_API_KEY: ${{{{ secrets.GIE_API_KEY }}}}
          # La SEC refuse (403) toute requête sans contact nominatif. La valeur vit
          # dans un secret : l'adresse ne doit pas se trouver dans un dépôt public,
          # où elle serait récoltée dès l'indexation.
          SCF_CONTACT_UA: ${{{{ secrets.SCF_CONTACT_UA }}}}
        run: python tools/run_jobs.py --bucket {cadence}

      # Douze fichiers pèsent 93 des 114 Mo du parc (historiques de cours, atlas).
      # Les versionner à chaque passage ferait enfler le dépôt indéfiniment : ils
      # partent en pièces jointes d'une release, remplacées sur place.
      - name: Publier les gros fichiers
        env:
          GH_TOKEN: ${{{{ github.token }}}}
        run: |
          shopt -s nullglob
          FICHIERS=(release/*)
          if [ ${{#FICHIERS[@]}} -eq 0 ]; then
            echo "aucun gros fichier modifié"
            exit 0
          fi
          gh release view data >/dev/null 2>&1 || gh release create data \\
            --title "Données" \\
            --notes "Pièces jointes remplacées à chaque collecte, sans historique."
          gh release upload data "${{FICHIERS[@]}}" --clobber
          echo "${{#FICHIERS[@]}} fichier(s) publié(s) en pièce jointe"

      - name: Publier les données modifiées
        run: |
          git config user.name  "collecte"
          git config user.email "collecte@users.noreply.github.com"
          git add cache/
          if git diff --cached --quiet; then
            echo "aucun changement — rien à publier"
            exit 0
          fi
          N=$(git diff --cached --name-only | wc -l | tr -d ' ')
          git commit -q -m "données {cadence} : $N fichier(s)"

          # Plusieurs cadences peuvent publier en même temps. Le perdant rejoue son
          # commit au-dessus du gagnant plutôt que d'échouer : les fichiers touchés
          # sont disjoints, il n'y a rien à arbitrer, seulement à réessayer.
          for essai in 1 2 3 4 5; do
            if git push; then
              echo "publié (essai $essai)"
              exit 0
            fi
            git pull --rebase --autostash origin main || exit 1
            sleep $((essai * 5))
          done
          echo "échec de publication après 5 essais" >&2
          exit 1
'''


def main():
    ecrits = []
    for cadence, cron, humain in CADENCES:
        contenu = MODELE.format(cadence=cadence, cron=cron, humain=humain,
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
