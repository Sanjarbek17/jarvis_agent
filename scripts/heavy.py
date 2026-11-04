#!/usr/bin/env python3
"""Run one heavy job at a time on the 8 GB Mac (Gradle/Flutter builds, emulators, video renders,
Docker builds, big model runs). Workers wrap such commands with it:

    /Users/sanjarbek/dev/side_project/projects/jarvis/scripts/heavy.py ./gradlew assembleRelease

It waits until no other heavy job holds var/heavy.lock (saying who does), runs the command, and
releases the lock when it ends, also on errors or kills. Exit code = the command's. A second heavy job
may run next to the first (var/heavy2.lock) only while RAM clearly allows it (PARALLEL_MB available).
"""
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

LOCK = Path(__file__).resolve().parent.parent / "var" / "heavy.lock"
LOCK2 = LOCK.with_name("heavy2.lock")
PARALLEL_MB = int(os.environ.get("HEAVY_PARALLEL_MB", 3500))   # tests set it


def plenty_of_ram() -> bool:
    sys.path.insert(0, str(LOCK.parent.parent))
    try:
        from core import ramguard
        return (ramguard.available_mb() or 0) >= PARALLEL_MB
    except Exception:
        return False


def run(f, argv) -> int:
    f.seek(0)
    f.truncate()
    f.write(f"pid {os.getpid()} since {time.strftime('%H:%M:%S')} in {os.getcwd()}: {' '.join(argv)[:200]}")
    f.flush()
    return subprocess.call(argv)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if plenty_of_ram():                       # room for a second heavy job: take the 2nd slot
                with open(LOCK2, "a+") as f2:
                    try:
                        fcntl.flock(f2, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        return run(f2, sys.argv[1:])
                    except BlockingIOError:
                        pass
            f.seek(0)
            print(f"heavy.py: waiting for another heavy job to finish: {f.read().strip() or '?'}", flush=True)
            fcntl.flock(f, fcntl.LOCK_EX)
        return run(f, sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
