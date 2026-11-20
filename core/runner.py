"""Runner loop: picks tickets marked "Queued" in the Jarvis column of each project's Notion
board and runs a Claude Code worker for each.

One ticket at a time per project (they'd edit the same files); how many projects at once is decided by free RAM (core/ramguard.py)
at once. Each project keeps its own Claude session, so it remembers earlier tasks there.
Steps go to the ticket live; Done / Failed / Needs me go to Sanjar via `notify`.
"""
import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from .paths import VAR
from . import autoanswer, board, hookset, grants, localboard, origin, ramguard, tgfmt, usagewatch
from .brain import BRAIN_MODEL   # workers run on the same model as the Jarvis brain (Opus)
from .board import Ticket
from .config import (rules_text, GIT_FLOW, HERE, LOGS, NEVER_DO, NEVER_DO_MAIN_OK, POLL_SECONDS,
                     SHOTS, STATE, project_dirs, AGY_CLI, AGY_WORKER_MODEL, JARVIS_ENGINE)

log = logging.getLogger("jarvis.runner")
SESSIONS = STATE / "sessions.json"
MAX_PER_PROJECT = 3   # workers on one project at once (projects.json "max_workers" overrides); RAM still caps the total


def project_cap(project: str) -> int:
    from .config import PROJECTS, reload_projects
    reload_projects()
    try:
        return max(1, int(PROJECTS.get(project, {}).get("max_workers", MAX_PER_PROJECT)))
    except (TypeError, ValueError):
        return MAX_PER_PROJECT


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)


def git_root(folder: Path) -> Path | None:
    try:
        r = _git(folder, "rev-parse", "--show-toplevel")
    except Exception:
        return None
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def can_parallel(project: str) -> bool:
    """A second worker needs its own copy of the code: only projects whose main folder is a git repo."""
    return git_root(project_dirs(project)[0]) is not None


def add_worktree(repo: Path, ref: str) -> Path:
    """A worktree of `repo` for one ticket, on branch ticket-<ref> made from dev (ticket e60b042f)."""
    path = repo.parent / f"{repo.name}-wt-{ref}"
    if (path / ".git").exists():
        return path
    branch = f"ticket-{ref}"
    has = lambda b: _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}").returncode == 0  # noqa: E731
    args = (["worktree", "add", str(path), branch] if has(branch)
            else ["worktree", "add", "-b", branch, str(path), "dev" if has("dev") else "HEAD"])
    r = _git(repo, *args)
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add failed in {repo}: {r.stderr.strip()[:300]}")
    return path


def remove_worktree(repo: Path, path: Path, ref: str) -> str:
    """After the worker: its worktree goes unless it holds uncommitted work, its branch only if merged.
    Returns why something was kept ('' = all gone)."""
    if not path.exists():
        return ""
    r = _git(repo, "worktree", "remove", str(path))
    if r.returncode != 0:
        return f"its worktree {path} was kept ({r.stderr.strip()[:160]})"
    branch = f"ticket-{ref}"
    # "merged" means merged into dev (plain -d asks about whatever the main checkout has checked out)
    base = "dev" if _git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/dev").returncode == 0 else "HEAD"
    merged = _git(repo, "branch", "--merged", base, "--format=%(refname:short)").stdout.split()
    if branch in merged:
        _git(repo, "branch", "-D", branch)
    return ""
NEEDS_ME = "NEEDS ME:"
MAX_CONTINUES = 3            # resume a worker this many times if its background work was cut off
BG_AFTER_RESULT = 10 * 60    # the claude CLI keeps a worker alive after its final answer while background work
                             # runs (and wakes it when that ends); past this, the ticket is over: stop it (ac668ebe)
SWEEP_EVERY = 30 * 60        # how often to look for tickets stuck In Progress
STUCK_AFTER = 2 * 3600       # In Progress this long with no live worker = stuck
START_GAP = 30                # seconds between starting workers: let the last one grow before measuring
RUNNER_STATUS = STATE / "runner_status.json"
# Workers push to GitHub over HTTPS with gh's login: after a restart the launchd-started Jarvis has no
# ssh key in its agent, so `git push` to git@github.com failed (2026-11-04). Only workers get this, via
# the environment; Sanjar's own git config is untouched.
GIT_ENV = {"GIT_CONFIG_COUNT": "3",
           "GIT_CONFIG_KEY_0": "url.https://github.com/.insteadOf", "GIT_CONFIG_VALUE_0": "git@github.com:",
           "GIT_CONFIG_KEY_1": "credential.https://github.com.helper", "GIT_CONFIG_VALUE_1": "",
           "GIT_CONFIG_KEY_2": "credential.https://github.com.helper",
           "GIT_CONFIG_VALUE_2": "!/opt/homebrew/bin/gh auth git-credential"}  # why tickets wait (for Jarvis to show)
PENDING = STATE / "pending_results.json"      # board writes that failed (e.g. GitHub rate limit); retried
REQUEUED = STATE / "requeued_after_restart.json"   # ref -> times put back in the queue after a restart
WRITE_TRIES = (0, 10, 30)                     # seconds to wait before each try of a board write
MAX_REQUEUES = 2
FROM_SANJAR = STATE / "from_sanjar.json"      # refs of tickets he asked for himself (Telegram / call)
HEARTBEAT = STATE / "runner_heartbeat"        # touched every runner loop; scripts/watchdog.sh watches it
RESTART_PENDING = VAR / "restart_pending"   # restart_when_idle.sh is waiting: start no new workers
REQUEUE_AFTER = STATE / "requeue_after.json"   # {"ref": [refs]}: queue those again once "ref" has started
AUTO_ANSWERS = STATE / "auto_answers.json"   # ticket ref -> how many questions Jarvis answered itself
STUCK_TOLD = STATE / "stuck_told.json"   # ticket ref -> when Sanjar was last told it's stuck
# Claude usage limit (ticket d23f4461): a worker stopped by it isn't Failed; it goes back to Queued and
# continues once the limit resets. {"until": epoch, "refs": [paused refs], "told": epoch}
USAGE_LIMIT = STATE / "usage_limit.json"
WORKERS = STATE / "workers.json"   # ref -> {pid, project, log, attempt}: live workers, adopted after a restart
ONLINE_HOST = ("api.anthropic.com", 443)
# workers can't open pages in Sanjar's real Chrome with `open` (30 tabs were left open, ticket 3a61de45)
WORKER_SETTINGS = hookset.SETTINGS
REAL_CHROME = HERE / "scripts" / "real_chrome.py"


async def close_chrome_of(owner: str, older_than: float = -1) -> None:
    """Close the real-Chrome windows opened through scripts/real_chrome.py by this owner (a ticket ref, "mac"),
    all of them or those older than `older_than` seconds ("" = every owner)."""
    with contextlib.suppress(Exception):
        args = ["sweep", "--owner", owner, "--older-than", str(older_than)] if older_than >= 0 else ["close", "--owner", owner]
        proc = await asyncio.create_subprocess_exec(str(HERE / ".venv/bin/python"), str(REAL_CHROME), *args,
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), 60)   # workers need it; no new worker starts while it's unreachable
LIMIT_MARGIN = 120           # start again this long after the announced reset
PROBE_EVERY = 10 * 60        # while held for the limit, read the live usage this often (an early reset ends the hold)
LIMIT_FALLBACK = 30 * 60     # no reset time known: try again after this long
_LIMIT_TEXT = re.compile(
    r"hit your (\S+ )?limit|usage limit reached|out of (extra )?usage|"
    r"individual quota reached|MODEL_CAPACITY_EXHAUSTED|Allocation Quota Reached|"
    r"RESOURCE_EXHAUSTED|quota (exceeded|reached|exhausted)",
    re.I,
)
# a "done" result whose ending says it is waiting on Sanjar (a merge, approval, decision...)
_BLOCKED = re.compile(
    r"(once|after|when|until)\b[^.\n]{0,80}\b(merged?|approved?|approve|push(ed)? to main|you (decide|confirm|choose|answer|reply))"
    r"|waiting (on|for) (you|your|sanjar|a merge|the merge|approval|a decision)"
    r"|blocked (on|by|until)|needs? your (approval|decision|ok|go-ahead|answer|input)"
    r"|(can'?t|cannot) (continue|proceed|go on) (until|without)", re.I)


