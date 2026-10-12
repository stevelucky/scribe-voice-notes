# Scribe — Front-End Roadmap

A consumption/synthesis layer on top of the existing capture pipeline. The menu-bar
app captures and processes audio into markdown notes; this front end turns that
growing pile of notes into a live operating picture.

## Principles

1. **Markdown stays the source of truth.** The front end is a lens and a writer,
   never a second database. Checking off / reassigning a to-do edits the source `.md`.
   This keeps the Obsidian workflow intact and avoids lock-in.
2. **Zero data entry.** Structure comes automatically from the voice → LLM pipeline
   (frontmatter: title/date/topics/action_items/decisions). The UI aggregates and
   presents; the user never re-types.
3. **Local-first & private.** Runs locally against the notes folder. No cloud sync.
4. **Reuse the Python stack.** Config, parsing, and the Claude integration already
   exist; the web layer builds on them.

## Architecture & stack

- **Backend:** FastAPI (Python, `scribe` conda env), reusing `src/notes_index.py`
  for parsing notes/frontmatter/action-items and markdown write-back; `src/brief.py`
  for the AI brief.
- **Front end:** server-rendered HTML with **htmx** + **Tailwind** + a little vanilla
  JS for animations and live counts. No Node toolchain.
- **AI:** reuses the Claude integration + the notes corpus.
- **Run:** **Open Dashboard** from the menu-bar app (launches a *persistent, detached*
  uvicorn on `:8765` and opens the browser). For dev: `uvicorn web.app:app --reload`
  → `localhost:8000`.

## Status at a glance

- **Phase 1 — Action Center:** ✅ shipped (+ extensive UX polish)
- **Phase 2 — This Week brief:** ✅ shipped
- **Someday (long-term ideas):** ✅ shipped — capture, review queue, menu-bar modes + auto-infer, develop-in-browser, and archive
- **Phase 3 — Entities:** ⬜ not started
- **Phase 4 — Ask-your-notes chat:** ⬜ not started
- **Phase 5 — Reading & browse:** ⬜ not started
- Pipeline/infra side-quests (extraction tightening, retention, server persistence,
  identity settings, favicon): ✅ shipped

---

## Someday — long-term ideas + review queue

A place for ideas you want to keep but not act on yet, plus a triage queue for
anything the extraction flags as "this might belong elsewhere." Symmetric model:
a meeting trusts its to-dos (→ Action Center) and flags stray *ideas* for review;
an idea session trusts its ideas (→ parked) and flags stray *to-dos* for review.
Markdown stays the source of truth — flagged/parked items are checkbox lines
tagged with an inline `<!-- someday: review|parked [idea|todo] -->` comment, so
they never touch the Action Center buckets or counts.

### Phase A — web + pipeline  ✅
- [x] New `idea` meeting type in `prompts.json` (idea-shaped extraction; to-dos
      flagged for review, never auto-filed).
- [x] `someday_ideas` field added to all 6 meeting prompts → flagged for review.
- [x] `output.py` renders flagged items as tagged review checkboxes (direction by
      note type); live meeting to-dos unchanged.
- [x] `notes_index.py` — parse/compose the someday tag, a `someday` bucket excluded
      from Action Center stats, and triage ops: confirm-idea (park), confirm-to-do
      (set owner+deadline → Action Center), dismiss (delete line).
- [x] **Someday** web tab (`/someday`): Needs-review queue + parked ideas / idea
      cards, with nav badge for the review count. Tests in `tests/test_someday.py`.
- Idea capture today: `python -m src.cli process <audio> --type idea` (or `quick`).

### Phase B — menu bar fast-follow  ✅
- [x] "Record Idea" action in the menu bar (records, then `process-latest --type idea`).
      A `CaptureMode` on `RecordingManager` tags the in-flight session; the dropdown
      shows "Recording idea". Idle menu reads **Record Meeting / Record Idea**.
      Rebuild the app to pick it up: `cd menubar && ./build.sh`.
- [x] **Auto-detect for imports** — files with no capture mode (Process Audio File,
      phone/watch-folder imports) run `process … --type auto`, which classifies the
      transcript as meeting vs idea (`processor.classify_recording`) and routes it.
      Explicit Record Meeting / Record Idea bypass it. Mis-guesses self-correct via
      the review queue, so the classifier only needs to be roughly right.

