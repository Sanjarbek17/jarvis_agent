"""Ticket 799dda33: a ticket asked for in the Jarvis app reports back in that app chat, not on Telegram."""
import ast
import asyncio
import contextlib
import json
import logging
import socket
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import uvicorn

from core import appserver, board, brain, origin, tools
from core.brain import is_silent


def test_origin_parse_and_store():
    assert origin.parse("app:abc") == {"channel": "app", "chat": "abc"}
    assert origin.parse("") == {} and origin.parse("app:") == {} and origin.parse("telegram") == {}
    origin.set("t1", {"channel": "app", "chat": "abc"})
    assert origin.get("t1")["chat"] == "abc"
    origin.set("t1", {})                       # no header (Jarvis's own answer_task / queue): keeps the app chat
    assert origin.get("t1")["chat"] == "abc"
    origin.set("t1", {"channel": "app", "chat": "def"})   # asked again in another app chat: that one
    assert origin.get("t1")["chat"] == "def"
    origin.set("t2", {})                       # asked on Telegram: no record, Telegram as before
    assert origin.get("t2") == {}
    assert origin.label({"channel": "app", "chat": "x"}, "Telegram") == "the app"
    assert origin.label({}, "Telegram") == "Telegram"


def test_app_brain_sends_its_chat_in_the_tool_header(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path)
    appserver._brains.clear()
    chat = appserver.new_chat("Jarvis")
    b = appserver._brain(chat)
    assert b.origin == f"app:{chat['id']}"
    started = {}

    async def fake_exec(*cmd, **kw):
        started["mcp"] = json.loads(cmd[cmd.index("--mcp-config") + 1])
        return NS(pid=1)

    monkeypatch.setattr(brain, "JARVIS_ENGINE", "claude")
    monkeypatch.setattr(brain.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(b.start())
    assert started["mcp"]["mcpServers"]["jarvis"]["headers"] == {"X-Jarvis-Origin": f"app:{chat['id']}"}
    asyncio.run(brain.Brain("chat").start())                   # Telegram's brain: no header
    assert "headers" not in started["mcp"]["mcpServers"]["jarvis"]
    appserver._brains.clear()


def test_tools_record_the_origin_over_real_http(monkeypatch):
    added = []

    async def add_ticket(project, title, words, source):
        added.append(source)
        return NS(ref="j-1")

    async def find_open(project, words):
        return [NS(ref="j-2", title="x", jarvis="", page_id="p")]

    async def update(t, **kw):
        added.append(kw["note"])

    monkeypatch.setattr(board, "boards", lambda: {"Jarvis": "b"})
    monkeypatch.setattr(board, "add_ticket", add_ticket)
    monkeypatch.setattr(board, "find_open", find_open)
    monkeypatch.setattr(board, "update", update)

    async def go():
        app = tools.make_server("Telegram").streamable_http_app(stateless_http=True, json_response=True)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        server.capture_signals = contextlib.nullcontext
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.02)
        base = {"accept": "application/json, text/event-stream", "content-type": "application/json",
                "mcp-protocol-version": "2025-06-18"}

        async def call(name, args, extra):
            async with httpx.AsyncClient() as c:
                r = await c.post(f"http://127.0.0.1:{port}/mcp", headers={**base, **extra},
                                 json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": name, "arguments": args}})
                assert r.status_code == 200 and not r.json()["result"]["isError"], r.text

        try:
            await call("add_task", {"project": "Jarvis", "task": "t", "my_words": "w"}, {"x-jarvis-origin": "app:c9"})
            await call("queue_ticket", {"project": "Jarvis", "ticket": "x"}, {})
        finally:
            server.should_exit = True
            await task

    asyncio.run(go())
    assert added == ["the app", "queued from Telegram"]
    assert origin.get("j-1") == {"channel": "app", "chat": "c9", "at": origin.get("j-1")["at"]}
    assert origin.get("j-2") == {}


def test_push_into_the_asking_chat_and_the_feed(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path)
    chat = appserver.new_chat("Jarvis", "Fix the runner")
    msg = appserver.push("The runner fix is done.", chat["id"])
    assert msg["chat"] == chat["id"]
    assert appserver.load_chat(chat["id"])["messages"][-1]["text"] == "The runner fix is done."
    assert appserver.updates_since(0)[-1]["text"] == "The runner fix is done."   # the phone still buzzes
    assert "The runner fix is done." in appserver._unseen_pushes(appserver.load_chat(chat["id"]))
    appserver.push("Plain update.", "no-such-chat")                               # unknown chat: feed only
    assert appserver.updates_since(0)[-1]["text"] == "Plain update."


def _relay(**g):
    src = open("jarvis.py").read()
    keep = [n for n in ast.parse(src).body if (isinstance(n, ast.AsyncFunctionDef) and n.name == "relay")
            or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in ("REPORT_PROMPT", "MUST_TELL"))]
    exec(compile(ast.Module(keep, []), "jarvis.py", "exec"), g)
    return g["relay"]


