# Local Open-Source TTS Alternatives — Assessment (2026-08-21)

> One-time eval, frozen. Re-read when someone wants to optimize the local TTS
> path on this host. Last assessed by Claude (MiniMax-M3) on 2026-08-21,
> during the work to expose `edge_tts` over MCP :8900.
>
> **Amended 2026-08-28** — the original assessment below is kept as the
> 2026-08-21 snapshot, but two of its load-bearing facts have since changed.
> Read the update section first; where the old text is now wrong it is marked
> inline with **[superseded 2026-08-28]**.

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

## TL;DR (as of 2026-08-21)

- The only TTS engine **currently working on this machine** is `edge_tts`
  (Microsoft Edge TTS, free online wrapper).
  **[superseded 2026-08-28]** — `piper_tts` now works too.
- Two higher-quality open-source engines — **Kokoro** and **Qwen3-TTS** —
  are **installed but cannot load their model weights** because this host
  cannot reach `huggingface.co` (network-level block, not a code/config bug).
  **[superseded 2026-08-28]** — HF is reachable via the `:7890` proxy; Kokoro
  is unblocked (Qwen3-TTS still ruled out by the missing GPU).
- The other realistic local option, **Piper**, is installed and the package
  imports cleanly but **no voice model has been downloaded yet** (would
  pull from ModelScope, which IS reachable).
  **[superseded 2026-08-28]** — voices downloaded, tool fixed, working.
- Recommendation: **wait until TTS becomes a bottleneck** (Microsoft rate-
  limits / IP blocks more voices, or production needs offline-only) before
  investing in this. Current quality from `edge_tts` is acceptable for
  Chinese narration.

## Environment Constraints (frozen on 2026-08-21)

| Constraint | Status |
|---|---|
| HuggingFace (`huggingface.co`) | **unreachable** — curl times out (3000+ ms) from this host; affects both Kokoro (`hexgrad/Kokoro-82M`) and Qwen3-TTS (`Qwen/Qwen3-TTS-12Hz-1.7B-*`) weight downloads<br>**[superseded 2026-08-28]** — direct still times out, but `-x http://127.0.0.1:7890` returns `200` |
| ModelScope (`modelscope.cn` / `modelscope.ai`) | **reachable** — confirmed via browser; can pull from there |
| GPU | **none** — no `nvidia-smi`; rules out GPU-required engines for production speed (CosyVoice 2, XTTS, Bark would all be slow) |
| Microsoft Edge TTS service | **partially blocked** — `zh-CN-YunxiNeural` returns `NoAudioReceived`; `zh-CN-XiaoxiaoNeural` / `YunjianNeural` / `XiaoyiNeural` / `en-US-AvaNeural` / `en-US-AndrewNeural` all work |

## What Was Evaluated

Tested the 5 TTS-related Python packages installed in `/root/.pyenv/versions/3.11.8/`:

| Package | Version | Status on this host |
|---|---|---|
| `edge-tts` | 7.2.8 | ✅ works (current default for `tts_selector`) |
| `piper-tts` | 1.4.2 | ⚠️ package imports cleanly (the `piper` shim has the same pyenv-bug as before, now worked around by `piper_tts.py` switching to `python -m piper`); **no ONNX voice downloaded**<br>**[superseded 2026-08-28]** — 1.7.0 in `.venv`, voices downloaded, tool fixed, ✅ working |
| `kokoro` | 0.9.4 | ❌ installed but cannot load — `KPipeline(lang_code='a'/'z')` triggers HF download, retries 5× then `LocalEntryNotFoundError` |
| `qwen-tts` | 0.1.1 | ❌ installed but cannot load — CLI defaults to `--device cuda:0`; model weights not on disk; HF download would be needed; CPU inference works in theory but is slow without GPU |

Plus the system binary:

| Binary | Path | Use |
|---|---|---|
| `espeak-ng` | `/usr/bin/espeak-ng` | fully offline, very robotic; emergency fallback only |

`tools/audio/tts_selector.py` will score and dispatch among the discovered
providers. Ranking with `operation: "rank"` shows only `edge_tts` and
`piper_tts` as AVAILABLE — every cloud TTS (elevenlabs/google/openai/
kling/dashscope/doubao) is UNAVAILABLE due to missing API keys.

