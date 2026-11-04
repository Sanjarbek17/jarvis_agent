import pytest

from core import runner


@pytest.fixture(autouse=True)
def isolated_runner_state(tmp_path, monkeypatch):
    """Tests never see Jarvis's real state files (a real requeue list made the runner call Notion)."""
    monkeypatch.setattr(runner, "REQUEUE_AFTER", tmp_path / "requeue_after.json")
    monkeypatch.setattr(runner, "RESTART_PENDING", tmp_path / "restart_pending")
    monkeypatch.setattr(runner, "HEARTBEAT", tmp_path / "runner_heartbeat")
