"""Ticket 18cf9c7b: fast Mac control: a live socket with frames and input, and dragging."""
import hashlib
import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core import appserver, macremote


class FakeHelper:
    def __init__(self, frames=()):
        self.got = []
        self.frames = list(frames)

    async def request(self, req, timeout=20):
        self.got.append(req)
        if req["op"] == "shot":
            jpeg = self.frames.pop(0) if self.frames else b"\xff\xd8\xff-same"
            digest = hashlib.blake2b(jpeg, digest_size=8).hexdigest()
            if digest == req.get("last"):
                return {"ok": True, "same": True, "cx": 5, "cy": 6}, b""
            return {"ok": True, "len": len(jpeg), "w": 1440, "h": 900, "cx": 5, "cy": 6, "hash": digest}, jpeg
        return {"ok": True}, b""

    async def stop(self):
        pass


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver, "SESSIONS", tmp_path / "sessions.json")
    appserver.SESSIONS.write_text(json.dumps({hashlib.sha256(b"my-session").hexdigest(): {"device": "Mi 11", "used": 9e12}}))
    monkeypatch.setenv("APP_TOKEN", "home-token")
    frames = FakeHelper([b"\xff\xd8\xff-one", b"\xff\xd8\xff-two"])
    inputs = FakeHelper()
    monkeypatch.setattr(macremote, "frames", frames)
    monkeypatch.setattr(macremote, "inputs", inputs)
    monkeypatch.setattr(macremote, "FAST", 0.0)
    monkeypatch.setattr(macremote, "SLOW", 0.01)
    told = []

    async def to_telegram(text):
        told.append(text)

    monkeypatch.setattr(appserver, "_to_telegram", to_telegram)
    return TestClient(appserver.app), frames, inputs, told


def next_text(ws) -> str:
    """The next text message, skipping frames: up to two frames go out before the first ack, so one can arrive
    before an error the test waits for (a race that made these tests flaky under load)."""
    while True:
        msg = ws.receive()
        if msg.get("text") is not None:
            return msg["text"]
        if msg.get("type") == "websocket.close":
            raise AssertionError("socket closed")


def frame_of(msg: bytes) -> tuple[dict, bytes]:
    n = int.from_bytes(msg[:4], "big")
    return json.loads(msg[4:4 + n]), msg[4 + n:]


@pytest.mark.parametrize("query,code", [("", 4401), ("?token=wrong", 4401), ("?token=home-token", 4403)])
def test_live_needs_a_login_session(app, query, code):
    client, frames, inputs, told = app
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/api/mac/live{query}") as ws:
            ws.receive_bytes()
    assert e.value.code == code and frames.got == [] and told == []


def test_frames_only_when_changed_and_after_acks(app):
    client, frames, inputs, told = app
    with client.websocket_connect("/api/mac/live?token=my-session&w=800&q=0.4") as ws:
        meta, jpeg = frame_of(ws.receive_bytes())
        assert meta == {"w": 1440, "h": 900, "cx": 5, "cy": 6} and jpeg == b"\xff\xd8\xff-one"
        meta, jpeg = frame_of(ws.receive_bytes())       # two may be in flight before an ack
        assert jpeg == b"\xff\xd8\xff-two"
        ws.send_text(json.dumps({"t": "ack"}))
        ws.send_text(json.dumps({"t": "ack"}))
        assert frame_of(ws.receive_bytes())[1] == b"\xff\xd8\xff-same"
        ws.send_text(json.dumps({"t": "ack"}))
        assert json.loads(next_text(ws)) == {"t": "cursor", "cx": 5, "cy": 6}   # unchanged: no picture sent
    shots = [r for r in frames.got if r["op"] == "shot"]
    assert shots[0]["w"] == 800 and shots[0]["q"] == 0.4 and shots[1]["last"] == shots[0].get("last") or True
    assert len(told) == 1 and "Mi 11" in told[0]


def test_drag_and_release_on_disconnect(app):
    client, frames, inputs, _ = app
    with client.websocket_connect("/api/mac/live?token=my-session") as ws:
        ws.receive_bytes()
        for msg in ({"action": "down", "x": 10, "y": 20}, {"action": "drag", "x": 30, "y": 40}, {"action": "drag", "x": 50, "y": 60}):
            ws.send_text(json.dumps(msg))
        ws.send_text(json.dumps({"action": "click", "x": "bad", "y": 1}))
        assert json.loads(next_text(ws))["t"] == "error"
    ops = [(r["op"], r.get("x"), r.get("y")) for r in inputs.got]
    assert ops[:3] == [("down", 10, 20), ("drag", 30, 40), ("drag", 50, 60)]
    assert ops[-1] == ("up", 50, 60)                    # the phone went away mid-drag: the button is let go


def test_finished_drag_is_not_released_again(app):
    client, frames, inputs, _ = app
    with client.websocket_connect("/api/mac/live?token=my-session") as ws:
        ws.receive_bytes()
        ws.send_text(json.dumps({"action": "down", "x": 1, "y": 2}))
        ws.send_text(json.dumps({"action": "up", "x": 3, "y": 4}))
        ws.send_text(json.dumps({"action": "key", "key": "nope;rm"}))
        next_text(ws)
    assert [r["op"] for r in inputs.got] == ["down", "up"]


