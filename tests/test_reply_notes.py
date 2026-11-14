"""Ticket 8da9f61b: a chat answer reaches the phone's notifications even if he left the chat or the app."""
import asyncio

import pytest
from starlette.testclient import TestClient

from core import appserver

AUTH = {"Authorization": "Bearer home-token"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(appserver, "REPLIES", tmp_path / "replies.json")
    monkeypatch.setattr(appserver, "SESSIONS", tmp_path / "sessions.json")
    monkeypatch.setenv("APP_TOKEN", "home-token")

    class FakeBrain:
        session_id = "s"
        lock = asyncio.Lock()

        async def reply(self, text):
            return "Here's the plan."

    monkeypatch.setattr(appserver, "_brain", lambda chat: FakeBrain())
    return TestClient(appserver.app)


def test_answers_show_up_in_the_feed(app):
    chat = app.post("/api/chats", json={"project": "General", "title": "Trip"}, headers=AUTH).json()
    feed = app.get("/api/notifications", headers=AUTH).json()
    assert feed["replies"] == []
    answer = app.post(f"/api/chats/{chat['id']}/messages", json={"text": "plan my trip"}, headers=AUTH).json()
    replies = app.get("/api/notifications", headers=AUTH).json()["replies"]
    assert replies == [{"chat": chat["id"], "chat_title": "Trip", "chat_project": "General",
                        "text": "Here's the plan.", "ts": answer["ts"]}]
    assert app.get(f"/api/notifications?after={answer['ts']}", headers=AUTH).json()["replies"] == []


def test_feed_keeps_only_recent(app, monkeypatch):
    monkeypatch.setattr(appserver, "KEEP_REPLIES", 3)
    chat = appserver.new_chat("General", "T")
    for i in range(5):
        appserver.note_reply(chat, {"text": f"a{i}", "ts": float(i + 1)})
    assert [n["text"] for n in appserver.replies_since(0)] == ["a2", "a3", "a4"]


def test_broken_file_is_no_problem(app):
    appserver.REPLIES.write_text("{nope")
    assert appserver.replies_since(0) == []
    appserver.note_reply(appserver.new_chat("General", "T"), {"text": "x", "ts": 1.0})
    assert len(appserver.replies_since(0)) == 1


def test_answers_in_missing_chats_are_left_out(app):
    appserver.note_reply({"id": "gone12345678", "title": "Test", "project": "General"}, {"text": "Moved it.", "ts": 5.0})
    chat = app.post("/api/chats", json={"project": "General", "title": "Real"}, headers=AUTH).json()
    appserver.note_reply({"id": chat["id"], "title": "Real", "project": "General"}, {"text": "real one", "ts": 6.0})
    assert [n["text"] for n in appserver.replies_since(0)] == ["real one"]


def test_tests_never_touch_the_real_feed():
    from core.config import STATE
    assert appserver.REPLIES != STATE / "app_replies.json"
