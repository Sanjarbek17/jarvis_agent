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
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from . import autoanswer, board, grants, ramguard, tgfmt
from .brain import BRAIN_MODEL   # workers run on the same model as the Jarvis brain (Opus)
from .board import Ticket
from .config import (rules_text, GIT_FLOW, HERE, LOGS, NEVER_DO, NEVER_DO_MAIN_OK, POLL_SECONDS,
                     SHOTS, STATE, project_dirs)

log = logging.getLogger("jarvis.runner")
SESSIONS = STATE / "sessions.json"
NEEDS_ME = "NEEDS ME:"
MAX_CONTINUES = 3            # resume a worker this many times if its background work was cut off
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
RESTART_PENDING = HERE / "var" / "restart_pending"   # restart_when_idle.sh is waiting: start no new workers
REQUEUE_AFTER = STATE / "requeue_after.json"   # {"ref": [refs]}: queue those again once "ref" has started
AUTO_ANSWERS = STATE / "auto_answers.json"   # ticket ref -> how many questions Jarvis answered itself
STUCK_TOLD = STATE / "stuck_told.json"   # ticket ref -> when Sanjar was last told it's stuck
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


def _load_sessions() -> dict[str, str]:
    try:
        return json.loads(SESSIONS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_session(project: str, session_id: str) -> None:
    sessions = _load_sessions()
    sessions[project] = session_id
    SESSIONS.write_text(json.dumps(sessions, indent=2))


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
    return f"""Ticket {t.ref} for project {t.project}, from Sanjar's Notion board: {t.title}
Ticket page: {t.url}
{f"Latest note on it: {t.note}" if t.note else ""}

First read the whole ticket page with your Notion tools (description, acceptance criteria,
sub-tasks, and any earlier log or answer from Sanjar at the bottom). Don't change the ticket's
Status or Jarvis columns in Notion; Jarvis does that. If this project has its own skill or
notes for working on board tickets (CLAUDE.md, a <project>-board skill), follow them.
"""


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
- Never touch ~/Documents, ~/Desktop or ~/Downloads (not even `ls`): macOS asks for permission
  there and the command hangs forever, since nobody is at the Mac to click Allow. If a note or
  memory says to put files there, skip that step and mention it in your summary.
- To send Sanjar a file (an APK, a PDF...), run:
  {HERE / ".venv/bin/python"} {HERE / "scripts/send_file.py"} <file> "<caption>"
- If you truly can't continue without a decision from Sanjar, stop and make the LAST line of
  your final message: {NEEDS_ME} <one short question>
- Never end your turn while background agents, workflows or background commands you started are
  still running: wait for each to finish (or stop it on purpose) before your final summary. When
  your turn ends, anything still running is killed and its work is lost.
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
    name, args = block.get("name", ""), block.get("input", {})
    if name in ("Edit", "Write", "MultiEdit"):
        return f"editing {Path(args.get('file_path', '')).name}"
    if name == "Read":
        return f"reading {Path(args.get('file_path', '')).name}"
    if name == "Bash":
        return f"running: {(args.get('description') or args.get('command', ''))[:80]}"
    if name in ("Grep", "Glob"):
        return "searching the code"
    if name == "TodoWrite":
        return "planning the steps"
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
    def __init__(self, notify: Notify):
        self.notify = notify
        self.running: dict[str, asyncio.Task] = {}      # project -> worker
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.current: dict[str, Ticket] = {}            # project -> ticket being worked on
        self.stop_reasons: dict[str, str] = {}          # project -> why its worker was stopped on purpose
        self.finished: dict[str, float] = {}            # ticket ref -> when it finished (monotonic)
        self.message: Callable[[str], Awaitable] | None = None   # plain Telegram text to Sanjar
        self.last_sweep = 0.0
        self.held_logged = 0.0                          # last "held for RAM" log line (monotonic)
        self.last_start: dict[str, float] = {}          # project -> when its last worker started (fair turns)
        self.wait_reason = ""                           # why the next queued ticket isn't starting
        self.started: dict[str, float] = {}             # project -> when its current ticket started (epoch)

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
        pending, counts = _load_json(PENDING), _load_json(REQUEUED)
        for t in await board.with_jarvis("Working"):
            if t.ref in pending:
                continue                      # its result is saved; _flush_pending writes it
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
        for t in self.pick(await board.queued()):
            if self.running and time.monotonic() - max(self.last_start.values(), default=0) < START_GAP:
                return                       # one start at a time; the next check measures it first
            pids = [p.pid for p in self.procs.values() if p.returncode is None]
            ok, why = ramguard.can_start(len(self.running), worker_pids=pids)
            if not ok:                       # 8 GB Mac: the ticket waits in the queue until RAM frees up
                self._waiting(t, why)
                return
            self._waiting(None, "")
            task = asyncio.create_task(self._work(t), name=f"worker:{t.project}")
            self.last_start[t.project] = time.monotonic()
            self.running[t.project] = task
            task.add_done_callback(lambda _t, p=t.project: self.running.pop(p, None))

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
        one per project (its own order, or its earliest deadline), tickets with a "Due" date first
        (soonest first), then the project whose last worker started longest ago, so a project with
        a long queue can't keep the slots from the others."""
        heads: dict[str, tuple[int, Ticket]] = {}
        for i, t in enumerate(queued):
            if t.project in self.running:
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
        for project, card in sorted(self.current.items(), key=lambda it: self.started.get(it[0], 0)):
            path = worker_log(project, card.ref)
            step, active = last_step(path) if path else ("starting", 0.0)
            workers.append({"project": project, "ref": card.ref, "title": card.title, "url": card.url,
                            "started": self.started.get(project, 0.0), "step": step, "active": active})
        return {"count": len(workers), "workers": workers, "waiting": self.wait_reason, "at": time.time()}

    def progress(self, project: str, ticket: str = "") -> str:
        """For the worker_progress tool: the live log of a project's worker (or a past ticket's)."""
        name = next((p for p in {*self.current, *board.boards(), *(l.stem.rsplit("-", 1)[0] for l in LOGS.glob("*-*.jsonl"))}
                     if p.lower() == project.strip().lower()), project)
        current = self.current.get(name)
        path = worker_log(name, ticket or (current.ref if current else ""))
        if not path:
            return f"No worker log for {project!r}" + (f" ticket {ticket}" if ticket else "") + "."
        return progress_text(path, running=current is not None and path.stem.endswith(current.ref))

    def stop_project(self, project: str, reason: str = "Sanjar used /stop") -> bool:
        """Stop a project's worker on purpose: its ticket goes off the queue (not "Failed")."""
        proc = self.procs.get(project)
        if proc and proc.returncode is None:
            self.stop_reasons[project] = reason
            proc.terminate()
            return True
        return False

    async def stop_and_wait(self, project: str, reason: str, timeout: float = 30) -> Ticket | None:
        """stop_project, then wait until the worker has wrapped up; returns the ticket it was on."""
        card = self.current.get(project)
        if not self.stop_project(project, reason):
            return None
        end = time.monotonic() + timeout
        while project in self.current and time.monotonic() < end:
            await asyncio.sleep(0.5)
        return card

    async def _work(self, card: Ticket):
        _mark_from_sanjar(card)
        dirs = project_dirs(card.project)
        shots = SHOTS / card.ref
        shots.mkdir(exist_ok=True)
        log.info("start %s in %s", card.line(), dirs[0])
        if not await self._write(card, jarvis="Working", note="starting", stage="doing", keep=False):
            self.finished[card.ref] = time.monotonic()   # board unreachable: leave it Queued, retry later
            return
        self.current[card.project] = card
        self.started[card.project] = time.time()

        def make_cmd(prompt: str) -> list[str]:
            cmd = ["claude", "-p", prompt, "--model", BRAIN_MODEL,
                   "--output-format", "stream-json",
                   "--verbose", "--permission-mode", "bypassPermissions",
                   "--disallowedTools", "Bash(sudo:*)", "Bash(git push --force:*)", "Bash(git push -f:*)",
                   "Bash(rm -rf /:*)", "Bash(rm -rf ~:*)"]
            for extra in dirs[1:]:
                cmd += ["--add-dir", str(extra)]
            session = _load_sessions().get(card.project)
            if session:
                cmd += ["--resume", session]
            return cmd

        result, is_error, last_push, pending = "", False, 0.0, []
        prompt, attempt = _worker_prompt(card, shots), 0
        logfile = (LOGS / f"{card.project}-{card.ref}.jsonl").open("w")
        try:
            while True:
                bg: dict[str, str] = {}        # background tasks started in this run, still running
                cut: dict[str, str] = {}       # ...and ones killed before finishing
                stopped_on_purpose: set[str] = set()
                proc = await asyncio.create_subprocess_exec(
                    *make_cmd(prompt), cwd=dirs[0], stdin=asyncio.subprocess.DEVNULL, env={**os.environ, **GIT_ENV},
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=50_000_000)
                self.procs[card.project] = proc
                while line := await proc.stdout.readline():
                    logfile.write(line.decode(errors="replace"))
                    logfile.flush()   # worker_progress reads it live
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("session_id"):
                        _save_session(card.project, event["session_id"])
                    kind = event.get("type")
                    if kind == "assistant":
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
                    elif kind == "result":
                        result, is_error = event.get("result") or "", bool(event.get("is_error"))
                    # push steps to Notion at most every 5 s
                    if pending and time.monotonic() - last_push > 5:
                        await self._push_steps(card, pending)
                        pending, last_push = [], time.monotonic()
                await proc.wait()
                if pending:
                    await self._push_steps(card, pending)
                    pending = []
                if proc.returncode and not result:
                    is_error, result = True, f"Worker exited with code {proc.returncode}."
                unfinished = [d for d in [*bg.values(), *cut.values()] if d]
                if (is_error or not unfinished or attempt >= MAX_CONTINUES or proc.returncode < 0
                        or card.project in self.stop_reasons):
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
        except Exception as e:
            log.exception("worker crashed for %s", card.ref)
            is_error, result = True, f"Worker crashed: {e}"
        finally:
            logfile.close()
            self.procs.pop(card.project, None)
            stopped = self.stop_reasons.pop(card.project, None)
            if stopped is not None:          # stopped on purpose: off the queue, not "Failed"
                with contextlib.suppress(Exception):
                    await board.unqueue(card, f"stopped: {stopped}")
                self.finished[card.ref] = time.monotonic()
            self.current.pop(card.project, None)
        if stopped is not None:
            log.info("%s stopped: %s", card.ref, stopped)
            await self.notify(card, "stopped", f"Stopped the worker ({stopped}). It's off the queue; "
                                               "queue it again to start over.", [])
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
