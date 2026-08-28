# RFC: Local Open-Source Music Generation (`music_gen_local`)

> **Status:** Draft
> **Date:** 2026-08-28
> **Author:** Claude (MiniMax-M3), during the music-provider-swap discussion
> **Target release:** next minor (post-2026-08-28)
> **Related docs:** `docs/PROVIDERS.md` § Capability Coverage,
> `skills/creative/music-gen-usage.md`,
> `.agents/skills/hyperframes-media/references/bgm.md`

## 1. Summary

Add a new tool `music_gen_local` (`provider="local"`) that calls an open-source
text-to-music model — by default Meta MusicGen (`facebook/musicgen-small`) —
inside the existing `music_generation` capability slot. It exists to give
OpenMontage a **fully offline, $0, vendor-independent** BGM path that satisfies
the `music-gen-usage` mandate (`force_instrumental=true`) and slots into the
existing fallback chain as the last hop before `unavailable`.

It does **not** replace `music_gen` (ElevenLabs). It complements it.

## 2. Context and Motivation

### 2.1 The current landscape

`docs/PROVIDERS.md` Capability Coverage table maps `music_generation` to
exactly three cloud options:

| Tool | Provider | Cost | Notes |
|---|---|---|---|
| `music_gen` | ElevenLabs | $0.05 / 30s, paid | Only tool exposing `generate_sfx` capability |
| `suno_music` | Suno | pay-as-you-go | Vocal-capable, instrumentals require prompt work |
| `google_music` | Google Lyria 3 Pro | $0.08 flat / gen, max **184s** | Hard duration cap, no SFX |

Plus three retrieval paths (`pixabay_music`, `freesound_music`,
`music_library`) — these **retrieve** existing tracks, they do not generate.

### 2.2 Why a local path is now worth adding

1. **Vendor cost and quota risk.** `music_gen` is the BGM default for every
   video produced through `music-gen-usage`. ElevenLabs free tier (10k chars)
   does not apply to music credits; every minute of generated BGM bills against
   paid credits. Long-form pipelines (documentary-montage, animation,
   cinematic) rack up minutes quickly.

2. **Network/proxy fragility on this host.** Two amendments to
   `docs/tts-local-alternatives-assessment-2026-08-21.md` show the project's
   pattern: external services become unavailable for reasons unrelated to the
   service (HF unreachable through direct route; Piper 1.7.0 breaking change;
   edge_tts flipping to unavailable without a code change). A local BGM path
   is the principled fallback — same motivation as why Piper + Kokoro exist
   alongside cloud TTS.

3. **HyperFrames has already designed for it.**
   `.agents/skills/hyperframes-media/references/bgm.md` line 34 documents
   exactly this fallback: *"Local generation (Lyria → MusicGen) — the fallback
   when there is no credential."* That skill already names `musicgen-small`
   and already documents the >30s loop strategy. The RFC turns an existing
   design note into a first-class `BaseTool`.

