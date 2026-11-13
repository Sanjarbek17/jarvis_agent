#!/usr/bin/env python3
"""Claude Code PreToolUse hook for ticket workers (ticket 3a61de45): no opening pages in Sanjar's real Chrome
with `open` (or AppleScript), because nobody closed them and 30 tabs piled up. Exit 2 blocks the command and the
message tells the worker what to do instead."""
import json
import re
import sys

RAW = re.compile(
    r"""(?:^|[;&|(`\s])open\s+(?:-[a-zA-Z]+\s+)*(?:"?[^"\s]*Chrome[^"\s]*"?\s+)?(?:--args\b[^\n]*?)?"""
    r"""['"]?(?:https?://|file://|[^\s'"]+\.html?\b)"""
    r"""|(?:^|[;&|(`\s])open\s+(?:-[a-zA-Z]+\s+)*-n?a\s+['"]?Google Chrome"""
    r"""|tell application "Google Chrome"[^\n]*(?:make new|open location)""",
    re.I)
HELP = ("Blocked: don't open pages in Sanjar's own Chrome with `open` / AppleScript; nobody closes them "
        "(ticket 3a61de45). Use a headless browser (Playwright) for anything that doesn't need his logins. If it truly "
        "needs his logged-in Chrome: /Users/sanjarbek/dev/side_project/projects/jarvis/.venv/bin/python "
        "/Users/sanjarbek/dev/side_project/projects/jarvis/scripts/real_chrome.py open <url> [--profile \"Profile 4\"], "
        "and `real_chrome.py close --window <id>` as soon as you're done (it's also closed when your ticket ends).")


def blocked(command: str) -> bool:
    return "real_chrome.py" not in command and bool(RAW.search(command))


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if data.get("tool_name") == "Bash" and blocked(str((data.get("tool_input") or {}).get("command", ""))):
        print(HELP, file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
