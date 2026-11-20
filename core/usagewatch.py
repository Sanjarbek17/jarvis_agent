"""The shared Claude and Gemini usage windows, told to Sanjar (tickets e6e75d74, 0965df43).

Tracks 5-hour and weekly limits across engines:
- Claude: rate_limit_event stream events (workers, Mac worker, brain)
- Gemini / Antigravity: local Language Server daemon quota summary (RetrieveUserQuotaSummary)

At most one note per window and event:
- close: utilization reached WARN_AT (80%),
- hit: the window refused work (with the reset time),
- reset: the reset time passed after a hit: work goes on.
Notes go straight to Sanjar (Telegram + app), not through the brain, so they're never held back.
"""
import asyncio
import contextlib
import json
import logging
import os
import ssl
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from .config import JARVIS_ENGINE, STATE

log = logging.getLogger("jarvis.usagewatch")
FILE = STATE / "usage_watch.json"     # {"window", "util", "warned", "hit", "reset_due", "gemini": {...}}
WARN_AT = 0.8
DAEMON_DIRS = [
    Path.home() / ".gemini" / "antigravity" / "daemon",
    Path.home() / ".gemini" / "antigravity-cli" / "daemon",
]


def _load() -> dict:
    try:
        return json.loads(FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(s: dict) -> None:
    FILE.write_text(json.dumps(s))


def _at(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


# --- Gemini quota extraction & live retrieval --------------------------------------------------

def parse_gemini_quota_summary(raw: dict) -> dict | None:
    """Parses RetrieveUserQuotaSummary response into a standardized structure."""
    if not isinstance(raw, dict):
        return None
    groups = (raw.get("response") or {}).get("groups") or raw.get("groups") or []
    gemini_group = next((g for g in groups if "gemini" in (g.get("displayName") or "").lower()), None)
    if not gemini_group and groups:
        gemini_group = groups[0]
    if not gemini_group:
        return None

    buckets = gemini_group.get("buckets") or []
    b5h = next((b for b in buckets if b.get("bucketId") == "gemini-5h" or b.get("window") == "5h"), None)
    bwk = next((b for b in buckets if b.get("bucketId") == "gemini-weekly" or b.get("window") == "weekly"), None)

    res = {}
    if b5h:
        rem = float(b5h.get("remainingFraction") if b5h.get("remainingFraction") is not None else 1.0)
        reset_ts = 0.0
        if b5h.get("resetTime"):
            try:
                reset_ts = datetime.fromisoformat(b5h["resetTime"].replace("Z", "+00:00")).timestamp()
            except Exception:
                reset_ts = 0.0
        res["gemini_5h"] = {
            "utilization": round(max(0.0, 1.0 - rem), 4),
            "remaining": round(rem, 4),
            "resets_at": reset_ts,
            "status": "rejected" if rem <= 0.0 else "allowed",
        }
    if bwk:
        rem = float(bwk.get("remainingFraction") if bwk.get("remainingFraction") is not None else 1.0)
        reset_ts = 0.0
        if bwk.get("resetTime"):
            try:
                reset_ts = datetime.fromisoformat(bwk["resetTime"].replace("Z", "+00:00")).timestamp()
            except Exception:
                reset_ts = 0.0
        res["gemini_weekly"] = {
            "utilization": round(max(0.0, 1.0 - rem), 4),
            "remaining": round(rem, 4),
            "resets_at": reset_ts,
        }
    return res if res else None


def fetch_gemini_quota() -> dict | None:
    """Reads live Gemini quota from the local Antigravity Language Server daemon.
    Returns parsed dict with gemini_5h and gemini_weekly, or None if unavailable."""
    hub_files = []
    for d in DAEMON_DIRS:
        if d.exists():
            hub_files.extend(d.glob("hub_*.json"))
    hub_files = sorted(hub_files, key=lambda p: p.stat().st_mtime, reverse=True)
    if not hub_files:
        return None

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for hf in hub_files:
        try:
            info = json.loads(hf.read_text())
            port = info.get("httpsPort")
            token = info.get("csrfToken")
            if not port or not token:
                continue
            url = f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
            req = urllib.request.Request(
                url, data=b"{}",
                headers={"Content-Type": "application/json", "x-codeium-csrf-token": token}
            )
            with urllib.request.urlopen(req, context=ctx, timeout=3) as resp:
                raw = json.loads(resp.read().decode())
                parsed = parse_gemini_quota_summary(raw)
                if parsed:
                    return parsed
        except Exception:
            continue
    return None


# --- Claude quota extraction -------------------------------------------------------------------

def five_hour(info: dict) -> dict:
    """{utilization, resetsAt, status} of the 5-hour window from one rate_limit_info, or {}."""
    win = ((info or {}).get("unifiedWindows") or {}).get("five_hour") or {}
    if not win and (info or {}).get("rateLimitType") == "five_hour":
        win = {k: info.get(k) for k in ("utilization", "resetsAt", "status")}
    return win if win.get("resetsAt") else {}


# --- Observing quota events --------------------------------------------------------------------

def observe_gemini(data: dict) -> list[str]:
    """Observe Gemini quota status and return notes to send (if any)."""
    if "response" in data or "groups" in data:
        data = parse_gemini_quota_summary(data) or {}
    g5h = data.get("gemini_5h") or {}
    if not g5h or not g5h.get("resets_at"):
        return []
    window = int(float(g5h["resets_at"]))
    util = float(g5h.get("utilization") or 0.0)
    rem = float(g5h.get("remaining") or max(0.0, 1.0 - util))
    rejected = g5h.get("status") == "rejected" or rem <= 0.0
    now = time.time()

    s = _load()
    gem = s.get("gemini") or {}
    gem.update(window=window, util=util, remaining=rem, seen_at=now, allowed=not rejected)
    if "gemini_weekly" in data:
        gwk = data["gemini_weekly"]
        gem.update(weekly_remaining=gwk.get("remaining"), weekly_resets_at=gwk.get("resets_at"))

    notes = []
    if not rejected and float(gem.get("reset_due") or 0) > now:
        gem["reset_due"] = now          # reset early: "work goes on" now

    if rejected and gem.get("hit") != window:
        gem.update(hit=window, warned=window, reset_due=window)
        notes.append(f"⛔ The Gemini 5-hour limit is used up. Workers pause and carry on by themselves after {_at(window)}.")
    elif not rejected and util >= WARN_AT and gem.get("warned") != window and gem.get("hit") != window:
        gem["warned"] = window
        notes.append(f"⚠️ Gemini's 5-hour usage is at {util:.0%}. It resets at {_at(window)}.")

    s["gemini"] = gem
    s["engine"] = "antigravity"
    if JARVIS_ENGINE == "antigravity" or not s.get("window"):
        s.update(engine="antigravity", window=window, util=util, remaining=rem, seen_at=now, allowed=not rejected)
        if "reset_due" in gem:
            s["reset_due"] = gem["reset_due"]
        elif "reset_due" in s:
            s.pop("reset_due", None)
        if "hit" in gem:
            s["hit"] = gem["hit"]
        if "warned" in gem:
            s["warned"] = gem["warned"]

    _save(s)
    for n in notes:
        log.info("gemini usage note: %s", n)
    return notes


def observe(info: dict) -> list[str]:
    """Notes to send now for this rate_limit_info (usually none)."""
    if not isinstance(info, dict):
        return []
    if "gemini_5h" in info or "groups" in info or "response" in info:
        return observe_gemini(info)

    win = five_hour(info)
    if not win:
        return []
    window = int(float(win["resetsAt"]))
    util = float(win.get("utilization") or 0)
    rejected = win.get("status") == "rejected" or (info.get("status") == "rejected" and info.get("rateLimitType") == "five_hour")
    s, notes = _load(), []
    now = time.time()
    s.update(window=window, util=util, seen_at=now, allowed=not rejected, engine="claude")
    if not rejected and float(s.get("reset_due") or 0) > now:
        s["reset_due"] = now          # reset early (e.g. Sanjar reset it): "work goes on" now, not at the old time
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
    notes = []

    # Check top-level / Claude reset
    due = s.get("reset_due")
    if due and now >= due:
        s.pop("reset_due", None)
        engine = s.get("engine", "claude")
        model = "Gemini" if engine == "antigravity" else "Claude"
        notes.append(f"✅ The {model} 5-hour limit has reset. Workers are running again.")

    # Check Gemini-specific reset
    gem = s.get("gemini") or {}
    gdue = gem.get("reset_due")
    if gdue and now >= gdue:
        gem.pop("reset_due", None)
        s["gemini"] = gem
        msg = "✅ The Gemini 5-hour limit has reset. Workers are running again."
        if msg not in notes:
            notes.append(msg)

    if notes:
        _save(s)
    return notes


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


async def report_gemini(data: dict) -> None:
    """observe_gemini() and send what it says."""
    await _send(observe_gemini(data))


async def report_reset() -> None:
    await _send(check_reset())


def allowed_since(ts: float) -> bool:
    """A reading taken after `ts` says model takes work again (ticket 59f4e775: the limit was reset early)."""
    s = _load()
    if JARVIS_ENGINE == "antigravity":
        gem = s.get("gemini") or {}
        if gem.get("allowed") is True and float(gem.get("seen_at") or 0) > ts:
            return True
    return s.get("allowed") is True and float(s.get("seen_at") or 0) > ts


PROBE_CMD = ["claude", "-p", "Reply with the single word: ok", "--model", "haiku", "--output-format", "stream-json",
             "--verbose", "--tools", "", "--setting-sources", "", "--no-session-persistence"]


async def probe(timeout: float = 90) -> bool:
    """One tiny check, only to read the live 5-hour window. While the runner holds
    work for the limit nothing else may call the model, so without this an early reset would go unnoticed."""
    if JARVIS_ENGINE == "antigravity":
        quota = await asyncio.to_thread(fetch_gemini_quota)
        if quota:
            await report_gemini(quota)
            return True
        # If fetch_gemini_quota is None (e.g. running in test), fall through to mockable PROBE_CMD
    try:
        proc = await asyncio.create_subprocess_exec(*PROBE_CMD, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except Exception as e:
        log.warning("usage probe failed: %r", e)
        with contextlib.suppress(Exception):
            proc.kill()
        return False
    got = False
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "rate_limit_event":
            await report(e.get("rate_limit_info") or {})
            got = True
    return got


def recently_hit(window_s: float = 6 * 3600) -> bool:
    """He was already told the 5-hour limit is used up (so the runner's own pause note isn't sent again)."""
    s = _load()
    gem = s.get("gemini") or {}
    if gem.get("hit") and gem["hit"] > time.time() - window_s:
        return True
    return bool(s.get("hit")) and s["hit"] > time.time() - window_s


def status_line(engine: str = "") -> str:
    """For get_status: the last reading of the 5-hour window."""
    s = _load()
    target_engine = (engine or s.get("engine") or JARVIS_ENGINE).lower()

    if target_engine in ("antigravity", "gemini") and (s.get("gemini") or not s.get("window")):
        gem = s.get("gemini") or {}
        win = gem.get("window") or s.get("window")
        if not win:
            quota = fetch_gemini_quota()
            if quota:
                observe_gemini(quota)
                s = _load()
                gem = s.get("gemini") or {}
                win = gem.get("window")
        if not win:
            return ""
        if time.time() > win:
            return "Gemini 5-hour usage: the last window has reset."
        util = float(gem.get("util") or s.get("util") or 0.0)
        seen = gem.get("seen_at") or s.get("seen_at", 0)
        return f"Gemini 5-hour usage: {util:.0%} (read {_at(seen)}), resets at {_at(win)}."

    if not s.get("window"):
        return ""
    if time.time() > s["window"]:
        return "Claude 5-hour usage: the last window has reset."
    return f"Claude 5-hour usage: {float(s.get('util') or 0):.0%} (read {_at(s.get('seen_at', 0))}), resets at {_at(s['window'])}."


def current_status() -> dict:
    """Returns structured limit data for the app and API."""
    s = _load()
    gem = s.get("gemini") or {}
    if not gem.get("window") and JARVIS_ENGINE == "antigravity":
        quota = fetch_gemini_quota()
        if quota:
            observe_gemini(quota)
            s = _load()
            gem = s.get("gemini") or {}

    res = {"engine": JARVIS_ENGINE}
    if gem.get("window"):
        now = time.time()
        win = gem["window"]
        rem = float(gem.get("remaining") if gem.get("remaining") is not None else max(0.0, 1.0 - float(gem.get("util") or 0.0)))
        util = float(gem.get("util") or max(0.0, 1.0 - rem))
        res["gemini"] = {
            "util": util,
            "remaining": rem,
            "resets_at": win,
            "resets_at_str": _at(win),
            "status": f"{rem:.0%} remaining · resets at {_at(win)}" if now < win else "Reset",
            "weekly_remaining": gem.get("weekly_remaining"),
            "weekly_resets_at": gem.get("weekly_resets_at"),
            "seen_at": gem.get("seen_at", 0),
        }
    if s.get("window") and JARVIS_ENGINE != "antigravity":
        win = s["window"]
        util = float(s.get("util") or 0.0)
        res["claude"] = {
            "util": util,
            "remaining": max(0.0, 1.0 - util),
            "resets_at": win,
            "resets_at_str": _at(win),
            "status": f"{util:.0%} used · resets at {_at(win)}" if time.time() < win else "Reset",
            "seen_at": s.get("seen_at", 0),
        }
    return res


def limit_summary() -> str:
    """A short one-line string for app waiting text or headers."""
    stat = current_status()
    if "gemini" in stat and JARVIS_ENGINE == "antigravity":
        g = stat["gemini"]
        return f"Gemini 5h: {g['remaining']:.0%} remaining · resets {g['resets_at_str']}"
    if "claude" in stat:
        c = stat["claude"]
        return f"Claude 5h: {c['util']:.0%} used · resets {c['resets_at_str']}"
    return ""
