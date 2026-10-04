"""Tests for the recorder's silence auto-pause + manual pause gating.

Exercises `_decide_write` with synthetic audio blocks (no real mic). Sample rate
is set to 100 so 10-frame blocks are 0.1s, keeping thresholds easy to reason about.
"""

import queue

import numpy as np

from src.recorder import Recorder


def _rec(auto_pause_after=1.0, sr=100):
    r = Recorder()                     # real __init__ (no audio until start())
    r._sample_rate = sr
    r._auto_pause_after = auto_pause_after
    r._silence_level = 0.01
    r._preroll_seconds = 0.05
    r._auto_paused = False
    r._manual_paused = False
    r._silence_run = 0.0
    r._preroll.clear(); r._preroll_dur = 0.0
    r._write_queue = queue.Queue()
    r._on_state = None
    return r


def _silent(n): return np.zeros((n, 1), dtype=np.float32)
def _loud(n):   return np.full((n, 1), 0.5, dtype=np.float32)


def test_pauses_after_sustained_silence_then_resumes_on_sound():
    r = _rec(auto_pause_after=1.0)     # 1s of silence → pause
    assert r._decide_write(_loud(10), 10) is True       # speech is written

    paused = False
    for _ in range(12):                # 1.2s of silence
        if r._decide_write(_silent(10), 10) is False:
            paused = True
    assert paused and r._auto_paused
    assert r._decide_write(_silent(10), 10) is False     # still silent → not writing

    assert r._decide_write(_loud(10), 10) is True        # sound back → resume
    assert r._auto_paused is False


def test_resume_flushes_preroll_lead_in():
    r = _rec(auto_pause_after=0.5)
    for _ in range(6):                 # 0.6s silence → pause
        r._decide_write(_silent(10), 10)
    assert r._auto_paused
    # a couple more silent blocks accumulate in the (bounded) pre-roll ring
    r._decide_write(_silent(10), 10)
    r._decide_write(_silent(10), 10)
    before = r._write_queue.qsize()
    r._decide_write(_loud(10), 10)     # resume flushes the buffered pre-roll lead-in
    assert r._write_queue.qsize() >= before + 1


def test_manual_pause_blocks_even_loud_audio():
    r = _rec()
    assert r._decide_write(_loud(10), 10) is True
    r.toggle_manual_pause()
    assert r._manual_paused and r.is_paused
    assert r._decide_write(_loud(10), 10) is False       # loud, but manually paused
    r.toggle_manual_pause()
    assert not r._manual_paused
    assert r._decide_write(_loud(10), 10) is True


def test_disabled_when_threshold_zero_never_pauses():
    r = _rec(auto_pause_after=0)
    for _ in range(200):               # 20s of dead silence
        assert r._decide_write(_silent(10), 10) is True


def test_emits_state_callbacks_on_transitions():
    states = []
    r = _rec(auto_pause_after=0.3)
    r._on_state = lambda paused, reason: states.append((paused, reason))
    for _ in range(4):                 # 0.4s silence → auto-pause
        r._decide_write(_silent(10), 10)
    assert (True, "silent") in states
    r._decide_write(_loud(10), 10)     # sound → resume
    assert (False, "recording") in states
    r.toggle_manual_pause()            # deliberate pause
    assert (True, "manual") in states
