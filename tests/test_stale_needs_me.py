"""Ticket 733814dd: at 12:14 Jarvis asked Sanjar to unlock the Mac for a reel that had posted hours before. The
"Needs me" question was read off the board as if it were still true; nothing said how old it was, and Jarvis
couldn't see whether the Mac was locked. Now the age is shown, the live screen state is a fact, and the brain
and the self-check are told to check before passing a question on."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

from core import board, brain, guard, selfcheck
from core.board import Ticket


def needs(edited):
    return Ticket(page_id="local:x", ref="733814dd", project="Jarvis", title="guard", status="In progress",
                  jarvis="Needs me", note="please unlock the Mac", sprint="", url="", edited=edited)


def test_a_needs_me_line_says_how_old_the_question_is():
    old = (datetime.now(timezone.utc) - timedelta(hours=14)).isoformat()
    assert "asked 14 h ago" in needs(old).line() and "out of date" in needs(old).line()
    assert "asked" not in needs("").line()
    done = Ticket(page_id="", ref="r", project="P", title="t", status="", jarvis="Done", note="", sprint="", url="",
                  edited=old)
    assert "asked" not in done.line()
    assert board.asked_ago((datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()) == "40 min"
    assert board.asked_ago((datetime.now(timezone.utc) - timedelta(days=3)).isoformat()) == "3 days"


def test_screen_locked_reads_the_session(monkeypatch):
    import Quartz
    monkeypatch.setattr(Quartz, "CGSessionCopyCurrentDictionary", lambda: {"CGSSessionScreenIsLocked": 1})
    assert guard.screen_locked() is True
    monkeypatch.setattr(Quartz, "CGSessionCopyCurrentDictionary", lambda: {"kCGSSessionOnConsoleKey": True})
    assert guard.screen_locked() is False                     # the key is only there while locked
    monkeypatch.setattr(Quartz, "CGSessionCopyCurrentDictionary", lambda: 1 / 0)
    assert guard.screen_locked() is None


def test_the_brain_and_self_check_are_told_to_check_first():
    assert "true when it was written, not necessarily now" in open(brain.__file__).read()
    assert "only as current as when it was asked" in selfcheck.PROMPT


def test_self_check_gives_the_age_and_the_screen(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(hours=14)).isoformat()

    async def with_jarvis(state):
        return [needs(old)] if state == "Needs me" else []

    async def queued():
        return []

    monkeypatch.setattr(board, "with_jarvis", with_jarvis)
    monkeypatch.setattr(board, "queued", queued)
    monkeypatch.setattr(guard, "screen_locked", lambda: False)
    r = type("R", (), {"current": {}, "wait_reason": "", "pick": lambda self, q: []})()
    facts, items = asyncio.run(selfcheck.gather(r, lambda: True))
    assert "Mac screen right now: unlocked" in facts
    assert items == [("needs:733814dd", "Needs me (asked 14 h ago): 733814dd [Jarvis] guard — please unlock the Mac")]
