#!/bin/zsh
# Refresh Sentiment Marche (CNN Fear & Greed reproduit, 6 composantes equal-weighted).
# Refonte 2026-05-26 : remplace refresh_sentiment_index.sh (ancien indice GT+VIX).
# Lance par launchd com.l'auteur.sentiment.fng quotidiennement apres cloture US.
# Sources : Twelve Data (SPY, TLT, 9 SPDR sectors) + FRED (VIXCLS, BAA10Y).
# Cf. project_site_crypto_launchd_pattern.md pour le pattern dual-dir.
set -euo pipefail
cd "$HOME/Library/Application Support/SiteCryptoFinance"

# Twelve Data API key (free tier basic, 8 req/min, 800 req/jour).
# La cle est aussi dans le code Python en fallback ; ici la variable env permet
# rotation rapide sans redeployer le script.
export TD_API_KEY="${TD_API_KEY:-81ceb5874fbd43b7a19268af4c2e148e}"

exec /opt/anaconda3/bin/python fetch_sentiment_fng.py "$@"
