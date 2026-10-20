# Jarvis

My assistant on the MacBook. I call or message it on Telegram (account @Sanjarbek177),
it puts tasks on each project's own Notion board (as tickets marked Queued in the "Jarvis"
column), and Claude Code workers do them.
Plan and design: [docs/JARVIS_PLAN.md](docs/JARVIS_PLAN.md).

```
jarvis.py            entry point: Telegram calls + messages, notifications, starts everything
core/
  config.py          settings and paths
  voice.py           ears (Gemini transcribe) + mouth (Gemini Live reading text aloud)
  brain.py           Claude brain sessions (call + Telegram chat)
  tools.py           tools the brain can use (MCP on 127.0.0.1:8775 / 8776)
  board.py           Notion API: each project's board + its Jarvis column
  runner.py          picks Queued tickets and runs Claude Code workers
config/projects.json project -> folders in ~/dev + its sprint board
scripts/login.py     one-time Telegram login for the Jarvis account
docs/                plan
var/                 made while running, not in git: Telegram session, logs, state, shots
.env                 keys (not in git)
```

## Running

Jarvis runs as a login agent (`~/Library/LaunchAgents/com.sanjar.jarvis.plist`): it starts
at login, restarts if it crashes, and keeps the Mac awake with `caffeinate`.

- restart: `pkill -f jarvis.py` (the agent starts it again)
- stop for good: `launchctl bootout gui/$(id -u)/com.sanjar.jarvis`
- start again: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sanjar.jarvis.plist`
- by hand (agent stopped): `uv run jarvis.py` or `uv run jarvis.py --call`
- logs: `var/logs/jarvis.log`; each worker run: `var/logs/<project>-<ticket ref>.jsonl`

Telegram: plain text = talk to Jarvis, `/status [project]`, `/stop <project>`, `/call`, `/transcript`.

## Boards

Every project board under Notion "Projects" has two extra columns:
- **Jarvis**: Queued → Working → Needs me / Done / Failed. Set a ticket to *Queued* (in the app,
  by call or by message) and a worker picks it up. Empty = Jarvis leaves it alone.
- **Jarvis note**: last step, the result, or the question when it needs you.
