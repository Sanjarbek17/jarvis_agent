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


def test_second_worker_needs_normal_pressure_and_free_ram():
    assert ramguard.can_start(1, OK) == (True, "")
    assert ramguard.can_start(1, {**OK, "level": 2}) == (False, "memory pressure is warning")
    assert ramguard.can_start(1, {**OK, "free_pct": 12}) == (False, "only 12% RAM free")


def test_missing_readings_dont_block():
    assert ramguard.can_start(1, {"level": None, "free_pct": None, "swap_used_mb": None, "swap_total_mb": None})[0]


def test_real_snapshot_reads_this_mac():
    s = ramguard.snapshot()
    assert s["level"] in (1, 2, 4) and 0 <= s["free_pct"] <= 100


def test_runner_holds_second_ticket_when_memory_is_tight(monkeypatch):
    tickets = [NS(ref="a1", project="A"), NS(ref="b1", project="B")]

    async def queued():
        return tickets

    started = []
    monkeypatch.setattr(runner.board, "queued", queued)
    monkeypatch.setattr(runner.ramguard, "snapshot", lambda: {**OK, "level": 2})

    async def go():
        r = runner.Runner(lambda *a: None)

        async def work(t):
            started.append(t.ref)
            await asyncio.sleep(0.2)

        r._work = work
        await r._tick()
        await asyncio.sleep(0)
        assert started == ["a1"] and list(r.running) == ["A"]      # B held: pressure is "warning"
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
