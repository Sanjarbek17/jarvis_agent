# Jarvis

My assistant on the MacBook. I call or message it on Telegram (account @Jarvis_Sanjar),
it puts tasks on each project's own Notion board (as tickets marked Queued in the "Jarvis"
column), and Claude Code workers do them.
Plan and design: [docs/JARVIS_PLAN.md](docs/JARVIS_PLAN.md).

```
jarvis.py            entry point: Telegram calls + messages, notifications, starts everything
core/
  config.py          settings and paths
  voice.py           ears (Gemini transcribe) + mouth (Gemini Live reading text aloud)
  brain.py           Claude brain sessions (call + Telegram chat)
  tools.py           tools the brain can use (MCP on 127.0.0.1:8775-8777)
  board.py           Notion API: each project's board + its Jarvis column
  runner.py          picks Queued tickets and runs Claude Code workers
  mac.py             Mac worker: does things on the Mac itself (apps, settings, screen)
config/projects.json project -> folders in ~/dev + its sprint board
scripts/login.py     one-time Telegram login for the Jarvis account
scripts/mac.py       mouse, keyboard and screenshots for the Mac worker
docs/                plan
var/                 made while running, not in git: Telegram session, logs, state, shots
.env                 keys (not in git)
```

## Running

Jarvis runs as a login agent (`~/Library/LaunchAgents/com.sanjar.jarvis.plist`): it starts
at login, restarts if it crashes, and keeps the Mac awake with `caffeinate`.

- restart: say "restart yourself" (Jarvis files a restart ticket), or by hand
  `scripts/restart_when_idle.sh` (waits for workers and calls; see CLAUDE.md). Avoid `pkill -f jarvis.py`:
  it kills running workers and the caffeinate helper
- stop for good: `launchctl bootout gui/$(id -u)/com.sanjar.jarvis`
- start again: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sanjar.jarvis.plist`
- by hand (agent stopped): `uv run jarvis.py` or `uv run jarvis.py --call`
- logs: `var/logs/jarvis.log`; each worker run: `var/logs/<project>-<ticket ref>.jsonl` (Jarvis
  reads these itself when you ask how a ticket is going)

Telegram: plain text or a voice message = talk to Jarvis, `/status [project]`, `/stop <project|mac>`, `/call`, `/transcript`.

Mac control: "turn the volume down", "open Spotify and play my liked songs", "find the PDF I
downloaded yesterday and send it to me". Quick one-step things Jarvis runs itself (`run_on_mac`);
anything bigger goes to the Mac worker (`mac_task`): a Claude Code session that uses commands,
AppleScript, and if needed looks at the screen and clicks and types (`scripts/mac.py`). One job at a
time, it remembers earlier Mac jobs; a job longer than a minute reports back in Telegram. Logs:
`var/logs/mac-<time>.jsonl`. Hard limits and "ask first" rules: `MAC_NEVER_DO` in `core/config.py`.
Needs Accessibility + Screen Recording for Jarvis once (System Settings > Privacy & Security);
Jarvis checks at start and messages you if they're missing.

Reading files: Jarvis can look things up itself (read-only: Read, Glob, Grep) in every project
folder from `config/projects.json` and its own folder, e.g. "what port does the taxi backend use".
No `.env` files, nothing outside those folders; changing anything is still a ticket.

Calling other people: "call Olimjon and tell him ..." (by message or on a call). Jarvis looks the
name up in the Jarvis account's Telegram contacts and chats (or takes an @username), asks if it's
not sure who, calls them with a brain that can only deliver the message and hang up, and sends
you the transcript. Asked during a call with you, it calls them after you hang up.

## Boards

New project: add it to `config/projects.json` (folders + board id). Jarvis notices the change
by itself, no restart; the brain restarts its own process with the new list and keeps the chat.

Push to main: workers never push or merge to main unless you allow it for one ticket ("you may
push ticket X to main"). Jarvis stores that in `var/state/grants.json` (not in Notion, so a worker
can't grant itself); it ends when the ticket is Done, or after 7 days.

Every project board under Notion "Projects" has two extra columns:
- **Jarvis**: Queued → Working → Needs me / Done / Failed. Set a ticket to *Queued* (in the app,
  by call or by message) and a worker picks it up. Empty = Jarvis leaves it alone.
- **Jarvis note**: last step, the result, or the question when it needs you.
