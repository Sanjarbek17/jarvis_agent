"""Ticket 0815c76f: the phone app shows how many workers run and which ticket each is on."""
import json

from starlette.testclient import TestClient

from core import appserver, runner
from core.board import Ticket


def _ticket(ref, project, title):
    return Ticket(page_id="p" + ref, ref=ref, project=project, title=title, status="In Progress",
                  jarvis="Working", note="", sprint="", url=f"https://notion.so/{ref}")


def _log(path, *events):
    path.write_text("\n".join(json.dumps(e) for e in events))


def test_last_step_reads_the_latest_tool_call(tmp_path):
    p = tmp_path / "X-abc.jsonl"
    _log(p, {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "1", "name": "Read",
                                                             "input": {"file_path": "/a/Api.kt"}}]}},
         {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "1", "content": "x"}]}},
         {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "2", "name": "Bash",
                                                         "input": {"description": "Build the APK", "command": "gradlew"}}]}},
         "not json")
    p.write_text(p.read_text() + "\n{broken")
    assert runner.last_step(p)[0] == "running: Build the APK"
    assert runner.last_step(tmp_path / "missing.jsonl") == ("", 0.0)
    _log(p, {"type": "result", "result": "done"})
    assert runner.last_step(p)[0] == "finishing up"


def test_snapshot_and_route(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LOGS", tmp_path)
    r = runner.Runner(notify=None)
    r.current = {"0815c76f": _ticket("0815c76f", "Jarvis App", "Show worker status"),
                 "11112222": _ticket("11112222", "Gas Finder", "Map pins")}
    r.started = {"0815c76f": 200.0, "11112222": 100.0}
    _log(tmp_path / "Jarvis App-0815c76f.jsonl",
         {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "1", "name": "Edit",
                                                         "input": {"file_path": "/x/Ui.kt"}}]}})
    r.wait_reason = "aaaa [Taxi] waits: low RAM (2 running)"
    snap = r.snapshot()
    assert snap["count"] == 2 and snap["waiting"].startswith("aaaa")
    assert [w["project"] for w in snap["workers"]] == ["Gas Finder", "Jarvis App"]     # oldest first
    assert snap["workers"][1]["step"] == "editing Ui.kt" and snap["workers"][0]["step"] == "starting"

    monkeypatch.setenv("APP_TOKEN", "t0k")
    client = TestClient(appserver.app)
    assert client.get("/api/workers").status_code == 401
    monkeypatch.setattr(appserver, "worker_status", None)
    assert client.get("/api/workers", headers={"Authorization": "Bearer t0k"}).json()["count"] == 0
    monkeypatch.setattr(appserver, "worker_status", r.snapshot)
    got = client.get("/api/workers", headers={"Authorization": "Bearer t0k"}).json()
    assert got["count"] == 2 and got["workers"][1]["title"] == "Show worker status"


def test_last_step_reads_antigravity_events(tmp_path):
    p = tmp_path / "Jarvis-8e2e4340.jsonl"
    _log(p,
         {"event": "init", "conversation_id": "c1"},
         {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "read_file",
                                                  "tool_info": {"name": "read_file", "parameters": {"Path": "/core/runner.py"}}}},
         {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "run_command",
                                                  "tool_info": {"name": "run_command",
                                                                "parameters": {"toolSummary": "Run tests",
                                                                               "CommandLine": "pytest tests/"}}}},
         "not json\n{bad")
    assert runner.last_step(p)[0] == "running: Run tests"

    _log(p,
         {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "edit_file",
                                                  "tool_info": {"name": "edit_file", "parameters": {"Path": "/core/runner.py"}}}})
    assert runner.last_step(p)[0] == "editing runner.py"

    _log(p,
         {"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "Fixing the bug now."}})
    assert runner.last_step(p)[0] == "writing: Fixing the bug now."

    _log(p, {"event": "result", "result": {"status": "SUCCESS", "response": "Done."}})
    assert runner.last_step(p)[0] == "finishing up"


def test_snapshot_supports_both_antigravity_and_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LOGS", tmp_path)
    r = runner.Runner(notify=None)
    r.current = {
        "claude-1": _ticket("claude-1", "ProjectA", "Claude Worker"),
        "agy-1": _ticket("agy-1", "ProjectB", "Antigravity Worker"),
    }
    r.started = {"claude-1": 100.0, "agy-1": 200.0}

    # Claude log format
    _log(tmp_path / "ProjectA-claude-1.jsonl",
         {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                         "input": {"command": "git status"}}]}})
    # Antigravity log format
    _log(tmp_path / "ProjectB-agy-1.jsonl",
         {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "edit_file",
                                                  "tool_info": {"name": "edit_file", "parameters": {"Path": "/app/Main.kt"}}}})

    snap = r.snapshot()
    assert snap["count"] == 2
    steps = {w["project"]: w["step"] for w in snap["workers"]}
    assert steps["ProjectA"] == "running: git status"
    assert steps["ProjectB"] == "editing Main.kt"


