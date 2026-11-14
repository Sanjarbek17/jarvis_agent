#!/usr/bin/env python3
"""Open a page in Sanjar's real Chrome (his logins) and close it again (ticket 3a61de45: workers kept opening
Chrome windows with `open -na "Google Chrome" ...` and never closed them: 30 tabs piled up).

Prefer a headless browser (Playwright) for anything that doesn't need his logged-in Chrome. When it does:

    scripts/real_chrome.py open <url> [--profile "Profile 4"]   # a NEW window; prints its id; remembered
    scripts/real_chrome.py close [--owner REF] [--window ID]    # closes only windows opened through this script
    scripts/real_chrome.py sweep [--older-than SECONDS] [--owner X]   # closes remembered windows older than that
    scripts/real_chrome.py list

The owner is $JARVIS_OWNER (the runner sets the ticket ref), so the runner closes a worker's windows when it ends.
Never touches windows or tabs that weren't opened through it.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from core.paths import VAR  # noqa: E402  (tests: a temporary folder, never the real one)

OPENED = VAR / "state" / "opened_chrome.json"


def _osa(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
    return r.stdout.strip()


def _running() -> bool:
    return subprocess.run(["pgrep", "-xq", "Google Chrome"]).returncode == 0


def automation_running() -> bool:
    """A Chrome started by a script (Playwright renders, the Instagram scripts) has command-line switches; his own
    has none. While one runs, AppleScript may talk to IT instead of his Chrome (found 13 Nov), so nothing here
    trusts AppleScript then."""
    out = subprocess.run(["pgrep", "-fl", "Google Chrome.app/Contents/MacOS/Google Chrome --"], capture_output=True, text=True).stdout
    return bool(out.strip())


def wait_for_his_chrome_only(timeout: float = 120) -> bool:
    end = time.time() + timeout
    while automation_running():
        if time.time() > end:
            return False
        time.sleep(2)
    return True


def window_ids() -> set[int]:
    if not _running():
        return set()
    out = _osa('tell application "Google Chrome" to get id of every window')
    return {int(x) for x in out.replace(" ", "").split(",") if x.strip().isdigit()}


def tab_with(url: str) -> tuple[int, int] | None:
    """(window id, tab id) of a tab showing this URL, or None."""
    out = _osa('tell application "Google Chrome"\nset r to ""\nrepeat with w in windows\nrepeat with t in tabs of w\n'
               'set r to r & (id of w) & " " & (id of t) & " " & (URL of t) & linefeed\nend repeat\nend repeat\nreturn r\nend tell')
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[2].rstrip("/") == url.rstrip("/"):
            return int(parts[0]), int(parts[1])
    return None


def _load() -> list[dict]:
    try:
        return json.loads(OPENED.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(rows: list[dict]) -> None:
    OPENED.parent.mkdir(parents=True, exist_ok=True)
    OPENED.write_text(json.dumps(rows, indent=1))


def open_page(url: str, profile: str = "", owner: str = "") -> dict:
    sure = wait_for_his_chrome_only()
    before = window_ids() if sure else set()
    cmd = ["open", "-na", "Google Chrome", "--args", "--new-window"]
    if profile:
        cmd.insert(4, f"--profile-directory={profile}")
    subprocess.run(cmd + [url], check=True, timeout=30)
    row = {"url": url, "profile": profile, "owner": owner, "at": time.time()}
    for _ in range(40 if sure else 0):            # Chrome needs a moment to make the window
        time.sleep(0.25)
        new = window_ids() - before
        if new:
            row["window"] = max(new)
            break
    else:
        hit = tab_with(url) if sure else None      # it went into an existing window as a tab
        if hit:
            row["window"], row["tab"] = hit
    _save(_load() + [row])
    return row


def _close(row: dict) -> bool | None:
    """True closed, False gone already, None not sure yet (keep it and try again later)."""
    if automation_running():
        return None
    if not row.get("window"):
        hit = tab_with(row["url"])                 # opened while a script's Chrome ran: find it by its exact address
        if not hit:
            return None if time.time() - row.get("at", 0) < 86400 else False
        row["window"], row["tab"] = hit
    w, t = row.get("window"), row.get("tab")
    if w not in window_ids():
        return False
    if t:
        _osa(f'tell application "Google Chrome" to close (every tab of (every window whose id is {w}) whose id is {t})')
    else:
        _osa(f'tell application "Google Chrome" to close (every window whose id is {w})')
    return True


def close(owner: str = "", window: int = 0, older_than: float = -1) -> int:
    keep, n = [], 0
    for row in _load():
        mine = (not owner or row.get("owner") == owner) and (not window or row.get("window") == window)
        old = older_than < 0 or time.time() - row.get("at", 0) > older_than
        if mine and old:
            try:
                done = _close(row)
            except Exception:
                done = False                       # Chrome quit or the window is gone: forget it
            if done is None:
                keep.append(row)                   # not sure which Chrome AppleScript sees: next sweep
            n += bool(done)
        else:
            keep.append(row)
    _save(keep)
    return n


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open"); o.add_argument("url"); o.add_argument("--profile", default="")
    c = sub.add_parser("close"); c.add_argument("--owner", default=None); c.add_argument("--window", type=int, default=0)
    s = sub.add_parser("sweep"); s.add_argument("--older-than", type=float, default=3 * 3600); s.add_argument("--owner", default="")
    sub.add_parser("list")
    a = p.parse_args(argv)
    owner = os.environ.get("JARVIS_OWNER", "")
    if a.cmd == "open":
        row = open_page(a.url, a.profile, owner)
        print(f"opened window {row.get('window', '?')} (close it with: scripts/real_chrome.py close --window {row.get('window', 0)})")
    elif a.cmd == "close":
        who = owner if a.owner is None else a.owner
        print(f"closed {close(who, a.window)} window(s)")
    elif a.cmd == "sweep":
        print(f"closed {close(a.owner, 0, a.older_than)} old window(s)")
    else:
        print(json.dumps(_load(), indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
