"""A follow-up that must wait for another ticket waits for that ticket, not for a guessed clock time
(ticket 5290b190: the video follow-up sat Queued until 21:00 although its blocker was done at 17:26)."""
import sqlite3
import subprocess
import sys
from pathlib import Path

from core import board, localboard, runner

HERE = Path(__file__).parent.parent


def _board(monkeypatch, tmp_path):
    monkeypatch.setattr(localboard, "DB", tmp_path / "boards.db")
    monkeypatch.setattr(localboard, "_ready", set())


def test_follow_up_starts_as_soon_as_its_blocker_is_done(monkeypatch, tmp_path):
    _board(monkeypatch, tmp_path)
    first = localboard.add_ticket("Taxi", "match the mockups", "")
    video = localboard.add_ticket("Taxi", "render the video", "", after=first.ref)
    r = runner.Runner(lambda *a: None)
    video = localboard.get(video.ref)
    assert video.after == first.ref
    assert board.waits_for(video) == f"ticket {first.ref} to finish"
    assert "waits for ticket" in video.line()
    assert [t.ref for t in r.pick([video])] == []                 # blocker still queued
    localboard.set_fields(first.ref, jarvis="Working")
    assert [t.ref for t in r.pick([video])] == []                 # still running
    localboard.set_fields(first.ref, jarvis="Done", status="Done")
    assert [t.ref for t in r.pick([video])] == [video.ref]        # right away, no clock time involved
    assert board.waits_for(video) == ""


def test_needs_me_or_failed_blocker_keeps_it_waiting_but_a_missing_one_does_not(monkeypatch, tmp_path):
    _board(monkeypatch, tmp_path)
    first = localboard.add_ticket("Taxi", "first", "")
    later = localboard.add_ticket("Taxi", "later", "", after=first.ref)
    for state in ("Needs me", "Failed"):
        localboard.set_fields(first.ref, jarvis=state)
        assert not localboard.get(later.ref).startable()
    localboard.set_fields(first.ref, jarvis="", status="Dropped")       # closed on the board counts as over
    assert localboard.get(later.ref).startable()
    localboard.set_fields(later.ref, after="gone1234")                  # a ref that isn't there never blocks
    assert localboard.get(later.ref).startable()


def test_old_board_gets_the_after_column(monkeypatch, tmp_path):
    db = tmp_path / "boards.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tickets (ref TEXT PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL, "
                "body TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '', jarvis TEXT NOT NULL DEFAULT '', "
                "note TEXT NOT NULL DEFAULT '', sprint TEXT NOT NULL DEFAULT '', due TEXT NOT NULL DEFAULT '', "
                "notion_url TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, edited TEXT NOT NULL, "
                "start_after TEXT NOT NULL DEFAULT '')")
    con.execute("INSERT INTO tickets (ref, project, title, created, edited) VALUES ('old1', 'P', 'old', 'x', 'x')")
    con.commit()
    con.close()
    monkeypatch.setattr(localboard, "DB", db)
    monkeypatch.setattr(localboard, "_ready", set())
    assert localboard.get("old1").after == "" and localboard.get("old1").startable()


def test_cli_add_and_show_after(tmp_path):
    env_db = tmp_path / "boards.db"
    code = f"""
import sys; sys.argv = sys.argv[:1] + sys.argv[1:]
from core import localboard, config
from pathlib import Path
localboard.DB = Path({str(env_db)!r})
p = next(iter(config.PROJECTS))
import importlib.util
spec = importlib.util.spec_from_file_location("cli", {str(HERE / 'scripts' / 'board.py')!r})
cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
localboard.is_local = lambda name: True
first = localboard.add_ticket("P", "first", "")
sys.argv = ["board.py", "add", "P", "second", "--queue", "--after", first.ref.upper()]
cli.main()
t = [x for x in localboard.tickets("P") if x.title == "second"][0]
assert t.after == first.ref, t.after
sys.argv = ["board.py", "add", "P", "third", "--after", "nope0000"]
assert cli.main() == 1
sys.argv = ["board.py", "edit", t.ref, "--after", ""]
cli.main()
assert localboard.get(t.ref).after == ""
print("ok")
"""
    out = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True)
    assert out.stdout.strip().endswith("ok"), out.stderr
