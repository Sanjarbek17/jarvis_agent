"""Jarvis tools for the Claude brain, served as MCP over local HTTP from inside jarvis.py.

Four servers: the call brain (has hang_up), the Telegram brain, the guest brain for calls
Jarvis places to other people (only hang_up, so the person on the line can't use the others),
and the group brain (Telegram groups: only the read-only Taxi App board status).
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

from . import board, grants, memory
from .config import DEV, PROJECTS, SHOTS, TOOLS_PORT, reload_projects
from .mac import MacWorker, run_quick

log = logging.getLogger("jarvis.tools")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
EDITOR = "Antigravity IDE"
GROUP_PROJECT = "Taxi App"   # the one board people in groups may ask about
MAC_WAIT = {"Call": 25, "Telegram": 60}   # seconds mac_task waits for a result, then goes on in the background


class Hooks:
    """Filled in by jarvis.py so tools can reach Telegram and the call."""
    send_photo: Callable[[Path, str], Awaitable[None]]
    send_photos: Callable[[list[Path], str], Awaitable[None]]
    hang_up: Callable[[], Awaitable[None]]
    call_contact: Callable[[str, str], Awaitable[str]]
    worker_progress: Callable[[str, str], str]
    mac: MacWorker
    mac_late: Callable[[str, str, list[Path], bool], Awaitable[None]]   # result after MAC_WAIT
    runner: "Runner"                                                    # core.runner.Runner


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

    if source == "Group":
        @server.tool()
        async def taxi_board() -> str:
            """The Taxi App board, read-only: what Jarvis is doing on it, and its open tickets by sprint."""
            return (f"Jarvis's work:\n{await board.status_text(GROUP_PROJECT)}\n\n"
                    f"Board:\n{await board.project_board(GROUP_PROJECT)}")
        return server

    @server.tool()
    async def remember(fact: str) -> str:
        """Save something to Jarvis's long-term memory so it's known in every later chat and call:
        a preference, decision, plan, name, fact about him or a project. One clear self-contained
        sentence (who/what/when), e.g. "Sanjar wants the taxi app work on the dev branch"."""
        item = await memory.add(fact, "fact", "brain")
        return f"Remembered (id {item})." if item else "Nothing to remember."

    @server.tool()
    async def recall(query: str) -> str:
        """Search long-term memory (saved facts, past chats and calls, from every session) for
        anything about `query`. Returns whole linked threads (everything said on that topic, in order),
        not single lines. Use it before answering about the past instead of guessing."""
        return await memory.recall_text(query) or "Nothing in memory about that."

    @server.tool()
    async def forget_memory(item_id: str) -> str:
        """Delete one memory item (by the id shown in recall / memory notes), e.g. a wrong or outdated fact."""
        return "Forgotten." if memory.forget(item_id.strip()) else f"No memory item {item_id}."

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
        text = await board.status_text(project)
        if hooks.runner.wait_reason:          # e.g. a ticket held back because RAM is short
            text += f"\n\nRunner: {hooks.runner.wait_reason}"
        return text

    @server.tool()
    async def project_board(project: str, sprint: str = "", status: str = "") -> str:
        """A project's board with counts per status and per sprint, then the tickets (with refs).
        Filter when a board is big: sprint = an exact sprint like "S5"; status = "open" (default),
        "done", "all", or a specific status name. Says if the list is truncated."""
        if project not in board.boards():
            return f"No board known for {project!r}."
        return await board.project_board(project, sprint, status)

    async def _editable(ticket_ref: str, stop: str = "") -> board.Ticket | str:
        """The ticket, if it may be changed. stop: a reason to stop its running worker first
        (drop / unqueue); without it a ticket being worked on is left alone."""
        t = await board.find_any(ticket_ref)
        if not t:
            return f"No ticket {ticket_ref}. Use project_board to find its ref."
        if t.jarvis == "Working":
            running = hooks.runner.current.get(t.project)
            if running and running.ref == t.ref:
                if not stop:
                    return f"{t.ref} is being worked on right now; stop it first with stop_worker."
                await hooks.runner.stop_and_wait(t.project, stop)
                t = await board.find_any(ticket_ref) or t
        return t

    @server.tool()
    async def stop_worker(project: str, reason: str = "") -> str:
        """Stop the worker running on a project's board right now (same as Sanjar's /stop command),
        e.g. when he says "stop it" about a ticket in progress. project: the project name, or the
        ref of the ticket it's working on. Its ticket goes off the queue; nothing else changes.
        reason: his words."""
        name = next((n for n in hooks.runner.current if n.lower() == project.strip().lower()), None)
        if not name:
            name = next((p for p, c in hooks.runner.current.items() if c.ref == project.strip()), None)
        if not name:
            busy = ", ".join(f"{p} ({c.ref})" for p, c in hooks.runner.current.items()) or "none"
            return f"No worker is running for {project!r}. Running now: {busy}."
        card = await hooks.runner.stop_and_wait(name, reason or "Sanjar asked to stop it")
        if not card:
            return f"The {name} worker had already finished."
        return f"Stopped the {name} worker on {card.ref} ({card.title}); that ticket is off the queue."

    @server.tool()
    async def unqueue_ticket(ticket_ref: str, reason: str) -> str:
        """Take a ticket off Jarvis's queue (clears its Jarvis column), e.g. queued by mistake.
        It stays on the board as it is; Jarvis just won't work on it."""
        t = await _editable(ticket_ref, stop=f"unqueued: {reason}")
        if isinstance(t, str):
            return t
        await board.unqueue(t, reason)
        return f"{t.ref} is off the queue: {t.title}"

    @server.tool()
    async def drop_ticket(ticket_ref: str, reason: str) -> str:
        """Drop a ticket that isn't needed: off Jarvis's queue and marked Dropped on its board
        (nothing is deleted; Sanjar can undo it in Notion)."""
        t = await _editable(ticket_ref, stop=f"dropped: {reason}")
        if isinstance(t, str):
            return t
        how = await board.drop(t, reason)
        return f"Dropped {t.ref} ({how}): {t.title}"

    @server.tool()
    async def mark_done(ticket_ref: str, reason: str = "") -> str:
        """Mark a ticket Done on its board (Jarvis column + the board's Status both set to Done),
        e.g. one Sanjar tested himself and confirmed works. Only on his clear say-so, not your own
        call. reason: his words or why it's done."""
        t = await _editable(ticket_ref)
        if isinstance(t, str):
            return t
        how = await board.mark_done(t, reason or "Sanjar confirmed it works")
        return f"Marked {t.ref} Done ({how}): {t.title}"

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

    @server.tool()
    async def run_on_mac(script: str, language: str = "shell") -> str:
        """Run ONE quick shell (zsh) or AppleScript snippet on the Mac right now and get its output:
        instant things like volume, open/quit an app, play/pause music, lock the screen, battery,
        what's running. language: "shell" or "applescript". Anything with several steps, the
        screen, or unknown apps -> mac_task. Same safety rules as mac_task."""
        log.info("run_on_mac (%s): %s", language, script[:200])
        return await run_quick(script, language)

    @server.tool()
    async def mac_task(task: str, sanjar_words: str) -> str:
        """Have the Mac worker do something on the Mac itself: use any app, change settings, find or
        move files, fill something in, click and type on the screen. It remembers earlier Mac
        tasks ("now close it"). task: what to do, clearly. sanjar_words: what he actually said.
        Waits a little for the result; a longer task finishes in the background and its
        result comes to Sanjar in Telegram (and to you as an [UPDATE])."""
        busy = hooks.mac.current
        job = asyncio.create_task(hooks.mac.run(task, sanjar_words))
        done, _ = await asyncio.wait({job}, timeout=MAC_WAIT[source])
        if job not in done:
            job.add_done_callback(lambda j: asyncio.create_task(hooks.mac_late(task, *j.result())))
            return ("Still working on it" + (f" (after the task before it: {busy})" if busy else "")
                    + "; the result goes to Sanjar in Telegram when it's done.")
        text, photos, question = job.result()
        await hooks.send_photos(photos[:10], "Mac")
        return (f"The Mac worker needs Sanjar's decision: {text}" if question else text) + \
               (f" ({len(photos)} screenshot(s) sent to Telegram)" if photos else "")

    @server.tool()
    async def mac_progress() -> str:
        """What the Mac worker is doing now, or did last: its task, last steps and result."""
        return hooks.mac.progress()

    @server.tool()
    async def stop_mac_task(forget: bool = False) -> str:
        """Stop the Mac task that's running. forget=True also clears its memory of earlier Mac tasks."""
        stopped = hooks.mac.stop()
        if forget:
            hooks.mac.forget()
        return ("Stopped it." if stopped else "No Mac task was running.") + (" Memory cleared." if forget else "")

    return server


async def serve() -> None:
    """Run the tools servers until cancelled."""
    servers = []
    for port, source in ((TOOLS_PORT, "Call"), (TOOLS_PORT + 1, "Telegram"), (TOOLS_PORT + 2, "Guest"),
                         (TOOLS_PORT + 3, "Group")):
        app = make_server(source).streamable_http_app(stateless_http=True, json_response=True)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        # uvicorn grabs SIGTERM/SIGINT while serving and only re-raises them after it stops; with
        # several servers the signal got lost and `pkill` left Jarvis running. Leave signals alone.
        server.capture_signals = contextlib.nullcontext
        servers.append(server.serve())
    log.info("tools servers on ports %s-%s", TOOLS_PORT, TOOLS_PORT + 3)
    await asyncio.gather(*servers)
