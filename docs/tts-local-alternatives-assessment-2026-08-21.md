# Local Open-Source TTS Alternatives — Assessment (2026-08-21)

> One-time eval, frozen on 2026-08-21, amended twice (morning + evening of
> 2026-08-28). Re-read when someone wants to optimize the local TTS path
> on this host. Last re-amended by Claude (MiniMax-M3) on 2026-08-28
> evening during the refactor-serenade reference-driven pipeline run.
>
> **Amended 2026-08-28 (twice)** — the original assessment below is kept as the
> 2026-08-21 snapshot, but multiple load-bearing facts have since changed.
> Read both updates (sections 1, 2, 3 in their original order; section 4 is
> new) before the table below; where the old text is now wrong it is marked
> inline with **[superseded 2026-08-28]** or **[superseded 2026-08-28 (ev)]**.

## Update — 2026-08-28

Two things invalidated parts of the frozen assessment.

### 1. HuggingFace is reachable through a proxy

The eval's central constraint was "HF unreachable → Kokoro and Qwen3-TTS
cannot load weights." That was measured on the **direct** route only. Through
the host's local proxy it works:

| Route | `huggingface.co` |
|---|---|
| direct | `000` (timeout) |
| `-x http://127.0.0.1:7890` | `200` |

**Decision-tree item 2 (Kokoro) is therefore unblocked — and now done.**
`tools/audio/kokoro_tts.py` exists, is auto-discovered by the registry, and
reports AVAILABLE. See section 3.

**The proxy is deliberately NOT baked into config.** Weight downloads are a
one-time bootstrap; hardcoding a proxy would make an "offline" engine silently
depend on it. Export `HTTPS_PROXY` for the download, not for runtime.

### 2. Piper is now installed, downloaded, and actually working

`piper-tts 1.7.0` is installed in the project venv (`.venv`, Python 3.10.12) —
not just in pyenv 3.11.8 — and `en_US-lessac-medium` (63 MB) plus
`zh_CN-huayan-medium` are downloaded to `/root/.piper/models/`.

**1.7.0 is a breaking change from the 1.4.2 the eval tested:**

| Behavior | 1.4.2 | 1.7.0 |
|---|---|---|
| Voice auto-download on first use | yes | **removed** → `python -m piper.download_voices` |
| `--download-dir` on the `piper` command | yes | **removed** (moved to `download_voices`) |
| `--data-dir` default | — | **current working directory** |

That last row caused a live defect: `piper_tts.get_status()` returned
AVAILABLE (package imports) while `execute()` exited 1, because `_generate()`
never passed `--data-dir` and the model was not in the caller's cwd.

**Fixed** in `tools/audio/piper_tts.py` — it now resolves the voice to an
absolute `.onnx` path (`data_dir` input > `PIPER_DATA_DIR` > `~/.piper/models`)
and passes that, so generation is cwd-independent; `get_status()` additionally
requires a voice on disk; a missing voice returns the download command instead
of a traceback. Verified end-to-end from three different cwds, on both the
English and Chinese voices (16-bit mono 22050 Hz WAV out).

The F-12 contract test was **already failing before this work** — it asserted
`shutil.which("piper")`, a check the code had abandoned (correctly: pyenv shims
lie). It was rewritten to the honest precondition (importable **and** a voice on
disk), preserving its original "importable is not enough" intent.

### Correction to the table below

The engine table called Piper "concatenative ONNX". Piper is **VITS** — an
end-to-end neural model, not concatenative synthesis. The practical judgment
(flat prosody, weaker than `edge_tts`) stands; the technical label was wrong.

### Still true

No GPU on this host. Qwen3-TTS remains impractical here regardless of HF
reachability. `edge_tts` remains the default and is still fine for Chinese.

### 3. Kokoro is wired in

`tools/audio/kokoro_tts.py` (new, 2026-08-28) wraps the already-installed
`kokoro 0.9.4` (PyTorch `KPipeline`, **not** `kokoro-onnx`). Registry
auto-discovery picks it up; no `tts_selector` change was needed.

Design notes worth knowing before editing it:

- **Language is derived from the voice name.** Kokoro voices are
  `<lang><gender>_<name>`, so `zf_xiaobei` → `lang_code='z'`. Callers pass one
  field, not two; `lang_code` is still accepted as an override.
