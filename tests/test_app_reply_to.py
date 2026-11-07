"""Ticket 256550ce: reply to a specific message in the app / website; Jarvis sees what it refers to."""
import asyncio

import pytest
from starlette.testclient import TestClient

from core import appserver

AUTH = {"Authorization": "Bearer home-token"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(appserver, "IMAGES", tmp_path / "images")
    monkeypatch.setattr(appserver, "SESSIONS", tmp_path / "sessions.json")
    monkeypatch.setenv("APP_TOKEN", "home-token")
    seen = []

    class FakeBrain:
        session_id = "s"
        lock = asyncio.Lock()

        async def reply(self, text):
            seen.append(text)
            return f"answer {len(seen)}"

    monkeypatch.setattr(appserver, "_brain", lambda chat: FakeBrain())
    client = TestClient(appserver.app)
    chat = client.post("/api/chats", json={"project": "General"}, headers=AUTH).json()["id"]
    return client, seen, chat


def test_reply_quotes_the_message_for_jarvis_and_the_chat(app):
    client, seen, chat = app
    url = f"/api/chats/{chat}/messages"
    client.post(url, json={"text": "list three colours"}, headers=AUTH)
    client.post(url, json={"text": "now three animals"}, headers=AUTH)
    msgs = client.get(url, headers=AUTH).json()
    first_answer = msgs[1]                                   # "answer 1", not the latest one
    client.post(url, json={"text": "why that order?", "reply_to": first_answer["ts"]}, headers=AUTH)
    assert "Replying to this earlier message from you (Jarvis):\nanswer 1" in seen[-1]
    assert seen[-1].rstrip().endswith("why that order?")
    mine = client.get(url, headers=AUTH).json()[-2]
    assert mine["reply"] == {"role": "jarvis", "text": "answer 1", "ts": first_answer["ts"]}


def test_reply_to_his_own_message_with_images(app, tmp_path):
    client, seen, chat = app
    url = f"/api/chats/{chat}/messages"
    c = appserver.load_chat(chat)
    c["messages"].append({"role": "me", "text": "", "ts": 1000.5, "images": ["0123456789abcdef.jpg"]})
    appserver.save_chat(c)
    client.post(url, json={"text": "what was in this?", "reply_to": 1000.5}, headers=AUTH)
    assert "from Sanjar" in seen[-1] and str(tmp_path / "images" / "0123456789abcdef.jpg") in seen[-1]
    assert client.get(url, headers=AUTH).json()[-2]["reply"] == {"role": "me", "text": "", "ts": 1000.5, "images": 1}


def test_unknown_or_bad_reply_to_still_sends(app):
    client, seen, chat = app
    url = f"/api/chats/{chat}/messages"
    for bad in (12345.0, "nope", None, [1]):
        assert client.post(url, json={"text": "hi", "reply_to": bad}, headers=AUTH).status_code == 200
    assert not any("Replying to" in s for s in seen)
    assert all("reply" not in m for m in client.get(url, headers=AUTH).json())


def test_voice_reply(app, monkeypatch):
    client, seen, chat = app
    url = f"/api/chats/{chat}/messages"
    client.post(url, json={"text": "pick a day"}, headers=AUTH)
    answer = client.get(url, headers=AUTH).json()[1]

    async def heard(audio):
        return "the second one"

    monkeypatch.setattr(appserver, "transcribe_audio", heard)
    r = client.post(f"/api/chats/{chat}/voice?reply_to={answer['ts']!r}", content=b"fake-audio", headers=AUTH).json()
    assert r["heard"] == "the second one" and "answer 1" in seen[-1]
