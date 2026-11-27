"""Ticket 0965df43: Track and show Gemini limit status on app."""
import json
import time

import pytest

from core import runner, usagewatch


SAMPLE_QUOTA_RAW = {
    "response": {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-weekly",
                        "remainingFraction": 0.85,
                        "resetTime": "2026-11-27T06:38:58Z",
                    },
                    {
                        "bucketId": "gemini-5h",
                        "remainingFraction": 0.52,
                        "resetTime": "2026-11-20T11:38:58Z",
                    },
                ],
            }
        ]
    }
}


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(usagewatch, "FILE", tmp_path / "usage.json")
    monkeypatch.setattr(usagewatch, "DAEMON_DIRS", [tmp_path / "daemon"])
    monkeypatch.setattr(usagewatch, "_last_gemini_fail", 0.0)


def test_parse_gemini_quota_summary():
    parsed = usagewatch.parse_gemini_quota_summary(SAMPLE_QUOTA_RAW)
    assert parsed is not None
    assert "gemini_5h" in parsed
    assert "gemini_weekly" in parsed

    g5h = parsed["gemini_5h"]
    assert g5h["remaining"] == 0.52
    assert g5h["utilization"] == 0.48
    assert g5h["status"] == "allowed"
    assert g5h["resets_at"] > 0

    gwk = parsed["gemini_weekly"]
    assert gwk["remaining"] == 0.85
    assert gwk["utilization"] == 0.15


def test_observe_gemini_close_warning_sent_once():
    reset = time.time() + 3600
    info_low = {
        "gemini_5h": {
            "utilization": 0.50,
            "remaining": 0.50,
            "resets_at": reset,
            "status": "allowed",
        }
    }
    assert usagewatch.observe_gemini(info_low) == []

    info_high = {
        "gemini_5h": {
            "utilization": 0.84,
            "remaining": 0.16,
            "resets_at": reset,
            "status": "allowed",
        }
    }
    notes = usagewatch.observe_gemini(info_high)
    assert len(notes) == 1
    assert "Gemini's 5-hour usage is at 84%" in notes[0]
    assert "resets at" in notes[0]

    # Same window: not repeated
    assert usagewatch.observe_gemini(info_high) == []

    # Next window: can trigger again
    info_next = {
        "gemini_5h": {
            "utilization": 0.85,
            "remaining": 0.15,
            "resets_at": reset + 18000,
            "status": "allowed",
        }
    }
    assert len(usagewatch.observe_gemini(info_next)) == 1


def test_observe_gemini_used_up_and_reset():
    reset = time.time() + 600
    info_exhausted = {
        "gemini_5h": {
            "utilization": 1.0,
            "remaining": 0.0,
            "resets_at": reset,
            "status": "rejected",
        }
    }
    hit = usagewatch.observe_gemini(info_exhausted)
    assert len(hit) == 1
    assert "Gemini 5-hour limit is used up" in hit[0]
    assert time.strftime("%H:%M", time.localtime(reset)) in hit[0]

    # No duplicate while used up
    assert usagewatch.observe_gemini(info_exhausted) == []
    assert usagewatch.recently_hit()

    # Before reset time: no reset note
    assert usagewatch.check_reset(now=reset - 10) == []

    # After reset time: reset note sent once
    reset_notes = usagewatch.check_reset(now=reset + 10)
    assert len(reset_notes) == 1
    assert "Gemini 5-hour limit has reset" in reset_notes[0]
    assert usagewatch.check_reset(now=reset + 60) == []


def test_observe_handles_raw_gemini_payload():
    reset = time.time() + 1800
    raw = {
        "response": {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "bucketId": "gemini-5h",
                            "remainingFraction": 0.15,
                            "resetTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset)),
                        }
                    ],
                }
            ]
        }
    }
    notes = usagewatch.observe(raw)
    assert len(notes) == 1
    assert "Gemini" in notes[0]
    assert "85%" in notes[0]


def test_status_line_and_current_status():
    reset = time.time() + 3600
    usagewatch.observe_gemini({
        "gemini_5h": {
            "utilization": 0.48,
            "remaining": 0.52,
            "resets_at": reset,
            "status": "allowed",
        },
        "gemini_weekly": {
            "utilization": 0.15,
            "remaining": 0.85,
            "resets_at": reset + 86400 * 6,
        },
    })
    line = usagewatch.status_line("gemini")
    assert "Gemini 5-hour usage: 48%" in line
    assert "resets at" in line

    curr = usagewatch.current_status()
    assert "gemini" in curr
    assert curr["gemini"]["remaining"] == 0.52
    assert curr["gemini"]["util"] == 0.48
    assert "resets_at_str" in curr["gemini"]
    assert curr["gemini"]["weekly_remaining"] == 0.85

    summary = usagewatch.limit_summary()
    assert "Gemini 5h: 52% remaining" in summary


