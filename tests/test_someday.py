"""Tests for the Someday feature: the inline review/parked tag, idea-vs-todo
rendering in output.py, the someday bucket, and the triage operations."""

import os

import pytest

from src import notes_index as ni
from src.output import write_note
from src.config import CONFIG


# ─── inline tag parse / compose ──────────────────────────────────────────────

def _recompose(p: dict) -> str:
    return ni._compose_item(p["task"], p["owner"], p["deadline"], p["note"],
                            p["merge_id"], p["merge_primary"],
                            p["someday_state"], p["someday_kind"])


def test_review_idea_tag_roundtrips():
    raw = "Build a homeowner scheduling app <!-- someday: review idea -->"
    p = ni._parse_item(raw)
    assert p["task"] == "Build a homeowner scheduling app"
    assert p["someday_state"] == "review"
    assert p["someday_kind"] == "idea"
    assert _recompose(p) == raw


def test_review_todo_tag_coexists_with_owner_and_deadline():
    raw = "**Steven**: grab the domain (by Friday) <!-- someday: review todo -->"
    p = ni._parse_item(raw)
    assert p["owner"] == "Steven"
    assert p["deadline"] == "Friday"
    assert p["task"] == "grab the domain"
    assert p["someday_state"] == "review"
    assert p["someday_kind"] == "todo"
    assert _recompose(p) == raw


def test_parked_tag_drops_kind():
    p = ni._parse_item("A long-term idea <!-- someday: parked -->")
    assert p["someday_state"] == "parked"
    back = _recompose(p)
    assert "<!-- someday: parked -->" in back
    assert "parked idea" not in back  # kind is only emitted while in review


def test_someday_coexists_with_note_and_merge_tags():
    raw = ("Ship the thing <!-- note: blocked on legal --> "
           "<!-- someday: review idea -->")
    p = ni._parse_item(raw)
    assert p["note"] == "blocked on legal"
    assert p["someday_state"] == "review"
    assert p["task"] == "Ship the thing"


# ─── rendering + bucketing (filesystem) ──────────────────────────────────────

