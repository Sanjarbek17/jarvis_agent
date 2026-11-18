"""Ticket 59c7e073: shortcuts let go of their modifiers, and clicks never carry a stuck one."""
import pytest

Q = pytest.importorskip("Quartz")
import sys
sys.path.insert(0, "scripts")
import mac  # noqa: E402

CMD, SHIFT, CTRL = Q.kCGEventFlagMaskCommand, Q.kCGEventFlagMaskShift, Q.kCGEventFlagMaskControl


def test_cmd_tab_presses_and_releases_cmd():
    assert mac.combo_events(["cmd"], mac.KEYS["tab"]) == [(55, True, CMD), (48, True, CMD), (48, False, CMD), (55, False, 0)]


def test_lock_combo_releases_in_reverse_and_ends_with_nothing_held():
    ev = mac.combo_events(["ctrl", "cmd"], mac.KEYS["q"])
    assert ev[:2] == [(59, True, CTRL), (55, True, CTRL | CMD)]
    assert ev[2:4] == [(12, True, CTRL | CMD), (12, False, CTRL | CMD)]
    assert ev[4:] == [(55, False, CTRL), (59, False, 0)]


def test_plain_key_has_no_modifier_events():
    assert mac.combo_events([], mac.KEYS["return"]) == [(36, True, 0), (36, False, 0)]


def test_posted_events_end_clean_and_clicks_carry_nothing(monkeypatch):
    posted = []
    monkeypatch.setattr(mac, "_post", lambda e: posted.append((Q.CGEventGetType(e), Q.CGEventGetFlags(e) & (CMD | SHIFT | CTRL))))
    mac.key("cmd+shift+t")
    assert posted[-1][1] == 0                                # the last event of a shortcut holds nothing
    posted.clear()
    mac.click(10, 10)
    assert posted and all(flags == 0 for _, flags in posted)   # a click is a plain click, whatever is held


def test_unknown_modifier_refused():
    with pytest.raises(SystemExit):
        mac.key("hyper+t")
