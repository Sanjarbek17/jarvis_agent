"""Ticket 3b226456: the guard alerts when the Mac is touched, only while Sanjar has armed it."""
import asyncio
import stat
import time

import pytest

from core import guard


@pytest.fixture
def g(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "FILE", tmp_path / "guard.json")
    monkeypatch.setattr(guard, "SHOTS", tmp_path)
    monkeypatch.setattr(guard, "SECOND_PHOTO_AFTER", 0)
    monkeypatch.setattr(guard, "SEND_TRIES", (0, 0))
    texts, photos = [], []

    async def text(t):
        texts.append(t)

    async def pics(paths, caption):
        photos.append((list(paths), caption))

    async def shot():
        return tmp_path / "screen.png"

    async def camera():
        return tmp_path / "cam.jpg", ""

    monkeypatch.setattr(guard, "take_photo", camera)
    x = guard.Guard(text, pics, shot)
    x.texts, x.photos = texts, photos
    return x


def touched(monkeypatch, seconds_ago):
    monkeypatch.setattr(guard, "idle_seconds", lambda: seconds_ago)


def test_nothing_happens_unless_armed(g, monkeypatch):
    touched(monkeypatch, 0)
    assert not asyncio.run(g.check()) and g.texts == []


def test_touch_while_armed_sends_alert_and_photos(g, monkeypatch):
    g.arm(grace=0)
    touched(monkeypatch, 5000)                                  # nobody near it
    assert not asyncio.run(g.check())
    time.sleep(0.01)
    touched(monkeypatch, 0)
    assert asyncio.run(g.check())
    assert "Someone touched the Mac" in g.texts[0]
    names = [[p.name for p in ps] for ps, _ in g.photos]
    assert names == [["cam.jpg", "screen.png"], ["cam.jpg"]]    # camera first, then one a few seconds later


def test_no_alert_during_the_grace_period_or_after_disarm(g, monkeypatch):
    touched(monkeypatch, 0)
    g.arm(grace=60)
    assert not asyncio.run(g.check())                           # he's still walking away
    g.arm(grace=0)
    assert g.disarm() == "Guard is off." and g.status() == "Guard is off."
    assert not asyncio.run(g.check()) and g.disarm() == "Guard wasn't on."


def test_no_alert_for_touches_before_it_started_watching(g, monkeypatch):
    g.arm(grace=0)
    time.sleep(0.01)
    touched(monkeypatch, 3600)                                  # the last input was an hour ago
    assert not asyncio.run(g.check())


def test_repeats_are_limited_and_it_stays_armed(g, monkeypatch):
    g.arm(grace=0)
    time.sleep(0.01)
    touched(monkeypatch, 0)
    assert asyncio.run(g.check())
    assert not asyncio.run(g.check())                           # right after: no second alert
    s = g.state()
    s["last_alert"] -= guard.REPEAT_AFTER + 1
    g._save(s)
    assert asyncio.run(g.check()) and "last alert" in g.status()


def test_the_mac_workers_own_clicks_dont_count(g, monkeypatch):
    g.arm(grace=0)
    time.sleep(0.01)
    touched(monkeypatch, 0)
    g.busy = lambda: True
    assert not asyncio.run(g.check()) and g.texts == []
    g.busy = lambda: False
    assert not asyncio.run(g.check())                           # its last click isn't counted just after either


def test_alert_goes_out_even_without_camera_or_screenshot(g, monkeypatch):
    async def no_camera():
        return None, guard.NO_CAMERA

    async def no_shot():
        raise RuntimeError("display asleep")

    monkeypatch.setattr(guard, "take_photo", no_camera)
    g.screenshot = no_shot
    asyncio.run(g.alert(time.time()))
    assert "Someone touched" in g.texts[0] and "No camera photo" in g.texts[1] and g.photos == []


def test_sending_is_retried(g):
    tries = []

    async def flaky(t):
        tries.append(t)
        if len(tries) == 1:
            raise OSError("network not back yet")

    g.send_text = flaky
    asyncio.run(g._deliver(g.send_text, "x"))
    assert len(tries) == 2


def test_take_photo_real_ffmpeg_gives_a_jpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "SHOTS", tmp_path)
    monkeypatch.setattr(guard, "CAMERA", ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=30"])
    path, why = asyncio.run(guard.take_photo())
    assert why == "" and path.read_bytes()[:2] == b"\xff\xd8"


def test_take_photo_times_out_when_macos_waits_for_permission(tmp_path, monkeypatch):
    hang = tmp_path / "ffmpeg"
    hang.write_text("#!/bin/sh\nexec sleep 30\n")
    hang.chmod(hang.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(guard, "SHOTS", tmp_path)
    monkeypatch.setattr(guard, "FFMPEG", str(hang))
    monkeypatch.setattr(guard, "PHOTO_TIMEOUT", 0.2)
    assert asyncio.run(guard.take_photo()) == (None, guard.NO_CAMERA)


def test_real_idle_reading():
    assert guard.idle_seconds() >= 0
