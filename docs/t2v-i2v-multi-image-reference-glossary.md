# T2V / I2V / multi-image reference — vocabulary glossary

> **Status:** stable reference doc. Define the three video-generation capability terms so future verification notes, pipeline specs, and "does model X support Y?" questions have a shared vocabulary.
> **Owner:** OpenMontage MCP server (`mcp_server.py` on `:8900`)
> **Audience:** agents and humans writing or reading `docs/*` and `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/*`.

## TL;DR

| Term | Full name | Conditioning input(s) | What the model does |
|---|---|---|---|
| **T2V** | Text-to-Video | A text prompt only | Generates a video clip from scratch — no visual reference at all |
| **I2V** | Image-to-Video | **One** reference image (+ optional text prompt) | Animates that single static image into a video clip; the image provides first frame / subject / style |
| **Multi-image reference** | (no canonical acronym) | **Two or more** reference images (+ optional text prompt) | Conditions generation on multiple images jointly — used for character consistency, multi-subject scenes, style transfer, prop anchoring, etc. |

"Model X only supports T2V/I2V" therefore means: model X can take text alone OR a single image, but **cannot** take 2+ reference images as joint conditioning. It is not a downgrade — it is a different capability tier.

## Why the distinction matters

The capability tiers are not equivalent. They unlock different production tasks:

- **T2V only** — useful for pure b-roll generation, mood shots, abstract visuals. Cannot keep a specific face / product / character consistent across shots.
- **T2V + I2V** — can anchor one reference per shot. Good enough for single-shot b-roll (e.g. animate a product photo). Cannot combine "this face" with "this scene" with "this prop" in a single generation.
- **T2V + I2V + multi-image reference** — required for hero work where consistency across shots matters (e.g. a specific character moves through a specific environment holding a specific prop, generated across multiple clips).

When a verification note or pipeline brief says "model only does T2V/I2V, no multi-image reference", that is a capability statement, not a quality statement. The model can still be the right choice for tasks that don't need multi-image conditioning — and the wrong choice for tasks that do.

## Where this applies in OpenMontage

Current MiniMax / Hailuo surface (`tools/video/minimax_video.py` via `fal.ai`):

- ✅ T2V (text prompt → video clip)
- ✅ I2V (one reference image → video clip)
- ❌ Multi-image reference — not supported. If a pipeline needs character consistency / multi-subject conditioning, route through a different model (e.g. Kling multi-image reference, VEO reference images).

For the live evidence that this is what `minimax_video` actually supports (not what its docs claim), see [`docs/minimax-multimodal-newapi-verification-2026-09-09.md`](minimax-multimodal-newapi-verification-2026-09-09.md) § "What was NOT verified this round" + the T2V/I2V verification rows.

For the related quota constraint that gates T2V/I2V verification on this host, see `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/minimax-hailuo-2-3-quota-2067.md`.

## How to use this glossary

When writing a new doc that names one of these capabilities:

- Spell it out once on first use ("text-to-video (T2V)") then use the acronym.
- When stating a capability boundary, name the tier explicitly: "supports T2V + I2V, no multi-image reference". Don't say "doesn't support video" — that's wrong and misleading.

When reading a vendor's marketing page:

- "Reference image" / "image conditioning" with no count stated = I2V only (default assumption).
- "Multiple references" / "subject reference + scene reference" / "image grid input" = multi-image reference support (verify with a smoke test; marketing pages sometimes overstate).

## References

- [`docs/minimax-multimodal-newapi-verification-2026-09-09.md`](minimax-multimodal-newapi-verification-2026-09-09.md) — the verification note that surfaced the T2V/I2V-only observation
- [`tools/video/minimax_video.py`](../tools/video/minimax_video.py) — the OM-side tool wrapping fal.ai queue API; `op="text_to_video"` and `op="image_to_video"` are the two wired entry points
- `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/minimax-hailuo-2-3-quota-2067.md` — quota gate that throttles T2V/I2V smoke tests on this host
- `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/minimax-multimodal-newapi-verification.md` — short agent-recall pointer