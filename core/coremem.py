"""Jarvis's core memory: a small curated file that is ALWAYS in the brain's prompt (OpenClaw-style;
Sanjar 2026-11-04: "just writing a rule doesn't make it happen", ticket 35ec396e).

var/state/memory/CORE.md holds four sections of short dated lines, each with a key:
  Decisions · Preferences and rules · Facts · Open requests (things he asked for that aren't done)
A newer entry with the same key replaces the old one (superseding), so two contradicting lines can't
both stand. The chat and call brains get the whole file in their system prompt and restart with the
new one when it changes (core/brain.py), so a decision doesn't depend on a similarity search.

Who writes it:
  - the `remember` tool, right away (core/tools.py)
  - consolidate(): a model pass over the conversations since the last pass (core/memory.py items) that
    adds, updates and removes entries (open requests that got done go away). It runs nightly, at every
    Jarvis start (so a restart never loses what wasn't consolidated yet) and before /clear
    ("save before forgetting"). Only Sanjar's own words count: worker reports and self-checks are not
    in the chat log at all, and the pass is told to ignore runner/system text and secrets.
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path

from . import memory
from .config import STATE

log = logging.getLogger("jarvis.core")
CORE = STATE / "memory" / "CORE.md"
STATE_FILE = STATE / "memory" / "core_state.json"      # {"last_ts": newest chat item already consolidated}
SECTIONS = {"decision": "Decisions", "preference": "Preferences and rules", "fact": "Facts",
            "open": "Open requests"}
MAX_CHARS = 7000                 # what goes into the prompt; newest entries win if it's longer
MAX_ENTRIES = 90
MODEL = "claude-opus-5-5"        # once a day / per /clear: worth the best judgement
LINE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2})\] \(([\w.-]+)\) (.*)$")
_lock = asyncio.Lock()


def load() -> dict[str, dict]:
    """key -> {"kind", "date", "text"} from CORE.md."""
    out, kind = {}, "fact"
    if not CORE.exists():
        return out
    titles = {v: k for k, v in SECTIONS.items()}
    for line in CORE.read_text().splitlines():
        if line.startswith("## "):
            kind = titles.get(line[3:].strip(), kind)
        elif m := LINE.match(line.strip()):
            out[m.group(2)] = {"kind": kind, "date": m.group(1), "text": m.group(3).strip()}
    return out


def save(entries: dict[str, dict]) -> None:
    if len(entries) > MAX_ENTRIES:              # oldest go first; the vector memory still has them
        keep = sorted(entries.items(), key=lambda kv: kv[1]["date"])[-MAX_ENTRIES:]
        entries = dict(keep)
    lines = ["# Core memory (Sanjar's lasting decisions, rules, facts and open requests)", "",
             "Kept by Jarvis: a newer line with the same key replaces the old one. Edit freely.", ""]
    for kind, title in SECTIONS.items():
        rows = sorted((e["date"], k, e["text"]) for k, e in entries.items() if e["kind"] == kind)
        lines += [f"## {title}", ""] + [f"- [{d}] ({k}) {t}" for d, k, t in rows] + [""]
    CORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CORE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(CORE)


def slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:5]) or f"note-{int(time.time())}"


def put(text: str, kind: str = "fact", key: str = "", date: str = "") -> str:
    """Add or replace one entry; returns its key."""
    kind = kind if kind in SECTIONS else "fact"
    key = re.sub(r"[^\w.-]+", "-", (key or slug(text)).strip().lower()).strip("-")[:48]
    entries = load()
    entries[key] = {"kind": kind, "date": date or time.strftime("%Y-%m-%d"), "text": " ".join(text.split())[:400]}
    save(entries)
    return key


def drop(key: str) -> bool:
    entries = load()
    if entries.pop(key.strip(), None) is None:
        return False
    save(entries)
    return True


def text() -> str:
    """The block for the system prompt (empty if there's nothing yet)."""
    entries = load()
    if not entries:
        return ""
    body = CORE.read_text()
    if len(body) > MAX_CHARS:                  # newest first until it fits
        rows = sorted(entries.items(), key=lambda kv: kv[1]["date"], reverse=True)
        keep, size = {}, 0
        for k, e in rows:
            size += len(e["text"]) + 30
            if size > MAX_CHARS:
                break
            keep[k] = e
        body = "\n".join(f"- [{e['date']}] ({SECTIONS[e['kind']]}) {e['text']}" for k, e in
                         sorted(keep.items(), key=lambda kv: kv[1]["kind"]))
    return body


def version() -> int:
    return CORE.stat().st_mtime_ns if CORE.exists() else 0


PROMPT = """You maintain Jarvis's core memory for Sanjar: a short curated list that is always in Jarvis's prompt.
Current entries (key | kind | date | text):
{entries}

New conversation since the last update (oldest first; "Sanjar:" is him, "Jarvis:" is the assistant):
{talk}

Update the list from what SANJAR said:
- decision: something he decided ("use the +7 906 account for job messages", "reels post at 19:30")
- preference: how he wants things done, rules for Jarvis ("one item at a time", "never phone me")
- fact: a lasting fact about him, his projects, accounts or people (not secrets)
- open: something he asked Jarvis for that isn't done yet; remove it (in "remove") once it's done or
  he dropped it
Reuse an existing key to update or correct an entry (the newer statement wins); new keys are short
kebab-case. Write each text as one plain self-contained sentence. Ignore small talk, Jarvis's own
promises, runner/worker/system text, and never store passwords, tokens or codes. Keep it lean: only
what matters next week too. If nothing changes, return empty lists.

Reply with JSON only: {{"set": [{{"key": "...", "kind": "decision|preference|fact|open", "text": "..."}}], "remove": ["key", ...]}}"""


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


async def consolidate(max_chars: int = 40000) -> tuple[int, int]:
    """Fold the chats and calls since the last pass into CORE.md. Returns (set, removed)."""
    async with _lock:
        since = _state().get("last_ts", 0)
        items = [it for it in memory._load() if it["kind"] in ("chat", "call") and it["ts"] > since]
        if not items:
            return 0, 0
        items.sort(key=lambda it: it["ts"])
        lines, size = [], 0
        for it in reversed(items):                      # newest first until the budget, then oldest first
            line = f"[{time.strftime('%m-%d %H:%M', time.localtime(it['ts']))}] {it['text'][:700]}"
            size += len(line)
            if size > max_chars:
                break
            lines.append(line)
        lines.reverse()
        entries = load()
        listing = "\n".join(f"{k} | {e['kind']} | {e['date']} | {e['text']}" for k, e in entries.items()) or "(empty)"
        try:
            ans = await memory._ask_claude_json(PROMPT.format(entries=listing, talk="\n".join(lines)),
                                                model=MODEL, timeout=300)
        except Exception as e:
            log.warning("core memory consolidation failed: %r", e)
            return 0, 0
        today, n_set, n_rm = time.strftime("%Y-%m-%d"), 0, 0
        for e in ans.get("set", []) or []:
            if isinstance(e, dict) and str(e.get("text", "")).strip():
                key = re.sub(r"[^\w.-]+", "-", str(e.get("key") or slug(e["text"])).lower()).strip("-")[:48]
                kind = e.get("kind") if e.get("kind") in SECTIONS else "fact"
                old = entries.get(key)
                if not old or old["text"] != e["text"].strip() or old["kind"] != kind:
                    entries[key] = {"kind": kind, "date": today, "text": " ".join(str(e["text"]).split())[:400]}
                    n_set += 1
        for key in ans.get("remove", []) or []:
            if entries.pop(str(key), None) is not None:
                n_rm += 1
        save(entries)
        STATE_FILE.write_text(json.dumps({"last_ts": items[-1]["ts"], "at": time.time()}))
        log.info("core memory: %d set, %d removed (%d exchanges read)", n_set, n_rm, len(lines))
        return n_set, n_rm
