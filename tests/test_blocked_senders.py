"""Tests for blocking spam Telegram senders on Telegram (ticket fb74bc39)."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from core import blocked, received, tools
from telethon.tl.functions.contacts import BlockRequest, UnblockRequest
from telethon.tl.types import InputPeerUser


def test_load_save_add_remove(monkeypatch, tmp_path):
    f = tmp_path / "blocked.json"
    monkeypatch.setattr(blocked, "BLOCKED_FILE", f)

    assert blocked.load() == {}
    assert not blocked.is_blocked(12345)

    entry = blocked.add_blocked(12345, name="Spammer 1", reason="crypto spam")
    assert entry["id"] == 12345
    assert entry["name"] == "Spammer 1"
    assert entry["reason"] == "crypto spam"
    assert blocked.is_blocked(12345)
    assert blocked.is_blocked("12345")

    # update existing
    blocked.add_blocked(12345, username="spambot")
    loaded = blocked.load()
    assert loaded[12345]["username"] == "spambot"
    assert loaded[12345]["name"] == "Spammer 1"

    assert blocked.remove_blocked(12345)
    assert not blocked.is_blocked(12345)
    assert not blocked.remove_blocked(99999)


def test_block_and_unblock_requests(monkeypatch, tmp_path):
    f = tmp_path / "blocked.json"
    monkeypatch.setattr(blocked, "BLOCKED_FILE", f)
    monkeypatch.setattr(blocked, "OWNER_ID", 111)

    tg = AsyncMock()
    tg.return_value = True
    tg.get_input_entity = AsyncMock(return_value=InputPeerUser(user_id=222, access_hash=123456))

    async def run():
        # Cannot block owner or service ids
        ok, msg = await blocked.block(tg, 111)
        assert not ok and "Sanjar" in msg
        ok, msg = await blocked.block(tg, 777000)
        assert not ok and "service accounts" in msg

        # Block user 222
        user_obj = NS(id=222, first_name="Courtney", last_name="Ca", username="courtney", access_hash=123456)
        ok, msg = await blocked.block(tg, user_obj)
        assert ok and "Courtney Ca" in msg and "222" in msg
        assert blocked.is_blocked(222)
        assert tg.call_count == 1
        call_arg = tg.call_args[0][0]
        assert isinstance(call_arg, BlockRequest)
        assert call_arg.id.user_id == 222

        # Unblock user 222
        ok, msg = await blocked.unblock(tg, user_obj)
        assert ok and "Unblocked" in msg
        assert not blocked.is_blocked(222)
        assert tg.call_count == 2
        unblock_arg = tg.call_args[0][0]
        assert isinstance(unblock_arg, UnblockRequest)
        assert unblock_arg.id.user_id == 222

    asyncio.run(run())


def test_sync_startup(monkeypatch, tmp_path):
    f = tmp_path / "blocked.json"
    monkeypatch.setattr(blocked, "BLOCKED_FILE", f)

    blocked.add_blocked(1001, name="Spam1", access_hash=1111)
    blocked.add_blocked(1002, name="Spam2", access_hash=2222)

    tg = AsyncMock()

    async def run():
        count = await blocked.sync_startup(tg)
        assert count == 2
        assert tg.call_count == 2

    asyncio.run(run())


def test_received_ignores_blocked_user(monkeypatch, tmp_path):
    f = tmp_path / "blocked.json"
    monkeypatch.setattr(blocked, "BLOCKED_FILE", f)
    monkeypatch.setattr(received, "PEOPLE", tmp_path / "people")

    blocked.add_blocked(9148298716, name="Courtney Ca", reason="spam")

    async def sender():
        return NS(id=9148298716, first_name="Courtney", last_name="Ca", username="", bot=False, is_self=False)

    msg = NS(id=1, raw_text="Hello", document=None, photo=None, file=None)
    ev = NS(message=msg, raw_text="Hello", sender_id=9148298716, get_sender=sender)

    async def run():
        note = await received.on_arrival(ev)
        assert note == ""

    asyncio.run(run())


def test_tools_block_and_unblock(monkeypatch):
    seen = []

    async def fake_block(who):
        seen.append(("block", who))
        return f"Blocked {who}"

    async def fake_unblock(who):
        seen.append(("unblock", who))
        return f"Unblocked {who}"

    monkeypatch.setattr(tools.hooks, "block_contact", fake_block, raising=False)
    monkeypatch.setattr(tools.hooks, "unblock_contact", fake_unblock, raising=False)

    async def run():
        server = tools.make_server("Telegram")
        res1 = await server.call_tool("block_contact", {"contact": "Courtney Ca"})
        assert "Blocked Courtney Ca" in str(res1)

        res2 = await server.call_tool("unblock_contact", {"contact": "Courtney Ca"})
        assert "Unblocked Courtney Ca" in str(res2)

        assert seen == [("block", "Courtney Ca"), ("unblock", "Courtney Ca")]

    asyncio.run(run())



