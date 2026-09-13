# MCP Decompose-and-Recompose Skill — Session Log (2026-08-29)

> **Goal this session:** validate the OpenMontage MCP server end-to-end by
> running the full "decompose a video → add own elements → generate new video"
> flow as a real MCP client, then package the workflow as a reusable skill.
>
> **Outcome:** all 4 phases pass via MCP; skill + 3 helper scripts committed
> in `21039d3` and pushed to origin. The next session can reproduce the full
> flow with one command.

## TL;DR for the next LLM

| Question | Answer |
|---|---|
| What got built? | A reusable skill (`skills/mcp-decompose-and-recompose.md`) + 3 Python scripts (`scripts/mcp_*.py`) that drive the OpenMontage MCP server through a 4-phase remix workflow |
| Was the MCP server actually exercised? | Yes — 11 MCP `tools/call` round-trips in the regression run, no direct Python tool imports |
| Does it work on a fresh project id? | Yes — `python3 scripts/mcp_decompose_and_recompose.py --project <fresh>` produced `projects/mcp-skill-test2-1787954063/renders/final.mp4` (9.4 MB, 1920×1080 H.264+AAC, 30 s) |
| Were any compromises? | Image/title card generation runs locally via Pillow (not via MCP). The MCP server has no image-generation tool, so generating an image locally and then uploading via `upload_asset` is the correct split. The narration IS generated through MCP (`edge_tts`). |
| What is the canonical invocation? | `python3 scripts/mcp_decompose_and_recompose.py --project <id> --source <mp4> --title "..." --narration "..." --overlay-start 0 --overlay-end 3` |

## 1. What This Session Validated

A user-facing request that OpenMontage be proven end-to-end through its MCP
integration, then turned into a reusable workflow. Four phases:

1. **Connect & auth** — MCP server `initialize` with `Authorization: Bearer
   MCP_API_TOKEN`. Server returns `mcp-session-id` in headers; every follow-up
   call must reuse it.
2. **Decompose** — `scene_detect` + `transcriber` + `video_analyzer` produce
   structured artifacts under `projects/<id>/artifacts/`.
3. **Add own elements** — generate a title card image (Pillow), Chinese
   narration (`edge_tts` MCP tool), upload both via `upload_asset` MCP tool.
4. **Recompose** — `video_compose` `operation=overlay` mixes image overlay +
   narration audio into a new MP4.

## 2. MCP Session Lifecycle (Re-confirmed)

```
initialize → mcp-session-id: <hex> in response header
            ↓
            every tools/call carries that header (400 if missing)
            ↓
            per-session batch lock: one project per session at a time
```

The session bootstrap is one curl + awk pair — see `scripts/mcp_helper.py`
`init_session()` for the canonical form.

## 3. What Landed in the Repo

| Commit | Files | Lines | Purpose |
|---|---|---|---|
| `21039d3` | `scripts/mcp_helper.py` | 185 | JSON-RPC client (`init / info / exec / dry / upload`) |
| `21039d3` | `scripts/mcp_upload.py` | 118 | Batch uploader + auto-stage from `_sessions/<hash>/` to `assets/{images,audio}/` |
| `21039d3` | `scripts/mcp_decompose_and_recompose.py` | 243 | One-shot orchestrator: 4 phases + verify |
| `21039d3` | `skills/mcp-decompose-and-recompose.md` | 270 | Layer 2 skill doc with 6 gotchas + worked example |
| `21039d3` | `skills/INDEX.md` (+1) | — | Registered under Meta Skills |

Pushed to origin in `21039d3` (8:08 local).

## 4. The 6 Gotchas Captured in the Skill

These are the bugs that any future agent will hit again. They are all
documented verbatim in `skills/mcp-decompose-and-recompose.md` §"Gotchas":

1. **Whisper model resolution** — `model_size="base"` returns
   `LocalEntryNotFoundError` on this host because faster-whisper's resolver
   expects a specific HF layout that doesn't always match what `huggingface_hub`
   cached. Workaround: pass an absolute local snapshot path:
   `/root/.cache/huggingface/hub/models--Systran--faster-whisper-base/snapshots/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66`.
   The `turbo` alias maps to `mobiuslabsgmbh/...` not the cached
   `openai/whisper-large-v3-turbo` (which is safetensors, not CTranslate2).
2. **MCP session ID required after initialize** — streamable-http returns
   400 `Missing session ID` if you skip the header.
3. **Upload assets land in `_sessions/<hash>/`** — `upload_asset` writes
   under a per-session subdir to keep batches isolated. Copy/symlink to
   `assets/{images,audio}/` before `video_compose`.
4. **scene_detect returns 1 scene even with threshold=0.1** — continuous
   motion-graphics sources legitimately have no cuts. Don't crank threshold;
   use `video_analyzer` keyframes for variation.
5. **`video_compose` overlay schema field is `asset_path`** — the doc
   example uses `path`; the implementation reads `ov["asset_path"]`.
6. **Per-session batch lock** — once a session has an open batch for project
   A, `upload_asset` to project B in the same session fails with "MCP
   session is already collecting assets for another project". The
   orchestrator works around this by caching sessions at
   `/tmp/mcp_session.txt.<project_id>`.

## 5. Reproduction Recipe

