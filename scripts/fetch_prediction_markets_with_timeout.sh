#!/bin/zsh
# Watchdog EXTERNE pour fetch_prediction_markets.py.
#
# Pourquoi (incident du 2026-07-28) : la collecte de l'historique de toutes les
# issues enchaîne ~900 appels CLOB. Le fetcher est resté figé 43 min dans une
# lecture SSL (pile `_ssl__SSLSocket_read` → `PySSL_select`) ALORS QUE SIGALRM
# était armé à 40 min : un gestionnaire de signal Python ne s'exécute qu'entre
# deux bytecodes, jamais pendant un appel C bloquant, et le timeout de requests
# n'a pas repris la main non plus. Le job launchd serait resté verrouillé
# indéfiniment, en silence.
#
# Même conclusion que pour fetch_tradfi et fetch_news (cf. mémoire
# project_tradfi_external_watchdog) : seul un watchdog EXTERNE comparant
# l'horloge MURALE (`date +%s`) est fiable — un sleep ou un Timer in-process ne
# ticque pas pendant la veille macOS.

PYTHON=python3
SCRIPT="$HOME/Library/Application Support/SiteCryptoFinance/fetch_prediction_markets.py"
TIMEOUT_SEC=3900   # 65 min d'horloge murale. Relevé de 2880 s le 2026-08-03 :
                   # depuis le balayage par liquidité (~370 marchés au lieu de
                   # 156) le run quotidien enchaîne ~370 historiques de favoris,
                   # ~1 250 historiques d'issues et jusqu'à 10 min de résumés
                   # français — ~30 min mesurées. La borne doit rester NETTEMENT
                   # au-dessus du temps nominal, sinon elle tue des runs sains et
                   # on croit à une panne de source. Le garde-fou interne du .py
                   # (55 min) se déclenche avant, celui-ci est le filet.

cd "$HOME/Library/Application Support/SiteCryptoFinance" || exit 1

# ── VERROU (2026-08-04) ──────────────────────────────────────────────────────
# La collecte est passée de 6 runs/jour à un run toutes les 30 min (cf. plist).
# Le run nominal dure ~90 s, mais celui qui reconstruit l'historique des issues
# (1 fois par jour, ~900 appels CLOB) dure ~30 min et peut mordre sur le créneau
# suivant. Deux fetchers en parallèle écriraient le même cache en même temps :
# on saute le créneau au lieu de courir contre soi-même.
LOCK="/tmp/predmarkets.run.pid"
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "[verrou] collecte déjà en cours (pid $(cat "$LOCK")) — créneau sauté" >&2
  exit 0
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT INT TERM

# stdout Python est block-buffered vers un fichier : au kill -9 tout le buffer
# est perdu et le post-mortem est aveugle.
export PYTHONUNBUFFERED=1

# caffeinate -i : sans lui, l'idle-sleep macOS fige les sleeps du watchdog.
/usr/bin/caffeinate -is "$PYTHON" "$SCRIPT" "$@" &
PY_PID=$!

DEADLINE=$(( $(date +%s) + TIMEOUT_SEC ))

(
  while [ $(date +%s) -lt $DEADLINE ]; do
    kill -0 $PY_PID 2>/dev/null || exit 0
    sleep 30
  done
  if kill -0 $PY_PID 2>/dev/null; then
    echo "[watchdog] hard-kill pid=$PY_PID après ${TIMEOUT_SEC}s d'horloge murale" >&2
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
