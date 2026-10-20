"""Runner loop: picks tickets marked "Queued" in the Jarvis column of each project's Notion
board and runs a Claude Code worker for each.

One ticket at a time per project (they'd edit the same files), up to MAX_WORKERS projects
at once. Each project keeps its own Claude session, so it remembers earlier tasks there.
Steps go to the ticket live; Done / Failed / Needs me go to Sanjar via `notify`.
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

from . import board
from .board import Ticket
from .config import LOGS, MAX_WORKERS, NEVER_DO, POLL_SECONDS, SHOTS, STATE, project_dirs

log = logging.getLogger("jarvis.runner")
SESSIONS = STATE / "sessions.json"
NEEDS_ME = "NEEDS ME:"

# notify(ticket, kind, text, photos) — kind is "done", "failed" or "needs_me"
Notify = Callable[[Ticket, str, str, list[Path]], Awaitable[None]]


def _load_sessions() -> dict[str, str]:
    try:
        return json.loads(SESSIONS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_session(project: str, session_id: str) -> None:
    sessions = _load_sessions()
    sessions[project] = session_id
    SESSIONS.write_text(json.dumps(sessions, indent=2))


def _worker_prompt(t: Ticket, shots: Path) -> str:
    return f"""Ticket {t.ref} for project {t.project}, from Sanjar's Notion board: {t.title}
Ticket page: {t.url}
{f"Latest note on it: {t.note}" if t.note else ""}

First read the whole ticket page with your Notion tools (description, acceptance criteria,
sub-tasks, and any earlier log or answer from Sanjar at the bottom). Don't change the ticket's
Status or Jarvis columns in Notion; Jarvis does that. If this project has its own skill or
notes for working on board tickets (CLAUDE.md, a <project>-board skill), follow them.

Sanjar is away from the computer and won't approve anything, so work on your own until it's done.
{NEVER_DO}
- If you change something visible in a web app, start it (or use the running one) and save a
  screenshot of the result as a PNG in {shots}/ (e.g. with headless Chrome or Playwright).
- If you truly can't continue without a decision from Sanjar, stop and make the LAST line of
  your final message: {NEEDS_ME} <one short question>
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


class Runner:
    def __init__(self, notify: Notify):
        self.notify = notify
        self.running: dict[str, asyncio.Task] = {}      # project -> worker
        self.procs: dict[str, asyncio.subprocess.Process] = {}

    async def run_forever(self):
        await self._recover()
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("runner tick failed")
            await asyncio.sleep(POLL_SECONDS)

    async def _recover(self):
        """Tickets left Working by a crash/restart can't be resumed mid-step: mark them Failed."""
        for t in await board.with_jarvis("Working"):
            await board.update(t, jarvis="Failed",
                               note="Interrupted: Jarvis restarted while this was running. Set Jarvis to Queued to retry.")
            await self.notify(t, "failed", "Interrupted by a Jarvis restart.", [])

    async def _tick(self):
        for t in await board.queued():
            if len(self.running) >= MAX_WORKERS:
                return
            if t.project in self.running:
                continue
            task = asyncio.create_task(self._work(t), name=f"worker:{t.project}")
            self.running[t.project] = task
            task.add_done_callback(lambda _t, p=t.project: self.running.pop(p, None))

    def stop_project(self, project: str) -> bool:
        proc = self.procs.get(project)
        if proc and proc.returncode is None:
            proc.terminate()
            return True
        return False

    async def _work(self, card: Ticket):
        dirs = project_dirs(card.project)
        shots = SHOTS / card.ref
        shots.mkdir(exist_ok=True)
        log.info("start %s in %s", card.line(), dirs[0])
        await board.update(card, jarvis="Working", note="starting", stage="doing")

        cmd = ["claude", "-p", _worker_prompt(card, shots), "--output-format", "stream-json",
               "--verbose", "--permission-mode", "bypassPermissions",
               "--disallowedTools", "Bash(sudo:*)", "Bash(git push --force:*)", "Bash(git push -f:*)",
               "Bash(rm -rf /:*)", "Bash(rm -rf ~:*)"]
        for extra in dirs[1:]:
            cmd += ["--add-dir", str(extra)]
        session = _load_sessions().get(card.project)
        if session:
            cmd += ["--resume", session]

        result, is_error, last_push, pending = "", False, 0.0, []
        logfile = (LOGS / f"{card.project}-{card.ref}.jsonl").open("w")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=dirs[0], stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, limit=50_000_000)
            self.procs[card.project] = proc
            while line := await proc.stdout.readline():
                logfile.write(line.decode(errors="replace"))
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("session_id"):
                    _save_session(card.project, event["session_id"])
                if event.get("type") == "assistant":
                    for block in event["message"].get("content", []):
                        if block.get("type") == "tool_use":
                            pending.append(_step_text(block))
                elif event.get("type") == "result":
                    result, is_error = event.get("result") or "", bool(event.get("is_error"))
                # push steps to Notion at most every 5 s
                if pending and time.monotonic() - last_push > 5:
                    await self._push_steps(card, pending)
                    pending, last_push = [], time.monotonic()
            await proc.wait()
            if pending:
                await self._push_steps(card, pending)
            if proc.returncode and not result:
                is_error, result = True, f"Worker exited with code {proc.returncode}."
        except Exception as e:
            log.exception("worker crashed for %s", card.ref)
            is_error, result = True, f"Worker crashed: {e}"
        finally:
            logfile.close()
            self.procs.pop(card.project, None)

        photos = sorted(shots.glob("*.png"))
        question = ""
        lines = result.strip().splitlines()
        if lines and lines[-1].strip().upper().startswith(NEEDS_ME):
            question = lines[-1].split(":", 1)[1].strip()
        status, kind = ("Needs me", "needs_me") if question else ("Failed", "failed") if is_error else ("Done", "done")
        await board.update(card, jarvis=status, note=question or result[-1900:],
                           stage="done" if status == "Done" else None)
        await board.append_log(card.page_id, [f"Jarvis {status}: {result[-1800:]}"])
        log.info("%s %s", card.ref, status)
        await self.notify(card, kind, question or result, photos)

    async def _push_steps(self, card: Ticket, steps: list[str]):
        try:
            await board.update(card, note=steps[-1])
            await board.append_log(card.page_id, [f"• {s}" for s in steps])
        except Exception:
            log.warning("couldn't update steps on %s", card.ref)
