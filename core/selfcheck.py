"""Jarvis checks on himself every 15 minutes, with nobody asking (Sanjar, 2026-11-04: "have an interval
scheduled that you always check ... not watchdog"; ticket e2d3398a).

Each round first gathers the facts in plain code (cheap, no model call):
  - the runner: is its task alive, how old is its heartbeat; workers running vs queued; RAM and why
    a ticket waits
  - tickets in Needs me and Failed, tickets marked Working with no live worker
  - tickets with a Due date in the next hours that haven't started
A dead runner task is restarted on the spot. If anything *new* needs handling, the chat brain gets a
[SELF-CHECK] message and acts with its tools (answers Needs me itself per the standing "never ask him,
find a workaround" rule, re-queues what failed for a passing reason or lost its worker, makes sure
deadline tickets are queued), then writes Sanjar a short note of what changed or truly needs him, or
NO_REPLY. Items already handled aren't sent again for 12 h, so a quiet system costs nothing and
sends nothing.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

from . import board, ramguard
from . import runner as runner_mod
from .config import STATE

log = logging.getLogger("jarvis.selfcheck")
EVERY = 15 * 60
FIRST_AFTER = 120                 # first round shortly after start
REPEAT_AFTER = 12 * 3600          # an unresolved item is brought up again after this long
DEADLINE_SOON = 4 * 3600          # Due within this long and not started -> make sure it moves
HEARTBEAT_STALE = 10 * 60
SEEN = STATE / "selfcheck.json"   # item key -> last time it was handed to the brain

PROMPT = """[SELF-CHECK] Scheduled self-check (every 15 min; Sanjar didn't write this). Facts:
{facts}

New things to handle:
{items}

Act on them yourself with your tools, following Sanjar's standing rule: never ask him, find a workaround.
- Needs me: decide the answer yourself (memory, his rules, the project's docs) and give it with answer_task.
  For ids, tokens, keys and configs, look in his other projects under ~/dev first (.env files too) or
  have the worker create a new one; never repeat a secret's value. Leave it for him only if it truly
  can't be found or created: money, his own password, a code sent to his phone, publishing in his
  name, or merging to main.
- Failed: look with worker_progress. If the cause was passing (a restart, a usage or rate limit, the
  network), queue it again with queue_ticket; otherwise leave it and say why in one line.
- Working with no live worker: its worker is gone; queue it again with queue_ticket.
- A Due ticket that hasn't started: make sure it's queued (Due tickets go first).
Before leaving anything for him, check memory / recall and the recent chat: if he already answered or
did it, act on that instead of asking again. A question is only as current as when it was asked (its age is in
the line): check the facts now (the Mac screen line above, live_stats, worker_progress) and if what it waits
for already happened, answer_task with that instead of asking him.
Then write Sanjar a very short Telegram note: at most one line on what you did, and at most ONE thing
he himself must do, in plain words (no ids, no worker text, no internal steps). If nothing is worth
telling him, reply exactly NO_REPLY."""


def _load() -> dict:
    try:
        return json.loads(SEEN.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _due_in(t) -> float | None:
    """Seconds until the ticket's Due date (negative = overdue), or None."""
    if not getattr(t, "due", ""):
        return None
    try:
        d = datetime.fromisoformat(t.due.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:                  # a bare date: end of that day, local time
        d = d.replace(hour=23, minute=59).astimezone()
    return d.timestamp() - time.time()


async def gather(runner, runner_alive: Callable[[], bool]) -> tuple[list[str], list[tuple[str, str]]]:
    """(facts, items): facts are lines for context; items are (key, line) things that may need acting on."""
    facts, items = [], []
    hb = runner_mod.HEARTBEAT
    age = time.time() - hb.stat().st_mtime if hb.exists() else None
    facts.append(f"runner: {'alive' if runner_alive() else 'STOPPED'}, last loop "
                 f"{'never' if age is None else f'{age:.0f} s ago'}")
    live = {t.ref for t in runner.current.values()}
    facts.append("working now: " + (", ".join(f"{t.ref} [{p}]" for p, t in runner.current.items()) or "none"))
    queued = await board.queued()
    nxt = runner.pick(queued)
    facts.append(f"queued: {len(queued)}" + (f" (next: {nxt[0].ref})" if nxt else ""))
    from . import guard
    if (locked := guard.screen_locked()) is not None:
        facts.append(f"Mac screen right now: {'LOCKED' if locked else 'unlocked'}")
    facts.append(f"memory: {ramguard.describe()}" + (f"; {runner.wait_reason}" if runner.wait_reason else ""))
    if age is not None and age > HEARTBEAT_STALE and runner_alive():
        items.append(("runner-stale", f"the runner's loop hasn't ticked for {age / 60:.0f} min (its watchdog "
                                      "restarts Jarvis at 20 min)"))
    for t in await board.with_jarvis("Needs me"):
        age = board.asked_ago(getattr(t, "edited", ""))
        items.append((f"needs:{t.ref}", f"Needs me{f' (asked {age} ago)' if age else ''}: {t.ref} [{t.project}] "
                                        f"{t.title} — {t.note[:300]}"))
    for t in await board.with_jarvis("Failed"):
        items.append((f"failed:{t.ref}", f"Failed: {t.ref} [{t.project}] {t.title} — {t.note[:200]}"))
    for t in await board.with_jarvis("Working"):
        if t.ref not in live:
            items.append((f"lost:{t.ref}", f"Working with no live worker: {t.ref} [{t.project}] {t.title}"))
    for t in queued:
        left = _due_in(t)
        if left is not None and left < DEADLINE_SOON and t.ref not in live:
            items.append((f"due:{t.ref}", f"Due {'overdue' if left < 0 else f'in {left / 3600:.1f} h'} and not "
                                          f"started: {t.ref} [{t.project}] {t.title}"))
    return facts, items


async def run_once(runner, runner_alive, restart_runner, ask: Callable[[str], Awaitable[str]],
                   send: Callable[[str], Awaitable[None]]) -> str:
    """One self-check round; returns what was sent to Sanjar ("" if nothing)."""
    note = ""
    if not runner_alive():
        restart_runner()
        note = "The ticket runner had stopped; I restarted it."
        log.warning("self-check: runner task was dead, restarted it")
    facts, items = await gather(runner, runner_alive)
    seen, now = _load(), time.time()
    new = [(k, line) for k, line in items if now - seen.get(k, 0) > REPEAT_AFTER]
    seen = {k: v for k, v in seen.items() if k in {key for key, _ in items}}   # resolved items are forgotten
    if not new:
        SEEN.write_text(json.dumps(seen))
        if note:
            await send(note)
        log.info("self-check: nothing new (%s)", "; ".join(facts))
        return note
    for k, _ in new:
        seen[k] = now
    SEEN.write_text(json.dumps(seen))
    log.info("self-check: %d new item(s): %s", len(new), ", ".join(k for k, _ in new))
    reply = (await ask(PROMPT.format(facts="\n".join(f"- {f}" for f in facts),
                                     items="\n".join(f"- {line}" for _, line in new)))).strip()
    from .brain import is_silent
    text = "\n".join(p for p in (note, "" if is_silent(reply) else reply) if p)
    if text:
        await send(text)
    return text


async def run_forever(runner, runner_alive, restart_runner, ask, send) -> None:
    await asyncio.sleep(FIRST_AFTER)
    while True:
        try:
            await run_once(runner, runner_alive, restart_runner, ask, send)
        except Exception:
            log.exception("self-check failed")
        await asyncio.sleep(EVERY)
