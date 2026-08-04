#!/bin/zsh
# Refresh Backtest.html — re-knit TCC-safe (calqué sur refresh_macro_trends.sh).
#
# POURQUOI : la page Backtest embarque AU KNIT deux indicateurs qui sinon gèlent :
#   - __CRISIS_DATA__ / Sentiment (lu depuis data/sentiment_index.json au render)
#   - __CMC_FNG_DATA__ / Fear&Greed crypto (fetch CMC live au render)
# Les autres données (radar_backtest_cache.js, macro_fred_cache.js, radar_v3_cache.js)
# sont chargées DYNAMIQUEMENT au runtime (?t=Date.now()) → déjà fraîches sans re-knit.
# Avant ce job, Backtest.html n'était jamais re-knit → Sentiment+F&G figés (bug 2026-06-15).
#
# Le radar_backtest_cache lui-même est rafraîchi par scf.backtestradar (backtest_radar.py).
set -euo pipefail
export PATH="/opt/anaconda3/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SRC="$HOME/Desktop/Site_Crypto_Finance"
CACHE="$HOME/Library/Caches/site_crypto_finance/backtest_render"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Backtest refresh"

mkdir -p "$CACHE"

# Synchronise UNIQUEMENT les inputs lus au knit (le reste est chargé au runtime
# côté navigateur depuis le dossier du site, pas besoin au render).
rsync -a --delete \
  --include='Backtest.Rmd' \
  --include='custom.css' \
  --include='_after_body.html' \
  --include='backtest_styles.css' \
  --include='backtest_panel.html' \
  --include='v2/' \
  --include='v2/backtest_panel_v2.html' \
  --include='data/' \
  --include='data/sentiment_index.json' \
  --exclude='*' \
  "$SRC/" "$CACHE/"

# Garde-fou : sentiment_index.json présent et non vide, sinon le knit prend
# silencieusement la branche null et l'indicateur Sentiment casse.
if [[ ! -s "$CACHE/data/sentiment_index.json" ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: sentiment_index.json absent/vide dans le cache — render annulé" >&2
  exit 2
fi

cd "$CACHE"
/usr/local/bin/Rscript -e "rmarkdown::render('Backtest.Rmd', quiet=TRUE)"

OUT="$CACHE/Backtest.html"

# Garde-fou post-render : refuse de propager un HTML tronqué ou amputé de ses
# injections critiques (panel V2 + Sentiment + moteur). Sans ça, un render foireux
# (réseau, pandoc, libs) écraserait la bonne version Desktop.
SIZE=$(stat -f%z "$OUT" 2>/dev/null || echo 0)
PANEL_REFS=$(grep -c 'fciBacktestPanel' "$OUT" 2>/dev/null || echo 0)
CRISIS_REFS=$(grep -c '__CRISIS_DATA__' "$OUT" 2>/dev/null || echo 0)
ENGINE_REFS=$(grep -c 'backtest_engine.js' "$OUT" 2>/dev/null || echo 0)
if [[ "$SIZE" -lt 1500000 || "$PANEL_REFS" -lt 5 || "$CRISIS_REFS" -lt 1 || "$ENGINE_REFS" -lt 1 ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ABORT propagation : size=$SIZE (>=1500000), panel=$PANEL_REFS (>=5), crisis=$CRISIS_REFS (>=1), engine=$ENGINE_REFS (>=1). Version Desktop préservée." >&2
  exit 3
fi

cp "$OUT" "$SRC/Backtest.html"
rsync -a "$CACHE/Backtest_files/" "$SRC/Backtest_files/" 2>/dev/null || true

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backtest refresh complete (size=$SIZE, sentiment+F&G ré-embarqués)"
