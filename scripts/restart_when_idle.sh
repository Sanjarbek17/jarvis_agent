#!/bin/sh
# Restart Jarvis safely: stop it when there is no phone call or active chat answer; launchd
# starts it again within seconds. Running ticket workers run detached and are adopted by the
# new Jarvis process (ticket d0bc8d2d, 2475b845), so restart does not wait for running workers.
#
#   scripts/restart_when_idle.sh            wait for calls and active answers (default)
#   scripts/restart_when_idle.sh --now-ish  drop active calls too (Sanjar said dropping the call is fine)
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
busy() {   # an interactive Mac task, a call brain ("live phone call") or the brain of a
           # group voice chat Jarvis sits in ("You joined the group voice chat")
  ps -axww -o ppid=,args= | awk -v p="$JPID" -v c="$CALLS" '$1 == p && ((($2 ~ /(claude|agy)$/) && $3 == "-p" && $4 == "Mac" && $5 == "task") || (c == "yes" && (/live phone call/ || /You joined the group voice chat/))) { f = 1 } END { exit !f }' && return 0
  # a chat answer in progress (app or Telegram): restarting killed his answers at 02:08 and 03:22 (ticket 11ff3b8a)
  find "$(dirname "$0")/../var/state/answering" -type f -mmin -20 2>/dev/null | grep -q . && return 0
  return 1
}
echo "$(date) restarting Jarvis $JPID$([ $CALLS = yes ] && echo ' (waiting if calls/answers active)')" >> "$LOG"
FLAG="$(dirname "$0")/../var/restart_pending"
touch "$FLAG"
trap 'rm -f "$FLAG"' EXIT
while [ "$(date +%s)" -lt "$end" ]; do
  kill -0 "$JPID" 2>/dev/null || { echo "$(date) Jarvis $JPID already gone, nothing to do" >> "$LOG"; exit 0; }
  if ! busy; then
    echo "$(date) idle, restarting Jarvis $JPID" >> "$LOG"; kill "$JPID"; exit 0
  fi
  sleep 5
done
echo "$(date) gave up: still busy after 3 h, not restarted" >> "$LOG"
