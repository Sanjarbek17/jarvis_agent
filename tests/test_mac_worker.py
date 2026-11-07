"""Ticket 7ce4c9a5: a Mac task crashed before starting (`os` wasn't imported in core/mac.py)."""
import asyncio
import json

from core import mac


class FakeProc:
    def __init__(self):
        self.lines = [json.dumps({"type": "result", "result": "Done: opened the deck.", "is_error": False}).encode() + b"\n", b""]
        self.returncode = 0
        self.stdout = self

    async def readline(self):
        return self.lines.pop(0)

    async def wait(self):
        return 0


def test_a_mac_task_starts_its_worker_with_the_app_chat(tmp_path, monkeypatch):
    for name in ("LOGS", "SHOTS", "WORKDIR"):
        monkeypatch.setattr(mac, name, tmp_path)
    monkeypatch.setattr(mac, "SESSION", tmp_path / "session.json")
    seen = {}

    async def exec_(*cmd, **kw):
        seen["env"], seen["cmd"] = kw["env"], cmd
        return FakeProc()

    monkeypatch.setattr(mac.asyncio, "create_subprocess_exec", exec_)
    text, photos, question = asyncio.run(mac.MacWorker().run("open the deck", "open it", app_chat="c1"))
    assert "opened the deck" in text and not question
    assert seen["env"]["JARVIS_APP_CHAT"] == "c1" and seen["env"]["PATH"]           # his environment plus the chat
    assert seen["cmd"][:2] == ("claude", "-p")
