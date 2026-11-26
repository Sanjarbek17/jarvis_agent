"""Live Instagram numbers for status questions (ticket 0960b065: "when I ask status you give live").

Jarvis had answered "status" in the Haydovchi darslari chat with the 21:30 check of the day before. Now, in code:
- `fetch(project)` reads followers and reel views right now (scripts/ig_stats.mjs, each account's own logged-in
  hidden Chrome), reusing a reading only while it is under MAX_AGE (15 min);
- `is_status()` spots a status question, and the app chat / Telegram paths fetch BEFORE the brain answers and hand
  it the numbers labelled with their time (`brief()`), so it never has to remember the rule;
- anything older (the twice-a-day checks with likes / saves) is shown only with its age.
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from .config import DEV, HERE, STATE

log = logging.getLogger("jarvis.livestats")
MAX_AGE = 15 * 60                # older than this is not "live"
PER_ACCOUNT_TIMEOUT = 120
AT_ONCE = 2                      # hidden Chromes at the same time (8 GB Mac)
CACHE = STATE / "live_stats.json"
SCRIPT = HERE / "scripts" / "ig_stats.mjs"
NODE = "/opt/homebrew/bin/node"
HAYDOVCHI = DEV / "side_project" / "projects" / "haydovchi_darslari"
CHANNELS = DEV / "side_project" / "projects" / "instagram_channels"
_locks: dict[str, asyncio.Lock] = {}

# a status question: in Uzbek, Russian or English
STATUS = re.compile(r"\b(status|statusi|holat\w*|statist\w*|stats?|views?|ko'?rish\w*|ko‘rish\w*|prosmotr\w*|"
                    r"followers?|obunachi\w*|podpischik\w*|просмотр\w*|статус\w*|статистик\w*|подписчик\w*|"
                    r"how many|nechta|qancha)\b", re.I)
# which project a message outside its project chat is about (Telegram, General); "instagram" alone = both
MENTIONS = {"Haydovchi darslari": re.compile(r"haydovchi|driving|yo'?l harakat", re.I),
            "Instagram Channels": re.compile(r"instagram channels|inglizcha|ingliz|english channel|rust|learn\.rust|"
                                             r"read\.classics|books? channel|kanallar", re.I)}
BOTH = re.compile(r"instagram|insta\b|reels?\b|kanal", re.I)


def accounts(project: str) -> list[dict]:
    """[{account, profile, own}] for a project with live stats, [] otherwise."""
    if project == "Haydovchi darslari":
        return [{"account": "haydovchi_darslari", "profile": str(HAYDOVCHI / "posting" / "chrome-profile"), "own": False}]
    if project == "Instagram Channels":
        try:
            chans = json.loads((CHANNELS / "channels.json").read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return [{"account": c["username"], "profile": str(CHANNELS / "accounts" / f"{key}-chrome"), "own": True}
                for key, c in chans.items() if c.get("username") and (CHANNELS / "accounts" / f"{key}-chrome").exists()]
    return []


PROJECTS = ("Haydovchi darslari", "Instagram Channels")


def is_status(text: str) -> bool:
    return bool(STATUS.search(text or ""))


def projects_of(text: str) -> list[str]:
    """The live-stats projects a message outside a project chat is about."""
    hits = [p for p, rx in MENTIONS.items() if rx.search(text or "")]
    return hits or (list(PROJECTS) if BOTH.search(text or "") else [])


async def brief_for(project: str, text: str) -> str:
    """The live-stats block to put in front of this message, or "" (not a status question / no live data here).
    project: the app chat's project ("" or General: worked out from the words)."""
    return (await live_for(project, text))[0]


async def live_for(project: str, text: str) -> tuple[str, dict | None]:
    """brief_for, and what the answer will be a report on (ticket 0a97efa8): {"about": [projects], "read_at": when
    the oldest of its numbers was read}, or None. The chat keeps it with the answer, so a later report on the same
    projects (or a day passing) shows this one as old data."""
    if not is_status(text):
        return "", None
    wanted = [project] if project in PROJECTS else projects_of(text)
    out, read = [], []
    for p in wanted:
        try:
            res = await asyncio.wait_for(fetch(p), 4 * PER_ACCOUNT_TIMEOUT)
            out.append(brief(res))
            read.append(res.get("at", time.time()))
        except Exception as e:
            log.exception("live stats for %s failed", p)
            out.append(f"[LIVE STATS for {p} could not be read just now ({e!s:.120}). Say so; don't present saved "
                       f"numbers as current: name their time.]\n")
    report = {"about": sorted(wanted), "read_at": min(read)} if wanted and read else None
    return "".join(out), report


