#!/usr/bin/env bash
# une_passe.sh — UNE collecte complète d'une cadence, de bout en bout.
#
# POURQUOI CE FICHIER EXISTE
# Le contenu de ce script vivait dans les workflows, en trois étapes successives.
# Il en sort parce qu'il doit maintenant être RÉPÉTÉ : GitHub n'accorde qu'un seul
# réveil par heure et par workflow, quelle que soit la fréquence demandée. Mesuré sur
# 300 exécutions : la cadence « 5 minutes » demandait 12 passages par heure et en
# obtenait 1,1. Les cadences fines n'ont jamais existé.
#
# La réponse n'est pas de réveiller plus souvent — c'est refusé — mais de rester au
# travail : un réveil horaire lance une boucle qui rappelle ce script à intervalle
# régulier. Le rythme redevient celui qu'on demande, sans dépendre d'une machine
# allumée à la maison.
#
# Une étape de workflow ne peut pas se répéter ; un script, si. D'où ce fichier.
#
# Usage : tools/une_passe.sh <cadence>

set -u

CADENCE="${1:?cadence attendue (5min, 10min, 1h, 6h, daily, weekly)}"
RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RACINE"

# ── 1. COLLECTE ───────────────────────────────────────────────────────────────
# Le code de sortie est délibérément ignoré : l'orchestrateur sort toujours en
# succès, l'échec d'un collecteur se lit dans son bilan et ne doit pas emporter la
# publication des autres.
python tools/run_jobs.py --bucket "$CADENCE"

# ── 2. GROS FICHIERS ──────────────────────────────────────────────────────────
# Douze fichiers pèsent 93 des 114 Mo du parc (historiques de cours, atlas). Les
# versionner à chaque passage ferait enfler le dépôt indéfiniment : ils partent en
# pièces jointes d'une release, remplacées sur place.
#
# On ne renvoie QUE ce que l'orchestrateur dit avoir modifié, et non tout le dossier.
# La distinction ne coûtait rien tant qu'il y avait un envoi par heure ; avec dix,
# elle évite de réexpédier des dizaines de mégaoctets inchangés dix fois — `release/`
# doit rester peuplé entre les passages, puisqu'il sert de base de comparaison au
# passage suivant. On ne peut donc pas le vider après envoi.
if [ -s .upload_list ]; then
  FICHIERS=()
  while IFS= read -r nom; do
    [ -n "$nom" ] && [ -f "release/$nom" ] && FICHIERS+=("release/$nom")
  done < .upload_list

  if [ ${#FICHIERS[@]} -gt 0 ]; then
    gh release view data >/dev/null 2>&1 || gh release create data \
      --title "Données" \
      --notes "Pièces jointes remplacées à chaque collecte, sans historique."
    gh release upload data "${FICHIERS[@]}" --clobber
    echo "${#FICHIERS[@]} fichier(s) publié(s) en pièce jointe"
  fi
else
  echo "aucun gros fichier modifié"
fi

# ── 3. DONNÉES VERSIONNÉES ────────────────────────────────────────────────────
git config user.name  "collecte"
git config user.email "collecte@users.noreply.github.com"

if [ ! -s .publish_list ]; then
  echo "aucun changement — rien à publier"
  exit 0
fi

# Reposer NOTRE contenu au-dessus du dernier état publié, sans rebase.
# `reset` (mixte) remet l'index sur le distant : c'est indispensable, la variante
# souple laisserait l'index sur la base d'origine et la publication annulerait les
# fichiers ajoutés entre-temps par les autres cadences — mesuré : sur sept bilans,
# un seul survivait.
# `--pathspec-from-file` ne prend QUE les fichiers que l'orchestrateur dit avoir
# touchés : ceux des autres cadences ne sont ni écrasés ni supprimés.
for essai in 1 2 3 4 5 6 7 8; do
  git fetch -q origin main
  git reset -q origin/main
  git add --pathspec-from-file=.publish_list --ignore-missing 2>/dev/null \
    || git add --pathspec-from-file=.publish_list
  if git diff --cached --quiet; then
    echo "déjà publié par une autre exécution — rien à faire"
    exit 0
  fi
  N=$(git diff --cached --name-only | wc -l | tr -d ' ')
  git commit -q -m "données $CADENCE : $N fichier(s)"
  if git push -q; then
    echo "publié : $N fichier(s) (essai $essai)"
    exit 0
  fi
  echo "publication concurrente — nouvelle tentative"
  sleep $((essai * 4))
done
echo "échec de publication après 8 essais" >&2
exit 1
