"""Audio recorder using sounddevice. Records from default mic to WAV files.

Writes audio incrementally to disk so data is never lost on crash/force-kill.
Monitors for default input device changes and seamlessly switches mid-recording.
"""

import atexit
import os
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np
import sounddevice as sd
import soundfile as sf

from .config import CONFIG

# Live-meter parameters. Bands are log-spaced across the voice-relevant range;
# magnitudes are mapped from dB so true silence (a dead/muted mic) reads ~0 while
# normal speech lights up the middle bands.
_METER_BANDS = 5
_METER_INTERVAL = 0.066          # ~15 Hz emit rate
_METER_DB_FLOOR = -60.0          # dB at/below which a band reads 0 (silence)
_METER_DB_CEIL = -12.0           # dB at/above which a band reads 1 (loud)
_METER_MAX_SAMPLES = 2048        # cap FFT size for a cheap, bounded callback


def _compute_bands(mono: "np.ndarray", sample_rate: int) -> "list[float]":
    """Return _METER_BANDS normalized levels (0–1) from a mono audio block."""
    n = mono.shape[0]
    if n == 0:
        return [0.0] * _METER_BANDS
    if n > _METER_MAX_SAMPLES:
        mono = mono[-_METER_MAX_SAMPLES:]
        n = _METER_MAX_SAMPLES
    spec = np.abs(np.fft.rfft(mono * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    nyquist = sample_rate / 2.0
    edges = np.logspace(np.log10(80.0), np.log10(min(8000.0, nyquist)), _METER_BANDS + 1)
    out = []
    for i in range(_METER_BANDS):
        mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
        amp = (spec[mask].mean() / (n / 2.0)) if mask.any() else 0.0
        db = 20.0 * np.log10(amp + 1e-9)
        level = (db - _METER_DB_FLOOR) / (_METER_DB_CEIL - _METER_DB_FLOOR)
        out.append(float(min(1.0, max(0.0, level))))
    return out


def _default_input_name() -> str | None:
    """Return the name of the current default input device, or None."""
    try:
        idx = sd.default.device[0]
        if idx < 0:
            return None
        return sd.query_devices(idx)["name"]
    except Exception:
        return None


def _open_stream(sample_rate, channels, callback):
    stream = sd.InputStream(
        samplerate=sample_rate,
        channels=channels,
        dtype="float32",
        callback=callback,
    )
    stream.start()
    return stream


class Recorder:
    def __init__(self, output_path: str = None, on_level=None, on_state=None):
        self._cfg = CONFIG["recording"]
        self._sample_rate = self._cfg["sample_rate"]
        self._channels = self._cfg["channels"]
        self._max_duration = self._cfg["max_duration_minutes"] * 60
        self._recordings_dir = self._cfg["recordings_dir"]
        self._output_path = output_path
        # Optional live-meter callback: receives a list of band levels (0–1).
        self._on_level = on_level
        # Optional pause-state callback: receives (paused: bool, reason: str).
        self._on_state = on_state
        self._last_meter = 0.0

        # Silence auto-pause: after this many seconds of continuous silence, stop
        # writing frames (the file "pauses") until sound returns — so a meeting left
        # running over a break doesn't bloat into a huge mostly-silent file. 0 = off.
        self._auto_pause_after = float(self._cfg.get("auto_pause_silence_seconds", 0) or 0)
        self._silence_level = float(self._cfg.get("auto_pause_level", 0.01) or 0.01)  # RMS
        self._preroll_seconds = 1.0        # lead-in flushed on resume, so no clipped word
        self._auto_paused = False
        self._manual_paused = False
        self._silence_run = 0.0            # seconds of continuous silence so far
        self._preroll = deque()            # bounded ring of recent blocks
        self._preroll_dur = 0.0
        os.makedirs(self._recordings_dir, exist_ok=True)

        self._stream = None
        self._stream_lock = threading.Lock()
        self._recording = False
        self._start_time = None
        self._lock = threading.Lock()
        self._file = None
        self._filepath = None
        self._write_queue = None
        self._writer_thread = None
        self._stop_writer = threading.Event()
        self._monitor_thread = None
        self._stop_monitor = threading.Event()

    @property
    def is_recording(self):
        return self._recording

    @property
    def elapsed(self):
        if self._start_time and self._recording:
            return time.time() - self._start_time
        return 0.0

    @property
    def filepath(self):
        return self._filepath

    def _callback(self, indata, frames, time_info, status):
        # Audio thread: enqueue only, never block on disk I/O.
        if status:
            print(f"[recorder] {status}", file=sys.stderr)
        if self._write_queue is not None:
            block = indata.copy()
            if self._decide_write(block, frames):
                self._write_queue.put(block)
        # Live meter: compute band levels at a throttled rate. Cheap FFT on a
        # small block; failures here must never disturb recording. Keep running
        # while paused so the user still sees input (and when sound returns).
        if self._on_level is not None:
            now = time.time()
            if now - self._last_meter >= _METER_INTERVAL:
                self._last_meter = now
                try:
                    mono = indata[:, 0] if indata.shape[1] == 1 else indata.mean(axis=1)
                    self._on_level(_compute_bands(np.asarray(mono, dtype=np.float64),
                                                  self._sample_rate))
                except Exception:
                    pass

    # ─── silence auto-pause + manual pause ──────────────────────────────────
    # All of this runs on the audio thread (except toggle_manual_pause, called
    # from a signal handler). Booleans are flipped/read across threads under the
    # GIL, which is atomic enough here; the write queue is thread-safe.

    def _decide_write(self, block, frames) -> bool:
        """Whether to write this audio block. Honors a manual pause and a
        silence-based auto-pause; on resume it flushes a short pre-roll so the
        first word back is never clipped."""
        if self._manual_paused:
            self._push_preroll(block)
            return False
        if self._auto_pause_after <= 0:
            return True  # auto-pause disabled → always write

        try:
            rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64)))) if block.size else 0.0
        except Exception:
            return True  # on any error, err toward keeping the audio
        silent = rms < self._silence_level

        if self._auto_paused:
            self._push_preroll(block)
            if silent:
                return False
            self._auto_paused = False        # sound returned → resume
            self._silence_run = 0.0
            self._flush_preroll()
            self._emit_state("recording")
            return True

        if silent:
            self._silence_run += frames / self._sample_rate
            if self._silence_run >= self._auto_pause_after:
                self._auto_paused = True
                self._preroll.clear(); self._preroll_dur = 0.0
                self._emit_state("silent")
                return False
        else:
            self._silence_run = 0.0
        return True

    def _push_preroll(self, block):
        """Keep ~preroll_seconds of the most recent blocks so a resume can flush a
        lead-in. Blocks are ~equal length, so bound by count."""
        dur = block.shape[0] / self._sample_rate
        self._preroll.append(block)
        self._preroll_dur += dur
        while self._preroll_dur > self._preroll_seconds and len(self._preroll) > 1:
            self._preroll.popleft()
            self._preroll_dur -= dur

    def _flush_preroll(self):
        if self._write_queue is not None:
            while self._preroll:
                self._write_queue.put(self._preroll.popleft())
        self._preroll.clear()
        self._preroll_dur = 0.0

    def _emit_state(self, reason: str):
        if self._on_state is None:
            return
        try:
            self._on_state(self._manual_paused or self._auto_paused, reason)
        except Exception:
            pass

    @property
    def is_paused(self):
        return self._manual_paused or self._auto_paused

    def toggle_manual_pause(self):
        """Toggle a deliberate pause (e.g. from the menu bar's Pause button)."""
        if self._manual_paused:
            self._manual_paused = False
            self._flush_preroll()
            self._emit_state("recording")
        else:
            self._manual_paused = True
            self._auto_paused = False
            self._silence_run = 0.0
            self._preroll.clear(); self._preroll_dur = 0.0
            self._emit_state("manual")

    def _writer_loop(self):
        while True:
            try:
                chunk = self._write_queue.get(timeout=0.1)
            except queue.Empty:
                if self._stop_writer.is_set():
                    break
                continue
            try:
                if self._file is not None:
                    self._file.write(chunk)
            except Exception as e:
                print(f"[recorder] writer error: {e}", file=sys.stderr)
            finally:
                self._write_queue.task_done()

    def _device_monitor_loop(self):
        """Poll for default input device changes and restart the stream if needed."""
        current_device = _default_input_name()
        while not self._stop_monitor.wait(timeout=2.0):
            new_device = _default_input_name()
            if new_device and new_device != current_device:
                print(
                    f"[recorder] Input device changed: {current_device!r} → {new_device!r}",
                    file=sys.stderr,
                )
                current_device = new_device
                self._switch_stream()

    def _switch_stream(self):
        """Replace the active audio stream with a new one on the current default device.

        Stop the old stream *before* starting the new one so the two callbacks
        can't both enqueue audio at once (which would interleave/duplicate frames
        in the WAV). A brief gap during the switch is preferable to garbled audio.
        """
        with self._stream_lock:
            old = self._stream
            self._stream = None
            if old is not None:
                try:
                    old.stop()
                    old.close()
                except Exception:
                    pass
            try:
                self._stream = _open_stream(self._sample_rate, self._channels, self._callback)
            except Exception as e:
                print(f"[recorder] Failed to open new stream: {e}", file=sys.stderr)

    def start(self):
        with self._lock:
            if self._recording:
                return None

            if self._output_path:
                self._filepath = self._output_path
                parent = os.path.dirname(self._filepath)
                if parent:
                    os.makedirs(parent, exist_ok=True)
            else:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"recording_{timestamp}.wav"
                self._filepath = os.path.join(self._recordings_dir, filename)
            self._file = sf.SoundFile(
                self._filepath,
                mode="w",
                samplerate=self._sample_rate,
                channels=self._channels,
                format="WAV",
                subtype="FLOAT",
            )

            # Writer thread starts before audio stream so queue is drained from frame 1.
            self._write_queue = queue.Queue()
            self._stop_writer.clear()
            self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
            self._writer_thread.start()

            with self._stream_lock:
                self._stream = _open_stream(self._sample_rate, self._channels, self._callback)
            self._recording = True
            self._start_time = time.time()

            # Watch for input device changes (e.g. AirPods removed mid-meeting).
            self._stop_monitor.clear()
            self._monitor_thread = threading.Thread(target=self._device_monitor_loop, daemon=True)
            self._monitor_thread.start()

            atexit.register(self._emergency_save)

        return True

    def stop(self):
        with self._lock:
            if not self._recording:
                return None
            self._recording = False

            # Stop device monitor first.
            self._stop_monitor.set()
            if self._monitor_thread is not None:
                self._monitor_thread.join(timeout=5)
                self._monitor_thread = None

            with self._stream_lock:
                stream = self._stream
                self._stream = None
            if stream is not None:
                stream.stop()
                stream.close()

            filepath = self._filepath
            duration = time.time() - self._start_time if self._start_time else 0
            self._start_time = None

            self._stop_writer.set()
            if self._writer_thread is not None:
                self._writer_thread.join(timeout=10)
                if self._writer_thread.is_alive():
                    print(
                        "[recorder] WARNING: writer thread did not drain within 10s; "
                        "trailing audio may be lost.",
                        file=sys.stderr,
                    )
                self._writer_thread = None
            self._write_queue = None

            if self._file is not None:
                self._file.close()
                self._file = None

            try:
                atexit.unregister(self._emergency_save)
            except Exception:
                pass

        if filepath and os.path.exists(filepath):
            # A header-only float WAV is ~58 bytes (fact/PEAK chunks), so a byte
            # threshold can't tell "empty" from "tiny". Check actual frames instead.
            try:
                frames = sf.info(filepath).frames
            except Exception:
                frames = os.path.getsize(filepath)  # fall back to bytes if unreadable
            if frames > 0:
                print(f"[recorder] Saved: {filepath} ({duration:.1f}s)", file=sys.stderr)
                return filepath
            else:
                os.remove(filepath)
                return None
        return None

    def _emergency_save(self):
        """Called by atexit — close the file so whatever was written is valid.

        Stop the audio stream and the writer thread before closing the file, so
        the (daemon) writer can't race close() — SoundFile is not thread-safe.
        """
        try:
            with self._stream_lock:
                stream = self._stream
                self._stream = None
            if stream is not None:
                stream.stop()
                stream.close()
            # Signal the writer to stop and give it a moment to drain.
            self._stop_writer.set()
            writer = self._writer_thread
            if writer is not None:
                writer.join(timeout=2)
            if self._file is not None:
                self._file.close()
                self._file = None
                print(f"\n[recorder] Emergency save: {self._filepath}", file=sys.stderr)
        except Exception:
            pass

    def toggle(self):
        """Toggle recording on/off. Returns filepath when stopping, True when starting."""
        if self._recording:
            return self.stop()
        else:
            return self.start()
