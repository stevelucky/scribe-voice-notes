"""LLM-based processing of transcripts into structured meeting notes.

Loads prompt templates from prompts.json. Meeting type selectable via --type flag.
Supports multiple LLM providers: gemini, openai, anthropic — set via config.yaml.
"""

import json
import os
import re
import sys
from datetime import datetime

from .config import CONFIG

# Cap on LLM output tokens. Long meeting notes can be sizeable, so this is set
# well above the typical note length to avoid truncated (invalid) JSON.
_MAX_OUTPUT_TOKENS = 8192

_CHUNK_SECTION_PROMPT = """You are summarizing one section of a longer meeting transcript.

This is section {section_num} of {total_sections}, covering minutes {start_min}-{end_min} of the meeting.

Return JSON with this exact shape:
{{
  "section_title": "<short title describing what this section was about>",
  "summary": "<2-4 sentence summary of what happened in this section>",
  "decisions": ["<decision made in this section>", ...],
  "action_items": [{{"task": "<what>", "owner": "<who, or null>", "deadline": "<when, or null>"}}, ...],
  "key_points": ["<important point discussed>", ...],
  "open_threads": ["<unresolved question or topic to revisit>", ...]
}}

Rules:
- Only include items actually present in this section. Empty arrays are fine.
- Do not invent owners or dates. Use null when not stated.
- Be concise. No filler."""

_CHUNK_SYNTHESIS_PROMPT = """You are synthesizing a long meeting from per-section summaries.

You will receive a JSON array of section summaries. Produce a single final note in JSON with this shape:
{{
  "title": "<short descriptive title for the whole meeting>",
  "summary": "<3-5 sentence summary of the whole meeting>",
  "sections": [
    {{"title": "<section title>", "summary": "<the section's summary>", "minutes": "<start-end>"}}, ...
  ],
  "decisions": ["<deduplicated decision across the whole meeting>", ...],
  "action_items": [{{"task": "<what>", "owner": "<who or null>", "deadline": "<when or null>"}}, ...],
  "key_points": ["<deduplicated key point>", ...],
  "open_threads": ["<deduplicated unresolved item>", ...]
}}

Rules:
- Deduplicate aggressively. If section 1 and section 3 both flag the same action item, list it once.
- Preserve the chronological order of sections.
- The summary should read as one coherent narrative, not a list of section recaps.
- Do not invent items. Only consolidate what's already in the section summaries."""



_SPEAKER_ATTRIBUTION_INSTRUCTION = """SPEAKER ATTRIBUTION:
The transcript includes anonymous speaker labels (Speaker_00, Speaker_01, etc.).
If you can confidently infer a real name or role from the conversation — self-introductions ("I'm Mark from sales"), direct address by another speaker ("Mark, can you handle that?"), or distinctive role context ("as the controller, I..."), use the inferred name when attributing action items, decisions, or quotes in your output JSON.

If the user has provided meeting context naming the attendees, prefer those names and try to map each Speaker_NN to one of them based on the conversation.

If you cannot infer a name with high confidence, leave the label as Speaker_NN or use null in the owner field. NEVER GUESS names. Wrong attribution is worse than no attribution."""

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROMPTS_PATH = os.path.join(_BASE_DIR, "prompts.json")


def load_prompts() -> dict:
    with open(_PROMPTS_PATH) as f:
        prompts = json.load(f)
    prompts.pop("_description", None)
    return prompts


def available_types() -> dict:
    """Return dict of type_key -> {name, description}."""
    prompts = load_prompts()
    return {k: {"name": v["name"], "description": v["description"]} for k, v in prompts.items()}


