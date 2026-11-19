"""Messages other people send to Jarvis's own Telegram account reach Jarvis and Sanjar (ticket 3e945138)."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

from core import dms, tools

OWNER = 111


def user(id, first, username="", bot=False, last=None):
    return NS(id=id, first_name=first, last_name=last, username=username, bot=bot, is_self=False)


class FakeFile:
    name, ext, mime_type = "timer.html", ".html", "text/html"


def msg(id, text="", out=False, file=False, chat_id=5):
    async def download_media(file=None):
        Path(file).write_text("<html>timer</html>")
        return file
    return NS(id=id, chat_id=chat_id, raw_text=text, out=out, date=datetime(2026, 11, 19, 7, tzinfo=timezone.utc),
              document=object() if file else None, photo=None, voice=None, video_note=None, audio=None,
              file=FakeFile() if file else None, download_media=download_media)


class FakeTg:
    def __init__(self, dialogs, history):
        self.dialogs, self.history = dialogs, history

    async def iter_dialogs(self, limit=None):
        for d in self.dialogs:
            yield d

    async def iter_messages(self, entity, limit=None):
        for m in self.history.get(entity.id, [])[:limit]:
            yield m


ZILOLA = user(5, "Zilola", "zilola_sh", last="Sh")
DIALOGS = [NS(is_user=True, entity=user(OWNER, "Sanjar"), message=msg(1, "hi")),
           NS(is_user=True, entity=user(9, "SpamBot", "SpamBot", bot=True), message=msg(2, "ok")),
           NS(is_user=True, entity=user(777000, "Telegram"), message=msg(3, "Login code: 12345")),
           NS(is_user=True, entity=ZILOLA, message=msg(12, "here is my timer", file=True))]
HISTORY = {5: [msg(12, "here is my timer", file=True), msg(11, "Assalomu alaykum")]}   # newest first, like Telethon


def test_only_people_are_told():
    assert dms.from_person(ZILOLA)
    assert not dms.from_person(user(9, "SpamBot", bot=True))
    assert not dms.from_person(user(777000, "Telegram"))      # login codes are never passed on


def test_message_body_saves_the_file_and_prompt_must_tell(tmp_path):
    text = asyncio.run(dms.body(msg(12, "here is my timer", file=True), tmp_path))
    assert "here is my timer" in text and "timer.html" in text
    saved = list(tmp_path.iterdir())
    assert len(saved) == 1 and saved[0].read_text() == "<html>timer</html>"
    p = dms.prompt(ZILOLA, text)
    assert "Zilola Sh (@zilola_sh)" in p and str(saved[0]) in p and "NO_REPLY is not allowed" in p


def test_list_private_chats_skips_owner_bots_and_service():
    out = asyncio.run(dms.private_chats(FakeTg(DIALOGS, HISTORY), OWNER))
    assert "Zilola Sh" in out and "here is my timer" in out
    assert "Sanjar" not in out.split("\n", 1)[1] and "SpamBot" not in out and "12345" not in out


def test_read_one_chat_oldest_first_with_files(tmp_path):
    out = asyncio.run(dms.private_chats(FakeTg(DIALOGS, HISTORY), OWNER, "zilola", inbox=tmp_path))
    assert out.index("Assalomu alaykum") < out.index("here is my timer")
    assert str(tmp_path) in out
    assert "No private chat" in asyncio.run(dms.private_chats(FakeTg(DIALOGS, HISTORY), OWNER, "nobody"))


def test_brain_tool_reads_jarvis_dms(monkeypatch):
    async def fake(person="", limit=30):
        return f"chat with {person}"
    monkeypatch.setattr(tools.hooks, "private_chats", fake)
    res = asyncio.run(tools.make_server("Telegram").call_tool("jarvis_private_chats", {"person": "Zilola"}))
    assert "chat with Zilola" in str(res)
