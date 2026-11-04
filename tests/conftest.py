import pytest

from core import runner


@pytest.fixture(autouse=True)
def isolated_runner_state(tmp_path, monkeypatch):
    """Tests never see Jarvis's real state files (a real requeue list made the runner call Notion)."""
    monkeypatch.setattr(runner, "REQUEUE_AFTER", tmp_path / "requeue_after.json")
    monkeypatch.setattr(runner, "RESTART_PENDING", tmp_path / "restart_pending")
    monkeypatch.setattr(runner, "HEARTBEAT", tmp_path / "runner_heartbeat")
    monkeypatch.setattr(runner, "PENDING", tmp_path / "pending_results.json")
    monkeypatch.setattr(runner, "REQUEUED", tmp_path / "requeued_after_restart.json")
    monkeypatch.setattr(runner, "WRITE_TRIES", (0, 0, 0))
    monkeypatch.setattr(runner, "FROM_SANJAR", tmp_path / "from_sanjar.json")
    monkeypatch.setattr(runner, "USAGE_LIMIT", tmp_path / "usage_limit.json")
