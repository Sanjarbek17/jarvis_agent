"""Jarvis tools for the Claude brain, served as MCP over local HTTP from inside jarvis.py.

Two servers: one for the call brain (has hang_up) and one for the Telegram brain.
The tools hold the Notion token and the Telegram session; Claude only sees results.
"""
import asyncio
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


hooks = Hooks()


async def take_screenshot(url: str = "") -> Path:
    path = SHOTS / f"shot_{int(time.time())}.png"
    if url:
        cmd = [CHROME, "--headless=new", "--hide-scrollbars", "--window-size=1440,900",
               f"--screenshot={path}", url]
    else:
        cmd = ["screencapture", "-x", str(path)]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)
    await asyncio.wait_for(proc.wait(), 60)
    if not path.exists():
        raise RuntimeError("screenshot failed")
    return path


def make_server(source: str) -> MCPServer:
    server = MCPServer("jarvis")

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
    async def screenshot(url: str = "") -> str:
        """Send Sanjar a screenshot on Telegram: of a web page if url is given, else of the Mac screen."""
        path = await take_screenshot(url)
        await hooks.send_photo(path, url or "Mac screen")
        return "Sent to Telegram"

    if source == "Call":
        @server.tool()
        async def hang_up() -> str:
            """End the phone call (after saying bye)."""
            asyncio.create_task(hooks.hang_up())
            return "Hanging up"

    return server


async def serve() -> None:
    """Run both tools servers until cancelled."""
    servers = []
    for port, source in ((TOOLS_PORT, "Call"), (TOOLS_PORT + 1, "Telegram")):
        app = make_server(source).streamable_http_app(stateless_http=True, json_response=True)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        servers.append(uvicorn.Server(config).serve())
    log.info("tools servers on ports %s and %s", TOOLS_PORT, TOOLS_PORT + 1)
    await asyncio.gather(*servers)