- **Default voice is `zf_xiaobei`** (Mandarin female), matching this project's
  primary narration language rather than Kokoro's English default.
- **All chunks are concatenated.** `KPipeline` splits on newlines and yields
  one chunk per segment; taking only the first silently truncates multi-line
  narration.
- **Pipelines are cached per language** on the class — building one loads the
  82M model, and a production run synthesizes one line per scene.
- **`get_status()` requires cached weights**, not just an importable package —
  the same honesty rule applied to `piper_tts` above.

Chinese needs the misaki Mandarin backend: `pip install 'misaki[zh]'`
(installed 2026-08-28 — pulls jieba, pypinyin, cn2an, ordered-set). Without it
`KPipeline(lang_code='z')` dies on `ModuleNotFoundError: ordered_set`; the tool
turns that into an actionable error naming the extra.

Weights and **all** voice packs cache to
`~/.cache/huggingface/hub/models--hexgrad--Kokoro-82M/` (~330 MB) — one
bootstrap, offline thereafter.

Verified end-to-end: Chinese (`zf_xiaobei`) and English (`af_heart`), 24 kHz
WAV, status AVAILABLE through the registry.

### 4. (NEW 2026-08-28 evening) edge_tts flipped + registry vs runtime gap

This amendment came out of running a reference-driven video pipeline
(`the-refactor-serenade`, 8-stage animated-explainer) end-to-end. Two new
facts emerged that the morning update didn't predict.

#### 4a. edge_tts is no longer the default

**Morning update** claimed edge_tts is the running default. **Evening fact:**
registry now reports `edge_tts | status=unavailable`. No source change
between these two events in the same project workspace — the flip appears to
be either a transient `edge-tts` 7.x dependency-resolution issue (silent
import error) or a Microsoft-side rate-limit / voice-list churn. The
provider was not invoked in the pipeline (Kokoro was selected at sample-first
validation), so the underlying cause was not investigated further.

**Until someone debugs `edge_tts`, treat it as offline.** Don't list it as a
fallback for new production runs; switch the default in `tts_selector`
scoring to `kokoro_tts` > `piper_tts` > `openai_tts` (paid) in that order.

#### 4b. (NEW governance rule) registry `status="available"` ≠ runtime works

This is the most important finding from this re-evaluation. During the
refactor-serenade run, the agent pulled `voicebox_tts` from the registry
(provider=voicebox, status=available), then `execute()`'d it. The HTTP API
answered 200 OK on `/health`; the engine-side qwen TTS load then died
with:

> We couldn't connect to `https://huggingface.co` to load the files, and
> couldn't find them in the cached files. ... (LocalEntryNotFoundError)

**The registry's `status="available"` is a static check — imports load,
config parses, but the engine's first network call only happens at
`generate()` time, and that is what fails.** This is invisible at the
registry probe step and breaks decision_log assumptions in
`research_brief.json` and `cost_estimate` line items that quote
voicebox voice-clone availability.

**Rule added (this amendment):** Before claiming any TTS provider is
"live" in a research_brief or cost_estimate, run a 5-second smoke
synthesis and confirm non-empty `audio_bytes`. Concretely:

```python
from tools.tool_registry import registry
registry.discover()
vb = registry._tools['voicebox_tts']
r = vb.execute({"operation": "text_to_speech", "text": "smoke",
                "profile_id": "<one-existing-from-list>",
                "engine": "qwen", "model_size": "0.6B",
                "language": "en", "timeout_seconds": 60})
assert r.success and r.data.get("audio_path"), f"voicebox smoke failed: {r.error}"
```

Then call the existing `provider_menu_summary()` only after each claimed
provider has passed this probe. The probe cost is cents of CPU seconds and
a few MB on disk; the cost of NOT probing is a broken-assumption
`decision_log` entry that contaminates the rest of the run.

**Provider-by-provider smoke status as of 2026-08-28 evening** (re-verified
in this run; all done from `/opt/OpenMontage_Voicebox` via `.venv/bin/python`):

