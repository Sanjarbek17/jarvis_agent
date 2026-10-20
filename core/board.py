"""The Notion "Jarvis Tasks" board: add cards, read status, update cards."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .config import NOTION_DB, NOTION_TOKEN

API = "https://api.notion.com/v1"
HEADERS = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28"}
ACTIVE = ("To do", "Doing", "Needs me")

_http = httpx.AsyncClient(base_url=API, headers=HEADERS, timeout=30)


@dataclass
class Card:
    page_id: str
    id: str             # e.g. JT-3
    task: str
    status: str
    project: str
    folder: str
    my_words: str
    last_step: str
    summary: str
    created: str
    finished: str
    url: str

    def line(self) -> str:
        extra = self.last_step if self.status == "Doing" else self.summary
        return f"{self.id} [{self.project}] {self.status}: {self.task}" + (f" — {extra}" if extra else "")


def _text(prop) -> str:
    items = prop.get("title") or prop.get("rich_text") or []
    return "".join(i.get("plain_text", "") for i in items)


def _card(page) -> Card:
    p = page["properties"]
    uid = p["ID"]["unique_id"]
    return Card(
        page_id=page["id"],
        id=f"{uid['prefix']}-{uid['number']}",
        task=_text(p["Task"]),
        status=(p["Status"]["status"] or {}).get("name", ""),
        project=(p["Project"]["select"] or {}).get("name", ""),
        folder=_text(p["Folder"]),
        my_words=_text(p["My words"]),
        last_step=_text(p["Last step"]),
        summary=_text(p["Summary"]),
        created=page["created_time"],
        finished=((p["Finished"]["date"] or {}).get("start") or ""),
        url=page["url"],
    )


def _rich(text: str) -> dict:
    return {"rich_text": [{"text": {"content": text[:1900]}}]} if text else {"rich_text": []}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _query(filter_: dict, sorts=None) -> list[Card]:
    body = {"filter": filter_, "page_size": 100}
    if sorts:
        body["sorts"] = sorts
    r = await _http.post(f"/databases/{NOTION_DB}/query", json=body)
    r.raise_for_status()
    return [_card(p) for p in r.json()["results"]]


async def add_task(project: str, task: str, my_words: str, source: str, folder: str = "") -> Card:
    props = {
        "Task": {"title": [{"text": {"content": task[:200]}}]},
        "Status": {"status": {"name": "To do"}},
        "Project": {"select": {"name": project}},
        "Source": {"select": {"name": source}},
        "My words": _rich(my_words),
        "Folder": _rich(folder),
    }
    r = await _http.post("/pages", json={"parent": {"database_id": NOTION_DB}, "properties": props})
    r.raise_for_status()
    return _card(r.json())


async def get(page_id: str) -> Card:
    r = await _http.get(f"/pages/{page_id}")
    r.raise_for_status()
    return _card(r.json())


async def find(card_id: str) -> Card | None:
    """Find a card by its short id, e.g. JT-3."""
    try:
        number = int(card_id.upper().replace("JT-", "").strip())
    except ValueError:
        return None
    cards = await _query({"property": "ID", "unique_id": {"equals": number}})
    return cards[0] if cards else None


async def todo() -> list[Card]:
    return await _query({"property": "Status", "status": {"equals": "To do"}},
                        sorts=[{"timestamp": "created_time", "direction": "ascending"}])


async def with_status(status: str) -> list[Card]:
    return await _query({"property": "Status", "status": {"equals": status}})


async def status_cards(project: str = "") -> list[Card]:
    """What matters for a status answer: active cards + anything finished in the last day."""
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    f = {"or": [{"property": "Status", "status": {"equals": s}} for s in ACTIVE]
         + [{"property": "Finished", "date": {"after": since}}]}
    if project:
        f = {"and": [f, {"property": "Project", "select": {"equals": project}}]}
    return await _query(f, sorts=[{"timestamp": "created_time", "direction": "descending"}])


async def status_text(project: str = "") -> str:
    cards = await status_cards(project)
    if not cards:
        return "No active or recent tasks" + (f" for {project}." if project else ".")
    return "\n".join(c.line() for c in cards)


async def update(page_id: str, *, status: str | None = None, last_step: str | None = None,
                 summary: str | None = None, my_words: str | None = None,
                 started: bool = False, finished: bool = False) -> None:
    props = {}
    if my_words is not None:
        props["My words"] = _rich(my_words)
    if status:
        props["Status"] = {"status": {"name": status}}
    if last_step is not None:
        props["Last step"] = _rich(last_step)
    if summary is not None:
        props["Summary"] = _rich(summary)
    if started:
        props["Started"] = {"date": {"start": _now()}}
    if finished:
        props["Finished"] = {"date": {"start": _now()}}
    r = await _http.patch(f"/pages/{page_id}", json={"properties": props})
    r.raise_for_status()


async def append_log(page_id: str, lines: list[str]) -> None:
    """Add lines to the card's page body (its live log)."""
    blocks = [{"object": "block", "type": "paragraph",
               "paragraph": {"rich_text": [{"text": {"content": line[:1900]}}]}} for line in lines if line]
    if blocks:
        r = await _http.patch(f"/blocks/{page_id}/children", json={"children": blocks[:100]})
        r.raise_for_status()


# --- the older per-project sprint boards (read only) --------------------------------------

FINISHED = {"done", "shipped", "complete", "completed", "cancelled", "canceled", "won't do", "archived"}


def _choice(prop) -> str:
    if not prop:
        return ""
    value = prop.get(prop["type"])
    return value.get("name", "") if isinstance(value, dict) else ""


async def project_board(db_id: str, include_done: bool = False) -> str:
    """Summary of a project's own sprint board: counts per status + the open tickets."""
    rows, cursor = [], None
    while True:
        body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        r = await _http.post(f"/databases/{db_id}/query", json=body)
        r.raise_for_status()
        data = r.json()
        for page in data["results"]:
            p = page["properties"]
            title = next((_text(v) for v in p.values() if v["type"] == "title"), "")
            rows.append({
                "name": title, "status": _choice(p.get("Status")), "sprint": _choice(p.get("Sprint")),
                "epic": _choice(p.get("Epic")), "order": (p.get("Order") or {}).get("number") or 0,
            })
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"] or "no status"] = counts.get(row["status"] or "no status", 0) + 1
    open_rows = [r for r in rows if include_done or r["status"].lower() not in FINISHED]
    open_rows.sort(key=lambda r: (r["sprint"] or "~", r["order"]))
    lines = ["Counts: " + ", ".join(f"{k} {v}" for k, v in counts.items())]
    for row in open_rows[:40]:
        where = " / ".join(x for x in (row["sprint"], row["epic"]) if x)
        lines.append(f"- [{row['status'] or '?'}] {row['name']}" + (f" ({where})" if where else ""))
    if len(open_rows) > 40:
        lines.append(f"... and {len(open_rows) - 40} more")
    return "\n".join(lines)
