#!/bin/bash
# install.sh — Branche le site sur le dépôt de collecte.
#
# CE QUE ÇA CHANGE, EN UNE PHRASE : une donnée devient publique dès qu'elle est
# collectée, sans redéploiement — la chaîne « collecte → dépôt local → synchronisation
# → redéploiement », dont chaque maillon pouvait figer le site, disparaît.
#
# TROIS MODIFICATIONS, TOUTES RÉVERSIBLES
#   1. une fonction `/data/<fichier>` qui lit le dépôt de collecte, avec repli sur la
#      copie déployée si le dépôt ne répond pas ;
#   2. les fichiers de données déplacés du RACINE vers `public/data/` au moment du
#      build — ainsi plus aucun fichier statique n'occupe l'adresse d'origine, et la
#      redirection vers la fonction s'applique sans dépendre d'une règle de priorité
#      que la documentation ne garantit pas ;
#   3. `_redirects` (133 règles) et `_routes.json`, pour que les adresses existantes
#      continuent de fonctionner sans toucher à une seule page.
#
# Par défaut : SIMULATION. Rien n'est écrit tant qu'on ne passe pas --appliquer.

set -u
SITE="$HOME/Desktop/Site_Crypto_Finance"
ICI="$(cd "$(dirname "$0")" && pwd)"
MANIFESTE="$ICI/../cache_manifest.txt"
APPLIQUER=0
[ "${1:-}" = "--appliquer" ] && APPLIQUER=1

dire() { [ "$APPLIQUER" = 1 ] && echo "  $*" || echo "  [simulation] $*"; }

[ -d "$SITE" ] || { echo "Dépôt du site introuvable : $SITE"; exit 1; }
[ -f "$MANIFESTE" ] || { echo "Manifeste introuvable : $MANIFESTE"; exit 1; }

echo "── 1. Fonction de lecture des données"
if [ "$APPLIQUER" = 1 ]; then
  mkdir -p "$SITE/functions/data"
  cp "$ICI/functions/data/[[file]].js" "$SITE/functions/data/[[file]].js"
fi
dire "functions/data/[[file]].js installée"

echo "── 2. Redirections des adresses existantes"
# Une règle par fichier de données. Les pages continuent de demander
# « /truc_cache.js » ; la redirection les envoie vers la fonction. Aucune page,
# aucun script, aucune balise à modifier — et le site local n'est pas touché.
REDIR="$SITE/_redirects_data"
{
  echo "# Données servies par la fonction /data (générées, ne pas éditer à la main)."
  echo "# Chaque ligne redirige l'adresse historique d'un fichier de cache vers la"
  echo "# fonction, qui lit le dépôt de collecte. Sans ces règles, les pages"
  echo "# continueraient de lire la copie figée du déploiement."
  while read -r f; do
    [ -n "$f" ] && echo "/$f  /data/$f  302"
  done < "$MANIFESTE"
} > "/tmp/_redirects_data.$$"
N=$(grep -c '^/' "/tmp/_redirects_data.$$")
if [ "$APPLIQUER" = 1 ]; then mv "/tmp/_redirects_data.$$" "$REDIR"; else rm -f "/tmp/_redirects_data.$$"; fi
dire "$N redirections préparées (plafond Cloudflare : 2 000)"

echo "── 3. Limitation des routes de fonctions"
# Sans ce fichier, TOUTE requête du site passe par le runtime des fonctions et
# consomme le quota gratuit (100 000/jour). En le limitant aux deux routes qui en
# ont besoin, le reste du site redevient du statique — gratuit et illimité.
ROUTES="$SITE/public/_routes.json"
cat > "/tmp/_routes.json.$$" <<'JSON'
{
  "version": 1,
  "include": ["/data/*", "/live/*"],
  "exclude": []
}
JSON
if [ "$APPLIQUER" = 1 ]; then mkdir -p "$SITE/public" && mv "/tmp/_routes.json.$$" "$ROUTES"; else rm -f "/tmp/_routes.json.$$"; fi
dire "_routes.json limité à /data/* et /live/*"

echo
echo "── Reste à faire À LA MAIN dans build_public.sh (deux points) :"
echo "   a. autoriser 'data' dans la liste blanche des fonctions déployables,"
echo "      à côté de 'live' — sinon le build la retire comme les autres ;"
echo "   b. après la copie de public/, déplacer les fichiers du manifeste"
echo "      vers public/data/ et concaténer _redirects_data dans public/_redirects."
echo
echo "   Ces deux points touchent votre chaîne de déploiement : je préfère qu'ils"
echo "   soient relus plutôt qu'appliqués par un script."
[ "$APPLIQUER" = 1 ] || echo
[ "$APPLIQUER" = 1 ] || echo "Rien n'a été écrit. Relancer avec --appliquer pour installer."