| Provider | registry status | smoke status | note |
|---|---|---|---|
| `kokoro_tts` | available | **smoke pass** | 6 narration MP3s produced for the refactor-serenade via direct `KPipeline`, bypassing the wrapper tool |
| `piper_tts` | available | not smoke-tested this run | voices present (`en_US-lessac-medium.onnx` + `zh_CN-huayan-medium.onnx` in `/root/.piper/models/`), expected to work |
| `openai_tts` | available | not smoke-tested this run | API key present in `.env`; lowest-risk fallback |
| `voicebox_tts` | available (misleading) | **smoke fail** | qwen engine tries to fetch weights from HF; first-call fails |
| `edge_tts` | **unavailable** | — | morning update was wrong; investigate before relying on it |
| `elevenlabs_tts`, `google_tts`, `kling_tts`, `dashscope_tts`, `doubao_tts` | unavailable | — | missing API keys (unchanged) |

#### 4c. Proxy unblock has a scope

The morning update noted that `export HTTPS_PROXY=http://127.0.0.1:7890`
unblocks direct HF routes. This is verified and still true:

```
curl -x http://127.0.0.1:7890 -o /dev/null -w '%{http_code}' https://huggingface.co
→ 200
curl    https://huggingface.co
→ 000
```

**However**, this proxy only helps **agent processes that inherit
`HTTPS_PROXY`** in their env. The voicebox server process running at
`:17493` is presumably a long-lived service that was started without
that env var, so changing the agent's env does not change voicebox's
runtime — voicebox is still broken even today, even though HF is now
reachable in principle. To fully unblock voicebox qwen, someone needs
to restart the voicebox server with `HTTPS_PROXY=http://127.0.0.1:7890`
in its startup env.

**Same pattern for any HF-loading tool:** when running it from a script,
export the proxy; when calling it as a service / daemon / sidecar, fix
that service's startup env.

## TL;DR (as of 2026-08-28 evening)

- The only TTS engines that pass both registry status AND runtime smoke
  on this host today are: **`kokoro_tts`** (highest quality, recommended
  default for English) and **`piper_tts`** (offline fallback, lower quality,
  50 MB ONNX per voice).
  **[superseded 2026-08-28 (morning)]** — earlier TL;DR said "edge_tts works":
  no longer true as of evening 2026-08-28.
- `edge_tts` flipped to registry-unavailable during this run. Cause
  uninvestigated; do not list it as a runtime fallback until debugged.
- `voicebox_tts` exposes the **registry AVAILABLE ≠ runtime WORKS** gap.
  Per the new governance rule (section 4b), every TTS provider must pass
  a 5-second smoke before it is quoted in `research_brief` or `cost_estimate`.
- `openai_tts` is the only paid cloud option whose key is set; it remains a
  silent-fallback option but no longer the primary recommendation now that
  Kokoro works offline.
- HF is reachable via `:7890` proxy; agent processes inherit the env, but
  long-lived services (voicebox) do not — fix on the service-startup level.

> The original 2026-08-21 TL;DR follows below for historical context — **read
> the evening TL;DR above for current truth**:

## Environment Constraints (frozen on 2026-08-21)

| Constraint | Status |
|---|---|
| HuggingFace (`huggingface.co`) | **direct unreachable** — curl times out (3000+ ms) from this host; affects both Kokoro (`hexgrad/Kokoro-82M`) and Qwen3-TTS (`Qwen/Qwen3-TTS-12Hz-1.7B-*`) weight downloads<br>**[superseded 2026-08-28 (morning)]** — direct still times out, but `-x http://127.0.0.1:7890` returns `200`<br>**[superseded 2026-08-28 (evening)]** — verified again this run; proxy works for agent processes that inherit env, but not for long-lived services (voicebox) unless their startup env also has the proxy |
| ModelScope (`modelscope.cn` / `modelscope.ai`) | **reachable** — confirmed via browser; can pull from there |
| GPU | **none** — no `nvidia-smi`; rules out GPU-required engines for production speed (CosyVoice 2, XTTS, Bark would all be slow) |
| Microsoft Edge TTS service | **partially blocked** — `zh-CN-YunxiNeural` returns `NoAudioReceived`; `zh-CN-XiaoxiaoNeural` / `YunjianNeural` / `XiaoyiNeural` / `en-US-AvaNeural` / `en-US-AndrewNeural` all work<br>**[superseded 2026-08-28 (evening)]** — `edge_tts` provider now reports `status=unavailable` in registry (parent tool decides its own delegate). cause uninvestigated; do not list as a fallback until debugged |

