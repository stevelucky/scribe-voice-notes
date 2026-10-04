"""Output module: writes structured markdown notes."""

import os
import json
import re
from datetime import datetime

from .config import CONFIG


def _yaml_str(value: str) -> str:
    """Serialize a string as a safe single-line YAML scalar (properly quoted/escaped).

    A JSON string literal is also a valid YAML double-quoted scalar, so json.dumps
    gives correct quoting/escaping without yaml.safe_dump's trailing `...` document
    marker.
    """
    return json.dumps(value, ensure_ascii=False)


def _parse_recording_time(audio_path: str) -> datetime:
    """Extract datetime from recording filename, fall back to file mtime, then now."""
    fname = os.path.basename(audio_path)
    m = re.search(r'(\d{8})_(\d{6})', fname)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S')
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(os.path.getmtime(audio_path))
    except Exception:
        return datetime.now()


def _safe_filename(title: str, max_len: int = 80) -> str:
    """Convert a title to a safe filename, preserving case and spaces."""
    # Replace characters invalid on macOS/Windows filesystems
    for ch in r'/\:*?"<>|':
        title = title.replace(ch, "-")
    # Collapse multiple spaces/dashes and strip edges
    title = " ".join(title.split()).strip(" -")
    return title[:max_len].rstrip(" -")


