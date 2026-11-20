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
    assert runner.hit_limit("Error: individual quota reached for gemini-3.8-flash-high", True, 0) > time.time()
    assert runner.hit_limit("Exception: MODEL_CAPACITY_EXHAUSTED", True, 0) > time.time()
    assert runner.hit_limit("ResourceExhausted: 429 quota exhausted", True, 0) > time.time()


def test_runner_snapshot_includes_limits(monkeypatch):
    r = runner.Runner(lambda *a: None)
    snap = r.snapshot()
    assert "limits" in snap
    assert "limit_status" in snap
