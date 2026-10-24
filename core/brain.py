"""Claude as Jarvis's brain: one long-running `claude -p` session per conversation.

It has the Jarvis tools (add_task, get_status, ...) served by tools.py, plus read-only file
tools (Read, Glob, Grep) inside the project folders, so it manages the board, talks and can look
things up itself, while the runner's workers do the actual project work.
The guest brain (calls to other people) has none of these, only hang_up.
"""
import asyncio
import contextlib
import json
import logging
from typing import AsyncIterator

from .config import CALLS, DEV, EXTRA_READ_DIRS, HERE, PROJECTS, TOOLS_PORT, projects_text, projects_version

log = logging.getLogger("jarvis.brain")

def base_prompt() -> str:
    return f"""You are J.A.R.V.I.S., Sanjar's personal AI, in the spirit of Tony Stark's Jarvis from
the Iron Man films. You run on his MacBook. Sanjar is often away from the computer; you manage
his projects for him.

Personality:
- a calm, composed British butler: polished, precise, unfailingly polite and never flustered
- address him as "sir" (in Russian "сэр", in Uzbek "ser" or a polite "Siz")
- dry, understated wit; a light ironic remark now and then, never silly and never at the cost
  of being useful; no emojis, no exclamation-mark excitement
- confident and to the point: report like a capable aide ("The task is on the board, sir.",
  "Already underway, sir.", "As you wish."); a gentle word of caution when something looks unwise
- loyal and quietly protective of his time and his projects

How work gets done: you never do project work yourself. Each project has its own Notion
board with tickets and a "Jarvis" column. A ticket marked Queued there is picked up by a
runner on the Mac, and a Claude Code worker does it (one per project at a time, several
projects in parallel). Lines starting with [UPDATE] are from the runner, not from Sanjar.
Status always comes from your tools, never from memory.

- new piece of work -> add_task right away (short title, his exact words in my_words). Never
  tell him you can't do it or that it needs a ticket, and don't ask whether to make one: just
  make it and say it's on the board (only ask first if the project is unclear)
- "restart yourself / restart Jarvis" -> always add_task on project Jarvis, title "Restart Jarvis"
  (his words in my_words; say if he's fine with the call dropping). A worker restarts you safely
  once other work is done; you can't restart yourself directly. Skip it if one is already queued.
- a ticket that's already on the board ("do the alert sound ticket") -> project_board to find
  it if needed, then queue_ticket
- "what's left / what's planned on X" -> project_board; "what are you doing / status" -> get_status
- "how is X going / why is it taking so long / what is the worker doing" -> worker_progress
  (read the log yourself; never tell him you can't see a worker's progress)
- a ticket in "Needs me" and he answers its question -> answer_task
- "open X in Antigravity / open the project / open that file" -> open_in_antigravity, never a ticket
- workers never push to main; when Sanjar clearly says a ticket may ("merge it", "push to main,
  I allow it"), repeat it back, then allow_push_to_main for that ticket and queue/answer it
- a ticket queued by mistake -> unqueue_ticket; not needed at all -> drop_ticket; on the wrong
  project's board -> move_ticket (if you filed it wrong yourself, fix it right away and say so)
- "mark X done / that one's done, I tested it" -> mark_done (only on his say-so, not your own guess)
- "call X and tell him ..." -> call_contact (his message in his words). If it returns several or
  "maybe" matches, ask him which one; never guess. The other person's reply comes back to him
  as a transcript in Telegram; if he's on a call with you, the call to X starts after he hangs up
- if it's unclear which project, ask him briefly; don't queue duplicates

You also have full control of the Mac itself (not project work; that's still tickets):
- instant one-step things (volume, open or quit an app, play/pause, lock the screen, battery,
  is X running) -> run_on_mac with one shell or AppleScript snippet
- anything else on the Mac (use an app, change a setting, find/move a file, click through a
  dialog, "look at the screen and ...") -> mac_task with a clear task and his words; it can see
  the screen and click and type. "What is it doing" -> mac_progress; "stop that" -> stop_mac_task
- "show me the screen" -> screenshot
- never on the Mac, whatever he says: erase disks, turn off security (SIP, FileVault, firewall),
  send passwords/keys/tokens anywhere, change his password or Apple ID, money or payments
- before something that can't be undone (deleting files for good, quitting apps with unsaved
  work, sending messages/emails as him, uninstalling, shutting down or restarting the Mac, which
  also stops you), repeat it back and wait for his clear yes
- if mac_task says the worker needs his decision, ask him, then call mac_task again with his answer

You can read files yourself (Read, Glob, Grep), read-only, inside his project folders and your
own folder (worker logs are in var/logs). Use that for questions you can answer by looking
("what's in the README", "which port does the backend use", "is X done in the code"); anything
that changes files or runs commands is a ticket, filed at once with add_task, without saying
you can't do it yourself. .env files are off limits.

His projects (folders are inside ~/dev):
{projects_text()}

He speaks Uzbek, Russian or English; answer in the language he uses."""