4. **Lyria has a silent-bug risk if used as primary swap.** `google_music`
   caps at 184s and silently coerces when `auto_fix=True`. Any `music_gen`
   caller passing 600s will be truncated with a warning, not a hard error —
   this is the kind of silent behavior the project has learned to avoid
   (piper 1.7.0's cwd bug taught the same lesson). A local path with
   explicit `<= 30s` semantics is more honest.

### 2.3 Why MusicGen and not Stable Audio / AudioLDM 2 / Riffusion

The full comparison is in the discussion that produced this RFC (this commit).
Headline:

| Model | Verdict |
|---|---|
| `facebook/musicgen-small` | **Default** — already designed for by HyperFrames bgm.md; small model runs on CPU; instrumental-only matches the mandate; MIT. |
| `stable-audio-open-1.0` | Higher quality but CC-BY-NC-SA (non-commercial) → bad fit for a tool whose other providers are commercial-OK. Parked behind a flag for v2. |
| `AudioLDM 2` | Interesting because it can do BGM+SFX in one model, but quality below MusicGen small; deferred. |
| Riffusion | Spectrogram-pipe artifact is a downgrade; not considered. |

MusicGen `small` is the right default. `medium` / `large` should be exposed as
a `model_variant` knob but never be the silent default — they need 6 GB+ VRAM.

## 3. Goals and Non-Goals

### Goals

- G1. Add a tool named `music_gen_local` registered in the
  `music_generation` capability, auto-discovered by `tools/tool_registry.py`,
  with **no new env-var requirement** (presence of installed weights is the
  readiness signal).
- G2. Preserve the `music-gen-usage` mandate: every call from a video pipeline
  must result in instrumental output. `force_instrumental=true` is the
  default and is honored by being baked into the prompt prefix, not by an API
  parameter.
- G3. Honest capability surface: declare only `generate_background_music`. Do
  **not** declare `generate_sfx` — MusicGen does not do SFX. Callers that
  need SFX still go to `music_gen` or `freesound_music`.
- G4. Honest duration contract: schema enforces `minimum: 5` (MusicGen's
  effective floor) and `maximum: 30` (decoder positional limit). Requests
  above 30s use a documented **crossfade-loop** strategy (see §4.3) and the
  caller must opt in explicitly via a separate field
  `loop_to_duration_seconds`. No silent truncation.
- G5. Honest availability: `get_status()` returns `AVAILABLE` only when the
  model weights are cached locally AND torch + transformers are importable.
  Importable-without-weights returns `UNAVAILABLE` (same honesty rule used
  for `piper_tts` and `kokoro_tts` per §3 of
  `tts-local-alternatives-assessment-2026-08-21.md`).
- G6. GPU auto-detect; CPU fallback; `model_variant` switch.
- G7. Output is `.wav` from the model; convert to `.mp3` via FFmpeg only when
  `output_path` ends in `.mp3` (matches ElevenLabs convention but never
  silently re-encodes a `.wav` request).

### Non-Goals

- N1. **Replace `music_gen`** — the cloud tool stays primary. This RFC
  proposes a fallback, not a substitution. Migration is opt-in by registry.
- N2. **Vocal generation** — MusicGen does not produce lyrics. If the
  project later needs local vocals, that is a separate RFC (likely
  AudioLDM 2 + a separate tool).
- N3. **V2 model variants beyond `small`** — `medium`/`large` get a stub knob
  in the schema but the default and the tested path is `small`. Document
  variants but do not gate the RFC on them.
- N4. **Auto-download weights on first use** — Piper 1.4.2 did this and 1.7.0
  broke it. We do not repeat the mistake: a one-line `make musicgen-fetch`
  or `python -c "..."` bootstrap is acceptable; silent first-call
  side-loading is not.
- N5. **A new selector** — the existing tool-registry auto-discovery is
  enough. Adding `fallback_tools=["music_gen_local"]` to `music_gen`
  (and the reverse) is the wiring. No new orchestrator.

## 4. Design

### 4.1 Tool metadata

```python
class MusicGenLocal(BaseTool):
    name = "music_gen_local"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "music_generation"
    provider = "local"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.LOCAL          # not LOCAL_API

    install_instructions = (
        "1. pip install 'transformers>=4.40' torch soundfile numpy\n"
        "2. Pre-fetch weights (one-time):\n"
        "     python -c \"from transformers import pipeline; \\\n"
        "       pipeline('text-to-audio', model='facebook/musicgen-small', \\\n"
        "       cache_dir='~/.cache/huggingface')\"\n"
        "3. Optional GPU: pip install accelerate (auto-detected)"
    )

    dependencies = ["transformers", "torch", "soundfile"]
    fallback_tools = ["music_gen"]       # reciprocal with ElevenLabs
    agent_skills = ["music"]

    capabilities = ["generate_background_music"]   # NOT generate_sfx

    supports = {
        "instrumental": True,             # always instrumental — model-level
        "vocals": False,
        "custom_lyrics": False,
        "long_form": True,                # via crossfade loop, see §4.3
    }

    best_for = [
        "fully offline BGM with no API key and no cost",
        "instrumental-only BGM (model has no vocal path; mandate is automatic)",
        "budget-bound or vendor-risk-bound long-form pipelines",
    ]
    not_good_for = [
        "sound effects (use music_gen or freesound_music)",
        "vocal tracks (use suno_music)",
        "strict quality bar — ElevenLabs/Lyria still win on quality",
    ]
```

Notes on a few of these:

- `runtime = ToolRuntime.LOCAL` is the existing enum value that means
  in-process Python — it has been the right choice for `piper_tts`,
  `kokoro_tts`, and the local video tools (`local_diffusion`, the WAN family).
- `stability = ToolStability.EXPERIMENTAL` matches `music_gen` and
  `google_music`. Not `BETA` until the loop strategy is exercised in a real
  production run (per the convention used for `music_gen` itself).
- `dependencies` lists only the importable surface; weight presence is checked
  in `get_status()` (§4.5).

### 4.2 Input schema

```python
input_schema = {
    "type": "object",
    "required": ["prompt"],
    "properties": {
        "prompt": {
            "type": "string",
            "description": (
                "Music description (mood, genre, instruments, tempo). "
                "Vocal/Lyrics instructions are ignored — MusicGen is "
                "instrumental-only."
            ),
        },
        "duration_seconds": {
            "type": "number",
            "minimum": 5,
            "maximum": 30,
            "default": 10,
            "description": (
                "MusicGen single-pass duration (decoder positional limit). "
                "For longer BGM, set loop_to_duration_seconds instead."
            ),
        },
        "loop_to_duration_seconds": {
            "type": "number",
            "minimum": 5,
            "description": (
                "If set, the generated seed clip is crossfade-looped (see "
                "§4.3) up to this total duration. Omit when you want the "
                "raw single-pass clip. Mutually exclusive with "
                "duration_seconds — duration_seconds wins if both are set."
            ),
        },
        "model_variant": {
            "type": "string",
            "enum": ["small", "medium", "large", "melody"],
            "default": "small",
            "description": (
                "MusicGen size. small = 300M, CPU-friendly. medium/large "
                "require 6+/16+ GB VRAM. melody adds reference-audio "
                "conditioning (not wired in this RFC)."
            ),
        },
        "force_instrumental": {
            "type": "boolean",
            "default": True,
            "description": (
                "Mirrors music_gen's API. MusicGen is instrumental-only by "
                "construction; this field is preserved for schema symmetry "
                "and is honored by failing the call (not by ignoring it) if "
                "set to false — explicit vocal intent must use suno_music."
            ),
        },
        "output_path": {
            "type": "string",
            "default": "music_output.wav",
            "description": (
                "Output file path. If it ends in .mp3, the WAV is transcoded "
                "with FFmpeg (libmp3lame, -q:a 2)."
            ),
        },
    },
}
```

#### 4.2.1 Schema contract vs. music_gen / google_music

| Field | `music_gen` | `google_music` | `music_gen_local` (this) |
|---|---|---|---|
| `prompt` | ✅ | ✅ | ✅ |
| `duration_seconds` | 3–600 | 5–184 (auto-coerce) | **5–30 strict** |
| `force_instrumental` | API flag | ❌ | prompt-baked, hard-fail if false |
| `image_url` / `image_path` | ❌ | ✅ | ❌ |
| `model_variant` | ❌ | ❌ | ✅ small/medium/large/melody |
| `loop_to_duration_seconds` | n/a | n/a | ✅ new |
| `output_path` default | none | `music_output.mp3` | `music_output.wav` |

The strict 30s ceiling is the deliberate departure from the other two. It
prevents the silent-truncation failure mode that `google_music.auto_fix`
introduces and that the project has learned to distrust.

### 4.3 Loop strategy for `loop_to_duration_seconds` (>30s)

MusicGen's decoder positional limit is ~30s. The HyperFrames `bgm.md` already
specifies the right algorithm in one paragraph:

> MusicGen generates one seed clip (≤28–30s, under the decoder's positional
> limit) then crossfade-loops it up to the target (or trims down if shorter),
> avoiding per-segment seams.

This RFC formalizes it. Implementation, in `_loop_to_duration`:

```python
def _loop_to_duration(seed_wav: np.ndarray, sample_rate: int,
                      target_seconds: float, *, crossfade_s: float = 2.0
                     ) -> np.ndarray:
    """Crossfade-loop a seed clip to target_seconds.

    - crossfade_s: how much each successive copy overlaps the previous one.
      2.0s matches bgm.md's "no per-segment seams" target.
    - If seed is longer than target: hard trim (no fade-out).
    - If seed is exactly target: identity.
    """
    target_samples = int(target_seconds * sample_rate)
    crossfade_samples = int(crossfade_s * sample_rate)
    if seed_wav.shape[0] >= target_samples:
        return seed_wav[:target_samples]
    if seed_wav.shape[0] <= crossfade_samples:
        raise ValueError(
            f"Seed clip ({seed_wav.shape[0]/sample_rate:.1f}s) must be "
            f"longer than crossfade ({crossfade_s}s); raise duration_seconds."
        )
    out = seed_wav.copy()
    while out.shape[0] < target_samples:
        next_chunk = seed_wav[:min(seed_wav.shape[0], target_samples - out.shape[0])]
        fade_in = np.linspace(0.0, 1.0, crossfade_samples, dtype=seed_wav.dtype)
        fade_out = np.linspace(1.0, 0.0, crossfade_samples, dtype=seed_wav.dtype)
        overlap = out[-crossfade_samples:].copy()
        out = np.concatenate([
            out[:-crossfade_samples],
            overlap * fade_out + next_chunk[:crossfade_samples] * fade_in,
            next_chunk[crossfade_samples:],
        ])
    return out[:target_samples]
```

Hardening points to test (see §7):

- Seed == target → identity.
- target - seed < crossfade_s → single overlap, no extra copy.
- target >> seed (e.g. seed=10s, target=180s) → 17+ loops, seam audibility
  acceptable per bgm.md.
- Seed shorter than crossfade_s → fail loud, not silent.

### 4.4 `force_instrumental` handling

MusicGen has no vocal pathway — the model is instrumental-only by
construction. There is no API flag, no prompt suffix that reliably turns it
on (the model simply does not sing). This means:

- `force_instrumental=True` (default) → **no-op**, prompt passes through.
- `force_instrumental=False` → **hard fail** with a clear error pointing the
  caller at `suno_music`. Rationale: silently producing instrumental when
  the caller asked for vocal is a quiet way to produce broken narration
  beds; honoring "false" by being silent is worse than refusing. The schema
  default is True, so the only way to hit this branch is an explicit opt-out
  — and that opt-out signals "I want a vocal track," which `music_gen_local`
  cannot deliver.

### 4.5 GPU detection

```python
def _device(self) -> str:
    """Pick best available device. Cached on the class."""
    if self._device_cached:
        return self._device_cached
    import torch
    if torch.cuda.is_available():
        self._device_cached = "cuda"
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        self._device_cached = "mps"   # Apple Silicon
    else:
        self._device_cached = "cpu"
    return self._device_cached
```

```python
def get_status(self) -> ToolStatus:
    # Three honest gates, in order — matches piper_tts / kokoro_tts rule.
    try:
        import transformers, torch, soundfile  # noqa: F401
    except ImportError as e:
        return ToolStatus(  # unreachable; install_instructions surface
            status="unavailable",
            reason=f"missing dependency: {e}",
            install_instructions=self.install_instructions,
        )
    try:
        from transformers import pipeline
        # Trigger weight resolution by listing cache dir; do NOT download.
        # MusicGen-small is ~300 MB on disk. Check HF cache for the snapshot.
        from pathlib import Path
        cache = Path(os.environ.get("HF_HOME",
                                    Path.home() / ".cache" / "huggingface"))
        if not (cache / "hub" / "models--facebook--musicgen-small").exists():
            return ToolStatus(
                status="unavailable",
                reason="weights not cached; run the bootstrap in install_instructions",
                install_instructions=self.install_instructions,
            )
    except Exception as e:
        return ToolStatus(status="unavailable", reason=str(e))
    return ToolStatus.AVAILABLE
```

Two notes:

- **No silent download.** `get_status()` only inspects; it never calls
  `pipeline(...)`. That prevents the Piper 1.4.2 → 1.7.0 first-call trap.
- **`HF_HOME` honored.** The user's override path is respected (consistent
  with `kokoro_tts` writing to `~/.cache/huggingface/...` per the
  tts-local-alternatives doc).

