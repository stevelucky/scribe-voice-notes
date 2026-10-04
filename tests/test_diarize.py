"""Diarization must never lose a note: a failure (e.g. GPU out-of-memory on a very
long meeting) is caught and the transcript passes through unchanged."""

from unittest.mock import patch

from src.cli import _apply_diarization


def test_apply_diarization_is_non_fatal_on_error():
    transcript = {"text": "the meeting text",
                  "segments": [{"start": 0, "end": 1, "text": "hi"}]}
    with patch("src.diarizer.Diarizer", side_effect=RuntimeError(
            "MPS backend out of memory (MPS allocated: 21.62 GiB)")):
        out = _apply_diarization(transcript, "/tmp/x.wav")
    assert out is transcript          # unchanged → the note still gets written


def test_apply_diarization_merges_on_success():
    transcript = {"text": "hi", "segments": [{"start": 0, "end": 1, "text": "hi"}]}

    class FakeDiarizer:
        def diarize(self, path):
            return [{"start": 0, "end": 1, "speaker": "SPEAKER_00"}]

    merged = {"text": "SPEAKER_00: hi", "segments": transcript["segments"]}
    with patch("src.diarizer.Diarizer", FakeDiarizer), \
         patch("src.diarizer.identify_and_merge", return_value=merged) as m:
        out = _apply_diarization(transcript, "/tmp/x.wav")
    m.assert_called_once()
    assert out["text"] == "SPEAKER_00: hi"
