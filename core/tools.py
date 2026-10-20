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
from .config import PROJECTS, SHOTS, TOOLS_PORT

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
    async def add_task(project: str, task: str, my_words: str, folder: str = "") -> str:
        """Put a new task on Sanjar's Notion board; a Claude Code worker will do it.

        project: one of the known project names. task: short title. my_words: what Sanjar
        actually said. folder: only if the work is in a specific folder inside ~/dev.
        """
        if project not in PROJECTS:
            return f"Unknown project {project!r}. Known: {', '.join(PROJECTS)}"
        card = await board.add_task(project, task, my_words, source, folder)
        return f"Added {card.id} to {project}: {task}"

    @server.tool()
    async def get_status(project: str = "") -> str:
        """Current tasks (To do, Doing, Needs me, and finished in the last day). Empty project = all."""
        return await board.status_text(project)

    @server.tool()
    async def project_board(project: str, include_done: bool = False) -> str:
        """Read a project's own older sprint board in Notion (planned tickets, not Jarvis tasks):
        counts per status and the open tickets by sprint. To get one of them done, use add_task."""
        db = PROJECTS.get(project, {}).get("board")
        if not db:
            return f"No sprint board known for {project!r}."
        return await board.project_board(db, include_done)

    @server.tool()
    async def answer_task(card_id: str, answer: str) -> str:
        """Give Sanjar's answer to a card waiting in "Needs me" (e.g. JT-4); the worker continues."""
        card = await board.find(card_id)
        if not card:
            return f"No card {card_id}"
        await board.update(card.page_id, status="To do",
                           my_words=f"{card.my_words}\n\nSanjar's answer: {answer}")
        await board.append_log(card.page_id, [f"Sanjar answered: {answer}"])
        return f"{card.id} is back in To do with the answer"

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
