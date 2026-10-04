"""Tests for CLI helpers — transcription progress emission and ETA formatting."""

from src.cli import _fmt_secs, _make_transcribe_progress


def test_fmt_secs():
    assert _fmt_secs(5) == "5s"
    assert _fmt_secs(65) == "1m 5s"
    assert _fmt_secs(3700) == "1h 1m"
    assert _fmt_secs(-3) == "0s"


def test_transcribe_progress_throttles_and_emits_pct():
    calls = []
    cb = _make_transcribe_progress(lambda name, detail, **extra: calls.append((name, detail, extra)))
    cb(0.0)      # first call → emits
    cb(0.001)    # within the throttle window → suppressed
    cb(1.0)      # final (>=0.999) → always emits
    assert [c[2]["pct"] for c in calls] == [0, 100]
    assert all(c[0] == "transcribing" for c in calls)
    assert "%" in calls[-1][1]
