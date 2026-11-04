import asyncio
import json
import time
from types import SimpleNamespace as NS

from core import runner
from core.board import Ticket


def card(ref="aet-11", jarvis="Working"):
    return Ticket(page_id=f"p-{ref}", ref=ref, project="AI English Teacher", title="t", status="", jarvis=jarvis,
                  note="", sprint="", url="u")


class Board:
    def __init__(self, fail=0, working=()):
        self.fail, self.writes, self.logs, self.working = fail, [], [], list(working)

    async def update(self, t, **kw):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("GraphQL: API rate limit exceeded")
        self.writes.append((t.ref, kw.get("jarvis"), kw.get("note")))

    async def append_log(self, pid, lines):
        self.logs.append((pid, lines))

    async def with_jarvis(self, state):
        return self.working if state == "Working" else []


def patch(monkeypatch, b):
    for name in ("update", "append_log", "with_jarvis"):
        monkeypatch.setattr(runner.board, name, getattr(b, name))


def test_failed_write_is_kept_and_written_later(monkeypatch):
    b = Board(fail=3)
    patch(monkeypatch, b)
    r = runner.Runner(lambda *a: None)
    assert asyncio.run(r._write(card(), jarvis="Needs me", note="q?", log_lines=["result"])) is False
    saved = json.loads(runner.PENDING.read_text())["aet-11"]
    assert saved["jarvis"] == "Needs me" and saved["log"] == ["result"]
    asyncio.run(r._flush_pending())                         # GitHub is back
    assert b.writes == [("aet-11", "Needs me", "q?")] and b.logs == [("p-aet-11", ["result"])]
    assert json.loads(runner.PENDING.read_text()) == {}


def test_write_retries_before_giving_up(monkeypatch):
    b = Board(fail=2)
    patch(monkeypatch, b)
    assert asyncio.run(runner.Runner(lambda *a: None)._write(card(), jarvis="Done")) is True
    assert not runner.PENDING.exists()


def test_restart_requeues_instead_of_failing(monkeypatch):
    b = Board(working=[card("a"), card("b")])
    patch(monkeypatch, b)
    runner.PENDING.write_text(json.dumps({"b": {"page_id": "p-b"}}))   # b's result is saved: not re-queued
    notes = []
    r = runner.Runner(lambda *a: notes.append(a))
    asyncio.run(r._recover())
    assert b.writes == [("a", "Queued", "queued again: its worker stopped when Jarvis restarted")] and notes == []


def test_gives_up_after_two_requeues(monkeypatch):
    b = Board(working=[card("a")])
    patch(monkeypatch, b)
    runner.REQUEUED.write_text(json.dumps({"a": 2}))
    told = []

    async def notify(*a):
        told.append(a[1])

    asyncio.run(runner.Runner(notify)._recover())
    assert b.writes[-1][1] == "Failed" and told == ["failed"]


def test_unreachable_board_at_start_leaves_ticket_queued(monkeypatch):
    b = Board(fail=99)
    patch(monkeypatch, b)
    monkeypatch.setattr(runner, "project_dirs", lambda p: [runner.HERE])
    monkeypatch.setattr(runner, "SHOTS", runner.HEARTBEAT.parent)
    launched = []

    async def fake_exec(*a, **k):
        launched.append(a)

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_exec)
    r = runner.Runner(lambda *a: None)
    asyncio.run(r._work(card(jarvis="Queued")))
    assert launched == [] and "aet-11" in r.finished and r.current == {}


def test_inplace_runner_restart_does_not_recover(monkeypatch):
    called = []
    r = runner.Runner(lambda *a: None)

    async def recover():
        called.append(1)

    async def tick():
        raise asyncio.CancelledError

    r._recover, r._tick = recover, tick
    try:
        asyncio.run(r.run_forever(recover=False))
    except asyncio.CancelledError:
        pass
    assert called == []
