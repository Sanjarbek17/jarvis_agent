"""RAM guard for the runner (Sanjar, 2026-11-03: "keep in mind the RAM of the Mac"; it has 8 GB).

Before starting another worker next to running ones, look at macOS memory: the kernel's pressure
level (1 normal, 2 warning, 4 critical) and `memory_pressure`'s free percentage. A new worker only
starts next to others when pressure is normal and enough is free; otherwise its ticket just waits
in the queue. With no worker running, one always starts, so the queue can't stall.
"""
import re
import subprocess

MIN_FREE_PCT = 20          # below this, no second/third worker
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


def can_start(running: int, snap: dict | None = None) -> tuple[bool, str]:
    """May the runner start one more worker while `running` already run? (ok, why not)."""
    if running == 0:
        return True, ""
    s = snap or snapshot()
    if s["level"] is not None and s["level"] != NORMAL:
        return False, f"memory pressure is {LEVELS.get(s['level'], s['level'])}"
    if s["free_pct"] is not None and s["free_pct"] < MIN_FREE_PCT:
        return False, f"only {s['free_pct']}% RAM free"
    return True, ""


def describe(s: dict | None = None) -> str:
    s = s or snapshot()
    swap = (f", swap {s['swap_used_mb'] / 1024:.1f}/{s['swap_total_mb'] / 1024:.1f} GB"
            if s["swap_used_mb"] is not None and s["swap_total_mb"] else "")
    return f"pressure {LEVELS.get(s['level'], s['level'])}, {s['free_pct']}% free{swap}"
