# When to use external text-to-video (T2V) services in OpenMontage

> **Status:** stable decision framework. Define when external T2V/I2V is the right tool, vs. when OM's native composition stack already covers the visual.
> **Owner:** OpenMontage agent contract (`AGENT_GUIDE.md` § "Rule Zero" + § "Critical Rule: Motion-Required Requests").
> **Audience:** agents and humans deciding per-shot whether to spend an external-generation quota slot.
> **Date:** 2026-09-10.

## TL;DR

**Default: don't use external T2V.** OM's native composition stack (Remotion / HyperFrames / FFmpeg) + 12 stock `cut.type` scene types + Ink Theater / Manim / D3 / avatar / synthetic screen recording already covers the majority of "video" visual needs without burning a quota slot. External T2V is a **specialty tool that earns its cost only when the brief binds the visual to real-world motion that native can't credibly fake**. Otherwise: native → stock footage → I2V → T2V (last resort).

## The decision in one diagram

```
                  ┌─────────────────────────────┐
                  │ Need photoreal motion?      │
                  │ (real people / objects /    │
                  │ environments in motion)    │
                  └──────────┬──────────────────┘
                             │
                ┌────────────┴────────────┐
                │                         │
              NO                          YES
                │                         │
                ▼                         ▼
   ┌────────────────────────┐   ┌──────────────────────────┐
   │ Native composition     │   │ Does brief promise      │
   │ (Remotion stock /      │   │ this real-world motion  │
   │  HyperFrames / Manim / │   │ as its core deliverable?│
   │  Ink / stock footage / │   └──────────┬───────────────┘
   │  avatar / synthetic    │              │
   │  screen recording)     │     ┌────────┴────────┐
   └────────────────────────┘     │                  │
                                NO                  YES
                                  │                  │
                                  ▼                  ▼
                        ┌──────────────┐   ┌──────────────────────┐
                        │ Stock        │   │ Have a usable        │
                        │ (pexels /    │   │ first_frame image?   │
                        │  pixabay)    │   └──────────┬───────────┘
                        └──────────────┘              │
                                          ┌──────────┴──────────┐
                                          │                     │
                                          YES                   NO
                                          │                     │
                                          ▼                     ▼
                                    ┌──────────┐         ┌──────────┐
                                    │ I2V      │         │ T2V      │
                                    │ (anchor  │         │ (last    │
                                    │  frame)  │         │ resort)  │
                                    └──────────┘         └──────────┘
```

The two gating questions are **photoreal motion** AND **brief-bound delivery promise**. Both must be YES to enter the I2V/T2V branch at all.

## What OM already covers without external T2V

The "default → native" branch above resolves to one of these paths. None of them consume a quota slot.

| Visual form | Native tool | When to prefer |
|---|---|---|
| Text cards / stat cards / data charts | Remotion `text_card` `stat_card` `bar_chart` `line_chart` `pie_chart` `kpi_grid` `comparison` `callout` (12 stock `cut.type` scene types — see `remotion-composer/SCENE_TYPES.md`) | Any data- / concept- / number-driven content |
| Abstract motion graphics / kinetic typography | Remotion + GSAP / framer-motion | Brand stingers, product narratives, launch teasers |
| HTML / CSS animation | HyperFrames (registry blocks) | Website-to-video, product promos |
| Hand-drawn doodle / character acting | Ink Theater + Ink Puppet (`skills/creative/ink-theater.md`) | "Sketch that comes to life", "stick figure that explains X" |
| Math / science visualization | Manim (3b1b style, `manimce-best-practices`) | Formula derivation, geometric demos, algorithm walkthroughs |
| Interactive / custom data viz | D3 (`d3-viz`) | Dashboards, complex bespoke charts |
| Real presenter / digital avatar | HeyGen avatar / lip-sync (`avatar-video`, `heygen`) | Spokesperson videos, tutorials |
| Terminal / CLI demo | Remotion `TerminalScene` — **synthetic** screen recording (deterministic, privacy-safe) | Install tutorials, command-line walkthroughs |
| Generic B-roll (city / nature / office / abstract) | Pexels / Pixabay stock footage | Most neutral sensory footage |
| Cartoon character continuous motion | `character-animation` pipeline + SVG rig + pose library | Short skits, repeating action sequences |

If the brief resolves to any of these, stop here. Don't burn quota.

## When external T2V/I2V earns its cost

Two conditions must both hold.

### Condition 1: the visual IS real-world motion, not conceptual expression