### Phase 2 — in-browser / linked capture  ✅
- [x] **Develop this idea** — record straight in the browser (MediaRecorder on the
      idea's note page) → upload to `/idea/develop`, which runs the normal pipeline
      as a subprocess (`process --type default --from-idea <origin>`) → a working
      note with real to-dos in the Action Center, linked back to the idea.
- [x] **Backlinks** both directions: the idea lists "Developed into → sessions";
      each session shows "Developed from idea → …" (resolved via `from_idea`).
- [x] **Archive / Restore** an idea — `archived` frontmatter flag removes it from
      Someday while keeping it in All Notes (distinct from Delete → trash); reversible.
- Needs a browser mic grant (one-time) and a dashboard restart to pick up the new
  routes. Optional later: a quick "Develop" / "developed" badge on the Someday card.

## Phase 1 — Action-item command center  ✅

- [x] `src/notes_index.py` — shared parser (frontmatter + action items w/ owner,
      deadline, source, line number) and markdown write-back.
- [x] FastAPI app + Action Center at `/actions`, grouped by note.
- [x] Check-off writes `- [ ]` → `- [x]` (verified round-trip).
- [x] **Identity + buckets** — collapse the user's many owner labels into one
      identity (`user.name`/`aliases`, editable in **Settings → Identity**); split
      into **Mine / Others / Unassigned**, default Mine. SPEAKER_NN → Unassigned.
- [x] **Reassign owners** — per-item dropdown writes back and re-buckets; the row
      **animates out of the current tab** when its bucket changes.
- [x] **Done model** — checking strikes a row in place (reversible by un-checking);
      a sticky **Clear completed** button sweeps checked rows to the **Done** tab with
      a fade + slide-up animation; Done tab lets you un-check to restore.
- [x] **Sticky header** — stats line, tabs, Clear-completed button, and search stay
      pinned while the list scrolls.
- [x] **Live counts** — header total, per-tab badges (via `/stats`), and a live
      **"N selected"** indicator update without a page refresh.
- [x] Text search; browser ghost-check fixed (`autocomplete=off`).
- [ ] Nice-to-have: sort options (by deadline / recency / note).

## Phase 2 — This Week brief  ✅

- [x] **This Week** is the landing page (`/`); top nav switches to Action Center.
- [x] **AI brief** (`src/brief.py`) — second-person LLM narrative (the gist / your
      next actions / waiting on / deadlines) from the last 30 days, cached to
      `.scribe_brief.json`, with a Refresh button.
- [x] Deterministic sections: **Deadlines** and **Recent meetings** (last 30 days).
- [x] Today's date shown on the header.
- [x] **Deadline date-parsing** (`dateparser`) — freeform deadlines ("by Sept 1",
      "next week", "Week of …") are parsed relative to the meeting date; the
      Deadlines section now **sorts by urgency** (overdue → soon → later → undated)
      and **color-codes** (red overdue / amber within 7d / slate dated / gray
      undated) with relative labels ("37d overdue", "in 3 days", "Aug 1"). Vague
      strings (project phases, conditions) stay undated.
- [ ] **Weekly update archive** *(requested)* — make the brief a dated, saved weekly
      artifact and let the user browse **past weekly updates** as a running record of
      "where things stood." Implies storing dated briefs (not one rolling cache) plus
      a history view; also resolves the "This Week" vs 30-day-window naming tension.
- [ ] (Optional) scheduled/auto-generation of the brief.

## Pipeline & capture-app work done along the way  ✅

- [x] **Extraction tightened** across all 6 meeting types: only explicit, concrete
      commitments; `null` owner when unclear (no guessing).
- [x] `reprocess-all` CLI to re-extract existing notes in place.
      **Limitation found:** notes whose transcript (and original audio) no longer
      exist can't be reprocessed — most of the legacy backlog is in that bucket, so
      it ages out via recency rather than cleanup.
- [x] **Audio retention** — `recording.retention_days` + Settings picker; auto-prune.
- [x] **Identity settings** — Name + aliases in Settings (new installs start blank).
- [x] **Persistent dashboard server** — detached launch so browser refresh works.
- [x] **Favicon** — small generated icon in `web/static/`.

---

## Phone capture (record away from the laptop)  🟡 Mac side done

Transport: record in **Voice Memos** → share the memo into a watched iCloud Drive
folder → the Mac auto-imports it. (Chosen over a custom recorder for v1: Voice Memos
already nails background recording, pause, low power, and crash-safety.)

- [x] Pipeline accepts non-WAV imports — `_ensure_wav` transcodes m4a/etc. to 16kHz
      mono via ffmpeg, then runs the normal transcribe → extract → note flow.
- [x] **Folder watcher** in the menu-bar app — polls the watched folder, triggers
      iCloud download of placeholders, imports one ready file per pass, then deletes
      the shared copy on success (failures go to `_failed/` for retry). The processed
      audio lands in `recordings/` and is governed by "Keep audio files" retention, so
      the iCloud folder doesn't bloat. Settings: enable + pick folder.
- [ ] Optional later: a custom SwiftUI **iOS app** (one-tap record/pause, m4a 16kHz,
      background audio mode, auto-save to the same iCloud folder) — only if the Voice
      Memos flow proves useful. Needs a paid Apple Developer account.

## Phase 3 — Entities  ⬜

- [ ] Extract people / companies / projects (frontmatter owners + an LLM pass).
- [ ] Entity pages aggregating every mention, action item, and decision for a
      person/company ("everything Service Titan", "every call with Doug").
- [ ] Consolidate the **MCP server's note parsing onto `src/notes_index.py`** (it
      still has its own copy).

## Phase 4 — Ask-your-notes chat  ⬜

- [ ] Chat panel with streaming answers over the whole corpus.
- [ ] Citations linking back to source notes.
- [ ] Reuse provider config from `config.yaml` (the MCP server already proves the
      retrieval; this makes it first-class in the app).

## Phase 5 — Reading & browse  ⬜

- [ ] Note list with topic/date filters + full-text search.
- [ ] Reader view with a frontmatter header card + rendered transcript.
- [ ] Polish, theming.

---

## Comparison notes (what to borrow / skip)

- **Granola:** organize by person/company; ask across meetings. *Borrow (→ Phase 3/4).*
- **Notion:** database-with-views, rollups, relations. *Borrow the filtered-view +
  rollup model (largely done in Phase 1); skip the generic editor & collaboration.*
- **Mem/Reflect/Tana:** chat-with-notes, auto-linking. *Borrow RAG-over-corpus (→ Phase 4).*
- **Don't rebuild:** generic editor, permissions/collab, or a fancier markdown
  renderer — Obsidian already covers rendering.