## What Each Engine Would Give (if it worked)

| Engine | Quality vs edge_tts | Chinese support | Voice cloning | Offline | Footprint |
|---|---|---|---|---|---|
| `edge_tts` (current) | baseline | good (Microsoft Neural voices) | ❌ | ❌ online only | 0 (no model) |
| `piper_tts` | lower (VITS neural model, but flat prosody — the 2026-08-21 text said "concatenative", which was wrong) | yes (`zh_CN-huayan-medium` etc.) | ❌ | ✅ | ~50 MB per voice ONNX |
| `kokoro` | higher than edge_tts for English; competitive for zh | yes (lang_code='z' with `zf_xiaobei`, `zm_yunxi`, etc.) | ❌ | ✅ | 82 MB single model |
| `qwen-tts` (Qwen3-TTS) | SOTA open-source, Alibaba | strong (CustomVoice + VoiceDesign + Base variants; 1.7B params) | ✅ `VoiceClonePromptItem` in the API | ✅ | 1.7B params ≈ 3.5 GB bf16 per variant |
| `espeak-ng` | robotic | yes | ❌ | ✅ | 0 (system binary) |

## Decision Tree When the Time Comes

When the user (or a future Claude) actually needs to swap off `edge_tts`:

1. **If just wanting offline resilience with Chinese quality OK:** ~~download a
   Piper Chinese voice ONNX from ModelScope~~ **[done 2026-08-28]** —
   `en_US-lessac-medium` and `zh_CN-huayan-medium` are in
   `/root/.piper/models/`, and `piper_tts` works out of the box. Add more
   voices with `python -m piper.download_voices --download-dir
   /root/.piper/models <voice>` (needs `HTTPS_PROXY`).

2. **If HF connectivity can be restored:** **[unblocked 2026-08-28 — this is
   now the top recommendation]** `pip install kokoro` and the existing
   `kokoro` package will just work on first call (auto-downloads
   `hexgrad/Kokoro-82M`, ~82 MB, via the `:7890` proxy). Then add
   `kokoro_tts.py` to `tools/audio/`, register `capability="tts"`, and
   `tts_selector` will pick it up via auto-discovery. ~1 hour. Kokoro beats
   both `edge_tts` and `piper_tts` on quality and needs no per-voice
   downloads (one 82 MB model carries every voice as a style vector).

3. **If voice cloning is needed AND a GPU machine is available:** install Qwen3-TTS `CustomVoice` + `VoiceDesign` variants, route via `qwen3_tts.py` Base mode + voice prompt, expose as `qwen3_voice_clone` MCP wrapper analogous to `clone_voice`. Multi-hour effort, but replaces the (now-removed) ElevenLabs clone path with an open-source equivalent.

4. **If quality is the only goal and cost is no issue:** wire one of the cloud TTS providers (Google TTS, OpenAI TTS —) — both already have working `tools/audio/google_tts.py` / `openai_tts.py`, just need API keys in `.env`.

## What I Did NOT Do

- Did NOT pull down any Piper voice ONNX (user said "先不下载 voice").
- Did NOT attempt to fix the HuggingFace reachability (network-level, not config).
- Did NOT add `kokoro_tts.py` / `qwen3_tts.py` to `tools/audio/` — package is installed but cannot load weights in this environment.
- Did NOT switch the default `edge_tts` voice back to `zh-CN-YunxiNeural` (it's blocked here, kept the working default).

## Pointer Files

- `tools/audio/tts_selector.py` — provider dispatcher (auto-discoversits
  from registry, no code changes needed when adding new providers).
- `tools/audio/edge_tts.py` — currently the only working provider.
  **[superseded 2026-08-28]** — `piper_tts` works too.
- `tools/audio/piper_tts.py` — fixed 2026-08-28 (absolute voice-path
  resolution, `PIPER_DATA_DIR`, honest `get_status()`).
- `/root/.piper/models/` — downloaded Piper voices (not in git, ~63 MB each).
- `mcp_server.py:706-750` — `@mcp.tool() edge_tts` dedicated MCP wrapper
  added 2026-08-21.
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