def _load() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


async def _one(acc: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        cmd = [NODE, str(SCRIPT), "--profile", acc["profile"], "--account", acc["account"]] + (["--own-profile"] if acc["own"] else [])
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), PER_ACCOUNT_TIMEOUT)
            return json.loads(out.decode().strip().splitlines()[-1])
        except asyncio.TimeoutError:
            proc.kill()
            return {"account": acc["account"], "error": "timed out"}
        except Exception as e:
            return {"account": acc["account"], "error": str(e)[:200]}


async def fetch(project: str, max_age: float = MAX_AGE) -> dict:
    """{"project", "at" (epoch), "accounts": [...]}; reuses a reading younger than max_age."""
    hit = _load().get(project)
    if hit and time.time() - hit.get("at", 0) < max_age:
        return hit
    lock = _locks.setdefault(project, asyncio.Lock())
    async with lock:                               # a second status question waits for the same reading
        hit = _load().get(project)
        if hit and time.time() - hit.get("at", 0) < max_age:
            return hit
        accs = accounts(project)
        sem = asyncio.Semaphore(AT_ONCE)
        got = await asyncio.gather(*(_one(a, sem) for a in accs))
        res = {"project": project, "at": time.time(), "accounts": list(got)}
        data = _load()
        data[project] = res
        CACHE.write_text(json.dumps(data))
        log.info("live stats for %s: %s", project, ", ".join(
            f"{a['account']} {a.get('followers')}f" if not a.get("error") else f"{a['account']} error" for a in got))
        return res


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    return "just now" if m < 1 else f"{m} min ago" if m < 90 else f"{m // 60} h {m % 60} min ago"


def last_check() -> dict | None:
    """The newest Haydovchi autopilot check (likes, saves, shares), with its time: older, but has more."""
    files = sorted((HAYDOVCHI / "autopilot" / "state").glob("check-*.json"))
    if not files:
        return None
    try:
        data = json.loads(files[-1].read_text())
        at = datetime.strptime(files[-1].stem.split("-", 1)[1], "%Y%m%d%H%M%S").timestamp()
    except (ValueError, json.JSONDecodeError, OSError):
        return None
    return {"at": at, "data": data}


def text(res: dict) -> str:
    """The numbers, each block labelled with when it was read."""
    age = time.time() - res.get("at", 0)
    live = age < MAX_AGE
    head = (f"LIVE from Instagram, read at {time.strftime('%H:%M', time.localtime(res['at']))} ({_ago(age)})" if live
            else f"NOT LIVE: read at {time.strftime('%d %b %H:%M', time.localtime(res['at']))} ({_ago(age)}), say so")
    lines = [f"{res['project']}: {head}"]
    for a in res.get("accounts", []):
        if a.get("error"):
            lines.append(f"- @{a['account']}: couldn't read it now ({a['error'][:120]})")
            continue
        views = ", ".join(f"{r['views'] if r.get('views') is not None else '?'}" for r in a.get("reels", []))
        lines.append(f"- @{a['account']}: {a.get('followers')} followers; views of the newest reels (newest first): {views or 'none yet'}"
                     + "".join(f"\n  {r['url']} {r.get('views')} views" for r in a.get("reels", [])[:6]))
    if res["project"] == "Haydovchi darslari" and (c := last_check()):
        old = time.time() - c["at"]
        reels = c["data"].get("reels", [])[:3]
        lines.append(f"Older, only for likes/saves/shares: the autopilot check of {time.strftime('%d %b %H:%M', time.localtime(c['at']))} "
                     f"({_ago(old)}): " + "; ".join(f"{(r.get('first_line') or '')[:40]}: {r.get('likes')} likes, "
                                                   f"{r.get('saves')} saves, {r.get('shares')} shares" for r in reels))
    return "\n".join(lines)


def brief(res: dict) -> str:
    """What the app chat / Telegram path puts in front of a status question for the brain."""
    return ("[LIVE STATS, fetched by Jarvis's code just now for this status question (ticket 0960b065). Answer with "
            "THESE numbers and their time; never with saved check-*.json numbers as if they were current. Anything "
            "older than 15 min must be named with its time.]\n" + text(res) + "\n[/LIVE STATS]\n")
