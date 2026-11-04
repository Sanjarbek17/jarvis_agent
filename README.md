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
scripts/relogin.py   fresh Telegram login, no scan/code: the current login approves it, 2FA from
                     .env; saved as var/jarvis_new.session, swapped in at Jarvis's next start
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

Saying "stop it" about a running ticket is enough: Jarvis stops that worker itself (`stop_worker`, same as
`/stop`); a stopped ticket goes off the queue (⏹), not Failed. Dropping a running ticket stops it first.
Telegram: plain text or a voice message = talk to Jarvis, `/status [project]`, `/stop <project|mac>`, `/call`, `/transcript`, `/clear`, `/2fa <password>` (only while `scripts/qr_login.py` waits for it; the message is deleted at once).
`/joblogin` logs in the Upwork job worker's own Telegram account (+7 906, `~/dev/upwork_job`): a QR in the
hunter bot chat to scan with the phone; `/joblogin code` + `/jobcode 1-2-3-4-5` for the code route. It runs as
the worker's own process on its own session, so Jarvis keeps answering.
Scripts that message Sanjar (`send_file.py`, `qr_login.py`) go through Jarvis's outbox (`core/outbox.py`)
while it runs: a second copy of its Telegram session would make Telegram stop delivering messages to Jarvis.
Send a file (document, photo, …), with or without a caption, and Jarvis saves it to `var/state/inbox/`
and reads it to answer; a file sent as a reply works too.

Mac control: "turn the volume down", "open Spotify and play my liked songs", "find the PDF I
downloaded yesterday and send it to me". Quick one-step things Jarvis runs itself (`run_on_mac`);
anything bigger goes to the Mac worker (`mac_task`): a Claude Code session that uses commands,
AppleScript, and if needed looks at the screen and clicks and types (`scripts/mac.py`). One job at a
time, it remembers earlier Mac jobs; a job longer than a minute reports back in Telegram. Logs:
`var/logs/mac-<time>.jsonl`. Hard limits and "ask first" rules: `MAC_NEVER_DO` in `core/config.py`.
Needs Accessibility + Screen Recording for Jarvis once (System Settings > Privacy & Security);
Jarvis checks at start and messages you if they're missing.

Phone app: the Jarvis App (`~/dev/side_project/projects/jarvis_app`, a ~200 KB APK for Android 5+) talks to
`core/appserver.py` (port 8790, home Wi-Fi, `APP_TOKEN` in .env): projects, chats per project (each its own
Claude conversation that knows the project, kept in `var/state/app_chats/`), voice messages (transcribed like
Telegram voice notes) and live calls over a WebSocket (same ears / brain / voice as Telegram calls).
Away from home the app reaches it over Tailscale: LaunchAgent `com.sanjar.jarvis-tailscale` runs `tailscaled` in
userspace mode (no root, no system network changes; state in `~/.local/share/jarvis-tailscale`), device name
`jarvis-mac`. CLI: `scripts/ts.sh status`.

Memory: Jarvis keeps long-term memory in `var/state/memory/` (facts it was told to `remember`, plus
every chat and call turn, and old call transcripts). Relevant bits are pulled in front of each
message by meaning (Gemini embeddings, Uzbek/Russian/English), so it doesn't guess after a restart,
`/clear` or a new call. Messages are linked into topic threads (a fast Claude model — Haiku via the claude CLI — files each exchange
into a thread and links related ones), so recall brings back the whole conversation on a topic. Tools: `remember`, `recall`, `forget_memory`.
At start Jarvis also imports our whole Telegram chat into memory through its own connection (the first time
all of it, then only newer messages; already-stored ones are skipped). Passwords, keys and tokens are
scrubbed out of everything memory stores (`memory.scrub`).

Core memory (ticket 35ec396e, after OpenClaw; `core/coremem.py`): `var/state/memory/CORE.md` is a short
curated list of Sanjar's lasting decisions, preferences, facts and open requests, one dated line per key.
It is in every chat and call prompt (not found by search), and a new line with the same key replaces the
old one, so two rules never contradict. It is filled by `remember(fact, kind, key)` / `forget_core(key)`
and by a consolidation pass (Opus reads the chats since the last pass, adds, replaces and removes lines;
open requests stay until done) that runs nightly at 03:00, at start, and before `/clear` throws a session
away. When CORE.md changes, the chat brain restarts its session so it sees the new version.
Reliability: messages sent while Jarvis is busy join the next turn together (no queueing one by one); a
worker failure on a ticket Sanjar asked for, or any Needs-me, is always sent to him, with the report's
own lines if the brain stays silent or fails (`relay(..., must=True)`).

Reading files: Jarvis can look things up itself (read-only: Read, Glob, Grep) anywhere under
`~/dev`, e.g. "what port does the taxi backend use", and Sanjar's global skills in `~/.claude/skills`
(e.g. taxi-server). `.env` files too (since 2026-11-04, to find ids/tokens before asking Sanjar; values are never repeated); nothing else outside `~/dev`
(home, `~/.ssh`, `~/Documents` stay off limits); changing anything is still a ticket.

