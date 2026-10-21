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
# only one waiting restart at a time
if pgrep -f "restart_when_idle.sh" | grep -qv "^$$\$"; then
  echo "$(date) a restart is already waiting, not starting another" >> "$LOG"; exit 0
fi
# Jarvis's own pid as launchd knows it
JPID=$(launchctl print "gui/$(id -u)/com.sanjar.jarvis" | awk '$1 == "pid" && $2 == "=" { print $3; exit }')
[ -n "$JPID" ] || { echo "$(date) Jarvis isn't running under launchd, nothing to do" >> "$LOG"; exit 0; }
end=$(( $(date +%s) + 3*3600 ))
busy() {   # a worker (claude -p Ticket ... / Mac task ...) or the call brain (its prompt says "live phone call")
  ps -axww -o ppid=,args= | awk -v p="$JPID" -v c="$CALLS" '$1 == p && (($2 == "claude" && $3 == "-p" && ($4 == "Ticket" || ($4 == "Mac" && $5 == "task"))) || (c == "yes" && /live phone call/)) { f = 1 } END { exit !f }'
}
echo "$(date) waiting for workers$([ $CALLS = yes ] && echo ' and calls') of $JPID to finish" >> "$LOG"
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