@pytest.fixture
def notes_env(tmp_path, monkeypatch):
    """Point the notes dir at a temp folder so write_note / notes_index operate
    in isolation."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    monkeypatch.setitem(CONFIG["output"], "notes_dir", str(notes_dir))
    return notes_dir


def _write_meeting_with_idea(idea_text: str, audio="recording_20260103_100000.wav"):
    processed = {
        "title": "Planning Meeting",
        "summary": "We discussed things.",
        "someday_ideas": [idea_text],
        "_meeting_type": "default",
    }
    transcript = {"text": "transcript body", "segments": [], "language": "en"}
    return write_note(processed, transcript, audio_path=audio, duration=0)


def test_idea_note_flags_action_items_for_review(notes_env):
    me = ni.user_name()
    processed = {
        "title": "Homeowner App",
        "summary": "An app idea for homeowners.",
        "premise": "Homeowners struggle to schedule maintenance.",
        "shape": ["scheduling", "reminders"],
        "action_items": [{"owner": me, "task": "grab the domain", "deadline": "Friday"}],
        "_meeting_type": "idea",
    }
    transcript = {"text": "blah", "segments": [], "language": "en"}
    path = write_note(processed, transcript, "recording_20260101_090000.wav", 0)
    content = open(path, encoding="utf-8").read()

    # The idea's to-do is flagged, not live; idea body fields still render.
    assert "## Possible To-Dos (needs review)" in content
    assert "<!-- someday: review todo -->" in content
    assert "## Premise" in content
    assert "## Shape" in content

    items = ni.action_items(include_done=True)
    todo = next(it for it in items if "grab the domain" in it.text)
    assert todo.bucket == "someday"
    assert todo.someday_state == "review"
    assert todo.someday_kind == "todo"

    s = ni.stats()
    assert s["open_action_items"] == 0   # nothing reached the Action Center
    assert s["someday_review"] == 1


def test_meeting_flags_someday_idea_but_keeps_live_todo(notes_env):
    me = ni.user_name()
    processed = {
        "title": "Strategy Call",
        "summary": "We talked strategy.",
        "action_items": [{"owner": me, "task": "send the deck", "deadline": None}],
        "someday_ideas": ["Build a homeowner scheduling app someday"],
        "_meeting_type": "default",
    }
    transcript = {"text": "blah", "segments": [], "language": "en"}
    path = write_note(processed, transcript, "recording_20260102_090000.wav", 0)
    content = open(path, encoding="utf-8").read()

    assert "## Action Items" in content
    assert "## Someday / Ideas" in content
    assert "<!-- someday: review idea -->" in content
    # The live to-do line carries no someday tag.
    deck_line = next(l for l in content.splitlines() if "send the deck" in l)
    assert "someday:" not in deck_line

    items = ni.action_items()
    deck = next(it for it in items if "send the deck" in it.text)
    idea = next(it for it in items if "homeowner scheduling" in it.text)
    assert deck.bucket == "mine"
    assert idea.bucket == "someday" and idea.someday_state == "review"

    s = ni.stats()
    assert s["open_action_items"] == 1   # only the real to-do
    assert s["someday_review"] == 1


# ─── triage operations ───────────────────────────────────────────────────────

def test_confirm_idea_parks_the_item(notes_env):
    _write_meeting_with_idea("Explore a franchising model")
    review = ni.someday_items(state="review")
    assert len(review) == 1
    it = review[0]

    assert ni.confirm_idea(it.note_filename, it.line_no)
    assert ni.someday_items(state="review") == []
    parked = ni.someday_items(state="parked")
    assert len(parked) == 1
    assert parked[0].someday_state == "parked"
    # still outside the Action Center
    assert ni.stats()["open_action_items"] == 0


def test_identity_change_rebuilds_mine_matcher_live():
    """classify_owner reflects a live identity edit (Settings → Identity rewrites
    config.yaml) without reimporting notes_index — the matcher is keyed on the
    current identity, not frozen at import."""
    original = CONFIG.get("user")
    try:
        CONFIG["user"] = {"name": "Alice", "aliases": ""}
        assert ni.classify_owner("Alice") == "mine"
        assert ni.classify_owner("Bob") == "waiting"

        # User renames themselves (and adds a nickname) while the server runs.
        CONFIG["user"] = {"name": "Bob", "aliases": "Bobby"}
        assert ni.classify_owner("Bob") == "mine"
        assert ni.classify_owner("Bobby") == "mine"
        assert ni.classify_owner("Alice") == "waiting"
    finally:
        if original is None:
            CONFIG.pop("user", None)
        else:
            CONFIG["user"] = original


def test_confirm_todo_promotes_to_action_center(notes_env):
    _write_meeting_with_idea("Grab the premium domain")
    me = ni.user_name()
    it = ni.someday_items(state="review")[0]

    assert ni.confirm_todo(it.note_filename, it.line_no, owner=me, deadline="next Friday")
    assert ni.someday_items() == []            # left the someday world entirely

    promoted = next(x for x in ni.action_items() if "Grab the premium domain" in x.text)
    assert promoted.bucket == "mine"
    assert promoted.deadline == "next Friday"
    assert promoted.someday_state == ""
    assert ni.stats()["open_action_items"] == 1


def test_dismiss_removes_the_line(notes_env):
    path = _write_meeting_with_idea("A throwaway thought")
    it = ni.someday_items(state="review")[0]

    assert ni.dismiss_item(it.note_filename, it.line_no)
    assert ni.someday_items() == []
    assert "A throwaway thought" not in open(path, encoding="utf-8").read()


def test_dismiss_rejects_non_checkbox_line(notes_env):
    path = _write_meeting_with_idea("Some idea")
    # line 0 is frontmatter, never a checkbox
    assert ni.dismiss_item(path.split("/")[-1], 0) is False


# ─── title rename ────────────────────────────────────────────────────────────

def test_set_note_title_updates_frontmatter_and_h1_only(notes_env):
    processed = {"title": "Homeowner App", "summary": "An idea.", "_meeting_type": "idea"}
    transcript = {"text": "t", "segments": [], "language": "en"}
    path = write_note(processed, transcript, "recording_20260101_090000.wav", 0)
    filename = path.split("/")[-1]

    assert ni.set_note_title(filename, "  Homeowner Maintenance Scheduler  ")  # trims
    raw = open(path, encoding="utf-8").read()
    assert 'title: "Homeowner Maintenance Scheduler"' in raw
    assert "# Homeowner Maintenance Scheduler" in raw
    # filename is the stable key — it must NOT change
    assert os.path.exists(path)

    note = next(n for n in ni.load_notes() if n.filename == filename)
    assert note.title == "Homeowner Maintenance Scheduler"


def test_set_note_title_rejects_blank_and_bad_path(notes_env):
    path = write_note({"title": "X", "summary": "s", "_meeting_type": "default"},
                      {"text": "t", "segments": [], "language": "en"},
                      "recording_20260101_090000.wav", 0)
    filename = path.split("/")[-1]
    assert ni.set_note_title(filename, "   ") is False
    assert ni.set_note_title("../escape.md", "Nope") is False


# ─── note deletion ───────────────────────────────────────────────────────────

def _write_simple_note(title, audio, task=None):
    me = ni.user_name()
    processed = {"title": title, "summary": "s", "_meeting_type": "default"}
    if task:
        processed["action_items"] = [{"owner": me, "task": task, "deadline": None}]
    return write_note(processed, {"text": "t", "segments": [], "language": "en"}, audio, 0)


def test_delete_note_moves_to_trash(notes_env):
    path = _write_simple_note("Throwaway", "recording_20260101_090000.wav")
    filename = path.split("/")[-1]

    assert ni.delete_note(filename)
    assert filename not in [n.filename for n in ni.load_notes()]
    assert os.path.exists(os.path.join(str(notes_env), ".trash", filename))
    assert not os.path.exists(path)


def test_delete_note_rejects_path_traversal(notes_env):
    assert ni.delete_note("../escape.md") is False


def test_category_stamped_and_parsed_not_rendered_in_body(notes_env):
    path = write_note({"title": "Bank Call", "summary": "s", "category": "Phone call",
                       "_meeting_type": "default"},
                      {"text": "t", "segments": [], "language": "en"},
                      "recording_20260101_090000.wav", 0)
    raw = open(path, encoding="utf-8").read()
    assert 'category: "Phone call"' in raw
    assert "## Category" not in raw           # it's frontmatter, not a body section
    note = next(n for n in ni.load_notes() if n.filename == path.split("/")[-1])
    assert note.category == "Phone call"


def test_write_note_stamps_from_idea(notes_env):
    path = write_note({"title": "Kickoff", "summary": "s", "_meeting_type": "default"},
                      {"text": "t", "segments": [], "language": "en"},
                      "recording_20260102_100000.wav", 0, from_idea="20260101_0900_Idea.md")
    assert 'from_idea: "20260101_0900_Idea.md"' in open(path, encoding="utf-8").read()


def test_developed_sessions_links_idea_to_session(notes_env):
    idea = write_note({"title": "Idea", "summary": "s", "_meeting_type": "idea"},
                      {"text": "t", "segments": [], "language": "en"},
                      "recording_20260101_090000.wav", 0)
    idea_fn = idea.split("/")[-1]
    sess = write_note({"title": "Session", "summary": "s", "_meeting_type": "default"},
                      {"text": "t", "segments": [], "language": "en"},
                      "recording_20260102_100000.wav", 0, from_idea=idea_fn)
    sess_fn = sess.split("/")[-1]
    assert [n.filename for n in ni.developed_sessions(idea_fn)] == [sess_fn]
    assert ni.note_by_filename(sess_fn).meta.get("from_idea") == idea_fn


def test_archive_hides_from_someday_keeps_in_all_notes(notes_env):
    idea = write_note({"title": "Shelved", "summary": "s", "_meeting_type": "idea"},
                      {"text": "t", "segments": [], "language": "en"},
                      "recording_20260101_090000.wav", 0)
    fn = idea.split("/")[-1]
    assert len(ni.idea_notes()) == 1

    assert ni.set_archived(fn, True)
    assert ni.idea_notes() == []                                   # gone from Someday
    assert fn in [n.filename for n in ni.load_notes()]             # still in All Notes
    assert len(ni.idea_notes(include_archived=True)) == 1

    assert ni.set_archived(fn, False)                              # restore
    assert len(ni.idea_notes()) == 1


def test_delete_note_unmerges_groups_so_other_items_survive(notes_env):
    a = _write_simple_note("Note A", "recording_20260101_090000.wav", task="shared A")
    b = _write_simple_note("Note B", "recording_20260102_090000.wav", task="shared B")
    fa, fb = a.split("/")[-1], b.split("/")[-1]
    ia = next(it for it in ni.action_items() if it.note_filename == fa)
    ib = next(it for it in ni.action_items() if it.note_filename == fb)
    ni.set_merge(fa, ia.line_no, "g1", primary=True)
    ni.set_merge(fb, ib.line_no, "g1", primary=False)
    assert next(it for it in ni.action_items() if it.note_filename == fb).bucket == "merged"

    # Deleting the note holding the primary must not orphan B's folded item.
    assert ni.delete_note(fa)
    b_item = next(it for it in ni.action_items() if it.note_filename == fb)
    assert b_item.bucket == "mine"
    assert b_item.merge_id == ""
