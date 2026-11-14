"""Ticket 0a97efa8: an old report (stats, status) shows as old data in the app and website."""
import asyncio

import pytest
from starlette.testclient import TestClient

from core import appserver, livestats

AUTH = {"Authorization": "Bearer home-token"}
H, I = "Haydovchi darslari", "Instagram Channels"


def rep(about, read_at, ts):
    return {"role": "jarvis", "text": "numbers", "ts": ts, "report": {"about": about, "read_at": read_at}}


def test_newer_report_on_the_same_thing_makes_the_older_old():
    now = 10_000.0
    msgs = [rep([H], 1000, 1000), {"role": "me", "text": "status", "ts": 2000}, rep([H], 2000, 2000)]
    out = appserver.mark_stale(msgs, now)
    assert out[0]["stale"] is True and "stale" not in out[2] and "stale" not in out[1]


def test_reports_on_other_things_dont_touch_each_other():
    out = appserver.mark_stale([rep([H], 1000, 1000), rep([I], 2000, 2000)], 3000)
    assert not any(m.get("stale") for m in out)


def test_overlap_counts():
    out = appserver.mark_stale([rep([H], 1000, 1000), rep([H, I], 2000, 2000)], 3000)
    assert out[0]["stale"] and "stale" not in out[1]


def test_the_newest_goes_grey_after_a_day():
    day = appserver.REPORT_FRESH
    assert "stale" not in appserver.mark_stale([rep([H], 1000, 1000)], 1000 + day - 1)[0]
    assert appserver.mark_stale([rep([H], 1000, 1000)], 1000 + day + 1)[0]["stale"]


def test_plain_messages_and_odd_reports_untouched():
    msgs = [{"role": "jarvis", "text": "hi", "ts": 1}, {"role": "jarvis", "text": "x", "ts": 2, "report": "bad"}]
    assert appserver.mark_stale(msgs, 5) == msgs


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setenv("APP_TOKEN", "home-token")
    import time
    reads = iter([time.time() - 600, time.time() - 60])

    async def live_for(project, text):
        return ("[LIVE]", {"about": [H], "read_at": next(reads)}) if "status" in text else ("", None)

    class FakeBrain:
        session_id = "s"
        lock = asyncio.Lock()

        async def reply(self, text):
            return "Followers: 1200"

    monkeypatch.setattr(livestats, "live_for", live_for)
    monkeypatch.setattr(appserver, "_brain", lambda chat: FakeBrain())
    return TestClient(appserver.app)


def test_status_answers_are_tagged_and_the_older_shows_old(app):
    chat = app.post("/api/chats", json={"project": "General"}, headers=AUTH).json()["id"]
    url = f"/api/chats/{chat}/messages"
    app.post(url, json={"text": "status"}, headers=AUTH)
    app.post(url, json={"text": "thanks"}, headers=AUTH)
    first = app.get(url, headers=AUTH).json()
    assert first[1]["report"]["about"] == [H] and "stale" not in first[1] and "report" not in first[3]
    app.post(url, json={"text": "status again"}, headers=AUTH)
    msgs = app.get(url, headers=AUTH).json()
    assert msgs[1]["stale"] is True and "stale" not in msgs[-1] and msgs[-1]["report"]["read_at"] > msgs[1]["report"]["read_at"]


def test_telegram_and_daily_summary_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    appserver.telegram_said("jarvis", "Followers 10", {"about": [H], "read_at": 1.0})
    appserver.push("Today: 3 tickets done", report={"about": ["daily summary"], "read_at": 2.0})
    msgs = appserver.load_chat(appserver.UPDATES)["messages"]
    assert msgs[0]["report"]["about"] == [H] and msgs[1]["report"]["about"] == ["daily summary"]
