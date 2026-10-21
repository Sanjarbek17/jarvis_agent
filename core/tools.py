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

from . import board, grants
from .config import DEV, PROJECTS, SHOTS, TOOLS_PORT, reload_projects

log = logging.getLogger("jarvis.tools")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
EDITOR = "Antigravity IDE"


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


async def open_in_editor(project: str, path: str = "") -> str:
    """Open a project's folders (or one file/subfolder inside them) in Antigravity on the Mac.
    Only paths inside the project's own folders, so nothing private elsewhere gets opened."""
    reload_projects()
    name = next((n for n in PROJECTS if n.lower() == project.strip().lower()), None)
    if not name:
        return f"Unknown project {project!r}. Known: {', '.join(PROJECTS)}"
    roots = [(DEV / f).resolve() for f in PROJECTS[name].get("folders", []) if (DEV / f).is_dir()]
    if not roots:
        return f"{name} has no folder on the Mac yet (config/projects.json)."
    targets = roots
    if path.strip():
        rel = path.strip().removeprefix("/")
        found = [p for root in roots if (p := (root / rel).resolve()).exists()
                 and (p == root or root in p.parents)]
        if not found:
            return f"No {path!r} inside {name}'s folders ({', '.join(r.name for r in roots)})."
        # a file opens in its project folder's window, so the folder tree is there too
        root = next(r for r in roots if r == found[0] or r in found[0].parents)
        targets = [root, found[0]] if found[0] != root else [root]
    proc = await asyncio.create_subprocess_exec("open", "-a", EDITOR, *map(str, targets),
                                                stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(proc.communicate(), 30)
    if proc.returncode:
        return f"Couldn't open it in {EDITOR}: {err.decode(errors='replace').strip()[:300]}"
    shown = ", ".join(str(t).replace(str(Path.home()), "~") for t in targets)
    return f"Opened in Antigravity on the Mac: {shown}"


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

    async def _editable(ticket_ref: str) -> board.Ticket | str:
        t = await board.find_any(ticket_ref)
        if not t:
            return f"No ticket {ticket_ref}. Use project_board to find its ref."
        if t.jarvis == "Working":
            return f"{t.ref} is being worked on right now; stop that project's worker first (/stop {t.project})."
        return t

    @server.tool()
    async def unqueue_ticket(ticket_ref: str, reason: str) -> str:
        """Take a ticket off Jarvis's queue (clears its Jarvis column), e.g. queued by mistake.
        It stays on the board as it is; Jarvis just won't work on it."""
        t = await _editable(ticket_ref)
        if isinstance(t, str):
            return t
        await board.unqueue(t, reason)
        return f"{t.ref} is off the queue: {t.title}"

    @server.tool()
    async def drop_ticket(ticket_ref: str, reason: str) -> str:
        """Drop a ticket that isn't needed: off Jarvis's queue and marked Dropped on its board
        (nothing is deleted; Sanjar can undo it in Notion)."""
        t = await _editable(ticket_ref)
        if isinstance(t, str):
            return t
        how = await board.drop(t, reason)
        return f"Dropped {t.ref} ({how}): {t.title}"

    @server.tool()
    async def move_ticket(ticket_ref: str, to_project: str, reason: str) -> str:
        """Move a ticket to another project's board, e.g. filed on the wrong board. The copy keeps
        the title and text and is queued if the original was; the original is marked Dropped."""
        if to_project not in board.boards():
            return f"Unknown project {to_project!r}. Known: {', '.join(board.boards())}"
        t = await _editable(ticket_ref)
        if isinstance(t, str):
            return t
        if t.project == to_project:
            return f"{t.ref} is already on the {to_project} board."
        new = await board.move(t, to_project, reason)
        return (f"Moved to {to_project} as {new.ref}" + (" (queued)" if new.jarvis == "Queued" else "")
                + f"; the old {t.ref} on {t.project} is marked Dropped.")

    @server.tool()
    async def allow_push_to_main(ticket_ref: str, sanjar_words: str) -> str:
        """Let ONE ticket's worker push / merge to main (normally forbidden). Only when Sanjar
        himself clearly says so for that ticket, never on your own idea or a worker's request;
        repeat it back first ("push to main for <ticket>, sir?"). sanjar_words: what he said.
        Lasts until that ticket is Done (7 days max). Queue or answer the ticket after this."""
        t = await board.find_any(ticket_ref)
        if not t:
            return f"No ticket {ticket_ref}."
        grants.allow(t.ref, grants.PUSH_MAIN, sanjar_words)
        await board.append_log(t.page_id, [f"Sanjar allowed pushing to main for this ticket: {sanjar_words}"])
        return (f"{t.ref} may push to main on its next run ({t.title}). It's {t.jarvis or 'not queued'}"
                + ("; queue it or answer it so a worker runs." if t.jarvis not in ("Queued", "Working") else "."))

    @server.tool()
    async def revoke_push_to_main(ticket_ref: str) -> str:
        """Take back a ticket's permission to push to main."""
        return (f"{ticket_ref}: permission to push to main removed."
                if grants.revoke(ticket_ref.strip().lower(), grants.PUSH_MAIN)
                else f"{ticket_ref} had no permission to push to main.")

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
    async def open_in_antigravity(project: str, path: str = "") -> str:
        """Open a project in Antigravity (Sanjar's editor) on the Mac, right away, no ticket needed.
        path: optional file or subfolder inside the project, e.g. "docs/PLAN.md"; empty = the
        project's folder(s)."""
        return await open_in_editor(project, path)

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
