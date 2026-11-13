"""The shared Claude 5-hour usage window, told to Sanjar (ticket e6e75d74: "I should get warning for the claude
5 hours limit reaching and refreshed data").

Every Claude stream (workers, the Mac worker, Jarvis's brain) carries `rate_limit_event`s with the 5-hour window's
utilization and reset time. observe() reads them; at most one note per window and event:
- close: utilization reached WARN_AT (80%),
- hit: the window refused work (with the reset time),
- reset: the reset time passed after a hit (checked by the runner every loop): work goes on.
Notes go straight to him (Telegram + app), not through the brain, so they're never held back.
"""
import json
import logging
import time
from typing import Awaitable, Callable

from .config import STATE

log = logging.getLogger("jarvis.usagewatch")
FILE = STATE / "usage_watch.json"     # {"window", "util", "warned", "hit", "reset_due"}
WARN_AT = 0.8


def _load() -> dict:
    try:
        return json.loads(FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(s: dict) -> None:
    FILE.write_text(json.dumps(s))


def _at(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def five_hour(info: dict) -> dict:
    """{utilization, resetsAt, status} of the 5-hour window from one rate_limit_info, or {}."""
    win = ((info or {}).get("unifiedWindows") or {}).get("five_hour") or {}
    if not win and (info or {}).get("rateLimitType") == "five_hour":
        win = {k: info.get(k) for k in ("utilization", "resetsAt", "status")}
    return win if win.get("resetsAt") else {}


def observe(info: dict) -> list[str]:
    """Notes to send now for this rate_limit_info (usually none)."""
    win = five_hour(info)
    if not win:
        return []
    window = int(float(win["resetsAt"]))
    util = float(win.get("utilization") or 0)
    rejected = win.get("status") == "rejected" or (info.get("status") == "rejected" and info.get("rateLimitType") == "five_hour")
    s, notes = _load(), []
    s.update(window=window, util=util, seen_at=time.time())
    if rejected and s.get("hit") != window:
        s.update(hit=window, warned=window, reset_due=window)
        notes.append(f"⛔ The Claude 5-hour limit is used up. Workers pause and carry on by themselves after {_at(window)}.")
    elif not rejected and util >= WARN_AT and s.get("warned") != window and s.get("hit") != window:
        s["warned"] = window
        notes.append(f"⚠️ Claude's 5-hour usage is at {util:.0%}. It resets at {_at(window)}.")
    _save(s)
    for n in notes:
        log.info("usage note: %s", n)
    return notes


def check_reset(now: float | None = None) -> list[str]:
    """After a hit: once its reset time has passed, one note that work goes on."""
    now = time.time() if now is None else now
    s = _load()
    due = s.get("reset_due")
    if not due or now < due:
        return []
    s.pop("reset_due", None)
    _save(s)
    return ["✅ The Claude 5-hour limit has reset. Workers are running again."]


sender: Callable[[str], Awaitable] | None = None      # set by jarvis.py: straight to Sanjar (Telegram + app)


async def _send(notes: list[str]) -> None:
    for n in notes:
        if sender:
            try:
                await sender(n)
            except Exception:
                log.exception("couldn't send a usage note")


async def report(info: dict) -> None:
    """observe() and send what it says."""
    await _send(observe(info))


async def report_reset() -> None:
    await _send(check_reset())


def recently_hit(window_s: float = 6 * 3600) -> bool:
    """He was already told the 5-hour limit is used up (so the runner's own pause note isn't sent again)."""
    s = _load()
    return bool(s.get("hit")) and s["hit"] > time.time() - window_s


def status_line() -> str:
    """For get_status: the last reading of the 5-hour window."""
    s = _load()
    if not s.get("window"):
        return ""
    if time.time() > s["window"]:
        return "Claude 5-hour usage: the last window has reset."
    return f"Claude 5-hour usage: {float(s.get('util') or 0):.0%} (read {_at(s.get('seen_at', 0))}), resets at {_at(s['window'])}."
