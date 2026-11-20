"""Ticket 3a61de45: workers opened pages in Sanjar's own Chrome and never closed them (30 tabs)."""
import asyncio
import importlib.util
import json
from pathlib import Path

from core import runner
from tests.test_usage_limit import card

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("no_raw_chrome", HERE / "scripts/hooks/no_raw_chrome.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
spec2 = importlib.util.spec_from_file_location("real_chrome", HERE / "scripts/real_chrome.py")
rc = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(rc)


def test_the_hook_blocks_the_ways_workers_opened_pages():
    for c in ('open -na "Google Chrome" --args --profile-directory="Profile 4" "https://mail.google.com/#search"',
              'for p in a b; do open -na "Google Chrome" --args --profile-directory="$p" "https://console.cloud.google.com"; done',
              'open https://example.com', 'open "file:///tmp/a.html"', 'cd studio && open reel.html',
              'open -a "Google Chrome" index.html', "osascript -e 'tell application \"Google Chrome\" to make new window'"):
        assert hook.blocked(c), c
    for c in ('open -g -a Calculator', '.venv/bin/python scripts/real_chrome.py open https://x', 'git log',
              'grep -n "open https" a.py', 'python -c "open(\'a.html\').read()"', 'open -a Finder .'):
        assert not hook.blocked(c), c


def test_workers_get_the_hook_and_their_owner_and_their_windows_are_closed(monkeypatch, tmp_path):
    seen, closed = {}, []

    class Proc:
        def __init__(self, out):
            out.write(json.dumps({"type": "result", "result": "Done.", "is_error": False}).encode() + b"\n")
            self.returncode, self.pid = 0, 1

        async def wait(self):
            return 0

    async def exec_(*cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return Proc(kw["stdout"])

    async def nothing(*a, **k):
        pass

    async def close(owner, older_than=-1):
        closed.append(owner)

    monkeypatch.setattr(runner, "JARVIS_ENGINE", "claude")
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(runner.board, "update", nothing)
    monkeypatch.setattr(runner.board, "append_log", nothing)
    monkeypatch.setattr(runner, "close_chrome_of", close)                 # over conftest's no-op
    monkeypatch.setattr(runner, "project_dirs", lambda p: [tmp_path])
    monkeypatch.setattr(runner, "SHOTS", tmp_path)
    monkeypatch.setattr(runner, "LOGS", tmp_path)
    monkeypatch.setattr(runner, "SESSIONS", tmp_path / "s.json")
    asyncio.run(runner.Runner(nothing)._work(card("aet-9")))
    settings = json.loads(seen["cmd"][seen["cmd"].index("--settings") + 1])
    assert "no_raw_chrome.py" in settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert seen["env"]["JARVIS_OWNER"] == "aet-9" and closed == ["aet-9"]
    assert "real_chrome.py" in seen["cmd"][seen["cmd"].index("-p") + 1]          # the prompt says how


def test_the_helper_never_trusts_applescript_while_a_script_chrome_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(rc, "OPENED", tmp_path / "opened.json")
    rc._save([{"url": "https://x", "owner": "t", "at": 1e12, "window": 5}])
    monkeypatch.setattr(rc, "automation_running", lambda: True)
    assert rc.close("t") == 0 and len(rc._load()) == 1                  # kept for the next sweep
    monkeypatch.setattr(rc, "automation_running", lambda: False)
    monkeypatch.setattr(rc, "window_ids", lambda: {5})
    calls = []
    monkeypatch.setattr(rc, "_osa", lambda s: calls.append(s) or "")
    assert rc.close("t") == 1 and rc._load() == [] and "whose id is 5" in calls[-1]



def test_the_hook_blocks_copying_his_real_chrome_profile():
    """Ticket c09280c2: Yugur workers copied Default's cookies into their own Chromes; Google signed him out."""
    for c in ('SRC=~/Library/Application\\ Support/Google/Chrome; cp "$SRC/Default/Cookies" .chrome/Default/',
              'sqlite3 "$HOME/Library/Application Support/Google/Chrome/Default/Cookies" ".backup \'x\'"',
              'cp ~/Library/Application\\ Support/Google/Chrome/Local\\ State .chrome/',
              'node x.mjs --user-data-dir="/Users/sanjarbek/Library/Application Support/Google/Chrome"'):
        assert hook.touches_real_profile(c), c
    for c in ('sqlite3 marketing/chrome-profile/Default/Cookies "select 1"', 'open -a Calculator', 'git status'):
        assert not hook.touches_real_profile(c), c


# --- ticket 81332fee: every Claude Jarvis starts gets the hook; pages he asked to see stay ----------------------

def test_mac_tasks_run_with_the_same_hook(monkeypatch, tmp_path):
    from core import hookset, mac
    seen = {}

    async def exec_(*cmd, **kw):
        seen["cmd"] = list(cmd)
        raise RuntimeError("stop here")

    monkeypatch.setattr(mac, "JARVIS_ENGINE", "claude")
    monkeypatch.setattr(mac.asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(mac, "LOGS", tmp_path)
    monkeypatch.setattr(mac, "SHOTS", tmp_path)
    w = mac.MacWorker()
    asyncio.run(w.run("open the weather site", "show me the weather"))
    assert seen["cmd"][seen["cmd"].index("--settings") + 1] == hookset.SETTINGS
    assert runner.WORKER_SETTINGS == hookset.SETTINGS
    assert "--keep" in seen["cmd"][seen["cmd"].index("-p") + 1]


def test_a_page_he_asked_to_see_survives_sweeps_but_not_a_direct_close(monkeypatch, tmp_path):
    monkeypatch.setattr(rc, "OPENED", tmp_path / "opened.json")
    monkeypatch.setattr(rc, "automation_running", lambda: False)
    monkeypatch.setattr(rc, "window_ids", lambda: {5, 6})
    calls = []
    monkeypatch.setattr(rc, "_osa", lambda s: calls.append(s) or "")
    hour_ago = __import__("time").time() - 3600
    rc._save([{"url": "https://weather", "owner": "mac", "at": hour_ago, "window": 5, "keep": True},
              {"url": "https://step", "owner": "mac", "at": hour_ago, "window": 6}])
    assert rc.close("mac", older_than=60) == 1 and "whose id is 6" in calls[-1]       # the sweep: only the step page
    assert [r["url"] for r in rc._load()] == ["https://weather"]
    assert rc.close("", window=5) == 1 and rc._load() == []                           # closed on purpose: yes
