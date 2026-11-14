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
    path, why = asyncio.run(guard.capture_photo())
    assert why == "" and path.read_bytes()[:2] == b"\xff\xd8"


def test_take_photo_times_out_when_macos_waits_for_permission(tmp_path, monkeypatch):
    hang = tmp_path / "ffmpeg"
    hang.write_text("#!/bin/sh\nexec sleep 30\n")
    hang.chmod(hang.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(guard, "SHOTS", tmp_path)
    monkeypatch.setattr(guard, "FFMPEG", str(hang))
    monkeypatch.setattr(guard, "PHOTO_TIMEOUT", 0.2)
    assert asyncio.run(guard.capture_photo()) == (None, guard.NO_CAMERA)


def test_real_idle_reading():
    assert guard.idle_seconds() >= 0


# --- ticket 4946d461: lock the Mac (silently since ticket 62fce184) -------------------------------------------------

def test_a_touch_locks_the_mac_after_the_screenshot_silently(g, monkeypatch, tmp_path):
    order = []

    async def shot():
        order.append("screenshot")
        return tmp_path / "screen.png"

    g.screenshot = shot
    g.lock = lambda: order.append("lock") or True
    g.arm(grace=0)
    time.sleep(0.01)
    touched(monkeypatch, 0)
    assert asyncio.run(g.check())
    assert order == ["screenshot", "lock"]                         # the screenshot still shows what they opened
    assert "Locking it" in g.texts[0] and not any("couldn't lock" in t for t in g.texts)


def test_he_is_told_when_the_lock_fails(g, monkeypatch):
    g.lock = lambda: False
    g.arm(grace=0)
    time.sleep(0.01)
    touched(monkeypatch, 0)
    asyncio.run(g.check())
    assert any("couldn't lock" in t for t in g.texts)


def test_the_real_lock_call_exists_on_this_mac():
    import ctypes
    assert hasattr(ctypes.CDLL(guard.LOGIN_FRAMEWORK), "SACLockScreenImmediate")


# --- ticket 70e8ad2f: Sanjar driving the Mac from the app isn't a touch -----------------------------------------

def test_sanjars_remote_clicks_dont_lock_the_mac(tmp_path, monkeypatch):
    from core import macremote
    monkeypatch.setattr(guard, "FILE", tmp_path / "guard.json")
    monkeypatch.setattr(macremote, "_last_remote", {"at": 0.0})

    class Inputs:
        async def request(self, step, timeout=20):
            return {"ok": True}, b""

    monkeypatch.setattr(macremote, "inputs", Inputs())
    alerts = []

    async def text(t):
        alerts.append(t)

    async def nothing(*a):
        return tmp_path / "x.png"

    g = guard.Guard(text, nothing, nothing, busy=macremote.controlling)    # as jarvis.py wires it
    g.arm(grace=0)
    time.sleep(0.01)
    asyncio.run(macremote.do_input({"action": "click", "x": 10, "y": 10}))
    touched(monkeypatch, 0)                                   # macOS sees the click as input
    assert macremote.controlling() and not asyncio.run(g.check()) and alerts == []
    # he stops; long after, someone touches it for real: that still locks
    macremote._last_remote["at"] = time.time() - macremote.REMOTE_GRACE - 1
    time.sleep(guard.POLL + 0.1)
    touched(monkeypatch, 0)
    assert not macremote.controlling() and asyncio.run(g.check()) and alerts


def test_waking_the_screen_from_the_app_counts_as_him(monkeypatch):
    from core import macremote
    monkeypatch.setattr(macremote, "_last_remote", {"at": 0.0})

    async def run(*cmd, **kw):
        return 0, ""

    monkeypatch.setattr(macremote, "_run", run)
    asyncio.run(macremote.wake())
    assert macremote.controlling()



def test_no_test_can_reach_the_real_camera():
    """Ticket 04cf4647: a guard test armed the guard and took two real camera photos on every test run."""
    assert asyncio.run(guard.take_photo()) == (None, "camera blocked in tests")
    assert "avfoundation" not in guard.CAMERA



# --- ticket 733814dd: Jarvis's own clicks (the reel poster through scripts/mac.py) aren't a touch ------------------

def test_input_right_after_jarvis_automation_doesnt_lock(g, monkeypatch):
    locks = []
    g.lock = lambda: locks.append(1) or True
    g.arm(grace=0)
    time.sleep(0.01)
    guard.AUTOMATION.write_text("now")                     # scripts/mac.py just clicked
    touched(monkeypatch, 0)
    assert not asyncio.run(g.check()) and locks == [] and g.texts == []
    # long after the automation, a real touch still locks
    import os
    old = time.time() - guard.AUTOMATION_GRACE - 5
    os.utime(guard.AUTOMATION, (old, old))
    time.sleep(guard.POLL + 0.1)
    touched(monkeypatch, 0)
    assert asyncio.run(g.check()) and locks == [1]


def test_mac_tool_marks_every_event(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("mac_tool", Path(__file__).resolve().parent.parent / "scripts" / "mac.py")
    mac = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mac)
    posted = []
    monkeypatch.setattr(mac, "AUTOMATION", tmp_path / "automation_input")
    monkeypatch.setattr(mac.Q, "CGEventPost", lambda tap, e: posted.append(e))
    mac._marked[0] = 0.0
    mac._post("event")
    assert posted == ["event"] and (tmp_path / "automation_input").exists()
    assert "CGEventPost(Q.kCGHIDEventTap, event)" in (Path(__file__).resolve().parent.parent / "scripts" / "mac.py").read_text()



def test_the_guard_makes_no_sound():
    """Ticket 62fce184: "Remove alert sound, we don't need it"."""
    src = open(guard.__file__).read()
    assert "afplay" not in src and "set volume" not in src and not hasattr(guard, "play_warning")


# --- ticket 52d168c3: decided by when the input happened, not by whether the guard looked in time -----------------

def test_his_app_input_is_his_even_when_the_guard_looks_late(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "FILE", tmp_path / "guard.json")
    texts, locks = [], []

    async def text(t):
        texts.append(t)

    async def nothing(*a):
        return tmp_path / "x.png"

    clicked = {"at": 0.0}
    g = guard.Guard(text, nothing, nothing, ours_at=lambda: clicked["at"])
    g.lock = lambda: locks.append(1) or True
    g.arm(grace=0)
    time.sleep(0.01)
    clicked["at"] = time.time()                         # he clicks from the app; the guard doesn't look (busy Jarvis)
    time.sleep(0.05)
    touched(monkeypatch, time.time() - clicked["at"])   # it looks later: the last input is that click
    assert not asyncio.run(g.check()) and locks == [] and texts == []
    touched(monkeypatch, 0)                             # someone touches it now, long after his click
    monkeypatch.setattr(guard, "OURS_SLACK", 0)         # (no slack, so "now" counts as after his click)
    time.sleep(0.01)
    assert asyncio.run(g.check()) and locks == [1]
