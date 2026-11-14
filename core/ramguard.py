"""RAM guard for the runner: how many workers run is decided by free memory only, no fixed count
(Sanjar, 2026-11-04: "Limit should be according to the ram not hard limit workers"; the Mac has 8 GB).

Another worker starts when the memory that's really available minus a reserve for macOS, Jarvis and
the brain still fits one more worker. "Available" is the larger of free + inactive + speculative +
purgeable pages (vm_stat) and macOS's own "memory free percentage" (memory_pressure, the number
Activity Monitor's pressure is based on): the page count alone ignores what macOS can compress and
held aet-2 at 52% free (ticket c84fea43). The kernel's "warning" level stays on for long stretches on
this 8 GB Mac even at 40%+ free, so it only counts as pressure while free memory is really low. A worker's size is measured: the average memory of the running workers' process trees (a
Claude worker is ~300-500 MB). The reserve doubles under "warning" pressure or nearly full swap and
grows while a heavy job (build, render, emulator: scripts/heavy.py) runs; "critical" holds every new
start. One worker may always run, so the queue can't stall, and running workers are never stopped.
"""
import os
import re
import subprocess
from pathlib import Path

from .paths import VAR

NORMAL = 1                 # kern.memorystatus_vm_pressure_level: 1 normal, 2 warning, 4 critical
LEVELS = {1: "normal", 2: "warning", 4: "critical"}
RESERVE_MB = 1024          # always left for macOS, Jarvis itself and the Claude brains
WORKER_MB = 450            # a worker's size when none runs to measure (Claude worker + its tools)
MIN_WORKER_MB = 300
HEAVY_MB = 1500            # extra headroom while a heavy job holds var/heavy.lock
SWAP_FULL = 0.9            # swap this full counts like "warning"
LOW_FREE_PCT = 25          # "warning" from the kernel only counts when macOS's free percentage is below this
TOTAL_MB = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024))
HEAVY_LOCK = VAR / "heavy.lock"


def _run(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""


def available_mb(vm: str | None = None) -> int | None:
    """Memory that can be had without swapping much: free + inactive + speculative + purgeable."""
    vm = _run("vm_stat") if vm is None else vm
    page = re.search(r"page size of (\d+) bytes", vm)
    if not page:
        return None
    pages = 0
    for key in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
        m = re.search(rf"{key}:\s+(\d+)", vm)
        pages += int(m.group(1)) if m else 0
    return pages * int(page.group(1)) // (1024 * 1024)


def tree_mb(pids: list[int]) -> list[int]:
    """Memory (RSS) of each pid's whole process tree, in MB."""
    rows = [l.split() for l in _run("ps", "-A", "-o", "pid=,ppid=,rss=").splitlines()]
    kids: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for r in rows:
        if len(r) == 3 and all(x.isdigit() for x in r):
            pid, ppid, kb = map(int, r)
            kids.setdefault(ppid, []).append(pid)
            rss[pid] = kb
    out = []
    for p in pids:
        todo, total = [p], 0
        while todo:
            q = todo.pop()
            total += rss.get(q, 0)
            todo += kids.get(q, [])
        out.append(total // 1024)
    return out


def heavy_running() -> bool:
    """Is a heavy job (scripts/heavy.py) holding its lock right now?"""
    import fcntl
    try:
        with open(HEAVY_LOCK) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        pass
    return False


def snapshot(worker_pids: list[int] | None = None) -> dict:
    level = _run("sysctl", "-n", "kern.memorystatus_vm_pressure_level").strip()
    free = re.search(r"free percentage:\s*(\d+)%", _run("memory_pressure", "-Q"))
    swap = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", _run("sysctl", "-n", "vm.swapusage"))
    sizes = [m for m in tree_mb(worker_pids or []) if m > 0]
    return {"level": int(level) if level.isdigit() else None,
            "free_pct": int(free.group(1)) if free else None,
            "swap_total_mb": float(swap.group(1)) if swap else None,
            "swap_used_mb": float(swap.group(2)) if swap else None,
            "available_mb": available_mb(),
            "worker_mb": max(MIN_WORKER_MB, sum(sizes) // len(sizes)) if sizes else WORKER_MB,
            "heavy": heavy_running()}


def under_pressure(s: dict) -> bool:
    """Real memory pressure: nearly full swap, or the kernel's "warning" while free memory is low."""
    swap_full = bool(s.get("swap_total_mb")) and s["swap_used_mb"] / s["swap_total_mb"] >= SWAP_FULL
    pct = s.get("free_pct")
    return swap_full or (s.get("level") == 2 and (pct is None or pct < LOW_FREE_PCT))


def have_mb(s: dict) -> int | None:
    """Memory a new worker can get: vm_stat's reclaimable pages or macOS's free percentage, the larger."""
    pages, pct = s.get("available_mb"), s.get("free_pct")
    by_pct = pct * TOTAL_MB // 100 if pct is not None else None
    vals = [v for v in (pages, by_pct) if v is not None]
    return max(vals) if vals else None


def need_mb(s: dict) -> int:
    """Memory that must be available to start one more worker now."""
    reserve = RESERVE_MB
    if under_pressure(s):
        reserve *= 2
    if s.get("heavy"):
        reserve += HEAVY_MB
    return reserve + s.get("worker_mb", WORKER_MB)


def can_start(running: int, snap: dict | None = None, worker_pids: list[int] | None = None) -> tuple[bool, str]:
    """May the runner start one more worker while `running` already run? (ok, why not)."""
    if running == 0:
        return True, ""
    s = snap or snapshot(worker_pids)
    if s.get("level") == 4:
        return False, "memory pressure is critical"
    have = have_mb(s)
    if have is None:                     # can't measure: be careful, one more only at normal pressure
        return (s.get("level") in (None, NORMAL), "can't read memory")
    need = need_mb(s)
    if have >= need:
        return True, ""
    return False, (f"needs {need} MB free for another worker (~{s.get('worker_mb')} MB each"
                   f"{', a heavy job is running' if s.get('heavy') else ''}), {have} MB available")


def describe(s: dict | None = None) -> str:
    s = s or snapshot()
    swap = (f", swap {s['swap_used_mb'] / 1024:.1f}/{s['swap_total_mb'] / 1024:.1f} GB"
            if s.get("swap_used_mb") is not None and s.get("swap_total_mb") else "")
    return (f"pressure {LEVELS.get(s.get('level'), s.get('level'))}{' (real)' if under_pressure(s) else ''}, "
            f"{have_mb(s)} MB available"
            f", {s.get('free_pct')}% free{swap}")
