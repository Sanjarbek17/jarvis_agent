import asyncio
import json
import time
from pathlib import Path
import pytest
from starlette.testclient import TestClient

from core import answering, appserver
from core.brain import Brain, _step_text


def test_step_text_formatting():
    # Antigravity tool_call format
    assert _step_text({"step_type": "tool_call", "tool_name": "read_file", "tool_args": {"Path": "/foo/bar/Api.kt"}}) == "reading Api.kt"
    assert _step_text({"step_type": "tool_call", "tool_name": "run_command", "tool_args": {"CommandLine": "git status"}}) == "running: git status"
    assert _step_text({"step_type": "tool_call", "tool_name": "grep_search", "tool_args": {}}) == "searching the code"
    # Claude tool_use format
    assert _step_text({"type": "tool_use", "name": "Edit", "input": {"file_path": "/a/b/ChatActivity.kt"}}) == "editing ChatActivity.kt"
    assert _step_text({"type": "tool_use", "name": "TodoWrite", "input": {}}) == "planning the steps"


def test_answering_clear_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(answering, "DIR", tmp_path / "answering")
    answering.clear()
    with answering.mark("chat-1"):
        with answering.mark("chat-2"):
            assert answering.is_answering("chat-1")
            assert answering.is_answering("chat-2")
            answering.clear_chat("chat-1")
            assert not answering.is_answering("chat-1")
            assert answering.is_answering("chat-2")
    answering.clear()


def test_chat_thinking_and_stop_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "token", lambda: "secret")
    monkeypatch.setattr(appserver, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(answering, "DIR", tmp_path / "answering")
    appserver.CHATS.mkdir(parents=True, exist_ok=True)
    answering.clear()

    chat = {"id": "test-chat-1", "title": "Test", "project": "General", "messages": [], "updated": time.time()}
    appserver.save_chat(chat)

    client = TestClient(appserver.app)

    # Unauthorized
    resp = client.get("/api/chats/test-chat-1/thinking")
    assert resp.status_code == 401

    resp = client.post("/api/chats/test-chat-1/stop")
    assert resp.status_code == 401

    headers = {"Authorization": "Bearer secret"}

    # Initially not answering
    resp = client.get("/api/chats/test-chat-1/thinking", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["answering"] is False
    assert data["steps"] == []

    # Simulate active thinking
    appserver._thinking["test-chat-1"] = {
        "started": time.time() - 2.0,
        "step": "reading Api.kt",
        "steps": ["searching the code", "reading Api.kt"],
    }
    with answering.mark("test-chat-1"):
        # GET thinking
        resp = client.get("/api/chats/test-chat-1/thinking", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["answering"] is True
        assert data["step"] == "reading Api.kt"
        assert data["steps"] == ["searching the code", "reading Api.kt"]
        assert data["elapsed"] >= 2.0

        # GET messages headers check
        resp = client.get("/api/chats/test-chat-1/messages", headers=headers)
        assert resp.headers.get("X-Jarvis-Answering") == "true"
        assert resp.headers.get("X-Jarvis-Step") == "reading Api.kt"

        # Mock brain in _brains
        mock_brain = Brain("chat")
        mock_brain.was_stopped = False
        appserver._brains["test-chat-1"] = (mock_brain, time.monotonic())

        # POST stop
        resp = client.post("/api/chats/test-chat-1/stop", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["stopped"] is True
        assert mock_brain.was_stopped is True
        assert "test-chat-1" not in appserver._thinking
        assert not answering.is_answering("test-chat-1")

    # Clean up
    appserver._brains.pop("test-chat-1", None)
    appserver._thinking.pop("test-chat-1", None)
    answering.clear()
