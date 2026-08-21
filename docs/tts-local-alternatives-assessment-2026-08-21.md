# Local Open-Source TTS Alternatives — Assessment (2026-08-21)

> One-time eval, frozen. Re-read when someone wants to optimize the local TTS
> path on this host. Last assessed by Claude (MiniMax-M3) on 2026-08-21,
> during the work to expose `edge_tts` over MCP :8900.

## TL;DR

- The only TTS engine **currently working on this machine** is `edge_tts`
  (Microsoft Edge TTS, free online wrapper).
- Two higher-quality open-source engines — **Kokoro** and **Qwen3-TTS** —
  are **installed but cannot load their model weights** because this host
  cannot reach `huggingface.co` (network-level block, not a code/config bug).
- The other realistic local option, **Piper**, is installed and the package
  imports cleanly but **no voice model has been downloaded yet** (would
  pull from ModelScope, which IS reachable).
- Recommendation: **wait until TTS becomes a bottleneck** (Microsoft rate-
  limits / IP blocks more voices, or production needs offline-only) before
  investing in this. Current quality from `edge_tts` is acceptable for
  Chinese narration.

## Environment Constraints (frozen on 2026-08-21)

| Constraint | Status |
|---|---|
| HuggingFace (`huggingface.co`) | **unreachable** — curl times out (3000+ ms) from this host; affects both Kokoro (`hexgrad/Kokoro-82M`) and Qwen3-TTS (`Qwen/Qwen3-TTS-12Hz-1.7B-*`) weight downloads |
| ModelScope (`modelscope.cn` / `modelscope.ai`) | **reachable** — confirmed via browser; can pull from there |
| GPU | **none** — no `nvidia-smi`; rules out GPU-required engines for production speed (CosyVoice 2, XTTS, Bark would all be slow) |
| Microsoft Edge TTS service | **partially blocked** — `zh-CN-YunxiNeural` returns `NoAudioReceived`; `zh-CN-XiaoxiaoNeural` / `YunjianNeural` / `XiaoyiNeural` / `en-US-AvaNeural` / `en-US-AndrewNeural` all work |

## What Was Evaluated

Tested the 5 TTS-related Python packages installed in `/root/.pyenv/versions/3.11.8/`:

| Package | Version | Status on this host |
|---|---|---|
| `edge-tts` | 7.2.8 | ✅ works (current default for `tts_selector`) |
| `piper-tts` | 1.4.2 | ⚠️ package imports cleanly (the `piper` shim has the same pyenv-bug as before, now worked around by `piper_tts.py` switching to `python -m piper`); **no ONNX voice downloaded** |
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
| `piper_tts` | lower (concatenative ONNX, no neural expressiveness) | yes (`zh_CN-huayan-medium` etc.) | ❌ | ✅ | ~50 MB per voice ONNX |
| `kokoro` | higher than edge_tts for English; competitive for zh | yes (lang_code='z' with `zf_xiaobei`, `zm_yunxi`, etc.) | ❌ | ✅ | 82 MB single model |
| `qwen-tts` (Qwen3-TTS) | SOTA open-source, Alibaba | strong (CustomVoice + VoiceDesign + Base variants; 1.7B params) | ✅ `VoiceClonePromptItem` in the API | ✅ | 1.7B params ≈ 3.5 GB bf16 per variant |
| `espeak-ng` | robotic | yes | ❌ | ✅ | 0 (system binary) |

## Decision Tree When the Time Comes

When the user (or a future Claude) actually needs to swap off `edge_tts`:

1. **If just wanting offline resilience with Chinese quality OK:** download a Piper Chinese voice ONNX from ModelScope (e.g., `zh_CN-huayan-medium`), set `PIPER_VOICE_MODEL` env or pass to `piper_tts.execute({"model": "..."})`, add a `voice_download` helper. Half-day effort.

2. **If HF connectivity can be restored:** `pip install kokoro` and the existing `kokoro` package will just work on first call (auto-downloads `hexgrad/Kokoro-82M`, ~82 MB). Then add `kokoro_tts.py` to `tools/audio/`, register `capability="tts"`, and `tts_selector` will pick it up via auto-discovery. ~1 hour.

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