## What Was Evaluated

Tested the 5 TTS-related Python packages installed in `/root/.pyenv/versions/3.11.8/`:

| Package | Version | Status on this host |
|---|---|---|
| `edge-tts` | 7.2.8 | ⚠️ **was working (morning 2026-08-28); flipped to registry-unavailable by evening 2026-08-28.** Investigation deferred; do not list as a fallback without debug |
| `piper-tts` | 1.4.2 | ⚠️ package imports cleanly (the `piper` shim has the same pyenv-bug as before, now worked around by `piper_tts.py` switching to `python -m piper`); **no ONNX voice downloaded**<br>**[superseded 2026-08-28 (morning)]** — 1.7.0 in `.venv`, voices downloaded, tool fixed, ✅ working |
| `kokoro` | 0.9.4 | ⚠️ **was failing (morning); now working.** Initial HF download via proxy succeeded; runtime smoke in the refactor-serenade pipeline produced 6 narration MP3s<br>**[superseded 2026-08-28 (morning+evening)]** — currently the recommended default |
| `qwen-tts` | 0.1.1 | ❌ installed but cannot load — CLI defaults to `--device cuda:0`; model weights not on disk; HF download would be needed; CPU inference works in theory but is slow without GPU |

Plus the system binary:

| Binary | Path | Use |
|---|---|---|
| `espeak-ng` | `/usr/bin/espeak-ng` | fully offline, very robotic; emergency fallback only |

`tools/audio/tts_selector.py` will score and dispatch among the discovered
providers. Ranking with `operation: "rank"` shows `kokoro_tts` and `piper_tts`
as AVAILABLE plus `openai_tts` (paid) and `voicebox_tts` (registry claims
available but engine-blocked at runtime — see section 4b). **Plus**
`edge_tts` (former default) is currently reported unavailable as of
2026-08-28 evening — investigation deferred. Cloud TTS (elevenlabs /
google / kling / dashscope / doubao) remains UNAVAILABLE due to missing
API keys.

## What Each Engine Would Give (if it worked)

| Engine | Quality vs edge_tts | Chinese support | Voice cloning | Offline | Footprint |
|---|---|---|---|---|---|
| `edge_tts` (current) | baseline | good (Microsoft Neural voices) | ❌ | ❌ online only | 0 (no model) |
| `piper_tts` | lower (VITS neural model, but flat prosody — the 2026-08-21 text said "concatenative", which was wrong) | yes (`zh_CN-huayan-medium` etc.) | ❌ | ✅ | ~50 MB per voice ONNX |
| `kokoro` | higher than edge_tts for English; competitive for zh | yes (lang_code='z' with `zf_xiaobei`, `zm_yunxi`, etc.) | ❌ | ✅ | 82 MB single model |
| `qwen-tts` (Qwen3-TTS) | SOTA open-source, Alibaba | strong (CustomVoice + VoiceDesign + Base variants; 1.7B params) | ✅ `VoiceClonePromptItem` in the API | ✅ | 1.7B params ≈ 3.5 GB bf16 per variant |
| `espeak-ng` | robotic | yes | ❌ | ✅ | 0 (system binary) |

## Decision Tree When the Time Comes

When the user (or a future Claude) actually needs to swap off the current
default providers (kokoro_tts + piper_tts in offline mode):

1. **If offline resilience is enough and Chinese quality OK:** add a
   Piper Chinese voice ONNX to `/root/.piper/models/` via
   `python -m piper.download_voices --download-dir /root/.piper/models <voice>`
   (needs `HTTPS_PROXY`). **Already done for `en_US-lessac-medium` and
   `zh_CN-huayan-medium` as of 2026-08-28 morning** — and `piper_tts` works
   out of the box now.

2. **If higher quality is needed and the host env has HTTPS_PROXY set:**
   Kokoro is **already wired** (see section 3) and verified end-to-end in
   production this run. Skip "decision tree when the time comes" — start
   using it directly. (Before this amend, the doc had a deferred-work
   framing; the evening amendment marks Kokoro as shipped.)

