#!/bin/bash
# Refresh complet du cache Radar v3 (Indicateur.html).
# Pipeline : fetch signals -> consolidate -> build scores -> build cache JS.
# Declenche par launchd com.l'auteur.radarv3.refresh toutes les 24h.
set -u
cd "$HOME/Library/Application Support/SiteCryptoFinance/radar_audit"

PY=python3
LOG=/tmp/radarv3_refresh.log

echo "===== $(date '+%Y-%m-%d %H:%M:%S') start =====" >> "$LOG"

# 1. Fetch funding / stablecoins / TVL (Binance + DefiLlama).
$PY fetch_radar_signals_history.py >> "$LOG" 2>&1 || echo "[WARN] fetch_radar_signals_history non-zero" >> "$LOG"

# 2. Fetch klines BTC/ETH (spot + perp) — pour btc_eth_ratio et btc_vol.
$PY fetch_radar_signals_part2.py >> "$LOG" 2>&1 || echo "[WARN] fetch_radar_signals_part2 non-zero" >> "$LOG"

# 3. Fetch OI / Long-Short / Taker (data.binance.vision).
$PY fetch_binance_metrics.py >> "$LOG" 2>&1 || echo "[WARN] fetch_binance_metrics non-zero" >> "$LOG"

# 4. Consolide les data_raw/*.json en radar_signals_history.json.
$PY consolidate_signals.py >> "$LOG" 2>&1 || { echo "[FATAL] consolidate_signals fail" >> "$LOG"; exit 1; }

# 5. Calcule les scores Radar v3 (radar_v3_history.json).
$PY build_radar_v3.py >> "$LOG" 2>&1 || { echo "[FATAL] build_radar_v3 fail" >> "$LOG"; exit 1; }

# 6. Construit le cache JS lu par Indicateur.html (radar_v3_cache.js).
$PY build_radar_v3_cache.py >> "$LOG" 2>&1 || { echo "[FATAL] build_radar_v3_cache fail" >> "$LOG"; exit 1; }

echo "===== $(date '+%Y-%m-%d %H:%M:%S') done =====" >> "$LOG"
