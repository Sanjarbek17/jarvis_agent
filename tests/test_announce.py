"""ticket ee89e121: one approved post per group from the Jarvis account, through the outbox, never spam."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from core import announce


class Rights(SimpleNamespace):
    send_messages = False; send_media = False; send_photos = False


class FakeTG:
    def __init__(self, rights=None, fail=None, removed=False):
        self.rights, self.fail, self.sent, self.joined = rights or Rights(), fail, [], []
        self.removed = removed

    async def __call__(self, req):
        name = type(req).__name__
        if name == "JoinChannelRequest":
            self.joined.append(req.channel)
            return None
        return SimpleNamespace(chats=[SimpleNamespace(default_banned_rights=self.rights, username="metan_gaz_urgut")])

    async def send_message(self, chat, text, link_preview=False):
        if self.fail:
            raise self.fail
        self.sent.append(("text", chat, text)); return SimpleNamespace(id=101)

    async def send_file(self, chat, file, caption=""):
        self.sent.append(("photo", chat, caption)); return None if self.removed else SimpleNamespace(id=102)


@pytest.fixture(autouse=True)
def allow(tmp_path, monkeypatch):
    monkeypatch.setattr(announce, "ALLOW", tmp_path / "allow.json")
    real_sleep = asyncio.sleep
    monkeypatch.setattr(announce.asyncio, "sleep", lambda s: real_sleep(0))
    announce._save({"chats": {"metan_gaz_urgut": {}, "Jizzax_metan_24": {}}, "stopped": None})


def run(c):
    return asyncio.run(c)


def test_posts_once_with_photo_and_link(tmp_path):
    pic = tmp_path / "map.png"; pic.write_bytes(b"png")
    tg = FakeTG()
    r = run(announce.post(tg, "metan_gaz_urgut", "Salom", str(pic)))
    assert r == {"ok": True, "link": "https://t.me/metan_gaz_urgut/102", "photo": True}
    assert tg.joined == ["metan_gaz_urgut"] and tg.sent == [("photo", "metan_gaz_urgut", "Salom")]
    again = run(announce.post(tg, "metan_gaz_urgut", "Salom", str(pic)))          # never twice
    assert not again["ok"] and "already" in again["error"] and len(tg.sent) == 1


def test_only_approved_chats():
    tg = FakeTG()
    assert run(announce.post(tg, "some_other_group", "Salom"))["error"] == "not an approved chat"
    assert tg.sent == [] and tg.joined == []


def test_text_only_where_photos_blocked(tmp_path):
    pic = tmp_path / "map.png"; pic.write_bytes(b"png")
    tg = FakeTG(rights=Rights(send_photos=True))
    r = run(announce.post(tg, "Jizzax_metan_24", "Salom", str(pic)))
    assert r["ok"] and not r["photo"] and tg.sent[0][0] == "text"


def test_nothing_where_members_cant_post():
    tg = FakeTG(rights=Rights(send_messages=True))
    r = run(announce.post(tg, "Jizzax_metan_24", "Salom"))
    assert not r["ok"] and tg.sent == []


def test_flood_error_stops_everything():
    class PeerFloodError(Exception):
        pass
    tg = FakeTG(fail=PeerFloodError("Too many requests"))
    r = run(announce.post(tg, "metan_gaz_urgut", "Salom"))
    assert not r["ok"] and r["stop"]
    r2 = run(announce.post(FakeTG(), "Jizzax_metan_24", "Salom"))                  # nothing more after a flood
    assert not r2["ok"] and "stopped" in r2["error"]
    assert json.loads(announce.ALLOW.read_text())["stopped"].startswith("PeerFloodError")


def test_post_deleted_at_once_still_counts_as_done(tmp_path):
    # Telethon returns None when Telegram answers with MessageEmpty (the group's anti-spam deleted the post at once)
    pic = tmp_path / "map.png"; pic.write_bytes(b"png")
    tg = FakeTG(removed=True)
    r = run(announce.post(tg, "metan_gaz_urgut", "Salom", str(pic)))
    assert not r["ok"] and r["removed"] and not r["stop"]
    again = run(announce.post(tg, "metan_gaz_urgut", "Salom", str(pic)))          # never resent
    assert "already" in again["error"] and len(tg.sent) == 1


class SpamBotTG:
    def __init__(self, answer, hang=False):
        self.answer, self.hang = answer, hang

    async def send_message(self, chat, text):
        if self.hang:
            await asyncio.Event().wait()

    async def get_messages(self, chat, limit=1):
        return [SimpleNamespace(message=self.answer)]


def test_spambot_limited_answer_stops_all_later_posts():
    r = run(announce.spambot_check(SpamBotTG("I'm afraid some Telegram users found your messages annoying ... limited")))
    assert r["ok"] and not r["free"]
    assert "limited" in json.loads(announce.ALLOW.read_text())["stopped"]
    assert "stopped" in run(announce.post(FakeTG(), "Jizzax_metan_24", "Salom"))["error"]


def test_spambot_free_answer_keeps_going():
    r = run(announce.spambot_check(SpamBotTG("Good news, no limits are currently applied to your account.")))
    assert r["free"] and not json.loads(announce.ALLOW.read_text())["stopped"]


def test_spambot_check_never_hangs_the_outbox():
    r = run(announce.spambot_check(SpamBotTG("", hang=True), timeout=0.05))
    assert not r["ok"] and "Timeout" in r["error"]
