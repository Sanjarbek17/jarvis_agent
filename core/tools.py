"""Jarvis tools for the Claude brain, served as MCP over local HTTP from inside jarvis.py.

Three servers: the call brain (has hang_up), the Telegram brain, and the guest brain for calls
Jarvis places to other people (only hang_up, so the person on the line can't use the others).
The tools hold the Notion token and the Telegram session; Claude only sees results.
"""
import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

import uvicorn
from mcp.server.mcpserver import MCPServer

from . import board
from .config import SHOTS, TOOLS_PORT

log = logging.getLogger("jarvis.tools")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class Hooks:
    """Filled in by jarvis.py so tools can reach Telegram and the call."""
    send_photo: Callable[[Path, str], Awaitable[None]]
    hang_up: Callable[[], Awaitable[None]]
    call_contact: Callable[[str, str], Awaitable[str]]
    worker_progress: Callable[[str, str], str]


hooks = Hooks()


async def take_screenshot(url: str = "") -> Path:
    path = SHOTS / f"shot_{int(time.time())}.png"
    if url:
        cmd = [CHROME, "--headless=new", "--hide-scrollbars", "--window-size=1440,900",
               f"--screenshot={path}", url]
    else:
        cmd = ["screencapture", "-x", str(path)]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(proc.communicate(), 60)
    if not path.exists():
        why = err.decode(errors="replace").strip()[-300:] or f"exit code {proc.returncode}"
        if not url and "could not create image" in why.lower():
            why += (" (macOS blocks screen capture for Jarvis: it needs Screen Recording permission in "
                    "System Settings > Privacy & Security for the app that runs it, and the display must be awake)")
        raise RuntimeError(why)
    return path


def make_server(source: str) -> MCPServer:
    server = MCPServer("jarvis")

    if source in ("Call", "Guest"):
        @server.tool()
        async def hang_up() -> str:
            """End the phone call (after saying bye)."""
            asyncio.create_task(hooks.hang_up())
            return "Hanging up"

    if source == "Guest":
        return server

    @server.tool()
    async def add_task(project: str, task: str, my_words: str) -> str:
        """Put a NEW ticket on the project's own Notion board, queued for Jarvis; a Claude Code
        worker will do it. project: a known project name. task: short title. my_words: what
        Sanjar actually said. For a ticket that already exists on the board, use queue_ticket."""
        if project not in board.boards():
            return f"Unknown project {project!r}. Known: {', '.join(board.boards())}"
        t = await board.add_ticket(project, task, my_words, source)
        return f"Added ticket {t.ref} to the {project} board and queued it: {task}"

    @server.tool()
    async def queue_ticket(project: str, ticket: str) -> str:
        """Hand an EXISTING ticket on the project's board to Jarvis (sets its Jarvis column to Queued).
        ticket: words from its title, or its ref. Returns the matches if it's ambiguous."""
        if project not in board.boards():
            return f"Unknown project {project!r}. Known: {', '.join(board.boards())}"
        found = await board.find_open(project, ticket)
        if len(found) != 1:
            return (f"{len(found)} open tickets match {ticket!r}" +
                    ("".join(f"\n- {t.ref} {t.title}" for t in found[:10]) if found else ". Try other words."))
        t = found[0]
        if t.jarvis in ("Queued", "Working"):
            return f"{t.ref} is already {t.jarvis}: {t.title}"
        await board.update(t, jarvis="Queued", note=f"queued from {source}")
        return f"Queued {t.ref}: {t.title}"

    @server.tool()
    async def get_status(project: str = "") -> str:
        """Jarvis's work: tickets Queued, Working, Needs me, and finished in the last day. Empty = all projects."""
        return await board.status_text(project)

    @server.tool()
    async def project_board(project: str, include_done: bool = False) -> str:
        """A project's whole board: counts per status and its open tickets by sprint (with refs)."""
        if project not in board.boards():
            return f"No board known for {project!r}."
        return await board.project_board(project, include_done)

    @server.tool()
    async def answer_task(ticket_ref: str, answer: str) -> str:
        """Give Sanjar's answer to a ticket waiting in "Needs me"; it's queued again and the worker continues."""
        t = await board.find(ticket_ref)
        if not t:
            return f"No ticket {ticket_ref}"
        await board.append_log(t.page_id, [f"Sanjar answered: {answer}"])
        await board.update(t, jarvis="Queued", note=f"Sanjar's answer: {answer}")
        return f"{t.ref} is queued again with the answer"

    @server.tool()
    async def worker_progress(project: str, ticket: str = "") -> str:
        """How a Claude Code worker is doing, read from its live log: running or finished, how long,
        its last steps, the command it's waiting on and that command's latest output (e.g. a slow
        build). ticket: a ref for an older run; empty = the project's current or latest run."""
        return hooks.worker_progress(project, ticket)

    @server.tool()
    async def screenshot(url: str = "") -> str:
        """Send Sanjar a screenshot on Telegram: of a web page if url is given, else of the Mac screen."""
        try:
            path = await take_screenshot(url)
        except Exception as e:
            log.warning("screenshot failed: %s", e)
            return f"Screenshot failed: {e}"
        await hooks.send_photo(path, url or "Mac screen")
        return "Sent to Telegram"

    @server.tool()
    async def call_contact(contact: str, message: str) -> str:
        """Phone someone on Telegram and give them a message from Sanjar by voice; Sanjar gets the
        call's transcript afterwards. contact: their name as saved in contacts, or @username.
        message: what to tell them, in Sanjar's words. Returns the matches if it's not clear who."""
        return await hooks.call_contact(contact, message)

    return server


async def serve() -> None:
    """Run the tools servers until cancelled."""
    servers = []
    for port, source in ((TOOLS_PORT, "Call"), (TOOLS_PORT + 1, "Telegram"), (TOOLS_PORT + 2, "Guest")):
        app = make_server(source).streamable_http_app(stateless_http=True, json_response=True)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        # uvicorn grabs SIGTERM/SIGINT while serving and only re-raises them after it stops; with
        # several servers the signal got lost and `pkill` left Jarvis running. Leave signals alone.
        server.capture_signals = contextlib.nullcontext
        servers.append(server.serve())
    log.info("tools servers on ports %s-%s", TOOLS_PORT, TOOLS_PORT + 2)
    await asyncio.gather(*servers)
