"""Ticket 733814dd / c2f2a3c7: at 12:14 Jarvis asked Sanjar to unlock the Mac for a reel that had posted hours before.
The root cause: the request was a static note copied onto a ticket, so the poster, the only code that knew whether it
still needed the screen, couldn't take it back. Now such requests are owned asks (core/asks.py): opened and withdrawn
by the program itself, their message deleted when withdrawn."""
import asyncio
from datetime import datetime, timedelta, timezone

from core import asks, board, outbox, selfcheck
from core.board import Ticket

import pytest


@pytest.fixture(autouse=True)
def own_asks(tmp_path, monkeypatch):
    monkeypatch.setattr(asks, "FILE", tmp_path / "asks.json")


def needs(edited):
    return Ticket(page_id="local:x", ref="733814dd", project="Jarvis", title="guard", status="In progress",
                  jarvis="Needs me", note="which colour?", sprint="", url="", edited=edited)


def test_a_needs_me_line_says_when_it_was_asked():
    old = (datetime.now(timezone.utc) - timedelta(hours=14)).isoformat()
    assert "asked 14 h ago" in needs(old).line()
    assert "asked" not in needs("").line()
    assert board.asked_ago((datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()) == "40 min"
    assert board.asked_ago((datetime.now(timezone.utc) - timedelta(days=3)).isoformat()) == "3 days"


def outbox_spy(monkeypatch, running=True):
    sent = []
    monkeypatch.setattr(outbox, "jarvis_running", lambda: running)
    monkeypatch.setattr(outbox, "send", lambda **kw: sent.append(kw) or {"ok": True})
    return sent


def test_an_ask_is_sent_once_and_its_message_goes_when_its_owner_withdraws_it(monkeypatch):
    sent = outbox_spy(monkeypatch)
    assert asks.open_ask("haydovchi:unlock:r11", "Haydovchi poster", "Unlock the Mac: reel r11 is waiting") is True
    assert asks.open_ask("haydovchi:unlock:r11", "Haydovchi poster", "Unlock the Mac: reel r11 is waiting") is False
    assert [s["text"] for s in sent] == ["Unlock the Mac: reel r11 is waiting"] and sent[0]["key"] == "ask:haydovchi:unlock:r11"
    assert "Haydovchi poster: Unlock the Mac" in asks.status_lines()
    assert asks.close_ask("haydovchi:unlock:r11") is True
    assert sent[-1] == {"wait": 30, "delete_key": "ask:haydovchi:unlock:r11"}
    assert asks.close_ask("haydovchi:unlock:r11") is False and len(sent) == 2   # closing twice: nothing more
    assert asks.status_lines() == "" and asks.open_asks() == {}


def test_asks_still_count_when_jarvis_is_down(monkeypatch):
    sent = outbox_spy(monkeypatch, running=False)
    asks.open_ask("k", "P", "t")
    assert sent == [] and "k" in asks.open_asks()


def test_get_status_lists_open_asks(monkeypatch):
    outbox_spy(monkeypatch)
    from core import tools
    from core.runner import Runner

    async def status_text(project=""):
        return "Jarvis has nothing queued or running."

    monkeypatch.setattr(tools.board, "status_text", status_text)
    tools.hooks.runner = Runner(lambda *a: None)
    asks.open_ask("haydovchi:unlock:r11", "Haydovchi poster", "Unlock the Mac")
    r = asyncio.run(tools.make_server("test").call_tool("get_status", {}))
    assert "Waiting on Sanjar right now" in str(r) and "Haydovchi poster: Unlock the Mac" in str(r)
    asks.close_ask("haydovchi:unlock:r11")
    r = asyncio.run(tools.make_server("test").call_tool("get_status", {}))
    assert "Waiting on Sanjar" not in str(r)


def test_workers_are_told_not_to_copy_a_programs_wait_into_needs_me():
    from core import runner
    t = Ticket(page_id="local:a", ref="a", project="Jarvis", title="t", status="", jarvis="", note="", sprint="", url="")
    assert "Never\n  for a state a program is waiting on" in runner._worker_prompt(t, runner.SHOTS)


def test_self_check_gives_the_age(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(hours=14)).isoformat()

    async def with_jarvis(state):
        return [needs(old)] if state == "Needs me" else []

    async def queued():
        return []

    monkeypatch.setattr(board, "with_jarvis", with_jarvis)
    monkeypatch.setattr(board, "queued", queued)
    r = type("R", (), {"current": {}, "wait_reason": "", "pick": lambda self, q: []})()
    _, items = asyncio.run(selfcheck.gather(r, lambda: True))
    assert items == [("needs:733814dd", "Needs me (asked 14 h ago): 733814dd [Jarvis] guard — which colour?")]
