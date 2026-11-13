"""Answers in progress (ticket 11ff3b8a): Sanjar's app messages at 02:08 and 03:22 on 13 Nov never got a reply,
because Jarvis was restarted while the brain was still answering them. Each chat answer leaves a marker file
here while it runs; scripts/restart_when_idle.sh waits while a fresh one exists, and at start the markers of
the old process are cleared (whatever they were answering was lost: appserver.tell_lost_answers says so)."""
import contextlib
import time

from .config import STATE

DIR = STATE / "answering"
STALE = 20 * 60          # a marker older than this is a stuck answer, not one to wait for


@contextlib.contextmanager
def mark(key: str):
    DIR.mkdir(parents=True, exist_ok=True)
    f = DIR / f"{''.join(c for c in key if c.isalnum() or c in '-_')[:60] or 'x'}-{time.time_ns()}"
    f.write_text(str(time.time()))
    try:
        yield
    finally:
        f.unlink(missing_ok=True)


def busy() -> bool:
    return any(time.time() - p.stat().st_mtime < STALE for p in DIR.glob("*")) if DIR.exists() else False


def clear() -> None:
    """At start: the old process's answers are gone with it."""
    for p in DIR.glob("*") if DIR.exists() else []:
        p.unlink(missing_ok=True)