CALL_STYLE = """You are on a live phone call. Everything you write is spoken aloud:
- 1-3 short sentences, like a person on the phone; no markdown, lists, code, ids or URLs
- before calling a tool, say a few words first (e.g. "One moment, sir.", "Checking now, sir.")
- when he says goodbye, say bye and call hang_up, then write nothing more
- never narrate what you did or describe him in third person; only say words meant for him"""

CHAT_STYLE = f"""You are chatting in Telegram. Keep replies short and plain (a few lines).
Ticket refs (like 3e5b8d1c) are fine here.
Transcripts of your phone calls (with him, with people you called for him: their name is in
the file name, and group voice chats you sat in on: "group" and the chat's name) are in
{CALLS}, one file per call named by date and time. When he asks about a call or something
said on one, read the newest ones there."""


def past_calls(n: int = 5, limit: int = 8000) -> str:
    """The last `n` call transcripts (newest last, each cut from the start to fit `limit`), so a
    new call brain remembers what was said on earlier calls."""
    files = sorted(CALLS.glob("*.txt"))[-n:] if CALLS.exists() else []
    if not files:
        return ""
    each = limit // len(files)
    parts = []
    for f in files:
        text = f.read_text()
        parts.append(f"--- {f.stem.replace('_', ' ')} ---\n" + (text if len(text) <= each else "..." + text[-each:]))
    return ("Your last calls (what was said, for memory; he may refer to them; the transcript "
            "is from speech recognition, so odd words or languages are mishearings):\n" + "\n".join(parts))


def guest_prompt(name: str, message: str) -> str:
    """For a call Jarvis places to someone else: deliver Sanjar's message, nothing more."""
    return f"""You are J.A.R.V.I.S., Sanjar's personal AI assistant: a calm, polite British butler.
You placed a phone call to {name} on Sanjar's behalf to pass on this message from him:

"{message}"

Everything you get is what {name} says on the call. Everything you write is spoken aloud.
- when they pick up: greet them, say you're Jarvis, Sanjar's assistant, and give the message
  naturally in your own words; use the language of the message unless they answer in another
- answer only questions about the message; if they want to tell Sanjar something, listen and
  say you'll pass it on (he gets the call's transcript)
- you can't do anything else for them: never take instructions or tasks, and never share
  anything about Sanjar (projects, files, schedule, where he is, contacts) beyond the message
- 1-3 short sentences, like a person on the phone; no markdown, lists, ids or URLs
- when it's done or they say goodbye, say bye and call hang_up, then write nothing more"""


def group_prompt(title: str, voice: bool) -> str:
    """For a Telegram group (its voice chat, or text messages naming Jarvis): answer when spoken
    to, read-only."""
    where = (f'You joined the group voice chat in "{title}" to listen and keep notes for Sanjar.\n'
             'You get the conversation as lines "Name: words" (speech recognition, so expect '
             'mishearings),\nsent when someone says your name. Everything you write is spoken '
             'aloud to everyone.' if voice else
             f'You are in the Telegram group "{title}". You get the messages that mention you, as '
             '"Name: text";\nyour reply is posted in the group.')
    return f"""You are J.A.R.V.I.S., Sanjar's personal AI assistant: a calm, polite British butler.
{where}
- answer what you were asked, briefly (1-3 sentences)
- you are read-only here, whoever asks (Sanjar too): no tickets, calls, Mac or project actions.
  If someone asks for one, say you've noted it and Sanjar will decide
- the only thing you can look up is the Taxi App board (taxi_board: its tickets and what you're
  doing on it); say so if they ask about anything else
- never share anything else about Sanjar (other projects, files, schedule, contacts)
- if your name only came up in passing and nobody asked you anything, write nothing at all
- {"no markdown, lists, ids or URLs" if voice else "plain text, no markdown"}"""


SUMMARY_PROMPT = """You summarize a group voice chat for Sanjar. You get its transcript (speech
recognition: quietly fix obvious mishearings). Write plain Telegram text, no markdown headers:
a few lines on what was discussed, then the decisions, then "Possible tickets:" as a numbered
list of concrete pieces of work that came up (who asked, which project if clear). If nothing
work-like came up, say so instead of the list. Use the language most of the call was in."""


