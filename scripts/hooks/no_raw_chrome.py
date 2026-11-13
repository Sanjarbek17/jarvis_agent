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
# his real profile data: copying its cookies into another browser made Google sign him out of every account in
# Chrome, because Google treats a session showing up in a second browser as stolen (ticket c09280c2)
REAL_PROFILE = re.compile(r"Application(?:\\ |\s|%20)Support/Google/Chrome", re.I)
PROFILE_HELP = ("Blocked: never copy or use Sanjar's real Chrome profile data (Cookies, Login Data, Local State, the "
                "profile folder). Google signs him out of all his accounts when his session shows up in another browser "
                "(ticket c09280c2). Log in to the site in your own browser profile (e.g. a Playwright profile in the project, "
                "and ask Jarvis for the password from the project's .env), or work in his real Chrome itself with "
                "/Users/sanjarbek/dev/side_project/projects/jarvis/scripts/real_chrome.py.")
HELP = ("Blocked: don't open pages in Sanjar's own Chrome with `open` / AppleScript; nobody closes them "
        "(ticket 3a61de45). Use a headless browser (Playwright) for anything that doesn't need his logins. If it truly "
        "needs his logged-in Chrome: /Users/sanjarbek/dev/side_project/projects/jarvis/.venv/bin/python "
        "/Users/sanjarbek/dev/side_project/projects/jarvis/scripts/real_chrome.py open <url> [--profile \"Profile 4\"], "
        "and `real_chrome.py close --window <id>` as soon as you're done (it's also closed when your ticket ends).")


def blocked(command: str) -> bool:
    return "real_chrome.py" not in command and bool(RAW.search(command))


def touches_real_profile(command: str) -> bool:
    return bool(REAL_PROFILE.search(command))


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    command = str((data.get("tool_input") or {}).get("command", "")) if data.get("tool_name") == "Bash" else ""
    if command and touches_real_profile(command):
        print(PROFILE_HELP, file=sys.stderr)
        sys.exit(2)
    if command and blocked(command):
        print(HELP, file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
