"""Ticket 6e558d62: restart_when_idle.sh must not mistake the shell that started it for a waiting restart."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "restart_when_idle.sh"


def waiter_running() -> bool:
    out = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
    rows = [l.split() for l in out.splitlines()]
    return any(len(r) > 1 and r[0].endswith("sh") and r[1].endswith("restart_when_idle.sh") for r in rows)


@pytest.fixture
def copy(tmp_path):
    if waiter_running():
        pytest.skip("a real restart is waiting right now")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "var" / "logs").mkdir(parents=True)
    (tmp_path / "bin").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts")
    fake = tmp_path / "bin" / "launchctl"                # never the real Jarvis: "no pid" ends the script
    fake.write_text("#!/bin/sh\necho 'state = not running'\n")
    fake.chmod(0o755)
    return tmp_path


def run(cmd, root):
    subprocess.run(["/bin/zsh", "-c", cmd], cwd=root, env={**os.environ, "PATH": f"{root}/bin:{os.environ['PATH']}"},
                   timeout=20)
    log_file = root / "var" / "logs" / "restart.log"
    for _ in range(30):
        if log_file.exists():
            break
        time.sleep(0.1)
    return log_file.read_text()


def test_starting_it_from_a_shell_command_is_not_a_waiting_restart(copy):
    log = run("(nohup scripts/restart_when_idle.sh >/dev/null 2>&1 &); sleep 1; true", copy)
    assert "already waiting" not in log and "isn't running under launchd" in log


def test_a_really_waiting_restart_still_blocks_a_second(copy, tmp_path_factory):
    other = tmp_path_factory.mktemp("waiter") / "restart_when_idle.sh"
    other.write_text("sleep 20\n")
    other.chmod(0o755)
    waiter = subprocess.Popen(["/bin/sh", str(other)])
    try:
        time.sleep(0.2)
        assert "already waiting" in run("scripts/restart_when_idle.sh", copy)
    finally:
        waiter.kill()
