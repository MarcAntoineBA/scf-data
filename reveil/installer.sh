#!/bin/bash
# installer.sh — Déploie le réveil des cadences sur Cloudflare.
#
# À lancer UNE fois. Deux valeurs sont lues sur cette machine et ne transitent nulle
# part ailleurs : le jeton Cloudflare (~/.cftoken) pour déployer, et le jeton GitHub
# (~/.ghtoken) qui devient un SECRET du Worker — chiffré chez Cloudflare, jamais
# écrit dans un fichier du dépôt.
set -u
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")" || exit 1

[ -f "$HOME/.cftoken" ] || { echo "Jeton Cloudflare absent : ~/.cftoken"; exit 1; }
[ -f "$HOME/.ghtoken" ] || { echo "Jeton GitHub absent : ~/.ghtoken"; exit 1; }

export CLOUDFLARE_API_TOKEN="$(tr -d '\n' < "$HOME/.cftoken")"
export CLOUDFLARE_ACCOUNT_ID="5d577092260614ef2f4c143bad823b68"
W="wrangler@4.103.0"

echo "── 1/3 déploiement du Worker"
npx --yes "$W" deploy || { echo "échec du déploiement"; exit 1; }

echo "── 2/3 dépôt du jeton GitHub comme secret"
tr -d '\n' < "$HOME/.ghtoken" | npx --yes "$W" secret put GITHUB_TOKEN || {
  echo "échec du dépôt du secret"; exit 1; }

echo "── 3/3 vérification des droits (aucune collecte déclenchée)"
URL=$(npx --yes "$W" deployments list 2>/dev/null | grep -oE "https://[a-z0-9.-]+workers.dev" | head -1)
echo
echo "Installé. Le réveil déclenchera les cadences toutes les 10 minutes."
[ -n "$URL" ] && echo "Contrôle des droits à la demande : curl $URL"
