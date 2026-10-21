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

from . import board, grants
from .board import Ticket
from .config import HERE, LOGS, MAX_WORKERS, NEVER_DO, NEVER_DO_MAIN_OK, POLL_SECONDS, SHOTS, STATE, project_dirs

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
{NEVER_DO_MAIN_OK if grants.allowed(t.ref, grants.PUSH_MAIN) else NEVER_DO}
- If you change something visible in a web app, start it (or use the running one) and save a
  screenshot of the result as a PNG in {shots}/ (e.g. with headless Chrome or Playwright).
- Never touch ~/Documents, ~/Desktop or ~/Downloads (not even `ls`): macOS asks for permission
  there and the command hangs forever, since nobody is at the Mac to click Allow. If a note or
  memory says to put files there, skip that step and mention it in your summary.
- To send Sanjar a file (an APK, a PDF...), run:
  {HERE / ".venv/bin/python"} {HERE / "scripts/send_file.py"} <file> "<caption>"
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

    def progress(self, project: str, ticket: str = "") -> str:
        """For the worker_progress tool: the live log of a project's worker (or a past ticket's)."""
        name = next((p for p in {*self.current, *(l.stem.rsplit("-", 1)[0] for l in LOGS.glob("*-*.jsonl"))}
                     if p.lower() == project.strip().lower()), project)
        current = self.current.get(name)
        path = worker_log(name, ticket or (current.ref if current else ""))
        if not path:
            return f"No worker log for {project!r}" + (f" ticket {ticket}" if ticket else "") + "."
        return progress_text(path, running=current is not None and path.stem.endswith(current.ref))

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
        self.current[card.project] = card
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
                logfile.flush()   # worker_progress reads it live
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
            self.current.pop(card.project, None)

        photos = sorted(shots.glob("*.png"))
        question = ""
        lines = result.strip().splitlines()
        if lines and lines[-1].strip().upper().startswith(NEEDS_ME):
            question = lines[-1].split(":", 1)[1].strip()
        status, kind = ("Needs me", "needs_me") if question else ("Failed", "failed") if is_error else ("Done", "done")
        if status == "Done":
            grants.revoke(card.ref)          # a permission covers one ticket's work, not later ones
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
