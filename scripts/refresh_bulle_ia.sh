#!/bin/zsh
# Refresh Bulle_IA.html — runs from TCC-safe location
# Copies Rmd + deps to cache dir, renders, copies result back

set -euo pipefail
export PATH="/opt/anaconda3/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SRC="$HOME/Desktop/Site_Crypto_Finance"
CACHE="$HOME/Library/Caches/site_crypto_finance/bulle_ia_render"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Bulle IA refresh"

# Refresh NVDA hist cache (utilise par chart DeepSeek crash + Risk Index fallback)
python3 \
  "$HOME/Library/Application Support/SiteCryptoFinance/fetch_nvda_hist.py" || true

# Create cache render dir
mkdir -p "$CACHE"

# Sync needed files (Rmd + dependencies)
rsync -a --delete \
  --include='Bulle_IA.Rmd' \
  --include='AI_Models.Rmd' \
  --include='custom.css' \
  --include='_site.yml' \
  --include='_after_body.html' \
  --include='logos_svg_b64.js' \
  --include='reveal.js' \
  --include='fetch_ai_models.py' \
  --include='fetch_ai_research.py' \
  --include='ai_models_cache.json' \
  --include='ai_models_cache.js' \
  --include='ai_research_cache.json' \
  --include='mag7_hist_cache.js' \
  --include='pe_hist_cache.js' \
  --include='cycle_cache.js' \
  --include='nvda_hist_cache.js' \
  --include='ai_adoption_cache.js' \
  --include='Macro_Trends.html' \
  --include='Bulle_IA_files/***' \
  --exclude='*' \
  "$SRC/" "$CACHE/"

# Resolve symlinks for cache files (mag7/pe are symlinks to Caches)
for f in mag7_hist_cache.js pe_hist_cache.js cycle_cache.js nvda_hist_cache.js ai_adoption_cache.js; do
  if [ -L "$CACHE/$f" ]; then
    target=$(readlink "$CACHE/$f")
    if [ -f "$target" ]; then
      rm "$CACHE/$f"
      cp "$target" "$CACHE/$f"
    fi
  fi
done

# Render Bulle_IA + AI_Models (data Artificial Analysis live)
cd "$CACHE"
/usr/local/bin/Rscript -e "rmarkdown::render('Bulle_IA.Rmd', quiet=TRUE)"
/usr/local/bin/Rscript -e "rmarkdown::render('AI_Models.Rmd', quiet=TRUE)"

# Copy results back
cp "$CACHE/Bulle_IA.html" "$SRC/Bulle_IA.html"
cp "$CACHE/AI_Models.html" "$SRC/AI_Models.html"
# Refresh cache JSON + JS dans le repo source pour cohérence des autres pages.
# Le .js est INDISPENSABLE : la widget "dernier modèle IA" de l'Accueil le charge
# via <script src="ai_models_cache.js"> en file:// (fetch JSON bloqué CORS).
# Oublier le .js = widget figée à la dernière date de render Bulle_IA, pas live.
cp "$CACHE/ai_models_cache.json" "$SRC/ai_models_cache.json" 2>/dev/null || true
cp "$CACHE/ai_models_cache.js"   "$SRC/ai_models_cache.js"   2>/dev/null || true

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Bulle IA + AI Models refresh complete"
