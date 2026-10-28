"""Settings shared by every part of Jarvis."""
import json
import logging
import os
import socket
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).parent.parent      # the jarvis folder
VAR = HERE / "var"                       # everything made while running (not in git)
SESSION = VAR / "jarvis"                 # Telegram login of the Jarvis account (.session)
load_dotenv(HERE / ".env")

# IPv6 to Google here takes 1-10 s and times out the Gemini handshake; IPv4 takes 0.1 s
_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda host, port, family=0, *a, **kw: _getaddrinfo(host, port, socket.AF_INET, *a, **kw)

OWNER_ID = int(os.environ["OWNER_ID"])
TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]

PROJECTS_URL = "https://app.notion.com/p/39bb8d1c364a814486faef3db95d72e5"   # Notion "Projects" page

DEV = Path.home() / "dev"
PROJECTS_FILE = HERE / "config" / "projects.json"
RULES_FILE = HERE / "config" / "rules.md"   # Sanjar's standing rules, read live by workers and the brain
PROJECTS: dict[str, dict] = {}   # updated in place by reload_projects(), so imports of it stay live
_projects = {"mtime": 0.0, "version": 0}


def reload_projects() -> bool:
    """Re-read projects.json if it changed on disk (a cheap stat otherwise), so a new project,
    board or folder works without restarting Jarvis. True if the list changed."""
    try:
        mtime = PROJECTS_FILE.stat().st_mtime
        if mtime == _projects["mtime"]:
            return False
        data = json.loads(PROJECTS_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:   # half-saved edit: keep the old list, retry next time
        logging.getLogger("jarvis.config").warning("can't read %s: %r", PROJECTS_FILE.name, e)
        return False
    _projects["mtime"] = mtime
    if data == PROJECTS:
        return False
    PROJECTS.clear()
    PROJECTS.update(data)
    _projects["version"] += 1
    return True


def projects_version() -> int:
    """Goes up each time the project list changes (the brain restarts itself when it does)."""
    reload_projects()
    return _projects["version"]


reload_projects()

STATE = VAR / "state"            # per-project Claude sessions, etc.
SHOTS = VAR / "shots"            # screenshots to send to me
LOGS = VAR / "logs"
CALLS = STATE / "calls"          # call transcripts
INBOX = STATE / "inbox"          # files Sanjar sends in the Telegram chat, saved for the brain to read
for d in (STATE, SHOTS, LOGS, INBOX):
    d.mkdir(exist_ok=True)

# voice
TRANSCRIBE_MODEL = "gemini-3.5-transcribe-live"
TTS_MODEL = "gemini-3.8-live"      # Live model used as a reader: no daily limit on the free plan
TTS_VOICE = "Charon"
CALL_RATE = 48000                 # Telegram call audio, mono s16le

# Jarvis calling me by itself
CALL_ON = {"needs_me", "failed"}  # runner results worth a phone call (done ones only go to Telegram)
DAILY_CALL_AT = (20, 0)           # daily summary call, Mac local time
SOFT_SKILLS_REMINDER_AT = (16, 0) # daily Telegram nudge to continue soft skills, Mac local time
QUIET_HOURS = (23, 8)             # no calls from 23:00 to 08:00, Telegram only
CALL_COOLDOWN = 10 * 60           # at most one automatic call per 10 minutes

# tools server for the Claude brain
TOOLS_PORT = 8775

# runner
POLL_SECONDS = 10
MAX_WORKERS = 3                   # different projects at the same time
NEVER_DO = """Never: delete files outside this project, run `rm -rf` on broad paths, force-push or
rewrite git history on shared branches, push to main/master, deploy to production servers,
touch anything involving money or payments, or change system settings."""
# the same rules for a ticket Sanjar allowed to push to main (see grants.py)
NEVER_DO_MAIN_OK = """Never: delete files outside this project, run `rm -rf` on broad paths, force-push or
rewrite git history on shared branches, deploy to production servers, touch anything involving
money or payments, or change system settings.
- Sanjar explicitly allowed THIS ticket to push / merge to main (or master). Push or merge only
  the changes this ticket is about (e.g. the PRs it names), never force-push, and list exactly
  what you pushed or merged (branch, PR numbers, commits) in your summary."""

def rules_text() -> str:
    """Sanjar's standing rules (config/rules.md), read fresh each time so edits apply at once."""
    try:
        return RULES_FILE.read_text().strip()
    except OSError:
        return ""


# how workers hand off finished code: into each project's `dev` branch, never main without a grant
GIT_FLOW = """When your ticket changes code in a git repo:
- Do the work on your own branch, not directly on `dev` or main. When it's finished and its tests
  pass, merge your branch into the project's `dev` branch yourself and push `dev` if the repo has a
  remote — no need to ask. Create `dev` from the default branch (main/master) if it doesn't exist.
- Never ask Sanjar before merging into `dev`, and never wait for someone else to merge into `dev`:
  if your next step needs something in `dev`, merge it yourself and carry on.
- `dev` is where finished work collects so Sanjar can test it all together. Never touch main/master
  (no commits, merges or pushes) unless THIS ticket was explicitly allowed to, and don't ask him
  about main for a single ticket. Only if your ticket finishes a whole build or sprint (no open
  tickets left for it on the board) end with NEEDS ME: merge dev into main?
- Never force-push or rewrite history. If merging into `dev` hits a conflict you can't safely resolve,
  leave `dev` untouched, keep your branch, and say so in your summary.
- If you made a git worktree for this ticket, clean it up once your branch is merged into `dev` and
  pushed: from the main checkout (not inside the worktree) run `git worktree remove <path>` (add
  `--force` only if it refuses over the merged work), then delete your now-merged local branch. Never
  remove the main checkout, another ticket's worktree, or one with unmerged changes."""
# the Mac worker (core/mac.py) may do anything on the Mac except these
MAC_NEVER_DO = """Never, even if asked in this task: erase or format disks, turn off SIP / FileVault /
the firewall, delete user folders or large sets of files (move to the Trash instead, never `rm -rf`
outside temp folders), send passwords, keys, tokens, .env files or Keychain contents anywhere,
change the login password or Apple ID, or touch anything involving money or payments.
Ask him first (NEEDS ME) before anything that can't be undone: deleting files for good, quitting
apps that may have unsaved work, sending messages or emails as him, uninstalling apps, shutting
down or restarting the Mac (that also stops Jarvis until he's back)."""


def project_dirs(project: str, folder: str = "") -> list[Path]:
    """Folders a task for this project works in. `folder` (inside ~/dev) overrides the default."""
    if folder:
        path = (DEV / folder).resolve()
        if path.is_dir() and DEV in [path, *path.parents]:
            return [path]
    reload_projects()
    dirs = [DEV / f for f in PROJECTS.get(project, {}).get("folders", [])]
    return [d for d in dirs if d.is_dir()] or [DEV]


def projects_text() -> str:
    reload_projects()
    lines = []
    for name, p in PROJECTS.items():
        board = "" if p.get("board") else ", no board"
        lines.append(f"- {name}: {p['about']} (folders: {', '.join(p['folders']) or 'unknown'}{board})")
    return "\n".join(lines)