def _call_llm(provider: str, model: str, system_prompt: str, prompt: str, temperature: float) -> str:
    """Dispatch an LLM call to the configured provider. Returns raw response text.

    Provider SDKs are imported lazily so only the configured provider's package
    needs to be installed.
    """
    # Auto-prepend speaker attribution when transcript has Speaker_NN labels
    if re.search(r"Speaker_\d+", prompt):
        system_prompt = _SPEAKER_ATTRIBUTION_INSTRUCTION + "\n\n" + system_prompt

    if provider == "gemini":
        from google import genai
        from google.genai import types as genai_types

        client = genai.Client()
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=temperature,
                response_mime_type="application/json",
                max_output_tokens=_MAX_OUTPUT_TOKENS,
            ),
        )
        return response.text

    elif provider == "openai":
        from openai import OpenAI

        client = OpenAI()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            response_format={"type": "json_object"},
            max_tokens=_MAX_OUTPUT_TOKENS,
        )
        return response.choices[0].message.content

    elif provider == "anthropic":
        from anthropic import Anthropic

        client = Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        if response.stop_reason == "max_tokens":
            print(
                "[processor] WARNING: response hit max_tokens and may be truncated; "
                "JSON parsing may fall back to partial output.",
                file=sys.stderr,
            )
        return response.content[0].text

    elif provider == "ollama":
        from openai import OpenAI

        client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            response_format={"type": "json_object"},
            max_tokens=_MAX_OUTPUT_TOKENS,
        )
        return response.choices[0].message.content

    else:
        raise ValueError(
            f"Unknown LLM provider '{provider}'. Available: gemini, openai, anthropic, ollama"
        )


