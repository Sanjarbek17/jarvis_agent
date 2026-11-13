"""Mac worker: a Claude Code session that does things on the Mac itself for Sanjar — apps,
settings, files, AppleScript, and the mouse and keyboard (scripts/mac.py) for apps that can't
be scripted. One job at a time; it keeps one session, so "now close it" knows what "it" is.

The brain hands jobs over with the mac_task tool. A quick job's result goes straight back to
the brain; a slow one keeps running and its result comes later via `notify`.
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable

from .config import HERE, LOGS, MAC_NEVER_DO, SHOTS, STATE
from .runner import NEEDS_ME, progress_text

log = logging.getLogger("jarvis.mac")
SESSION = STATE / "mac_session.json"
WORKDIR = STATE / "mac"
WORKDIR.mkdir(exist_ok=True)
PY = HERE / ".venv/bin/python"

# notify(task, text, photos, question) — a job that finished after mac_task stopped waiting
Notify = Callable[[str, str, list[Path], bool], Awaitable[None]]


def _prompt(task: str, words: str, shots: Path) -> str:
    return f"""Mac task from Sanjar (via Jarvis): {task}
His words: "{words}"

You are Jarvis's hands on Sanjar's MacBook (user {Path.home().name}, home {Path.home()}). He's
usually away from it and drives it by phone, so do the task yourself, start to finish.
How to get things done, in this order of preference:
1. shell commands, `open -a "<App>"`, `osascript` (AppleScript / JXA: apps, windows, volume,
   music, notifications, System Events), `defaults`, `shortcuts run "<name>"`
2. for things only the screen can do (an app with no scripting, a button, a dialog), look and act:
     {PY} {HERE / "scripts/mac.py"} shot           -> prints a PNG path; open it with Read to see the screen
     {PY} {HERE / "scripts/mac.py"} click X Y      (also: --right / --double, move, drag, scroll, type, key)
   Coordinates are the pixels of that screenshot. Take a new shot after each step to check it worked.
   If it says Accessibility / Screen Recording isn't allowed, stop and tell Sanjar (run `mac.py check`).
- Commands that may trigger a macOS permission prompt (~/Documents, ~/Desktop, ~/Downloads, Photos,
  Contacts...) can hang forever: always wrap them as `timeout 20 <command>`. If one times out, take
  a shot: if a dialog is asking, you may click Allow on it for this task.
- A web page you open only to do a step or look something up: {PY} {HERE / "scripts/real_chrome.py"} open <url>
  [--profile "Profile 4"] (a new window), and `real_chrome.py close --window <id>` when you're done (they're also
  closed after 30 min). Leave open only what he asked to see. Never pile up tabs (ticket 3a61de45).
- Never copy his Chrome profile's cookies, Login Data or Local State into another browser or folder: Google then
  signs him out of all his accounts (ticket c09280c2). Work in his real Chrome instead.
- Save a screenshot of the end result as a PNG in {shots}/ when it's something visible.
- To send Sanjar a file, run: {PY} {HERE / "scripts/send_file.py"} <file> "<caption>"

{MAC_NEVER_DO}
- If it would do something on that list, or you can't continue without a decision from him,
  stop and make the LAST line of your final message: {NEEDS_ME} <one short question>
  (he answers and the next task says what he decided).

Finish with 1-3 plain sentences meant for him: what you did and what he should know."""


class MacWorker:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.proc: asyncio.subprocess.Process | None = None
        self.current = ""          # task being done
        self.log_path: Path | None = None

    def _session(self) -> str:
        try:
            return json.loads(SESSION.read_text()).get("id", "")
        except (FileNotFoundError, json.JSONDecodeError):
            return ""

    async def run(self, task: str, words: str, app_chat: str = "") -> tuple[str, list[Path], bool]:
        """Do one task (waits for the one before it). Returns (text for Sanjar, photos, is_question)."""
        async with self.lock:
            stamp = time.strftime("%Y%m%d%H%M%S")
            shots = SHOTS / f"mac-{stamp}"
            shots.mkdir(exist_ok=True)
            self.current, self.log_path = task, LOGS / f"mac-{stamp}.jsonl"
            cmd = ["claude", "-p", _prompt(task, words, shots), "--output-format", "stream-json",
                   "--verbose", "--permission-mode", "bypassPermissions", "--add-dir", str(Path.home()),
                   "--disallowedTools", "Bash(sudo:*)", "Bash(rm -rf /:*)", "Bash(rm -rf ~:*)",
                   "Bash(diskutil erase:*)", "Bash(csrutil:*)"]
            if session := self._session():
                cmd += ["--resume", session]
            log.info("mac task: %s", task)
            result, is_error = "", False
            try:
                with self.log_path.open("w") as logfile:
                    self.proc = await asyncio.create_subprocess_exec(
                        *cmd, cwd=WORKDIR, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT, limit=50_000_000,
                        env={**os.environ, "JARVIS_APP_CHAT": app_chat, "JARVIS_OWNER": "mac"})
                    while line := await self.proc.stdout.readline():
                        logfile.write(line.decode(errors="replace"))
                        logfile.flush()
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get("session_id"):
                            SESSION.write_text(json.dumps({"id": event["session_id"]}))
                        if event.get("type") == "result":
                            result, is_error = event.get("result") or "", bool(event.get("is_error"))
                    await self.proc.wait()
                    if self.proc.returncode and not result:
                        is_error, result = True, f"The Mac worker stopped (exit code {self.proc.returncode})."
            except Exception as e:
                log.exception("mac worker crashed")
                is_error, result = True, f"The Mac worker crashed: {e}"
            finally:
                self.proc, self.current = None, ""
            lines = result.strip().splitlines()
            question = bool(lines) and lines[-1].strip().upper().startswith(NEEDS_ME)
            if question:
                result = "\n".join(lines[:-1] + [lines[-1].split(":", 1)[1].strip()])
            log.info("mac task %s: %s", "failed" if is_error else "needs Sanjar" if question else "done", task)
            return ("Failed: " if is_error else "") + result.strip(), sorted(shots.glob("*.png")), question

    def progress(self) -> str:
        if not self.log_path or not self.log_path.exists():
            return "No Mac task has run yet."
        head = f"Mac task now: {self.current}\n" if self.current else ""
        return head + progress_text(self.log_path, running=bool(self.current))

    def stop(self) -> bool:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            return True
        return False

    def forget(self) -> None:
        """Start the next task with a fresh session (no memory of earlier Mac tasks)."""
        SESSION.unlink(missing_ok=True)


async def run_quick(script: str, language: str = "shell", timeout: float = 30) -> str:
    """One shell or AppleScript snippet, for instant things (volume, open an app, lock the screen)."""
    cmd = ["osascript", "-e", script] if language.lower() in ("applescript", "osascript") else ["/bin/zsh", "-c", script]
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=WORKDIR, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        return f"Timed out after {timeout:.0f} s (maybe a macOS permission prompt is waiting); use mac_task instead."
    text = out.decode(errors="replace").strip()[-3000:]
    return f"exit {proc.returncode}" + (f"\n{text}" if text else "")

