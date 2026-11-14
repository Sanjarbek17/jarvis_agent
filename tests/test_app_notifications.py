"""Ticket e028892d: what Jarvis writes first also reaches the phone app (polled, no Google push)."""
import os

from starlette.testclient import TestClient

from core import appserver


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setenv("APP_TOKEN", "t0k")
    return TestClient(appserver.app), {"Authorization": "Bearer t0k"}


def test_push_lands_in_updates_chat_and_feed(tmp_path, monkeypatch):
    client, auth = _setup(tmp_path, monkeypatch)
    assert client.get("/api/notifications").status_code == 401
    assert client.get("/api/notifications", headers=auth).json()["items"] == []

    first = appserver.push("Ticket **done**: the login page works.")
    appserver.push("NO_REPLY")                       # silent replies are never pushed
    appserver.push("   ")
    second = appserver.push("Needs you: which colour?")

    feed = client.get("/api/notifications", headers=auth).json()
    assert feed["chat"] == appserver.UPDATES
    assert [m["text"] for m in feed["items"]] == [first["text"], second["text"]]
    later = client.get(f"/api/notifications?after={first['ts']}", headers=auth).json()["items"]
    assert [m["text"] for m in later] == [second["text"]]
    assert client.get("/api/notifications?after=junk", headers=auth).status_code == 200

    # the updates chat shows up under General like any other chat
    chats = client.get("/api/chats?project=General", headers=auth).json()
    assert chats[0]["id"] == appserver.UPDATES and chats[0]["title"] == "Updates from Jarvis"


def test_push_keeps_only_recent(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(appserver, "KEEP_UPDATES", 3)
    for i in range(5):
        appserver.push(f"u{i}")
    assert [m["text"] for m in appserver.load_chat(appserver.UPDATES)["messages"]] == ["u2", "u3", "u4"]


def test_reply_in_updates_chat_tells_brain_what_jarvis_wrote(tmp_path, monkeypatch):
    import asyncio
    _setup(tmp_path, monkeypatch)
    seen = []

    class FakeBrain:
        session_id = "s"
        lock = asyncio.Lock()

        async def reply(self, text):
            seen.append(text)
            return "ok"

    monkeypatch.setattr(appserver, "_brain", lambda chat: FakeBrain())
    appserver.push("Ticket X needs you: pick a colour.")
    chat = appserver.load_chat(appserver.UPDATES)
    asyncio.run(appserver.ask(chat, "blue"))
    assert "pick a colour" in seen[0] and seen[0].endswith("blue")
    asyncio.run(appserver.ask(appserver.load_chat(appserver.UPDATES), "thanks"))
    assert "pick a colour" not in seen[1]           # told once, not every turn