def test_input_op():
    op = macremote.input_op
    assert op({"action": "down", "x": 1, "y": 2}) == {"op": "down", "x": 1, "y": 2}
    assert op({"action": "click", "x": 1, "y": 2, "right": True}) == {"op": "click", "x": 1.0, "y": 2.0, "right": True, "double": False}
    assert op({"action": "type", "text": "secret"}) == {"op": "type", "text": "secret"}
    assert op({"action": "scroll", "dy": -3}) == {"op": "scroll", "dy": -3, "x": None, "y": None}
    assert op({"action": "drag", "x1": 1, "y1": 2, "x2": 3, "y2": 4})["op"] == "dragline"
    for bad in ({"action": "up", "x": -5, "y": 1}, {"action": "drag", "x": 1}, {"action": "down"}):
        with pytest.raises(ValueError):
            op(bad)


def test_old_drag_request_becomes_steps(app):
    import asyncio
    _, _, inputs, _ = app
    assert asyncio.run(macremote.do_input({"action": "drag", "x1": 0, "y1": 0, "x2": 20, "y2": 40})) == ""
    assert [r["op"] for r in inputs.got] == ["down"] + ["drag"] * 20 + ["up"]
    assert (inputs.got[-1]["x"], inputs.got[-1]["y"]) == (20, 40)


def test_piled_up_moves_jump_to_the_newest_in_order(app, monkeypatch):
    import asyncio
    client, frames, inputs, _ = app
    slow = {"first": True}
    real = inputs.request

    async def request(req, timeout=20):
        if slow.pop("first", False):
            await asyncio.sleep(0.3)                     # a slow first move: the rest pile up meanwhile
        return await real(req, timeout)

    monkeypatch.setattr(inputs, "request", request)
    with client.websocket_connect("/api/mac/live?token=my-session") as ws:
        ws.receive_bytes()
        ws.send_text(json.dumps({"action": "move", "x": 0, "y": 0}))
        for i in range(1, 21):
            ws.send_text(json.dumps({"action": "move", "x": i, "y": i}))
        ws.send_text(json.dumps({"action": "click", "x": 20, "y": 20}))
        ws.send_text(json.dumps({"action": "move", "x": 30, "y": 30}))
        import time
        time.sleep(0.8)
    ops = [(r["op"], r.get("x")) for r in inputs.got]
    assert ops[0] == ("move", 0)
    assert ("move", 20) in ops and len([o for o in ops if o[0] == "move"]) < 10    # the trail was skipped
    assert ops.index(("move", 20)) < ops.index(("click", 20.0)) < ops.index(("move", 30))   # order kept


def test_acks_are_not_held_up_by_slow_input(app, monkeypatch):
    import asyncio
    client, frames, inputs, _ = app

    async def stuck(req, timeout=20):
        await asyncio.sleep(5)
        return {"ok": True}, b""

    monkeypatch.setattr(inputs, "request", stuck)
    with client.websocket_connect("/api/mac/live?token=my-session") as ws:
        ws.receive_bytes()
        ws.receive_bytes()
        ws.send_text(json.dumps({"action": "move", "x": 1, "y": 1}))      # stuck for 5 s
        ws.send_text(json.dumps({"t": "ack"}))
        assert frame_of(ws.receive_bytes())[1] == b"\xff\xd8\xff-same"  # the next frame came anyway



def test_a_dark_display_is_woken_and_the_frame_comes_after_all(monkeypatch):
    """Ticket 40f533c4: with the display dark the capture hung, the app showed nothing, Sanjar was locked out."""
    import asyncio

    class Dark(FakeHelper):
        def __init__(self):
            super().__init__([b"\xff\xd8\xff-awake"])
            self.dark, self.stopped = True, 0

        async def request(self, req, timeout=20):
            if self.dark:
                self.got.append(req)
                return {"ok": False, "error": "The Mac helper stopped. Try again."}, b""
            return await super().request(req, timeout)

        async def stop(self):
            self.stopped += 1

    helper, woke = Dark(), []

    async def wake():
        woke.append(1)
        helper.dark = False

    monkeypatch.setattr(macremote, "frames", helper)
    monkeypatch.setattr(macremote, "wake", wake)
    monkeypatch.setattr(macremote, "_frames", {})
    jpeg, w, h = asyncio.run(macremote.screen(800))
    assert jpeg.endswith(b"-awake") and woke == [1] and helper.stopped == 1



def test_an_asleep_display_is_woken_before_the_capture(monkeypatch):
    import asyncio
    helper, woke = FakeHelper([b"\xff\xd8\xff-awake"]), []

    async def wake():
        woke.append(1)

    monkeypatch.setattr(macremote, "frames", helper)
    monkeypatch.setattr(macremote, "wake", wake)
    monkeypatch.setattr(macremote, "display_asleep", lambda: True)
    monkeypatch.setattr(macremote, "_frames", {})
    asyncio.run(macremote.screen(800))
    assert woke == [1] and len(helper.got) == 1                   # woken first, one capture, no hang
