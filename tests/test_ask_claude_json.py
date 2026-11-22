import asyncio
import json
import pytest
from core import memory


class FakeProc:
    def __init__(self, stdout_data):
        self._out = stdout_data

    async def communicate(self):
        return self._out, b""

    def kill(self):
        pass


def test_ask_claude_json_retries_on_empty_conversation_id(monkeypatch):
    calls = 0

    async def fake_subprocess_exec(*cmd, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            # First attempt: engine fails with empty conversation id
            payload = {
                "status": "ERROR",
                "is_error": True,
                "error": "failed to start conversation: backend returned an empty conversation id",
            }
            return FakeProc(json.dumps(payload).encode())
        else:
            # Second attempt: succeeds
            payload = {
                "status": "SUCCESS",
                "is_error": False,
                "response": '{"thread": "t1", "label": "test topic"}',
            }
            return FakeProc(json.dumps(payload).encode())

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0.0001))

    res = asyncio.run(memory._ask_claude_json("hello"))
    assert res == {"thread": "t1", "label": "test topic"}
    assert calls == 2


def test_ask_claude_json_raises_after_max_retries(monkeypatch):
    calls = 0

    async def fake_subprocess_exec(*cmd, **kwargs):
        nonlocal calls
        calls += 1
        payload = {
            "status": "ERROR",
            "is_error": True,
            "error": "failed to start conversation: backend returned an empty conversation id",
        }
        return FakeProc(json.dumps(payload).encode())

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0.0001))

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(memory._ask_claude_json("hello"))
    assert "empty conversation id" in str(exc_info.value)
    assert calls == 3
