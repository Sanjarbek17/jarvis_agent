"""Ticket ac668ebe: a worker gave its final answer at 11:33 but left a watcher running for the 19:30 reel. The
claude CLI keeps the process alive while its background work runs, and the runner counted a live process as a
working worker, so it held its slot and a waiting restart blocked every new ticket for 40 minutes.
Now the ticket is over at its final answer: leftover background work gets BG_AFTER_RESULT, then it's stopped.
Later checks go on the board as follow-ups that wait (start_after)."""
import asyncio
import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

from core import localboard, runner
from core.board import Ticket

HERE = Path(__file__).resolve().parent.parent

WORKER = """
import json, sys, time
mode = sys.argv[1]
def out(e):
    sys.stdout.write(json.dumps(e) + "\\n"); sys.stdout.flush()
out({"type": "system", "subtype": "init", "session_id": "s-1"})
out({"type": "system", "subtype": "task_started", "task_id": "w1", "description": "Watch tonight's reel"})
out({"type": "result", "result": "Fixed it. The watcher is running for tonight's reel.", "is_error": False})
if mode == "short":            # the background work ends soon: the CLI wakes the model, which reports and exits
    time.sleep(0.5)
    out({"type": "system", "subtype": "task_notification", "task_id": "w1", "status": "completed"})
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": "It posted."}]}})
    out({"type": "result", "result": "Fixed it, and tonight's reel posted.", "is_error": False})
else:
    time.sleep(60)             # a watcher for hours: only the background work keeps it alive
"""


def card():
    return Ticket(page_id="p-1", ref="r-1", project="Haydovchi darslari", title="Autopilot", status="",
                  jarvis="Queued", note="", sprint="", url="u")


def run(monkeypatch, tmp_path, mode):
    script = tmp_path / "fake_claude.py"
    script.write_text(WORKER)
    real_exec = asyncio.create_subprocess_exec
    told, writes, pids = [], [], []

    async def exec_(*cmd, **kw):
        p = await real_exec(sys.executable, str(script), mode, **{k: v for k, v in kw.items() if k != "env"})
        pids.append(p.pid)
        return p

    async def update(t, **kw):
        writes.append(kw)

    async def append_log(pid, lines):
        pass

    async def notify(c, kind, text, photos):
        told.append((kind, text))

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(runner.board, "update", update)
    monkeypatch.setattr(runner.board, "append_log", append_log)
    monkeypatch.setattr(runner, "project_dirs", lambda p: [tmp_path])
    monkeypatch.setattr(runner, "SHOTS", tmp_path)
    monkeypatch.setattr(runner, "LOGS", tmp_path)
    monkeypatch.setattr(runner, "SESSIONS", tmp_path / "sessions.json")
    monkeypatch.setattr(runner, "BG_AFTER_RESULT", 1.5)
    started = time.monotonic()
    asyncio.run(asyncio.wait_for(runner.Runner(notify)._work(card()), 20))
    return told, pids, time.monotonic() - started


def test_a_finished_worker_doesnt_keep_its_slot_for_a_long_watcher(monkeypatch, tmp_path):
    told, pids, took = run(monkeypatch, tmp_path, "long")
    assert took < 10                                       # not the watcher's 60 s
    assert told and told[0][0] == "done"
    assert "Fixed it" in told[0][1] and "Jarvis stopped it: Watch tonight's reel" in told[0][1]
    assert len(pids) == 1 and not runner._pid_alive(pids[0], check_cmd=False)   # stopped, not resumed
    assert "r-1" not in runner._workers()


def test_background_work_that_ends_soon_still_finishes_the_ticket(monkeypatch, tmp_path):
    told, pids, _ = run(monkeypatch, tmp_path, "short")
    assert told == [("done", "Fixed it, and tonight's reel posted.")]
    assert len(pids) == 1


def test_a_follow_up_waits_for_its_time(monkeypatch):
    r = runner.Runner(lambda *a: None)
    later = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 3600))
    past = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 60))
    a = Ticket(page_id="local:a", ref="a", project="P", title="check tonight", status="", jarvis="Queued",
               note="", sprint="", url="", start_after=later)
    b = Ticket(page_id="local:b", ref="b", project="P", title="other", status="", jarvis="Queued",
               note="", sprint="", url="")
    c = Ticket(page_id="local:c", ref="c", project="Q", title="due now", status="", jarvis="Queued",
               note="", sprint="", url="", start_after=past)
    assert [t.ref for t in r.pick([a, b, c])] == ["b", "c"]   # P's next ticket goes ahead of its waiting follow-up
    assert Ticket(page_id="", ref="x", project="", title="", status="", jarvis="", note="", sprint="", url="",
                  start_after="nonsense").startable()        # a bad value never blocks a ticket


def test_board_keeps_start_after_and_upgrades_old_boards(monkeypatch, tmp_path):
    db = tmp_path / "boards.db"
    con = sqlite3.connect(db)                                 # a board made before start_after existed
    con.executescript(localboard.SCHEMA)
    con.execute("INSERT INTO tickets (ref, project, title, created, edited) VALUES ('old1', 'P', 'old', 'x', 'x')")
    con.commit()
    con.close()
    monkeypatch.setattr(localboard, "DB", db)
    monkeypatch.setattr(localboard, "_ready", set())
    assert localboard.get("old1").start_after == ""
    t = localboard.add_ticket("P", "Check the reel", "did it post?", start_after="2026-11-14 19:45")
    assert localboard.get(t.ref).start_after == "2026-11-14 19:45" and localboard.get(t.ref).jarvis == "Queued"
    localboard.set_fields(t.ref, start_after="")
    assert localboard.get(t.ref).startable()


def test_start_after_accepts_a_bare_time():
    spec = importlib.util.spec_from_file_location("board_cli", HERE / "scripts" / "board.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    noon = time.mktime(time.strptime("2026-11-14 12:00", "%Y-%m-%d %H:%M"))
    assert cli.start_time("19:30", noon) == "2026-11-14 19:30"
    assert cli.start_time("09:00", noon) == "2026-11-15 09:00"      # already passed today: tomorrow
    assert cli.start_time("2026-11-20 08:15") == "2026-11-20 08:15" and cli.start_time("") == ""
