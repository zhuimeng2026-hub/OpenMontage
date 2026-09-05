# video_analyzer — Keyframe Sub-step Silent Failure

- **Date**: 2026-09-05
- **Reporter**: Claude Code (OpenMontage agent, automated reference-video analysis)
- **Severity**: 🟠 HIGH — `video_analyzer.deep` mode silently returns an empty `keyframes[]`, blocking all downstream LLM-synthesis over frames. The reference-analysis pipeline (`AGENT_GUIDE.md` §"Reference Video Entry Point") cannot close without keyframes.
- **Affected**: `tools/analysis/video_analyzer.py`, both branches of `STEP 4: Keyframe extraction` (lines ~555–605). Affects `analysis_depth ∈ {"standard", "deep"}` on any source (URL or local_file).
- **Status**: Awaiting OM team fix. Document is the evidence + suggested fix; root cause is the missing `else` / `steps_failed` branch when `FrameSampler.execute()` returns `success=False`.

---

## TL;DR

`video_analyzer.execute()` calls `FrameSampler.execute(...)` to extract keyframes.
The control-flow reads:

```python
fs_result = sampler.execute({...})
if fs_result.success:
    for frame in fs_result.data.get("frames", []):
        keyframes.append({...})
    steps_completed.append("keyframes")
except Exception as e:
    steps_failed.append(f"keyframes: {e}")
```

When `fs_result.success` is `False` (e.g. permission/scope/path error inside
`FrameSampler`), **none** of the three branches runs:

- `steps_completed.append("keyframes")` — guarded by `if fs_result.success`
- `steps_completed.append("keyframes_uniform")` — same guard on the no-scenes fallback
- `steps_failed.append("keyframes: <err>")` — guarded by `try/except Exception`, not by `success=False`

Net effect: `_analysis_meta` reports `steps_completed` without `keyframes` /
`keyframes_uniform`, `steps_failed: []`, and `keyframes: []` in the brief.
**Nothing in the artifact reveals that the sub-step failed.** An operator who
reads only the brief will believe the keyframe step was intentionally
deferred to a downstream agent — but it's actually broken.

---

## 1. Reproduction

- **Source**: `/opt/OpenMontage_Voicebox/projects/users/.../weibo-ref-5326097668309060/source.mp4`
  (Weibo 视频, 720×1280, 118.14 s, H264+AAC, downloaded 2026-09-05).
- **Tool call**:

  ```python
  registry._tools["video_analyzer"].execute({
      "source": "<abs path to source.mp4>",
      "project_id": "weibo-ref-5326097668309060",
      "userid": "local-dev",
      "analysis_depth": "deep",
      "max_keyframes": 24,
      "max_duration_seconds": 600,
  })
  ```

- **Result**: `success=True`, `data.keyframes = []`,
  `data._analysis_meta.steps_completed = ["metadata","scene_detect","motion_classification","audio_energy"]`,
  `data._analysis_meta.steps_failed = []`,
  `data._analysis_meta.keyframe_count = 0`.

Expected: at least 20 keyframe entries with `{timestamp, scene_index, path,
description}`. Actual: zero keyframes, no failure recorded.

Side-channel evidence the keyframe call was attempted and failed: the
`source_frames/` directory contains 58 extracted JPGs from the separately-run
`video-understand` script, but no `keyframes/` subdirectory was created by
`video_analyzer`. `FrameSampler.execute()` did not write any output (its
internal `output_dir` is the relative `keyframes/` subpath, which doesn't
exist after the failed call).

---

## 2. Root cause

`tools/analysis/video_analyzer.py` lines ~555–605, both branches:

```python
if fs_result.success:
    for frame in fs_result.data.get("frames", []):
        keyframes.append({...})
    steps_completed.append("keyframes")
except Exception as e:
    steps_failed.append(f"keyframes: {e}")
```

Missing:

```python
else:
    err = fs_result.error or "FrameSampler.execute returned success=False"
    steps_failed.append(f"keyframes: {err}")
    # Optional: still record an empty entry so the agent knows it was tried
```

The same gap exists for the no-scenes fallback (uses `"keyframes_uniform"`).

The pattern repeats elsewhere in the file (transcript_fetcher / transcriber
fallback chains at lines ~380–460), but in those paths the outer `try` wraps
the whole block including the network call, so an `Exception` from a network
glitch is at least recorded. `FrameSampler` returns a `ToolResult` instead
of raising — that's why the silent path is reachable here.

---

## 3. Suggested fix

Three options, ranked by intrusiveness:

### 3.1 Minimal — add `else:` branch (recommended)

```python
fs_result = sampler.execute({...})
if fs_result.success:
    for frame in fs_result.data.get("frames", []):
        ...
    steps_completed.append("keyframes")
else:
    err = (fs_result.error or "FrameSampler returned success=False").strip()
    steps_failed.append(f"keyframes: {err}")
```

Same change for `keyframes_uniform`. Two `else:` clauses; ~6 lines total.

### 3.2 Defensive — wrap in helper

Extract `def _extract_keyframes(...) -> tuple[list, str|None]` that always
returns `(keyframes, error_or_None)` and never silently drops state.

### 3.3 Architecture — promote keyframe output to a top-level artifact

Treat keyframes as a first-class artifact that downstream stages depend on,
and have `video_analyzer` raise (or return `success=False`) if the keyframe
step fails AND `analysis_depth != "transcript_only"`. This surfaces the
failure to the caller instead of hiding it in `_analysis_meta`.

---

## 4. Why this matters

`AGENT_GUIDE.md` §"Reference Video Entry Point" requires:

> Run the reference analysis workflow using the local analysis tools
> (`video_analyzer`, transcript extraction, scene detection, frame sampling)
> → Produce a grounded summary of what the reference is doing ...

The current behavior makes this workflow report success while providing zero
keyframes — agents downstream (idea-director / video-template-remix /
video-reference-analyst meta skill) will assume the analysis is complete
and skip the LLM-synthesis step entirely. Result: research_brief.json is
empty in the agent-fillable fields, the loop doesn't close, and the bug
doesn't show up until a human notices the empty filmstrip on the Backlot
board.

---

## 5. Workaround (until fix lands)

Run `video-understand` separately to extract frames, then have the agent
merge them into the brief:

```python
# 1. Extract scene frames + Whisper transcript with video-understand skill
import subprocess
subprocess.run(["python3", ".claude/skills/video-understand/scripts/understand_video.py",
                "<abs path to source.mp4>", "--max-frames", "24",
                "--whisper-model", "base", "-o", "<workspace>/understanding.json"],
               check=True)

# 2. Run video_analyzer to get the structural skeleton
brief = registry._tools["video_analyzer"].execute({...}).data

# 3. Merge understanding.json['frames'] into brief['keyframes']
import json
u = json.load(open("<workspace>/understanding.json"))
brief["keyframes"] = [
    {"timestamp": f["timestamp"], "timestamp_formatted": f["timestamp_formatted"],
     "path": f["path"], "scene_index": None,
     "description": "scene-change moment"} for f in u["frames"]
]
```

This is the path used in the 2026-09-05 Weibo reference run; it produced a
viable `research_brief.json` for downstream pipelines. Not a long-term fix —
the silent failure needs a PR.

---

## 6. Files

- `tools/analysis/video_analyzer.py` — bug site (lines ~555–605)
- `tools/analysis/frame_sampler.py` — caller that returns `success=False`
  rather than raising. Worth checking whether FrameSampler has its own
  `steps_failed`-style log so the chain is traceable.
- `tests/backlot/` — there is currently no test that asserts
  `brief["keyframes"]` is non-empty when `analysis_depth != "transcript_only"`.
  One is needed.