"""Shared read/parse layer over the notes folder.

Source of truth is the markdown in `notes/` (frontmatter + body). This module
parses notes and action items and supports write-back (checking a to-do edits the
source `.md`). Used by the web front end; the MCP server can be consolidated onto
it later.
"""

import json
import os
import re
import shutil
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta

import yaml

from .config import CONFIG

_CHECKBOX_RE = re.compile(r"^(?P<indent>\s*)- \[(?P<mark>[ xX])\]\s+(?P<text>.*\S)\s*$")
_OWNER_RE = re.compile(r"^\*\*(?P<owner>.+?)\*\*:\s*(?P<rest>.*)$")
_DEADLINE_RE = re.compile(r"\s*\(by (?P<deadline>[^)]+)\)\s*$")
# Per-item metadata (a user note, a merge-group tag) rides on the same line as
# trailing HTML comments, so adding/editing it never shifts line numbers of other
# items (which the write-back relies on). Comments are order-independent.
# Body is "tempered" so it can't contain `-->`, so with several trailing comments
# this matches only the LAST one (peeled off one per loop iteration).
_TRAILING_COMMENT_RE = re.compile(r"\s*<!--\s*(?P<body>(?:(?!-->).)*?)\s*-->\s*$")
_NOTE_BODY_RE = re.compile(r"^note:\s*(?P<note>.*)$", re.I | re.S)
_MERGE_BODY_RE = re.compile(r"^merge:\s*(?P<gid>\S+)(?P<primary>\s+primary)?\s*$", re.I)
# Someday tag: a flagged item awaiting triage ("review", with a suggested kind)
# or a confirmed long-term idea ("parked"). Keeps these out of the Action Center.
_SOMEDAY_BODY_RE = re.compile(
    r"^someday:\s*(?P<state>review|parked)(?:\s+(?P<kind>idea|todo))?\s*$", re.I)


def _parse_item(inner: str) -> dict:
    """Parse the text inside a checkbox into its parts.

    Returns {task, owner, deadline, note, merge_id, merge_primary, someday_state,
    someday_kind}. Peels trailing HTML comments (note / merge / someday, any order)
    first, then the deadline suffix, then the leading owner."""
    text = inner.strip()
    note = None
    merge_id = ""
    merge_primary = False
    someday_state = ""
    someday_kind = ""
    while True:
        m = _TRAILING_COMMENT_RE.search(text)
        if not m:
            break
        body = m.group("body")
        mm = _MERGE_BODY_RE.match(body)
        sm = _SOMEDAY_BODY_RE.match(body)
        nm = _NOTE_BODY_RE.match(body)
        if mm:
            merge_id = mm.group("gid")
            merge_primary = bool(mm.group("primary"))
        elif sm:
            someday_state = sm.group("state").lower()
            someday_kind = (sm.group("kind") or "").lower()
        elif nm:
            note = nm.group("note").strip() or None
        else:
            break  # an unrelated trailing comment — leave it on the task text
        text = text[: m.start()].rstrip()

    deadline = None
    m = _DEADLINE_RE.search(text)
    if m:
        deadline = m.group("deadline").strip()
        text = text[: m.start()].rstrip()

    owner = None
    m = _OWNER_RE.match(text)
    if m:
        owner = m.group("owner").strip()
        text = m.group("rest").strip()

    return {"task": text, "owner": owner, "deadline": deadline,
            "note": note, "merge_id": merge_id, "merge_primary": merge_primary,
            "someday_state": someday_state, "someday_kind": someday_kind}


def _compose_item(task: str, owner: "str | None", deadline: "str | None",
                  note: "str | None" = None, merge_id: str = "",
                  merge_primary: bool = False, someday_state: str = "",
                  someday_kind: str = "") -> str:
    """Rebuild a checkbox's inner text from its parts (inverse of _parse_item)."""
    inner = f"**{owner}**: {task}" if owner else task
    if deadline:
        inner += f" (by {deadline})"
    clean_note = " ".join((note or "").split()).replace("-->", "--").strip()
    if clean_note:
        inner += f" <!-- note: {clean_note} -->"
    if merge_id:
        inner += f" <!-- merge: {merge_id}{' primary' if merge_primary else ''} -->"
    if someday_state:
        # kind is only meaningful while a flagged item is awaiting review.
        kind = f" {someday_kind}" if (someday_state == "review" and someday_kind) else ""
        inner += f" <!-- someday: {someday_state}{kind} -->"
    return inner