def _format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def write_note(processed: dict, transcript: dict, audio_path: str, duration: float = 0,
               *, base_name: str = None, timestamp=None, from_idea: str = None) -> str:
    """Write a structured markdown note + raw transcript. Returns the note filepath.

    base_name / timestamp let callers (e.g. reprocess-all) overwrite an existing
    note in place, preserving its filename and date instead of deriving new ones.
    from_idea links this note back to the Someday idea it was developed from.
    """
    notes_dir = CONFIG["output"]["notes_dir"]
    os.makedirs(notes_dir, exist_ok=True)

    if timestamp is None:
        timestamp = _parse_recording_time(audio_path)
    date_str = timestamp.strftime("%Y-%m-%d")
    time_str = timestamp.strftime("%H:%M")

    if base_name is None:
        title_safe = _safe_filename(processed.get("title", "Untitled"))
        base_name = f"{timestamp.strftime('%Y%m%d_%H%M')}_{title_safe}"
        # Avoid clobbering an existing note (same minute + title): append -2, -3, …
        candidate = base_name
        n = 2
        while os.path.exists(os.path.join(notes_dir, f"{candidate}.md")):
            candidate = f"{base_name}-{n}"
            n += 1
        base_name = candidate
    filename = f"{base_name}.md"
    filepath = os.path.join(notes_dir, filename)

    # Save raw transcript alongside the note
    transcripts_dir = os.path.join(os.path.dirname(notes_dir), "transcripts")
    os.makedirs(transcripts_dir, exist_ok=True)
    transcript_path = os.path.join(transcripts_dir, f"{base_name}.txt")
    with open(transcript_path, "w") as f:
        f.write(transcript.get("text", ""))
    # Also save structured transcript data as JSON for reprocessing
    transcript_json_path = os.path.join(transcripts_dir, f"{base_name}.json")
    with open(transcript_json_path, "w") as f:
        json.dump(transcript, f, indent=2, ensure_ascii=False)

    title = processed.get("title", "Untitled")
    lines = []
    lines.append("---")
    lines.append(f"title: {_yaml_str(title)}")
    lines.append(f"date: {date_str}")
    lines.append(f"time: {time_str}")
    meeting_type = processed.get("_meeting_type", "default")
    lines.append(f"type: {meeting_type}")
    category = processed.get("category")
    if category:
        lines.append(f"category: {_yaml_str(str(category))}")
    lines.append(f"transcript: {_yaml_str(os.path.basename(transcript_path))}")
    if duration:
        # duration may arrive as seconds (number) or a preformatted string (reprocess).
        dur_str = duration if isinstance(duration, str) else _format_duration(duration)
        lines.append(f"duration: {_yaml_str(dur_str)}")
    lines.append(f"audio: {_yaml_str(os.path.basename(audio_path))}")
    if from_idea:
        lines.append(f"from_idea: {_yaml_str(from_idea)}")
    lines.append(f"topics: {json.dumps(processed.get('topics', []))}")
    lines.append("---")
    lines.append("")

    # Summary
    lines.append(f"# {processed.get('title', 'Untitled')}")
    lines.append("")
    lines.append(processed.get("summary", ""))
    lines.append("")

    # Render all sections from the processed output dynamically
    _SKIP_KEYS = {"title", "summary", "topics", "category"}

    # Idea-session notes flag their (rare) action items for review rather than
    # dropping them straight into the Action Center — see the someday review queue.
    is_idea_note = processed.get("_meeting_type") == "idea"

    for key, value in processed.items():
        # Skip rendered-elsewhere keys and any internal metadata (keys starting
        # with "_", e.g. _meeting_type, _chunked, _section_count).
        if key in _SKIP_KEYS or key.startswith("_") or not value:
            continue

        heading = key.replace("_", " ").title()

        if key == "action_items" and isinstance(value, list):
            # In an idea session, a to-do is the exception, not the default — tag
            # it pending-review so it lands in the Someday review queue instead of
            # the Action Center. The inline comment rides on the item's own line
            # (parsed by notes_index) and never shifts other line numbers.
            if is_idea_note:
                lines.append("## Possible To-Dos (needs review)")
                review_tag = " <!-- someday: review todo -->"
            else:
                lines.append("## Action Items")
                review_tag = ""
            for item in value:
                if isinstance(item, dict):
                    owner = item.get("owner") or "unassigned"
                    task = item.get("task", "")
                    deadline = item.get("deadline")
                    deadline_str = f" (by {deadline})" if deadline else ""
                    lines.append(f"- [ ] **{owner}**: {task}{deadline_str}{review_tag}")
                elif is_idea_note:
                    lines.append(f"- [ ] {item}{review_tag}")
                else:
                    lines.append(f"- {item}")
            lines.append("")

        elif key == "someday_ideas" and isinstance(value, list):
            # Long-term ideas surfaced from a meeting: flagged for review, not live
            # to-dos. Rendered as checkboxes so notes_index can triage them, but
            # tagged so they stay out of the Action Center buckets and counts.
            lines.append("## Someday / Ideas")
            for idea in value:
                if isinstance(idea, dict):
                    text = idea.get("idea") or idea.get("task") or json.dumps(idea)
                else:
                    text = idea
                lines.append(f"- [ ] {text} <!-- someday: review idea -->")
            lines.append("")

        elif key == "updates" and isinstance(value, list):
            # Standup format
            lines.append("## Updates")
            for update in value:
                if isinstance(update, dict):
                    person = update.get("person", "Unknown")
                    lines.append(f"### {person}")
                    if update.get("yesterday"):
                        lines.append(f"- **Yesterday**: {update['yesterday']}")
                    if update.get("today"):
                        lines.append(f"- **Today**: {update['today']}")
                    if update.get("blockers") and update["blockers"] != "none":
                        lines.append(f"- **Blockers**: {update['blockers']}")
                    lines.append("")
                else:
                    lines.append(f"- {update}")
            lines.append("")

        elif key == "options_discussed" and isinstance(value, list):
            # Strategy format
            lines.append("## Options Discussed")
            for opt in value:
                if isinstance(opt, dict):
                    lines.append(f"### {opt.get('option', 'Option')}")
                    if opt.get("champion"):
                        lines.append(f"*Championed by: {opt['champion']}*")
                    if opt.get("pros"):
                        lines.append("**Pros:**")
                        for p in opt["pros"]:
                            lines.append(f"  - {p}")
                    if opt.get("cons"):
                        lines.append("**Cons:**")
                        for c in opt["cons"]:
                            lines.append(f"  - {c}")
                    lines.append("")
                else:
                    lines.append(f"- {opt}")
            lines.append("")

        elif key == "ideas" and isinstance(value, list):
            # Brainstorm format
            lines.append("## Ideas")
            for idea in value:
                if isinstance(idea, dict):
                    energy = idea.get("energy", "")
                    energy_str = f" [{energy}]" if energy else ""
                    by = idea.get("proposed_by", "")
                    by_str = f" *(by {by})*" if by else ""
                    lines.append(f"- {idea.get('idea', '')}{energy_str}{by_str}")
                else:
                    lines.append(f"- {idea}")
            lines.append("")

        elif key == "sections" and isinstance(value, list):
            # Chunked long-meeting format: per-section recap with time ranges.
            lines.append("## Sections")
            for sec in value:
                if isinstance(sec, dict):
                    title = sec.get("title", "Section")
                    minutes = sec.get("minutes", "")
                    heading_suffix = f" ({minutes} min)" if minutes else ""
                    lines.append(f"### {title}{heading_suffix}")
                    if sec.get("summary"):
                        lines.append(sec["summary"])
                    lines.append("")
                else:
                    lines.append(f"- {sec}")
            lines.append("")

        elif key == "key_responses" and isinstance(value, list):
            # Interview format
            lines.append("## Key Responses")
            for resp in value:
                if isinstance(resp, dict):
                    lines.append(f"**Q: {resp.get('question', '')}**")
                    lines.append(f"- {resp.get('response_summary', '')}")
                    if resp.get("notable"):
                        lines.append(f"- *Notable: {resp['notable']}*")
                    lines.append("")
                else:
                    lines.append(f"- {resp}")
            lines.append("")

        elif isinstance(value, list):
            # Generic list section
            lines.append(f"## {heading}")
            for item in value:
                if isinstance(item, dict):
                    lines.append(f"- {json.dumps(item)}")
                else:
                    lines.append(f"- {item}")
            lines.append("")

        elif isinstance(value, str):
            # Single string section (e.g. "context", "problem_statement", "overall_impression")
            lines.append(f"## {heading}")
            lines.append(value)
            lines.append("")

    # Formatted transcript (collapsed)
    from .cleaner import format_transcript_text
    formatted = format_transcript_text(transcript)
    lines.append("> [!note]- Transcript")
    for line in formatted.splitlines():
        lines.append(f"> {line}" if line else ">")

    content = "\n".join(lines) + "\n"
    with open(filepath, "w") as fh:
        fh.write(content)

    import sys; print(f"[output] Note saved: {filepath}", file=sys.stderr)
    return filepath


def format_slack_message(processed: dict, note_path: str) -> str:
    """Format processed notes as a Slack-friendly message."""
    lines = []
    lines.append(f"*{processed.get('title', 'Voice Note')}*")
    lines.append(f"_{processed.get('summary', '')}_")

    action_items = processed.get("action_items", [])
    if action_items:
        lines.append("")
        lines.append("*Action Items:*")
        for item in action_items:
            if isinstance(item, dict):
                owner = item.get("owner") or "unassigned"
                task = item.get("task", "")
                lines.append(f"  - *{owner}*: {task}")
            else:
                lines.append(f"  - {item}")

    insights = processed.get("insights", [])
    if insights:
        lines.append("")
        lines.append("*Insights:*")
        for i in insights:
            lines.append(f"  - {i}")

    lines.append(f"\n_Full note: `{note_path}`_")
    return "\n".join(lines)
