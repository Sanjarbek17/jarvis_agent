import asyncio
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

from core import ramguard, runner

OK = {"level": 1, "free_pct": 10, "swap_used_mb": 100.0, "swap_total_mb": 1024.0,
      "available_mb": 3000, "worker_mb": 400, "heavy": False}


def test_first_worker_always_starts():
    assert ramguard.can_start(0, {**OK, "level": 4, "available_mb": 10}) == (True, "")


def test_another_worker_when_memory_fits_one_more():
    assert ramguard.need_mb(OK) == 1024 + 400
    assert ramguard.can_start(3, OK)[0]                            # no fixed count: 3 running is fine
    ok, why = ramguard.can_start(1, {**OK, "available_mb": 1200})
    assert not ok and "needs 1424 MB" in why and "1200 MB available" in why


def test_reserve_grows_under_pressure_full_swap_or_heavy_job():
    assert ramguard.need_mb({**OK, "level": 2}) == 2048 + 400
    assert ramguard.need_mb({**OK, "level": 2, "free_pct": 45}) == 1024 + 400   # kernel "warning" at 45% free: not real
    assert ramguard.need_mb({**OK, "swap_used_mb": 950.0}) == 2048 + 400
    assert ramguard.need_mb({**OK, "heavy": True}) == 1024 + 1500 + 400
    assert ramguard.can_start(1, {**OK, "level": 4}) == (False, "memory pressure is critical")


def test_available_from_vm_stat():
    vm = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 100.\nPages active: 999.\n"
          "Pages inactive: 200.\nPages speculative: 20.\nPages purgeable: 4.\n")
    assert ramguard.available_mb(vm) == (100 + 200 + 20 + 4) * 16384 // (1024 * 1024)


def test_missing_readings_are_careful():
    assert ramguard.can_start(1, {**OK, "available_mb": None, "free_pct": None})[0]
    assert not ramguard.can_start(1, {**OK, "available_mb": None, "free_pct": None, "level": 2})[0]


def test_aet2_case_starts_at_half_memory_free(monkeypatch):
    """Ticket c84fea43: 1814 MB in vm_stat pages, 52% free per macOS, kernel 'warning', 2 running -> start."""
    monkeypatch.setattr(ramguard, "TOTAL_MB", 8192)
    s = {**OK, "level": 2, "free_pct": 52, "available_mb": 1814, "worker_mb": 300, "swap_used_mb": 2042.0,
         "swap_total_mb": 3072.0}
    assert ramguard.have_mb(s) == 52 * 8192 // 100
    assert ramguard.can_start(2, s) == (True, "")
    tight = {**s, "free_pct": 12, "available_mb": 900}           # really low: held, reserve doubled
    ok, why = ramguard.can_start(2, tight)
    assert not ok and "needs 2348 MB" in why
    assert not ramguard.can_start(2, {**s, "level": 4})[0]      # critical still holds everything


def test_real_snapshot_reads_this_mac():
    s = ramguard.snapshot()
    assert s["level"] in (1, 2, 4) and 0 <= s["free_pct"] <= 100 and s["available_mb"] > 0