def test_progress_text_supports_antigravity_events(tmp_path):
    p = tmp_path / "ProjectB-agy1.jsonl"
    _log(p,
         {"event": "init", "timestamp": "2026-11-21T10:00:00Z"},
         {"event": "step_update", "step_update": {"step_index": 1, "state": "ACTIVE", "step_type": "tool",
                                                  "tool_name": "read_file",
                                                  "tool_info": {"name": "read_file", "parameters": {"Path": "/app/App.kt"}}}},
         {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "tool",
                                                  "tool_name": "read_file",
                                                  "tool_info": {"name": "read_file", "parameters": {"Path": "/app/App.kt"},
                                                                "output": "class App {}"}}},
         {"event": "step_update", "step_update": {"step_index": 2, "state": "ACTIVE", "step_type": "tool",
                                                  "tool_name": "run_command",
                                                  "tool_info": {"name": "run_command", "parameters": {"CommandLine": "gradle build"}}}},
         {"event": "result", "result": {"status": "SUCCESS", "response": "Build succeeded.", "duration_seconds": 120.0}})

    text = runner.progress_text(p, running=False)
    assert "Worker log for agy1" in text
    assert "finished" in text
    assert "Result after 2 min" in text
    assert "Build succeeded." in text
    assert "reading App.kt" in text
    assert "running: gradle build" in text


def test_worker_pushes_antigravity_step_updates(monkeypatch, tmp_path):
    writes = []
    logs = []

    async def update(card, **kw):
        writes.append(kw)

    async def append_log(pid, lines):
        logs.extend(lines)

    monkeypatch.setattr(runner.board, "update", update)
    monkeypatch.setattr(runner.board, "append_log", append_log)
    monkeypatch.setattr(runner, "project_dirs", lambda p: [tmp_path])
    monkeypatch.setattr(runner, "SHOTS", tmp_path)
    monkeypatch.setattr(runner, "LOGS", tmp_path)
    monkeypatch.setattr(runner, "SESSIONS", tmp_path / "sessions.json")

    class FakeProc:
        def __init__(self, events, out):
            out.write(b"".join(json.dumps(e).encode() + b"\n" for e in events))
            self.returncode, self.pid = 0, 1

        async def wait(self):
            return self.returncode

    events = [
        {"event": "init", "conversation_id": "c-123"},
        {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "read_file",
                                                 "tool_info": {"name": "read_file", "parameters": {"Path": "/app/App.kt"}},
                                                 "state": "ACTIVE"}},
        {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "run_command",
                                                 "tool_info": {"name": "run_command", "parameters": {"toolSummary": "Run gradle build", "CommandLine": "gradle"}},
                                                 "state": "ACTIVE"}},
        {"event": "result", "result": {"status": "SUCCESS", "response": "Done."}}
    ]

    async def exec_(*cmd, **kw):
        return FakeProc(events, kw["stdout"])

    async def fake_notify(*a):
        pass

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", exec_)
    r = runner.Runner(notify=fake_notify)
    card = _ticket("test-1", "TestProject", "Test Title")
    import asyncio
    asyncio.run(r._work(card))

    notes = [w.get("note") for w in writes if "note" in w]
    assert any("reading App.kt" in str(n) or "running: Run gradle build" in str(n) for n in notes)
    assert any("reading App.kt" in line or "running: Run gradle build" in line for line in logs)


