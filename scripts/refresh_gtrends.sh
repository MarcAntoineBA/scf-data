#!/bin/zsh
# Refresh Google_Trends.html — re-knit depuis un emplacement TCC-safe.
#
# POURQUOI CE SCRIPT (2026-06-15) : update_gtrends.py rafraichit les caches JSON
# (Library/Caches, via com.l'auteur.gtrends.serpapi) mais RIEN ne re-genere le HTML.
# Le Rmd inline les donnees dans window.__GTRENDS__ AU KNIT → la page restait gelee
# au dernier knit manuel (donnees vieilles de plusieurs jours/semaines).
# Ce script re-knit la page a partir des caches canoniques. Pattern calque sur
# refresh_macro_trends.sh (render isole dans Caches, garde-fous, cp HTML vers Desktop).
set -euo pipefail
export PATH="/opt/anaconda3/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export RSTUDIO_PANDOC="/opt/anaconda3/bin"

SRC="$HOME/Desktop/Site_Crypto_Finance"
CANON="$HOME/Library/Caches/site_crypto_finance"          # caches canoniques (ecrits par update_gtrends.py)
CACHE="$HOME/Library/Caches/site_crypto_finance/gtrends_render"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Google Trends refresh"

mkdir -p "$CACHE"

# 1. Rmd + assets de presentation depuis Desktop (source de verite du code)
rsync -a \
  --include='Google_Trends.Rmd' \
  --include='custom.css' \
  --include='_after_body.html' \
  --include='_site.yml' \
  --include='assets/' \
  --include='assets/google_trends_hero.png' \
  --include='Google_Trends_files/***' \
  --include='gtrends_widgets.js' \
  --include='google_logo_3d.js' \
  --exclude='*' \
  "$SRC/" "$CACHE/"

# 2. Caches JSON depuis Library/Caches (canonique = TOUJOURS le plus frais,
#    independamment du timing de mirroring vers Desktop). Le Rmd les lit par
#    chemin relatif dans son working dir.
CACHE_OK=1
for f in gtrends_cache.json gtrends_cache_US.json gtrends_cache_FR.json \
         gtrends_cache_meta.json gtrends_cache_US_meta.json gtrends_cache_FR_meta.json; do
  if [[ -s "$CANON/$f" ]]; then
    cp -f "$CANON/$f" "$CACHE/$f"
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARN: $f absent dans Library/Caches" >&2
    [[ "$f" == "gtrends_cache.json" ]] && CACHE_OK=0
  fi
done

# Garde-fou amont : sans le cache World, le knit produirait une page vide.
if [[ "$CACHE_OK" -ne 1 ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: gtrends_cache.json (World) introuvable — abort" >&2
  exit 2
fi

# 3. Render
cd "$CACHE"
/usr/local/bin/Rscript -e "rmarkdown::render('Google_Trends.Rmd', quiet=TRUE)"

# 4. Garde-fou post-render : refuse de propager un HTML qui a perdu l'injection
#    critique window.__GTRENDS__ (le chunk R peut foirer silencieusement,
#    warning=FALSE avale les erreurs) ou qui est anormalement petit.
GT_REFS=$(grep -c 'window.__GTRENDS__=' "$CACHE/Google_Trends.html" 2>/dev/null || echo 0)
SIZE=$(stat -f%z "$CACHE/Google_Trends.html" 2>/dev/null || echo 0)
if [[ "$GT_REFS" -lt 1 || "$SIZE" -lt 1000000 ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ABORT propagation : __GTRENDS__=$GT_REFS (>=1 attendu), size=$SIZE (>=1000000 attendu). Version Desktop preservee." >&2
  exit 3
fi

# 5. Propagation vers Desktop : HTML + assets _files (self_contained:false).
cp "$CACHE/Google_Trends.html" "$SRC/Google_Trends.html"
rsync -a "$CACHE/Google_Trends_files/" "$SRC/Google_Trends_files/" 2>/dev/null || true

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Google Trends refresh complete (size=$SIZE, __GTRENDS__=$GT_REFS)"