def blocked_question(result: str) -> str:
    """If a finished worker's last lines say it's waiting on Sanjar but it didn't use NEEDS ME,
    return a question for him built from those lines; empty string if it isn't blocked."""
    # quoted words ("... when you're ready") and sentences about this check itself ("the runner read
    # that as waiting on you") are reports, not a wait: they bounced ticket 8bea031c back twice
    tail = re.sub(r'"[^"\n]{0,200}"|“[^”\n]{0,200}”', '""', result.strip()[-700:])
    if not _BLOCKED.search(tail):
        return ""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", tail) if s.strip()]
    # merging into dev is the worker's own job, never something to wait on Sanjar for
    hits = [s for s in sentences if _BLOCKED.search(s) and not re.search(r"\bdev\b|\brunner\b|by mistake", s, re.I)]
    if not hits:
        return ""
    return ("Blocked, waiting on you: " + " ".join(hits[-2:]))[:600]

# notify(ticket, kind, text, photos) — kind is "done", "failed" or "needs_me"
Notify = Callable[[Ticket, str, str, list[Path]], Awaitable[None]]


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def from_sanjar(ref: str) -> bool:
    """Did Sanjar ask for this ticket himself? Then its failures are never kept from him."""
    return ref in _load_json(FROM_SANJAR)


def _mark_from_sanjar(card: Ticket) -> None:
    note = (card.note or "").lower()
    if any(s in note for s in ("from telegram", "from call", "from a call", "from the app")):
        marks = _load_json(FROM_SANJAR)
        if card.ref not in marks:
            marks[card.ref] = time.time()
            FROM_SANJAR.write_text(json.dumps(marks))


def online(timeout: float = 3) -> bool:
    """Can workers reach their model right now? (ticket 2475b845: a network drop at 03:53 made every new worker
    fail at once)."""
    try:
        socket.create_connection(ONLINE_HOST, timeout=timeout).close()
        return True
    except OSError:
        return False


def _pid_alive(pid: int, check_cmd: bool = True) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if not check_cmd:
        return True
    try:              # a reused pid isn't our worker: it must still be a claude or agy process
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return True
    return "claude" in cmd or "agy" in cmd


class WorkerProc:
    """A worker running detached from Jarvis (its own session, output straight into its log file), so a Jarvis
    restart no longer kills it (ticket 2475b845). Either our child (proc) or one adopted after a restart (pid)."""

    def __init__(self, pid: int, proc: asyncio.subprocess.Process | None = None):
        self.pid, self.proc = pid, proc
        self.end_by = 0.0          # it gave its final answer with background work still running: stop it at this time
        self.ended_late = False    # ...and that happened

    @property
    def returncode(self) -> int | None:
        if self.proc is not None:
            return self.proc.returncode
        return None if _pid_alive(self.pid, check_cmd=False) else 0

    def terminate(self) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pid, signal.SIGTERM)     # the worker leads its own group: its tools stop too


def _workers() -> dict:
    return _load_json(WORKERS)


def _set_worker(ref: str, entry: dict | None) -> None:
    """Remember (or forget) a live worker for adoption after a restart. Only bookkeeping: a failed write (disk
    full) is logged, never raised: it ended the following of a healthy worker on 18 Nov (ticket 9304deae)."""
    data = _workers()
    if entry is None:
        data.pop(ref, None)
    else:
        data[ref] = entry
    try:
        WORKERS.write_text(json.dumps(data))
    except OSError as e:
        log.warning("couldn't save workers.json (%s): %s", ref, e)


def _has_result(path: Path) -> bool:
    try:
        return '"type":"result"' in path.read_text(errors="replace").replace('"type": "result"', '"type":"result"')
    except OSError:
        return False


async def _follow(path: Path, offset: int, handle: WorkerProc, replay_until: int = -1):
    """Lines the worker writes to its log file from `offset` on, until it exits. Yields (line, replayed): replayed
    lines were already handled by the Jarvis before a restart (their steps are on the board already)."""
    buf, pos = b"", offset
    with path.open("rb") as f:
        f.seek(offset)
        while True:
            chunk = f.read(1 << 16)
            if not chunk:
                if handle.returncode is None:
                    if handle.end_by and time.time() > handle.end_by and not handle.ended_late:
                        handle.ended_late = True    # its answer is in; only leftover background work keeps it alive
                        handle.terminate()
                    await asyncio.sleep(0.5)
                    continue
                chunk = f.read()            # it exited: whatever it wrote last
                if not chunk:
                    if buf.strip():
                        yield buf, False
                    return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                pos += len(line) + 1
                yield line, pos <= replay_until


