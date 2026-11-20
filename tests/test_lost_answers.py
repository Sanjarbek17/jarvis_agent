"""Ticket 11ff3b8a: Sanjar's app messages lost their answers to restarts, and an app call went deaf when the
phone sent only silence."""
import asyncio
import time

from core import answering, appserver
from core.voice import SilenceWatch


def test_an_answer_in_progress_holds_a_restart_and_is_cleared_at_start(tmp_path, monkeypatch):
    monkeypatch.setattr(answering, "DIR", tmp_path / "answering")
    assert not answering.busy()
    with answering.mark("chat-1"):
        assert answering.busy()
        assert answering.is_answering("chat-1")
        assert not answering.is_answering("chat-2")
    assert not answering.busy()
    assert not answering.is_answering("chat-1")
    with answering.mark("x"):
        answering.clear()                       # a new process: the old one's answers are gone
        assert not answering.busy()


def test_the_app_marks_its_answer_while_the_brain_thinks(tmp_path, monkeypatch):
    monkeypatch.setattr(answering, "DIR", tmp_path / "answering")
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(appserver, "_brains", {})
    seen = []

    class Brain:
        session_id = "s"

        async def reply(self, text):
            seen.append(answering.busy())
            return "ok"

    monkeypatch.setattr(appserver, "_brain", lambda c: Brain())
    chat = appserver.new_chat("General", "t")
    asyncio.run(appserver.ask(chat, "hello"))
    assert seen == [True] and not answering.busy()


def test_a_lost_answer_is_owned_up_to_at_start(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    now = time.time()
    lost = appserver.new_chat("Yugur", "lost")
    lost["messages"] = [{"role": "me", "text": "Show me, I said show me visually.", "ts": now - 600}]
    appserver.save_chat(lost)
    fine = appserver.new_chat("Yugur", "fine")
    fine["messages"] = [{"role": "me", "text": "hi", "ts": now - 600}, {"role": "jarvis", "text": "hello", "ts": now - 590}]
    appserver.save_chat(fine)
    old = appserver.new_chat("Yugur", "old")
    old["messages"] = [{"role": "me", "text": "last week", "ts": now - 8 * 86400}]
    appserver.save_chat(old)
    assert appserver.tell_lost_answers(now) == [lost["id"]]
    last = appserver.load_chat(lost["id"])["messages"][-1]
    assert last["role"] == "jarvis" and "restarted before I could answer" in last["text"] and "Show me" in last["text"]
    assert appserver.tell_lost_answers(now) == []          # told once: its last message is Jarvis's now


def test_a_phone_sending_only_silence_is_noticed_once():
    w = SilenceWatch(after=6)
    zeros, voice = bytes(1920), b"\x10\x00" * 960
    assert not w.heard(voice, 0)
    assert not w.heard(zeros, 1) and not w.heard(zeros, 5)
    assert w.heard(zeros, 7.5)                               # 6 s of pure zeros: tell him
    assert not w.heard(zeros, 20)                            # once per silent stretch
    assert not w.heard(voice, 21) and not w.heard(zeros, 22) and w.heard(zeros, 28.5)   # back, then gone again


def test_restart_script_waits_for_answers():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "scripts" / "restart_when_idle.sh").read_text()
    assert "var/state/answering" in src
