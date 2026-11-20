#!/bin/sh
# Restart Jarvis safely: wait until it has no ticket workers (and no phone call), then stop it;
# launchd starts it again within seconds. Never `pkill -f jarvis.py`: that also matches the
# caffeinate helper and kills the workers mid-ticket.
#
#   scripts/restart_when_idle.sh            wait for workers and calls (default)
#   scripts/restart_when_idle.sh --now-ish  wait for workers only (Sanjar said dropping the call is fine)
#
# Runs in the background on its own (start it with nohup ... &); gives up after 3 hours.
# Log: var/logs/restart.log
HERE=$(cd "$(dirname "$0")/.." && pwd); LOG="$HERE/var/logs/restart.log"
CALLS=yes; [ "$1" = "--now-ish" ] && CALLS=no
# only one waiting restart at a time. Look for the script itself being run by sh: `pgrep -f` also matched
# whatever shell started it (its command line contains the name), so it refused to start and said one
# was already waiting when none was (Mac guard, ticket 6e558d62)
if ps -axo pid=,command= | awk -v me="$$" '$1 != me && $2 ~ /(^|\/)(ba|da|z)?sh$/ && $3 ~ /(^|\/)restart_when_idle\.sh$/ { f = 1 } END { exit !f }'; then
  echo "$(date) a restart is already waiting, not starting another" >> "$LOG"; exit 0
fi
# Jarvis's own pid as launchd knows it
JPID=$(launchctl print "gui/$(id -u)/com.sanjar.jarvis" | awk '$1 == "pid" && $2 == "=" { print $3; exit }')
[ -n "$JPID" ] || { echo "$(date) Jarvis isn't running under launchd, nothing to do" >> "$LOG"; exit 0; }
end=$(( $(date +%s) + 3*3600 ))
busy() {   # a worker (claude/agy -p Ticket ... / Mac task ...), a call brain ("live phone call") or the brain of a
           # group voice chat Jarvis sits in ("You joined the group voice chat")
  ps -axww -o ppid=,args= | awk -v p="$JPID" -v c="$CALLS" '$1 == p && ((($2 ~ /(claude|agy)$/) && $3 == "-p" && ($4 == "Ticket" || ($4 == "Mac" && $5 == "task"))) || (c == "yes" && (/live phone call/ || /You joined the group voice chat/))) { f = 1 } END { exit !f }' && return 0
  # a chat answer in progress (app or Telegram): restarting killed his answers at 02:08 and 03:22 (ticket 11ff3b8a)
  find "$(dirname "$0")/../var/state/answering" -type f -mmin -20 2>/dev/null | grep -q . && return 0
  # workers adopted after an earlier restart aren't Jarvis's children any more (ticket 2475b845)
  for w in $(grep -o '"pid": [0-9]*' "$(dirname "$0")/../var/state/workers.json" 2>/dev/null | grep -o '[0-9]*$'); do
    ps -p "$w" -o args= 2>/dev/null | grep -E -q '^(claude|agy) -p Ticket' && return 0
  done
  return 1
}
echo "$(date) waiting for workers$([ $CALLS = yes ] && echo ' and calls') of $JPID to finish" >> "$LOG"
# while this waits, the runner starts no new workers (core/runner.py RESTART_PENDING), or a busy queue
# would keep the restart from ever happening
FLAG="$(dirname "$0")/../var/restart_pending"
touch "$FLAG"
trap 'rm -f "$FLAG"' EXIT
while [ "$(date +%s)" -lt "$end" ]; do
  kill -0 "$JPID" 2>/dev/null || { echo "$(date) Jarvis $JPID already gone, nothing to do" >> "$LOG"; exit 0; }
  if ! busy; then
    sleep 30   # let the runner post the last result to Notion and Telegram
    busy && continue
    echo "$(date) idle, restarting Jarvis $JPID" >> "$LOG"; kill "$JPID"; exit 0
  fi
  sleep 20
done
echo "$(date) gave up: still busy after 3 h, not restarted" >> "$LOG"
