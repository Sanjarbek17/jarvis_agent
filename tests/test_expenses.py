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
    ("10 tok 15 suv", [10_000, 15_000]), ("14, 10", [14_000, 10_000]), ("26, 15", [26_000, 15_000]),   # all count
    ("non oldim", []), ("ertaga pul beraman", []), ("0", []),
])
def test_amounts(line, want):
    assert expenses.amounts(line) == want


ROWS = [{"name": "Sanjar", "text": "10 tok", "ts": 1794100000}, {"name": "Bahodir", "text": "15 suv non", "ts": 1794103000},
        {"name": "Abdumannon", "text": "kv 63", "ts": 1794106000}, {"name": "Sanjar", "text": "20 gaz", "ts": 1794109000},
        {"name": "Bahodir", "text": "non oldim", "ts": 1794110000}, {"name": "Sanjar", "text": "10 tok 5 suv", "ts": 1794111000}]


def test_totals_per_person_and_unclear_lines_are_not_counted():
    s = expenses.summarize(ROWS)
    assert s["people"]["Sanjar"]["total"] == 45_000 and s["people"]["Bahodir"]["total"] == 15_000
    assert s["people"]["Abdumannon"]["total"] == 63_000 and s["total"] == 123_000 and s["counted"] == 5
    assert s["unclear"] == ["Bahodir: non oldim"]
    assert "10 tok 5 suv = 15 000" in s["people"]["Sanjar"]["lines"]


def test_report_lists_biggest_first_with_each_line_and_what_to_check():
    out = expenses.report(ROWS, "Expenses")
    assert out.index("Abdumannon: 63 000") < out.index("Sanjar: 45 000") < out.index("Bahodir: 15 000")
    assert "  10 tok" in out and "Total: 123 000 so'm" in out
    assert "Not counted, please check (1):" in out and "  Bahodir: non oldim" in out
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


def msg(text, sender_id=5, name="Bahodir", out=False, fwd=None, day=8, id=1, **media):
    async def get_sender():
        return NS(first_name=name)
    return NS(message=text, sender_id=sender_id, out=out, fwd_from=fwd, get_sender=get_sender, id=id,
              date=datetime(2026, 11, day, tzinfo=timezone.utc), **media)


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


@pytest.mark.parametrize("title", ["Expences", "Expenses", "expense", "Expanses", "Xarajatlar", "Расходы"])
def test_the_topic_is_found_however_it_is_spelled(title):
    tg = FakeTg([], topics=("New flat options", title, "General"))             # his group's real topics
    assert asyncio.run(expenses.find_topic(tg)) == (11, title)


def test_other_topics_are_not_taken_for_it():
    assert not any(expenses.TOPIC.search(t) for t in ("New flat options", "General", "Viewings"))


# --- checks: photos and files ---------------------------------------------------------------------------

DAY = 86400
T = 1794100000


def test_a_check_counts_for_who_posted_it():
    s = expenses.summarize([{"name": "Ikrom", "text": "", "ts": T, "check": {"total": 57_900, "what": "Korzinka"}}])
    assert s["total"] == 57_900 and s["people"]["Ikrom"]["lines"] == ["check 57 900 Korzinka"] and s["unclear"] == []


@pytest.mark.parametrize("check_first", [True, False])
def test_a_check_for_money_already_written_counts_once(check_first):
    line = {"name": "Ikrom", "text": "58", "ts": T + 60}
    check = {"name": "Ikrom", "text": "", "ts": T, "check": {"total": 57_900, "what": ""}}
    s = expenses.summarize([check, line] if check_first else [line, check])
    assert s["total"] == 58_000 and s["people"]["Ikrom"]["total"] == 58_000
    assert any("counted once" in l for l in s["people"]["Ikrom"]["lines"])


