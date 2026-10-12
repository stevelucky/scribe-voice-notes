"""Speaker Diarization using pyannote.audio."""
import os
import sys

class Diarizer:
    def __init__(self):
        token = os.environ.get("HF_TOKEN")
        if not token:
            raise ValueError(
                "HF_TOKEN environment variable not set. "
                "Please get a token from Hugging Face and accept the pyannote/speaker-diarization-3.1 terms."
            )

        # Let MPS use the machine's full memory instead of the default ~70%
        # watermark, so a long meeting doesn't hit an artificial GPU cap. Must be
        # set before torch initializes the backend; a user override is respected.
        os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.0")

        try:
            from pyannote.audio import Pipeline
            import torch
        except ImportError:
            raise ImportError(
                "pyannote.audio is not installed. "
                "Please install it with: pip install pyannote.audio torch torchaudio"
            )
        self._torch = torch

        print("[diarizer] Loading pyannote pipeline...", file=sys.stderr)
        self.pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=token
        )

        # Prefer the GPU (Apple Silicon MPS / CUDA), else CPU.
        self._device = "cpu"
        if torch.backends.mps.is_available():
            self._device = "mps"
        elif torch.cuda.is_available():
            self._device = "cuda"
        self.pipeline.to(torch.device(self._device))

    def _empty_cache(self):
        try:
            if self._torch.backends.mps.is_available():
                self._torch.mps.empty_cache()
            elif self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()
        except Exception:
            pass

    def diarize(self, audio_path: str) -> list:
        """Run diarization and return a list of segments.

        On a GPU out-of-memory (long meetings can exceed the Metal/CUDA budget)
        it retries once on CPU — slower, but bounded by system RAM rather than the
        GPU's cap.
        """
        print(f"[diarizer] Diarizing on {self._device}: {audio_path}", file=sys.stderr)
        try:
            out = self.pipeline(audio_path)
        except (RuntimeError, MemoryError) as e:
            if self._device == "cpu" or "out of memory" not in str(e).lower():
                raise
            print(f"[diarizer] {self._device.upper()} ran out of memory on this file — "
                  f"retrying on CPU (slower).", file=sys.stderr)
            self._empty_cache()
            self._device = "cpu"
            self.pipeline.to(self._torch.device("cpu"))
            out = self.pipeline(audio_path)

        # pyannote.audio 3.1+ may return a DiarizeOutput object
        if hasattr(out, "itertracks"):
            annotation = out
        else:
            annotation = out.speaker_diarization

        segments = []
        for turn, _, speaker in annotation.itertracks(yield_label=True):
            segments.append({
                "start": turn.start,
                "end": turn.end,
                "speaker": speaker
            })

        return segments


def identify_and_merge(transcript: dict, speaker_segments: list, audio_path: str, project_dir: str) -> dict:
    """Identify known speakers, then merge into transcript."""
    from .speaker_profiles import identify_speakers
    speaker_segments = identify_speakers(project_dir, speaker_segments, audio_path)
    return merge_diarization(transcript, speaker_segments)


def merge_diarization(transcript: dict, speaker_segments: list) -> dict:
    """Merge speaker segments into the transcript segments."""
    if not speaker_segments or not transcript.get("segments"):
        return transcript
        
    for t_seg in transcript["segments"]:
        t_start = t_seg.get("start", 0)
        t_end = t_seg.get("end", 0)
        
        # Find the speaker segment with the most overlap
        max_overlap = 0
        best_speaker = "UNKNOWN"
        
        for s_seg in speaker_segments:
            overlap_start = max(t_start, s_seg["start"])
            overlap_end = min(t_end, s_seg["end"])
            overlap = max(0, overlap_end - overlap_start)
            
            if overlap > max_overlap:
                max_overlap = overlap
                best_speaker = s_seg["speaker"]
                
        t_seg["speaker"] = best_speaker

    # Reconstruct the full text with speaker labels
    # We want to collapse contiguous segments from the same speaker
    final_text = ""
    current_speaker = None
    
    for seg in transcript["segments"]:
        speaker = seg.get("speaker", "UNKNOWN")
        text = seg.get("text", "").strip()
        if not text:
            continue
            
        if speaker != current_speaker:
            if final_text:
                final_text += "\n\n"
            final_text += f"{speaker}: {text}"
            current_speaker = speaker
        else:
            final_text += f" {text}"
            
    transcript["text"] = final_text
    return transcript
