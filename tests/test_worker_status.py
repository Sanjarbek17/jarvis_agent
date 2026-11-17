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
