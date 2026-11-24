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


def test_busy_ignores_ticket_workers_and_workers_json():
    """Ticket d0bc8d2d: restart_when_idle.sh must not wait for running ticket workers."""
    src = SCRIPT.read_text()
    assert "-p Ticket" not in src
    assert "workers.json" not in src
    assert "answering" in src
    assert "live phone call" in src
    assert "Mac" in src and "task" in src


def test_busy_behavior_with_mocked_ps(tmp_path):
    """Test that busy() returns false for ticket workers, but true for calls/mac tasks."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_ps = bin_dir / "ps"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    ans_dir = tmp_path / "var" / "state" / "answering"
    ans_dir.mkdir(parents=True)

    # Shell script testing busy() extracted from restart_when_idle.sh
    test_sh = scripts_dir / "test_busy.sh"
    test_sh.write_text(f"""#!/bin/sh
JPID=100
CALLS=yes
# extract busy function from actual script
eval "$(sed -n '/^busy() {{/,/^}}/p' "{SCRIPT}")"

# 1. Ticket worker: should NOT be busy
echo '100 /usr/local/bin/claude -p Ticket 16b23e08' > "{tmp_path}/ps_out"
busy && exit 1

# 2. Antigravity Ticket worker: should NOT be busy
echo '100 /usr/local/bin/agy -p Ticket 16b23e08' > "{tmp_path}/ps_out"
busy && exit 2

# 3. Interactive Mac task: SHOULD be busy
echo '100 /usr/local/bin/claude -p Mac task volume down' > "{tmp_path}/ps_out"
busy || exit 3

# 4. Live phone call: SHOULD be busy
echo '100 /usr/bin/python jarvis.py -- live phone call' > "{tmp_path}/ps_out"
busy || exit 4

# 5. Live phone call with CALLS=no (--now-ish): should NOT be busy
CALLS=no
busy && exit 5
CALLS=yes

# 6. Idle processes, but answering file exists: SHOULD be busy
echo '100 python jarvis.py' > "{tmp_path}/ps_out"
busy && exit 6
touch "{ans_dir}/ans-1"
busy || exit 7

exit 0
""")
    test_sh.chmod(0o755)

    fake_ps.write_text(f"""#!/bin/sh
cat "{tmp_path}/ps_out"
""")
    fake_ps.chmod(0o755)

    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    res = subprocess.run([str(test_sh)], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert res.returncode == 0, f"Failed with exit code {res.returncode}: {res.stderr}"
