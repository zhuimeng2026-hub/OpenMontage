# Video Template Remix — Asset Director

## When to Use

Use this stage after replacement slots are approved and before edit decisions are finalized.

## Prerequisites
Read `scene_plan` and approved slot decisions. Read the relevant provider skill before any image/video generation call.

## Preflight (Mandatory — Run Before Any Generation Call)

These three host-specific constraints live in MEMORY.md but are easy to forget on a single run. They are codified here so every asset-generation call checks them automatically. **Skip the preflight = risk of silent partial-run, quota burn, or wasted provider spend.** The verification commands below are quick smoke pings, not full calls.

### 1. Voicebox Live Check (TTS narration)

Voicebox (the project's TTS gateway) is known to fail at runtime even when the registry reports `status="available"` (see memory `voicebox-blocked-kokoro-escape-hatch`). Before generating narration segments, probe whether voicebox actually serves this session:

```python
from tools.tool_registry import registry
registry.discover()
tts = registry._tools.get("voicebox_tts") or registry.get_by_capability("tts")
# 1-token smoke ping — if this fails or returns success=False, voicebox is
# effectively down for this run regardless of registry status.
probe = tts.execute({"text": "ping", "voice_id": "default", "smoke": True})
voicebox_live = bool(probe.success and probe.data.get("audio_path"))
```

If `voicebox_live` is False, **do NOT batch voicebox narration calls**. Switch to the documented fallback path: direct `kokoro==0.9.4` `KPipeline` with `HF_HUB_OFFLINE=1` (per the same memory). Log the fallback in `decision_log` with `category: "voice_selection"`, `subject: "Narration TTS provider"`, and `rejected_because: "voicebox live check failed (registry status was misleading)"`.

### 2. MiniMax Hailuo Quota Probe (video gen)

`minimax_video_direct` exhausts the host's MiniMax Token Plan after roughly 3 generations and starts returning HTTP 2067 (see memory `minimax-hailuo-2-3-quota-2067`). Before batching clips:

```python
from tools.tool_registry import registry
registry.discover()
probe = registry._tools["minimax_video_direct"].execute({
    "operation": "quota_probe",   # cheap metadata call, no clip generated
})
quota_ok = probe.success
if not quota_ok:
    # Pivot: defer the broll until quota resets, or swap to a provider whose
    # quota probe succeeds. Do NOT re-probe inside the batch loop — each
    # retry consumes an attempt on the rolled-up quota window.
    pass
```

When quota is exhausted mid-batch, **stop and surface the blocker** rather than substituting silently. The user can either wait for quota reset, upgrade, or approve an alternate provider. Log `decision_log` entries for every quota-driven swap with the same `(category, subject)` pair per the append-only rule.

### 3. Destruction-Prompt Vocabulary Filter (Hailuo safety softening)

Hailuo silently softens destruction / breakage prompts — `CRACKS`, `gives way`, `stress test`, `break`, `shatter`, `collapse`, `destruction` all produce intact objects in the output (see memory `minimax-hailuo-safety-softening-violence`). Before sending any video-gen prompt, scan it for the documented vocabulary:

```python
DESTRUCTION_VOCAB = [
    "crack", "cracks", "give way", "gives way", "stress test",
    "break", "breaks", "shatter", "shatter", "collapse",
    "destruction", "destroys", "destroyed", "demolish", "splinter",
    "fracture", "rupture",
]
def has_destruction_vocab(prompt: str) -> list[str]:
    lower = prompt.lower()
    return [w for w in DESTRUCTION_VOCAB if w in lower]

hits = has_destruction_vocab(prompt)
if hits:
    # Two acceptable paths — pick one, log to decision_log:
    #   (a) rewrite the prompt to describe the END STATE of the destruction
    #       ("wheel DETACHES and flies off") instead of the destructive
    #       verb. Keep the cinematic intent.
    #   (b) pivot to text_card / stock / user-recorded footage for that slot
    #       and surface the swap to the user.
    # Do NOT send the original prompt — the result will be the opposite of
    # what was requested and cost a generation.
```

This filter is a contract, not a suggestion. The hailuo-broll plan for `projects/remix-luggage-v1` had to manually rewrite the L02 prompt for exactly this reason; future runs should not have to discover it from scratch.

## Process
Resolve user-provided or licensed assets first. For each replace slot, record source slot, asset path, provenance, rights, aspect ratio, duration, crop/fill behavior, and whether generation was approved. Fit replacement media to the original hold; reject assets that require timing changes unless edit approval explicitly allows it. Preserve source audio and subtitles as assets unless marked replace.

## Self-Evaluate
5 means every replacement is traceable, duration-safe, and visually compatible; 3 means one manual crop warning; 0 means an unapproved or untraceable asset is used.

## Pitfalls
- Never use a generated “similar” asset to fill an unapproved slot. Never overwrite source files. Do not claim a local placeholder is final.
- Never skip the preflight. A "looks-available" registry status is not the same as a live provider — voicebox proves this; MiniMax quota proves it on the time axis.
- When a preflight check fails, do NOT compensate by raising batch size on the working path. Fix the broken one and proceed, or stop and ask the user.