3. **If voice cloning is needed AND a GPU machine is available:** install Qwen3-TTS `CustomVoice` + `VoiceDesign` variants, route via `qwen3_tts.py` Base mode + voice prompt, expose as `qwen3_voice_clone` MCP wrapper analogous to `clone_voice`. Multi-hour effort, but replaces the (now-removed) ElevenLabs clone path with an open-source equivalent.

4. **If quality is the only goal and cost is no issue:** wire one of the cloud TTS providers (Google TTS, OpenAI TTS —) — `tools/audio/openai_tts.py` already works end-to-end with the existing `OPENAI_API_KEY`; `google_tts.py` and `elevenlabs_tts.py` exist as code but their dependency-error branch needs keys. Edge case: the Google TTS service IP block on `zh-CN-YunxiNeural` is host-specific, not a service outage — set a working default voice in `tts_selector` and move on.

## What I Did NOT Do

- Did NOT pull down any Piper voice ONNX originally (user said "先不下载 voice").
  **[superseded 2026-08-28 morning]** — voices downloaded, this list item no longer applies.
- Did NOT attempt to fix the HuggingFace reachability (network-level, not config).
- Did NOT add `kokoro_tts.py` to `tools/audio/` originally — package was
  installed but its weight load required HF access.
  **[superseded 2026-08-28 morning]** — `tools/audio/kokoro_tts.py` exists,
  registered AVAILABLE, and was used end-to-end in production this run.
- Did NOT switch the default `edge_tts` voice back to `zh-CN-YunxiNeural`
  (it's blocked here, kept the working default at the time).
  **[superseded 2026-08-28 evening]** — `edge_tts` flipped to registry-
  unavailable; default-management is now actively unclear, see section 4a.
- Did NOT install Qwen3-TTS route or restart voicebox with HTTPS_PROXY
  (both deferred to "GPU machine" and "operator action" respectively).

## Pointer Files

- `tools/audio/tts_selector.py` — provider dispatcher (auto-discoversits
  from registry, no code changes needed when adding new providers).
- `tools/audio/edge_tts.py` — was current default; **flipped to registry-
  unavailable 2026-08-28 evening** — investigate before relying.
- `tools/audio/piper_tts.py` — fixed 2026-08-28 morning (absolute voice-path
  resolution, `PIPER_DATA_DIR`, honest `get_status()`).
- `tools/audio/kokoro_tts.py` — shipped 2026-08-28 morning, wraps the
  already-installed `kokoro 0.9.4` (PyTorch `KPipeline`, **not** `kokoro-onnx`).
  Per `tts_selector` auto-discovery, picks up without code changes elsewhere.
- `/root/.piper/models/` — downloaded Piper voices (not in git, ~63 MB each).
- `mcp_server.py:706-750` — `@mcp.tool() edge_tts` dedicated MCP wrapper
  added 2026-08-21.
- `docs/whisper-availability-assessment-2026-08-28.md` — companion
  documentation for the ASR side of the pipeline (`faster-whisper-base`
  via local HF cache + `HF_HUB_OFFLINE=1`); relevant because it's the
  workaround that **unblocked the caption-burn post-step** of the
  refactor-serenade pipeline.
- `.gitignore` — `mcp_server.log` excluded (runtime log dumps are not in
  git; this assessment is).

## Reproduction Commands

```bash
# Check what's installed
/root/.pyenv/versions/3.11.8/bin/pip list 2>&1 | grep -iE "tts|piper|cosy|fish|bark|silero|kokoro|openvoice|coqui|parler|edge|elevenlabs|qwen-tts"

# Check what's reachable
for u in huggingface.co www.modelscope.cn api.github.com speech.microsoft.com; do
  echo "$u: $(curl -s -o /dev/null -w '%{http_code}' --max-time 4 $u)"
done

# Rank TTS providers via MCP
SID=$(curl -s -i --max-time 5 -X POST http://127.0.0.1:8900/mcp \
  -H "Authorization: Bearer $MCP_API_TOKEN" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"x","version":"1"}}}' \
  | awk -F': ' '/^mcp-session-id:/ {gsub(/\r/,""); print $2}')
curl ... -H "Mcp-Session-Id: $SID" -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"execute_tool","arguments":{"tool_name":"tts_selector","inputs":{"operation":"rank"}}}}'
```
