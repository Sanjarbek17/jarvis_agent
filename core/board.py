"""Each project's own Notion sprint board, with a "Jarvis" column added.

Jarvis column: Queued -> Working -> Needs me / Done / Failed. The runner only takes tickets
marked Queued; tickets with an empty Jarvis column are left alone. "Jarvis note" holds the
last step or the result. The board's own Status column is moved to in-progress / done too.
Projects with "github" instead of "board" in projects.json keep their tickets as GitHub Issues
(core/ghboard.py), and projects with "local": true keep them on the Mac (core/localboard.py); the
functions here hand those over, so callers don't need to care.
"""
import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from . import ghboard, localboard
from .config import NOTION_TOKEN, PROJECTS, reload_projects

API = "https://api.notion.com/v1"
HEADERS = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28"}
ACTIVE = ("Queued", "Working", "Needs me")
FINISHED = {"done", "shipped", "complete", "completed", "cancelled", "canceled", "dropped",
            "won't do", "archived"}
# how each board names its stages, best match first
STAGE_NAMES = {
    "todo": ("to do", "todo", "not started", "backend todo", "frontend todo", "backlog"),
    "doing": ("in progress", "doing"),
    "done": ("done",),
}

CACHE_SECONDS = 60     # one full read per Notion board per minute: filtered reads of every board on
                       # every 10 s tick came close to Notion's rate limit
log = logging.getLogger("jarvis.board")
_http = httpx.AsyncClient(base_url=API, headers=HEADERS, timeout=30)
_schemas: dict[str, dict] = {}       # db id -> {"title": prop name, "status": (type, [options])}
_cache: dict[str, tuple[float, list]] = {}    # project -> (when read, every ticket, oldest first)
_writes: dict[str, int] = {}                  # project -> writes so far: spots a write during a read
_read_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_unreadable: dict[str, float] = {}    # project -> last time its board failed (for quiet logging)


@dataclass
class Ticket:
    page_id: str
    ref: str            # short id Jarvis and I use: last 8 chars of the page id (the start is shared)
    project: str
    title: str
    status: str         # the board's own Status
    jarvis: str         # Jarvis column
    note: str
    sprint: str
    url: str
    edited: str = ""    # last edited time (ISO), for spotting tickets stuck In Progress
    due: str = ""       # the board's "Due" date (ISO), if set: such tickets go ahead in the queue
    start_after: str = ""   # "YYYY-MM-DD HH:MM" local time: not started before then (follow-up checks, ticket ac668ebe)

    def startable(self, now: float | None = None) -> bool:
        return startable(self, now)

    def line(self) -> str:
        jarvis = f"Jarvis {self.jarvis}" if self.jarvis else "not for Jarvis"
        if self.jarvis == "Needs me" and (age := asked_ago(self.edited)):
            jarvis += f" (asked {age} ago)"
        return (f"{self.ref} [{self.project}] {self.title} — {self.status or '?'}, {jarvis}"
                + (f": {self.note[:200]}" if self.note else ""))


