import ast
import asyncio
import json
import logging
import time
from types import SimpleNamespace as NS

import pytest

from core import brain, coremem, runner
from core.brain import is_silent


@pytest.fixture(autouse=True)
def core_files(tmp_path, monkeypatch):
    monkeypatch.setattr(coremem, "CORE", tmp_path / "CORE.md")
    monkeypatch.setattr(coremem, "STATE_FILE", tmp_path / "core_state.json")


def test_put_replaces_by_key_and_drop_removes():
    coremem.put("Use the +7 906 account for job messages.", "decision", "job-account")
    coremem.put("Reels post at 19:30.", "decision", "reel-time")
    coremem.put("Use the +7 906 account for job messages, never Jarvis's own.", "decision", "job-account")
    e = coremem.load()
    assert len(e) == 2 and e["job-account"]["text"].endswith("never Jarvis's own.")
    assert "## Decisions" in coremem.CORE.read_text()
    assert coremem.drop("reel-time") and list(coremem.load()) == ["job-account"]
    assert not coremem.drop("nope")


def test_text_goes_into_the_brain_prompt():
    coremem.put("One item at a time when he asks what needs him.", "preference", "one-item")
    assert "One item at a time" in coremem.text()
    assert "CORE MEMORY" in brain.Brain("chat")._system() and "One item at a time" in brain.Brain("chat")._system()
    assert "CORE MEMORY" in brain.Brain("call")._system()


def test_consolidate_reads_only_new_talk(monkeypatch):
    items = [{"kind": "chat", "ts": 100.0, "text": "Sanjar: post reels at 19:30\nJarvis: ok"},
             {"kind": "chat", "ts": 200.0, "text": "Sanjar: make the gas bot\nJarvis: on it"},
             {"kind": "fact", "ts": 300.0, "text": "not a conversation"}]
    monkeypatch.setattr(coremem.memory, "_load", lambda: items)
    asked = []

    async def ask(prompt, model=None, timeout=None):
        asked.append(prompt)
        return {"set": [{"key": "reel-time", "kind": "decision", "text": "Reels post at 19:30."},
                        {"key": "gas-bot", "kind": "open", "text": "He asked for the gas finder bot."}],
                "remove": []}

    monkeypatch.setattr(coremem.memory, "_ask_claude_json", ask)
    assert asyncio.run(coremem.consolidate()) == (2, 0)
    assert "post reels at 19:30" in asked[0] and "not a conversation" not in asked[0]
    assert set(coremem.load()) == {"reel-time", "gas-bot"}
    assert asyncio.run(coremem.consolidate()) == (0, 0) and len(asked) == 1     # nothing new: no model call

    items.append({"kind": "chat", "ts": 400.0, "text": "Sanjar: the gas bot is done, thanks\nJarvis: great"})

    async def ask2(prompt, model=None, timeout=None):
        asked.append(prompt)
        return {"set": [], "remove": ["gas-bot"]}

    monkeypatch.setattr(coremem.memory, "_ask_claude_json", ask2)
    assert asyncio.run(coremem.consolidate()) == (0, 1)
    assert "gas bot is done" in asked[1] and "post reels" not in asked[1]   # only the new part
    assert set(coremem.load()) == {"reel-time"}


def test_failed_consolidation_keeps_state(monkeypatch):
    monkeypatch.setattr(coremem.memory, "_load", lambda: [{"kind": "chat", "ts": 5.0, "text": "Sanjar: x"}])

    async def boom(*a, **k):
        raise RuntimeError("limit")

    monkeypatch.setattr(coremem.memory, "_ask_claude_json", boom)
    assert asyncio.run(coremem.consolidate()) == (0, 0) and not coremem.STATE_FILE.exists()   # retried next time


def _jarvis_funcs(*names, **g):
    src = open("jarvis.py").read()
    keep = [n for n in ast.parse(src).body if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names)
            or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in ("REPORT_PROMPT", "MUST_TELL"))
            or (isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "_inbox")]
    exec(compile(ast.Module(keep, []), "jarvis.py", "exec"), g)
    return g


def test_must_tell_is_enforced_even_if_the_brain_stays_silent():
    sent = []

    async def send(tg, chat, text, **kw):
        sent.append(text)

    for reply in ("NO_REPLY", RuntimeError("brain down")):
        sent.clear()

        async def chat_reply(t, reply=reply):
            if isinstance(reply, Exception):
                raise reply
            return reply

        g = _jarvis_funcs("relay", chat=NS(reply=chat_reply), tg=None, OWNER_ID=1, is_silent=is_silent,
                          tgfmt=NS(send=send, md=lambda t: t), app_push=lambda t: None, send_photos=None,
                          log=logging.getLogger(), Path=__import__("pathlib").Path)
        asyncio.run(g["relay"]("Ticket t1 [Teacher] \"Test call\" is failed.\nThe call didn't connect: no account.", must=True))
        assert len(sent) == 1 and "Test call" in sent[0]
        asyncio.run(g["relay"]("Ticket t2 is done.\nfine", must=False))
        assert len(sent) == 1                     # not forced: silence stays silence


def test_messages_while_busy_join_one_turn():
    calls, replies = [], []

    async def chat_reply(t):
        calls.append(t)
        await asyncio.sleep(0.05)
        return "ok"

    class Action:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    async def reply(event, html):
        replies.append(event)

    g = _jarvis_funcs("chat_turn", chat=NS(reply=chat_reply), tg=NS(action=lambda *a: Action()), OWNER_ID=1,
                      tgfmt=NS(reply=reply, md=lambda t: t), _inbox=[], _inbox_running=False, asyncio=asyncio)

    async def go():
        first = asyncio.create_task(g["chat_turn"]("e1", "hello"))
        await asyncio.sleep(0.01)
        await g["chat_turn"]("e2", "and also this")
        await g["chat_turn"]("e3", "and one more")
        await first

    asyncio.run(go())
    assert len(calls) == 2 and calls[0] == "hello"
    assert "and also this" in calls[1] and "and one more" in calls[1] and replies == ["e1", "e3"]


def test_tickets_from_sanjar_are_remembered():
    card = NS(ref="t9", note="added from Telegram")
    runner._mark_from_sanjar(card)
    runner._mark_from_sanjar(NS(ref="aet-3", note="answered by Jarvis: ..."))
    assert runner.from_sanjar("t9") and not runner.from_sanjar("aet-3")