def notes_dir() -> str:
    return CONFIG["output"]["notes_dir"]


# ─── owner identity / bucketing ─────────────────────────────────────────────
# The LLM labels the user many different ways (Steven, "Steven (Valentine Home)",
# Steve, "speaker", "me"). We collapse those into one "mine" bucket, route named
# others to "waiting", and unlabeled items to "unassigned".

_UNASSIGNED_TOKENS = {"", "unassigned", "speaker", "unknown", "tbd", "n/a", "na", "none", "?"}


def user_identity() -> list[str]:
    u = CONFIG.get("user", {}) or {}
    raw_aliases = u.get("aliases") or []
    if isinstance(raw_aliases, str):
        raw_aliases = raw_aliases.split(",")  # UI stores aliases as a comma-separated string
    names = [str(u.get("name", "") or "")] + [str(a) for a in raw_aliases]
    return [n.strip() for n in names if n and n.strip()]


def _build_me_matcher():
    names = user_identity()
    if not names:
        return None
    pattern = "|".join(re.escape(n) for n in names)
    return re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE)


# The compiled matcher is cached but keyed on the current identity, so if the
# user edits their name/aliases live (Settings → Identity, which rewrites
# config.yaml) the next lookup rebuilds it instead of serving a stale match.
_ME_MATCHER_KEY = None
_ME_MATCHER = None


def _me_matcher():
    global _ME_MATCHER_KEY, _ME_MATCHER
    key = tuple(user_identity())
    if key != _ME_MATCHER_KEY:
        _ME_MATCHER = _build_me_matcher()
        _ME_MATCHER_KEY = key
    return _ME_MATCHER


_SPEAKER_LABEL_RE = re.compile(r"^speaker[_ ]?\d+$", re.IGNORECASE)


def classify_owner(owner: str | None) -> str:
    """Return 'mine' | 'waiting' | 'unassigned' for an action item owner."""
    o = (owner or "").strip().lower()
    # Unresolved diarization labels (SPEAKER_00) aren't a real person.
    if o in _UNASSIGNED_TOKENS or _SPEAKER_LABEL_RE.match(o):
        return "unassigned"
    matcher = _me_matcher()
    if matcher and matcher.search(owner or ""):
        return "mine"
    return "waiting"


@dataclass
class ActionItem:
    text: str          # human-facing task text (owner/deadline stripped out)
    raw: str           # the full text inside the checkbox, as written
    done: bool
    owner: str | None
    deadline: str | None
    note_filename: str
    note_title: str
    note_date: str
    line_no: int       # 0-based line index in the source file (for write-back)
    bucket: str = "unassigned"   # 'mine' | 'waiting' | 'unassigned' | 'merged' | 'someday'
    note: str = ""     # optional user-added annotation (stored inline in the .md)
    merge_id: str = ""           # shared id when consolidated with other to-dos
    merge_primary: bool = False  # the canonical row of its merge group
    sources: list = field(default_factory=list)  # folded members (set at render)
    someday_state: str = ""      # '' | 'review' (flagged, awaiting triage) | 'parked'
    someday_kind: str = ""       # suggested kind while in review: 'idea' | 'todo'


