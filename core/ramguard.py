"""RAM guard for the runner (Sanjar, 2026-11-03: "keep in mind the RAM of the Mac"; it has 8 GB;
2026-11-04: "it should not spare but do not use too much").

How many workers may run follows macOS memory: the kernel's pressure level (1 normal, 2 warning,
4 critical) and `memory_pressure`'s free percentage. slots(): normal and plenty free -> 3, normal or
warning with a fair amount free -> 2, otherwise 1. One worker may always run, so the queue can't
stall. Heavy builds/renders are kept to one at a time separately (scripts/heavy.py).
"""
import re
import subprocess

MIN_FREE_PCT = 20          # below this, no second/third worker
THIRD_FREE_PCT = 30        # a third worker needs normal pressure and this much free
WARN_FREE_PCT = 25         # under "warning" pressure, a second worker needs this much free
MAX_SLOTS = 3
NORMAL = 1                 # kern.memorystatus_vm_pressure_level: 1 normal, 2 warning, 4 critical
LEVELS = {1: "normal", 2: "warning", 4: "critical"}


def _run(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""


def snapshot() -> dict:
    """{level, free_pct, swap_used_mb, swap_total_mb}; missing values are None."""
    level = _run("sysctl", "-n", "kern.memorystatus_vm_pressure_level").strip()
    free = re.search(r"free percentage:\s*(\d+)%", _run("memory_pressure", "-Q"))
    swap = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", _run("sysctl", "-n", "vm.swapusage"))
    return {"level": int(level) if level.isdigit() else None,
            "free_pct": int(free.group(1)) if free else None,
            "swap_total_mb": float(swap.group(1)) if swap else None,
            "swap_used_mb": float(swap.group(2)) if swap else None}


def slots(snap: dict | None = None) -> int:
    """How many workers the Mac can carry right now (1..MAX_SLOTS)."""
    s = snap or snapshot()
    level, free = s["level"], s["free_pct"]
    if level is None and free is None:
        return 2                                  # can't tell: the old fixed limit
    free = 100 if free is None else free
    if level in (None, NORMAL):
        return 3 if free >= THIRD_FREE_PCT else 2 if free >= MIN_FREE_PCT else 1
    if level == 2:                                # warning: common on 8 GB with swap in use
        return 2 if free >= WARN_FREE_PCT else 1
    return 1                                      # critical


def can_start(running: int, snap: dict | None = None) -> tuple[bool, str]:
    """May the runner start one more worker while `running` already run? (ok, why not)."""
    if running == 0:
        return True, ""
    s = snap or snapshot()
    n = slots(s)
    if running < n:
        return True, ""
    return False, f"{running} running and memory allows {n} ({describe(s)})"


def describe(s: dict | None = None) -> str:
    s = s or snapshot()
    swap = (f", swap {s['swap_used_mb'] / 1024:.1f}/{s['swap_total_mb'] / 1024:.1f} GB"
            if s["swap_used_mb"] is not None and s["swap_total_mb"] else "")
    return f"pressure {LEVELS.get(s['level'], s['level'])}, {s['free_pct']}% free{swap}"