def test_runner_detects_gemini_quota_exhaustion():
    assert runner.hit_limit("Error: individual quota reached for gemini-4-hydrogen-high", True, 0) > time.time()
    assert runner.hit_limit("Exception: MODEL_CAPACITY_EXHAUSTED", True, 0) > time.time()
    assert runner.hit_limit("ResourceExhausted: 429 quota exhausted", True, 0) > time.time()


def test_runner_snapshot_includes_limits(monkeypatch):
    r = runner.Runner(lambda *a: None)
    snap = r.snapshot()
    assert "limits" in snap
    assert "limit_status" in snap


def test_gemini_quota_live_refresh_when_stale(monkeypatch):
    now = time.time()
    reset = now + 7200
    usagewatch.observe_gemini({
        "gemini_5h": {
            "utilization": 0.20,
            "remaining": 0.80,
            "resets_at": reset,
            "status": "allowed",
        }
    })
    # Make cached reading older than GEMINI_CACHE_TTL
    s = usagewatch._load()
    s["gemini"]["seen_at"] = now - (usagewatch.GEMINI_CACHE_TTL + 5)
    usagewatch._save(s)

    called = []

    def mock_fetch():
        called.append(True)
        return {
            "gemini_5h": {
                "utilization": 0.65,
                "remaining": 0.35,
                "resets_at": reset,
                "status": "allowed",
            }
        }

    monkeypatch.setattr(usagewatch, "fetch_gemini_quota", mock_fetch)

    curr = usagewatch.current_status()
    assert len(called) == 1
    assert curr["gemini"]["util"] == 0.65
    assert curr["gemini"]["remaining"] == 0.35

    line = usagewatch.status_line("gemini")
    assert "65%" in line


def test_gemini_quota_uses_cache_within_ttl(monkeypatch):
    now = time.time()
    reset = now + 7200
    usagewatch.observe_gemini({
        "gemini_5h": {
            "utilization": 0.20,
            "remaining": 0.80,
            "resets_at": reset,
            "status": "allowed",
        }
    })
    # Cache is fresh (seen_at is 5s ago)
    s = usagewatch._load()
    s["gemini"]["seen_at"] = now - 5
    usagewatch._save(s)

    called = []
    monkeypatch.setattr(usagewatch, "fetch_gemini_quota", lambda: called.append(True))

    curr = usagewatch.current_status()
    assert len(called) == 0
    assert curr["gemini"]["util"] == 0.20


def test_gemini_quota_refreshes_when_window_resets(monkeypatch):
    now = time.time()
    # Cache has expired window (past reset time)
    expired_reset = now - 60
    usagewatch.observe_gemini({
        "gemini_5h": {
            "utilization": 0.90,
            "remaining": 0.10,
            "resets_at": expired_reset,
            "status": "allowed",
        }
    })
    # Even if seen_at is recent, window is in the past
    s = usagewatch._load()
    s["gemini"]["seen_at"] = now - 2
    usagewatch._save(s)

    new_reset = now + 18000
    called = []

    def mock_fetch():
        called.append(True)
        return {
            "gemini_5h": {
                "utilization": 0.05,
                "remaining": 0.95,
                "resets_at": new_reset,
                "status": "allowed",
            }
        }

    monkeypatch.setattr(usagewatch, "fetch_gemini_quota", mock_fetch)

    curr = usagewatch.current_status()
    assert len(called) == 1
    assert curr["gemini"]["status"] != "Reset"
    assert curr["gemini"]["util"] == 0.05
    assert curr["gemini"]["remaining"] == 0.95

    line = usagewatch.status_line("gemini")
    assert "Gemini 5-hour usage: 5%" in line


def test_gemini_quota_force_refresh(monkeypatch):
    now = time.time()
    reset = now + 7200
    usagewatch.observe_gemini({
        "gemini_5h": {
            "utilization": 0.10,
            "remaining": 0.90,
            "resets_at": reset,
            "status": "allowed",
        }
    })
    called = []

    def mock_fetch():
        called.append(True)
        return {
            "gemini_5h": {
                "utilization": 0.30,
                "remaining": 0.70,
                "resets_at": reset,
                "status": "allowed",
            }
        }

    monkeypatch.setattr(usagewatch, "fetch_gemini_quota", mock_fetch)

    # Cache is fresh, but force=True triggers fetch
    curr = usagewatch.current_status(force=True)
    assert len(called) == 1
    assert curr["gemini"]["util"] == 0.30


def test_gemini_quota_graceful_fallback_when_daemon_fails(monkeypatch):
    now = time.time()
    reset = now + 3600
    usagewatch.observe_gemini({
        "gemini_5h": {
            "utilization": 0.40,
            "remaining": 0.60,
            "resets_at": reset,
            "status": "allowed",
        }
    })
    # Stale cache
    s = usagewatch._load()
    s["gemini"]["seen_at"] = now - 100
    usagewatch._save(s)

    monkeypatch.setattr(usagewatch, "fetch_gemini_quota", lambda: None)

    # Should not raise exception, falls back to cached data
    curr = usagewatch.current_status()
    assert curr["gemini"]["util"] == 0.40
    assert curr["gemini"]["remaining"] == 0.60