@dataclass
class Note:
    filename: str
    title: str
    date: str
    time: str
    type: str
    topics: list[str]
    duration: str
    body: str
    category: str = ""   # LLM-inferred kind label (Phone call, Voice note, …)
    meta: dict = field(default_factory=dict)


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body). Tolerant of malformed frontmatter."""
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                return {}, raw
            if isinstance(meta, dict):
                return meta, parts[2].strip()
    return {}, raw


def _time_from_filename(filename: str) -> str:
    m = re.match(r"\d{8}_(\d{4})_", filename)
    return f"{m.group(1)[:2]}:{m.group(1)[2:]}" if m else ""


def load_notes() -> list[Note]:
    """All notes, newest first."""
    d = notes_dir()
    if not os.path.isdir(d):
        return []
    notes: list[Note] = []
    for name in sorted(os.listdir(d), reverse=True):
        if not name.endswith(".md") or name == "CLAUDE.md":
            continue
        path = os.path.join(d, name)
        try:
            raw = open(path, encoding="utf-8").read()
        except OSError:
            continue
        meta, body = _parse_frontmatter(raw)
        topics = meta.get("topics") or []
        if not isinstance(topics, list):
            topics = [str(topics)]
        notes.append(Note(
            filename=name,
            title=str(meta.get("title", name)),
            date=str(meta.get("date", "")),
            time=str(meta.get("time", "") or _time_from_filename(name)),
            type=str(meta.get("type", "")),
            topics=[str(t) for t in topics],
            duration=str(meta.get("duration", "")),
            body=body,
            category=str(meta.get("category", "")),
            meta=meta if isinstance(meta, dict) else {},
        ))
    return notes


def _rewrite_item(filename: str, line_no: int, **changes) -> bool:
    """Parse the action item at filename:line_no, apply field changes (owner / note /
    merge_id / merge_primary), and rewrite the line in place — preserving everything
    else and never shifting line numbers. Returns True on success."""
    d = notes_dir()
    path = os.path.normpath(os.path.join(d, filename))
    if os.path.dirname(path) != os.path.normpath(d) or not os.path.isfile(path):
        return False
    lines = open(path, encoding="utf-8").read().split("\n")
    if line_no < 0 or line_no >= len(lines):
        return False
    m = _CHECKBOX_RE.match(lines[line_no])
    if not m:
        return False
    p = _parse_item(m.group("text"))
    p.update(changes)
    inner = _compose_item(p["task"], p["owner"], p["deadline"],
                          p["note"], p["merge_id"], p["merge_primary"],
                          p["someday_state"], p["someday_kind"])
    lines[line_no] = f"{m.group('indent')}- [{m.group('mark')}] {inner}"
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    return True


def action_items(include_done: bool = False) -> list[ActionItem]:
    """All checkbox action items across notes. Open items only unless include_done."""
    items: list[ActionItem] = []
    d = notes_dir()
    for note in load_notes():
        path = os.path.join(d, note.filename)
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            m = _CHECKBOX_RE.match(line)
            if not m:
                continue
            done = m.group("mark").lower() == "x"
            if done and not include_done:
                continue
            inner = m.group("text")
            p = _parse_item(inner)
            # Someday-tagged items (flagged for review or parked) live in their own
            # bucket — never in the Action Center. Folded (non-primary) merge
            # members are likewise hidden from the normal owner buckets.
            if p["someday_state"]:
                bucket = "someday"
            elif p["merge_id"] and not p["merge_primary"]:
                bucket = "merged"
            else:
                bucket = classify_owner(p["owner"])
            items.append(ActionItem(
                text=p["task"], raw=inner, done=done, owner=p["owner"], deadline=p["deadline"],
                note_filename=note.filename, note_title=note.title,
                note_date=note.date, line_no=i, bucket=bucket,
                note=p["note"] or "", merge_id=p["merge_id"], merge_primary=p["merge_primary"],
                someday_state=p["someday_state"], someday_kind=p["someday_kind"],
            ))
    return items


def toggle_action_item(filename: str, line_no: int, done: bool) -> bool:
    """Set the checkbox on `filename` at `line_no` to done/not-done in the source
    markdown. Returns True on success. Guards against the file having shifted by
    verifying the target line is still a checkbox."""
    d = notes_dir()
    # Containment: filename must resolve to a file directly inside notes_dir.
    path = os.path.normpath(os.path.join(d, filename))
    if os.path.dirname(path) != os.path.normpath(d) or not os.path.isfile(path):
        return False
    text = open(path, encoding="utf-8").read()
    lines = text.split("\n")
    if line_no < 0 or line_no >= len(lines):
        return False
    m = _CHECKBOX_RE.match(lines[line_no])
    if not m:
        return False
    new_mark = "x" if done else " "
    lines[line_no] = f"{m.group('indent')}- [{new_mark}] {m.group('text')}"
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    return True


def user_name() -> str:
    names = user_identity()
    return names[0] if names else "Me"


def known_people() -> list[str]:
    """Distinct named owners that aren't the user (for the reassign dropdown)."""
    people = set()
    for it in action_items(include_done=True):
        if it.bucket == "waiting" and it.owner:
            people.add(it.owner.strip())
    return sorted(people)