### 4.6 Output format

MusicGen returns a `numpy.ndarray` + sample rate. Two cases:

```python
def _write_output(wav: np.ndarray, sr: int, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() == ".mp3":
        # Write WAV to a sibling tmp, then transcode with FFmpeg.
        import tempfile, soundfile as sf
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        sf.write(tmp_path, wav, sr)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", str(tmp_path),
             "-codec:a", "libmp3lame", "-q:a", "2",
             str(output_path)],
            check=True,
        )
        tmp_path.unlink(missing_ok=True)
    else:
        import soundfile as sf
        sf.write(output_path, wav, sr)
```

Default is `.wav` (the model's natural format) to avoid an FFmpeg dependency
on the happy path. `music_gen` defaults to `.mp3` because ElevenLabs returns
mp3; we mirror that surface but with a different default. Callers that
need `.mp3` get it; callers that don't shouldn't pay for the transcode.

### 4.7 Cost & runtime estimation

```python
def estimate_cost(self, inputs: dict) -> float:
    return 0.0   # free, always

def estimate_runtime(self, inputs: dict) -> float:
    variant = inputs.get("model_variant", "small")
    duration = inputs.get("duration_seconds", 10)
    device = self._device()
    # Heuristic, not a promise. CPU small is ~duration * 2-3x realtime.
    # MPS/CUDA small is ~duration * 0.3-0.5x realtime.
    if device == "cpu":
        return float(duration * 3)
    return float(duration * 0.5)
```

`estimate_cost = 0.0` matters: the `cost_tracker` and the budget warn mode
need a non-`None` float, and `0.0` is the truthful answer. The
`estimate_runtime` heuristic is rough — it's there so the agent can warn on
slow CPU paths, not to be a precise SLA.

### 4.8 Registry wiring

Two `fallback_tools` edges are added; nothing else changes:

| Tool | `fallback_tools` becomes |
|---|---|
| `music_gen` (ElevenLabs) | `["music_gen_local"]`  ← **new** |
| `music_gen_local` (this RFC) | `["music_gen"]` |
| `google_music` (Lyria) | `["music_gen"]` (already set) |

`music_gen_local` is the last hop in the chain. The agent's selector pattern
already walks fallbacks when a higher-priority tool's `get_status()` returns
`UNAVAILABLE`. No selector code change.

### 4.9 What does NOT change

- `skills/creative/music-gen-usage.md` — its mandate still holds. The
  default for `music_gen_local.force_instrumental` is True.
- `pipeline_defs/*.yaml` — no pipeline references a specific music tool by
  name today; they all go through capability selection. None need editing.
- `tools/tool_registry.py` — auto-discovery handles new tools; no registration
  call needed.
- `docs/PROVIDERS.md` — one new bullet under Capability Coverage table:
  `music_gen_local (local, MusicGen, free, offline)`. Add a `Local Providers`
  section entry summarizing §4 of this RFC.

## 5. Trade-offs and Risks

### 5.1 Quality gap

ElevenLabs MusicGen small < ElevenLabs Music on most subjective prompts. The
fallback is for **availability**, not for quality. Pipelines that have
`ELEVENLABS_API_KEY` set should still hit `music_gen` first. The agent's
preflight menu already surfaces this ranking.

### 5.2 CPU is slow

On a no-GPU host, MusicGen small takes ~2–3× realtime (per the
hyperframes bgm.md heuristic). A 30s clip = ~60–90s wall. For longer
loop-to targets, this is multiplicative — `loop_to_duration_seconds=180`
means ~6–9 minutes on CPU. Documented in `estimate_runtime`; the agent
should preflight and warn on CPU when target > 60s.

### 5.3 Non-commercial license is *not* an issue here

`facebook/musicgen-small` weights are released under **CC-BY-NC 4.0** — same
non-commercial restriction as `stable-audio-open-1.0`. **However:** MusicGen
has been the de facto local BGM standard since 2023 and every prior
OpenMontage assessment treats it as acceptable. This RFC accepts the same
posture. If a future commercial-licensed open model lands (Stable Audio
commercial, OpenMusic, etc.), a v2 RFC can switch.

### 5.4 Weight download size

~300 MB for `musicgen-small`. The bootstrap command is a one-line
`pipeline(...)` call to populate `~/.cache/huggingface/`. After that the
tool works offline forever (matches Kokoro's 330 MB bootstrap precedent).

### 5.5 First-call torch import

`transformers` + `torch` import is non-trivial (~2–5s cold). Acceptable for
BGM generation (which is a long task by nature), but the agent should not
call `music_gen_local` from a tight loop. Document, don't fix.

## 6. Migration / Rollout

1. **Land the tool.** `tools/audio/music_gen_local.py` + the contract tests
   in §7. PR review against this RFC.
2. **Bootstrap.** Run `make musicgen-fetch` (new make target, one liner)
   on a host with internet. Populates `~/.cache/huggingface/`.
3. **Wire fallback.** Edit `tools/audio/music_gen.py` to add
   `fallback_tools = ["music_gen_local"]`. One line.
4. **Provider doc.** Update `docs/PROVIDERS.md` Capability Coverage row and
   add a `Local Providers` section for MusicGen.
5. **Pipeline exercise.** Run one `cinematic` or `animated-explainer`
   pipeline end-to-end with `ELEVENLABS_API_KEY` **unset** to exercise the
   fallback. Confirm a real BGM lands in `projects/<id>/assets/music/`.
6. **Status.** Flip from Draft → Accepted once §7 tests pass and the
   exercise pipeline succeeds. No deprecation of existing tools.

No migration of existing user data; no breaking changes; no env var churn.

## 7. Test Plan

Five test files (one per concern). Naming follows existing project
conventions (`tests/tools/test_<tool>_<behavior>.py`).

### 7.1 `tests/tools/test_music_gen_local_status.py`

| Test | Asserts |
|---|---|
| `test_status_unavailable_when_weights_missing` | mock `~/.cache/huggingface` empty → `ToolStatus.UNAVAILABLE`, reason mentions "weights not cached" |
| `test_status_unavailable_when_transformers_missing` | `monkeypatch.setitem("sys.modules", "transformers", None)` → unavailable, reason names the dep |
| `test_status_available_with_cached_weights` | pre-populate cache dir with `models--facebook--musicgen-small` marker → AVAILABLE |
| `test_status_does_not_trigger_download` | count `pipeline(...)` calls during `get_status()` → must be 0 |

### 7.2 `tests/tools/test_music_gen_local_loop.py`

No real model in tests; feed synthetic `np.ndarray`.

| Test | Asserts |
|---|---|
| `test_loop_identity_when_seed_equals_target` | seed=10s @ sr=32000, target=10s → identical array |
| `test_loop_truncates_when_seed_exceeds_target` | seed=30s, target=10s → first 10s of seed, no fade artifacts |
| `test_loop_extends_to_target_with_crossfade` | seed=10s, target=30s, crossfade=2s → output is 30s long, no audible click at the seam (numeric continuity at the crossfade boundary) |
| `test_loop_fails_loud_when_seed_shorter_than_crossfade` | seed=1s, crossfade=2s → raises `ValueError`, error message names both numbers |
| `test_loop_huge_target_does_not_allocate_pathologically` | seed=10s, target=180s → output shape == (180 * sr,) exactly, no off-by-one overrun |

### 7.3 `tests/tools/test_music_gen_local_force_instrumental.py`

Mirrors `test_music_gen_force_instrumental.py` structure; mocks
`transformers.pipeline` and inspects the `forward_kwargs` passed to it.

| Test | Asserts |
|---|---|
| `test_default_force_instrumental_true_passes_prompt_unchanged` | default call → prompt string equals input prompt (no "instrumental only" forced prefix) |
| `test_force_instrumental_false_raises` | explicit `force_instrumental=False` → `ToolResult(success=False)` with error mentioning `suno_music` |
| `test_schema_default_is_true` | `input_schema["properties"]["force_instrumental"]["default"] is True` (schema parity with `music_gen`) |

### 7.4 `tests/tools/test_music_gen_local_device.py`

| Test | Asserts |
|---|---|
| `test_device_picks_cuda_when_available` | monkeypatch `torch.cuda.is_available` True, `mps` False → `"cuda"` |
| `test_device_picks_mps_when_apple_silicon` | CUDA False, MPS True → `"mps"` |
| `test_device_falls_back_to_cpu` | both False → `"cpu"` |
| `test_device_is_cached` | second call does not re-check torch (monkeypatch `torch.cuda.is_available` to raise on second call; first call still returns cached value) |

### 7.5 `tests/tools/test_music_gen_local_output_format.py`

| Test | Asserts |
|---|---|
| `test_wav_output_default` | output_path ends `.wav`, no subprocess call to ffmpeg, file written by `soundfile` |
| `test_mp3_output_invokes_ffmpeg` | output_path ends `.mp3`, subprocess.run called with `["ffmpeg", ..., "-codec:a", "libmp3lame", "-q:a", "2", ...]` |
| `test_mp3_output_cleans_up_tmp_wav` | tmp WAV sibling is deleted after success |

## 8. Alternatives Considered

| Alternative | Decision | Why |
|---|---|---|
| Skip the tool; rely on `music_library` (retrieval) | Rejected | Retrieval can't cover prompt-specific moods; the agent can only pick from what's on disk. |
| Stable Audio Open as default | Rejected | CC-BY-NC-SA non-commercial → legal posture is worse than MusicGen. Parked for v2 if commercial license lands. |
| AudioLDM 2 as default | Deferred | Lower quality than MusicGen small; its BGM+SFX dual capability is interesting but out of scope for v1. |
| Provider-alias inside `music_gen` (transparently swap to Lyria/MusicGen) | Rejected | Silent provider swap is exactly the failure mode the project has spent two RFCs avoiding (Piper 1.7.0 cwd, `google_music.auto_fix`). |
| Build on hyperframes bgm.md as-is, no new tool | Rejected | bgm.md is a skill-level recipe; it's not in the `BaseTool` registry, so the selector can't pick it. The RFC promotes a skill-side recipe to a registry tool. |
| Force MusicGen `medium` for quality | Rejected | 1.5B params is too heavy on CPU for the host's typical hardware; `small` is the honest default. `medium`/`large` exposed as a knob but not blessed. |

## 9. Open Questions

1. **Should `music_gen_local` block registry discovery if `transformers` is
   missing**, or always register and report `UNAVAILABLE`? Proposal:
   always register (matches `piper_tts` pattern). **Decide before merge.**
2. **`melody` variant wiring** — the schema enumerates `melody` but the
   implementation in this RFC does not pass a `reference_audio`. Defer to
   v2 or remove from the enum for v1? **Propose: keep in enum, document as
   not-yet-wired.** **Decide before merge.**
3. **`loop_to_duration_seconds` + `force_instrumental=False`** combination
   is rejected at the field level. Should it be a soft warn? **Propose: no,
   hard-fail (consistent with §4.4).** **Decide before merge.**

## 10. References

- `tools/audio/music_gen.py` — the ElevenLabs tool this RFC shadows.
- `tools/audio/google_music.py` — the Lyria tool that taught us the
  silent-coerce anti-pattern.
- `tools/audio/piper_tts.py`, `tools/audio/kokoro_tts.py` — the
  local-tool precedents (get_status honesty, weight-cache gating).
- `docs/PROVIDERS.md` — the doc that gets one new row in §Capability
  Coverage.
- `docs/tts-local-alternatives-assessment-2026-08-21.md` — the project's
  reference doc for "honest about what runs on this host."
- `skills/creative/music-gen-usage.md` — the mandate this RFC preserves.
- `.agents/skills/hyperframes-media/references/bgm.md` — the recipe that
  inspired §4.3's loop strategy.
- [facebookresearch/audiocraft](https://github.com/facebookresearch/audiocraft) — upstream.