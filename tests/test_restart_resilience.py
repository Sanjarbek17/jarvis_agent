"""Ticket 2475b845: a network drop made Jarvis restart every ~15 s (Telethon gave up after 5 tries) and every
restart cut off the running workers. Now Telegram retries forever, no worker starts while offline, workers run
detached and a restarted Jarvis adopts them, and the crash in notify (runner.from_sanjar) is gone."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from core import ghboard, runner
from core.board import Ticket

HERE = Path(__file__).resolve().parent.parent

WORKER = """
import json, sys, time
steps = int(sys.argv[1])
def out(e):
    sys.stdout.write(json.dumps(e) + "\\n"); sys.stdout.flush()
out({"type": "system", "subtype": "init", "session_id": "s-1"})
for i in range(steps):
    out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
         "input": {"command": f"echo {i}", "description": f"step {i}"}}]}})
    time.sleep(0.4)
out({"type": "result", "result": "All done.", "is_error": False})
"""


def card(jarvis="Queued"):
    return Ticket(page_id="p-1", ref="r-1", project="Yugur", title="Y4", status="", jarvis=jarvis,
                  note="", sprint="", url="u")


def setup(monkeypatch, tmp_path, steps=6):
    script = tmp_path / "fake_claude.py"          # "claude" in the command line: it passes the reused-pid check
    script.write_text(WORKER)
    real_exec = asyncio.create_subprocess_exec
    log = {"update": [], "append": [], "told": []}

    async def exec_(*cmd, **kw):
        return await real_exec(sys.executable, str(script), str(steps), **{k: v for k, v in kw.items() if k != "env"})

    async def update(t, **kw):
        log["update"].append(kw)

    async def append_log(pid, lines):
        log["append"].append(lines)

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(runner.board, "update", update)
    monkeypatch.setattr(runner.board, "append_log", append_log)
    monkeypatch.setattr(runner, "project_dirs", lambda p: [tmp_path])
    monkeypatch.setattr(runner, "SHOTS", tmp_path)
    monkeypatch.setattr(runner, "LOGS", tmp_path)
    monkeypatch.setattr(runner, "SESSIONS", tmp_path / "sessions.json")
    return log


def test_worker_survives_a_jarvis_stop_and_is_adopted(monkeypatch, tmp_path):
    log = setup(monkeypatch, tmp_path)

    async def notify(c, kind, text, photos):
        log["told"].append(kind)

    async def first_jarvis():
        r = runner.Runner(notify)
        task = asyncio.create_task(r._work(card()))
        await asyncio.sleep(1.2)                   # a few steps in
        task.cancel()                              # Jarvis stops (launchd restart, crash...)
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task

    asyncio.run(first_jarvis())
    w = runner._workers()["r-1"]
    assert runner._pid_alive(w["pid"])             # the worker kept running without Jarvis
    pushed_before = sum(len(x) for x in log["append"])

    async def with_jarvis(state):
        return [card("Working")] if state == "Working" else []

    monkeypatch.setattr(runner.board, "with_jarvis", with_jarvis)

    async def second_jarvis():
        r = runner.Runner(notify)
        await r._recover()                         # adopts instead of re-queueing
        assert "r-1" in r.running
        await r.running["r-1"]

    asyncio.run(second_jarvis())
    assert log["told"] == ["done"]
    assert log["update"][-1]["jarvis"] == "Done"
    assert not any("queued again" in (u.get("note") or "") for u in log["update"])
    assert runner._workers() == {}                 # finished: no longer listed
    steps = [s for x in log["append"] for s in x if s.startswith("• ")]
    assert len(steps) == len(set(steps))           # steps from before the restart weren't pushed twice
    assert pushed_before > 0


def test_a_dead_worker_without_a_result_is_requeued_as_before(monkeypatch, tmp_path):
    log = setup(monkeypatch, tmp_path)
    runner._set_worker("r-1", {"pid": 999_999, "project": "Yugur", "log": str(tmp_path / "none.jsonl")})

    async def with_jarvis(state):
        return [card("Working")] if state == "Working" else []

    monkeypatch.setattr(runner.board, "with_jarvis", with_jarvis)
    asyncio.run(runner.Runner(lambda *a: None)._recover())
    assert log["update"][-1]["jarvis"] == "Queued" and runner._workers() == {}


def test_stop_ends_the_detached_worker_and_its_group(monkeypatch, tmp_path):
    log = setup(monkeypatch, tmp_path, steps=50)

    async def notify(c, kind, text, photos):
        log["told"].append(kind)

    async def go():
        r = runner.Runner(notify)
        task = asyncio.create_task(r._work(card()))
        await asyncio.sleep(1.0)
        pid = r.procs["r-1"].pid
        assert r.stop_project("Yugur", "test")
        await asyncio.wait_for(task, 10)
        return pid

    pid = asyncio.run(go())
    time.sleep(0.2)
    assert not runner._pid_alive(pid) and log["told"] == ["stopped"]


def test_no_worker_starts_while_offline(monkeypatch):
    monkeypatch.setattr(runner, "online", lambda timeout=3: False)

    async def queued():
        return [card()]

    monkeypatch.setattr(runner.board, "queued", queued)
    r = runner.Runner(lambda *a: None)
    started = []
    monkeypatch.setattr(r, "_work", lambda t: started.append(t))
    asyncio.run(r._tick())
    assert started == [] and "no internet" in r.wait_reason


def test_notify_can_ask_the_runner_instance_from_sanjar(monkeypatch):
    marks = runner.FROM_SANJAR
    marks.write_text(json.dumps({"r-1": time.time()}))
    r = runner.Runner(lambda *a: None)
    assert r.from_sanjar("r-1") and not r.from_sanjar("r-2")      # crashed with AttributeError at 03:59


def test_telegram_keeps_retrying_instead_of_exiting():
    src = (HERE / "jarvis.py").read_text()
    assert "connection_retries=-1" in src and "auto_reconnect=True" in src


def test_github_board_falls_back_to_the_copy_on_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(ghboard, "SAVED", tmp_path)
    monkeypatch.setattr(ghboard, "_cache", {})
    monkeypatch.setattr(ghboard, "_backoff", {})
    monkeypatch.setattr(ghboard, "projects", lambda: {"AI English Teacher": {"project": 4, "owner": "o", "prefix": "aet"}})
    t = Ticket(page_id="i1", ref="aet-1", project="AI English Teacher", title="x", status="Todo", jarvis="",
               note="", sprint="", url="u")
    ghboard._save("AI English Teacher", [t])

    async def gh(*a, **k):
        raise RuntimeError("gh project item-list 4: GitHub unreachable (no internet?): unknown owner type")

    monkeypatch.setattr(ghboard, "_gh", gh)
    assert asyncio.run(ghboard.tickets("AI English Teacher")) == [t]
