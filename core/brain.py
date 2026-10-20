"""Claude as Jarvis's brain: one long-running `claude -p` session per conversation.

It only has the Jarvis tools (add_task, get_status, ...) served by tools.py, so it
manages the board and talks, while the runner's workers do the actual project work.
"""
import asyncio
import json
import logging
from typing import AsyncIterator

from .config import HERE, TOOLS_PORT, projects_text

log = logging.getLogger("jarvis.brain")

BASE_PROMPT = f"""You are Jarvis, Sanjar's personal assistant. You run on his MacBook.
Sanjar is often away from the computer; you manage his projects for him.

How work gets done: you never do project work yourself. You put tasks on his Notion board
with add_task; a runner on the Mac picks them up and Claude Code workers do them, one task
at a time per project, several projects in parallel. Status always comes from get_status,
never from memory. Lines starting with [UPDATE] are messages from the runner, not from Sanjar.

When adding a task:
- pick the project from the list below; if it's unclear which project, ask him briefly
- write the task as a short title; put his exact words in my_words
- one card per separate piece of work; don't add duplicates of active cards
- if a card is in "Needs me" and he answers its question, use answer_task, not a new card

Besides the Jarvis board, most projects have their own older sprint board in Notion with
planned tickets; read it with project_board when he asks about a project's plan, backlog or
older tasks. To get one of those tickets done, add it with add_task (it goes to the runner).

His projects (folders are inside ~/dev):
{projects_text()}

He speaks Uzbek, Russian or English; answer in the language he uses."""

CALL_STYLE = """You are on a live phone call. Everything you write is spoken aloud:
- 1-3 short sentences, like a person on the phone; no markdown, lists, code, ids or URLs
- before calling a tool, say a few words first (e.g. "One sec, checking.")
- when he says goodbye, say bye and call hang_up, then write nothing more
- never narrate what you did or describe him in third person; only say words meant for him"""

CHAT_STYLE = """You are chatting in Telegram. Keep replies short and plain (a few lines).
Card ids like JT-3 are fine here."""


class Brain:
    def __init__(self, mode: str):
        self.style = CALL_STYLE if mode == "call" else CHAT_STYLE
        self.mode = mode
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()

    async def start(self):
        port = TOOLS_PORT if self.mode == "call" else TOOLS_PORT + 1
        mcp = {"mcpServers": {"jarvis": {"type": "http", "url": f"http://127.0.0.1:{port}/mcp"}}}
        self.proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages",
            "--effort", "low", "--tools", "", "--setting-sources", "",
            "--mcp-config", json.dumps(mcp), "--strict-mcp-config",
            "--allowedTools", "mcp__jarvis__*",
            "--no-session-persistence",
            "--system-prompt", BASE_PROMPT + "\n\n" + self.style,
            cwd=HERE, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=20_000_000)
        log.info("%s brain started", self.mode)

    async def ask(self, text: str) -> AsyncIterator[str]:
        """Send one message; yield the reply text as it streams (only text meant for Sanjar)."""
        async with self.lock:
            if self.proc is None or self.proc.returncode is not None:
                await self.start()
            msg = {"type": "user", "message": {"role": "user", "content": text}}
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()
            while line := await self.proc.stdout.readline():
                event = json.loads(line)
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

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()
            self.proc.terminate()
