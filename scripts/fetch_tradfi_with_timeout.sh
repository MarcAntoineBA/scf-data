#!/bin/zsh
# External watchdog wrapper for fetch_tradfi.py.
# Wall-clock deadline loop + caffeinate : evite que /bin/sleep ou le Timer Python
# soient figes pendant App Nap / suspension macOS (cas zombie 2026-05-05).

PYTHON=python3
SCRIPT="$HOME/Library/Application Support/SiteCryptoFinance/fetch_tradfi.py"
TIMEOUT_SEC=3000   # 50 min hard cap (wall clock) — yfinance peut bloquer 60min/ticker quand Yahoo throttle

cd "$HOME/Library/Application Support/SiteCryptoFinance" || exit 1

# stdout Python est block-buffered quand il pointe sur un fichier : lors d'un
# kill -9, tout le buffer non vide est PERDU. Le 2026-07-28, tradfi.out.log
# n'avait donc rien du run mort — diagnostic impossible, seul stderr (unbuffered)
# subsistait. PYTHONUNBUFFERED=1 rend les deux flux exploitables post-mortem.
export PYTHONUNBUFFERED=1

# caffeinate -i empeche l'idle-sleep pendant le fetch (sinon les sleeps du
# watchdog ne ticquent pas). -s tient aussi le system awake quand sur secteur.
/usr/bin/caffeinate -is "$PYTHON" "$SCRIPT" &
PY_PID=$!

DEADLINE=$(( $(date +%s) + TIMEOUT_SEC ))

(
  while [ $(date +%s) -lt $DEADLINE ]; do
    kill -0 $PY_PID 2>/dev/null || exit 0
    sleep 30
  done
  if kill -0 $PY_PID 2>/dev/null; then
    echo "[watchdog] hard-kill pid=$PY_PID after ${TIMEOUT_SEC}s wall-clock" >&2
    kill -9 $PY_PID 2>/dev/null
    # tuer aussi les enfants Python eventuels (yfinance threads/processes)
    pkill -9 -P $PY_PID 2>/dev/null
  fi
) &
WATCHDOG_PID=$!

wait $PY_PID
EXIT_CODE=$?

kill $WATCHDOG_PID 2>/dev/null
wait $WATCHDOG_PID 2>/dev/null

exit $EXIT_CODE
