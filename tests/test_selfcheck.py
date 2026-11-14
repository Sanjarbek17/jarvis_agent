import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from core import runner as runner_mod, selfcheck


def T(ref, project="P", title="t", note="", due=""):
    return NS(ref=ref, project=project, title=title, note=note, due=due)


def setup(monkeypatch, tmp_path, queued=(), states=None):
    states = states or {}
    monkeypatch.setattr(selfcheck, "SEEN", tmp_path / "seen.json")
    monkeypatch.setattr(selfcheck.ramguard, "describe", lambda s=None: "pressure normal")

    async def q():
        return list(queued)

    async def wj(state):
        return list(states.get(state, []))

    monkeypatch.setattr(selfcheck.board, "queued", q)
    monkeypatch.setattr(selfcheck.board, "with_jarvis", wj)
    runner_mod.HEARTBEAT.touch()
    return NS(current={}, wait_reason="", pick=lambda q: list(q))


def run(r, alive=True, reply="Did it.", asked=None, sent=None, restarted=None):
    async def ask(text):
        asked.append(text)
        return reply

    async def send(text):
        sent.append(text)

    return asyncio.run(selfcheck.run_once(r, lambda: alive, lambda: restarted.append(1), ask, send))


def test_quiet_system_costs_nothing(monkeypatch, tmp_path):
    r = setup(monkeypatch, tmp_path)
    asked, sent = [], []
    assert run(r, asked=asked, sent=sent) == "" and asked == [] and sent == []


def test_dead_runner_is_restarted_and_reported(monkeypatch, tmp_path):
    r = setup(monkeypatch, tmp_path)
    asked, sent, restarted = [], [], []
    run(r, alive=False, asked=asked, sent=sent, restarted=restarted)
    assert restarted == [1] and sent == ["The ticket runner had stopped; I restarted it."]


def test_new_items_go_to_the_brain_once(monkeypatch, tmp_path):
    r = setup(monkeypatch, tmp_path, states={"Needs me": [T("n1", note="which colour?")],
                                             "Failed": [T("f1")], "Working": [T("w1")]})
    asked, sent = [], []
    run(r, asked=asked, sent=sent)
    assert len(asked) == 1 and "[SELF-CHECK]" in asked[0]
    for line in ("Needs me: n1", "Failed: f1", "Working with no live worker: w1"):
        assert line in asked[0]
    assert sent == ["Did it."]
    run(r, asked=asked, sent=sent)                       # same items again: not repeated
    assert len(asked) == 1


def test_no_reply_sends_nothing(monkeypatch, tmp_path):
    r = setup(monkeypatch, tmp_path, states={"Failed": [T("f1")]})
    asked, sent = [], []
    run(r, reply="NO_REPLY", asked=asked, sent=sent)
    assert len(asked) == 1 and sent == []


def test_due_soon_and_not_started_is_flagged(monkeypatch, tmp_path):
    soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    later = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    r = setup(monkeypatch, tmp_path, queued=[T("d1", due=soon), T("d2", due=later)])
    asked, sent = [], []
    run(r, asked=asked, sent=sent)
    assert "Due in 2.0 h and not started: d1" in asked[0] and "d2" not in asked[0]


def test_live_worker_is_not_lost(monkeypatch, tmp_path):
    r = setup(monkeypatch, tmp_path, states={"Working": [T("w1")]})
    r.current = {"P": T("w1")}
    asked, sent = [], []
    run(r, asked=asked, sent=sent)
    assert asked == []