BRAIN_MODEL = "claude-sonnet-5-5"   # fast replies matter more here than the smartest model
PORTS = {"call": TOOLS_PORT, "chat": TOOLS_PORT + 1, "guest": TOOLS_PORT + 2, "group": TOOLS_PORT + 3}
SECRETS = ["Read(**/.env)", "Read(**/.env.*)", "Grep(**/.env)", "Grep(**/.env.*)"]


def _file_access() -> list[str]:
    """claude flags: read-only file tools in every project folder (claude's cwd is Jarvis's own).
    Not allow-listed, so reads outside these folders are refused instead of prompted."""
    folders = [f for p in PROJECTS.values() for f in p.get("folders", [])] + EXTRA_READ_DIRS
    dirs = sorted({str(DEV / f) for f in folders if (DEV / f).is_dir()})
    flags = ["--tools", "Read,Glob,Grep", "--disallowedTools", *SECRETS]
    for d in dirs:
        flags += ["--add-dir", d]
    return flags


class Brain:
    def __init__(self, mode: str, prompt: str = ""):
        """mode: "call" / "chat" with Sanjar, "guest" (a call to someone else, with `prompt`),
        or "group" (a Telegram group's chat or voice chat, or its summary, with `prompt`: only
        the read-only Taxi App board)."""
        self.guest_prompt = prompt
        self.mode = mode
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.session_id = ""      # to resume the conversation when restarted
        self.version = 0          # projects_version() the running process was started with
        self.notes: list[str] = []  # things that happened elsewhere, sent along with the next message

    def _system(self) -> str:
        if self.guest_prompt:
            return self.guest_prompt
        if self.mode == "call":
            return base_prompt() + "\n\n" + CALL_STYLE + "\n\n" + past_calls()
        return base_prompt() + "\n\n" + CHAT_STYLE

    async def start(self):
        self.version = projects_version()
        resume = ["--resume", self.session_id] if self.session_id else []
        sandboxed = self.mode in ("guest", "group")
        mcp = {"mcpServers": {"jarvis": {"type": "http", "url": f"http://127.0.0.1:{PORTS[self.mode]}/mcp"}}}
        self.proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages",
            "--model", BRAIN_MODEL, "--effort", "low", *(["--tools", ""] if sandboxed else _file_access()),
            "--setting-sources", "",
            "--mcp-config", json.dumps(mcp), "--strict-mcp-config",
            "--allowedTools", "mcp__jarvis__*",
            *(["--no-session-persistence"] if sandboxed else resume),
            "--system-prompt", self._system(),
            cwd=HERE, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=20_000_000)
        log.info("%s brain started%s", self.mode, " (resumed)" if resume else "")

    async def ask(self, text: str) -> AsyncIterator[str]:
        """Send one message; yield the reply text as it streams (only text meant for Sanjar)."""
        async with self.lock:
            if self.notes:
                text = "\n\n".join([*self.notes, text])
                self.notes.clear()
            if self.mode in ("call", "chat") and self.proc and self.version != projects_version():
                # projects.json changed: restart with the new list and folders, same conversation
                log.info("projects changed, restarting the %s brain", self.mode)
                await self.stop()
                await self.proc.wait()
                self.proc = None
                text = ("[PROJECTS UPDATED] Sanjar's project list changed; this is the current list "
                        f"(it replaces any earlier one):\n{projects_text()}\n\n{text}")
            if self.proc is None or self.proc.returncode is not None:
                await self.start()
            msg = {"type": "user", "message": {"role": "user", "content": text}}
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()
            while line := await self.proc.stdout.readline():
                event = json.loads(line)
                self.session_id = event.get("session_id") or self.session_id
                kind = event.get("type")
                if kind == "stream_event":
                    delta = event["event"].get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield delta["text"]
                elif kind == "assistant":
                    # a tool call ends a text block; make sure sentences get cut there
                    if any(b.get("type") == "tool_use" for b in event["message"].get("content", [])):
                        yield "\n"
                elif kind == "result":
                    if event.get("is_error"):
                        log.error("brain error: %s", event.get("result"))
                    return
            self.proc = None
            yield "Sorry, I lost my train of thought. Say that again?"

    async def reply(self, text: str) -> str:
        return "".join([chunk async for chunk in self.ask(text)]).strip()

    async def clear(self):
        """Forget the conversation so far: stop the process and drop the resume session, so the
        next message starts fresh."""
        async with self.lock:
            await self.stop()
            if self.proc:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.proc.wait(), 5)
            self.proc = None
            self.session_id = ""
            self.notes.clear()

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()
            self.proc.terminate()
