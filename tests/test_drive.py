"""Ticket e6199929: Jarvis reaches Google Drive through a helper that has only the Drive connector."""
import asyncio
import json
from types import SimpleNamespace as NS

from core import drive, tools


def flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_helper_gets_drive_tools_only_and_never_trash():
    cmd = drive.command("read my lesson deck", "read the deck")
    assert flag(cmd, "--setting-sources") == "user"                       # the claude.ai connectors come with these
    assert flag(cmd, "--tools") == "" and flag(cmd, "--allowedTools") == "mcp__claude_ai_Google_Drive__*"
    assert json.loads(flag(cmd, "--settings")) == {"outputStyle": "default"}
    blocked = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--no-session-persistence")]
    assert "mcp__claude_ai_Google_Drive__trash_file" in blocked and "mcp__claude_ai_Google_Drive__share_file" in blocked
    assert "--strict-mcp-config" not in cmd


def test_sharing_only_when_asked():
    cmd = drive.command("share the deck with Ali", share=True)
    blocked = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--no-session-persistence")]
    assert blocked == ["mcp__claude_ai_Google_Drive__trash_file"]
    assert "Don't share" not in flag(cmd, "-p") and "Don't share" in flag(drive.command("x"), "-p")


def fake_proc(monkeypatch, out: bytes, hang: bool = False):
    class Proc:
        returncode = 0

        async def communicate(self):
            if hang:
                await asyncio.sleep(10)
            return out, b""

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def exec_(*a, **k):
        return Proc()

    monkeypatch.setattr(drive.asyncio, "create_subprocess_exec", exec_)


def test_answer_errors_and_timeouts_come_back_as_text(monkeypatch):
    fake_proc(monkeypatch, json.dumps({"result": "Found it: deck X", "is_error": False}).encode())
    assert asyncio.run(drive.run("x")) == "Found it: deck X"
    fake_proc(monkeypatch, json.dumps({"result": "limit", "is_error": True}).encode())
    assert asyncio.run(drive.run("x")).startswith("Google Drive failed")
    fake_proc(monkeypatch, b"")
    assert "didn't answer" in asyncio.run(drive.run("x"))
    fake_proc(monkeypatch, b"", hang=True)
    assert "was stopped" in asyncio.run(drive.run("x", timeout=0.1))


def test_tool_is_there_for_chat_and_calls_not_guests():
    def names(source):
        return {t.name for t in asyncio.run(tools.make_server(source).list_tools())}
    assert "google_drive" in names("Telegram") and "google_drive" in names("Call")
    assert "google_drive" not in names("Guest") and "google_drive" not in names("Group")
