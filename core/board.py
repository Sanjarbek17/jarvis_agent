"""Each project's own Notion sprint board, with a "Jarvis" column added.

Jarvis column: Queued -> Working -> Needs me / Done / Failed. The runner only takes tickets
marked Queued; tickets with an empty Jarvis column are left alone. "Jarvis note" holds the
last step or the result. The board's own Status column is moved to in-progress / done too.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

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

_http = httpx.AsyncClient(base_url=API, headers=HEADERS, timeout=30)
_schemas: dict[str, dict] = {}       # db id -> {"title": prop name, "status": (type, [options])}


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

    def line(self) -> str:
        jarvis = f"Jarvis {self.jarvis}" if self.jarvis else "not for Jarvis"
        return (f"{self.ref} [{self.project}] {self.title} — {self.status or '?'}, {jarvis}"
                + (f": {self.note[:200]}" if self.note else ""))


def boards() -> dict[str, str]:
    reload_projects()
    return {name: p["board"] for name, p in PROJECTS.items() if p.get("board")}


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
        sprint=_choice(p.get("Sprint")), url=page["url"],
    )


async def _query(project: str, filter_: dict | None = None, sorts=None) -> list[Ticket]:
    db, out, cursor = boards()[project], [], None
    while True:
        body = {"page_size": 100, **({"filter": filter_} if filter_ else {}),
                **({"sorts": sorts} if sorts else {}), **({"start_cursor": cursor} if cursor else {})}
        r = await _http.post(f"/databases/{db}/query", json=body)
        r.raise_for_status()
        data = r.json()
        out += [_ticket(project, page) for page in data["results"]]
        if not data.get("has_more"):
            return out
        cursor = data["next_cursor"]


def _jarvis_is(*states: str) -> dict:
    return {"or": [{"property": "Jarvis", "select": {"equals": s}} for s in states]}


async def queued() -> list[Ticket]:
    """Tickets waiting for a worker, oldest first, across all boards."""
    out = []
    for project in boards():
        out += await _query(project, _jarvis_is("Queued"),
                            sorts=[{"timestamp": "created_time", "direction": "ascending"}])
    return out


async def with_jarvis(state: str) -> list[Ticket]:
    out = []
    for project in boards():
        out += await _query(project, _jarvis_is(state))
    return out


async def add_ticket(project: str, title: str, my_words: str, source: str) -> Ticket:
    """A new ticket on the project's board, queued for Jarvis."""
    db = boards()[project]
    schema = await _schema(db)
    props = {schema["title"]: {"title": [{"text": {"content": title[:200]}}]},
             "Jarvis": {"select": {"name": "Queued"}}, "Jarvis note": _rich(f"added from {source}")}
    if todo := await _status_value(db, "todo"):
        props["Status"] = todo
    body = [{"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"text": {"content": f"Sanjar's words ({source}): {my_words}"[:1900]}}]}}]
    r = await _http.post("/pages", json={"parent": {"database_id": db}, "properties": props, "children": body})
    r.raise_for_status()
    return _ticket(project, r.json())


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
    for project in boards():
        for t in await _query(project, {"property": "Jarvis", "select": {"is_not_empty": True}}):
            if t.ref == ref.strip().lower():
                return t
    return None


async def find_any(ref: str) -> Ticket | None:
    """Any ticket on any board by its short ref, also ones Jarvis was never asked to do."""
    if t := await find(ref):
        return t
    for project in boards():
        for t in await _query(project):
            if t.ref == ref.strip().lower():
                return t
    return None


async def unqueue(t: Ticket, reason: str) -> None:
    """Clear the Jarvis column: the runner leaves the ticket alone from now on."""
    r = await _http.patch(f"/pages/{t.page_id}", json={"properties": {
        "Jarvis": {"select": None}, "Jarvis note": _rich(reason)}})
    r.raise_for_status()
    await append_log(t.page_id, [f"Taken off Jarvis's queue: {reason}"])


async def drop(t: Ticket, reason: str) -> str:
    """Unqueue and mark the ticket Dropped on its board. Select-type Status columns get a
    "Dropped" option (Notion adds it on first use); fixed status-type columns can't, so the
    title gets a [dropped] prefix instead. Returns how it was marked."""
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
    _schemas.pop(db, None)          # a new "Dropped" option may have been added
    await append_log(t.page_id, [f"Dropped: {reason}"])
    return how


async def body_lines(page_id: str) -> list[str]:
    """The ticket page's text, block by block (plain text, no formatting)."""
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
    new = _ticket(to_project, r.json())
    await drop(t, f"moved to {to_project} as {new.ref}: {reason}")
    return new


async def update(t: Ticket, *, jarvis: str | None = None, note: str | None = None,
                 stage: str | None = None) -> None:
    props = {}
    if jarvis:
        props["Jarvis"] = {"select": {"name": jarvis}}
    if note is not None:
        props["Jarvis note"] = _rich(note)
    if stage and (value := await _status_value(boards()[t.project], stage)):
        props["Status"] = value
    r = await _http.patch(f"/pages/{t.page_id}", json={"properties": props})
    r.raise_for_status()


async def append_log(page_id: str, lines: list[str]) -> None:
    """Add lines to the ticket's page body (its live log)."""
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


async def project_board(project: str, include_done: bool = False) -> str:
    """The whole board: counts per status and the open tickets by sprint."""
    tickets = await _query(project)
    counts: dict[str, int] = {}
    for t in tickets:
        counts[t.status or "no status"] = counts.get(t.status or "no status", 0) + 1
    rows = [t for t in tickets if include_done or t.status.lower() not in FINISHED]
    rows.sort(key=lambda t: (t.sprint or "~", t.title))
    lines = ["Counts: " + ", ".join(f"{k} {v}" for k, v in counts.items())]
    for t in rows[:40]:
        lines.append(f"- {t.ref} [{t.status or '?'}] {t.title}" + (f" ({t.sprint})" if t.sprint else "")
                     + (f" — Jarvis {t.jarvis}" if t.jarvis else ""))
    if len(rows) > 40:
        lines.append(f"... and {len(rows) - 40} more")
    return "\n".join(lines)