def set_owner(filename: str, line_no: int, owner: str) -> bool:
    """Rewrite the owner of the action item at `filename:line_no`, preserving task,
    deadline, note, merge tag, indent, and checkbox state."""
    label = (owner or "").strip() or "unassigned"
    return _rewrite_item(filename, line_no, owner=label)


def set_note(filename: str, line_no: int, note: str) -> bool:
    """Add/replace/remove the inline note on the action item at filename:line_no."""
    return _rewrite_item(filename, line_no, note=note)


def set_note_title(filename: str, title: str) -> bool:
    """Rename a note's *displayed* title in place — the frontmatter `title:` and
    the body's first `# ` heading. The filename is left untouched (the whole app
    keys off it), so only the human-facing title changes. Returns True on success.
    """
    d = notes_dir()
    path = os.path.normpath(os.path.join(d, filename))
    if os.path.dirname(path) != os.path.normpath(d) or not os.path.isfile(path):
        return False
    new_title = " ".join((title or "").split()).strip()
    if not new_title:
        return False
    quoted = json.dumps(new_title, ensure_ascii=False)  # a valid single-line YAML scalar
    lines = open(path, encoding="utf-8").read().split("\n")
    title_done = h1_done = False
    fences = 0  # 0 = pre-frontmatter, 1 = inside it, 2+ = body
    for i, line in enumerate(lines):
        if line.rstrip() == "---":
            fences += 1
            continue
        if not title_done and fences == 1 and re.match(r"^title:\s", line):
            lines[i] = f"title: {quoted}"
            title_done = True
        elif not h1_done and fences >= 2 and re.match(r"^#\s+\S", line):
            lines[i] = f"# {new_title}"
            h1_done = True
    if not (title_done or h1_done):
        return False
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    return True


def set_merge(filename: str, line_no: int, merge_id: str, primary: bool = False) -> bool:
    """Tag the action item at filename:line_no as part of merge group `merge_id`
    (the `primary` one is the canonical/displayed row)."""
    return _rewrite_item(filename, line_no, merge_id=merge_id, merge_primary=primary)


def clear_merge(filename: str, line_no: int) -> bool:
    """Remove any merge tag from the action item at filename:line_no (unmerge)."""
    return _rewrite_item(filename, line_no, merge_id="", merge_primary=False)


def delete_note(filename: str) -> bool:
    """Soft-delete a note: move its .md into notes/.trash/ (recoverable, and hidden
    from both Scribe and Obsidian since dot-folders are ignored). Returns True on
    success.

    Before moving, un-merge any consolidated to-do groups this note took part in —
    otherwise a merged item in *another* note could be left folded with no primary
    to display it, making it silently vanish from the Action Center. The note's own
    transcript and audio are left in place (orphaned, governed by retention).
    """
    d = notes_dir()
    path = os.path.normpath(os.path.join(d, filename))
    if os.path.dirname(path) != os.path.normpath(d) or not os.path.isfile(path):
        return False

    gids = {it.merge_id for it in action_items(include_done=True)
            if it.note_filename == filename and it.merge_id}
    if gids:
        for it in action_items(include_done=True):
            if it.merge_id in gids and it.note_filename != filename:
                clear_merge(it.note_filename, it.line_no)

    trash = os.path.join(d, ".trash")
    os.makedirs(trash, exist_ok=True)
    dest = os.path.join(trash, filename)
    n = 2
    while os.path.exists(dest):  # never clobber a previously-trashed note
        stem, ext = os.path.splitext(filename)
        dest = os.path.join(trash, f"{stem}-{n}{ext}")
        n += 1
    shutil.move(path, dest)
    return True


def _parse_note_date(value) -> "date | None":
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def recent_notes(days: int = 14) -> list[Note]:
    """Notes whose date is within the last `days` (newest first)."""
    today = date.today()
    out = []
    for n in load_notes():
        d = _parse_note_date(n.date)
        if d and 0 <= (today - d).days <= days:
            out.append(n)
    return out


