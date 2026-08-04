#!/bin/zsh
# Refresh Macro_Trends.html — runs from TCC-safe location
set -euo pipefail
export PATH="/opt/anaconda3/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SRC="$HOME/Desktop/Site_Crypto_Finance"
CACHE="$HOME/Library/Caches/site_crypto_finance/macro_trends_render"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Macro Trends refresh"

mkdir -p "$CACHE"

rsync -a --delete \
  --include='Macro_Trends.Rmd' \
  --include='custom.css' \
  --include='_site.yml' \
  --include='_after_body.html' \
  --include='reveal.js' \
  --include='Macro_Trends_files/***' \
  --include='google-trends-crisis/***' \
  --include='data/' \
  --include='data/sentiment_index.json' \
  --exclude='*' \
  "$SRC/" "$CACHE/"

# Garde-fou : echec immediat si le JSON sentiment n'est pas dans le cache
# (sinon le knit prend silencieusement la branche null et casse les 2 charts).
if [[ ! -s "$CACHE/data/sentiment_index.json" ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: sentiment_index.json missing or empty in cache — aborting render" >&2
  exit 2
fi

cd "$CACHE"
/usr/local/bin/Rscript -e "rmarkdown::render('Macro_Trends.Rmd', quiet=TRUE)"

# Audit a11y : supprime le h1 auto-title de rmarkdown (doublon avec hero mt-title)
# Cibles : <div id="header">…<h1 class="title toc-ignore">…</h1>…</div>
# sed BSD (macOS) — pattern multi-ligne avec /N pour fusionner lignes consecutives.
/usr/bin/sed -i '' '/<div id="header">/,/<\/div>/d' "$CACHE/Macro_Trends.html"

# Garde-fou post-render (2026-05-29, seuil revu 2026-06-11) : refuse de propager un
# HTML qui a perdu l'injection critique __MACRO_DATA__. Le chunk R qui l'injecte foire
# parfois silencieusement (warning=FALSE qui avale les erreurs) → HTML tronqué.
# Le VRAI garde-fou est MACRO_REFS>=5 (présence de __MACRO_DATA__). Le seuil de taille
# n'est qu'un filet anti-catastrophe : depuis l'externalisation des données vers
# macro_fred_cache.js (cache-first, 2026-05-29) la page saine pèse ~650-700 Ko, pas
# 1,4 Mo — l'ancien seuil 1 Mo abortait donc tous les renders. Plancher abaissé à 400 Ko.
MACRO_REFS=$(grep -c '__MACRO_DATA__' "$CACHE/Macro_Trends.html" 2>/dev/null || echo 0)
SIZE=$(stat -f%z "$CACHE/Macro_Trends.html" 2>/dev/null || echo 0)
if [[ "$MACRO_REFS" -lt 5 || "$SIZE" -lt 400000 ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ABORT propagation : __MACRO_DATA__=$MACRO_REFS (>=5 attendu), size=$SIZE (>=400000 attendu). Ancienne version Desktop preservee." >&2
  exit 3
fi

cp "$CACHE/Macro_Trends.html" "$SRC/Macro_Trends.html"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Macro Trends refresh complete"
