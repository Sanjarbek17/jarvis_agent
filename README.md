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
web/                 the website, served by core/appserver.py
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
Telegram: plain text or a voice message = talk to Jarvis, `/status [project]`, `/stop <project|mac>`, `/call`, `/transcript`, `/clear`, `/logoutall` (ends every app and website login, so also Mac control), `/guard [off|status|test]`, `/2fa <password>` (only while `scripts/qr_login.py` waits for it; the message is deleted at once).
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
Ticket news goes to its project's chat (ticket f4f11c38, `appserver.project_chat`): asked in General or in Updates, a
ticket's result lands in that project's chat in the app (the chat he asked in if it's that project's, else its latest
one, else a new "<project> updates" chat); asked on Telegram, it still goes to Telegram too. A General app chat in which
he files tickets for one project moves under that project (`follow_ticket`); one about several projects stays.
Answers go back where Sanjar asked (ticket 799dda33, `core/origin.py`): each app chat's brain sends
`X-Jarvis-Origin: app:<chat id>` on its tool calls; add_task / queue_ticket / answer_task remember it per ticket
(`var/state/ticket_origin.json`, note "added from the app"), and that ticket's result is written into the same
app chat (plus the app's notification feed) instead of Telegram. That exact chat, not just any chat of the project
(ticket 484828da): a call without an app chat (Jarvis answering or re-queueing the ticket itself, the self-check)
never clears the recorded chat; only asking again from another app chat moves it there.
His own passwords (ticket 5ad3f371): when Sanjar himself asks for the login of one of his own accounts, Jarvis may
give it in his Telegram DM or an app/website chat opened with a login code (`[signed in]` on each message); never
on calls, to the APK's built-in key (`[not signed in]`, and the reply is scrubbed in code too), or to anyone else.
A message's `text` is always saved scrubbed (`memory.scrub`, .env values re-read every 10 min). A signed-in reply that
held one also keeps its full text in `own`, which `GET /api/chats/<id>/messages` gives back only to his login sessions
(the app and site reload the chat after each reply, so it would vanish otherwise, ticket 7896b7af); the APK key never sees it.
Images in the app (ticket 546d965e): screenshots from mac_task / screenshot asked in an app chat are shown in that
chat (not Telegram); ticket and Mac results carry their screenshots into the app too (the Updates chat gets them even
when the answer went to Telegram). Images are kept shrunk to 1280 px JPEG in `var/state/app_images/` and served at
`GET /api/images/{name}` with the app login. Needs app 0.7+.

Website: `web/` (plain HTML/JS, no build step), served by the same `core/appserver.py`, so the phone app and the
site share one backend, chats and login. It does what the app does: projects, chats, images, voice messages, live
calls, workers, Updates. Open `https://jarvis.sanjarbek17.uz`, or `http://127.0.0.1:8790` on the Mac (the tunnel in
`~/.cloudflared/jarvis-app.yml` passes the whole address; the server 404s anything but `/api/*` and `WEB_FILES`). Login is the Telegram
code; the site never has `APP_TOKEN`. Microphone features need https or localhost, so not the plain home Wi-Fi address.
Only the files in `WEB_FILES` are served; add a new one there. No login is needed for those files: they hold no data.

Moving a chat: the folder button in a chat (website, app 0.8+) moves it to another project, messages and Claude
conversation included (`POST /api/chats/{id}/move`). Jarvis does the same with `move_chat` when asked in that chat.
For a project that doesn't exist yet it files the setup ticket and uses `when_created=True`: the chat waits, and
`finish_moves` moves it within a minute once a project with that name is in config/projects.json.

Files: whatever `scripts/send_file.py` sends to Telegram also shows in the app chat it was asked from (workers and
the Mac worker get `JARVIS_APP_CHAT`), else in Updates. Copies live in `var/state/app_files/` for 30 days, up to
500 MB each, served at `GET /api/files/{name}` with the login. Images show inline. The website plays videos and
downloads the rest. Files sent before this existed are only on Telegram. Other outbox files (like login QRs) stay
out on purpose: only `send_file.py` marks a file for the app.

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

Google Drive (ticket e6199929, `core/drive.py`): the `google_drive` tool hands a Drive job to a short `claude -p` helper
that loads Sanjar's user settings (so it gets the Google Drive connector of his Claude account, on
sanjarsaidov173@gmail.com) but may use only the Drive tools: search, read Docs / Sheets / Slides, create, copy, rename,
move, comment. Never trash; sharing only when he asked. It can't change the text inside an existing Doc or deck
(the connector has no such tool). The helper may use ToolSearch, which waits for a connector that's still connecting,
and is tried once more if the Drive tools still weren't there (ticket c270b9ef).

