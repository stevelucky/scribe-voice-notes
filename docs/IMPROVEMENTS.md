# Improvements & Roadmap

Tracks known bottlenecks, rough edges, and planned work. Keep entries short;
move anything that becomes an architecture decision into `docs/adr/`.

## Open

- **Project name consolidation → Scribe (DONE, one manual step left).** The
  codebase has been swept from Chikki/Singdana to **Scribe**. The conda env was
  cloned `chikki` → `scribe`, all code defaults now point at `scribe`
  (`environment.yml`, `install.sh`, `src/hotkey.py`, and `defaultCondaEnv` in
  `RecordingManager.swift`), and the Swift app resolves the env name from a
  single `RecordingManager.condaEnvName()` (overridable via
  `defaults write com.local.scribe condaEnv <name>`).

  The currently-installed app was pointed at the new env with
  `defaults write com.local.scribe condaEnv scribe` (bridges until it's rebuilt
  with the new binary). The old `chikki` env is **intentionally left in place**
  as a rollback safety net.

  **Remaining manual steps (after you've verified a real record→note cycle):**
  1. Rebuild/reinstall the menu bar app so the binary's own default is `scribe`:
     `cd menubar && ./build.sh --install`
  2. Drop the now-redundant override: `defaults delete com.local.scribe condaEnv`
  3. Remove the old env: `conda env remove -n chikki -y`

  Rollback at any point before step 3: `defaults write com.local.scribe condaEnv chikki`.
- **Anthropic JSON reliability.** Unlike gemini/openai, the Anthropic branch has
  no native JSON-mode; it relies on prompt instructions + best-effort parsing.
  Consider assistant-prefill (`{`) if truncation/invalid-JSON recurs.
- **Chunked-note rendering.** `process_chunked` emits a `sections` list that
  `output.py` renders generically. A dedicated renderer would read better.

## Done

- **Diarization no longer loses long-meeting notes to GPU OOM.** pyannote on MPS
  hit `MPS backend out of memory` on long files, crashing the whole pipeline (the
  transcribed note was lost). Now: MPS gets full-memory headroom
  (`PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0`), an OOM retries once on CPU, and the
  diarization step is non-fatal — on any failure the note is still written, just
  without speaker labels (`_apply_diarization` in `cli.py`). Same CPU fallback on
  the speaker-embedding path.
- Fixed `process_chunked` crash (called a nonexistent `self._call_llm`).
- Hardened the MCP server against path traversal and malformed-note parse errors.
- Enforced `recording.max_duration_minutes` as a safety cap.
