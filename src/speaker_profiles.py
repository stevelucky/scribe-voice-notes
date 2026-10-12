"""Speaker profile enrollment and identification.

Profiles are stored in {project_dir}/speakers/:
  profiles.json          — name, role, notes, sample filename
  samples/NAME.wav       — voice sample audio
  embeddings/NAME.npy    — precomputed embedding vector
"""

import os
import sys
import json
import numpy as np

_PROFILES_FILE = "profiles.json"
_SAMPLES_DIR = "samples"
_EMBEDDINGS_DIR = "embeddings"

# Cosine similarity threshold for a confident match (0-1, higher = stricter)
MATCH_THRESHOLD = 0.75


def speakers_dir(project_dir: str) -> str:
    return os.path.join(project_dir, "speakers")


def load_profiles(project_dir: str) -> list:
    path = os.path.join(speakers_dir(project_dir), _PROFILES_FILE)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def save_profiles(project_dir: str, profiles: list):
    sdir = speakers_dir(project_dir)
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, _PROFILES_FILE), "w") as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)


def enroll_speaker(project_dir: str, name: str, role: str, notes: str, sample_path: str) -> dict:
    """Compute embedding for a voice sample and save the profile. Returns the profile."""
    sdir = speakers_dir(project_dir)
    samples_dir = os.path.join(sdir, _SAMPLES_DIR)
    embeddings_dir = os.path.join(sdir, _EMBEDDINGS_DIR)
    os.makedirs(samples_dir, exist_ok=True)
    os.makedirs(embeddings_dir, exist_ok=True)

    safe_name = name.replace(" ", "_").replace("/", "-")
    sample_dest = os.path.join(samples_dir, f"{safe_name}.wav")
    embedding_path = os.path.join(embeddings_dir, f"{safe_name}.npy")

    # Copy sample to speakers/samples/
    import shutil
    shutil.copy2(sample_path, sample_dest)

    # Compute embedding
    embedding = _compute_embedding(sample_dest)
    np.save(embedding_path, embedding)

    # Update profiles list
    profiles = load_profiles(project_dir)
    profiles = [p for p in profiles if p["name"] != name]  # replace if exists
    profile = {
        "name": name,
        "role": role,
        "notes": notes,
        "sample": f"{_SAMPLES_DIR}/{safe_name}.wav",
        "embedding": f"{_EMBEDDINGS_DIR}/{safe_name}.npy",
    }
    profiles.append(profile)
    save_profiles(project_dir, profiles)
    print(f"[speakers] Enrolled: {name}", file=sys.stderr)
    return profile


def delete_speaker(project_dir: str, name: str):
    profiles = load_profiles(project_dir)
    to_delete = next((p for p in profiles if p["name"] == name), None)
    if to_delete:
        for key in ("sample", "embedding"):
            rel = to_delete.get(key)
            if not rel:
                continue  # missing key would resolve to the speakers/ dir itself
            path = os.path.join(speakers_dir(project_dir), rel)
            if os.path.isfile(path):
                os.remove(path)
    save_profiles(project_dir, [p for p in profiles if p["name"] != name])


def identify_speakers(project_dir: str, diarized_segments: list, audio_path: str) -> list:
    """Replace SPEAKER_XX labels with known names where confidence is high enough."""
    profiles = load_profiles(project_dir)
    if not profiles:
        return diarized_segments

    sdir = speakers_dir(project_dir)

    # Load enrolled embeddings
    enrolled = []
    for p in profiles:
        emb_path = os.path.join(sdir, p["embedding"])
        if os.path.exists(emb_path):
            enrolled.append({"name": p["name"], "role": p.get("role", ""), "embedding": np.load(emb_path)})

    if not enrolled:
        return diarized_segments

    # Get unique speaker labels from this meeting
    unique_speakers = list({s["speaker"] for s in diarized_segments})

    # For each unique diarized speaker, collect all their audio segments and compute a merged embedding
    import soundfile as sf
    audio_data, sr = sf.read(audio_path)
    if audio_data.ndim > 1:
        audio_data = audio_data.mean(axis=1)

    speaker_mapping = {}  # SPEAKER_XX -> real name

    for spk in unique_speakers:
        segments = [s for s in diarized_segments if s["speaker"] == spk]
        # Concatenate audio chunks for this speaker
        chunks = []
        for seg in segments:
            start = int(seg["start"] * sr)
            end = int(seg["end"] * sr)
            chunk = audio_data[start:end]
            if len(chunk) > sr * 0.5:  # skip chunks < 0.5s
                chunks.append(chunk)

        if not chunks:
            continue

        combined = np.concatenate(chunks)
        # Need at least 2 seconds of audio for a reliable embedding
        if len(combined) < sr * 2:
            continue

        # Save temp file for embedding computation
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        sf.write(tmp_path, combined, sr)

        try:
            meeting_emb = _compute_embedding(tmp_path)
        finally:
            os.unlink(tmp_path)

        # Find best match among enrolled speakers
        best_name, best_score = _best_match(meeting_emb, enrolled)
        if best_score >= MATCH_THRESHOLD:
            speaker_mapping[spk] = best_name
            print(f"[speakers] {spk} → {best_name} (score={best_score:.2f})", file=sys.stderr)
        else:
            print(f"[speakers] {spk} → unrecognized (best={best_name}, score={best_score:.2f})", file=sys.stderr)

    # Apply mapping
    for seg in diarized_segments:
        if seg["speaker"] in speaker_mapping:
            seg["speaker"] = speaker_mapping[seg["speaker"]]

    return diarized_segments


def _compute_embedding(audio_path: str) -> np.ndarray:
    """Compute a speaker embedding using wespeaker-voxceleb-resnet34-LM via pyannote."""
    os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.0")
    import torch
    from pyannote.audio import Model, Inference

    token = os.environ.get("HF_TOKEN")

    # pyannote/wespeaker-voxceleb-resnet34-LM is gated but approved alongside
    # pyannote/speaker-diarization-3.1 — no separate request needed.
    model = Model.from_pretrained(
        "pyannote/wespeaker-voxceleb-resnet34-LM",
        use_auth_token=token
    )
    inference = Inference(model, window="whole")

    use_mps = torch.backends.mps.is_available()
    if use_mps:
        inference.to(torch.device("mps"))

    try:
        embedding = inference(audio_path)
    except (RuntimeError, MemoryError) as e:
        if not use_mps or "out of memory" not in str(e).lower():
            raise
        print("[speakers] MPS out of memory computing an embedding — falling back to CPU.",
              file=sys.stderr)
        try:
            torch.mps.empty_cache()
        except Exception:
            pass
        inference.to(torch.device("cpu"))
        embedding = inference(audio_path)
    return np.array(embedding)


def _best_match(query: np.ndarray, enrolled: list) -> tuple:
    """Return (name, cosine_similarity) for the best matching enrolled speaker."""
    best_name = ""
    best_score = -1.0
    for e in enrolled:
        score = float(np.dot(query, e["embedding"]) /
                      (np.linalg.norm(query) * np.linalg.norm(e["embedding"]) + 1e-9))
        if score > best_score:
            best_score = score
            best_name = e["name"]
    return best_name, best_score