New slide decks with content (ticket 917f6c5a, `core/slides.py`, tool `google_slides`): the connector can't fill a
deck, but it uploads a file that Google turns into Google Slides. Jarvis draws the deck as a tiny .odp (a title slide
with every step's emoji, one slide per step with a big emoji picture, heading and a few lines, and a recap with arrows;
~3,400 base64 characters, scaled to Google's 27.94 cm page) and the Drive helper copies it into `create_file`, then
reads the deck back to check every slide title arrived. A .pptx was 24,000 characters and got miscopied.

Jarvis's own Google sign-in (ticket c270b9ef, `core/google.py`), for editing inside Docs and Slides and for any of his
Google accounts: `google_sign_in` gives a link (OAuth with PKCE, the OAuth client of `~/dev/server/gdrive_uploader`,
`GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` in `.env`). Signed in on the Mac, a small server on 127.0.0.1:8791 finishes it by
itself; on his phone the last page can't load, so he sends its address and `google_sign_in_finish` takes it.
Refresh tokens per account in `var/state/google_tokens.json`. Tools: `google_files`, `google_file_text`,
`google_replace_text` (Docs and Slides), `google_add_text` (Docs). New files with content (ticket 7ce4c9a5; the connector can
only make an empty deck): `google_create_slides` (title slide plus one slide per heading / lines / optional
picture), `google_add_slide`, `google_create_doc` (headings and text).

Flatmates expenses (ticket d59ccf5e, `core/expenses.py`): the `flatmates_expenses` tool reads the group's expense topic
with Jarvis's own Telegram and adds it up per person for Sanjar. A short number means
thousands ("10 tok" is 10,000) and several numbers on a line all count ("14, 10"); a line with no number is listed to
check. Check photos and PDFs are read by a small model once per message (`var/state/expense_checks.json`), and a check for
money the same person wrote down within a day counts once (ticket 4eb67dbf). When he asks, `flatmates_post_expenses` posts it in that
topic: a short summary split between the five, who pays and who gets, and a text file with every line (ticket 32abbb65).

Talking over Jarvis on a call (ticket ea6c257f, `Interrupter` in `core/voice.py`): the sound level on the line pauses
Jarvis in about 0.2 s, with no network. Words from the transcription confirm it and drop the rest of the reply; with no words
it carries on and that sound won't pause it again. Its own echo is learned in its first 0.4 s of sound.

Reading files: Jarvis can look things up itself (read-only: Read, Glob, Grep) anywhere under
`~/dev`, e.g. "what port does the taxi backend use", and Sanjar's global skills in `~/.claude/skills`
(e.g. taxi-server). `.env` files too (since 2026-11-04, to find ids/tokens before asking Sanjar; values are never repeated); nothing else outside `~/dev`
(home, `~/.ssh`, `~/Documents` stay off limits); changing anything is still a ticket.

Calling other people: "call Olimjon and tell him ..." (by message or on a call). Jarvis looks the
name up in the Jarvis account's Telegram contacts and chats (or takes an @username), asks if it's
not sure who, calls them with a brain that can only deliver the message and hang up, and sends
you the transcript. Asked during a call with you, it calls them after you hang up.

Mac guard (ticket 3b226456, `core/guard.py`): Sanjar arms it himself (`/guard`, or just tell Jarvis "guard"; there is
no automatic away detection, so it can't photograph him by mistake). After 60 s, any keyboard, mouse or trackpad
input sends him a Telegram alert at once, a screenshot, and camera photos, again at most every 2 min while it goes on.
`/guard off` before touching the Mac. It stays on across restarts, and the Mac worker's own clicks don't count. The
camera needs macOS's Camera permission for what runs Jarvis: `/guard test` at the Mac brings up the prompt.

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

Boards on the Mac: a project with `"local": true` keeps its tickets in `var/state/boards.db` (`core/localboard.py`), not
Notion. The website and app show every board (Board in a project's chat list; `/api/board`, `/api/tickets/{ref}`):
search, queue, take off the queue, done, drop, and for boards on the Mac also edit and status. Workers and skills use
`scripts/board.py` (show, list, add, edit, log). Move a board with `scripts/notion_to_local.py <project> --switch`
(it refuses while a ticket is Working); Notion isn't changed and the "board" id stays in projects.json as the way back.
Every night `scripts/backup_boards.py` copies those boards to the private repo `Sanjarbek17/jarvis-boards` as readable JSON (launchd: `com.sanjar.jarvis-board-backup`, log in `var/logs/board-backup.log`). Every password and key value from the `.env` files under `~/dev`, and anything shaped like one, is hidden first. `--restore <folder> <new.db>` rebuilds a board file from a clone.
Images and files stay on the old Notion page, which each copied ticket links to.

Notion reads: each board is read in full at most once a minute (`CACHE_SECONDS` in core/board.py) and filtered
locally, instead of every board on every 10 s check. Jarvis's own writes update that copy at once; if Notion fails,
the last copy is served. So a ticket queued by hand on Notion can take up to a minute to start.

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

Claude usage limit (ticket d23f4461): a worker stopped by it (the stream's `rate_limit_event` "rejected", or a
"hit your limit" result) isn't Failed. The ticket goes back to Queued ("paused by the Claude usage limit"), no new
worker starts until the reset time it announced (+2 min; `var/state/usage_limit.json`, survives restarts), and
then the paused tickets start again in the same session, told to continue where they stopped. Sanjar gets one
note per limit, not one per ticket.

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
