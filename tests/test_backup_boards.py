"""The nightly board backup: nothing secret leaves the Mac, and a backup can be restored."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from core import localboard

spec = importlib.util.spec_from_file_location("backup_boards", Path(__file__).parent.parent / "scripts" / "backup_boards.py")
backup_boards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_boards)


@pytest.fixture(autouse=True)
def fake_env_files(tmp_path, monkeypatch):
    root = tmp_path / "dev"
    (root / "app" / "node_modules").mkdir(parents=True)
    (root / "app" / ".env").write_text("TG_2FA_PASSWORD=Plain.Looking1\nPOSTGRES_PASSWORD=postgres\nHOST=somehost.example\n")
    (root / "app" / ".env.example").write_text("API_KEY=example-value\n")
    (root / "app" / "node_modules" / ".env").write_text("API_KEY=from-a-package\n")
    monkeypatch.setattr(backup_boards, "ENV_ROOT", root)
    backup_boards.secret_values.cache_clear()
    yield root
    backup_boards.secret_values.cache_clear()


def test_real_values_from_env_files_are_hidden_wherever_they_appear():
    assert backup_boards.mask("the code is Plain.Looking1 ok") == "the code is [hidden] ok"


def test_only_real_secrets_count():
    assert backup_boards.secret_values() == ("Plain.Looking1",)
    text = "postgres on somehost.example, example-value, from-a-package"
    assert backup_boards.mask(text) == text


def test_nothing_goes_out_when_no_secrets_can_be_found(fake_env_files, tmp_path, monkeypatch):
    monkeypatch.setattr(backup_boards, "ENV_ROOT", tmp_path / "nowhere")
    backup_boards.secret_values.cache_clear()
    with pytest.raises(RuntimeError):
        backup_boards.mask("anything")


@pytest.mark.parametrize("text", [
    "ghp_" + "a" * 36, "sk-ant-" + "b" * 30, "123456789:" + "c" * 35, "AKIA" + "D" * 16,
    "password: hunter22", "parol=hunter22", "api key: abc123", "postgres://me:hunter22@db/x",
    "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"])
def test_secrets_are_hidden(text):
    out = backup_boards.mask(f"before {text} after")
    assert "[hidden]" in out
    assert not any(bit in out for bit in ("hunter22", "abc123", "aaaa", "bbbb", "cccc", "DDDD"))


def test_ordinary_words_stay():
    text = "All tests pass: 130. The password screen is done, Telegram asks for it."
    assert backup_boards.mask(text) == text


def test_export_and_restore_keep_everything(tmp_path):
    with localboard._db():
        pass
    first = localboard.add_ticket("Yo'lchi", "First", "the text\npassword: hunter22")
    second = localboard.add_ticket("Theater", "Second", "more")
    localboard.append_log(first.page_id, ["a line"])

    folder = tmp_path / "boards"
    assert backup_boards.export(localboard.DB, folder) == 2
    assert sorted(f.name for f in folder.glob("*.json")) == ["theater.json", "yo-lchi.json"]
    assert "hunter22" not in (folder / "yo-lchi.json").read_text()

    new = tmp_path / "restored.db"
    assert backup_boards.restore(folder, new) == 2
    con = sqlite3.connect(new)
    assert con.execute("SELECT count(*) FROM log").fetchone()[0] == 1
    assert con.execute("SELECT title FROM tickets WHERE ref=?", (second.ref,)).fetchone()[0] == "Second"
    con.close()
    with pytest.raises(SystemExit):
        backup_boards.restore(folder, new)


def test_a_dropped_projects_file_goes(tmp_path):
    folder = tmp_path / "boards"
    folder.mkdir()
    (folder / "gone.json").write_text("[]")
    with localboard._db():
        pass
    localboard.add_ticket("Theater", "Second", "more")
    backup_boards.export(localboard.DB, folder)
    assert not (folder / "gone.json").exists()


def test_an_empty_board_file_is_never_exported(tmp_path):
    with localboard._db():
        pass
    folder = tmp_path / "boards"
    folder.mkdir()
    (folder / "theater.json").write_text("[]")
    with pytest.raises(RuntimeError):
        backup_boards.export(localboard.DB, folder)
    assert (folder / "theater.json").exists()