def test_same_money_from_someone_else_or_another_day_is_separate():
    check = {"name": "Ikrom", "text": "", "ts": T, "check": {"total": 58_000, "what": ""}}
    assert expenses.summarize([{"name": "Bahodir", "text": "58", "ts": T}, check])["total"] == 116_000
    assert expenses.summarize([{"name": "Ikrom", "text": "58", "ts": T + 3 * DAY}, check])["total"] == 116_000


def test_one_written_line_only_cancels_one_check():
    check = {"name": "Ikrom", "text": "", "ts": T, "check": {"total": 58_000, "what": ""}}
    assert expenses.summarize([{"name": "Ikrom", "text": "58", "ts": T}, check, dict(check)])["total"] == 116_000


def test_unreadable_files_are_listed_not_counted():
    s = expenses.summarize([{"name": "Ikrom", "text": "bozor", "ts": T, "unread": "a check I couldn't read just now"}])
    assert s["total"] == 0 and s["unclear"] == ["Ikrom: a check I couldn't read just now (bozor)"]


@pytest.fixture
def checks(tmp_path, monkeypatch):
    monkeypatch.setattr(expenses, "CHECKS", tmp_path / "checks.json")
    monkeypatch.setattr(expenses, "FILES", tmp_path / "files")
    calls = []

    async def model(path):
        calls.append(path.name)
        return {"check": True, "total_som": 57900, "what": "Korzinka"}

    monkeypatch.setattr(expenses, "ask_model", model)
    return calls


class Downloads(FakeTg):
    async def download_media(self, m, file=None):
        open(file, "wb").write(b"x")
        return file


PDF = NS(mime_type="application/pdf", size=1000, attributes=[NS(file_name="check.pdf")])
SHEET = NS(mime_type="application/vnd.ms-excel", size=1000, attributes=[NS(file_name="list.xls")])


def test_photos_and_pdfs_are_read_once_and_remembered(checks):
    tg = Downloads([msg("", id=2, photo=object()), msg("", id=3, document=PDF)])
    rows, _ = asyncio.run(expenses.read(tg))
    assert [r["check"]["total"] for r in rows] == [57_900, 57_900] and sorted(checks) == ["2.jpg", "3.pdf"]
    asyncio.run(expenses.read(tg))
    assert len(checks) == 2                                                  # the second report doesn't read them again


def test_a_forwarded_check_counts_a_forwarded_line_doesnt(checks):
    rows, _ = asyncio.run(expenses.read(Downloads([msg("", id=2, photo=object(), fwd=object()), msg("99 x", id=3, fwd=object())])))
    assert len(rows) == 1 and rows[0]["check"]["total"] == 57_900


def test_a_number_in_the_caption_wins_and_the_picture_isnt_read(checks):
    rows, _ = asyncio.run(expenses.read(Downloads([msg("35 gaz", id=2, photo=object())])))
    assert checks == [] and "check" not in rows[0] and expenses.summarize(rows)["total"] == 35_000


def test_other_files_and_stickers(checks):
    rows, _ = asyncio.run(expenses.read(Downloads([msg("", id=2, document=SHEET), msg("", id=3, document=PDF, sticker=object())])))
    assert len(rows) == 1 and rows[0]["unread"] == "a file I can't read: list.xls" and checks == []


def test_not_a_check_is_listed_and_a_failure_is_tried_again(tmp_path, monkeypatch, checks):
    async def not_check(path):
        return {"check": False, "total_som": None, "what": ""}

    monkeypatch.setattr(expenses, "ask_model", not_check)
    rows, _ = asyncio.run(expenses.read(Downloads([msg("", id=2, photo=object())])))
    assert "isn't a check" in rows[0]["unread"]

    async def limit(path):
        raise RuntimeError("usage limit")

    monkeypatch.setattr(expenses, "ask_model", limit)
    rows, _ = asyncio.run(expenses.read(Downloads([msg("", id=9, photo=object())])))
    assert "couldn't read just now" in rows[0]["unread"] and "9" not in expenses._kept()
