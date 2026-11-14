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
from mcp.server.mcpserver import Context, MCPServer

from . import board, coremem, grants, memory, origin
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
    mac_late: Callable[..., Awaitable[None]]   # result after MAC_WAIT
    guard: object = None                       # core/guard.py's Guard
    expenses: Callable[[], Awaitable[str]] = None   # the Flatmates expense topic, read with Jarvis's own Telegram
    post_expenses: Callable[[], Awaitable[str]] = None   # ...and posted there
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


async def show_photos(photos: list[Path], label: str, ctx=None) -> str:
    """Images for Sanjar go where he asked (ticket 546d965e): into the app chat if the call came from the
    Jarvis app, else to Telegram. Returns where they went, for the tool's answer."""
    o = origin.from_ctx(ctx)
    if not photos:
        return ""
    if o.get("channel") == "app":
        from . import appserver
        if appserver.add_images(o["chat"], photos, label):
            return "shown in this app chat"
    await hooks.send_photos(photos, label)
    return "sent to Telegram"


def _followed(o: dict, project: str) -> str:
    """A General app chat about one project moves into it when he files a ticket there (ticket f4f11c38)."""
    if o.get("channel") != "app":
        return ""
    from . import appserver
    moved = appserver.follow_ticket(o["chat"], project)
    return f" This chat is about {moved} now, so it moved under {moved} in the app." if moved else ""


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
    async def remember(fact: str, kind: str = "fact", key: str = "") -> str:
        """Save something to Jarvis's CORE memory, which is in every later chat and call: one clear
        self-contained sentence. kind: "decision" (he decided), "preference" (how he wants things,
        rules for you), "fact", or "open" (something he asked for that isn't done yet). key: a short
        kebab-case name; reuse an existing key from CORE MEMORY to replace that line (when he changes
        his mind, or an open request is now done with new info)."""
        if not fact.strip():
            return "Nothing to remember."
        k = coremem.put(fact, kind, key)
        await memory.add(fact, "fact", "brain")
        return f"Remembered in core memory as {k} ({kind})."

    @server.tool()
    async def forget_core(key: str) -> str:
        """Remove one line from CORE memory by its key: wrong, outdated, or an open request that's done."""
        return "Removed from core memory." if coremem.drop(key) else f"No core memory line {key}."

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
    async def add_task(project: str, task: str, my_words: str, ctx: Context = None) -> str:
        """Put a NEW ticket on the project's own board, queued for Jarvis; a Claude Code
        worker will do it. project: a known project name. task: short title. my_words: what
        Sanjar actually said. For a ticket that already exists on the board, use queue_ticket."""
        if project not in board.boards():
            return f"Unknown project {project!r}. Known: {', '.join(board.boards())}"
        o = origin.from_ctx(ctx)
        t = await board.add_ticket(project, task, my_words, origin.label(o, source))
        origin.set(t.ref, o)              # its result goes back where he asked (the app chat or Telegram)
        return f"Added ticket {t.ref} to the {project} board and queued it: {task}" + _followed(o, project)

    @server.tool()
    async def queue_ticket(project: str, ticket: str, ctx: Context = None) -> str:
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
        o = origin.from_ctx(ctx)
        await board.update(t, jarvis="Queued", note=f"queued from {origin.label(o, source)}")
        origin.set(t.ref, o)
        return f"Queued {t.ref}: {t.title}" + _followed(o, project)

    @server.tool()
    async def move_chat(project: str, when_created: bool = False, ctx: Context = None) -> str:
        """Move the Jarvis app or website chat Sanjar is talking in to another project ("General" too).
        Its messages and this conversation stay. when_created=True: the project doesn't exist yet and
        you have filed the ticket that sets it up; the chat moves by itself once a project with exactly
        this name is on his list."""
        from . import appserver
        o = origin.from_ctx(ctx)
        if o.get("channel") != "app":
            return "Only chats in the Jarvis app or website can be moved, and this isn't one."
        chat = appserver.move_chat(o["chat"], project, when_created)
        if not chat:
            return f"Couldn't move this chat to {project!r}: no such project, or this chat can't be moved."
        if chat.get("move_to"):
            return f"This chat will move to {chat['move_to']} as soon as that project exists."
        return f"Moved this chat to {chat['project']}."

    def asks_line() -> str:
        from . import asks
        lines = asks.status_lines()
        return f"\n\n{lines}" if lines else ""

    @server.tool()
    async def get_status(project: str = "") -> str:
        """Jarvis's work: tickets Queued, Working, Needs me, and finished in the last day. Empty = all projects."""
        text = await board.status_text(project)
        if hooks.runner.wait_reason:          # e.g. a ticket held back because RAM is short
            text += f"\n\nRunner: {hooks.runner.wait_reason}"
        from . import usagewatch
        if line := usagewatch.status_line():
            text += f"\n{line}"
        return text + asks_line()

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
        (nothing is deleted; Sanjar can undo it on the board)."""
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
    async def answer_task(ticket_ref: str, answer: str, ctx: Context = None) -> str:
        """Give Sanjar's answer to a ticket waiting in "Needs me"; it's queued again and the worker continues."""
        t = await board.find(ticket_ref)
        if not t:
            return f"No ticket {ticket_ref}"
        await board.append_log(t.page_id, [f"Sanjar answered: {answer}"])
        await board.update(t, jarvis="Queued", note=f"Sanjar's answer: {answer}")
        origin.set(t.ref, origin.from_ctx(ctx))
        return f"{t.ref} is queued again with the answer"

    @server.tool()
    async def live_stats(project: str) -> str:
        """Instagram numbers read RIGHT NOW (followers, views of the newest reels) for "Haydovchi darslari" or
        "Instagram Channels", each labelled with the time it was read. Use it for every status / views / followers
        question about them; never answer those from saved check-*.json files or memory (Sanjar, ticket 0960b065).
        Takes up to a minute or two the first time; a reading is reused for 15 min."""
        from . import livestats
        if project not in livestats.PROJECTS:
            return f"No live stats for {project!r}; they exist for: {', '.join(livestats.PROJECTS)}"
        return livestats.text(await livestats.fetch(project))

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
    async def screenshot(url: str = "", ctx: Context = None) -> str:
        """Show Sanjar a screenshot (in the chat he's talking in: the Jarvis app or Telegram): of a web
        page if url is given, else of the Mac screen."""
        try:
            path = await take_screenshot(url)
        except Exception as e:
            log.warning("screenshot failed: %s", e)
            return f"Screenshot failed: {e}"
        return "Screenshot " + await show_photos([path], url or "Mac screen", ctx)

    @server.tool()
    async def call_contact(contact: str, message: str) -> str:
        """Phone someone on Telegram and give them a message from Sanjar by voice; Sanjar gets the
        call's transcript afterwards. contact: their name as saved in contacts, or @username.
        message: what to tell them, in Sanjar's words. Returns the matches if it's not clear who."""
        return await hooks.call_contact(contact, message)

    @server.tool()
    async def lock_mac() -> str:
        """Lock the Mac for real: the login window comes up and only Sanjar's password opens it (not display
        sleep, which may not ask for a password). Use this whenever he says lock the Mac / the screen."""
        from . import guard
        return "Locked the Mac." if await asyncio.to_thread(guard.lock_screen) else "Couldn't lock the Mac."

    @server.tool()
    async def run_on_mac(script: str, language: str = "shell") -> str:
        """Run ONE quick shell (zsh) or AppleScript snippet on the Mac right now and get its output:
        instant things like volume, open/quit an app, play/pause music, battery,
        what's running. language: "shell" or "applescript". Anything with several steps, the
        screen, or unknown apps -> mac_task. Same safety rules as mac_task."""
        log.info("run_on_mac (%s): %s", language, script[:200])
        return await run_quick(script, language)

    @server.tool()
    async def mac_task(task: str, sanjar_words: str, ctx: Context = None) -> str:
        """Have the Mac worker do something on the Mac itself: use any app, change settings, find or
        move files, fill something in, click and type on the screen. It remembers earlier Mac
        tasks ("now close it"). task: what to do, clearly. sanjar_words: what he actually said.
        Waits a little for the result; a longer task finishes in the background and its
        result comes to Sanjar where he asked (the app chat or Telegram), and to you as an [UPDATE]."""
        busy = hooks.mac.current
        job = asyncio.create_task(hooks.mac.run(task, sanjar_words, origin.from_ctx(ctx).get("chat", "")))
        done, _ = await asyncio.wait({job}, timeout=MAC_WAIT[source])
        if job not in done:
            app_chat = origin.from_ctx(ctx).get("chat", "")
            job.add_done_callback(lambda j: asyncio.create_task(hooks.mac_late(task, *j.result(), app_chat=app_chat)))
            return ("Still working on it" + (f" (after the task before it: {busy})" if busy else "")
                    + "; the result goes to Sanjar when it's done, where he asked.")
        text, photos, question = job.result()
        where = await show_photos(photos[:10], "Mac", ctx)
        return (f"The Mac worker needs Sanjar's decision: {text}" if question else text) + \
               (f" ({len(photos)} screenshot(s) {where})" if photos else "")

    @server.tool()
    async def mac_guard(action: str) -> str:
        """The Mac guard: when Sanjar says "guard" (he's leaving the Mac), arm it; while it's on, anyone
        touching the Mac gets photographed and he's alerted on Telegram. It never turns on by itself.
        action: "on", "off", "status", or "test" (take a camera photo now and send it)."""
        action = action.strip().lower()
        if action == "on":
            return hooks.guard.arm()
        if action == "off":
            return hooks.guard.disarm()
        if action == "test":
            return await hooks.guard.test()
        return hooks.guard.status()

    @server.tool()
    async def google_drive(task: str, sanjar_words: str, share: bool = False) -> str:
        """Read, search, create or edit Sanjar's Google Drive files, Google Docs, Sheets and Slides (his
        sanjarsaidov173@gmail.com Drive) through a helper that has the Drive tools. task: what to do,
        clearly (which file, what to read or change). sanjar_words: what he actually said. share=True
        only if he asked to share a file or change who can see it. Never deletes. Takes ~10-60 s."""
        from . import drive
        log.info("google_drive: %s", task[:200])
        return await drive.run(task, sanjar_words, share)

    @server.tool()
    async def google_slides(title: str, slides: list[dict], subtitle: str = "") -> str:
        """Make a NEW picture-heavy Google Slides deck in his Drive (sanjarsaidov173@gmail.com), no sign-in needed.
        slides: one per step/point, in order: {"icon": "one emoji as the picture", "title": "short heading",
        "short": "1-2 words for the overview", "lines": ["up to 4 short lines"]}. Adds a title slide with every
        icon and a recap slide with the icons joined by arrows. Takes 1-5 minutes; returns the link."""
        from . import drive
        log.info("google_slides: %s (%d slides)", title, len(slides))
        return await drive.upload_slides({"title": title, "subtitle": subtitle, "slides": slides[:12]})

    async def _google(fn, *args) -> str:
        from . import google
        try:
            return await fn(*args)
        except google.GoogleError as e:
            return str(e)
        except Exception as e:
            log.exception("google tool failed")
            return f"Google failed: {e}"

    @server.tool()
    async def google_sign_in(account: str = "") -> str:
        """A link that signs one of Sanjar's Google accounts in for Jarvis's own Google tools (editing inside
        Docs and Slides, any of his accounts). account: its email, to pre-fill. Send him the link and what the
        answer says."""
        from . import google
        try:
            link = google.sign_in_link(account)
        except google.GoogleError as e:
            return str(e)
        return (f"Sign-in link (works for 30 min): {link}\n\nTell him: open it, pick the account, press Allow (if "
                "Google warns the app isn't verified: Advanced, then go on). On the Mac it finishes by itself. On his "
                "phone the last page won't load: he copies that page's address (it starts with http://127.0.0.1:8791) "
                "and sends it to you; then call google_sign_in_finish with it. One account per link.")

    @server.tool()
    async def google_sign_in_finish(address: str) -> str:
        """Finish a Google sign-in with the address of the page Google sent him to (http://127.0.0.1:8791/?code=...)."""
        from . import google
        return await _google(google.finish, address)

    @server.tool()
    async def google_files(account: str = "", search: str = "") -> str:
        """Files in one of his signed-in Google accounts, newest first. account: email (or part); empty if only
        one is signed in. search: words in the name or text. Also lists which accounts are signed in."""
        from . import google

        async def go():
            email = google.which(account)
            files = await google.list_files(email, search)
            rows = [f"- {f['name']} ({google.MIME.get(f['mimeType'], f['mimeType'].split('.')[-1])}, "
                    f"{f['modifiedTime'][:10]}) {f.get('webViewLink', '')}" for f in files]
            return f"{email}:\n" + ("\n".join(rows) or "nothing found")
        if not google.accounts():
            return "No Google account is signed in for Jarvis's own tools yet: use google_sign_in."
        return await _google(go)

    @server.tool()
    async def google_file_text(account: str, file: str) -> str:
        """The text of a Google Doc, Sheet (as CSV) or Slides deck in a signed-in account. file: link or id."""
        from . import google
        return await _google(lambda: google.read_text(google.which(account), google.file_id(file)))

    @server.tool()
    async def google_replace_text(account: str, file: str, find: str, replace: str) -> str:
        """Edit inside a Google Doc or Slides deck: replace every `find` (exact, case-sensitive) with `replace`.
        Read the file first (google_file_text) so `find` matches exactly. Say back a big change and wait for his yes."""
        from . import google
        log.info("google_replace_text in %s: %r -> %r", file, find[:60], replace[:60])
        return await _google(lambda: google.replace_text(google.which(account), google.file_id(file), find, replace))

    @server.tool()
    async def google_add_text(account: str, file: str, text: str) -> str:
        """Add text at the end of a Google Doc in a signed-in account. file: link or id."""
        from . import google
        log.info("google_add_text to %s", file)
        return await _google(lambda: google.add_text(google.which(account), google.file_id(file), text))

    @server.tool()
    async def google_create_slides(account: str, title: str, slides: list[dict], subtitle: str = "") -> str:
        """Make a NEW Google Slides deck with content in a signed-in account: a title slide, then one slide
        per item {"title": "...", "body": "line 1\\nline 2", "image_url": "https://... (optional, public)"}.
        Keep slides short: a heading and 2-4 short lines."""
        from . import google
        return await _google(lambda: google.create_slides(google.which(account), title, slides, subtitle))

    @server.tool()
    async def google_add_slide(account: str, file: str, title: str, body: str = "", image_url: str = "") -> str:
        """Add one slide (heading, body lines, optional public picture link) at the end of a Slides deck."""
        from . import google
        return await _google(lambda: google.add_slide(google.which(account), google.file_id(file), title, body, image_url))

    @server.tool()
    async def google_create_doc(account: str, title: str, sections: list[dict]) -> str:
        """Make a NEW Google Doc with content in a signed-in account: sections [{"heading": "...", "text": "..."}]."""
        from . import google
        return await _google(lambda: google.create_doc(google.which(account), title, sections))

    @server.tool()
    async def flatmates_expenses() -> str:
        """Who spent how much: reads the expense topic of the Flatmates group as it is now and adds it up per person,
        with each person's lines and the ones it couldn't count. This is for Sanjar; it posts nothing."""
        try:
            return await hooks.expenses()
        except Exception as e:
            log.exception("expenses failed")
            return f"Couldn't read the expense topic: {e}"

    @server.tool()
    async def flatmates_post_expenses() -> str:
        """Post the expense report in the Flatmates group's expense topic: a short summary split between the five, who
        pays and who gets, with all the data as a file. ONLY when Sanjar himself asks for it to go to the group: everyone
        there sees it and it's about their money. Returns what was posted."""
        try:
            return await hooks.post_expenses()
        except Exception as e:
            log.exception("posting expenses failed")
            return f"Couldn't post the expense report: {e}"

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
