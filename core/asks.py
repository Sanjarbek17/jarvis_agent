"""Requests to Sanjar that belong to the program that needs them (ticket c2f2a3c7).

Root cause it fixes: "please unlock the Mac so the reel posts" was a static "Needs me" note a worker copied onto a
board ticket. The poster, the only code that knew whether it still needed the screen, had no way to take it back,
so after the Mac was unlocked and the reel posted, the note stayed and Jarvis passed it on hours later.

Now the program that is blocked opens the ask itself and withdraws it itself the moment it no longer needs it:

    asks.open_ask("haydovchi:unlock:<slug>", "Haydovchi poster", "Mac ekrani qulflangan ...")   # sent once
    asks.close_ask("haydovchi:unlock:<slug>")      # unlocked / posted / given up: gone, and so is the message

Each ask is one Telegram message under the outbox key "ask:<key>"; closing it deletes that message, so his chat
never holds a request nobody needs any more. get_status lists what's open. Only `core.outbox` and `core.paths`
are imported, so scripts outside Jarvis (the poster) can use it cheaply.
"""
import fcntl
import json
import time
from contextlib import contextmanager

from . import outbox
from .paths import VAR

FILE = VAR / "state" / "asks.json"     # key -> {"owner", "text", "at"}


@contextmanager
def _locked():
    FILE.parent.mkdir(parents=True, exist_ok=True)
    with (FILE.parent / ".asks.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        yield data
        FILE.write_text(json.dumps(data, ensure_ascii=False))


def _send(**kw) -> None:
    if outbox.jarvis_running():
        outbox.send(wait=30, **kw)


def open_ask(key: str, owner: str, text: str) -> bool:
    """Ask Sanjar for something only he can do. Sent once while it stays open; True if it was new."""
    with _locked() as data:
        if key in data:
            return False
        data[key] = {"owner": owner, "text": text, "at": time.time()}
    _send(text=text, key=f"ask:{key}")
    return True


def close_ask(key: str) -> bool:
    """The owner doesn't need it any more: it's no longer open and its message is deleted. True if it was open."""
    with _locked() as data:
        if data.pop(key, None) is None:
            return False
    _send(delete_key=f"ask:{key}")
    return True


def open_asks() -> dict:
    with _locked() as data:
        return dict(data)


def status_lines() -> str:
    """For get_status: what programs are waiting on Sanjar for right now (each withdraws its own)."""
    data = open_asks()
    if not data:
        return ""
    return "\n".join(["Waiting on Sanjar right now (each program takes its own request back when it's done):"]
                     + [f"- {a['owner']}: {a['text']}" for a in data.values()])
