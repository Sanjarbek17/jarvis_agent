#!/usr/bin/env python
"""Copy a project's Notion board to its board on the Mac; with --switch, make that the real board.

    .venv/bin/python scripts/notion_to_local.py <project>            # copy; safe to run again
    .venv/bin/python scripts/notion_to_local.py <project> --switch   # copy, then "local": true
    .venv/bin/python scripts/notion_to_local.py --all [--switch]

Notion itself isn't changed. The project's "board" id stays in projects.json: remove "local" to go back,
but tickets changed since then stay on the Mac. A board with a ticket being worked on isn't switched.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from core import board, localboard  # noqa: E402
from core.config import PROJECTS, PROJECTS_FILE, reload_projects  # noqa: E402

SKIP = {"Status", "Jarvis", "Jarvis note", "Sprint", "Due"}
PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ", "bulleted_list_item": "- ",
          "numbered_list_item": "1. ", "quote": "> ", "callout": "> ", "toggle": "- "}


async def notion(method: str, url: str, **kw) -> dict:
    """One Notion call, waiting and retrying when it is busy or failing."""
    for attempt in range(6):
        try:
            r = await board._http.request(method, url, **kw)
        except Exception:
            if attempt == 5:
                raise
            await asyncio.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == 5:
                r.raise_for_status()
            await asyncio.sleep(float(r.headers.get("retry-after") or 2 ** attempt))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def plain(rich: list) -> str:
    out = ""
    for piece in rich or []:
        text, link = piece.get("plain_text", ""), piece.get("href")
        out += f"[{text}]({link})" if link and link != text else text
    return out


def block_line(b: dict) -> str | None:
    """One Notion block as a line of text; None for blocks with nothing to show."""
    kind, value = b["type"], b.get(b["type"]) or {}
    if kind == "divider":
        return "---"
    if kind == "to_do":
        return f"- [{'x' if value.get('checked') else ' '}] {plain(value.get('rich_text'))}"
    if kind == "code":
        return f"```{value.get('language', '')}\n{plain(value.get('rich_text'))}\n```"
    if kind == "table_row":
        return " | ".join(plain(cell) for cell in value.get("cells", []))
    if kind in ("image", "file", "video", "pdf", "audio"):
        where = (value.get("external") or {}).get("url") or "only on the Notion page"
        return f"[{kind}: {plain(value.get('caption')) or value.get('name') or where}]"
    if kind in ("bookmark", "link_preview", "embed"):
        return value.get("url")
    if kind == "child_page":
        return f"[sub-page: {value.get('title', '')}]"
    if kind == "child_database":
        return f"[database: {value.get('title', '')}]"
    text = plain(value.get("rich_text"))
    return PREFIX.get(kind, "") + text if text else None


async def page_text(block_id: str, depth: int = 0) -> list[str]:
    lines, cursor = [], None
    while True:
        params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        data = await notion("GET", f"/blocks/{block_id}/children", params=params)
        for b in data["results"]:
            line = block_line(b)
            if line is not None:
                lines.append("  " * depth + line)
            if b.get("has_children") and b["type"] not in ("child_page", "child_database"):
                lines += await page_text(b["id"], depth + 1)
        if not data.get("has_more"):
            return lines
        cursor = data["next_cursor"]


def extra_fields(props: dict) -> list[str]:
    """The board's other columns (Epic, Priority...), so nothing is lost, as lines on top of the text."""
    out = []
    for name, p in props.items():
        kind = p["type"]
        if name in SKIP or kind == "title":
            continue
        v = p.get(kind)
        if kind in ("select", "status"):
            v = (v or {}).get("name")
        elif kind == "multi_select":
            v = ", ".join(o["name"] for o in v or [])
        elif kind == "rich_text":
            v = plain(v)
        elif kind == "date":
            v = (v or {}).get("start")
        elif kind == "checkbox":
            v = "yes" if v else None
        elif kind not in ("number", "url", "email", "phone_number"):
            continue
        if v not in (None, ""):
            out.append(f"{name}: {v}")
    return out


async def pages_of(db: str, filter_: dict | None = None) -> list[dict]:
    out, cursor = [], None
    while True:
        body = {"page_size": 100, "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
                **({"filter": filter_} if filter_ else {}), **({"start_cursor": cursor} if cursor else {})}
        data = await notion("POST", f"/databases/{db}/query", json=body)
        out += data["results"]
        if not data.get("has_more"):
            return out
        cursor = data["next_cursor"]


def status_name(status: str) -> str:
    """Notion's spellings of the three stages as the board on the Mac writes them, so they make one column."""
    low = status.lower()
    for stage in ("doing", "done"):
        if low in board.STAGE_NAMES[stage]:
            return localboard.STAGES[stage]
    return localboard.STAGES["todo"] if low in ("to do", "todo", "not started") else status


async def copy_page(project: str, page: dict) -> None:
    t = board._ticket(project, page)
    extras = extra_fields(page["properties"])
    text = await page_text(page["id"])
    localboard.copy_ticket({
        "ref": t.ref, "project": project, "title": t.title, "status": status_name(t.status), "jarvis": t.jarvis, "note": t.note,
        "sprint": t.sprint, "due": t.due, "notion_url": t.url, "created": page["created_time"], "edited": t.edited,
        "body": "\n".join(extras + ([""] if extras and text else []) + text)})


async def copy_board(project: str, db: str) -> int:
    pages = await pages_of(db)
    for i, page in enumerate(pages, 1):
        await copy_page(project, page)
        if i % 25 == 0:
            print(f"  {project}: {i}/{len(pages)}", flush=True)
    print(f"{project}: {len(pages)} tickets copied", flush=True)
    return len(pages)


def switch(project: str) -> None:
    data = json.loads(PROJECTS_FILE.read_text())
    data[project]["local"] = True
    PROJECTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


async def run(project: str, want_switch: bool) -> bool:
    db = PROJECTS[project].get("board")
    if PROJECTS[project].get("local") or not db:
        print(f"{project}: nothing to do (already local, or no Notion board)")
        return True
    await copy_board(project, db)
    if not want_switch:
        return True
    active = await pages_of(db, board._jarvis_is(*board.ACTIVE, "Failed"))   # anything that moved during the copy
    if any(board._ticket(project, p).jarvis == "Working" for p in active):
        print(f"{project}: NOT switched, a ticket is being worked on. Run this again when it's done.")
        return False
    for page in active:
        await copy_page(project, page)
    switch(project)
    print(f"{project}: switched to the board on the Mac")
    return True


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--switch", action="store_true")
    a = ap.parse_args()
    reload_projects()
    names = [n for n, p in PROJECTS.items() if p.get("board")] if a.all else [a.project]
    if not a.all and a.project not in PROJECTS:
        ap.error(f"unknown project {a.project!r}")
    ok = [await run(name, a.switch) for name in names]
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
