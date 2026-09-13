# MiniMax multimodal capability verification — 2026-09-09

> **Status:** T2I verified ✅ / T2V + I2V wired, not smoke-tested this round
> **Owner:** OpenMontage MCP server (`mcp_server.py` on `:8900`)
> **Purpose:** canonical evidence file. Read this first before re-probing or claiming "it doesn't work".

## TL;DR

`minimax_image` (Hailuo Image-01 via new-api-forwarded `MINIMAX_API_KEY`) was exercised end-to-end on 2026-09-09 and produced a faithful 1024×1024 PNG in 17.6 s for $0.003. The `MINIMAX_API_KEY` value in `/opt/OpenMontage_Voicebox/.env` is **not a direct MiniMax/Hailuo key** — it is a placeholder resolved by `new-api` to the actual MiniMax Token Plan package (confirmed by the user in conversation 2026-09-09).

`minimax_video` (T2V / I2V via `fal.ai`) is registered as `available` once `.env` is loaded, but was deliberately **not** exercised in this verification round to avoid burning the host's 2067 quota.

## What was verified

| Field | Value |
|---|---|
| Probe timestamp | 2026-09-09 23:27 local |
| Tool | `minimax_image` (registry name) |
| Model | `image-01` |
| Prompt | "a tiny glass cube catching morning light, studio shot, neutral background" |
| Inputs | `aspect_ratio=1:1`, `n=1`, no seed |
| Success | True |
| Latency | 17.61 s end-to-end (sync submit, single round-trip) |
| Cost | $0.003 (≈ ¥0.022) |
| Output | `projects/_scratch/t2i_smoke/probe.png` — 559 KB, 1024×1024 |
| Visual fidelity | High — refractive glass cube, studio shadow, neutral background, matches prompt |
| Log evidence | `logs/gen_detail.log` → `event=image_gen_detail state=done tool=minimax_image success=true outputs=1 cost_usd=0.0030 duration_s=17.61` |

The generated PNG shows the prompt faithfully — a small clear glass cube casting a refractive shadow under studio morning light against a neutral grey/beige background. Not a placeholder; not a re-served cached image.

## What this proves

1. **The new-api forwarding pipeline is wired up correctly** for `MINIMAX_API_KEY`. External callers hitting `:8900/mcp` and invoking `execute_tool(tool_name="minimax_image", inputs={...})` will get real generations, not auth errors or placeholder bytes.

2. **`tools/graphics/minimax_image.py::MiniMaxImage._collect_image_bytes` handles the real MiniMax response shape** — the test went through **branch 2** (`data.image_urls[]`, the comment-marked "confirmed real MiniMax response shape — observed 2026-08") and produced a PNG without falling through to the async-poll branch.

3. **Cost & latency are real numbers**, not the tool's `cost_estimate_confidence: low` placeholder: $0.0030 matches `MiniMaxImage.COST_PER_IMAGE_USD`; 17.6 s is in the expected band for a 1024×1024 generation over fal.ai / new-api.

## What was NOT verified this round

| Capability | Tool | Status |
|---|---|---|
| T2V (text-to-video) | `minimax_video` op=`text_to_video` | wired, not smoke-tested (2067 quota risk) |
| I2V (image-to-video) | `minimax_video` op=`image_to_video` + `image_url` | wired, not smoke-tested (2067 quota risk) |

`minimax_video` is gated on `FAL_KEY` (also a new-api-forwarded credential in `.env`). Registry status check returns `available` once the env is loaded — trust the wire-up, but **run a `hailuo-2.3-fast/standard` smoke before shipping any batch that depends on it**. One probe on the cheapest variant costs ~$0.08 and is cheap insurance against a 2067 surprise.

## Operational notes

- **Key provenance.** Both `MINIMAX_API_KEY` and `FAL_KEY` in `/opt/OpenMontage_Voicebox/.env` are **placeholder strings resolved by `new-api`**, not direct vendor keys. Don't be alarmed by `echo "$MINIMAX_API_KEY" | wc -c` showing 1 — the actual auth happens upstream in `new-api`.

- **Quota.** Hailuo-2.3 Token Plan hits HTTP 2067 after roughly 3 generations on this host. If you smoke-test, batch without re-probing and escalate immediately on 2067 (see `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/minimax-hailuo-2-3-quota-2067.md`).

- **Safety softening.** Hailuo silently softens destruction/breakage prompts ("CRACKS", "gives way", "stress test" → intact shot). Verify visual via ffmpeg frame extraction before iterating on prompt wording — wasted quota otherwise (see `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/minimax-hailuo-safety-softening-violence.md`).

- **`~/.claude/settings.json` is unrelated.** That file holds Anthropic proxy creds (mengxa.com) + Feishu/WeCom tokens. The `ANTHROPIC_DEFAULT_OPUS_MODEL=MiniMax-M3` / `SONNET=...-M2.7-highspeed` strings are Claude **model display names**, not MiniMax multimodal API keys.

## Reproduce / re-verify

```bash
cd /opt/OpenMontage_Voicebox
set -a; source .env; set +a
mkdir -p projects/_scratch/t2i_smoke
python - <<'PY'
from tools.tool_registry import registry
registry.discover()
res = registry.get("minimax_image").execute({
    "prompt": "a tiny glass cube catching morning light, studio shot, neutral background",
    "aspect_ratio": "1:1",
    "n": 1,
    "output_path": "projects/_scratch/t2i_smoke/probe.png",
})
print(res.success, res.cost_usd, res.duration_seconds, res.artifacts)
PY
```

## Open items

1. **T2V / I2V verification smoke — explicitly declined.** The user's host-wide video quota is limited and already consumed by other projects. Burning any of it on a verification probe is off the table; do not propose a "cheap-variant smoke for confidence". A batch that actually needs the videos will exercise the tool itself — that's where 2067 / safety issues will surface, and the agent should react to them at runtime rather than pre-emptively.
2. **Quota introspection** — if `new-api` exposes a quota introspection endpoint, surface a `quota_remaining` field in `minimax_image.estimate_cost()` and `minimax_video.estimate_cost()` so we fail fast before burning generations. (Lower priority: useful but blocked on new-api capability.)

## References

- `tools/graphics/minimax_image.py` — T2I tool, response parser handles 4 known MiniMax shapes
- `tools/video/minimax_video.py` — T2V / I2V tool, fal.ai queue API
- `docs/PROVIDERS.md` — provider summary
- `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/minimax-hailuo-2-3-quota-2067.md`
- `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/minimax-hailuo-safety-softening-violence.md`
- `~/.claude/projects/-opt-OpenMontage-Voicebox/memory/minimax-multimodal-newapi-verification.md` — short agent-recall pointer