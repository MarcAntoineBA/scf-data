#!/bin/zsh
# Refresh Ecosysteme_Crypto.html — actualise le Poids Secteur S&P 500 et le graphique 2 ans
# Pattern TCC-safe : copie Rmd + deps dans cache, rend, recopie le HTML final.

set -euo pipefail
export PATH="/opt/anaconda3/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SRC="$HOME/Desktop/Site_Crypto_Finance"
CACHE="$HOME/Library/Caches/site_crypto_finance/ecosysteme_render"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Ecosysteme refresh"

mkdir -p "$CACHE"

rsync -a --delete \
  --include='Ecosysteme_Crypto.Rmd' \
  --include='custom.css' \
  --include='_site.yml' \
  --include='_after_body.html' \
  --include='crypto_logos_svg_b64.js' \
  --include='Ecosysteme_Crypto_files/***' \
  --exclude='*' \
  "$SRC/" "$CACHE/"

cd "$CACHE"
/usr/local/bin/Rscript -e "rmarkdown::render('Ecosysteme_Crypto.Rmd', quiet=TRUE)"

cp "$CACHE/Ecosysteme_Crypto.html" "$SRC/Ecosysteme_Crypto.html"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Ecosysteme refresh complete"
