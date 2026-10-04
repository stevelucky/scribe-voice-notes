"""Tests for multi-provider LLM dispatch and processing logic."""
import json
from unittest.mock import MagicMock, patch
import pytest


# ─── _call_llm provider dispatch ────────────────────────────────────────────
# SDKs are imported lazily inside _call_llm, so patch at the import source.

def test_call_llm_gemini_dispatches_genai():
    """_call_llm with gemini provider calls google.genai."""
    from src.processor import _call_llm

    mock_response = MagicMock()
    mock_response.text = json.dumps({"title": "Test"})
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    with patch("google.genai.Client", return_value=mock_client):
        result = _call_llm("gemini", "gemini-flash", "sys prompt", "user prompt", 0.3)

    assert result == json.dumps({"title": "Test"})
    mock_client.models.generate_content.assert_called_once()


def test_call_llm_openai_dispatches_chat_completions():
    """_call_llm with openai provider calls openai.chat.completions.create."""
    from src.processor import _call_llm

    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({"title": "OpenAI Test"})
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("openai.OpenAI", return_value=mock_client):
        result = _call_llm("openai", "gpt-4o", "sys prompt", "user prompt", 0.3)

    assert result == json.dumps({"title": "OpenAI Test"})
    mock_client.chat.completions.create.assert_called_once()


def test_call_llm_anthropic_dispatches_messages():
    """_call_llm with anthropic provider calls anthropic.messages.create."""
    from src.processor import _call_llm

    mock_response = MagicMock()
    mock_response.content[0].text = json.dumps({"title": "Anthropic Test"})
    mock_response.stop_reason = "end_turn"
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("anthropic.Anthropic", return_value=mock_client):
        result = _call_llm("anthropic", "claude-opus-4-6", "sys prompt", "user prompt", 0.3)

    assert result == json.dumps({"title": "Anthropic Test"})
    mock_client.messages.create.assert_called_once()


def test_call_llm_unknown_provider_raises():
    """_call_llm raises ValueError for unknown provider."""
    from src.processor import _call_llm

    with pytest.raises(ValueError, match="Unknown LLM provider"):
        _call_llm("unknown_provider", "some-model", "sys", "prompt", 0.3)


# ─── _parse_json_response ────────────────────────────────────────────────────

def test_parse_json_plain():
    from src.processor import Processor
    assert Processor._parse_json_response('{"a": 1}') == {"a": 1}


def test_parse_json_strips_markdown_fences():
    from src.processor import Processor
    fenced = "```json\n{\"a\": 1}\n```"
    assert Processor._parse_json_response(fenced) == {"a": 1}


def test_parse_json_recovers_from_surrounding_prose():
    from src.processor import Processor
    messy = 'Sure! Here is the note:\n{"a": 1}\nHope that helps.'
    assert Processor._parse_json_response(messy) == {"a": 1}


def test_parse_json_raises_on_garbage():
    from src.processor import Processor
    with pytest.raises(json.JSONDecodeError):
        Processor._parse_json_response("not json at all")


# ─── _split_segments_by_time ─────────────────────────────────────────────────

def test_split_segments_by_time_groups_by_boundary():
    from src.processor import Processor
    segments = [
        {"start": 0, "end": 10, "text": "a"},
        {"start": 50, "end": 60, "text": "b"},
        {"start": 130, "end": 140, "text": "c"},
    ]
    chunks = Processor._split_segments_by_time(segments, chunk_seconds=60)
    # First chunk: 0-60s window (a, b); second chunk: 120-180s window (c).
    assert len(chunks) == 2
    assert [s["text"] for s in chunks[0]] == ["a", "b"]
    assert [s["text"] for s in chunks[1]] == ["c"]


def test_split_segments_empty():
    from src.processor import Processor
    assert Processor._split_segments_by_time([], 60) == []


# ─── process / process_chunked ───────────────────────────────────────────────

def _make_processor():
    from src.processor import Processor
    return Processor(meeting_type="default")


def test_process_parses_and_tags_meeting_type():
    p = _make_processor()
    with patch("src.processor._call_llm", return_value='{"title": "T", "summary": "S"}'):
        result = p.process("some transcript")
    assert result["title"] == "T"
    assert result["_meeting_type"] == "default"


def test_process_falls_back_on_bad_json():
    p = _make_processor()
    with patch("src.processor._call_llm", return_value="totally not json"):
        result = p.process("some transcript")
    assert result["title"] == "Untitled Recording"
    assert result["summary"] == "totally not json"
    assert result["_meeting_type"] == "default"


def test_process_chunked_uses_module_level_call_llm():
    """Regression: process_chunked must dispatch via the module-level _call_llm,
    return a dict with title/summary, and tag _meeting_type — not crash on a
    nonexistent self._call_llm."""
    p = _make_processor()
    transcript = {
        "text": "full text",
        "segments": [
            {"start": 0, "end": 60, "text": "first chunk content"},
            {"start": 1900, "end": 1960, "text": "second chunk content"},
            {"start": 3800, "end": 3860, "text": "third chunk content"},
        ],
    }

    section_json = json.dumps({
        "section_title": "Sec", "summary": "did things",
        "decisions": [], "action_items": [], "key_points": [], "open_threads": [],
    })
    synthesis_json = json.dumps({
        "title": "Whole Meeting", "summary": "the whole thing",
        "sections": [], "decisions": [], "action_items": [],
        "key_points": [], "open_threads": [],
    })

    # Three time-based sections → three section calls + one synthesis call.
    responses = [section_json, section_json, section_json, synthesis_json]
    with patch("src.processor._call_llm", side_effect=responses):
        result = p.process_chunked(transcript, chunk_minutes=30)

    assert result["title"] == "Whole Meeting"
    assert result["summary"] == "the whole thing"
    assert result["_meeting_type"] == "default"
    assert result["_chunked"] is True


# ─── classify_recording (auto-type) ──────────────────────────────────────────

def test_classify_recording_detects_idea():
    from src.processor import classify_recording
    with patch("src.processor._call_llm", return_value='{"type": "idea"}'):
        assert classify_recording("what if we built an app someday") == "idea"


def test_classify_recording_detects_meeting():
    from src.processor import classify_recording
    with patch("src.processor._call_llm", return_value='{"type": "meeting"}'):
        assert classify_recording("standup notes, blockers, etc.") == "meeting"


def test_classify_recording_defaults_to_meeting_on_error():
    from src.processor import classify_recording
    with patch("src.processor._call_llm", side_effect=RuntimeError("boom")):
        assert classify_recording("anything") == "meeting"


def test_classify_recording_defaults_to_meeting_on_unexpected_json():
    from src.processor import classify_recording
    with patch("src.processor._call_llm", return_value='{"type": "banana"}'):
        assert classify_recording("anything") == "meeting"


def test_classify_recording_empty_is_meeting_without_llm_call():
    from src.processor import classify_recording
    with patch("src.processor._call_llm") as m:
        assert classify_recording("   ") == "meeting"
    m.assert_not_called()


def test_process_chunked_single_chunk_falls_back_to_single_pass():
    p = _make_processor()
    transcript = {
        "text": "short text",
        "segments": [{"start": 0, "end": 30, "text": "short"}],
    }
    with patch("src.processor._call_llm", return_value='{"title": "Short", "summary": "S"}') as m:
        result = p.process_chunked(transcript, chunk_minutes=30)
    # One pass only, routed through process().
    assert result["title"] == "Short"
    assert m.call_count == 1
