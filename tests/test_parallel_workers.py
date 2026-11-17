"""Ticket e60b042f: several workers on one project at once. The first works in the main checkout; each extra one
gets a git worktree of every repo of the project on branch ticket-<ref>, its own Claude session, and the runner
removes the worktree when it's done (keeping one that holds uncommitted work)."""
import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace as NS

from core import runner
from core.board import Ticket

FAKE = """
import json, os, subprocess, sys, time
out = lambda e: (sys.stdout.write(json.dumps(e) + "\\n"), sys.stdout.flush())
out({"type": "system", "subtype": "init", "session_id": "s-" + os.path.basename(os.getcwd())})
if sys.argv[1] == "commit":                     # the worker commits on its branch and merges it into dev
    open("work.txt", "w").write("x")
    subprocess.run(["git", "add", "-A"]); subprocess.run(["git", "commit", "-qm", "work"])
    subprocess.run(["git", "checkout", "-q", "dev"]); subprocess.run(["git", "merge", "-q", "--no-ff", "-m", "m", sys.argv[2]])
elif sys.argv[1] == "dirty":                    # leaves uncommitted work behind
    open("half.txt", "w").write("x")
time.sleep(float(sys.argv[3]))
out({"type": "result", "result": "done", "is_error": False})
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def make_repo(tmp_path):
    repo = tmp_path / "proj"
    (repo / "app").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t"), git(repo, "config", "user.name", "t")
    (repo / "app" / "a.txt").write_text("a")
    git(repo, "add", "-A"), git(repo, "commit", "-qm", "init"), git(repo, "branch", "dev")
    return repo


def card(ref):
    return Ticket(page_id=f"local:{ref}", ref=ref, project="P", title=ref, status="", jarvis="Queued",
                  note="", sprint="", url="")


def setup(monkeypatch, tmp_path, repo, modes):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE)
    real_exec = asyncio.create_subprocess_exec
    seen = {}

    async def exec_(*cmd, cwd=None, **kw):
        ref = next(r for r in modes if f"Ticket {r} " in cmd[2])
        seen[ref] = {"cmd": list(cmd), "cwd": cwd}
        mode, wait = modes[ref]
        return await real_exec(sys.executable, str(script), mode, f"ticket-{ref}", str(wait), cwd=cwd,
                               **{k: v for k, v in kw.items() if k != "env"})

    async def update(t, **kw):
        pass

    async def append_log(pid, lines):
        pass

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(runner.board, "update", update)
    monkeypatch.setattr(runner.board, "append_log", append_log)
    monkeypatch.setattr(runner, "project_dirs", lambda p: [repo / "app"])
    monkeypatch.setattr(runner, "SHOTS", tmp_path)
    monkeypatch.setattr(runner, "LOGS", tmp_path)
    monkeypatch.setattr(runner, "SESSIONS", tmp_path / "sessions.json")
    monkeypatch.setattr(runner, "_worker_prompt", lambda t, shots: f"Ticket {t.ref} for project P")
    return seen


def test_second_worker_gets_its_own_worktree_branch_and_session(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    seen = setup(monkeypatch, tmp_path, repo, {"aaaa1111": ("none", 1.5), "bbbb2222": ("commit", 0.2)})
    told = []

    async def notify(c, kind, text, photos):
        told.append((c.ref, kind, text))

    async def go():
        r = runner.Runner(notify)
        first = asyncio.create_task(r._work(card("aaaa1111")))
        await asyncio.sleep(0.3)
        assert r.in_main == {"P": "aaaa1111"}
        await r._work(card("bbbb2222"))                 # while the first one is still busy
        await first

    asyncio.run(go())
    wt = tmp_path / "proj-wt-bbbb2222"
    assert seen["aaaa1111"]["cwd"] == repo / "app"                     # the first: main checkout
    assert seen["bbbb2222"]["cwd"] == wt / "app"                        # the second: its own copy
    prompt = seen["bbbb2222"]["cmd"][2]
    assert "branch\n`ticket-bbbb2222`" in prompt and str(wt) in prompt
    sessions = json.loads((tmp_path / "sessions.json").read_text())
    assert sessions["P"] == "s-app" and sessions["P#bbbb2222"] == "s-app"   # two separate session slots
    assert not wt.exists()                                              # merged: removed afterwards
    assert "ticket-bbbb2222" not in git(repo, "branch")                 # and its merged branch deleted
    assert "work" in git(repo, "log", "--oneline", "dev")
    assert all(kind == "done" for _, kind, _ in told)


def test_a_worktree_with_uncommitted_work_is_kept(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    setup(monkeypatch, tmp_path, repo, {"aaaa1111": ("none", 1.0), "cccc3333": ("dirty", 0.1)})
    told = []

    async def notify(c, kind, text, photos):
        told.append((c.ref, text))

    async def go():
        r = runner.Runner(notify)
        first = asyncio.create_task(r._work(card("aaaa1111")))
        await asyncio.sleep(0.3)
        await r._work(card("cccc3333"))
        await first

    asyncio.run(go())
    wt = tmp_path / "proj-wt-cccc3333"
    assert (wt / "app" / "half.txt").exists() and "ticket-cccc3333" in git(repo, "branch")
    assert any(ref == "cccc3333" and "worktree" in text and "kept" in text for ref, text in told)


def test_pick_lets_a_git_project_run_up_to_its_cap(monkeypatch):
    monkeypatch.setattr(runner, "can_parallel", lambda p: p == "G")
    monkeypatch.setattr(runner, "project_cap", lambda p: 2)
    r = runner.Runner(lambda *a: None)
    q = [NS(ref="g2", project="G", due=""), NS(ref="n2", project="N", due="")]
    r.project_of = {"g1": "G", "n1": "N"}
    assert [t.ref for t in r.pick(q)] == ["g2"]          # N isn't a git repo: one at a time
    r.project_of = {"g1": "G", "g0": "G"}
    assert [t.ref for t in r.pick(q)] == ["n2"]          # G is at its cap


def test_stop_by_project_or_by_ref(monkeypatch):
    r = runner.Runner(lambda *a: None)
    hits = []
    r.current = {"a1": card("a1"), "a2": card("a2")}
    r.procs = {ref: NS(returncode=None, terminate=lambda ref=ref: hits.append(ref)) for ref in r.current}
    assert r.stop_project("a2", "x") and hits == ["a2"]
    assert r.stop_project("P", "x") and hits == ["a2", "a1", "a2"]
    assert r.targets("nope") == []
