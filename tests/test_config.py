"""Tests for config loading, fallbacks, and private config git-ignoring (ticket 403a55ea)."""
import subprocess
from pathlib import Path

from core import config


def test_reload_projects_loads_projects():
    assert config.reload_projects() is not None
    assert isinstance(config.PROJECTS, dict)
    assert len(config.PROJECTS) > 0


def test_reload_projects_falls_back_to_example(tmp_path, monkeypatch):
    example = tmp_path / "projects.json.example"
    example.write_text('{"Fallback Project": {"about": "test", "folders": ["test"], "local": true}}')
    missing = tmp_path / "projects.json"

    monkeypatch.setattr(config, "PROJECTS_FILE", missing)
    monkeypatch.setattr(config, "PROJECTS_EXAMPLE", example)
    monkeypatch.setattr(config, "_projects", {"mtime": 0.0, "version": 0})

    assert config.reload_projects() is True
    assert "Fallback Project" in config.PROJECTS


def test_rules_text_falls_back_to_example(tmp_path, monkeypatch):
    example = tmp_path / "rules.md.example"
    example.write_text("Fallback standing rules")
    missing = tmp_path / "rules.md"

    monkeypatch.setattr(config, "RULES_FILE", missing)
    monkeypatch.setattr(config, "RULES_EXAMPLE", example)

    assert config.rules_text() == "Fallback standing rules"


def test_private_configs_not_tracked_by_git():
    out = subprocess.check_output(["git", "ls-files", "config/projects.json", "config/rules.md"], text=True)
    assert out.strip() == "", "Private configs must not be tracked in git index"


def test_gitignore_contains_private_configs():
    gitignore_text = (config.HERE / ".gitignore").read_text()
    assert "config/projects.json" in gitignore_text
    assert "config/rules.md" in gitignore_text
    assert ".env" in gitignore_text
