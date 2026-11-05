#!/usr/bin/env python
"""The boards kept on the Mac (core/localboard.py), for workers, skills and the terminal.

    .venv/bin/python scripts/board.py list <project> [--all]
    .venv/bin/python scripts/board.py show <ref>
    .venv/bin/python scripts/board.py add <project> <title> [--text-file F | --text T] [--sprint S] [--queue]
    .venv/bin/python scripts/board.py edit <ref> [--title T] [--text-file F | --text T] [--sprint S] [--status S]
    .venv/bin/python scripts/board.py log <ref> <line>...

--text-file - reads the text from stdin. Only Jarvis moves a ticket's Jarvis state; `add --queue` hands a
new ticket to it.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from core import localboard  # noqa: E402

CLOSED = ("done", "dropped", "cancelled", "canceled")


def _text(a) -> str | None:
    if a.text_file == "-":
        return sys.stdin.read()
    if a.text_file:
        return Path(a.text_file).read_text()
    return a.text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("project")
    p.add_argument("--all", action="store_true", help="finished tickets too")
    sub.add_parser("show").add_argument("ref")
    for name in ("add", "edit"):
        p = sub.add_parser(name)
        p.add_argument("project" if name == "add" else "ref")
        p.add_argument("title", nargs=None if name == "add" else "?")
        p.add_argument("--title", dest="new_title")
        p.add_argument("--text")
        p.add_argument("--text-file")
        p.add_argument("--sprint")
        p.add_argument("--status")
        p.add_argument("--queue", action="store_true")
    p = sub.add_parser("log")
    p.add_argument("ref")
    p.add_argument("lines", nargs="+")
    a = ap.parse_args()

    if a.cmd == "list":
        if not localboard.is_local(a.project):
            print(f"{a.project} isn't a board on the Mac", file=sys.stderr)
            return 1
        for t in localboard.tickets(a.project):
            if a.all or t.status.lower() not in CLOSED:
                print(t.line())
        return 0
    if a.cmd == "add":
        if not localboard.is_local(a.project):
            print(f"{a.project} isn't a board on the Mac", file=sys.stderr)
            return 1
        t = localboard.add_ticket(a.project, a.title, _text(a) or "", queued=a.queue, status=a.status or "",
                                  sprint=a.sprint or "")
        print(t.ref)
        return 0
    t = localboard.get(a.ref)
    if not t:
        print(f"No ticket {a.ref} on the boards on the Mac", file=sys.stderr)
        return 1
    if a.cmd == "show":
        d = localboard.details(t.ref)
        print(f"{t.ref} [{t.project}] {t.title}\nStatus: {t.status or '?'}   Jarvis: {t.jarvis or '-'}"
              f"   Sprint: {t.sprint or '-'}   Due: {t.due or '-'}")
        if t.note:
            print(f"Note: {t.note}")
        if d["notion_url"]:
            print(f"Old Notion page (images and files only there): {d['notion_url']}")
        print("\n" + d["text"])
        if d["log"]:
            print("\n--- log ---\n" + "\n".join(e["line"] for e in d["log"]))
    elif a.cmd == "edit":
        fields = {"title": a.new_title or a.title, "body": _text(a), "sprint": a.sprint, "status": a.status}
        localboard.set_fields(t.ref, **{k: v for k, v in fields.items() if v is not None})
    else:
        localboard.append_log(t.page_id, a.lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
