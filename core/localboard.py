"""Boards kept on the Mac instead of Notion: one small SQLite file, var/state/boards.db.

config/projects.json: "local": true on a project (its old "board" id may stay, as a way back).
board.py sends such projects here; everything else stays the same for the runner, tools and brain.

- The ref is 8 hex characters, page_id "local:<ref>". Tickets copied from Notion keep their old ref
  and the link to their Notion page (notion_url).
- body is the ticket's own text (plain text / markdown), the log is what Jarvis adds below it.
- Status is free text: new tickets use To do / In progress / Done, copied ones keep their board's words.
SQLite handles the locking, so scripts/board.py (workers) can use it next to the running Jarvis.
"""
import contextlib
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import quote

from .config import PROJECTS, STATE, reload_projects

DB = STATE / "boards.db"
SITE = "https://jarvis.sanjarbek17.uz"
STAGES = {"todo": "To do", "doing": "In progress", "done": "Done"}
EDITABLE = {"title", "body", "status", "sprint", "due", "jarvis", "note", "start_after"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    ref TEXT PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '', jarvis TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    sprint TEXT NOT NULL DEFAULT '', due TEXT NOT NULL DEFAULT '', notion_url TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL, edited TEXT NOT NULL, start_after TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS tickets_project ON tickets (project, created);
CREATE INDEX IF NOT EXISTS tickets_jarvis ON tickets (jarvis);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY, ref TEXT NOT NULL, at REAL NOT NULL, line TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS log_ref ON log (ref, id);
"""
_ready: set[str] = set()


@contextlib.contextmanager
def _db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        if str(DB) not in _ready:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            cols = {r[1] for r in con.execute("PRAGMA table_info(tickets)")}
            if "start_after" not in cols:            # a board from before follow-up checks (ticket ac668ebe)
                con.execute("ALTER TABLE tickets ADD COLUMN start_after TEXT NOT NULL DEFAULT ''")
            _ready.add(str(DB))
        with con:
            yield con
    finally:
        con.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ref(page_id: str) -> str:
    return page_id.removeprefix("local:")


def _ticket(row):
    from .board import Ticket
    return Ticket(page_id=f"local:{row['ref']}", ref=row["ref"], project=row["project"], title=row["title"],
                  status=row["status"], jarvis=row["jarvis"], note=row["note"], sprint=row["sprint"],
                  url=f"{SITE}/#/t/{quote(row['project'], safe='')}/{row['ref']}", edited=row["edited"], due=row["due"],
                  start_after=row["start_after"] if "start_after" in row.keys() else "")


def is_local(project: str) -> bool:
    reload_projects()
    return bool(PROJECTS.get(project, {}).get("local"))


def is_local_page(page_id: str) -> bool:
    return page_id.startswith("local:")


def tickets(project: str) -> list:
    """Every ticket on the board, oldest first."""
    with _db() as con:
        rows = con.execute("SELECT * FROM tickets WHERE project = ? ORDER BY created, ref", (project,)).fetchall()
    return [_ticket(r) for r in rows]


def get(ref: str):
    with _db() as con:
        row = con.execute("SELECT * FROM tickets WHERE ref = ?", (ref.strip().lower(),)).fetchone()
    return _ticket(row) if row else None


def details(ref: str) -> dict:
    """What the ticket screen shows on top of the Ticket: its text, log and Notion link."""
    with _db() as con:
        row = con.execute("SELECT body, notion_url FROM tickets WHERE ref = ?", (ref,)).fetchone()
        log = con.execute("SELECT at, line FROM log WHERE ref = ? ORDER BY id", (ref,)).fetchall()
    return {"text": row["body"] if row else "", "notion_url": row["notion_url"] if row else "",
            "log": [dict(r) for r in log]}


def add_ticket(project: str, title: str, body: str, queued: bool = True, status: str = "", sprint: str = "",
               note: str = "", start_after: str = ""):
    ref, now = secrets.token_hex(4), _now()
    with _db() as con:
        con.execute("INSERT INTO tickets (ref, project, title, body, status, jarvis, note, sprint, created, edited, "
                    "start_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ref, project, title[:200], body, status or STAGES["todo"], "Queued" if queued else "",
                     note, sprint, now, now, start_after))
    return get(ref)


def copy_ticket(row: dict) -> None:
    """A ticket copied from elsewhere (scripts/notion_to_local.py); running it again refreshes the copy.
    row: ref, project, title, body, status, jarvis, note, sprint, due, notion_url, created, edited."""
    names = ("ref", "project", "title", "body", "status", "jarvis", "note", "sprint", "due", "notion_url",
             "created", "edited")
    with _db() as con:
        con.execute(f"INSERT OR REPLACE INTO tickets ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                    tuple(row.get(n, "") for n in names))


def set_fields(ref: str, **fields) -> None:
    bad = set(fields) - EDITABLE
    if bad:
        raise ValueError(f"can't set {sorted(bad)}")
    fields["edited"] = _now()
    with _db() as con:
        con.execute(f"UPDATE tickets SET {', '.join(f'{k} = ?' for k in fields)} WHERE ref = ?",
                    (*fields.values(), ref))


def update(t, *, jarvis: str | None = None, note: str | None = None, stage: str | None = None) -> None:
    fields = {}
    if jarvis:
        fields["jarvis"] = jarvis
    if note is not None:
        fields["note"] = note[:1900]
    if stage:
        fields["status"] = STAGES[stage]
    set_fields(t.ref, **fields)


def clear_jarvis(t, reason: str) -> None:
    set_fields(t.ref, jarvis="", note=reason[:1900])


def drop(t, reason: str) -> str:
    set_fields(t.ref, jarvis="", note=f"dropped: {reason}"[:1900], status="Dropped")
    append_log(t.page_id, [f"Dropped: {reason}"])
    return "Status Dropped"


def append_log(page_id: str, lines: list[str]) -> None:
    ref, now = _ref(page_id), time.time()
    with _db() as con:
        con.executemany("INSERT INTO log (ref, at, line) VALUES (?, ?, ?)", [(ref, now, l) for l in lines if l])
        con.execute("UPDATE tickets SET edited = ? WHERE ref = ?", (_now(), ref))


def body_lines(page_id: str) -> list[str]:
    d = details(_ref(page_id))
    return [l for l in d["text"].splitlines() if l.strip()] + [e["line"] for e in d["log"]]