Calling other people: "call Olimjon and tell him ..." (by message or on a call). Jarvis looks the
name up in the Jarvis account's Telegram contacts and chats (or takes an @username), asks if it's
not sure who, calls them with a brain that can only deliver the message and hang up, and sends
you the transcript. Asked during a call with you, it calls them after you hang up.

## Boards

New project: add it to `config/projects.json` (folders + board id). Jarvis notices the change
by itself, no restart; the brain restarts its own process with the new list and keeps the chat.

Self-check (ticket e2d3398a): every 15 min Jarvis checks itself with nobody asking (`core/selfcheck.py`): the runner
task (restarted on the spot if it died) and its heartbeat, workers vs queue with RAM, Needs me, Failed, Working with
no live worker, and Due tickets that haven't started. Only new findings wake the chat brain with a [SELF-CHECK]
message: it acts with its tools (answers Needs me itself unless it truly needs Sanjar, re-queues what failed for a
passing reason or lost its worker) and sends a short 🔎 note of what changed, or nothing. Repeats wait 12 h.

Results are never lost (ticket 80987838): board writes are retried, and if a board stays unreachable (e.g. GitHub's
rate limit) the result is kept in `var/state/pending_results.json` and written later, also after a restart. At start,
tickets left Working go back in the queue (twice at most, then Failed) instead of being marked Failed; the
self-check's in-place runner restart doesn't touch them. Every restart path (restart_when_idle.sh, the watchdog)
waits until no worker runs.

Runner safety (ticket 7493382f): a board that can't be read (Notion/GitHub error, rate limit) is skipped instead of
stopping the whole queue; the GitHub board is read at most every 2 min and its last copy is used when GitHub refuses.
The runner touches `var/state/runner_heartbeat` every loop; LaunchAgent `com.sanjar.jarvis-watchdog` runs
`scripts/watchdog.sh` every 5 min and queues a safe restart if the heartbeat is 20+ min old.

RAM (8 GB Mac): no fixed worker count. Another worker starts whenever the memory that's really available (the larger of
free + inactive + purgeable from vm_stat and macOS's own free percentage) minus a 1 GB reserve fits one more worker
of the measured size (~300-500 MB); the reserve doubles under real pressure (the kernel's "warning" while under 25%
is free, or nearly full swap) and grows while a heavy job runs; "critical" holds
new starts; one worker may always run; one new start per 30 s (`core/ramguard.py`). Why a ticket waits is logged,
saved in `var/state/runner_status.json` and shown by `get_status`. Fair turns: one worker per project, tickets with a
"Due" date first, then the project whose last worker started longest ago. Heavy commands go through `scripts/heavy.py`:
one at a time, a second only with 3.5 GB+ available.

Branches: a worker merges its finished, tested work into the project's `dev` branch itself (no
asking) and pushes `dev`. Sanjar's standing rules live in `config/rules.md` (workers and the brain
read it live). You test `dev` as a whole, then tell Jarvis once to merge it to main.
Workers never touch main unless you allow it for one ticket ("you may push ticket X to main").
Jarvis stores that in `var/state/grants.json` (not in Notion, so a worker can't grant itself); it
ends when the ticket is Done, or after 7 days.

A project can keep its tickets as **GitHub Issues** instead (AI English Teacher): `"github": {repo, owner,
project, prefix}` in projects.json instead of `"board"`. Refs look like `aet-12` (issue #12). Queue an issue
by adding the label `jarvis:queued` (or ask Jarvis); the Jarvis column is the `jarvis:*` labels, the
board Status is the GitHub Project's Status field (Done also closes the issue, Dropped closes it as not
planned), and results / answers are issue comments (`core/ghboard.py`, needs `gh` with the project scope).

Every project (unless `"auto_answer": false`; Sanjar 2026-11-04 "only send it to you, then you tell me if needed"):
when a worker asks "Needs me", Jarvis answers it (memory of Sanjar's chats, the project's docs, the rules; `core/autoanswer.py`), posts the answer on
the ticket and re-queues it without messaging Sanjar. Only if it truly needs him, Jarvis tells him in plain words
what to do and why (not the worker's wording). Real-world steps only he can do become a checklist on the
ticket; "merge dev into main?" still goes to him; after 4 answers on one ticket it goes to him too.

Worker and runner messages (Done, Failed, Needs me, the stuck list, Mac task results) never go to Sanjar
directly (2026-11-04): they go to Jarvis's chat brain as a [WORKER REPORT] (`relay` in jarvis.py). Jarvis acts
where he can and sends Sanjar 1-2 plain lines only when it matters to him; reports aren't saved to memory.

Every project board under Notion "Projects" has two extra columns:
- **Jarvis**: Queued → Working → Needs me / Done / Failed. Set a ticket to *Queued* (in the app,
  by call or by message) and a worker picks it up. Empty = Jarvis leaves it alone.
- **Jarvis note**: last step, the result, or the question when it needs you.