def _load_sessions() -> dict[str, str]:
    try:
        return json.loads(SESSIONS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_session(project: str, session_id: str) -> None:
    """Called for every event a worker prints: writes only when the session changes, and a failed write (disk
    full) is logged, never raised, so it can't end the following of a running worker (ticket 9304deae)."""
    sessions = _load_sessions()
    if sessions.get(project) == session_id:
        return
    sessions[project] = session_id
    try:
        SESSIONS.write_text(json.dumps(sessions, indent=2))
    except OSError as e:
        log.warning("couldn't save the session of %s: %s", project, e)


def _ticket_intro(t: Ticket) -> str:
    if t.page_id.startswith("gh:"):          # a GitHub Issues project (core/ghboard.py)
        repo, num = t.page_id.removeprefix("gh:").rsplit("#", 1)
        return f"""Ticket {t.ref} for project {t.project}: GitHub issue #{num} in {repo}: {t.title}
Issue: {t.url}
{f"Latest note on it: {t.note}" if t.note else ""}

First read the whole issue with its comments: `gh issue view {num} -R {repo} --comments`
(scope, acceptance criteria, and any earlier result or answer from Sanjar in the comments). Don't
change its Status field on the GitHub Project, its jarvis:* labels or its open/closed state;
Jarvis does that. Tick acceptance-criteria checkboxes in the issue body only if you verified them.
If this project has its own skill or notes for its board (CLAUDE.md, a <project>-board skill),
follow them, except for the Status/labels part above.
"""
    if localboard.is_local_page(t.page_id):
        board_cli = f"{HERE / '.venv/bin/python'} {HERE / 'scripts/board.py'}"
        return f"""Ticket {t.ref} for project {t.project}, from Sanjar's Jarvis board: {t.title}
{f"Latest note on it: {t.note}" if t.note else ""}

First read the whole ticket: `{board_cli} show {t.ref}` (description, acceptance criteria, sub-tasks,
and any earlier log or answer from Sanjar at the bottom). This board lives on the Mac, not in Notion.
Don't change the ticket's status or Jarvis state; Jarvis does that. To add other tickets or edit this
one's text, see `{board_cli} --help`. If this project has its own skill or notes for working on board
tickets (CLAUDE.md, a <project>-board skill), follow them, but this board replaces any Notion steps in them.
"""
    return f"""Ticket {t.ref} for project {t.project}, from Sanjar's Notion board: {t.title}
Ticket page: {t.url}
{f"Latest note on it: {t.note}" if t.note else ""}

First read the whole ticket page with your Notion tools (description, acceptance criteria,
sub-tasks, and any earlier log or answer from Sanjar at the bottom). Don't change the ticket's
Status or Jarvis columns in Notion; Jarvis does that. If this project has its own skill or
notes for working on board tickets (CLAUDE.md, a <project>-board skill), follow them.
"""


def limit_state() -> dict:
    return _load_json(USAGE_LIMIT) if USAGE_LIMIT.exists() else {}


def limited_until() -> float:
    """Until when new workers wait for the Claude usage limit to reset (0 = not limited)."""
    until = float(limit_state().get("until") or 0)
    return until + LIMIT_MARGIN if until else 0.0


def hit_limit(result: str, is_error: bool, rejected_until: float) -> float:
    """Did this worker stop because of the usage limit? Returns when it resets (0 = no)."""
    tail = (result or "")[-400:]
    if _LIMIT_TEXT.search(tail) or (rejected_until and (is_error or not tail.strip())):
        if rejected_until:
            return rejected_until
        gem_reset = usagewatch.current_status(force=True).get("gemini", {}).get("resets_at")
        if JARVIS_ENGINE == "antigravity" and gem_reset and float(gem_reset) > time.time():
            return float(gem_reset)
        return time.time() + LIMIT_FALLBACK
    return 0.0


def parallel_note(ref: str, worktrees: list[tuple[Path, Path]]) -> str:
    """What a worker running beside another one on the same project must know (ticket e60b042f)."""
    lines = "\n".join(f"  - {wt} (your copy of {repo})" for repo, wt in worktrees)
    return f"""

Another worker is busy on this project right now, so you work in your own git worktree(s), on branch
`ticket-{ref}` made from dev:
{lines}
Make every change there, never in the main checkouts (the other worker is using them). When you're done:
commit on `ticket-{ref}` and merge it into dev. If dev isn't checked out anywhere (`git worktree list`), run
`git checkout dev` in your worktree, merge, push dev. If dev is checked out in the main checkout and that
checkout is clean (`git -C <main checkout> status --porcelain` prints nothing), merge there instead
(`git -C <main checkout> merge --no-ff ticket-{ref}`) and push. Otherwise another worker is busy with dev:
wait a minute and try again. Jarvis removes your worktree after you finish (it keeps one with uncommitted
work), so don't remove it yourself."""


def _worker_prompt(t: Ticket, shots: Path) -> str:
    return f"""{_ticket_intro(t)}
Sanjar is away from the computer and won't approve anything, so work on your own until it's done.
{NEVER_DO_MAIN_OK if grants.allowed(t.ref, grants.PUSH_MAIN) else NEVER_DO}
{GIT_FLOW}
- The Mac has only 8 GB of RAM and other workers may be running. Wrap heavy commands (Gradle /
  Flutter / Xcode builds, emulators, Docker builds, video renders, big local models) in
  {HERE / "scripts/heavy.py"} <command> so only one runs at a time, and stop what you started
  afterwards (`./gradlew --stop`, close emulators). Don't start sub-agents that each build.
- If you change something visible in a web app, start it (or use the running one) and save a
  screenshot of the result as a PNG in {shots}/ (e.g. with headless Chrome or Playwright).
- Never open pages in Sanjar's own Chrome with `open` (it's blocked: they were never closed). Use a headless
  browser; only when a page needs his logins: {HERE / ".venv/bin/python"} {HERE / "scripts/real_chrome.py"} open <url>
  [--profile "Profile 4"], then `real_chrome.py close --window <id>` when done (closed anyway when this ticket ends).
- Never copy or reuse his real Chrome profile's cookies / Login Data / Local State in another browser (blocked too):
  Google signs him out of every account when his session appears in a second browser.
- Never touch ~/Documents, ~/Desktop or ~/Downloads (not even `ls`): macOS asks for permission
  there and the command hangs forever, since nobody is at the Mac to click Allow. If a note or
  memory says to put files there, skip that step and mention it in your summary.
- To send Sanjar a file (an APK, a PDF...), run:
  {HERE / ".venv/bin/python"} {HERE / "scripts/send_file.py"} <file> "<caption>"
- If you truly can't continue without a decision from Sanjar, stop and make the LAST line of
  your final message: {NEEDS_ME} <one short question>
- NEEDS ME is only for a decision or something only Sanjar can give (a choice, a password, a code). Never
  for a state a program is waiting on (the screen locked, a page still loading, a post not out yet): that
  program asks him itself and takes it back when it's done (core/asks.py; the reel poster does this for
  the screen). Copied into a ticket it goes stale. Say in your summary what is waiting on what.
- Never end your turn while background agents, workflows or background commands you started are
  still running: wait for each to finish (or stop it on purpose) before your final summary. Your
  ticket is over once you give it: anything still running 10 minutes later is stopped.
- Never keep a watcher or wait loop running for something that happens later (an evening post, a
  reply expected tomorrow): it holds a worker slot and blocks Jarvis's restarts. Finish now, and for
  a later check on a board on the Mac add a queued follow-up that waits until then:
  `{HERE / '.venv/bin/python'} {HERE / 'scripts/board.py'} add "<project>" "Check <what>" --text "<what to check and why>" --queue --start-after HH:MM`
  (or "YYYY-MM-DD HH:MM"). Otherwise say in your summary what's still to check and when.
  If the follow-up must wait for another ticket (one still queued or running), use `--after <its ref>`
  instead of a guessed time: it starts as soon as that ticket is Done.
- The same if your work is done for now but the next step waits on him: a push to main, an
  approval, a choice, a key or account. Never finish quietly and wait — end with {NEEDS_ME} saying
  exactly what he needs to do. (Merging into `dev` is never a reason to wait: do it yourself.)

{rules_text()}
- Finish with a short plain summary for Sanjar's Telegram (it shows no colours or code blocks well):
  first ONE short sentence with the result (shown as the summary), then a blank line, then the
  details as short "- " lines: "✅ ..." for done/added, "⚠️ ..." for not done, pending or a problem
  (they are folded under "Details"). Which files, whether tests pass, anything he should know.
  No ```diff or other code blocks, even if an output style asks for them; `backticks` only for
  short refs and file names. A NEEDS ME line still goes last."""


def _step_text(block: dict) -> str:
    """A short human line for one worker tool call."""
    name = block.get("name", "") or block.get("tool_name", "")
    args = block.get("input", {}) or block.get("tool_args", {})
    if name in ("Edit", "Write", "MultiEdit", "replace_file_content", "write_to_file", "edit_file", "write_file"):
        path = args.get("file_path") or args.get("Path") or args.get("path") or ""
        return f"editing {Path(path).name}" if path else "editing files"
    if name in ("Read", "view_file", "read_file"):
        path = args.get("file_path") or args.get("Path") or args.get("path") or ""
        return f"reading {Path(path).name}" if path else "reading files"
    if name in ("Bash", "run_command"):
        cmd = args.get("description") or args.get("toolSummary") or args.get("CommandLine") or args.get("command") or ""
        return f"running: {cmd[:80]}"
    if name in ("Grep", "Glob", "grep_search", "find_by_name"):
        return "searching the code"
    if name in ("TodoWrite", "schedule"):
        return "planning the steps"
    if name == "call_mcp_tool":
        return f"calling {args.get('ToolName', 'mcp tool')}"
    return name.removeprefix("mcp__").replace("__", " ")


def _when(ts: float) -> str:
    mins = int((time.time() - ts) // 60)
    return "just now" if mins < 1 else f"{mins} min ago" if mins < 120 else f"{mins // 60} h ago"


def _tool_output(content) -> str:
    if isinstance(content, list):
        content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content or "")


def worker_log(project: str, ref: str = "") -> Path | None:
    """The worker log for a ticket of this project, or its newest one."""
    if ref:
        path = LOGS / f"{project}-{ref.strip().lower()}.jsonl"
        return path if path.exists() else None
    logs = sorted(LOGS.glob(f"{project}-*.jsonl"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def last_step(path: Path, tail: int = 200_000) -> tuple[str, float]:
    """The worker's latest step as a short line ("editing Api.kt"), and when its log last changed.
    Reads only the end of the log: they grow to megabytes."""
    try:
        size, mtime = path.stat().st_size, path.stat().st_mtime
        with path.open("rb") as f:
            f.seek(max(0, size - tail))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return "", 0.0
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("type") == "result":
            return "finishing up", mtime
        if e.get("type") == "assistant":
            for b in reversed(e.get("message", {}).get("content", [])):
                if b.get("type") == "tool_use":
                    return " ".join(_step_text(b).split()), mtime
                if b.get("type") == "text" and b.get("text", "").strip():
                    return "writing: " + " ".join(b["text"].split())[:80], mtime
    return "starting", mtime


def progress_text(path: Path, running: bool, steps: int = 12) -> str:
    """What a worker did and is doing, from its stream-json log: start, last steps, the command
    still running (and its latest output), and the result if it's finished."""
    done_steps, open_calls, last_out, said, result, started = [], {}, "", "", None, None
    for line in path.read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not started and e.get("timestamp"):
            started = e["timestamp"]
        kind = e.get("type")
        if kind == "assistant":
            for b in e["message"].get("content", []):
                if b.get("type") == "tool_use":
                    open_calls[b.get("id")] = _step_text(b)
                    done_steps.append(_step_text(b))
                elif b.get("type") == "text" and b.get("text", "").strip():
                    said = b["text"].strip()
        elif kind == "user":
            for b in e["message"].get("content", []) if isinstance(e["message"].get("content"), list) else []:
                if b.get("type") == "tool_result":
                    open_calls.pop(b.get("tool_use_id"), None)
                    last_out = _tool_output(b.get("content"))
        elif kind == "result":
            result = e
    ticket = path.stem.rsplit("-", 1)[1]
    lines = [f"Worker log for {ticket} ({path.stem.rsplit('-', 1)[0]}): "
             f"{'still running' if running and not result else 'finished' if result else 'stopped (no result)'}, "
             f"last activity {_when(path.stat().st_mtime)}" + (f", started {started[:16].replace('T', ' ')} UTC" if started else "")]
    if result:
        mins = (result.get("duration_ms") or 0) // 60000
        lines.append(f"Result after {mins} min ({'error' if result.get('is_error') else 'ok'}): "
                     f"{(result.get('result') or '')[-1200:]}")
    lines.append(f"Last steps (of {len(done_steps)}):" + "".join(f"\n- {' '.join(s.split())}" for s in done_steps[-steps:]))
    if open_calls and not result:
        lines.append("Still waiting on: " + "; ".join(" ".join(s.split()) for s in open_calls.values()))
    if said and not result:
        lines.append(f"Worker's last words: {said[-500:]}")
    if last_out.strip() and not result:
        lines.append(f"Latest tool output (end):\n{last_out.strip()[-1200:]}")
    return "\n".join(lines)


class Runner:
    from_sanjar = staticmethod(from_sanjar)    # jarvis.notify calls runner.from_sanjar (crashed at 03:59, 2475b845)

    def __init__(self, notify: Notify):
        self.notify = notify
        # all keyed by ticket ref: several workers may run on one project at once (ticket e60b042f)
        self.running: dict[str, asyncio.Task] = {}      # ref -> worker task
        self.project_of: dict[str, str] = {}            # ref -> its project (set when the task starts)
        self.procs: dict[str, WorkerProc] = {}
        self.current: dict[str, Ticket] = {}            # ref -> ticket being worked on
        self.stop_reasons: dict[str, str] = {}          # ref -> why its worker was stopped on purpose
        self.in_main: dict[str, str] = {}               # project -> ref of the worker in its main checkout
        self.finished: dict[str, float] = {}            # ticket ref -> when it finished (monotonic)
        self.message: Callable[[str], Awaitable] | None = None   # plain Telegram text to Sanjar
        self.last_sweep = 0.0
        self.held_logged = 0.0                          # last "held for RAM" log line (monotonic)
        self.last_start: dict[str, float] = {}          # project -> when its last worker started (fair turns)
        self.wait_reason = ""                           # why the next queued ticket isn't starting
        self.started: dict[str, float] = {}             # ref -> when its ticket started (epoch)
        self._adopted: set[str] = set()                 # refs whose live worker was adopted after a restart
        self._probed = 0.0                              # last live usage reading asked for (monotonic)

    async def run_forever(self, recover: bool = True):
        """recover: at Jarvis's start (workers of the old process are gone). False when the self-check
        restarts a dead runner task inside a running Jarvis: its workers may still be alive."""
        if recover:
            try:
                await self._recover()
            except Exception:                # a failed recovery used to kill the runner for good
                log.exception("recovering interrupted tickets failed")
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("runner tick failed")
            with contextlib.suppress(OSError):
                HEARTBEAT.touch()            # scripts/watchdog.sh restarts Jarvis if this goes stale
            if time.monotonic() - self.last_sweep > SWEEP_EVERY:
                self.last_sweep = time.monotonic()
                try:
                    await self._sweep_stuck()
                    await close_chrome_of("mac", 30 * 60)      # pages the Mac worker opened for a step
                    await close_chrome_of("", 3 * 3600)        # and anything left over (ticket 3a61de45)
                except Exception:
                    log.exception("stuck-ticket sweep failed")
            await asyncio.sleep(POLL_SECONDS)

    async def _sweep_stuck(self):
        """Tickets sitting In Progress for STUCK_AFTER with no live worker (e.g. sub-agents that
        were killed): tell Sanjar once a day per ticket so nothing silently rots."""
        if not self.message:
            return
        told = _load_json(STUCK_TOLD)
        now = time.time()
        live = {t.ref for t in self.current.values()}
        stuck = []
        for project in board.boards():
            for t in await board.in_progress(project):
                if t.ref in live or t.jarvis in ("Queued", "Working"):
                    continue
                try:
                    edited = datetime.fromisoformat(t.edited.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                if now - edited < STUCK_AFTER or now - told.get(t.ref, 0) < 86400:
                    continue
                stuck.append((t, (now - edited) / 3600))
                told[t.ref] = now
        if stuck:
            STUCK_TOLD.write_text(json.dumps(told))
            await self.message(tgfmt.stuck([(t.ref, t.project, t.title, h, t.jarvis) for t, h in stuck[:15]]))

    async def _recover(self):
        """Tickets left Working when Jarvis starts: their worker is gone. A result that couldn't be
        written is applied from PENDING; otherwise the ticket goes back in the queue (it starts over),
        up to MAX_REQUEUES times, then Failed."""
        pending, counts, alive = _load_json(PENDING), _load_json(REQUEUED), _workers()
        for t in await board.with_jarvis("Working"):
            if t.ref in pending:
                continue                      # its result is saved; _flush_pending writes it
            w = alive.get(t.ref)
            if w and (_pid_alive(int(w.get("pid", 0))) or _has_result(Path(w.get("log", "")))):
                self._adopt(t, w)             # still running (or finished meanwhile): carry on with it
                continue
            n = counts.get(t.ref, 0) + 1
            if n <= MAX_REQUEUES:
                counts[t.ref] = n
                await self._write(t, jarvis="Queued", note="queued again: its worker stopped when Jarvis restarted",
                                  log_lines=[f"Queued again: the worker stopped when Jarvis restarted ({n}/{MAX_REQUEUES})"])
                log.info("%s: re-queued after a restart (%d/%d)", t.ref, n, MAX_REQUEUES)
            else:
                await self._write(t, jarvis="Failed", note="stopped by Jarvis restarts too often; queue it again to retry")
                await self.notify(t, "failed", "Its worker was cut off by Jarvis restarts several times.", [])
        REQUEUED.write_text(json.dumps(counts))
        for ref in set(alive) - {p_ref for p_ref in self._adopted}:
            _set_worker(ref, None)           # its ticket isn't Working any more

    def _adopt(self, t: Ticket, w: dict) -> None:
        """A worker that outlived the old Jarvis: follow its log to the end like any other (ticket 2475b845)."""
        log.info("%s: adopted its running worker (pid %s) after the restart", t.ref, w.get("pid"))
        self._adopted.add(t.ref)
        self._launch(t, adopt=w)

    def _launch(self, t: Ticket, adopt: dict | None = None) -> None:
        task = asyncio.create_task(self._work(t, adopt=adopt), name=f"worker:{t.project}:{t.ref}")
        self.last_start[t.project] = time.monotonic()
        self.running[t.ref] = task
        self.project_of[t.ref] = t.project

        def done(_t, ref=t.ref):
            self.running.pop(ref, None)
            self.project_of.pop(ref, None)
        task.add_done_callback(done)

    def count(self, project: str) -> int:
        """Workers running (or starting) on this project."""
        return sum(1 for p in self.project_of.values() if p == project)

    async def _write(self, card: Ticket, *, jarvis: str | None = None, note: str | None = None,
                     stage: str | None = None, log_lines: list[str] | None = None, keep: bool = True) -> bool:
        """Write a ticket's state and log to its board, retrying; if the board stays unreachable (GitHub
        rate limit...), keep it in PENDING to write later, also across restarts, so a finished
        worker's result is never lost (aet-11, 2026-11-04). False = not written yet."""
        for wait in WRITE_TRIES:
            await asyncio.sleep(wait)
            try:
                await board.update(card, jarvis=jarvis, note=note, stage=stage)
                if log_lines:
                    await board.append_log(card.page_id, log_lines)
                return True
            except Exception as e:
                err = e
        log.warning("%s: board write failed (%s), %s", card.ref, str(err)[:150], "kept for later" if keep else "dropped")
        if keep:
            pending = _load_json(PENDING)
            old = pending.get(card.ref, {})
            pending[card.ref] = {"project": card.project, "page_id": card.page_id, "ref": card.ref,
                                 "title": card.title, "url": card.url,
                                 "jarvis": jarvis or old.get("jarvis"), "note": note if note is not None else old.get("note"),
                                 "stage": stage or old.get("stage"), "log": old.get("log", []) + (log_lines or [])}
            PENDING.write_text(json.dumps(pending, ensure_ascii=False))
        return False

    async def _flush_pending(self):
        """Write saved results whose board write failed before."""
        if not PENDING.exists() or time.monotonic() - getattr(self, "_flushed", 0) < 60:
            return
        self._flushed = time.monotonic()
        pending = _load_json(PENDING)
        for ref, p in list(pending.items()):
            card = Ticket(page_id=p["page_id"], ref=ref, project=p["project"], title=p.get("title", ""),
                          status="", jarvis="", note="", sprint="", url=p.get("url", ""))
            try:
                await board.update(card, jarvis=p.get("jarvis"), note=p.get("note"), stage=p.get("stage"))
                if p.get("log"):
                    await board.append_log(card.page_id, p["log"])
            except Exception:
                continue
            pending.pop(ref)
            log.info("%s: saved result written to the board", ref)
        PENDING.write_text(json.dumps(pending, ensure_ascii=False))

    async def _tick(self):
        cutoff = time.monotonic() - 120
        self.finished = {ref: ts for ref, ts in self.finished.items() if ts > cutoff}
        await self._requeue_after()
        await self._flush_pending()
        if RESTART_PENDING.exists():         # let the waiting restart happen; the queue resumes right after
            if time.monotonic() - self.held_logged > 300:
                log.info("restart pending: not starting new workers")
                self.held_logged = time.monotonic()
            return
        until = limited_until()
        state = limit_state()
        engine_name = state.get("engine") or ("Gemini" if JARVIS_ENGINE == "antigravity" else "Claude")
        if until > time.time() and usagewatch.allowed_since(self._hold_since()):
            log.info("%s usage was reset early: ending the hold now", engine_name)
            until = 1.0                      # the "it has reset" branch below clears it
        if until > time.time():              # the usage limit: every worker would stop at once
            if time.monotonic() - self._probed > PROBE_EVERY:
                self._probed = time.monotonic()
                asyncio.create_task(usagewatch.probe())     # its reading may end the hold on a later tick
            nxt = self.pick(await board.queued())
            if nxt:
                self._waiting(nxt[0], f"the {engine_name} usage limit; it resets at "
                              + time.strftime("%H:%M", time.localtime(until - LIMIT_MARGIN)))
            return
        await usagewatch.report_reset()      # "the 5-hour limit has reset", once after a hit
        if until:                            # it has reset: paused tickets are Queued and start now
            state = limit_state()
            state.pop("until", None)
            USAGE_LIMIT.write_text(json.dumps(state))
            log.info("usage limit reset: resuming %s", ", ".join(state.get("refs", [])) or "the queue")
        for t in self.pick(await board.queued()):
            if self.running and time.monotonic() - max(self.last_start.values(), default=0) < START_GAP:
                return                       # one start at a time; the next check measures it first
            if not await asyncio.to_thread(online):
                self._waiting(t, "no internet (workers would fail at once); it starts when it's back")
                return
            pids = [p.pid for p in self.procs.values() if p.returncode is None]
            ok, why = ramguard.can_start(len(self.running), worker_pids=pids)
            if not ok:                       # 8 GB Mac: the ticket waits in the queue until RAM frees up
                self._waiting(t, why)
                return
            self._waiting(None, "")
            self._launch(t)

    def _waiting(self, t: Ticket | None, why: str):
        """Remember (and log, at most every 5 min) why the next ticket waits; Jarvis shows it in get_status."""
        reason = f"{t.ref} [{t.project}] waits: {why} ({len(self.running)} running)" if t else ""
        if reason and (reason != self.wait_reason or time.monotonic() - self.held_logged > 300):
            log.info("holding %s", reason)
            self.held_logged = time.monotonic()
        if reason != self.wait_reason:
            with contextlib.suppress(OSError):
                RUNNER_STATUS.write_text(json.dumps({"at": time.time(), "running": sorted(self.running),
                                                     "waiting": reason}))
        self.wait_reason = reason

    async def _requeue_after(self):
        """Tickets set aside so an urgent one could go first go back in the queue once it has started."""
        if not REQUEUE_AFTER.exists() or time.monotonic() - getattr(self, "_requeue_checked", 0) < 60:
            return
        self._requeue_checked = time.monotonic()
        wait = _load_json(REQUEUE_AFTER)
        for first, later in list(wait.items()):
            t = await board.find_any(first)
            if t and t.jarvis == "Queued":
                continue                      # not started yet
            for ref in later:
                lt = await board.find_any(ref)
                if lt and not lt.jarvis:
                    await board.update(lt, jarvis="Queued", note=f"queued again: {first} has started")
                    log.info("re-queued %s now that %s started", ref, first)
            wait.pop(first)
        REQUEUE_AFTER.write_text(json.dumps(wait))

    def pick(self, queued: list[Ticket]) -> list[Ticket]:
        """Which queued tickets to start, best first (Sanjar, 2026-11-04: no project starves the rest):
        one per project per check (its own order, or its earliest deadline; a project may run up to
        project_cap() workers at once, the extra ones in git worktrees), tickets with a "Due" date first
        (soonest first), then the project whose last worker started longest ago, so a project with
        a long queue can't keep the slots from the others."""
        heads: dict[str, tuple[int, Ticket]] = {}
        for i, t in enumerate(queued):
            n = self.count(t.project)
            if n and (n >= project_cap(t.project) or not can_parallel(t.project)):
                continue
            if not board.startable(t):               # a follow-up waiting for its time or for another ticket
                continue
            if t.ref in self.finished:
                # just finished (the board may still read as Queued for a moment); don't re-run it
                log.info("skipping %s: finished %.0fs ago", t.ref, time.monotonic() - self.finished[t.ref])
                continue
            have = heads.get(t.project)
            if have is None or (t.due and (not have[1].due or t.due < have[1].due)):
                heads[t.project] = (i, t)
        order = sorted(heads.values(), key=lambda it: (not it[1].due, it[1].due or "",
                                                       self.last_start.get(it[1].project, 0.0), it[0]))
        return [t for _, t in order]

    def snapshot(self) -> dict:
        """For the phone app (ticket 0815c76f): how many workers run and what each is doing right now."""
        workers = []
        for ref, card in sorted(self.current.items(), key=lambda it: self.started.get(it[0], 0)):
            path = worker_log(card.project, ref)
            step, active = last_step(path) if path else ("starting", 0.0)
            workers.append({"project": card.project, "ref": ref, "title": card.title, "url": card.url,
                            "started": self.started.get(ref, 0.0), "step": step, "active": active})
        return {"count": len(workers), "workers": workers, "waiting": self.wait_reason, "at": time.time(),
                "limits": usagewatch.current_status(), "limit_status": usagewatch.limit_summary()}

    def progress(self, project: str, ticket: str = "") -> str:
        """For the worker_progress tool: the live log of a project's worker (or a past ticket's)."""
        live = {t.project for t in self.current.values()}
        name = next((p for p in {*live, *board.boards(), *(l.stem.rsplit("-", 1)[0] for l in LOGS.glob("*-*.jsonl"))}
                     if p.lower() == project.strip().lower()), project)
        current = next((t for t in self.current.values() if t.project == name and (not ticket or t.ref == ticket)), None)
        path = worker_log(name, ticket or (current.ref if current else ""))
        if not path:
            return f"No worker log for {project!r}" + (f" ticket {ticket}" if ticket else "") + "."
        return progress_text(path, running=current is not None and path.stem.endswith(current.ref))

    def targets(self, target: str) -> list[str]:
        """Refs of the running workers a name means: a ticket ref, or a project (all its workers)."""
        t = target.strip().lower()
        return [ref for ref, c in self.current.items() if ref.lower() == t or c.project.lower() == t]

    def stop_project(self, target: str, reason: str = "Sanjar used /stop") -> bool:
        """Stop workers on purpose (a project: all of its workers; a ticket ref: that one). Their tickets go off
        the queue (not "Failed")."""
        hit = False
        for ref in self.targets(target):
            proc = self.procs.get(ref)
            if proc and proc.returncode is None:
                self.stop_reasons[ref] = reason
                proc.terminate()
                hit = True
        return hit

    async def stop_and_wait(self, target: str, reason: str, timeout: float = 30) -> list[Ticket]:
        """stop_project, then wait until those workers have wrapped up; returns the tickets they were on."""
        refs = self.targets(target)
        cards = [self.current[r] for r in refs if r in self.current]
        if not self.stop_project(target, reason):
            return []
        end = time.monotonic() + timeout
        while any(r in self.current for r in refs) and time.monotonic() < end:
            await asyncio.sleep(0.5)
        return cards

    async def _work(self, card: Ticket, adopt: dict | None = None):
        """adopt: a worker still running from before a Jarvis restart (WORKERS entry): follow it, don't start one."""
        _mark_from_sanjar(card)
        dirs = project_dirs(card.project)
        shots = SHOTS / card.ref
        shots.mkdir(exist_ok=True)
        if adopt:
            log.info("following %s's worker again (pid %s)", card.ref, adopt.get("pid"))
            if not adopt.get("worktrees"):
                self.in_main[card.project] = card.ref
        else:
            log.info("start %s in %s", card.line(), dirs[0])
            if not await self._write(card, jarvis="Working", note="starting", stage="doing", keep=False):
                self.finished[card.ref] = time.monotonic()   # board unreachable: leave it Queued, retry later
                return
        # the first worker of a project works in its main checkout; others (ticket e60b042f) each get a git
        # worktree of every repo the project has, on their own branch, so nobody edits another one's files
        worktrees: list[tuple[Path, Path]] = [(Path(a), Path(b)) for a, b in (adopt or {}).get("worktrees", [])]
        busy_main = self.in_main.get(card.project)
        if not adopt and busy_main and busy_main != card.ref and busy_main in self.current:
            try:
                for d in dirs:
                    repo = await asyncio.to_thread(git_root, d)
                    if repo and all(r != repo for r, _ in worktrees):
                        worktrees.append((repo, await asyncio.to_thread(add_worktree, repo, card.ref)))
            except Exception as e:
                log.warning("%s: no worktree, back to the queue: %s", card.ref, e)
                for repo, wt in worktrees:
                    await asyncio.to_thread(remove_worktree, repo, wt, card.ref)
                await self._write(card, jarvis="Queued", note=f"waiting: couldn't make its own worktree ({str(e)[:200]})")
                self.finished[card.ref] = time.monotonic()
                return
        elif not worktrees:
            self.in_main[card.project] = card.ref

        def in_tree(d: Path) -> Path:
            for repo, wt in worktrees:
                if d == repo or repo in d.parents:
                    return wt / d.relative_to(repo)
            return d
        dirs = [in_tree(d) for d in dirs]
        session_key = f"{card.project}#{card.ref}" if worktrees else card.project
        self.current[card.ref] = card
        self.started[card.ref] = time.time()

        def make_cmd(prompt: str) -> list[str]:
            if JARVIS_ENGINE == "antigravity":
                cmd = [AGY_CLI, "-p", prompt, "--model", AGY_WORKER_MODEL,
                       "--output-format", "stream-json",
                       "--dangerously-skip-permissions"]
                for extra in dirs[1:]:
                    cmd += ["--add-dir", str(extra)]
                session = _load_sessions().get(session_key)
                if session:
                    cmd += ["--conversation", session]
                return cmd

            cmd = ["claude", "-p", prompt, "--model", BRAIN_MODEL, "--settings", WORKER_SETTINGS,
                   "--output-format", "stream-json",
                   "--verbose", "--permission-mode", "bypassPermissions",
                   "--disallowedTools", "Bash(sudo:*)", "Bash(git push --force:*)", "Bash(git push -f:*)",
                   "Bash(rm -rf /:*)", "Bash(rm -rf ~:*)"]
            for extra in dirs[1:]:
                cmd += ["--add-dir", str(extra)]
            session = _load_sessions().get(session_key)
            if session:
                cmd += ["--resume", session]
            return cmd

        result, is_error, last_push, pending = "", False, 0.0, []
        prompt, attempt = _worker_prompt(card, shots), int((adopt or {}).get("attempt", 0))
        if worktrees:
            prompt += parallel_note(card.ref, worktrees)
        state = limit_state()
        if card.ref in state.get("refs", []) and not adopt:     # it was cut off by the usage limit: carry on
            eng = state.get("engine", "Claude")
            prompt += (f"\n\nNote: you already started this ticket and were stopped mid-work by the {eng} "
                       "usage limit, which has reset now. Continue where you left off: check git status, your "
                       "branch and what's already done first, and don't redo finished steps.")
            state["refs"] = [r for r in state["refs"] if r != card.ref]
            USAGE_LIMIT.write_text(json.dumps(state))
        rejected_until, paused = 0.0, 0.0
        path = LOGS / f"{card.project}-{card.ref}.jsonl"
        cancelled = False
        try:
            while True:
                bg: dict[str, str] = {}        # background tasks started in this run, still running
                cut: dict[str, str] = {}       # ...and ones killed before finishing
                stopped_on_purpose: set[str] = set()
                if adopt:                      # still running from before the restart: read its log from its start
                    offset, replay_until = int(adopt.get("offset", 0)), path.stat().st_size if path.exists() else 0
                    handle = WorkerProc(int(adopt["pid"]))
                    adopt = None
                else:
                    # detached (own session) and writing straight into its log, so a Jarvis restart doesn't
                    # kill it (ticket 2475b845); worker_progress reads the same file live
                    offset, replay_until = path.stat().st_size if attempt and path.exists() else 0, -1
                    with path.open("ab" if attempt else "wb") as out:
                        proc = await asyncio.create_subprocess_exec(
                            *make_cmd(prompt), cwd=dirs[0], stdin=asyncio.subprocess.DEVNULL,
                            env={**os.environ, **GIT_ENV, "JARVIS_APP_CHAT": origin.get(card.ref).get("chat", ""),
                                 "JARVIS_OWNER": card.ref},
                            stdout=out, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                    handle = WorkerProc(proc.pid, proc)
                    _set_worker(card.ref, {"pid": proc.pid, "project": card.project, "log": str(path),
                                           "offset": offset, "attempt": attempt, "started": time.time(),
                                           "worktrees": [[str(a), str(b)] for a, b in worktrees]})
                self.procs[card.ref] = handle
                async for line, replayed in _follow(path, offset, handle, replay_until):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "event" in event:
                        event_type = event.get("event")
                        if event_type == "init":
                            cid = event.get("conversation_id")
                            if cid:
                                _save_session(session_key, cid)
                        elif event_type == "step_update":
                            step = event.get("step_update", {})
                            cid = step.get("conversation_id")
                            if cid:
                                _save_session(session_key, cid)
                            stype = step.get("step_type")
                            if stype == "tool_call":
                                handle.end_by = 0.0
                                pending.append(_step_text(step))
                        elif event_type == "result":
                            res = event.get("result", {})
                            cid = res.get("conversation_id")
                            if cid:
                                _save_session(session_key, cid)
                            result = res.get("response", "") or res.get("error", "")
                            is_error = res.get("status") != "SUCCESS"
                            handle.end_by = time.time() + BG_AFTER_RESULT if bg else 0.0
                        if replayed:
                            pending = []
                        if pending and time.monotonic() - last_push > 5:
                            await self._push_steps(card, pending)
                            pending, last_push = [], time.monotonic()
                        continue

                    if event.get("session_id"):
                        _save_session(session_key, event["session_id"])
                    kind = event.get("type")
                    if kind == "assistant":
                        handle.end_by = 0.0    # woken again (a background task ended): it's working, no deadline
                        for block in event["message"].get("content", []):
                            if block.get("type") == "tool_use":
                                pending.append(_step_text(block))
                                if block.get("name") == "TaskStop":
                                    args = block.get("input", {})
                                    stopped_on_purpose.add(str(args.get("task_id") or args.get("shell_id") or ""))
                    elif kind == "system" and event.get("subtype") == "task_started":
                        bg[event.get("task_id", "")] = event.get("description", "")
                    elif kind == "system" and event.get("subtype") == "task_notification":
                        tid = event.get("task_id", "")
                        if tid in bg:
                            desc = bg.pop(tid)
                            if event.get("status") == "stopped" and tid not in stopped_on_purpose:
                                cut[tid] = desc
                    elif kind == "rate_limit_event":
                        info = event.get("rate_limit_info") or {}
                        if not replayed:
                            await usagewatch.report(info)      # 80% / used up: one note each (ticket e6e75d74)
                        if info.get("status") == "rejected":
                            rejected_until = float(info.get("resetsAt") or 0) or time.time() + LIMIT_FALLBACK
                    elif kind == "result":
                        result, is_error = event.get("result") or "", bool(event.get("is_error"))
                        # still alive only for its background work: that may finish it off shortly, but never
                        # keeps the slot (or a waiting restart) for hours, like a watcher for an evening post
                        handle.end_by = time.time() + BG_AFTER_RESULT if bg else 0.0
                    if replayed:
                        pending = []           # these steps were pushed before the restart
                    # push steps to Notion at most every 5 s
                    if pending and time.monotonic() - last_push > 5:
                        await self._push_steps(card, pending)
                        pending, last_push = [], time.monotonic()
                if handle.proc is not None:
                    rc = await handle.proc.wait()
                else:                          # adopted: not our child, its exit code is unknown
                    rc = 0 if result else 1
                if pending:
                    await self._push_steps(card, pending)
                    pending = []
                if rc and not result:
                    is_error, result = True, f"Worker exited with code {rc}."
                unfinished = [d for d in [*bg.values(), *cut.values()] if d]
                paused = hit_limit(result, is_error, rejected_until)
                if handle.ended_late and result:   # it had finished: don't resume it for its leftover watcher
                    log.info("%s: finished, but background work was still running %d min later: stopped it (%s)",
                             card.ref, BG_AFTER_RESULT // 60, "; ".join(unfinished[:6]))
                    result += (f"\n\nNote: background work was still running {BG_AFTER_RESULT // 60} min after "
                               "the final answer, so Jarvis stopped it: " + "; ".join(unfinished[:6] or ["?"]))
                    break
                if (is_error or paused or not unfinished or attempt >= MAX_CONTINUES or rc < 0
                        or card.ref in self.stop_reasons):
                    if unfinished and not is_error:
                        result += ("\n\nNote: background work was still unfinished after "
                                   f"{attempt} continuation(s): " + "; ".join(unfinished[:6]))
                    break
                # the turn ended while sub-agents / background commands were running: they were
                # killed, so the ticket isn't done. Resume the same session and finish them.
                attempt += 1
                log.info("%s: %d background task(s) cut off, continuing (%d/%d)",
                         card.ref, len(unfinished), attempt, MAX_CONTINUES)
                await self._push_steps(card, [f"continuing: {len(unfinished)} background task(s) "
                                              "were cut off when the turn ended"])
                prompt = ("Your turn ended while background work you started was still running, so it was "
                          "stopped before finishing: " + "; ".join(f'"{d}"' for d in unfinished[:10]) +
                          ". The ticket is not done yet. Finish that work now yourself: run those agents or "
                          "commands again in the foreground (not in the background), or do the steps "
                          "directly, wait for everything to complete, check the results, merge into dev as "
                          "the rules say, and only then give your final summary.")
                result = ""
        except asyncio.CancelledError:
            cancelled = True                   # Jarvis is stopping: the worker keeps running, the next Jarvis adopts it
            raise
        except Exception as e:
            log.exception("worker crashed for %s", card.ref)
            is_error, result = True, f"Worker crashed: {e}"
        finally:
            # the slot is always given back, whatever fails below: a failed write here once left a finished
            # ticket "working" for hours and blocked its board (ticket 9304deae)
            self.procs.pop(card.ref, None)
            if self.in_main.get(card.project) == card.ref:
                self.in_main.pop(card.project, None)
            stopped = self.stop_reasons.pop(card.ref, None)
            self.current.pop(card.ref, None)
            self.started.pop(card.ref, None)
            if stopped is not None:          # stopped on purpose: off the queue, not "Failed"
                self.finished[card.ref] = time.monotonic()
            if not cancelled:
                _set_worker(card.ref, None)
                try:
                    await close_chrome_of(card.ref)          # the Chrome windows it opened for this ticket
                    kept = [why for repo, wt in worktrees
                            if (why := await asyncio.to_thread(remove_worktree, repo, wt, card.ref))]
                except Exception as e:
                    kept = [f"cleanup failed: {e}"]
                if kept:
                    log.warning("%s: %s", card.ref, "; ".join(kept))
                    result += "\n\nNote: " + "; ".join(kept)
            if stopped is not None:
                with contextlib.suppress(Exception):
                    await board.unqueue(card, f"stopped: {stopped}")
        if stopped is not None:
            log.info("%s stopped: %s", card.ref, stopped)
            await self.notify(card, "stopped", f"Stopped the worker ({stopped}). It's off the queue; "
                                               "queue it again to start over.", [])
            return

        if paused:
            await self._pause_for_limit(card, paused, result)
            return

        photos = sorted(shots.glob("*.png"))
        question = ""
        lines = result.strip().splitlines()
        if lines and lines[-1].strip().upper().startswith(NEEDS_ME):
            question = lines[-1].split(":", 1)[1].strip()
        elif not is_error:
            question = blocked_question(result)   # safety net: it waits on Sanjar but didn't say so
        status, kind = ("Needs me", "needs_me") if question else ("Failed", "failed") if is_error else ("Done", "done")
        if question and await self._auto_answer(card, question, result):
            return
        if status == "Done":
            grants.revoke(card.ref)          # a permission covers one ticket's work, not later ones
        await self._write(card, jarvis=status, note=question or result[-1900:],
                          stage="done" if status == "Done" else None, log_lines=[f"Jarvis {status}: {result[-1800:]}"])
        self.finished[card.ref] = time.monotonic()   # don't re-pick it while the board catches up
        counts = _load_json(REQUEUED)
        if counts.pop(card.ref, None) is not None:   # it finished: earlier restarts don't count any more
            REQUEUED.write_text(json.dumps(counts))
        log.info("%s %s", card.ref, status)
        await self.notify(card, kind, question or result, photos)

    @staticmethod
    def _hold_since() -> float:
        """When the current usage-limit hold began (older holds without set_at: when the file was written)."""
        state = limit_state()
        if state.get("set_at"):
            return float(state["set_at"])
        try:
            return USAGE_LIMIT.stat().st_mtime
        except OSError:
            return time.time()

    async def _pause_for_limit(self, card: Ticket, until: float, result: str):
        """The usage limit stopped this worker: back to Queued (not Failed), no new starts until the
        reset, then it continues in the same session (Sanjar, 2026-11-04: "when reset we should make
        them continue")."""
        engine_name = "Gemini" if JARVIS_ENGINE == "antigravity" or "quota" in (result or "").lower() else "Claude"
        state = limit_state()
        state["until"] = max(float(state.get("until") or 0), until)
        state.setdefault("set_at", time.time())          # readings after this can end the hold early
        state["refs"] = sorted(set(state.get("refs", [])) | {card.ref})
        state["engine"] = engine_name
        first = time.time() - float(state.get("told") or 0) > 3600
        if first:
            state["told"] = time.time()
        USAGE_LIMIT.write_text(json.dumps(state))
        at = time.strftime("%H:%M", time.localtime(state["until"]))
        await self._write(card, jarvis="Queued", note=f"paused by the {engine_name} usage limit; continues after {at}",
                          log_lines=[f"Paused: the {engine_name} usage limit stopped the worker ({result.strip()[-200:]}). "
                                     f"It continues by itself after {at}."])
        self.finished[card.ref] = time.monotonic()
        log.info("%s: paused by the %s usage limit until %s", card.ref, engine_name, at)
        if first and not usagewatch.recently_hit():      # one note per limit; usagewatch may have told him already
            await self.notify(card, "paused", f"The {engine_name} usage limit stopped the workers. They're back in the "
                                              f"queue and continue by themselves after {at}.", [])

    async def _auto_answer(self, card: Ticket, question: str, result: str) -> bool:
        """A worker's question goes to Jarvis first (core/autoanswer.py): he answers it and re-queues the
        ticket (nothing sent to Sanjar), or, if it truly needs Sanjar, tells him in plain words.
        False = Jarvis couldn't handle it: send the worker's question to Sanjar as before."""
        if not autoanswer.enabled(card.project):
            return False
        counts = json.loads(AUTO_ANSWERS.read_text()) if AUTO_ANSWERS.exists() else {}
        if counts.get(card.ref, 0) >= autoanswer.MAX_PER_TICKET:
            return False
        got = await autoanswer.answer(card, question, result)
        if not got:
            return False
        if got["needs_sanjar"]:            # truly his: Jarvis's plain words, not the worker's question
            ask = got["ask_sanjar"]
            await self._write(card, jarvis="Needs me", note=ask[:1900],
                              log_lines=[f"Jarvis Needs me: {result[-1500:]}", f"Jarvis checked it first; this needs Sanjar: {ask}"])
            self.finished[card.ref] = time.monotonic()
            log.info("%s: needs Sanjar after Jarvis looked: %s", card.ref, ask[:120])
            await self.notify(card, "needs_me", ask, [])
            return True
        reply, basis = got["answer"], got["basis"]
        counts[card.ref] = counts.get(card.ref, 0) + 1
        AUTO_ANSWERS.write_text(json.dumps(counts))
        await self._write(card, jarvis="Queued", note=f"answered by Jarvis: {reply}"[:1900],
                          log_lines=[f"Jarvis Needs me: {result[-1500:]}",
                                     f"Sanjar answered (Jarvis, on his behalf; basis: {basis}): {reply}"])
        # no self.finished mark: it should be picked again next, before later tickets in the queue
        # and no message to Sanjar: he asked to hear only what needs him (2026-11-04)
        log.info("%s: answered its question myself (%s), re-queued", card.ref, basis)
        return True

    async def _push_steps(self, card: Ticket, steps: list[str]):
        try:
            await board.update(card, note=steps[-1])
            await board.append_log(card.page_id, [f"• {s}" for s in steps])
        except Exception:
            log.warning("couldn't update steps on %s", card.ref)
