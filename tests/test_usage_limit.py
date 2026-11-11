"""Ticket d23f4461: workers stopped by the Claude usage limit go back to the queue and continue after the reset."""
import asyncio
import json
import time
from types import SimpleNamespace as NS

from core import runner
from core.board import Ticket

RESET = time.time() + 3600
LIMIT_STREAM = [
    {"type": "system", "subtype": "init", "session_id": "s1"},
    {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": RESET,
                                                     "rateLimitType": "five_hour"}, "session_id": "s1"},
    {"type": "assistant", "message": {"content": [{"type": "text",
                                                   "text": "You've hit your 5-hour limit · resets 6:10pm (Asia/Samarkand)"}]}},
    {"type": "result", "result": "You've hit your 5-hour limit · resets 6:10pm (Asia/Samarkand)", "is_error": False},
]


def card(ref="aet-25"):
    return Ticket(page_id=f"p-{ref}", ref=ref, project="AI English Teacher", title="t", status="", jarvis="Queued",
                  note="", sprint="", url="u")


class Proc:
    """A worker that already wrote its whole stream into its log file (stdout) and exited."""
    def __init__(self, events, out):
        out.write(b"".join(json.dumps(e).encode() + b"\n" for e in events))
        self.returncode, self.pid = 0, 1

    async def wait(self):
        return self.returncode


def run_worker(monkeypatch, tmp_path, events, ref="aet-25"):
    writes, told, prompts = [], [], []

    async def update(t, **kw):
        writes.append(kw)

    async def append_log(pid, lines):
        pass

    async def exec_(*cmd, **kw):
        prompts.append(cmd[cmd.index("-p") + 1])
        return Proc(events, kw["stdout"])

    async def notify(c, kind, text, photos):
        told.append((kind, text))

    monkeypatch.setattr(runner.board, "update", update)
    monkeypatch.setattr(runner.board, "append_log", append_log)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(runner, "project_dirs", lambda p: [tmp_path])
    monkeypatch.setattr(runner, "SHOTS", tmp_path)
    monkeypatch.setattr(runner, "LOGS", tmp_path)
    monkeypatch.setattr(runner, "SESSIONS", tmp_path / "sessions.json")
    r = runner.Runner(notify)
    asyncio.run(r._work(card(ref)))
    return writes, told, prompts


def test_hit_limit_detection():
    assert runner.hit_limit("You've hit your 5-hour limit · resets 6:10pm", False, 0) > time.time()
    assert runner.hit_limit("", True, 123.0) == 123.0
    assert runner.hit_limit("Claude AI usage limit reached|1793797800", True, 0)
    assert not runner.hit_limit("Done. Added a rate limit to the API.", False, 0)
    assert not runner.hit_limit("Worker exited with code 1.", True, 0)


def test_limited_worker_is_paused_not_failed(monkeypatch, tmp_path):
    writes, told, _ = run_worker(monkeypatch, tmp_path, LIMIT_STREAM)
    final = writes[-1]
    assert final["jarvis"] == "Queued" and "usage limit" in final["note"]
    assert not any(w.get("jarvis") == "Failed" for w in writes)
    state = json.loads(runner.USAGE_LIMIT.read_text())
    assert state["until"] == RESET and state["refs"] == ["aet-25"]
    assert [k for k, _ in told] == ["paused"]
    _, told2, _ = run_worker(monkeypatch, tmp_path, LIMIT_STREAM, ref="aet-26")   # second one: no second note
    assert told2 == [] and json.loads(runner.USAGE_LIMIT.read_text())["refs"] == ["aet-25", "aet-26"]


def test_resumed_worker_is_told_to_continue(monkeypatch, tmp_path):
    runner.USAGE_LIMIT.write_text(json.dumps({"refs": ["aet-25"]}))
    done = [{"type": "result", "result": "All done.", "is_error": False}]
    writes, _, prompts = run_worker(monkeypatch, tmp_path, done)
    assert "stopped mid-work by the Claude usage limit" in prompts[0]
    assert writes[-1]["jarvis"] == "Done" and json.loads(runner.USAGE_LIMIT.read_text())["refs"] == []
    _, _, prompts2 = run_worker(monkeypatch, tmp_path, done, ref="aet-9")
    assert "usage limit" not in prompts2[0]


def test_no_new_workers_until_reset_then_resume(monkeypatch):
    tickets = [NS(ref="a1", project="A", due="")]

    async def queued():
        return tickets

    monkeypatch.setattr(runner.board, "queued", queued)
    started = []

    async def go():
        r = runner.Runner(lambda *a: None)

        async def work(t):
            started.append(t.ref)

        r._work = work
        runner.USAGE_LIMIT.write_text(json.dumps({"until": time.time() + 600, "refs": ["a1"]}))
        await r._tick()
        assert started == [] and "usage limit" in r.wait_reason
        runner.USAGE_LIMIT.write_text(json.dumps({"until": time.time() - 3 * runner.LIMIT_MARGIN, "refs": ["a1"]}))
        await r._tick()
        await asyncio.sleep(0)
        assert started == ["a1"] and "until" not in json.loads(runner.USAGE_LIMIT.read_text())

    asyncio.run(go())
