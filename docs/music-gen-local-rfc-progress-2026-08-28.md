# music_gen_local — In-Flight Progress Snapshot (2026-08-28, late evening, post-pause)

> Pause-and-resume handoff. Picked up by a scheduled task at 2026-08-29 01:00.
> Read this first when resuming.

## TL;DR — STATE AT PAUSE+1

RFC ✅. Tool ✅. All 5 test files ✅ (19/19 tests green). Loop algorithm bug
patched in production ✅. ToolStatus refactor verified ✅ (`tests/tools/` shows
**395 passed, 6 skipped, 0 failed**). Everything builds clean.

## What landed (verified ✅)

| Deliverable | Path | Verified by |
|---|---|---|
| RFC | `docs/music-gen-local-rfc-2026-08-28.md` | file exists, 10 sections |
| Tool implementation | `tools/audio/music_gen_local.py` | `py_compile` OK, lazy imports verified, **loop patch applied** |
| Test: status (4) | `tests/tools/test_music_gen_local_status.py` | 4/4 ✅ |
| Test: device (4) | `tests/tools/test_music_gen_local_device.py` | 4/4 ✅ |
| Test: output_format (3) | `tests/tools/test_music_gen_local_output_format.py` | 3/3 ✅ |
| Test: force_instrumental (3) | `tests/tools/test_music_gen_local_force_instrumental.py` | 3/3 ✅ |
| Test: loop (5) | `tests/tools/test_music_gen_local_loop.py` | **5/5 ✅ (agent C)** |
| Wiring: fallback | `tools/audio/music_gen.py` | `MusicGen.fallback_tools == ['music_gen_local']` |
| Wiring: docs | `docs/PROVIDERS.md` | `grep -c music_gen_local` == 2 |
| Wiring: Makefile | `Makefile` | `make -n musicgen-fetch` resolves |
| ToolStatus refactor | `tools/base_tool.py` | **395 passed / 0 failed in tests/tools/** |
| Loop patch | `tools/audio/music_gen_local.py:_loop_to_duration` | tail-termination guard added |

## Loop patch (already applied — for the record)

Bug: when `take == crossfade_samples` in the inner while-loop, `next_chunk[crossfade_samples:]`
was empty and `out[:-crossfade_samples]` + overlap of equal length cancelled
exactly → infinite loop. Patched with an `if take <= crossfade_samples: out = np.concatenate([out, next_chunk]); continue` guard before the crossfade math. The production code now matches Agent C's reference fix; the comment in the source points to "RFC §4.3 patch, 2026-08-28".

## What is pending (small)

### 1. RFC §6 step 5 — end-to-end pipeline exercise (NOT RUN)

A real `cinematic` or `animated-explainer` pipeline run with
`ELEVENLABS_API_KEY` unset, to exercise the
ElevenLabs-unavailable → music_gen_local fallback chain on a real project.
**Skip if** no GPU / no internet / weights not cached (300 MB download).

### 2. `make musicgen-fetch` — NOT RUN

Downloads MusicGen weights to `~/.cache/huggingface/hub/`. Only useful if a
pipeline exercise is planned.

### 3. RFC status flip — Draft → Accepted

Final commit-level housekeeping. Done by appending a `## Acceptance` section
to the RFC (date + "all 19 contract tests green + ToolStatus refactor
backward-compatible + loop patch in production").

## Decision ledger (still in force)

From the RFC §9 (accepted by the user as RFC proposals):

1. **transformers missing** → always register; `get_status()` returns
   UNAVAILABLE with reason.
2. **melody variant** → keep in enum, document as not-yet-wired.
3. **loop + vocal** → hard fail, error mentions `suno_music`.

## Resume checklist (1 AM task — short, just verify + flip)

```bash
# 1. Sanity: full tool test suite still green
cd /opt/OpenMontage_Voicebox
python -m pytest tests/tools/ 2>&1 | tail -3
# Expected: 395 passed, 6 skipped (or close — count may grow if new tests
# landed overnight), 0 failed.

# 2. (optional, if GPU + internet) warm weights
make musicgen-fetch

# 3. (optional, if env supports) pipeline smoke
# python -m backlot open <project-id>  # requires a fresh project
# OR: run a tiny cinematic / animated-explainer pipeline with
# ELEVENLABS_API_KEY unset to exercise the fallback chain.

# 4. Flip RFC status: append an "## Acceptance" section to
#    docs/music-gen-local-rfc-2026-08-28.md with date + test summary.
```

## Files that exist after this session

```
docs/music-gen-local-rfc-2026-08-28.md                            # RFC ✅
docs/music-gen-local-rfc-progress-2026-08-28.md                   # this file
tools/audio/music_gen_local.py                                    # tool ✅ + loop patch ✅
tools/audio/music_gen.py                                          # +fallback_tools ✅
tools/base_tool.py                                                # ToolStatus refactored ✅
docs/PROVIDERS.md                                                 # +Local Providers section ✅
Makefile                                                          # +musicgen-fetch target ✅
tests/tools/test_music_gen_local_status.py                        # 4 tests ✅
tests/tools/test_music_gen_local_device.py                        # 4 tests ✅
tests/tools/test_music_gen_local_output_format.py                 # 3 tests ✅
tests/tools/test_music_gen_local_force_instrumental.py            # 3 tests ✅
tests/tools/test_music_gen_local_loop.py                          # 5 tests ✅
```

## What I would tell the user at resume

```
Resume of music_gen_local RFC + implementation:

| Stage                                          | State |
| Tool impl (music_gen_local.py)                 | ✅    |
| Loop algorithm patch (RFC §4.3 tail bug)       | ✅    |
| Tests: status (4)                              | ✅    |
| Tests: device (4)                              | ✅    |
| Tests: output_format (3)                       | ✅    |
| Tests: force_instrumental (3)                  | ✅    |
| Tests: loop (5)                                | ✅    |
| Wiring: fallback + docs + Makefile             | ✅    |
| ToolStatus refactor (25-regression fix)        | ✅    |
| pytest tests/tools/ all-green                  | ✅ 395/0 |
| Pipeline exercise                              | ?     |
| RFC status Draft → Accepted                    | ?     |
```

Then ask: do the pipeline exercise (probably skip on a CPU-only host), or
flip the RFC status and call it done?