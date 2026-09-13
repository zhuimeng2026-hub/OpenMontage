# video_analyzer — Does Not Reuse Pre-existing Transcript

- **Date**: 2026-09-05
- **Reporter**: Claude Code (OpenMontage agent, automated reference-video analysis)
- **Severity**: 🟡 MEDIUM — wastes a full faster-whisper pass on a video that already has a transcript on disk. Doubles wall-clock time for the analysis step, inflates the `steps_completed` count without adding signal, and trips up the `has_transcript: false` flag downstream agents rely on.
- **Affected**: `tools/analysis/video_analyzer.py`, the transcript-fetch block (lines ~365–460).
- **Status**: Awaiting OM team fix.

---

## TL;DR

`video_analyzer.execute()` resolves a transcript by **exactly one** of two routes:

1. `transcript_fetcher.execute(...)` — for YouTube/Shorts only (subtitle tracks).
2. Whisper on extracted audio — fallback when route 1 fails.

If both routes don't apply or fail, `transcript_data` stays `None`,
`has_transcript = False`, and the brief has no transcript even when the
caller has already produced one. The tool does **not** consult disk first —
no `input_path` argument, no glob for `_audio_transcript.json` /
`transcript.json` in the output dir, no `pre_existing_transcript` hook.

For a workflow that already runs `transcriber` independently (the project's
canonical pipeline for non-YouTube references), this is a redundant pass and
a misleading "no transcript" report.

---

## 1. Reproduction

- **Setup**:
  - Source video: `/opt/OpenMontage_Voicebox/projects/users/.../weibo-ref-5326097668309060/source.mp4` (118.14 s, Chinese)
  - Pre-existing transcript already on disk at:
    `<workspace>/_audio_transcript.json` (101 segments, `faster-whisper` base + `language=zh`)
- **Tool call**: `video_analyzer.execute({source: <abs path>, analysis_depth: "deep", ...})`
- **Result**:
  - `data._analysis_meta.has_transcript = false`
  - `data._analysis_meta.steps_completed` contains neither `transcript_youtube` nor `transcript_whisper`
  - `data._analysis_meta.steps_failed = []`
  - The brief's `narration_transcript` field is absent.

  Yet the workspace clearly contains a valid transcript file the tool could
  have loaded.

---

## 2. Root cause

`tools/analysis/video_analyzer.py` lines ~365–460. The transcript resolution
flow is:

```python
transcript_data = None
if _is_youtube(platform):
    try:
        from tools.analysis.transcript_fetcher import TranscriptFetcher
        ...
        transcript_data = brief["narration_transcript"]
        steps_completed.append("transcript_youtube")
    except Exception as e:
        steps_failed.append(f"transcript_youtube: {e}")

# Fallback: download + Whisper
if transcript_data is None and audio_path is None and video_path is None and is_url:
    ...

# Fallback: Whisper on audio
if transcript_data is None and audio_path:
    try:
        transcriber = Transcriber()
        tr_result = transcriber.execute({...})
        ...
        steps_completed.append("transcript_whisper")
    except Exception as e:
        steps_failed.append(f"transcript_whisper: {e}")
```

There is no path that consults the filesystem first. For non-URL sources
(`source: "/abs/path/video.mp4"`) the YouTube block is skipped, and the
fallback "download for whisper" is also skipped because `is_url` is False.
The tool expects `audio_path` to be already set, but `execute()` only sets
`audio_path` via `VideoDownloader.execute()` (URL path), so for local files
the Whisper fallback also doesn't fire — `transcript_data` stays None.

Net effect: local-file sources never get a transcript in `video_analyzer`
unless the caller manually runs the transcriber **and** somehow attaches the
result to the brief.

---

## 3. Suggested fix

### 3.1 Add a pre-existing-transcript lookup (minimal, recommended)

Before any of the existing branches, scan the output_dir for known
transcript files:

```python
import json as _json
from pathlib import Path
transcript_data = None
for cand in ("_audio_transcript.json", "transcript.json", "transcript.srt"):
    p = Path(output_dir) / cand
    if p.exists() and p.suffix == ".json":
        try:
            cached = _json.loads(p.read_text(encoding="utf-8"))
            segs = cached.get("segments", [])
            if segs and all({"start", "end", "text"} <= s.keys() for s in segs[:3]):
                transcript_data = cached
                brief["narration_transcript"] = {
                    "full_text": cached.get("text", "") or " ".join(s["text"] for s in segs),
                    "segments": segs,
                    "language": cached.get("language", "auto"),
                    "word_count": len((cached.get("text") or "").split()) or sum(len(s["text"]) for s in segs),
                }
                steps_completed.append("transcript_external")
                break
        except Exception as e:
            steps_failed.append(f"transcript_external:{cand}: {e}")
```

This honors the project pattern where `transcriber` writes
`<input_stem>_transcript.json` next to its input.

### 3.2 Add a `transcript_path` input (explicit, for callers that know)

Extend `input_schema` with:

```python
"transcript_path": {
    "type": "string",
    "description": "Optional pre-existing transcript JSON. If provided, "
                   "skips transcript_fetcher and Whisper entirely.",
}
```

Then early in `execute()`:

```python
if inputs.get("transcript_path"):
    transcript_data = json.loads(Path(inputs["transcript_path"]).read_text(...))
    steps_completed.append("transcript_supplied")
```

### 3.3 Combined (best)

Both: scan for `transcript.json` etc. as fallback; accept `transcript_path`
as an explicit override.

---

## 4. Why this matters

- **Redundant compute**: Whisper on a 118-second Chinese audio costs ~30 s
  on this host. The existing transcript had already been produced in <30 s
  by the agent's preflight. Re-running doubles analysis latency.
- **Misleading `has_transcript: false`**: downstream agents (idea-director,
  reviewer, video-reference-analyst) key off this flag to decide whether
  they can read narration. False negatives make the agent skip narration
  context even though it's literally in the workspace.
- **Audit drift**: two transcripts (one from preflight, one from
  video_analyzer) can disagree — different model sizes, different
  `language` settings. Caller has no single source of truth.

---

## 5. Workaround

Pre-flight the brief by hand-attaching the transcript file the agent already
produced:

```python
import json
brief = registry._tools["video_analyzer"].execute({...}).data

trans_path = "<workspace>/_audio_transcript.json"
if "narration_transcript" not in brief and __import__("os").path.exists(trans_path):
    cached = json.loads(open(trans_path, encoding="utf-8").read())
    segs = cached["segments"]
    brief["narration_transcript"] = {
        "full_text": " ".join(s["text"] for s in segs),
        "segments": segs,
        "language": cached.get("language", "zh"),
        "word_count": sum(len(s["text"]) for s in segs),
    }
    brief["_analysis_meta"]["has_transcript"] = True
```

This is the path used in the 2026-09-05 Weibo reference run.

---

## 6. Files

- `tools/analysis/video_analyzer.py` — bug site (lines ~365–460)
- `tools/analysis/transcriber.py` — produces `<input_stem>_transcript.json`
  next to its input. Naming convention worth aligning with the lookup above.
- `tools/analysis/transcript_fetcher.py` — current YouTube-only path; could
  grow to handle `<workspace>/transcript.srt` as well.
- `tests/` — no test for the "reuse pre-existing transcript" path yet.