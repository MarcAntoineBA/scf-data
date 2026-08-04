#!/bin/zsh
# Refresh Correlations_Macro_Crypto.html — runs from TCC-safe location
set -euo pipefail
export PATH="/opt/anaconda3/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SRC="$HOME/Desktop/Site_Crypto_Finance"
CACHE="$HOME/Library/Caches/site_crypto_finance/correlations_render"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Correlations refresh"

# Rafraîchir le cache macro AVANT le knit, sinon le R chunk fetch Yahoo direct
# (qui rate-limite régulièrement). fetch_macro_corr.py utilise yfinance + retry
# et n'écrit que si le cache a plus de 6h, donc coût quasi nul si déjà frais.
PYTHON_BIN="python3"
FETCHER="$HOME/Library/Application Support/SiteCryptoFinance/fetch_macro_corr.py"
if [[ -x "$PYTHON_BIN" && -f "$FETCHER" ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Refreshing macro cache (fetch_macro_corr.py)"
  "$PYTHON_BIN" "$FETCHER" 2>&1 | tail -20 || echo "[warn] fetch_macro_corr.py failed, knit utilisera cache existant"
fi

mkdir -p "$CACHE"

rsync -a --delete \
  --include='Correlations_Macro_Crypto.Rmd' \
  --include='custom.css' \
  --include='_site.yml' \
  --include='_after_body.html' \
  --include='reveal.js' \
  --include='Correlations_Macro_Crypto_files/***' \
  --exclude='*' \
  "$SRC/" "$CACHE/"

cd "$CACHE"
/usr/local/bin/Rscript -e "rmarkdown::render('Correlations_Macro_Crypto.Rmd', quiet=TRUE)"

# Strip legacy IE polyfills (html5shiv, respond.js) — inutiles en 2026
# Pandoc/rmarkdown les injecte automatiquement via le template Bootstrap 3
sed -i '' \
  -e '/bootstrap-3.3.5\/shim\/html5shiv.min.js/d' \
  -e '/bootstrap-3.3.5\/shim\/respond.min.js/d' \
  "$CACHE/Correlations_Macro_Crypto.html"

cp "$CACHE/Correlations_Macro_Crypto.html" "$SRC/Correlations_Macro_Crypto.html"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Correlations refresh complete"
