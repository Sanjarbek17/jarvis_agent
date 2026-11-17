"""Ticket e6e75d74: Sanjar hears when the Claude 5-hour window is close to its limit, used up, and reset."""
import asyncio
import time

import pytest

from core import usagewatch as u


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "FILE", tmp_path / "usage.json")


def info(util, reset, status="allowed"):
    return {"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.84,
            "unifiedWindows": {"five_hour": {"utilization": util, "resetsAt": reset, "status": status},
                               "seven_day": {"utilization": 0.84, "resetsAt": reset + 9e5, "status": "allowed_warning"}}}


def test_close_is_told_once_per_window():
    r = time.time() + 3600
    assert u.observe(info(0.5, r)) == []
    notes = u.observe(info(0.82, r))
    assert len(notes) == 1 and "82%" in notes[0] and "resets at" in notes[0]
    assert u.observe(info(0.9, r)) == []                         # same window: once
    assert len(u.observe(info(0.85, r + 18000))) == 1            # the next window: again


def test_used_up_then_reset_each_once():
    r = time.time() + 600
    hit = u.observe(info(1.0, r, "rejected"))
    assert len(hit) == 1 and "used up" in hit[0] and time.strftime("%H:%M", time.localtime(r)) in hit[0]
    assert u.observe(info(1.0, r, "rejected")) == [] and u.recently_hit()
    assert u.check_reset(now=r - 1) == []                          # still used up: no note before the reset time
    done = u.check_reset(now=r + 1)
    assert len(done) == 1 and "reset" in done[0] and u.check_reset(now=r + 60) == []
    # ticket 59f4e775: an "allowed" reading after a hit = it was reset early: the note comes now
    u.observe(info(1.0, r + 7, "rejected"))
    assert u.observe(info(0.95, r + 7)) == []                      # no "close" note after "used up"
    assert len(u.check_reset()) == 1


def test_old_style_event_without_windows():
    r = time.time() + 600
    notes = u.observe({"status": "rejected", "rateLimitType": "five_hour", "resetsAt": r, "utilization": 1.0})
    assert len(notes) == 1 and "used up" in notes[0]
    assert u.observe({"status": "allowed", "rateLimitType": "seven_day", "resetsAt": r}) == []


def test_notes_go_out_through_the_sender(monkeypatch):
    sent = []

    async def sender(t):
        sent.append(t)

    monkeypatch.setattr(u, "sender", sender)
    asyncio.run(u.report(info(0.81, time.time() + 600)))
    assert len(sent) == 1 and sent[0].startswith("⚠️")
    assert "81%" in u.status_line()