- Real objects moving in real environments (luggage handle being pulled, car driving through rain, coffee pouring with splash)
- Real people doing real actions (chef tossing a pan, athlete sprinting, child running)
- Real lighting / texture / physics (skin, fabric, metal reflections, depth-of-field, lens motion)

Remotion stock types and stock footage libraries can't supply these — stock doesn't have it, or animation visibly looks "drawn" and clashes with the brief's `delivery_promise`.

### Condition 2: the brief's promise is bound to that kind of shot

| Pipeline / form | Why T2V is nearly irreplaceable |
|---|---|
| `cinematic` (trailer / teaser / mood-led) | Brief promises "cinematic shot stream" — composited animation makes it read as "corporate promo" the moment it appears |
| `video-template-remix` where the reference video itself is T2V-derived | When swapping asset slots of a reference whose shots are AI-generated, only matching capability regenerates them |
| `hybrid` background plates needing real environments | Live-action compositing needs real material underneath |
| Real-product / real-scene endorsement clips | "Watch this product used in a real scene" — b-roll must be real material |
| Hero / launch / brand-defining shots | One-off shots that must be original and stand out — stock can't carry the brand weight |

**Strong signals** that should trigger T2V consideration:

- User provides a reference video and says "match this texture / feel"
- Brief uses words like photoreal / cinematic / 实拍 / live-action / real scene / authentic
- Shot script describes **camera physics** (push / pull / pan / tilt / dolly / depth-of-field shifts) — T2V / I2V strength band
- I2V first-frame lock needed (product photo, character plate, key art) — text can't carry that consistency

## T2V vs I2V vs multi-image reference

See [`t2v-i2v-multi-image-reference-glossary.md`](t2v-i2v-multi-image-reference-glossary.md) for canonical definitions. Decision rule:

- **Have a reference image?** → **I2V** (first choice). Brand consistency is dramatically better than text guessing.
- **No image, generating from scratch?** → **T2V** (last resort). Drift risk; expect to iterate prompts.
- **Need multi-image joint conditioning** (character + scene + prop locked across shots)? → **multi-image reference** model. Note: `minimax_video_direct` (the fal.ai-routed Hailuo path on this host) does **not** support multi-image reference. This is a capability boundary, not a quality judgment — see glossary.

## When to actively AVOID external T2V

**Don't** use T2V when any of these apply. Pivot to native or stock.

1. **Destruction / breakage / violence shots.** Hailuo-2.3's safety layer softens these to "intact object close-up" regardless of framing or `prompt_optimizer` flag. Confirmed across v1–v3 of the same prompt. **Don't iterate prompts — switch provider or pivot to text_card / stat_card / stock / user-recorded.** See `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/minimax-hailuo-safety-softening-violence.md`.

2. **Quota math doesn't work.** Hailuo-2.3 on this host caps at **~4 successful generations per cycle**. A 30s production with 5 segments (1 sample + 4 production) will hit HTTP 2067 mid-run. Options:
   - Upgrade MiniMax Token Plan first.
   - Cut to ≤3 production segments (18s total).
   - Use native for the slots T2V can't afford.

3. **Symbolic / conceptual visuals.** Brand color gradients, abstract particles, kinetic typography, logo entrance animations. Remotion / HyperFrames' home turf.

4. **Determinism required.** T2V output is non-deterministic — same prompt ≠ same clip. If you need reproducible output, multi-language localization variants, or A/B variants of the *same* visual, **don't use T2V**.

5. **Stock footage suffices.** Generic city / nature / office / abstract backgrounds / static objects → Pexels / Pixabay are free, deterministic, no quota.

6. **Strong cross-shot consistency required.** Same character across multiple shots, same product repeated. T2V is a lottery. Use I2V with same first_frame, or `character-animation` pipeline.

7. **Concept / data / code-driven content.** Anything that's "show a number / a process / a diagram" — Remotion stock types or synthetic screen recording.

## Per-shot checklist (assets stage)

Before committing a slot to T2V / I2V, ask the following for each shot:

