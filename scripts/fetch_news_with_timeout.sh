#!/bin/zsh
# External watchdog wrapper pour fetch_news.py.
# Cree 2026-05-20 apres zombie 5j 21h (PID 45701) — meme pattern que le
# wrapper tradfi : wall-clock deadline + caffeinate pour eviter App Nap qui
# fige /bin/sleep, fix le Timer Python in-process non-fiable sur macOS.

PYTHON=python3
SCRIPT="$HOME/Library/Application Support/SiteCryptoFinance/fetch_news.py"
TIMEOUT_SEC=300   # 5 min hard cap (10+ flux RSS, chacun ~10-30s, total <2min normal)

cd "$HOME/Library/Application Support/SiteCryptoFinance" || exit 1

# caffeinate -is : evite idle-sleep system pendant le fetch
/usr/bin/caffeinate -is "$PYTHON" "$SCRIPT" --force &
PY_PID=$!

DEADLINE=$(( $(date +%s) + TIMEOUT_SEC ))

(
  while [ $(date +%s) -lt $DEADLINE ]; do
    kill -0 $PY_PID 2>/dev/null || exit 0
    sleep 15
  done
  if kill -0 $PY_PID 2>/dev/null; then
    echo "[news-watchdog] hard-kill pid=$PY_PID after ${TIMEOUT_SEC}s wall-clock" >&2
    kill -9 $PY_PID 2>/dev/null
    # tue aussi les enfants Python eventuels (feedparser threads, urllib pool)
    pkill -9 -P $PY_PID 2>/dev/null
  fi
) &
WATCHDOG_PID=$!

wait $PY_PID
EXIT_CODE=$?

kill $WATCHDOG_PID 2>/dev/null
wait $WATCHDOG_PID 2>/dev/null

exit $EXIT_CODE