def items_with_deadlines(include_done: bool = False) -> list[ActionItem]:
    """Action items that carry a deadline string. Someday items (not yet committed)
    are excluded so unconfirmed ideas don't surface on the deadline radar."""
    return [it for it in action_items(include_done=include_done)
            if it.deadline and it.bucket != "someday"]


# ─── deadline date parsing ──────────────────────────────────────────────────
# Deadlines are freeform LLM text ("by Sept 1", "next week", "Phase 2"). Parse
# the ones that are actual dates (relative to the meeting date) so the brief can
# sort by urgency; leave vague ones (project phases, conditions) undated.

_DEADLINE_PREFIX = re.compile(
    r'^(by|before|due(?:\s+by|\s+on)?|no later than|around|on|starting|'
    r'the week of|week of|end of|eod|cob)\s+', re.I)
_deadline_cache: dict = {}


def parse_deadline(text: str | None, base_date: "date | None" = None) -> "date | None":
    """Best-effort parse of a freeform deadline into a date, relative to base_date
    (the meeting date). Returns None for non-date strings. Memoized."""
    if not text:
        return None
    key = (text, base_date.isoformat() if base_date else "")
    if key in _deadline_cache:
        return _deadline_cache[key]
    cleaned = _DEADLINE_PREFIX.sub('', text.strip()).strip()
    result = None
    if cleaned:
        try:
            import dateparser
            settings = {'PREFER_DATES_FROM': 'future', 'REQUIRE_PARTS': ['day', 'month']}
            if base_date:
                settings['RELATIVE_BASE'] = datetime(base_date.year, base_date.month, base_date.day)
            dt = dateparser.parse(cleaned, settings=settings)
            result = dt.date() if dt else None
        except Exception:
            result = None
    _deadline_cache[key] = result
    return result


def _relative_label(due: "date", today: "date") -> str:
    delta = (due - today).days
    if delta < 0:
        return "overdue" if delta == -1 else f"{-delta}d overdue"
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta <= 7:
        return f"in {delta} days"
    return due.strftime("%b %-d")


def deadline_view(items: list[ActionItem], today: "date | None" = None) -> list[dict]:
    """Annotate action items with a parsed due date + urgency status, sorted
    soonest/overdue first, undated last. status ∈ overdue|soon|later|nodate."""
    today = today or date.today()
    soon = today + timedelta(days=7)
    rows = []
    for it in items:
        due = parse_deadline(it.deadline, _parse_note_date(it.note_date))
        if due:
            status = "overdue" if due < today else ("soon" if due <= soon else "later")
            when = _relative_label(due, today)
        else:
            status, when = "nodate", it.deadline
        rows.append({"item": it, "due": due, "status": status, "when": when})
    rows.sort(key=lambda r: (r["due"] is None, r["due"] or date.max))
    return rows


def stats() -> dict:
    notes = load_notes()
    items = action_items(include_done=True)
    # Folded merge members aren't counted — a consolidated group counts once, via
    # its primary. Someday items (review/parked) are their own world, never the
    # Action Center, so they're excluded from the open/done tallies too.
    actionable = [it for it in items if it.bucket not in ("merged", "someday")]
    open_items = [it for it in actionable if not it.done]
    counts = {"mine": 0, "waiting": 0, "unassigned": 0}
    for it in open_items:
        counts[it.bucket] = counts.get(it.bucket, 0) + 1
    review = sum(1 for it in items
                 if it.bucket == "someday" and it.someday_state == "review" and not it.done)
    return {
        "note_count": len(notes),
        "open_action_items": len(open_items),
        "buckets": counts,
        "done": len(actionable) - len(open_items),
        "someday_review": review,
    }


def someday_items(state: str = "", include_done: bool = False) -> list[ActionItem]:
    """Items in the someday bucket — flagged review candidates and parked ideas.
    Filter to a single state ('review' | 'parked') when given. Newest note first."""
    out = [it for it in action_items(include_done=include_done) if it.bucket == "someday"]
    if state:
        out = [it for it in out if it.someday_state == state]
    return out


def idea_notes(include_archived: bool = False) -> list[Note]:
    """Dedicated idea-session notes (frontmatter type == 'idea'). Archived ideas
    are hidden from Someday by default but remain in All Notes."""
    out = [n for n in load_notes() if (n.type or "").lower() == "idea"]
    if not include_archived:
        out = [n for n in out if not n.meta.get("archived")]
    return out