def test_runner_holds_second_ticket_when_memory_is_tight(monkeypatch):
    tickets = [NS(ref="a1", project="A", due=""), NS(ref="b1", project="B", due="")]

    async def queued():
        return tickets

    started = []
    monkeypatch.setattr(runner.board, "queued", queued)
    monkeypatch.setattr(runner.ramguard, "snapshot", lambda worker_pids=None: {**OK, "level": 4})

    async def go():
        r = runner.Runner(lambda *a: None)

        async def work(t):
            started.append(t.ref)
            await asyncio.sleep(0.2)

        r._work = work
        await r._tick()
        await asyncio.sleep(0)
        assert started == ["a1"] and list(r.running) == ["A"]      # B held: pressure is critical
        monkeypatch.setattr(runner.ramguard, "snapshot", lambda worker_pids=None: OK)
        monkeypatch.setattr(runner, "START_GAP", 0)
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
    env = {**__import__("os").environ, "HEAVY_PARALLEL_MB": "999999999"}     # never "plenty of RAM"
    env["HEAVY_LOCK"] = str(tmp_path / "heavy.lock")    # not the real lock: a real build may hold it
    procs = [subprocess.Popen([sys.executable, str(heavy), sys.executable, "-c", job], stdout=subprocess.PIPE, env=env)
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


def test_starts_as_many_as_memory_allows_one_at_a_time(monkeypatch):
    tickets = [T("a1", "A"), T("b1", "B"), T("c1", "C"), T("d1", "D")]

    async def queued():
        return tickets

    monkeypatch.setattr(runner.board, "queued", queued)
    monkeypatch.setattr(runner, "START_GAP", 0)
    started = []
    box = {}
    # each running worker uses 600 MB of the 3000 available: A, B, C fit, the 4th doesn't
    monkeypatch.setattr(runner.ramguard, "snapshot",
                        lambda worker_pids=None: {**OK, "available_mb": 3000 - 600 * len(box["r"].running)})

    async def go():
        r = box["r"] = runner.Runner(lambda *a: None)

        async def work(t):
            started.append(t.ref)
            await asyncio.sleep(0.3)

        r._work = work
        for _ in range(5):
            await r._tick()
            await asyncio.sleep(0)
        assert started == ["a1", "b1", "c1"] and "d1 [D] waits: needs 1424 MB" in r.wait_reason
        await asyncio.sleep(0.4)

    asyncio.run(go())


def test_one_start_per_gap(monkeypatch):
    async def queued():
        return [T("a1", "A"), T("b1", "B")]

    monkeypatch.setattr(runner.board, "queued", queued)
    monkeypatch.setattr(runner.ramguard, "snapshot", lambda worker_pids=None: {**OK, "available_mb": 9000})
    started = []

    async def go():
        r = runner.Runner(lambda *a: None)

        async def work(t):
            started.append(t.ref)
            await asyncio.sleep(0.2)

        r._work = work
        await r._tick()
        await r._tick()
        await asyncio.sleep(0)
        assert started == ["a1"]                     # b1 waits for START_GAP, memory is measured first
        await asyncio.sleep(0.3)

    asyncio.run(go())


def test_no_new_workers_while_a_restart_waits(monkeypatch, tmp_path):
    flag = runner.RESTART_PENDING
    flag.touch()

    async def queued():
        return [T("a1", "A")]

    monkeypatch.setattr(runner.board, "queued", queued)
    started = []

    async def go():
        r = runner.Runner(lambda *a: None)

        async def work(t):
            started.append(t.ref)

        r._work = work
        await r._tick()
        assert started == []
        flag.unlink()
        await r._tick()
        await asyncio.sleep(0)
        assert started == ["a1"]

    asyncio.run(go())


def test_requeue_after_the_urgent_one_started(monkeypatch, tmp_path):
    f = runner.REQUEUE_AFTER
    f.write_text('{"urg1": ["big1"]}')
    state = {"urg1": "Queued", "big1": ""}
    updates = []

    async def find_any(ref):
        return NS(ref=ref, jarvis=state[ref])

    async def update(t, **kw):
        updates.append((t.ref, kw["jarvis"]))

    monkeypatch.setattr(runner.board, "find_any", find_any)
    monkeypatch.setattr(runner.board, "update", update)

    async def go():
        r = runner.Runner(lambda *a: None)
        await r._requeue_after()
        assert updates == [] and "urg1" in f.read_text()          # urgent one still waiting
        state["urg1"] = "Working"
        r._requeue_checked = 0
        await r._requeue_after()
        assert updates == [("big1", "Queued")] and f.read_text() == "{}"

    asyncio.run(go())


def test_second_heavy_job_runs_alongside_when_ram_allows(tmp_path):
    heavy = Path(__file__).resolve().parent.parent / "scripts" / "heavy.py"
    log = tmp_path / "log"
    job = f"import time; open({str(log)!r}, 'a').write('start '); time.sleep(1); open({str(log)!r}, 'a').write('end ')"
    env = {**__import__("os").environ, "HEAVY_PARALLEL_MB": "0"}             # always "plenty of RAM"
    env["HEAVY_LOCK"] = str(tmp_path / "heavy.lock")
    t0 = time.monotonic()
    procs = [subprocess.Popen([sys.executable, str(heavy), sys.executable, "-c", job], stdout=subprocess.PIPE, env=env)
             for _ in range(2)]
    [p.communicate() for p in procs]
    assert log.read_text().split()[:2] == ["start", "start"] and time.monotonic() - t0 < 1.9
