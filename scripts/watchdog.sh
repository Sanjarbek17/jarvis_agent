#!/bin/sh
# Jarvis runner watchdog (ticket 7493382f). Run every 5 min by the LaunchAgent com.sanjar.jarvis-watchdog.
# The runner touches var/state/runner_heartbeat on every loop (every ~10 s). If Jarvis is running but the
# heartbeat is older than STALE seconds, the runner is stuck: queue a safe restart (restart_when_idle.sh
# waits for workers and calls, and only one restart can wait at a time).
cd "$(dirname "$0")/.." || exit 0
STALE=1200
LOG=var/logs/watchdog.log
HB=var/state/runner_heartbeat
JPID=$(launchctl print "gui/$(id -u)/com.sanjar.jarvis" 2>/dev/null | awk '$1 == "pid" && $2 == "=" { print $3; exit }')
[ -n "$JPID" ] || exit 0                  # Jarvis isn't running under launchd: nothing to watch
[ -f "$HB" ] || exit 0                    # an older Jarvis without heartbeats
age=$(( $(date +%s) - $(stat -f %m "$HB") ))
# a freshly started Jarvis gets time to write its first heartbeat
started=$(ps -o etime= -p "$JPID" | awk -F'[-:]' '{ s = 0; for (i = 1; i <= NF; i++) s = s * 60 + $i; print s }')
[ "$age" -gt "$STALE" ] && [ "${started:-0}" -gt "$STALE" ] || exit 0
echo "$(date) runner heartbeat is ${age}s old (Jarvis $JPID): queueing a restart" >> "$LOG"
nohup scripts/restart_when_idle.sh >/dev/null 2>&1 &