```
Q1. Does this shot need real-world motion (real people/objects/environments in motion)?
    NO  → native (Remotion stock / HyperFrames / Manim / Ink / stock footage)
    YES ↓ Q2. Is real-world motion bound by the brief's delivery_promise?
          (cinematic / "like this reel" / launch hero / real-product demo)
    NO  → stock footage or atelier Remotion
    YES ↓ Q3. Is there a usable first_frame image (product photo, concept art, character plate)?
          YES → I2V (preferred — anchors consistency)
          NO  → T2V (last resort)
          ↓ Q4. Is current cycle quota healthy for this shot + all remaining shots?
                NO  → upgrade plan / cut slots / re-route to native
                YES ↓ Q5. Does the prompt touch safety-sensitive content?
                            (destruction / violence / medical / legal / political)
                            YES → don't burn quota; pivot to text_card / stat_card /
                                  stock / user-recorded
                            NO  → proceed with T2V/I2V
```

Final ordering: **native → stock → I2V → T2V**. T2V is the floor, not the ceiling.

## Provider selection on this host (2026-09-10)

| Provider | Status | Notes |
|---|---|---|
| `minimax_video_direct` (Hailuo-2.3 via fal.ai) | ✅ wired | T2V + I2V only — no multi-image reference. Quota cap ~4/cycle. Safety softening on destruction prompts. Default pick when configured. |
| `kling_video` / `seedance_video` / `veo_video` (FAL_KEY) | ❌ `FAL_KEY` not configured on this host (preflight 2026-09-10) | Seedance-2.0 is the Layer-3 "preferred premium default" for cinematic / trailer / multi-shot / synced audio / lip-sync |
| `kling_official_video` (KLING_API_KEY) | ❌ `KLING_API_KEY` not configured | — |
| `heygen_video_agent` (HEYGEN_API_KEY) | ❌ `HEYGEN_API_KEY` not configured | `create-video` / `avatar-video` paths |

Preflight must show the user the **provider menu summary** before any T2V commitment. If the capability row shows "0 / N configured", the T2V branch is closed — don't silently swap implementations.

## Operational guardrails

- **Probe quota before batching.** One cheap probe (~3-5s wall clock, but consumes 1 quota slot) before committing a 3-4 clip production batch. Don't re-probe between calls within a batch.
- **On HTTP 2067, stop and escalate.** Per AGENT_GUIDE "Escalate Blockers Explicitly": present options (upgrade plan / switch provider / drop slot to stock or talking-head / pause). Do **not** silently substitute Ken Burns or animatic.
- **Verify visually, don't trust `success=True`.** Hailuo returns `success=True` even when output has been safety-softened. Always extract a frame via ffmpeg before accepting a clip.
- **Lock `render_runtime` at proposal.** T2V-generated clips are *assets*, not composition. They still go through the locked `render_runtime` (Remotion / HyperFrames / FFmpeg) for final assembly. Don't quietly swap runtimes.
- **Log provider / model choices in `decision_log`.** Per AGENT_GUIDE "Re-log Changed Decisions": if provider or model swaps mid-run, append a new entry with the same `(category, subject)` pair. Never silently mutate.

## References

- [`AGENT_GUIDE.md`](../AGENT_GUIDE.md) § "Rule Zero" — all production through a pipeline; § "Critical Rule: Motion-Required Requests" — never silently degrade motion-led to still-led.
- [`AGENT_GUIDE.md`](../AGENT_GUIDE.md) § "Available Pipelines" — cinematic / video-template-remix / hybrid are the T2V-leaning pipelines.
- [`docs/PIPELINES-AND-OFFLINE-CAPABILITIES.md`](PIPELINES-AND-OFFLINE-CAPABILITIES.md) — full pipeline × offline-capability matrix.
- [`docs/t2v-i2v-multi-image-reference-glossary.md`](t2v-i2v-multi-image-reference-glossary.md) — capability-tier vocabulary.
- [`docs/PROVIDERS.md`](PROVIDERS.md) — every paid provider with setup, pricing, free-tier notes.
- [`docs/VIDEO-GEN-SMOKE-CHECKLIST.md`](VIDEO-GEN-SMOKE-CHECKLIST.md) — 6-probe preflight for kapon OneHub I2V.
- `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/minimax-hailuo-2-3-quota-2067.md` — quota cap (~4/cycle on this host), probe-then-batch protocol.
- `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/minimax-hailuo-safety-softening-violence.md` — destruction / breakage prompts softened by safety layer.
- `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/minimax-multimodal-newapi-verification-2026-09-09.md` — end-to-end I2V chain (`minimax_image → minimax_video_direct`) verification.
- `.agents/skills/seedance-2-0/` — Layer-3 default for premium cinematic T2V when env permits.
- `.agents/skills/ai-video-gen/` — multi-provider gateway (VEO / Kling / Sora / Runway / Seedance / MiniMax / Gemini Omni).