import asyncio
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

from core import ramguard, runner

OK = {"level": 1, "free_pct": 40, "swap_used_mb": 100.0, "swap_total_mb": 1024.0}


def test_first_worker_always_starts():
    assert ramguard.can_start(0, {**OK, "level": 4, "free_pct": 3}) == (True, "")


def test_slots_follow_free_ram():
    assert ramguard.slots({**OK, "free_pct": 55}) == 3
    assert ramguard.slots({**OK, "free_pct": 25}) == 2
    assert ramguard.slots({**OK, "free_pct": 12}) == 1
    assert ramguard.slots({**OK, "level": 2, "free_pct": 30}) == 2
    assert ramguard.slots({**OK, "level": 2, "free_pct": 20}) == 1
    assert ramguard.slots({**OK, "level": 4, "free_pct": 60}) == 1


def test_can_start_up_to_the_slots():
    assert ramguard.can_start(2, {**OK, "free_pct": 55})[0]
    assert not ramguard.can_start(3, {**OK, "free_pct": 55})[0]
    ok, why = ramguard.can_start(1, {**OK, "level": 4})
    assert not ok and "memory allows 1" in why


def test_missing_readings_dont_block():
    assert ramguard.can_start(1, {"level": None, "free_pct": None, "swap_used_mb": None, "swap_total_mb": None})[0]


def test_real_snapshot_reads_this_mac():
    s = ramguard.snapshot()
    assert s["level"] in (1, 2, 4) and 0 <= s["free_pct"] <= 100


def test_runner_holds_second_ticket_when_memory_is_tight(monkeypatch):
    tickets = [NS(ref="a1", project="A", due=""), NS(ref="b1", project="B", due="")]

    async def queued():
        return tickets

    started = []
    monkeypatch.setattr(runner.board, "queued", queued)
    monkeypatch.setattr(runner.ramguard, "snapshot", lambda: {**OK, "level": 4})

    async def go():
        r = runner.Runner(lambda *a: None)

        async def work(t):
            started.append(t.ref)
            await asyncio.sleep(0.2)

        r._work = work
        await r._tick()
        await asyncio.sleep(0)
        assert started == ["a1"] and list(r.running) == ["A"]      # B held: pressure is critical
        monkeypatch.setattr(runner.ramguard, "snapshot", lambda: OK)
        await r._tick()
        await asyncio.sleep(0)
        assert started == ["a1", "b1"]
        await asyncio.sleep(0.3)

    asyncio.run(go())


def test_heavy_jobs_run_one_at_a_time(tmp_path):
    heavy = Path(__file__).resolve().parent.parent / "scripts" / "heavy.py"
    log = tmp_path / "log"
    job = f"import time; open({str(log)!r}, 'a').write('start '); time.sleep(1); open({str(log)!r}, 'a').write('end ')"
    t0 = time.monotonic()
    procs = [subprocess.Popen([sys.executable, str(heavy), sys.executable, "-c", job], stdout=subprocess.PIPE)
             for _ in range(2)]
    outs = [p.communicate()[0].decode() for p in procs]
    assert log.read_text().split() == ["start", "end", "start", "end"]     # never overlapped
    assert time.monotonic() - t0 >= 2
    assert any("waiting for another heavy job" in o for o in outs)


def T(ref, project, due=""):
    return NS(ref=ref, project=project, due=due)


def test_pick_one_per_project_in_queue_order():
    r = runner.Runner(lambda *a: None)
    q = [T("a1", "A"), T("a2", "A"), T("b1", "B"), T("c1", "C")]
    assert [t.ref for t in r.pick(q)] == ["a1", "b1", "c1"]


def test_pick_skips_running_projects_and_gives_turns_fairly():
    r = runner.Runner(lambda *a: None)
    r.running = {"A": object()}
    r.last_start = {"B": 200.0, "C": 100.0}       # C waited longest since its last worker
    q = [T("a1", "A"), T("b1", "B"), T("c1", "C"), T("d1", "D")]     # D never ran: first
    assert [t.ref for t in r.pick(q)] == ["d1", "c1", "b1"]


def test_pick_puts_deadlines_first():
    r = runner.Runner(lambda *a: None)
    q = [T("a1", "A"), T("b1", "B", "2026-11-05"), T("h1", "H", "2026-11-04T19:30:00.000+05:00"),
         T("h2", "H")]
    assert [t.ref for t in r.pick(q)] == ["h1", "b1", "a1"]
    q2 = [T("x1", "X"), T("x2", "X", "2026-11-04")]                    # within a project too
    assert [t.ref for t in r.pick(q2)] == ["x2"]


def test_three_light_workers_when_memory_allows(monkeypatch):
    tickets = [T("a1", "A"), T("b1", "B"), T("c1", "C"), T("d1", "D")]

    async def queued():
        return tickets

    monkeypatch.setattr(runner.board, "queued", queued)
    monkeypatch.setattr(runner.ramguard, "snapshot", lambda: {**OK, "free_pct": 55})
    started = []

    async def go():
        r = runner.Runner(lambda *a: None)

        async def work(t):
            started.append(t.ref)
            await asyncio.sleep(0.1)

        r._work = work
        await r._tick()
        await asyncio.sleep(0)
        assert started == ["a1", "b1", "c1"]          # 3 slots, D waits
        await asyncio.sleep(0.2)

    asyncio.run(go())
