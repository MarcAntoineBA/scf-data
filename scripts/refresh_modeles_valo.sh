#!/bin/zsh
# Refresh Modeles_Valorisation_Crypto.html — runs from TCC-safe location.
#
# POURQUOI CE SCRIPT EXISTE (bug récurrent "le modèle n'est pas à jour") :
#   L'onglet Modeles_Valorisation_Crypto baked tout l'historique BTC + les
#   courbes du modèle MCO dans __BOURSE_DATA__ AU MOMENT DU RENDER. Le JS live
#   n'ajoute qu'UN point "aujourd'hui" (CoinGecko) et ÉTEND LE MODÈLE À PLAT.
#   Donc sans re-render, R²/MAPE et les bandes du modèle restent figés à la
#   date du dernier knit manuel → l'utilisateur voit un modèle périmé.
#   Fix durable = re-render quotidien automatique (cf mémoire
#   project_modeles_valo_chart_live_fix : "Limite restante" enfin câblée).
set -euo pipefail
export PATH="/opt/anaconda3/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# pandoc est dans /opt/anaconda3/bin ; rmarkdown a besoin de le trouver.
export RSTUDIO_PANDOC="/opt/anaconda3/bin"

SRC="$HOME/Desktop/Site_Crypto_Finance"
CACHE="$HOME/Library/Caches/site_crypto_finance/modelesvalo_render"
RMD="Modeles_Valorisation_Crypto.Rmd"
HTML="Modeles_Valorisation_Crypto.html"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Modeles Valo refresh"

mkdir -p "$CACHE"

# Copier seulement les fichiers nécessaires au knit (Rmd + thème + includes).
rsync -a --delete \
  --include="$RMD" \
  --include='custom.css' \
  --include='_site.yml' \
  --include='_after_body.html' \
  --include='reveal.js' \
  --include='Modeles_Valorisation_Crypto_files/***' \
  --exclude='*' \
  "$SRC/" "$CACHE/"

cd "$CACHE"

# Le knit fetch live Yahoo (BTC/NDX/DXY/Or/VIX/Brent) + FRED JSON (WM2NS/DGS10/DFF).
# L'Rmd a un stop() explicite si une série critique manque → render non-zéro,
# donc set -e nous fait sortir SANS écraser la bonne version sur le Desktop.
/usr/local/bin/Rscript -e "rmarkdown::render('$RMD', quiet=TRUE)" 2>&1 \
  | grep -v "^\[WARNING\] Deprecated" || true

if [[ ! -f "$CACHE/$HTML" ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERREUR : render n'a pas produit $HTML — on garde l'ancienne version." >&2
  exit 1
fi

# Strip legacy IE polyfills injectés par le template Bootstrap 3 (inutiles 2026).
sed -i '' \
  -e '/bootstrap-3.3.5\/shim\/html5shiv.min.js/d' \
  -e '/bootstrap-3.3.5\/shim\/respond.min.js/d' \
  "$CACHE/$HTML"

# GARDE-FOU FRAÎCHEUR : ne copier sur le Desktop QUE si le dernier point réel
# baké date d'aujourd'hui (UTC). Empêche d'écraser un bon render par un render
# silencieusement périmé (ex. Yahoo a renvoyé un historique tronqué).
FRESH=$(/opt/anaconda3/bin/python3 - "$CACHE/$HTML" <<'PY'
import re, json, sys, datetime
h = open(sys.argv[1]).read()
m = re.search(r'__BOURSE_DATA__\s*=\s*(\[.*?\]);', h, re.S)
if not m:
    print("NO_DATA"); sys.exit(0)
d = json.loads(m.group(1))
act = [p for p in d if p.get('a') is not None]
if not act:
    print("NO_ACTUAL"); sys.exit(0)
last = datetime.datetime.fromtimestamp(act[-1]['t'], datetime.UTC).date()
today = datetime.datetime.now(datetime.UTC).date()
# tolérance 2 jours (week-end / décalage fuseau Yahoo)
print("OK" if (today - last).days <= 2 else f"STALE:{last}")
PY
)

if [[ "$FRESH" == OK ]]; then
  cp "$CACHE/$HTML" "$SRC/$HTML"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Modeles Valo refresh complete (données fraîches, copié sur Desktop)"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️  Render périmé ($FRESH) — NON copié, ancienne version conservée." >&2
  exit 1
fi
