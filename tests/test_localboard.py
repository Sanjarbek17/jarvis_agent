import asyncio
import json
import subprocess
import sys
from pathlib import Path

from starlette.testclient import TestClient

from core import appserver, board, localboard

ROOT = Path(__file__).parent.parent


def _local(monkeypatch, *names):
    """Projects Local (and any others named) kept on the Mac."""
    names = names or ("Local",)
    monkeypatch.setattr(board, "PROJECTS", {n: {"local": True} for n in names})
    monkeypatch.setattr(board, "reload_projects", lambda: False)
    monkeypatch.setattr(localboard, "PROJECTS", board.PROJECTS)
    monkeypatch.setattr(localboard, "reload_projects", lambda: False)


def test_ticket_goes_through_the_whole_life_on_the_mac(monkeypatch):
    _local(monkeypatch)
    async def life():
        t = await board.add_ticket("Local", "Fix login", "the words", "test")
        assert t.jarvis == "Queued" and t.status == "To do"
        assert [q.ref for q in await board.queued()] == [t.ref]
        await board.update(t, jarvis="Working", note="starting", stage="doing")
        await board.append_log(t.page_id, ["• editing a.py", "Result: done"])
        t = await board.find_any(t.ref)
        assert (t.jarvis, t.status, t.note) == ("Working", "In progress", "starting")
        assert not await board.queued() and await board.with_jarvis("Working")
        await board.mark_done(t, "works")
        t = await board.find_any(t.ref)
        assert (t.jarvis, t.status) == ("Done", "Done")
        assert t.ref in await board.status_text()
        lines = await board.body_lines(t.page_id)
        assert lines[0].endswith("the words") and "Result: done" in lines
    asyncio.run(life())


def test_unqueue_drop_and_move_between_local_boards(monkeypatch):
    _local(monkeypatch, "A", "B")
    async def run():
        t = await board.add_ticket("A", "One", "text", "test")
        await board.unqueue(t, "by mistake")
        t = await board.find_any(t.ref)
        assert t.jarvis == "" and t.note == "by mistake"
        assert await board.find(t.ref) is None and await board.find_any(t.ref)
        q = await board.add_ticket("A", "Two", "text", "test")
        new = await board.move(q, "B", "wrong board")
        assert new.project == "B" and new.jarvis == "Queued"
        assert (await board.find_any(q.ref)).status == "Dropped"
        assert "Moved here from A" in "\n".join(await board.body_lines(new.page_id))
    asyncio.run(run())


def test_copying_a_ticket_again_refreshes_it():
    row = {"ref": "abcd1234", "project": "L", "title": "Old", "created": "2026-01-01", "edited": "2026-01-01"}
    localboard.copy_ticket(row)
    localboard.copy_ticket({**row, "title": "New", "notion_url": "https://notion.so/x"})
    assert localboard.get("abcd1234").title == "New"
    assert localboard.details("abcd1234")["notion_url"] == "https://notion.so/x"


def test_nothing_but_the_known_fields_can_be_set():
    try:
        localboard.set_fields("x", **{"ref = 'y', title": "z"})
    except ValueError:
        return
    raise AssertionError("an unknown field went into the SQL")


def _client(monkeypatch):
    monkeypatch.setattr(appserver, "_authorized", lambda req: True)
    monkeypatch.setattr(appserver, "worker_status", lambda: {"workers": [{"ref": "busybusy"}]})
    return TestClient(appserver.app)


def test_the_app_can_use_a_board(monkeypatch):
    _local(monkeypatch)
    c = _client(monkeypatch)
    ref = c.post("/api/board", json={"project": "Local", "title": "New one", "text": "words"}).json()["ticket"]["ref"]
    data = c.get("/api/board", params={"project": "Local"}).json()
    assert data["local"] and data["columns"] == ["To do"] and data["tickets"][0]["jarvis"] == ""
    assert c.post(f"/api/tickets/{ref}", json={"action": "queue"}).json()["ticket"]["jarvis"] == "Queued"
    t = c.post(f"/api/tickets/{ref}", json={"action": "edit", "title": "Better", "text": "more", "status": "Test"}).json()
    assert t["ticket"]["title"] == "Better" and t["ticket"]["status"] == "Test"
    assert c.get(f"/api/tickets/{ref}").json()["text"] == "more"
    assert c.post(f"/api/tickets/{ref}", json={"action": "done"}).json()["ticket"]["status"] == "Done"
    assert c.post(f"/api/tickets/{ref}", json={"action": "nonsense"}).status_code == 400
    assert c.get("/api/tickets/none").status_code == 404
    assert c.get("/api/board", params={"project": "Nope"}).status_code == 404


def test_the_app_leaves_a_running_ticket_alone(monkeypatch):
    _local(monkeypatch)
    localboard.copy_ticket({"ref": "busybusy", "project": "Local", "title": "Busy", "jarvis": "Working",
                            "created": "2026-01-01", "edited": "2026-01-01"})
    r = _client(monkeypatch).post("/api/tickets/busybusy", json={"action": "drop"})
    assert r.status_code == 409 and localboard.get("busybusy").jarvis == "Working"


def test_the_board_routes_need_a_login(monkeypatch):
    monkeypatch.setattr(appserver, "_authorized", lambda req: False)
    c = TestClient(appserver.app)
    assert c.get("/api/board", params={"project": "x"}).status_code == 401
    assert c.get("/api/tickets/x").status_code == 401
    assert c.post("/api/tickets/x", json={}).status_code == 401


def test_columns_run_from_todo_to_dropped():
    names = ["Dropped", "Done", "Test", "In Progress", "Backend Todo", "Backlog"]
    assert sorted(names, key=appserver._column_rank) == ["Backend Todo", "Backlog", "In Progress", "Test", "Done", "Dropped"]


def test_worker_gets_told_how_to_read_a_local_ticket(monkeypatch):
    _local(monkeypatch)
    from core import runner
    t = localboard.add_ticket("Local", "T", "b")
    intro = runner._ticket_intro(t)
    assert f"scripts/board.py show {t.ref}" in intro and "Notion tools" not in intro


def test_notion_blocks_become_text():
    sys.path.insert(0, str(ROOT / "scripts"))
    import notion_to_local as n
    rich = [{"plain_text": "hi", "href": None}]
    assert n.block_line({"type": "heading_2", "heading_2": {"rich_text": rich}}) == "## hi"
    assert n.block_line({"type": "to_do", "to_do": {"rich_text": rich, "checked": True}}) == "- [x] hi"
    assert n.block_line({"type": "image", "image": {"type": "file", "file": {"url": "x"}}}) == "[image: only on the Notion page]"
    assert n.block_line({"type": "paragraph", "paragraph": {"rich_text": []}}) is None
    props = {"Name": {"type": "title", "title": []}, "Status": {"type": "select", "select": {"name": "x"}},
             "Epic": {"type": "select", "select": {"name": "Login"}}, "Size": {"type": "number", "number": 3},
             "Tags": {"type": "multi_select", "multi_select": [{"name": "a"}, {"name": "b"}]}}
    assert n.extra_fields(props) == ["Epic: Login", "Size: 3", "Tags: a, b"]
    assert [n.status_name(x) for x in ("In Progress", "DONE", "Not started", "Test", "Backend Todo")] == \
        ["In progress", "Done", "To do", "Test", "Backend Todo"]


def test_cli_help_works():
    assert "board.py show" in subprocess.run([sys.executable, str(ROOT / "scripts/board.py"), "--help"],
                                             capture_output=True, text=True, cwd=ROOT).stdout
