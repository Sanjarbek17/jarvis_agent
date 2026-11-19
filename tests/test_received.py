"""Ticket 8d867330: Zilola sent her timer index.html to Jarvis's own Telegram account. Jarvis dropped every private
message from someone other than Sanjar without a trace, so nobody knew the file had come in and the Mac worker
reported "no file". Now such messages are logged and their files kept, and the brain reads those chats directly."""
import asyncio
import datetime as dt
from types import SimpleNamespace as NS

from core import received

OWNER = 111


def when(hours_ago):
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)


class Msg:
    def __init__(self, mid, text="", file=None, out=False, hours_ago=1):
        self.id, self.raw_text, self.out, self.date = mid, text, out, when(hours_ago)
        self.document = object() if file else None
        self.photo = None
        self.file = NS(name=file[0], size=file[1], ext="." + file[0].rsplit(".", 1)[-1]) if file else None
        self.downloads = 0

    async def download_media(self, file):
        self.downloads += 1
        open(file, "w").write("<html>timer</html>")
        return file


def user(uid, first, username="", bot=False):
    return NS(id=uid, first_name=first, last_name="", username=username, bot=bot, is_self=False)


class FakeTG:
    def __init__(self, chats):
        self.chats = chats          # [(user, dialog date hours ago, [Msg newest first])]

    async def iter_dialogs(self, limit):
        for u, ago, _ in self.chats:
            yield NS(entity=u, is_user=True, date=when(ago))

    async def iter_messages(self, u, limit):
        for m in next(msgs for x, _, msgs in self.chats if x is u):
            yield m


def test_listing_finds_a_file_someone_sent_to_jarvis_and_saves_it(monkeypatch, tmp_path):
    monkeypatch.setattr(received, "PEOPLE", tmp_path)
    zilola = user(42, "Zilola Sh", "zilola")
    html = Msg(7, "", file=("index.html", 10_752), hours_ago=2)
    tg = FakeTG([
        (user(OWNER, "Sanjar"), 0.1, [Msg(1, "hi")]),                     # Sanjar himself: not listed
        (user(5, "Some bot", bot=True), 0.5, [Msg(2, "ad")]),              # bots: not listed
        (zilola, 1, [Msg(8, "Qanday yuboray kodini?", hours_ago=1), html]),
        (user(9, "Old friend"), 24 * 30, [Msg(3, "long ago", hours_ago=24 * 30)]),   # older than `days`
    ])
    out = asyncio.run(received.listing(tg, OWNER, days=7))
    assert "Zilola Sh (@zilola), id 42" in out and "Qanday yuboray kodini?" in out
    assert "file index.html, 10 KB, saved at" in out
    assert (tmp_path / "42" / "7_index.html").read_text() == "<html>timer</html>"
    assert "Sanjar" not in out and "Some bot" not in out and "Old friend" not in out
    assert out.index("index.html") < out.index("Qanday")               # oldest first, like a chat
    asyncio.run(received.listing(tg, OWNER, days=7))
    assert html.downloads == 1                                          # saved once, not every time
    assert "Zilola" in asyncio.run(received.listing(tg, OWNER, who="zilo"))
    assert "No one matching 'Azim'" in asyncio.run(received.listing(tg, OWNER, who="Azim"))


def test_a_file_arriving_is_saved_and_logged(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(received, "PEOPLE", tmp_path)
    m = Msg(7, "mana", file=("index.html", 10_752))

    async def sender():
        return user(42, "Zilola Sh")

    ev = NS(message=m, raw_text="mana", sender_id=42, get_sender=sender)
    with caplog.at_level("INFO", logger="jarvis.received"):
        asyncio.run(received.on_arrival(ev))
    assert (tmp_path / "42" / "7_index.html").exists()
    assert "from Zilola Sh: 'mana' + file index.html (10 KB) saved at" in caplog.text


def test_too_big_files_are_listed_not_downloaded(monkeypatch, tmp_path):
    monkeypatch.setattr(received, "PEOPLE", tmp_path)
    big = Msg(7, "", file=("film.mp4", received.MAX_DOWNLOAD + 1))
    out = asyncio.run(received.listing(FakeTG([(user(42, "Zilola"), 1, [big])]), OWNER))
    assert "too big to download" in out and big.downloads == 0


def test_the_brain_tool_reads_through_the_hook(monkeypatch):
    from core import tools
    seen = []

    async def fake(who, days):
        seen.append((who, days))
        return "Private chats with Jarvis's own Telegram account: ..."

    monkeypatch.setattr(tools.hooks, "received", fake, raising=False)
    server = tools.make_server("Telegram")
    out = str(asyncio.run(server.call_tool("telegram_received", {"who": "Zilola", "days": 3})))
    assert "Private chats" in out and seen == [("Zilola", 3)]