def note_by_filename(filename: str) -> "Note | None":
    for n in load_notes():
        if n.filename == filename:
            return n
    return None


def developed_sessions(idea_filename: str) -> list[Note]:
    """Notes recorded by developing the given idea (frontmatter from_idea → it)."""
    return [n for n in load_notes() if str(n.meta.get("from_idea") or "") == idea_filename]


def set_category(filename: str, category: str) -> bool:
    """Add/replace the `category` frontmatter line on a note (nothing else touched).
    Non-destructive — preserves the body, checkmarks, merges, and inline notes."""
    d = notes_dir()
    path = os.path.normpath(os.path.join(d, filename))
    if os.path.dirname(path) != os.path.normpath(d) or not os.path.isfile(path):
        return False
    cat = " ".join((category or "").split()).strip()
    if not cat:
        return False
    quoted = json.dumps(cat, ensure_ascii=False)
    lines = open(path, encoding="utf-8").read().split("\n")
    if not lines or lines[0].strip() != "---":
        return False
    out, fences, done = [], 0, False
    for line in lines:
        if line.rstrip() == "---":
            fences += 1
            if fences == 2 and not done:
                out.append(f"category: {quoted}")   # no existing line — add before close
                done = True
            out.append(line)
            continue
        if fences == 1 and not done and re.match(r"^category:\s", line):
            out.append(f"category: {quoted}")        # replace existing
            done = True
            continue
        out.append(line)
    open(path, "w", encoding="utf-8").write("\n".join(out))
    return True


def set_archived(filename: str, archived: bool) -> bool:
    """Set/unset the `archived` frontmatter flag on a note. Archiving an idea
    removes it from Someday while leaving it in All Notes; restoring brings it back.
    """
    d = notes_dir()
    path = os.path.normpath(os.path.join(d, filename))
    if os.path.dirname(path) != os.path.normpath(d) or not os.path.isfile(path):
        return False
    lines = open(path, encoding="utf-8").read().split("\n")
    if not lines or lines[0].strip() != "---":
        return False
    out, fences, done = [], 0, False
    for line in lines:
        if line.rstrip() == "---":
            fences += 1
            if fences == 2 and archived and not done:
                out.append("archived: true")   # add just before the closing fence
                done = True
            out.append(line)
            continue
        if fences == 1 and re.match(r"^archived:\s", line):
            continue  # drop any existing flag (re-added above if still archiving)
        out.append(line)
    open(path, "w", encoding="utf-8").write("\n".join(out))
    return True


def confirm_idea(filename: str, line_no: int) -> bool:
    """Triage: keep a flagged item as a parked Someday idea (review -> parked)."""
    return _rewrite_item(filename, line_no, someday_state="parked", someday_kind="")


def confirm_todo(filename: str, line_no: int, owner: str = "", deadline: str = "") -> bool:
    """Triage: promote a flagged item into a live Action Center to-do.

    Strips the someday tag (so it re-buckets by owner) and optionally sets the
    owner and deadline the user supplied at convert time.
    """
    changes = {"someday_state": "", "someday_kind": ""}
    label = (owner or "").strip()
    if label:
        changes["owner"] = label
    dl = (deadline or "").strip()
    if dl:
        changes["deadline"] = dl
    return _rewrite_item(filename, line_no, **changes)


def dismiss_item(filename: str, line_no: int) -> bool:
    """Triage: delete a flagged Someday item's line from its note (declined).

    Removing the line shifts the line numbers of any later items in that file, so
    callers must re-read items afterward — the dashboard re-renders the whole list,
    so cached line_no values are never reused across a dismiss.
    """
    d = notes_dir()
    path = os.path.normpath(os.path.join(d, filename))
    if os.path.dirname(path) != os.path.normpath(d) or not os.path.isfile(path):
        return False
    lines = open(path, encoding="utf-8").read().split("\n")
    if line_no < 0 or line_no >= len(lines):
        return False
    if not _CHECKBOX_RE.match(lines[line_no]):
        return False
    del lines[line_no]
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    return True


def to_dict(obj) -> dict:
    return asdict(obj)
