"""Ticket d59ccf5e: who spent how much, from the Flatmates expense topic. Short numbers mean thousands of so'm."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from core import expenses, tools


@pytest.mark.parametrize("line,want", [
    ("10 tok", [10_000]), ("15 suv non", [15_000]), ("kv 63", [63_000]), ("63 kv", [63_000]),   # real lines from the group
    ("150 go'sht", [150_000]), ("25000 non", [25_000]), ("25 000 non", [25_000]), ("25,000", [25_000]),
    ("25k", [25_000]), ("25 ming non", [25_000]), ("3 mln kvartira", [3_000_000]), ("1.5 mln", [1_500_000]),
    ("2 ta non 30", [30_000]),                                                # "2 ta" is a count
    ("10 tok 15 suv", [10_000, 15_000]),                                      # two amounts: listed, not guessed
    ("non oldim", []), ("ertaga pul beraman", []), ("0", []),
])
def test_amounts(line, want):
    assert expenses.amounts(line) == want


ROWS = [{"name": "Sanjar", "text": "10 tok", "ts": 1794100000}, {"name": "Bahodir", "text": "15 suv non", "ts": 1794103000},
        {"name": "Abdumannon", "text": "kv 63", "ts": 1794106000}, {"name": "Sanjar", "text": "20 gaz", "ts": 1794109000},
        {"name": "Bahodir", "text": "non oldim", "ts": 1794110000}, {"name": "Sanjar", "text": "10 tok 5 suv", "ts": 1794111000}]


def test_totals_per_person_and_unclear_lines_are_not_counted():
    s = expenses.summarize(ROWS)
    assert s["people"]["Sanjar"]["total"] == 30_000 and s["people"]["Bahodir"]["total"] == 15_000
    assert s["people"]["Abdumannon"]["total"] == 63_000 and s["total"] == 108_000 and s["counted"] == 4
    assert s["unclear"] == ["Bahodir: non oldim", "Sanjar: 10 tok 5 suv"]


def test_report_lists_biggest_first_with_each_line_and_what_to_check():
    out = expenses.report(ROWS, "Expenses")
    assert out.index("Abdumannon: 63 000") < out.index("Sanjar: 30 000") < out.index("Bahodir: 15 000")
    assert "  10 tok" in out and "Total: 108 000 so'm" in out
    assert "Not counted, please check (2):" in out and "  Bahodir: non oldim" in out
    assert "no messages" in expenses.report([], "Expenses")


class FakeTg:
    def __init__(self, messages, topics=("new flat options", "Xarajatlar")):
        self.messages, self.sent, self.asked = messages, [], {}
        self.topics = [NS(id=10 + i, title=t) for i, t in enumerate(topics)]

    async def get_input_entity(self, peer):
        return peer

    async def __call__(self, request):
        return NS(topics=self.topics)

    async def iter_messages(self, peer, reply_to=None, limit=None):
        self.asked = {"peer": peer, "reply_to": reply_to, "limit": limit}
        for m in self.messages:
            yield m

    async def send_message(self, to, text, **kw):
        self.sent.append((to, text))


def msg(text, sender_id=5, name="Bahodir", out=False, fwd=None, day=8):
    async def get_sender():
        return NS(first_name=name)
    return NS(message=text, sender_id=sender_id, out=out, fwd_from=fwd, get_sender=get_sender,
              date=datetime(2026, 11, day, tzinfo=timezone.utc))


def test_reads_only_that_topic_from_people_oldest_first():
    tg = FakeTg([msg("15 suv non", day=8), msg("10 tok", expenses.OWNER_ID, "Sanjarbek", day=7), msg("Noted", out=True),
                 msg("99 forwarded", fwd=object()), msg("")])
    rows, title = asyncio.run(expenses.read(tg))
    assert title == "Xarajatlar" and tg.asked["reply_to"] == 11 and tg.asked["peer"] == expenses.FLATMATES_GROUP_ID
    assert [(r["name"], r["text"]) for r in rows] == [("Sanjar", "10 tok"), ("Bahodir", "15 suv non")]


def test_no_expense_topic_says_which_there_are():
    with pytest.raises(LookupError, match="new flat options"):
        asyncio.run(expenses.read(FakeTg([], topics=("new flat options",))))


def test_the_asked_for_report_goes_to_him_once_and_never_to_the_group(tmp_path, monkeypatch):
    monkeypatch.setattr(expenses, "REQUEST", tmp_path / "request")
    tg, copies = FakeTg([msg("15 suv non")]), []
    asyncio.run(expenses.send_if_asked(tg, 1, copies.append))
    assert tg.sent == []                                                     # nobody asked
    expenses.REQUEST.write_text("")
    asyncio.run(expenses.send_if_asked(tg, 1, copies.append))
    assert len(tg.sent) == 1 and tg.sent[0][0] == 1 and "Bahodir: 15 000" in tg.sent[0][1] and "Bahodir: 15 000" in copies[0]
    assert not expenses.REQUEST.exists()
    asyncio.run(expenses.send_if_asked(tg, 1, copies.append))
    assert len(tg.sent) == 1


def test_a_failed_read_tells_him_why(tmp_path, monkeypatch):
    monkeypatch.setattr(expenses, "REQUEST", tmp_path / "request")
    expenses.REQUEST.write_text("")
    tg = FakeTg([], topics=("new flat options",))
    asyncio.run(expenses.send_if_asked(tg, 1))
    assert "couldn't read" in tg.sent[0][1] and "no expense topic" in tg.sent[0][1]


def test_tool_is_for_him_not_for_guests_or_the_group(monkeypatch):
    def names(source):
        return {t.name for t in asyncio.run(tools.make_server(source).list_tools())}
    assert "flatmates_expenses" in names("Telegram") and "flatmates_expenses" not in names("Guest") | names("Group")

    async def fake():
        return "Sanjar: 10 000"
    monkeypatch.setattr(tools.hooks, "expenses", fake, raising=False)
    r = asyncio.run(tools.make_server("Telegram").call_tool("flatmates_expenses", {}))
    assert r.content[0].text == "Sanjar: 10 000"
