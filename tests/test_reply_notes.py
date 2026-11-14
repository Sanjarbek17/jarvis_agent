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


def test_tests_use_a_temporary_data_folder():
    """Ticket 6ced33f0: the root cause was tests writing the real data folder; now they can't."""
    from core import paths
    from core.config import STATE
    real = paths.HERE / "var"
    assert paths.VAR != real and not str(appserver.REPLIES).startswith(str(real)) and not str(STATE).startswith(str(real))


def test_jarvis_refuses_the_real_folder_under_tests():
    from core import paths
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != "JARVIS_VAR"} | {"PYTEST_CURRENT_TEST": "x"}
    r = subprocess.run([sys.executable, "-c", "import core.paths"], cwd=paths.HERE, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "real data folder" in r.stderr
    env["JARVIS_VAR"] = os.environ["JARVIS_VAR"]
    assert subprocess.run([sys.executable, "-c", "import core.paths"], cwd=paths.HERE, env=env).returncode == 0


@pytest.mark.parametrize("text,shown", [
    ("Here's the plan.", "Here's the plan."),
    ("Login: inglizcha.oson.qoida, password: [secret]", "New message. Open the chat to read it."),
    ("Your parol is ready", "New message. Open the chat to read it."),
    ("The API key is in .env as GEMINI_API_KEY", "New message. Open the chat to read it."),
    ("Kod: 12345", "New message. Open the chat to read it."),
])
def test_previews_never_show_credentials(text, shown):
    assert appserver.preview(text) == shown


def test_feed_texts_are_previews(app):
    chat = app.post("/api/chats", json={"project": "General", "title": "Acc"}, headers=AUTH).json()
    appserver.note_reply(appserver.load_chat(chat["id"]), {"text": "Login: me, password: hunter2", "ts": 7.0})
    feed = app.get("/api/notifications", headers=AUTH).json()["replies"]
    assert feed[-1]["text"] == "New message. Open the chat to read it."