class Processor:
    def __init__(self, meeting_type: str = None):
        self._cfg = CONFIG["processing"]
        self._provider = self._cfg.get("provider", "gemini")
        self._model_name = self._cfg["model"]
        self._temperature = self._cfg.get("temperature", 0.3)

        prompts = load_prompts()
        self._type = meeting_type or self._cfg.get("default_type", "default")
        if self._type not in prompts:
            raise ValueError(
                f"Unknown meeting type '{self._type}'. "
                f"Available: {', '.join(prompts.keys())}"
            )
        self._system_prompt = prompts[self._type]["system_prompt"]
        self._type_name = prompts[self._type]["name"]

    @property
    def meeting_type(self):
        return self._type

    @property
    def type_name(self):
        return self._type_name

    def process(self, transcript_text: str, context: str = "", correction: str = "") -> dict:
        """Process transcript text into structured notes using the configured LLM provider.

        correction: an authoritative user correction applied when regenerating an
        existing note (e.g. fixing a reversed speaker/name attribution). It is
        treated as ground truth that overrides the transcript where they conflict.
        """
        print(
            f"[processor] Provider: {self._provider} | Type: {self._type_name} | "
            f"Model: {self._model_name} | {len(transcript_text)} chars"
            f"{' | with correction' if correction else ''}",
            file=sys.stderr,
        )

        prompt = f"Here is the transcript:\n\n{transcript_text}"
        prompt += f"\nToday's date is {datetime.now().strftime('%Y-%m-%d')}"
        if context:
            prompt += f"\n\nAdditional context: {context}"
        if correction:
            prompt += (
                "\n\n=== USER CORRECTION (AUTHORITATIVE) ===\n"
                "The user reviewed an earlier version of these notes and is correcting a mistake. "
                "Treat the following as ground truth: it OVERRIDES anything in the transcript that "
                "conflicts with it (for example a reversed or mis-identified speaker/name). Apply it "
                "consistently throughout the whole note — fix the title, the summary, every action "
                "item owner, all decisions, and any quotes or names it affects. Do not reintroduce "
                "the mistake anywhere.\n"
                f"{correction.strip()}"
            )

        raw = _call_llm(self._provider, self._model_name, self._system_prompt, prompt, self._temperature)

        try:
            result = self._parse_json_response(raw)
        except (json.JSONDecodeError, ValueError):
            result = None
        # The model may legally return a non-object (array/string); only a dict
        # is usable downstream, so fall back to a safe shape otherwise.
        if not isinstance(result, dict):
            result = {
                "title": "Untitled Recording",
                "summary": raw,
                "key_points": [],
                "action_items": [],
                "decisions": [],
                "insights": [],
                "topics": [],
                "follow_ups": [],
            }

        result["_meeting_type"] = self._type
        print(f"[processor] Done. Title: {result.get('title', 'N/A')}", file=sys.stderr)
        return result

    @staticmethod
    def _split_segments_by_time(segments, chunk_seconds):
        """Group segments into time-based chunks. Each chunk is a list of segments
        whose start times fall within [chunk_idx * chunk_seconds, (chunk_idx+1) * chunk_seconds)."""
        if not segments:
            return []
        chunks = []
        current = []
        boundary = chunk_seconds
        for seg in segments:
            start = seg.get("start", 0)
            while start >= boundary:
                if current:
                    chunks.append(current)
                    current = []
                boundary += chunk_seconds
            current.append(seg)
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _parse_json_response(text):
        """Best-effort JSON parsing. Strips markdown fences, finds outermost braces.

        Raises json.JSONDecodeError if no valid JSON can be recovered.
        """
        s = (text or "").strip()
        # Strip ```json ... ``` fences
        s = re.sub(r'^```(?:json)?\s*', '', s)
        s = re.sub(r'\s*```$', '', s)
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            # Fall back to the outermost {...} span (handles prose around the JSON).
            i = s.find('{')
            j = s.rfind('}')
            if i >= 0 and j > i:
                return json.loads(s[i:j + 1])
            raise

    def process_chunked(self, transcript, context="", chunk_minutes=30):
        """Length-aware summarization for long meetings.
        Splits the transcript into time-based chunks, summarizes each section,
        then synthesizes the sections into a final exec-summary + per-section note.

        transcript: dict with 'text' and 'segments' (segments must have start/end/text)
        context: optional context string passed to each section
        chunk_minutes: chunk width in minutes (default 30)
        """
        segments = transcript.get("segments") or []
        text = transcript.get("text", "")

        if not segments:
            # No timestamps available — fall back to single-pass on the full text.
            return self.process(text, context=context)

        chunk_seconds = chunk_minutes * 60
        chunks = self._split_segments_by_time(segments, chunk_seconds)
        total = len(chunks)

        if total <= 1:
            # Meeting is shorter than one chunk — single pass is cheaper and just as good.
            return self.process(text, context=context)

        section_results = []
        for idx, chunk in enumerate(chunks, start=1):
            start_sec = chunk[0].get("start", 0)
            end_sec = chunk[-1].get("end", chunk[-1].get("start", 0))
            start_min = int(start_sec // 60)
            end_min = int(end_sec // 60)
            chunk_text = "\n".join(s.get("text", "").strip() for s in chunk if s.get("text"))

            system_prompt = _CHUNK_SECTION_PROMPT.format(
                section_num=idx,
                total_sections=total,
                start_min=start_min,
                end_min=end_min,
            )
            user_prompt = f"Meeting context: {context or 'none provided'}\n\nSection transcript:\n{chunk_text}"

            print(f"  [chunked] section {idx}/{total} ({start_min}-{end_min} min, {len(chunk_text)} chars)", file=sys.stderr)
            response = _call_llm(self._provider, self._model_name, system_prompt, user_prompt, self._temperature)
            try:
                parsed = self._parse_json_response(response)
            except Exception as e:
                print(f"  [chunked] section {idx} JSON parse failed: {e}. Storing raw text.", file=sys.stderr)
                parsed = {
                    "section_title": f"Section {idx}",
                    "summary": response,
                    "decisions": [],
                    "action_items": [],
                    "key_points": [],
                    "open_threads": [],
                }
            parsed["_minutes"] = f"{start_min}-{end_min}"
            section_results.append(parsed)

        # Synthesis pass
        synthesis_user = f"Meeting context: {context or 'none provided'}\n\nSection summaries (JSON array):\n{json.dumps(section_results, indent=2)}"
        print(f"  [chunked] synthesizing {total} sections", file=sys.stderr)
        synthesis_response = _call_llm(self._provider, self._model_name, _CHUNK_SYNTHESIS_PROMPT, synthesis_user, self._temperature)
        try:
            final = self._parse_json_response(synthesis_response)
            if not isinstance(final, dict):
                raise ValueError("synthesis did not return a JSON object")
        except Exception as e:
            print(f"  [chunked] synthesis JSON parse failed: {e}. Returning sections only.", file=sys.stderr)
            final = {
                "title": "Meeting Notes",
                "summary": "Synthesis failed — see sections below.",
                "sections": [{"title": s.get("section_title", f"Section {i+1}"),
                              "summary": s.get("summary", ""),
                              "minutes": s.get("_minutes", "")} for i, s in enumerate(section_results)],
                "decisions": [d for s in section_results for d in s.get("decisions", [])],
                "action_items": [a for s in section_results for a in s.get("action_items", [])],
                "key_points": [k for s in section_results for k in s.get("key_points", [])],
                "open_threads": [o for s in section_results for o in s.get("open_threads", [])],
            }

        final["_meeting_type"] = self._type
        final["_chunked"] = True
        final["_section_count"] = total
        return final


_CLASSIFY_SYSTEM = """You route a voice transcript to the right note template. Decide which ONE it is:

- "idea": one person thinking out loud about a longer-term idea, product, or project they might pursue SOMEDAY but are not actively working on right now — exploring a concept, riffing on "what if we built X", capturing a someday/maybe ambition.
- "meeting": everything else — a meeting, call, standup, 1:1, interview, or a voice note about current or ongoing work, tasks, and decisions.

Most recordings are "meeting". Choose "idea" ONLY when the whole recording is clearly one person exploring a future idea, not doing or discussing present work.

Reply with ONLY this JSON: {"type": "idea"} or {"type": "meeting"}."""


def classify_recording(transcript_text: str, max_chars: int = 4000) -> str:
    """Best-effort binary classification of a transcript: 'idea' or 'meeting'.

    Used by the auto-type path (imported audio files / phone recordings, which
    carry no explicit capture mode). Sends only the opening of the transcript to
    bound tokens, and falls back to 'meeting' on any error so a classifier hiccup
    never blocks processing.
    """
    text = (transcript_text or "").strip()
    if not text:
        return "meeting"
    cfg = CONFIG["processing"]
    try:
        raw = _call_llm(cfg.get("provider", "gemini"), cfg["model"],
                        _CLASSIFY_SYSTEM, text[:max_chars], 0.0)
        data = Processor._parse_json_response(raw)
        if isinstance(data, dict) and str(data.get("type", "")).strip().lower() == "idea":
            print("[classify] Detected: idea", file=sys.stderr)
            return "idea"
    except Exception as e:
        print(f"[classify] failed ({e}); defaulting to meeting.", file=sys.stderr)
    print("[classify] Detected: meeting", file=sys.stderr)
    return "meeting"


_CATEGORY_SYSTEM = """You label what KIND of recording a transcript is.

Reply with ONLY this JSON: {"category": "<label>"}.

Choose the best short (1-3 word) label, preferring one of: Meeting, Phone call, Voice note, 1:1, Interview, Standup, Brainstorm. Use another short label only if none truly fit.
Guidance: a customer-service or personal phone call → "Phone call"; a single-speaker memo or note-to-self → "Voice note"; a multi-person sync or discussion → "Meeting"."""


def classify_category(text: str, max_chars: int = 3000) -> str:
    """Infer a short 'kind of recording' label (Phone call, Meeting, Voice note, …)
    for an existing note. Returns '' on empty input or any error, so a backfill
    never mislabels on a hiccup."""
    t = (text or "").strip()
    if not t:
        return ""
    cfg = CONFIG["processing"]
    try:
        raw = _call_llm(cfg.get("provider", "gemini"), cfg["model"],
                        _CATEGORY_SYSTEM, t[:max_chars], 0.0)
        data = Processor._parse_json_response(raw)
        if isinstance(data, dict):
            return str(data.get("category", "")).strip()[:40]
    except Exception as e:
        print(f"[classify] category failed ({e})", file=sys.stderr)
    return ""

