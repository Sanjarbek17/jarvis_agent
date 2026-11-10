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


def test_signed_in_he_gets_it_and_it_stays_on_his_screen(tmp_path, monkeypatch):
    """Ticket 7896b7af: the app reloads the chat right after a reply; the password vanished on reload."""
    client, session, asked = _setup(tmp_path, monkeypatch)
    chat = appserver.new_chat("General", "pw")
    mine = {"Authorization": f"Bearer {session}"}
    got = client.post(f"/api/chats/{chat['id']}/messages", json={"text": "instagram password?"}, headers=mine).json()
    assert PW in got["text"] and appserver.OWN not in got            # the reply he asked for
    assert appserver.SIGNED_IN in asked[0] and "give it here plainly" in asked[0]
    reloaded = client.get(f"/api/chats/{chat['id']}/messages", headers=mine).json()
    assert PW in reloaded[-1]["text"] and all(appserver.OWN not in m for m in reloaded)   # still there after reload
    saved = appserver.load_chat(chat["id"])["messages"][-1]
    assert PW not in saved["text"] and "[secret]" in saved["text"]   # the visible field stays hidden
    history = client.get(f"/api/chats/{chat['id']}/messages", headers={"Authorization": "Bearer t0k"}).json()
    assert all(PW not in str(m) for m in history)                     # the APK key reads history: hidden
    assert PW not in str(appserver.chats_of("General"))               # nor the chat list preview


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


def test_not_signed_in_nothing_is_kept_for_later():
    msgs = [{"role": "jarvis", "text": "password: [secret]", "ts": 1, appserver.OWN: f"password: {PW}"}]
    assert appserver._for_reader(msgs, False) == [{"role": "jarvis", "text": "password: [secret]", "ts": 1}]
    assert appserver._for_reader(msgs, True)[0]["text"] == f"password: {PW}"


def test_the_prompt_no_longer_contradicts_itself():
    text = brain.base_prompt()
    assert "never repeat a password, key or token to anyone." not in text
    assert "send passwords/keys/tokens anywhere" not in text
    assert "without hesitating" in text
