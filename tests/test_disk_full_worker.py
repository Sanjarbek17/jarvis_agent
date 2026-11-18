"""Ticket 9304deae: the disk filled up while a worker ran. Saving its session id failed, which ended the runner's
following of a healthy worker, and the cleanup then failed too, so the finished ticket stayed "working" for hours
and blocked its project. Bookkeeping writes may fail now; the worker is followed to the end and its slot freed."""
import asyncio
import pathlib
import sys

from core import runner
from core.board import Ticket

WORKER = """
import json, sys, time
for e in [{"type": "system", "subtype": "init", "session_id": "s-1"},
          {"type": "assistant", "session_id": "s-2", "message": {"content": [{"type": "text", "text": "working"}]}},
          {"type": "result", "result": "Video sent.", "is_error": False, "session_id": "s-2"}]:
    sys.stdout.write(json.dumps(e) + "\\n"); sys.stdout.flush(); time.sleep(0.2)
"""


def test_a_full_disk_doesnt_lose_a_finished_worker(monkeypatch, tmp_path):
    script = tmp_path / "fake_claude.py"
    script.write_text(WORKER)
    real_exec, real_write = asyncio.create_subprocess_exec, pathlib.Path.write_text
    writes, told = [], []

    async def exec_(*cmd, **kw):
        return await real_exec(sys.executable, str(script), **{k: v for k, v in kw.items() if k != "env"})

    def write_text(self, *a, **kw):
        if self.name in ("sessions.json", "workers.json"):
            raise OSError(28, "No space left on device")
        return real_write(self, *a, **kw)

    async def update(t, **kw):
        writes.append(kw)

    async def append_log(pid, lines):
        pass

    async def notify(c, kind, text, photos):
        told.append(kind)

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(pathlib.Path, "write_text", write_text)
    monkeypatch.setattr(runner.board, "update", update)
    monkeypatch.setattr(runner.board, "append_log", append_log)
    monkeypatch.setattr(runner, "project_dirs", lambda p: [tmp_path])
    monkeypatch.setattr(runner, "SHOTS", tmp_path)
    monkeypatch.setattr(runner, "LOGS", tmp_path)
    monkeypatch.setattr(runner, "SESSIONS", tmp_path / "sessions.json")
    monkeypatch.setattr(runner, "WORKERS", tmp_path / "workers.json")
    card = Ticket(page_id="p-1", ref="5c060aaa", project="Taxi App", title="video", status="", jarvis="Queued",
                  note="", sprint="", url="u")
    r = runner.Runner(notify)
    asyncio.run(asyncio.wait_for(r._work(card), 20))
    assert writes[-1]["jarvis"] == "Done" and told == ["done"]
    assert r.current == {} and r.procs == {} and r.count("Taxi App") == 0


def test_the_session_is_written_only_when_it_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "SESSIONS", tmp_path / "sessions.json")
    runner._save_session("P", "s-1")
    stamp = (tmp_path / "sessions.json").stat().st_mtime_ns
    runner._save_session("P", "s-1")
    assert (tmp_path / "sessions.json").stat().st_mtime_ns == stamp
