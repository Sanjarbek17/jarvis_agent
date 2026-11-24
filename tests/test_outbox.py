import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

from core import outbox


def test_outbox_send_records_to(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "OUTBOX", tmp_path / "out")
    # To mock waiting loop without blocking
    monkeypatch.setattr(outbox.time, "sleep", lambda s: None)

    # When send is called, it writes the note with "to" field
    # We test with wait=0.01 so it times out after writing
    outbox.send(text="test message", to=715393503, wait=0.01)
    notes = list((tmp_path / "out").glob("*.json"))
    assert len(notes) == 1
    data = json.loads(notes[0].read_text())
    assert data["text"] == "test message"
    assert data["to"] == 715393503


def test_outbox_run_routes_to_custom_recipient(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "OUTBOX", tmp_path / "out")
    outbox.OUTBOX.mkdir(parents=True, exist_ok=True)

    sent_messages = []
    sent_files = []

    class MockTg:
        async def send_message(self, dest, text, link_preview=False):
            sent_messages.append((dest, text))
            return NS(id=101)

        async def send_file(self, dest, file_path, caption="", force_document=False):
            sent_files.append((dest, file_path, caption))
            return NS(id=102)

    # Note 1: default to owner_id
    (outbox.OUTBOX / "1.json").write_text(json.dumps({
        "text": "msg to owner", "to": 0
    }))
    # Note 2: custom int recipient (e.g. Ikrom)
    (outbox.OUTBOX / "2.json").write_text(json.dumps({
        "text": "msg to ikrom", "to": 715393503
    }))
    # Note 3: custom string recipient (e.g. @username)
    (outbox.OUTBOX / "3.json").write_text(json.dumps({
        "text": "msg to username", "to": "@username"
    }))
    # Note 4: file to custom recipient
    (outbox.OUTBOX / "4.json").write_text(json.dumps({
        "file": "/dummy/path.pdf", "caption": "file caption", "to": "715393503"
    }))

    async def run_test():
        tg = MockTg()
        task = asyncio.create_task(outbox.run(tg, owner_id=12345))
        await asyncio.sleep(0.1)
        task.cancel()

    asyncio.run(run_test())

    assert sent_messages == [
        (12345, "msg to owner"),
        (715393503, "msg to ikrom"),
        ("@username", "msg to username"),
    ]
    assert sent_files == [
        (715393503, "/dummy/path.pdf", "file caption"),
    ]