def test_relay_answers_where_he_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path)
    chat = appserver.new_chat("Jarvis")
    telegram, app, photos_sent = [], [], []

    async def send(tg, to, text, **kw):
        telegram.append(text)

    async def send_photos(p, label):
        photos_sent.append(len(p))

    async def reply(t):
        return "Done: the runner fix is merged."

    relay = _relay(chat=NS(reply=reply), tg=None, OWNER_ID=1, is_silent=is_silent, appserver=appserver,
                   tgfmt=NS(send=send, md=lambda t: t), app_push=lambda t, c="", ph=(): app.append((t, c, len(ph))),
                   send_photos=send_photos, log=logging.getLogger(), Path=Path)
    asyncio.run(relay("Ticket j-1 is done.", app_chat=chat["id"]))
    assert telegram == [] and app == [("Done: the runner fix is merged.", chat["id"], 0)]
    asyncio.run(relay("Ticket j-1 is done.", [Path("a.png")], app_chat=chat["id"]))
    assert telegram == [] and app[-1] == ("Done: the runner fix is merged.", chat["id"], 1) and photos_sent == []
    app.clear()
    asyncio.run(relay("Ticket j-2 is done.", [Path("a.png")]))              # asked on Telegram
    assert telegram == ["Done: the runner fix is merged."] and photos_sent == [1]
    assert app == [("Done: the runner fix is merged.", "", 1)]             # the app's Updates chat shows them too
    telegram.clear()
    asyncio.run(relay("Ticket j-3 is done.", app_chat="deleted-chat"))     # chat gone: Telegram
    assert telegram == ["Done: the runner fix is merged."]


def test_antigravity_brain_tracks_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path)
    appserver._brains.clear()
    chat = appserver.new_chat("Jarvis")
    b = appserver._brain(chat)
    assert b.origin == f"app:{chat['id']}"

    # Verify origin.from_ctx without header when no brain is active returns empty
    assert origin.from_ctx(None) == {}
    assert origin.get_active() == ""

    # Test active context
    with origin.active(b.origin):
        assert origin.get_active() == f"app:{chat['id']}"
        assert origin.from_ctx(None) == {"channel": "app", "chat": chat["id"]}
    assert origin.get_active() == ""
    assert origin.from_ctx(None) == {}


def test_antigravity_brain_exec_and_tools_attribution(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path)
    monkeypatch.setattr(origin, "FILE", tmp_path / "ticket_origin.json")
    appserver._brains.clear()
    chat = appserver.new_chat("Jarvis")
    b = appserver._brain(chat)

    class FakeStdin:
        def write(self, d): pass
        async def drain(self): pass
        def close(self): pass

    class FakeStdout:
        def __init__(self, lines):
            self.lines = list(lines)
        async def readline(self):
            return (self.lines.pop(0) + "\n").encode() if self.lines else b""

    # Mock agy exec
    lines = [
        json.dumps({"event": "init", "conversation_id": "conv-1"}),
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Thinking..."}}),
        json.dumps({"event": "result", "result": {"status": "OK"}}),
    ]
    fake_proc = NS(pid=12345, returncode=None, stdin=FakeStdin(), stdout=FakeStdout(lines), terminate=lambda: None)

    async def fake_exec(*cmd, **kw):
        return fake_proc

    monkeypatch.setattr(brain, "JARVIS_ENGINE", "antigravity")
    monkeypatch.setattr(brain.asyncio, "create_subprocess_exec", fake_exec)

    async def run_turn():
        await b.start()
        assert origin._proc_origins.get(12345) == f"app:{chat['id']}"

        # During b.ask(), origin should be active
        observed = []
        async for _ in b.ask("Please add a ticket"):
            observed.append(origin.from_ctx(None))
        return observed

    results = asyncio.run(run_turn())
    assert results and all(r == {"channel": "app", "chat": chat["id"]} for r in results)
    assert origin.get_active() == ""

    # Test tool call attribution during active origin
    added = []

    async def fake_add_ticket(project, title, words, source):
        added.append(source)
        return NS(ref="ag-1")

    monkeypatch.setattr(board, "boards", lambda: {"Jarvis": "b"})
    monkeypatch.setattr(board, "add_ticket", fake_add_ticket)

    async def call_tools():
        with origin.active(b.origin):
            server = tools.make_server("Telegram")
            await server.call_tool("add_task", {"project": "Jarvis", "task": "Task from app", "my_words": "words"})

    asyncio.run(call_tools())
    assert added == ["the app"]
    assert origin.get("ag-1")["chat"] == chat["id"]

    asyncio.run(b.stop())
    assert 12345 not in origin._proc_origins
    appserver._brains.clear()

