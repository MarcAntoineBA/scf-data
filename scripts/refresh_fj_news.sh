#!/bin/zsh
# Watchdog wrapper pour fetch_fj_news.py (FinancialJuice -> FR, widget Accueil).
# Même pattern anti-zombie macOS que fetch_tradfi_with_timeout.sh (caffeinate +
# deadline wall-clock). Travail borné (1 flux + <=18 trads x 12s) -> cap 300s.

PYTHON=python3
SCRIPT="$HOME/Library/Application Support/SiteCryptoFinance/fetch_fj_news.py"
TIMEOUT_SEC=300

cd "$HOME/Library/Application Support/SiteCryptoFinance" || exit 1

/usr/bin/caffeinate -is "$PYTHON" "$SCRIPT" &
PY_PID=$!

DEADLINE=$(( $(date +%s) + TIMEOUT_SEC ))
(
  while [ $(date +%s) -lt $DEADLINE ]; do
    kill -0 $PY_PID 2>/dev/null || exit 0
    sleep 15
  done
  if kill -0 $PY_PID 2>/dev/null; then
    echo "[watchdog] hard-kill pid=$PY_PID after ${TIMEOUT_SEC}s" >&2
    kill -9 $PY_PID 2>/dev/null
    pkill -9 -P $PY_PID 2>/dev/null
  fi
) &
WATCHDOG_PID=$!

wait $PY_PID
EXIT_CODE=$?
kill $WATCHDOG_PID 2>/dev/null
wait $WATCHDOG_PID 2>/dev/null
exit $EXIT_CODE
