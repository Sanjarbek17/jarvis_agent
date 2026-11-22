"""Ticket 9a3d81f1: multi-device read state sync to prevent notification spam."""
import asyncio
import pytest
from starlette.testclient import TestClient

from core import appserver

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(appserver, "REPLIES", tmp_path / "replies.json")
    monkeypatch.setattr(appserver, "READS", tmp_path / "reads.json")
    monkeypatch.setattr(appserver, "SESSIONS", tmp_path / "sessions.json")
    monkeypatch.setenv("APP_TOKEN", "test-token")

    class FakeBrain:
        session_id = "test-session"
        lock = asyncio.Lock()

        async def reply(self, text):
            return "Test response"

    monkeypatch.setattr(appserver, "_brain", lambda chat: FakeBrain())
    return TestClient(appserver.app)


def test_save_and_load_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(appserver, "READS", tmp_path / "reads.json")
    chat = appserver.new_chat("General", "Test")

    # Initially empty
    assert appserver.load_reads() == {}

    # Save read state
    appserver.save_read(chat["id"], 100.0)
    assert appserver.load_reads() == {chat["id"]: 100.0}
    reloaded_chat = appserver.load_chat(chat["id"])
    assert reloaded_chat.get("last_read") == 100.0

    # Monotonic: older timestamp does not regress
    appserver.save_read(chat["id"], 50.0)
    assert appserver.load_reads()[chat["id"]] == 100.0

    # Newer advances
    appserver.save_read(chat["id"], 150.0)
    assert appserver.load_reads()[chat["id"]] == 150.0


def test_chat_read_endpoint(app):
    chat = app.post("/api/chats", json={"project": "General", "title": "Chat1"}, headers=AUTH).json()
    res = app.post(f"/api/chats/{chat['id']}/read", json={"ts": 200.5}, headers=AUTH)
    assert res.status_code == 200
    assert res.json() == {"ok": True, "chat": chat["id"], "last_read": 200.5}

    # Summary includes last_read
    summary = app.get("/api/chats?project=General", headers=AUTH).json()
    c = next(x for x in summary if x["id"] == chat["id"])
    assert c["last_read"] == 200.5


def test_reading_messages_auto_marks_read(app):
    chat = app.post("/api/chats", json={"project": "General", "title": "Chat2"}, headers=AUTH).json()
    ans = app.post(f"/api/chats/{chat['id']}/messages", json={"text": "Hello"}, headers=AUTH).json()
    assert ans["ts"] > 0

    # Fetching messages (opening the chat on device 1) marks it read
    msgs = app.get(f"/api/chats/{chat['id']}/messages", headers=AUTH).json()
    assert len(msgs) >= 2
    last_ts = msgs[-1]["ts"]

    reads = appserver.load_reads()
    assert reads[chat["id"]] == last_ts


def test_multi_device_notifications_suppressed_if_already_read(app):
    chat = app.post("/api/chats", json={"project": "General", "title": "Chat3"}, headers=AUTH).json()
    ans = app.post(f"/api/chats/{chat['id']}/messages", json={"text": "Question"}, headers=AUTH).json()
    ans_ts = ans["ts"]

    # Before device 1 reads: device 2 would see the reply in notifications
    feed1 = app.get(f"/api/notifications?after={ans_ts - 10}", headers=AUTH).json()
    assert any(r["chat"] == chat["id"] for r in feed1["replies"])

    # Device 1 reads the chat
    app.post(f"/api/chats/{chat['id']}/read", json={"ts": ans_ts}, headers=AUTH)

    # Device 2 now checks /api/notifications:
    # 1. Replies for chat3 are OMITTED because they are already read!
    # 2. reads dict is returned so device 2 can sync badges and clear local notifications!
    feed2 = app.get(f"/api/notifications?after={ans_ts - 10}", headers=AUTH).json()
    assert not any(r["chat"] == chat["id"] for r in feed2["replies"])
    assert feed2["reads"][chat["id"]] == ans_ts


def test_updates_chat_read_suppresses_pushed_notifications(app):
    first = appserver.push("Update 1")
    second = appserver.push("Update 2")

    feed = app.get("/api/notifications", headers=AUTH).json()
    assert len(feed["items"]) == 2

    # Device 1 reads updates chat up to second["ts"]
    app.post(f"/api/chats/{appserver.UPDATES}/read", json={"ts": second["ts"]}, headers=AUTH)

    # Device 2 polls /api/notifications: already-read items are suppressed!
    feed2 = app.get("/api/notifications", headers=AUTH).json()
    assert feed2["items"] == []
    assert feed2["reads"][appserver.UPDATES] == second["ts"]
