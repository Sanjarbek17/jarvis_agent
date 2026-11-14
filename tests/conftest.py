import pytest

from core import guard, localboard, macremote, runner, usagewatch


@pytest.fixture(autouse=True)
def isolated_runner_state(tmp_path, monkeypatch):
    """Tests never see Jarvis's real state files (a real requeue list made the runner call Notion)."""
    monkeypatch.setattr(localboard, "DB", tmp_path / "boards.db")
    monkeypatch.setattr(runner, "REQUEUE_AFTER", tmp_path / "requeue_after.json")
    monkeypatch.setattr(runner, "RESTART_PENDING", tmp_path / "restart_pending")
    monkeypatch.setattr(runner, "HEARTBEAT", tmp_path / "runner_heartbeat")
    monkeypatch.setattr(runner, "PENDING", tmp_path / "pending_results.json")
    monkeypatch.setattr(runner, "REQUEUED", tmp_path / "requeued_after_restart.json")
    monkeypatch.setattr(runner, "WRITE_TRIES", (0, 0, 0))
    monkeypatch.setattr(runner, "FROM_SANJAR", tmp_path / "from_sanjar.json")
    monkeypatch.setattr(runner, "USAGE_LIMIT", tmp_path / "usage_limit.json")
    monkeypatch.setattr(runner, "WORKERS", tmp_path / "workers.json")
    monkeypatch.setattr(usagewatch, "FILE", tmp_path / "usage_watch.json")   # never the real usage state

    async def _no_caffeinate(*cmd, stdin="", timeout=30):
        return 0, ""

    monkeypatch.setattr(macremote, "_run", _no_caffeinate)             # tests never wake the real display
    monkeypatch.setattr(macremote, "display_asleep", lambda: False)

    async def _no_chrome(owner, older_than=-1):
        pass

    monkeypatch.setattr(runner, "close_chrome_of", _no_chrome)      # never closes windows in the real Chrome
    monkeypatch.setattr(guard, "lock_screen", lambda: True)          # tests never lock the real Mac...


    async def _no_camera():
        return None, "camera blocked in tests"

    monkeypatch.setattr(guard, "take_photo", _no_camera)             # ...or turn the real camera on (ticket 04cf4647)
    monkeypatch.setattr(guard, "AUTOMATION", tmp_path / "automation_input")   # never the real marker
    monkeypatch.setattr(guard, "CAMERA", ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=30"])   # even a direct call
    monkeypatch.setattr(runner, "online", lambda timeout=3: True)
    from core import origin
    monkeypatch.setattr(origin, "FILE", tmp_path / "ticket_origin.json")


@pytest.fixture(autouse=True)
def isolated_app_state(tmp_path, monkeypatch):
    """Tests never write into the phone's real notification feed (ticket 245df526: test answers like "Moved it."
    reached Sanjar's phone as notifications for chats that don't exist)."""
    from core import appserver
    monkeypatch.setattr(appserver, "REPLIES", tmp_path / "app_replies.json")