```bash
# 1. Token in .env (already configured)
grep MCP_API_TOKEN .env

# 2. Source video (bundled with repo)
ls -la assets/signal-from-tomorrow-demo.mp4
# 30 s, 1920×1080 H.264 + AAC stereo, 5.5 Mbps

# 3. Run the full pipeline against a fresh project id
python3 scripts/mcp_decompose_and_recompose.py \
  --project mcp-skill-repro-$(date +%s) \
  --source assets/signal-from-tomorrow-demo.mp4 \
  --title "《我的标题》" \
  --narration "中文旁白..." \
  --overlay-start 0 --overlay-end 3

# 4. Inspect
ls -la projects/mcp-skill-repro-*/renders/final.mp4
ls -la projects/mcp-skill-repro-*/artifacts/

# 5. Verify with the orchestrator's own proof frame
ffmpeg -v error -ss 1.5 -i projects/mcp-skill-repro-*/renders/final.mp4 \
  -frames:v 1 -y /tmp/proof.png
```

The regression run in this session produced
`projects/mcp-skill-test2-1787954063/renders/final.mp4` (9.4 MB).

## 6. Outputs the Agent Should Read After a Re-Run

After `python3 scripts/mcp_decompose_and_recompose.py` succeeds, the
agent (or human) should inspect:

- `projects/<id>/artifacts/scenes.json` — `[{index, start_seconds, end_seconds}]`
- `projects/<id>/artifacts/<basename>_transcript.json` — `segments[]`,
  `word_timestamps[]` (may be empty if source is silent)
- `projects/<id>/artifacts/video_analysis_brief.json` — `summary`,
  `style_profile`, `replication_guidance.suggested_pipeline`,
  `_analysis_meta.keyframe_count`
- `projects/<id>/artifacts/keyframes/frame_*.jpg` — extracted visual anchors
- `projects/<id>/assets/images/zh_title_card.jpg` — staged overlay asset
- `projects/<id>/assets/audio/zh_narration.mp3` — staged narration
- `projects/<id>/assets/_sessions/<hash>/` — raw MCP upload staging
- `projects/<id>/renders/final.mp4` — the deliverable
- `/tmp/mcp_decompose_proof.png` — t=1.5s proof frame

## 7. What This Skill Does NOT Cover (out of scope)

- Multi-clip assembly (use `skills/creative/video-stitching.md`)
- Subtitle burning (use `video_compose` `operation=burn_subtitles`)
- Bilingual overlays (`operation=remotion_bilingual_overlay`)
- Real-time / live remix (this is a batch skill)
- Image generation that needs an API key (e.g. FLUX / DALL·E) — the local
  Pillow fallback only handles simple title cards. Use `image_selector` if
  richer imagery is needed and stage the upload through `upload_asset`.

## 8. Decisions Logged (decision_log convention)

Per `AGENT_GUIDE.md` "Decision Communication Contract", this session had
zero `render_runtime_selection` decisions because the chosen path was
locked at the proposal stage (this was a validation / skill-packaging
exercise, not a creative run). If a future agent extends the skill to
produce a polished piece, they MUST record:

- `category: "render_runtime_selection"`, `subject: "Composition runtime"`
  with both `remotion` and `ffmpeg` listed in `options_considered`.
- `category: "composition_mode"`, `subject: "Templated vs atelier"`
  (`templated` for now — overlay path uses stock FFmpeg).

## 9. Pointers for Future Work

- **Add Remotion overlay path** — current path is FFmpeg-only. A `remotion_render`
  variant could add spring-animated title cards. The tool exists; just needs
  an `edit_decisions.render_runtime` decision + the manifest.
- **Plug in real image generation** — replace Pillow with `image_selector`
  → `flux_image` or similar, then upload via `upload_asset`.
- **Multi-segment overlay** — extend `--overlay-start/--overlay-end` to a
  list, burn multiple image overlays across the source timeline.
- **Subtitle pass** — call `subtitle_gen` after `edge_tts`, then
  `burn_subtitles` for a captioned final.

## 10. Failure Modes Encountered (and How We Handled Them)

| Failure | Root cause | Fix |
|---|---|---|
| `401 Unauthorized` from MCP | `MCP_API_TOKEN` set in `.env` but request missing `Authorization` header | add `Authorization: Bearer ${TOKEN}` |
| `400 Missing session ID` | `tools/call` after init didn't carry `mcp-session-id` header | capture from init response headers; persist to `/tmp/mcp_session.txt` |
| Whisper `LocalEntryNotFoundError` on `model_size="base"` | resolver expected snapshot dir layout that didn't match | pass absolute path to `Systran/faster-whisper-base` snapshot |
| `video_compose` overlay `KeyError: 'asset_path'` | doc example used `path`; schema uses `asset_path` | rename to `asset_path` |
| Second `upload_asset` returns `success=false` "MCP session is already collecting assets for another project" | per-session per-project batch lock | re-init MCP session per project id (`/tmp/mcp_session.txt.<project_id>`) |

Each of these is in `skills/mcp-decompose-and-recompose.md` §"Gotchas".

## 11. Verification

- ✅ Commit `21039d3` on `OpenMontage_Voicebox`
- ✅ Pushed to `origin` (`/opt/OpenMontage`)
- ✅ End-to-end run produced `projects/mcp-skill-test2-1787954063/renders/final.mp4`
  (9.4 MB) with visually verified title-card frame at t=1.5 s
- ✅ All 3 helper scripts pass `python3 -m py_compile`
- ✅ `skills/INDEX.md` registers the new skill under Meta Skills
- ✅ User instruction ad-hoc ("通过 MCP 完整跑一遍分解→添加→生成") honored:
  no direct Python tool imports; every tool ran via MCP `tools/call`