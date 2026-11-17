"""Ticket d23f4461: workers stopped by the Claude usage limit go back to the queue and continue after the reset."""
import asyncio
import json
import time
from types import SimpleNamespace as NS

from core import runner, usagewatch as _uw
from core.board import Ticket

REAL_PROBE = _uw.probe        # tests/conftest.py swaps it for a no-op per test; one test runs the real one

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
    notes = []

    async def note(t):
        notes.append(t)

    monkeypatch.setattr(runner.usagewatch, "sender", note)
    writes, told, _ = run_worker(monkeypatch, tmp_path, LIMIT_STREAM)
    final = writes[-1]
    assert final["jarvis"] == "Queued" and "usage limit" in final["note"]
    assert not any(w.get("jarvis") == "Failed" for w in writes)
    state = json.loads(runner.USAGE_LIMIT.read_text())
    assert state["until"] == RESET and state["refs"] == ["aet-25"]
    assert told == [] and len(notes) == 1 and "used up" in notes[0]        # one note, straight to him (e6e75d74)
    _, told2, _ = run_worker(monkeypatch, tmp_path, LIMIT_STREAM, ref="aet-26")   # second one: no second note
    assert told2 == [] and len(notes) == 1 and json.loads(runner.USAGE_LIMIT.read_text())["refs"] == ["aet-25", "aet-26"]


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

        async def work(t, adopt=None):
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


# --- ticket 59f4e775: Sanjar reset the usage early, but the runner kept waiting for the old reset time ------------

def hold_run(monkeypatch, set_at, reading):
    from core import usagewatch
    tickets = [NS(ref="a1", project="A", due="")]

    async def queued():
        return tickets

    monkeypatch.setattr(runner.board, "queued", queued)
    started, probes = [], []

    async def probe():
        probes.append(1)
        return True

    monkeypatch.setattr(usagewatch, "probe", probe)

    async def go():
        r = runner.Runner(lambda *a: None)

        async def work(t, adopt=None):
            started.append(t.ref)

        r._work = work
        runner.USAGE_LIMIT.write_text(json.dumps({"until": time.time() + 7200, "set_at": set_at, "refs": ["a1"]}))
        if reading:
            usagewatch.observe(reading)
        await r._tick()
        await asyncio.sleep(0)
        return r

    r = asyncio.run(go())
    return r, started, probes


def test_an_early_reset_ends_the_hold_at_once(monkeypatch):
    allowed = {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": time.time() + 18000, "utilization": 0.0}
    r, started, probes = hold_run(monkeypatch, time.time() - 60, allowed)
    assert started == ["a1"] and "until" not in json.loads(runner.USAGE_LIMIT.read_text())


def test_while_held_the_live_usage_is_read_now_and_then(monkeypatch):
    r, started, probes = hold_run(monkeypatch, time.time() - 60, None)
    assert started == [] and probes == [1] and "usage limit" in r.wait_reason


def test_a_reading_from_before_the_hit_does_not_end_it(monkeypatch):
    from core import usagewatch
    allowed = {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": time.time() + 18000, "utilization": 0.5}
    usagewatch.observe(allowed)
    r, started, probes = hold_run(monkeypatch, time.time() + 5, None)    # the hit came after that reading
    assert started == []


def test_an_early_reset_also_sends_the_reset_note_now():
    from core import usagewatch
    hit = time.time() + 7200
    usagewatch.observe({"status": "rejected", "rateLimitType": "five_hour", "resetsAt": hit})
    assert usagewatch.check_reset() == []
    usagewatch.observe({"status": "allowed", "rateLimitType": "five_hour", "resetsAt": time.time() + 18000})
    assert usagewatch.check_reset() == ["✅ The Claude 5-hour limit has reset. Workers are running again."]


def test_the_probe_reads_the_rate_limit_event(monkeypatch):
    from core import usagewatch
    lines = [json.dumps({"type": "system", "subtype": "init"}),
             json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour",
                                                                          "resetsAt": time.time() + 18000, "utilization": 0.02}}),
             json.dumps({"type": "result", "result": "ok"})]

    class P:
        async def communicate(self):
            return ("\n".join(lines)).encode(), b""

    seen = {}

    async def exec_(*cmd, **kw):
        seen["cmd"] = cmd
        return P()

    monkeypatch.setattr(usagewatch.asyncio, "create_subprocess_exec", exec_)
    assert asyncio.run(REAL_PROBE()) is True and "haiku" in seen["cmd"]
    assert usagewatch.allowed_since(time.time() - 5) and usagewatch.status_line().startswith("Claude 5-hour usage: 2%")