def asked_ago(edited: str, now: float | None = None) -> str:
    """ "40 min" / "14 h" / "2 days" since an ISO time; "" if unknown."""
    try:
        d = datetime.fromisoformat(edited.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    s = max(0.0, (time.time() if now is None else now) - d.timestamp())
    return f"{s / 60:.0f} min" if s < 3600 else f"{s / 3600:.0f} h" if s < 2 * 86400 else f"{s / 86400:.0f} days"


def startable(t, now: float | None = None) -> bool:
    """False while a ticket's start_after time is still ahead (none, or a bad value, never blocks it)."""
    when = (getattr(t, "start_after", "") or "").strip()
    if not when:
        return True
    try:
        at = time.mktime(time.strptime(when[:16], "%Y-%m-%d %H:%M"))
    except ValueError:
        return True
    return (time.time() if now is None else now) >= at


def boards() -> dict[str, str]:
    """Every project with a board: Notion database id, "github:<repo>" for GitHub Issues ones, or
    "local" for the ones kept on the Mac."""
    reload_projects()
    return {name: "local" if p.get("local") else p["board"] if p.get("board") else f"github:{p['github']['repo']}"
            for name, p in PROJECTS.items() if p.get("local") or p.get("board") or p.get("github")}


def _matches(t: "Ticket", f: dict | None) -> bool:
    """The Notion filters this module uses, applied to a ticket we already have."""
    if not f:
        return True
    if "or" in f:
        return any(_matches(t, x) for x in f["or"])
    if "and" in f:
        return all(_matches(t, x) for x in f["and"])
    if f.get("property") == "Jarvis":
        sel = f.get("select", {})
        return t.jarvis == sel["equals"] if "equals" in sel else bool(t.jarvis) if sel.get("is_not_empty") else True
    if f.get("timestamp") == "last_edited_time":
        if ghboard.is_gh_page(t.page_id):
            return ghboard.recently_finished(t)
        after = datetime.fromisoformat(f["last_edited_time"]["after"])
        return bool(t.edited) and datetime.fromisoformat(t.edited) > after
    return True


def _text(prop) -> str:
    items = prop.get("title") or prop.get("rich_text") or []
    return "".join(i.get("plain_text", "") for i in items)


def _choice(prop) -> str:
    if not prop:
        return ""
    value = prop.get(prop["type"])
    return value.get("name", "") if isinstance(value, dict) else ""


def _rich(text: str) -> dict:
    return {"rich_text": [{"text": {"content": text[:1900]}}]} if text else {"rich_text": []}


async def _schema(db: str) -> dict:
    if db not in _schemas:
        r = await _http.get(f"/databases/{db}")
        r.raise_for_status()
        props = r.json()["properties"]
        status = props.get("Status", {})
        kind = status.get("type", "select")
        _schemas[db] = {
            "title": next(name for name, p in props.items() if p["type"] == "title"),
            "status": (kind, [o["name"] for o in (status.get(kind) or {}).get("options", [])]),
        }
    return _schemas[db]


async def _status_value(db: str, stage: str) -> dict | None:
    kind, options = (await _schema(db))["status"]
    lower = {o.lower(): o for o in options}
    for name in STAGE_NAMES[stage]:
        if name in lower:
            return {kind: {"name": lower[name]}}
    return None


def _ticket(project: str, page) -> Ticket:
    p = page["properties"]
    return Ticket(
        page_id=page["id"], ref=page["id"].replace("-", "")[-8:], project=project,
        title=next((_text(v) for v in p.values() if v["type"] == "title"), ""),
        status=_choice(p.get("Status")), jarvis=_choice(p.get("Jarvis")),
        note=_text(p["Jarvis note"]) if "Jarvis note" in p else "",
        sprint=_choice(p.get("Sprint")), url=page["url"], edited=page.get("last_edited_time", ""),
        due=((p.get("Due") or {}).get("date") or {}).get("start") or "",
    )


def _quietly(project: str, message: str) -> None:
    """Warn about a failing board once in a while, not on every check."""
    if time.monotonic() - _unreadable.get(project, 0) > 600:
        log.warning("board %s %s", project, message[:200])
        _unreadable[project] = time.monotonic()


async def _read_board(project: str) -> list[Ticket]:
    db, out, cursor = boards()[project], [], None
    while True:
        body = {"page_size": 100, "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
                **({"start_cursor": cursor} if cursor else {})}
        r = await _http.post(f"/databases/{db}/query", json=body)
        r.raise_for_status()
        data = r.json()
        out += [_ticket(project, page) for page in data["results"]]
        if not data.get("has_more"):
            return out
        cursor = data["next_cursor"]


async def _tickets(project: str, fresh: bool = False) -> list[Ticket]:
    """Every ticket on a Notion board, read at most once per CACHE_SECONDS. If Notion fails, the last
    good copy is served and asked for again after CACHE_SECONDS."""
    def recent(entry) -> bool:
        return bool(entry) and time.monotonic() - entry[0] < CACHE_SECONDS

    if not fresh and recent(_cache.get(project)):
        return _cache[project][1]
    async with _read_locks[project]:
        hit = _cache.get(project)
        if not fresh and recent(hit):        # someone else read it while we waited
            return hit[1]
        writes = _writes.get(project, 0)
        try:
            out = await _read_board(project)
        except Exception as e:
            if not hit:
                raise
            _quietly(project, f"unreadable, using the copy from {time.monotonic() - hit[0]:.0f} s ago: {e}")
            _cache[project] = (time.monotonic(), hit[1])
            return hit[1]
        # a write during the read may be missing from it, so read again next time
        _cache[project] = (time.monotonic() if _writes.get(project, 0) == writes else 0.0, out)
        return out


def _remember(project: str, page: dict) -> Ticket:
    """Put a page Notion just returned from a write into the cached board, so the runner never acts
    on the old state of a ticket it just changed."""
    new = _ticket(project, page)
    _writes[project] = _writes.get(project, 0) + 1
    if hit := _cache.get(project):
        rows = hit[1]
        for i, t in enumerate(rows):
            if t.page_id == new.page_id:
                rows[i] = new
                break
        else:
            rows.append(new)
    return new


async def _query(project: str, filter_: dict | None = None, fresh: bool = False) -> list[Ticket]:
    if localboard.is_local(project):
        rows = localboard.tickets(project)
    elif ghboard.is_gh(project):
        rows = await ghboard.tickets(project)
    else:
        rows = await _tickets(project, fresh)
    return [t for t in rows if _matches(t, filter_)]


def _jarvis_is(*states: str) -> dict:
    return {"or": [{"property": "Jarvis", "select": {"equals": s}} for s in states]}


async def all_tickets(project: str) -> list[Ticket]:
    """Every ticket on the board, whatever kind of board it is."""
    return await _query(project)


async def in_progress(project: str) -> list[Ticket]:
    """Tickets whose board Status is the in-progress stage (In Progress / Doing)."""
    return [t for t in await _query(project) if (t.status or "").lower() in STAGE_NAMES["doing"]]


async def _each_board(filter_: dict) -> list[Ticket]:
    """Across all boards; a board that can't be read (Notion/GitHub error, rate limit) is skipped,
    so one bad board never stops the others (it stalled the whole runner on 2026-11-02..04)."""
    out = []
    for project in boards():
        try:
            out += await _query(project, filter_)
        except Exception as e:
            _quietly(project, f"unreadable, skipped: {e}")
    return out


async def queued() -> list[Ticket]:
    """Tickets waiting for a worker, oldest first, across all boards."""
    return await _each_board(_jarvis_is("Queued"))


async def with_jarvis(state: str) -> list[Ticket]:
    return await _each_board(_jarvis_is(state))


async def add_ticket(project: str, title: str, my_words: str, source: str, queued: bool = True) -> Ticket:
    """A new ticket on the project's board, queued for Jarvis unless queued=False."""
    if localboard.is_local(project):
        return localboard.add_ticket(project, title, f"Sanjar's words ({source}): {my_words}", queued=queued,
                                     note=f"added from {source}")
    if ghboard.is_gh(project):
        return await ghboard.add_ticket(project, title, f"Sanjar's words ({source}): {my_words}", queued=queued)
    db = boards()[project]
    schema = await _schema(db)
    props = {schema["title"]: {"title": [{"text": {"content": title[:200]}}]},
             "Jarvis note": _rich(f"added from {source}")}
    if queued:
        props["Jarvis"] = {"select": {"name": "Queued"}}
    if todo := await _status_value(db, "todo"):
        props["Status"] = todo
    body = [{"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"text": {"content": f"Sanjar's words ({source}): {my_words}"[:1900]}}]}}]
    r = await _http.post("/pages", json={"parent": {"database_id": db}, "properties": props, "children": body})
    r.raise_for_status()
    return _remember(project, r.json())


async def find_open(project: str, query: str) -> list[Ticket]:
    """Open tickets on a board whose title contains the words (or that have this ref)."""
    words = query.lower().split()
    found = []
    for t in await _query(project):
        if t.ref == query.strip().lower() or (
                t.status.lower() not in FINISHED and all(w in t.title.lower() for w in words)):
            found.append(t)
    return found


async def find(ref: str) -> Ticket | None:
    """A ticket Jarvis is involved with, by its short ref."""
    if (t := localboard.get(ref)) and t.jarvis:
        return t
    for project in boards():
        for t in await _query(project, {"property": "Jarvis", "select": {"is_not_empty": True}}):
            if t.ref == ref.strip().lower():
                return t
    return None


async def find_any(ref: str) -> Ticket | None:
    """Any ticket on any board by its short ref, also ones Jarvis was never asked to do."""
    if t := localboard.get(ref) or await find(ref):
        return t
    for project in boards():             # not in our copy: maybe it was only just added on Notion
        for t in await _query(project, fresh=True):
            if t.ref == ref.strip().lower():
                return t
    return None


async def unqueue(t: Ticket, reason: str) -> None:
    """Clear the Jarvis column: the runner leaves the ticket alone from now on."""
    if localboard.is_local_page(t.page_id):
        localboard.clear_jarvis(t, reason)
        await append_log(t.page_id, [f"Taken off Jarvis's queue: {reason}"])
        return
    if ghboard.is_gh_page(t.page_id):
        await ghboard.clear_jarvis(t, reason)
        await append_log(t.page_id, [f"Taken off Jarvis's queue: {reason}"])
        return
    r = await _http.patch(f"/pages/{t.page_id}", json={"properties": {
        "Jarvis": {"select": None}, "Jarvis note": _rich(reason)}})
    r.raise_for_status()
    _remember(t.project, r.json())
    await append_log(t.page_id, [f"Taken off Jarvis's queue: {reason}"])


async def drop(t: Ticket, reason: str) -> str:
    """Unqueue and mark the ticket Dropped on its board. Select-type Status columns get a
    "Dropped" option (Notion adds it on first use); fixed status-type columns can't, so the
    title gets a [dropped] prefix instead. Returns how it was marked."""
    if localboard.is_local_page(t.page_id):
        return localboard.drop(t, reason)
    if ghboard.is_gh_page(t.page_id):
        return await ghboard.drop(t, reason)
    db = boards()[t.project]
    kind, options = (await _schema(db))["status"]
    props: dict = {"Jarvis": {"select": None}, "Jarvis note": _rich(f"dropped: {reason}")}
    existing = next((o for o in options if o.lower() in ("dropped", "cancelled", "canceled", "won't do")), None)
    if kind == "select" or existing:
        props["Status"] = {kind: {"name": existing or "Dropped"}}
        how = f"Status {existing or 'Dropped'}"
    else:
        title = (await _schema(db))["title"]
        props[title] = {"title": [{"text": {"content": f"[dropped] {t.title}"[:200]}}]}
        how = "title marked [dropped]"
    r = await _http.patch(f"/pages/{t.page_id}", json={"properties": props})
    r.raise_for_status()
    _remember(t.project, r.json())
    _schemas.pop(db, None)          # a new "Dropped" option may have been added
    await append_log(t.page_id, [f"Dropped: {reason}"])
    return how


async def mark_done(t: Ticket, reason: str) -> str:
    """Mark the ticket Done: the Jarvis column Done, and the board's own Status to its done stage
    (every board here has a "Done"). Returns how it was marked."""
    if localboard.is_local_page(t.page_id):
        await update(t, jarvis="Done", note=f"done: {reason}", stage="done")
        await append_log(t.page_id, [f"Marked Done: {reason}"])
        return "Status + Jarvis Done"
    if ghboard.is_gh_page(t.page_id):
        await update(t, jarvis="Done", note=f"done: {reason}", stage="done")
        await append_log(t.page_id, [f"Marked Done: {reason}"])
        return "Status Done, issue closed"
    done = await _status_value(boards()[t.project], "done")
    await update(t, jarvis="Done", note=f"done: {reason}", stage="done")
    await append_log(t.page_id, [f"Marked Done: {reason}"])
    return "Status + Jarvis Done" if done else "Jarvis Done (board has no Done status)"


async def body_lines(page_id: str) -> list[str]:
    """The ticket page's text, block by block (plain text, no formatting)."""
    if localboard.is_local_page(page_id):
        return localboard.body_lines(page_id)
    if ghboard.is_gh_page(page_id):
        return await ghboard.body_lines(page_id)
    r = await _http.get(f"/blocks/{page_id}/children", params={"page_size": 100})
    r.raise_for_status()
    lines = []
    for b in r.json()["results"]:
        text = "".join(x.get("plain_text", "") for x in (b.get(b["type"]) or {}).get("rich_text", []))
        if text.strip():
            lines.append(text)
    return lines


async def move(t: Ticket, to_project: str, reason: str) -> Ticket:
    """Copy the ticket (title + text) to another project's board, then drop the original.
    The copy is queued for Jarvis only if the original was waiting for it."""
    if localboard.is_local(to_project):
        lines = await body_lines(t.page_id) + [f"Moved here from {t.project} ({t.ref}): {reason}"]
        new = localboard.add_ticket(to_project, t.title, "\n".join(lines), note=f"moved from {t.project} {t.ref}",
                                    queued=t.jarvis in ("Queued", "Needs me", "Failed"))
        await drop(t, f"moved to {to_project} as {new.ref}: {reason}")
        return new
    if ghboard.is_gh(to_project):
        lines = await body_lines(t.page_id) + [f"Moved here from {t.project} ({t.ref}): {reason}"]
        new = await ghboard.add_ticket(to_project, t.title, "\n\n".join(lines),
                                       queued=t.jarvis in ("Queued", "Needs me", "Failed"))
        await drop(t, f"moved to {to_project} as {new.ref}: {reason}")
        return new
    db = boards()[to_project]
    schema = await _schema(db)
    props = {schema["title"]: {"title": [{"text": {"content": t.title[:200]}}]},
             "Jarvis note": _rich(f"moved from {t.project} {t.ref}")}
    if t.jarvis in ("Queued", "Needs me", "Failed"):
        props["Jarvis"] = {"select": {"name": "Queued"}}
    if todo := await _status_value(db, "todo"):
        props["Status"] = todo
    lines = await body_lines(t.page_id) + [f"Moved here from {t.project} ({t.ref}): {reason}"]
    children = [{"object": "block", "type": "paragraph",
                 "paragraph": {"rich_text": [{"text": {"content": line[:1900]}}]}} for line in lines[:100]]
    r = await _http.post("/pages", json={"parent": {"database_id": db}, "properties": props, "children": children})
    r.raise_for_status()
    new = _remember(to_project, r.json())
    await drop(t, f"moved to {to_project} as {new.ref}: {reason}")
    return new


async def update(t: Ticket, *, jarvis: str | None = None, note: str | None = None,
                 stage: str | None = None) -> None:
    if localboard.is_local_page(t.page_id):
        return localboard.update(t, jarvis=jarvis, note=note, stage=stage)
    if ghboard.is_gh_page(t.page_id):
        return await ghboard.update(t, jarvis=jarvis, note=note, stage=stage)
    props = {}
    if jarvis:
        props["Jarvis"] = {"select": {"name": jarvis}}
    if note is not None:
        props["Jarvis note"] = _rich(note)
    if stage and (value := await _status_value(boards()[t.project], stage)):
        props["Status"] = value
    r = await _http.patch(f"/pages/{t.page_id}", json={"properties": props})
    r.raise_for_status()
    _remember(t.project, r.json())


async def append_log(page_id: str, lines: list[str]) -> None:
    """Add lines to the ticket's page body (its live log)."""
    if localboard.is_local_page(page_id):
        return localboard.append_log(page_id, lines)
    if ghboard.is_gh_page(page_id):
        return await ghboard.append_log(page_id, lines)
    blocks = [{"object": "block", "type": "paragraph",
               "paragraph": {"rich_text": [{"text": {"content": line[:1900]}}]}} for line in lines if line]
    if blocks:
        r = await _http.patch(f"/blocks/{page_id}/children", json={"children": blocks[:100]})
        r.raise_for_status()


async def status_text(project: str = "") -> str:
    """Jarvis's work: queued / working / needs me, plus what it finished in the last day."""
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    recent = {"timestamp": "last_edited_time", "last_edited_time": {"after": since}}
    f = {"or": [*_jarvis_is(*ACTIVE)["or"],     # Notion allows only two levels of nesting
                *({"and": [{"property": "Jarvis", "select": {"equals": s}}, recent]} for s in ("Done", "Failed"))]}
    tickets = []
    for name in ([project] if project else boards()):
        if name in boards():
            tickets += await _query(name, f)
    if not tickets:
        return "Jarvis has nothing queued or running" + (f" for {project}." if project else ".")
    return "\n".join(t.line() for t in tickets)


async def project_board(project: str, sprint: str = "", status: str = "", limit: int = 60) -> str:
    """The board, optionally filtered. sprint: an exact sprint like "S5" (empty = any). status:
    "" / "open" (open tickets, the default), "done", "all", or a specific status name. Always shows
    counts per status and per sprint; if the filtered list is longer than `limit`, says so."""
    tickets = await _query(project)
    by_status: dict[str, int] = {}
    by_sprint: dict[str, int] = {}
    for t in tickets:
        by_status[t.status or "no status"] = by_status.get(t.status or "no status", 0) + 1
        by_sprint[t.sprint or "no sprint"] = by_sprint.get(t.sprint or "no sprint", 0) + 1

    want = status.strip().lower()
    sprint_l = sprint.strip().lower()

    def keep(t: Ticket) -> bool:
        if sprint_l and (t.sprint or "").lower() != sprint_l:
            return False
        s = (t.status or "").lower()
        if want in ("", "open", "todo", "to do", "not done", "not finished"):
            return s not in FINISHED
        if want in ("done", "finished", "closed"):
            return s in FINISHED
        if want == "all":
            return True
        return s == want                 # a specific status option name

    rows = [t for t in tickets if keep(t)]
    rows.sort(key=lambda t: (t.sprint or "~", t.status or "", t.title))

    scope = " ".join(p for p in (sprint, want if want and want != "open" else "open") if p) or "open"
    lines = [f"{project}: {len(tickets)} tickets total.",
             "By status: " + ", ".join(f"{k} {v}" for k, v in sorted(by_status.items())),
             "By sprint: " + ", ".join(f"{k} {v}" for k, v in sorted(by_sprint.items())),
             f"Showing {scope} ({len(rows)}):"]
    for t in rows[:limit]:
        lines.append(f"- {t.ref} [{t.status or '?'}] {t.title}" + (f" ({t.sprint})" if t.sprint else "")
                     + (f" — Jarvis {t.jarvis}" if t.jarvis else ""))
    if not rows:
        lines.append("(none match; try another sprint or status, or status 'all')")
    if len(rows) > limit:
        lines.append(f"... and {len(rows) - limit} more — narrow it with a sprint (e.g. S5) or status.")
    return "\n".join(lines)
