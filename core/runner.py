"""Runner loop: picks tickets marked "Queued" in the Jarvis column of each project's Notion
board and runs a Claude Code worker for each.

One ticket at a time per project (they'd edit the same files), up to MAX_WORKERS projects
at once. Each project keeps its own Claude session, so it remembers earlier tasks there.
Steps go to the ticket live; Done / Failed / Needs me go to Sanjar via `notify`.
"""
import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from . import board, grants
from .brain import BRAIN_MODEL   # workers run on the same model as the Jarvis brain (Opus)
from .board import Ticket
from .config import (rules_text, GIT_FLOW, HERE, LOGS, MAX_WORKERS, NEVER_DO, NEVER_DO_MAIN_OK, POLL_SECONDS,
                     SHOTS, STATE, project_dirs)

log = logging.getLogger("jarvis.runner")
SESSIONS = STATE / "sessions.json"
NEEDS_ME = "NEEDS ME:"
MAX_CONTINUES = 3            # resume a worker this many times if its background work was cut off
SWEEP_EVERY = 30 * 60        # how often to look for tickets stuck In Progress
STUCK_AFTER = 2 * 3600       # In Progress this long with no live worker = stuck
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
    tail = result.strip()[-700:]
    if not _BLOCKED.search(tail):
        return ""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", tail) if s.strip()]
    # merging into dev is the worker's own job, never something to wait on Sanjar for
    hits = [s for s in sentences if _BLOCKED.search(s) and not re.search(r"\bdev\b", s, re.I)]
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
- Finish with a short plain summary (2-4 sentences): what you changed, which files, whether
  tests pass, anything he should know."""


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

    async def run_forever(self):
        await self._recover()
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("runner tick failed")
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
            lines = [f"- {t.ref} [{t.project}] {t.title} — untouched {h:.0f} h" + (f", Jarvis {t.jarvis}" if t.jarvis else "")
                     for t, h in stuck[:15]]
            await self.message("⏸ Stuck In Progress with nobody working on them:\n" + "\n".join(lines)
                               + "\n\nTell me to queue any of them (e.g. \"queue " + stuck[0][0].ref + "\") and a worker will pick it up.")

    async def _recover(self):
        """Tickets left Working by a crash/restart can't be resumed mid-step: mark them Failed."""
        for t in await board.with_jarvis("Working"):
            await board.update(t, jarvis="Failed",
                               note="Interrupted: Jarvis restarted while this was running. Set Jarvis to Queued to retry.")
            await self.notify(t, "failed", "Interrupted by a Jarvis restart.", [])

    async def _tick(self):
        cutoff = time.monotonic() - 120
        self.finished = {ref: ts for ref, ts in self.finished.items() if ts > cutoff}
        for t in await board.queued():
            if len(self.running) >= MAX_WORKERS:
                return
            if t.project in self.running:
                continue
            if t.ref in self.finished:
                # just finished (the board may still read as Queued for a moment); don't re-run it
                log.info("skipping %s: finished %.0fs ago", t.ref, time.monotonic() - self.finished[t.ref])
                continue
            task = asyncio.create_task(self._work(t), name=f"worker:{t.project}")
            self.running[t.project] = task
            task.add_done_callback(lambda _t, p=t.project: self.running.pop(p, None))

    def progress(self, project: str, ticket: str = "") -> str:
        """For the worker_progress tool: the live log of a project's worker (or a past ticket's)."""
        name = next((p for p in {*self.current, *(l.stem.rsplit("-", 1)[0] for l in LOGS.glob("*-*.jsonl"))}
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
        dirs = project_dirs(card.project)
        shots = SHOTS / card.ref
        shots.mkdir(exist_ok=True)
        log.info("start %s in %s", card.line(), dirs[0])
        self.current[card.project] = card
        await board.update(card, jarvis="Working", note="starting", stage="doing")

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
                    *make_cmd(prompt), cwd=dirs[0], stdin=asyncio.subprocess.DEVNULL,
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
        if status == "Done":
            grants.revoke(card.ref)          # a permission covers one ticket's work, not later ones
        await board.update(card, jarvis=status, note=question or result[-1900:],
                           stage="done" if status == "Done" else None)
        await board.append_log(card.page_id, [f"Jarvis {status}: {result[-1800:]}"])
        self.finished[card.ref] = time.monotonic()   # don't re-pick it while the board catches up
        log.info("%s %s", card.ref, status)
        await self.notify(card, kind, question or result, photos)

    async def _push_steps(self, card: Ticket, steps: list[str]):
        try:
            await board.update(card, note=steps[-1])
            await board.append_log(card.page_id, [f"• {s}" for s in steps])
        except Exception:
            log.warning("couldn't update steps on %s", card.ref)
