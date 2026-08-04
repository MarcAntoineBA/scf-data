#!/bin/zsh
# Refresh Valorisation_L1_Blockchain.html — fetch + re-knit + post-render.
# Chaîne : fetch_l1_valuation.py (cache JSON) → rmarkdown::render → post_render_l1.sh
# Pattern TCC-safe : rend dans un cache directory puis recopie le HTML final.

set -euo pipefail
export PATH="/opt/anaconda3/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SRC="$HOME/Desktop/Site_Crypto_Finance"
APP_SUPPORT="$HOME/Library/Application Support/SiteCryptoFinance"
CACHE="$HOME/Library/Caches/site_crypto_finance/l1_valuation_render"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Valorisation L1 refresh"

# === 1. Fetch cache (CoinGecko + DeFiLlama + blockchain.info) ===
python3 \
  "$APP_SUPPORT/fetch_l1_valuation.py" --force \
  || { echo "[$(date '+%H:%M:%S')] fetch failed, keeping previous cache"; }

# === 2. Stage Rmd + deps in TCC-safe cache dir ===
mkdir -p "$CACHE"
rsync -a --delete \
  --include='Valorisation_L1_Blockchain.Rmd' \
  --include='custom.css' \
  --include='_site.yml' \
  --include='_fontawesome.html' \
  --include='_after_body.html' \
  --include='valorisation_l1_overlay.css' \
  --include='Valorisation_L1_Blockchain_files/***' \
  --exclude='*' \
  "$SRC/" "$CACHE/"

# === 3. Knit Rmd (reads live cache JSON via jsonlite::fromJSON) ===
cd "$CACHE"
/usr/local/bin/Rscript -e "rmarkdown::render('Valorisation_L1_Blockchain.Rmd', quiet=TRUE)"

# === 4. Copy back the rendered HTML ===
cp "$CACHE/Valorisation_L1_Blockchain.html" "$SRC/Valorisation_L1_Blockchain.html"

# === 5. Post-render patches (nav-toggle, hero fixes, live JS disclaimer) ===
bash "$APP_SUPPORT/post_render_l1.sh"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Valorisation L1 refresh complete"
