"""Who spent how much, from the expense topic of the Flatmates group (ticket d59ccf5e). Only Sanjar sees this: it's a
tool for his chat and a one-off report, and nothing is ever posted in the group.

People write short lines there, like "10 tok", "15 suv non" or "kv 63": a short number means thousands of so'm, so
10 is 10,000. A number of 1,000 or more is taken as written, and "3 mln" as 3,000,000. "2 ta" is a count, not money.
A line with no number, or with several, isn't guessed at: it's listed for him to check, and only a line with
exactly one amount counts in the totals.

Read inside Jarvis, the only account in that group (a script must not open its session, see core/outbox.py).
"""
import logging
import re
import time
from collections import defaultdict

from .config import FLATMATES_GROUP_ID, OWNER_ID, VAR

log = logging.getLogger("jarvis.expenses")
TOPIC = re.compile(r"exp[ae]n[sc]|xarajat|harajat|xarj|rasxod|расход|затрат|chiqim", re.I)
REQUEST = VAR / "flatmates_expenses_request"      # exists: send him the report once, at the next start
LIMIT = 2000
_NOT_MONEY = re.compile(r"\b\d+\s*(ta|dona|шт|kishi|xona|qavat|etaj|kun|soat)\b", re.I)
_NUMBER = re.compile(r"(?<![\w.,])(\d{1,3}(?:[ .,]\d{3})+|\d+(?:[.,]\d+)?)\s*(mln|million|млн|ming|минг|тыс|k|к)?(?![\w])", re.I)


def amounts(text: str) -> list[int]:
    """The amounts of money in one line, in so'm."""
    out = []
    for digits, unit in _NUMBER.findall(_NOT_MONEY.sub(" ", text)):
        unit = unit.lower()
        whole = re.sub(r"[ .,]", "", digits) if re.fullmatch(r"\d{1,3}(?:[ .,]\d{3})+", digits) else digits.replace(",", ".")
        value = float(whole)
        if unit in ("mln", "million", "млн"):
            value *= 1_000_000
        elif unit or value < 1000:          # 25k, 25 ming, or just 25: thousands
            value *= 1000
        if value > 0:
            out.append(round(value))
    return out


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def summarize(rows: list[dict]) -> dict:
    """rows: {"name", "text", "ts"} oldest first. Returns people {name: {"total", "lines"}}, total and unclear lines."""
    people = defaultdict(lambda: {"total": 0, "lines": []})
    unclear, total, counted = [], 0, 0
    for r in rows:
        text = " ".join(r["text"].split())
        found = amounts(text)
        if len(found) == 1:
            people[r["name"]]["total"] += found[0]
            people[r["name"]]["lines"].append(text)
            total += found[0]
            counted += 1
        else:
            unclear.append(f"{r['name']}: {text}")
    return {"people": dict(people), "total": total, "counted": counted, "unclear": unclear}


def report(rows: list[dict], title: str = "") -> str:
    if not rows:
        return f"The {title or 'expense'} topic has no messages yet."
    s = summarize(rows)
    day = lambda ts: time.strftime("%d %b", time.localtime(ts))
    out = [f"{title or 'Expenses'}: {day(rows[0]['ts'])} to {day(rows[-1]['ts'])}, {len(rows)} messages (a short number = thousands of so'm)", ""]
    for name, p in sorted(s["people"].items(), key=lambda kv: -kv[1]["total"]):
        out.append(f"{name}: {money(p['total'])}")
        out += [f"  {line}" for line in p["lines"]]
        out.append("")
    out.append(f"Total: {money(s['total'])} so'm")
    if s["unclear"]:
        out += ["", f"Not counted, please check ({len(s['unclear'])}):"] + [f"  {line}" for line in s["unclear"]]
    return "\n".join(out)


async def find_topic(tg) -> tuple[int, str]:
    from telethon.tl.functions.messages import GetForumTopicsRequest
    r = await tg(GetForumTopicsRequest(peer=await tg.get_input_entity(FLATMATES_GROUP_ID), offset_date=None,
                                       offset_id=0, offset_topic=0, limit=100))
    titles = [getattr(t, "title", "") or "" for t in r.topics]
    for t in r.topics:
        if TOPIC.search(getattr(t, "title", "") or ""):
            return t.id, t.title
    raise LookupError(f"no expense topic among: {', '.join(titles)}")


async def read(tg) -> tuple[list[dict], str]:
    """The topic's messages as they are now (a deleted one is gone, an edited one counts as edited), oldest first."""
    topic, title = await find_topic(tg)
    rows, names = [], {}
    async for m in tg.iter_messages(FLATMATES_GROUP_ID, reply_to=topic, limit=LIMIT):
        text = (m.message or "").strip()
        if not text or m.out or getattr(m, "fwd_from", None):         # not Jarvis's, not passed on from elsewhere
            continue
        if m.sender_id not in names:
            sender = await m.get_sender()
            names[m.sender_id] = "Sanjar" if m.sender_id == OWNER_ID else (
                getattr(sender, "first_name", None) or getattr(sender, "title", None) or "someone")
        rows.append({"name": names[m.sender_id], "text": text, "ts": m.date.timestamp()})
    rows.reverse()
    return rows, title


async def text(tg) -> str:
    rows, title = await read(tg)
    return report(rows, title)


async def send_if_asked(tg, owner_id: int, also=lambda text: None) -> None:
    """At start: a report asked for while Jarvis couldn't read the group goes to him once."""
    if not REQUEST.exists():
        return
    try:
        out = await text(tg)
    except Exception as e:
        log.exception("expense report failed")
        out = f"I couldn't read the Flatmates expense topic: {e}"
    for start in range(0, len(out), 4000):
        await tg.send_message(owner_id, out[start:start + 4000], link_preview=False)
    also(out[:4000])
    REQUEST.unlink(missing_ok=True)
