"""Ticket 5ad3f371: Jarvis may give Sanjar his own account password, but only in his signed-in app chat or his
Telegram DM; chat files, the APK's built-in key and everyone else never get it."""
import asyncio

from starlette.testclient import TestClient

from core import appserver, brain, memory

PW = "Zq7-fake-Pw-81xk"


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(appserver, "SESSIONS", tmp_path / "sessions.json")
    monkeypatch.setenv("APP_TOKEN", "t0k")
    monkeypatch.setattr(appserver, "_brains", {})
    monkeypatch.setattr(memory, "_secret_values", lambda: (PW,))     # as if it were in instagram_channels/.env
    appserver._code.clear()
    appserver._code_times.clear()
    asked = []

    class FakeBrain:
        session_id = "s1"

        async def reply(self, text):
            asked.append(text)
            return f"Login: inglizcha.oson.qoida, password: {PW}"

    monkeypatch.setattr(appserver, "_brain", lambda chat: FakeBrain())
    client = TestClient(appserver.app)
    sent = []

    async def send(text):
        sent.append(text)

    monkeypatch.setattr(appserver, "send_code", send)
    client.post("/api/login/start", json={"device": "phone"})
    session = client.post("/api/login", json={"code": sent[0].split("code: ")[1][:6], "device": "phone"}).json()["token"]
    return client, session, asked


def test_signed_in_he_gets_it_once_and_the_chat_file_never_keeps_it(tmp_path, monkeypatch):
    client, session, asked = _setup(tmp_path, monkeypatch)
    chat = appserver.new_chat("General", "pw")
    got = client.post(f"/api/chats/{chat['id']}/messages", json={"text": "instagram password?"},
                      headers={"Authorization": f"Bearer {session}"}).json()
    assert PW in got["text"]                                          # the one reply he asked for
    assert appserver.SIGNED_IN in asked[0]                            # the brain knows it's really him
    saved = (appserver.CHATS / f"{chat['id']}.json").read_text()
    assert PW not in saved and "[secret]" in saved
    history = client.get(f"/api/chats/{chat['id']}/messages", headers={"Authorization": "Bearer t0k"}).json()
    assert all(PW not in m["text"] for m in history)                 # the APK key reads history: hidden


def test_the_apks_built_in_key_never_gets_a_password(tmp_path, monkeypatch):
    client, _, asked = _setup(tmp_path, monkeypatch)
    chat = appserver.new_chat("General", "pw")
    got = client.post(f"/api/chats/{chat['id']}/messages", json={"text": "instagram password?"},
                      headers={"Authorization": "Bearer t0k"}).json()
    assert PW not in got["text"] and "[secret]" in got["text"]        # even if the brain slipped
    assert appserver.NOT_SIGNED_IN in asked[0]


def test_pushes_and_the_telegram_copy_are_saved_without_it(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    appserver.telegram_said("jarvis", f"password: {PW}")
    appserver.push(f"worker says {PW}")
    assert PW not in (appserver.CHATS / f"{appserver.UPDATES}.json").read_text()


def test_the_rule_is_in_jarvis_prompt_with_its_limits():
    text = brain.base_prompt()
    assert "HIS OWN account logins" in text and "Never on a call" in text and "[not signed in]" in text
