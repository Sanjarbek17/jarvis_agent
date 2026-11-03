#!/usr/bin/env python3
"""Run one heavy job at a time on the 8 GB Mac (Gradle/Flutter builds, emulators, video renders,
Docker builds, big model runs). Workers wrap such commands with it:

    /Users/sanjarbek/dev/side_project/projects/jarvis/scripts/heavy.py ./gradlew assembleRelease

It waits until no other heavy job holds var/heavy.lock (saying who does), runs the command, and
releases the lock when it ends, also on errors or kills. Exit code = the command's.
"""
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

LOCK = Path(__file__).resolve().parent.parent / "var" / "heavy.lock"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.seek(0)
            print(f"heavy.py: waiting for another heavy job to finish: {f.read().strip() or '?'}", flush=True)
            fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        f.truncate()
        f.write(f"pid {os.getpid()} since {time.strftime('%H:%M:%S')} in {os.getcwd()}: {' '.join(sys.argv[1:])[:200]}")
        f.flush()
        return subprocess.call(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
