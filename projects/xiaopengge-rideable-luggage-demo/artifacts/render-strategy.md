# Render Strategy — `xiaopengge-rideable-luggage-demo` 60 s TikTok

> **Project**: `projects/xiaopengge-rideable-luggage-demo/` (10 PNG + script.json + scene_plan.json + image-prompts.md + ecommerce-sales-coverage.md, all pulled from remote `e883545`)
> **Snapshot**: 2026-09-10
> **Pipeline**: OM `cinematic` with `composition_mode=atelier` + `render_runtime=ffmpeg` + `color_grade=bright_clean`
> **Companion docs**: [`RESOURCES.md`](RESOURCES.md) (assets already on disk), [`PIPELINES-AND-OFFLINE-CAPABILITIES.md`](PIPELINES-AND-OFFLINE-CAPABILITIES.md) (OM capabilities), [`MUSIC-ISSUES-AND-FIXES.md`](MUSIC-ISSUES-AND-FIXES.md) (audio gotchas)

---

## TL;DR

For this 60-second project:

1. **Convert T2V → I2V for the one "generated" scene** (`sc03` at 11-17s). Reasoning below.
2. **Upload exactly ONE PNG to `ocbot.aixifs.com/videopic/`** (the `06-walk.png` rideable-luggage reference frame). The other 9 PNGs stay on disk as b-roll stills.
3. **Static segments**: use ffmpeg `zoompan` (slow Ken Burns push-in) on each PNG; do NOT upload them.
4. **Voiceover**: Kokoro / `zf_xiaobei` with 1 call concatenating all 8 narration lines via `[pause]` markers.
5. **BGM**: `/tmp/pixabay_happy.mp3` (CC0, optimistic, 124.6 s — needs ffmpeg loop to fill 60 s).
6. **Subtitles**: ffmpeg `drawtext` per scene in lower-third.
7. **Final assembly**: concat demuxer + amix (BGM duck under voice via `sidechaincompress`) + `bright_clean` color grade.

---

## §1 — Why I2V beats T2V for `sc03` (the one generated scene)

`sc03` is the **only scene** in `scene_plan.json` with `type: "generated"`. The original brief said it should be T2V (per `ecommerce-sales-coverage.md`: "唯一 6 秒文生视频用于'成人骑行演示'"). On paper that's reasonable. On the wire, it's a poor choice for this specific scene — and the ecommerce-coverage doc itself admits it:

> "由于是文生视频而非首帧图生视频，产品外形可能有轻微漂移，成片时应以动作证明为主，不把它当作精确结构展示。"

That admission is the whole problem. The brief wants the rider **demonstrating the product**, not **demonstrating motion**. Demonstrating the product requires preserving the exact pearl-silver suitcase shape, the T-handle, the front-one-rear-two wheels, the dual footpegs, and the "小鹏哥" brand mark — all of which are precisely what `image-prompts.md` already encoded into `06-walk.png`.

### Empirical evidence (today's session, 2026-09-10)

Comparing T2V vs I2V on H3-Max 480P/768P under similar conditions:

| Dimension | T2V | I2V |
|---|---|---|
| Product-shape consistency over 6s | drifts; "rideable luggage" can morph into bicycle/scooter/generic case | locked at frame 1 |
| Brand-text fidelity ("小鹏哥") | rarely correct; frequently garbled or absent | guaranteed if first frame contains it |
| Static-text / watermark artifacts | appears spontaneously mid-clip | never |
| Per-clip success rate | ~67% in today's run (4/6 T2V submits) | ~100% in today's run (5/5 I2V submits) |
| Cost | ¥0.33/s (480P) or ¥0.50/s (768P) | identical |
| Wall time | ~60-90s | ~60-90s |

### What H3-Max actually does well vs poorly

- **Good at**: human motion (walking, riding, gesturing), daylight scenes, simple camera moves (dolly-in, push-in), shallow DOF, optimistic commercial mood
- **Poor at**: complex industrial product geometries, consistent brand text, multi-character scenes, anything requiring literal text overlay

### The proposed I2V prompt for `sc03`

Reuse `06-walk.png` as the first frame. The new prompt is **shorter and action-focused** instead of shape-focused:

```
Cinematic tracking shot following a young woman in beige shirt, white tee,
jeans, and white shoes wheeling then briefly riding the silver pearl
rideable luggage on a flat hotel sidewalk. Slow push-in, shallow DOF,
bright daylight. Stable side-three-quarter angle. No text, no logo, no
dangerous motion, no high speed.
```

The first frame already encodes the product shape + person styling + scene setting + bright lighting. The prompt just asks H3-Max to **animate what the first frame shows** — which is exactly the part H3-Max is good at.

### Cost of the switch

- Upload 1 PNG (`06-walk.png`) to `ocbot.aixifs.com/videopic/` via imgbb (with HTTPS proxy 7890)
- Keep `sc03` as `type: "generated"` because that is the canonical scene-plan type. Set its generation mode to I2V in `scene_plan.metadata.generation_overrides.sc03`, with `first_frame_image: "assets/images/06-walk.png"` (or the runtime's resolved HTTPS mirror URL).
- Replace the verbose T2V prompt (referencing shape details) with the short action-focused I2V prompt above

Visual diversity cost: `06-walk.png` was originally assigned to `sc08` (41-47s, walking/pulling mode). `sc03` (11-17s, riding demo) and `sc08` (41-47s, walking) are 24 seconds apart, so the visual repetition is acceptable for a TikTok-fast edit.

---

## §2 — Scene-by-Scene Mapping (final plan)

| scene_plan id | start-end | type | audio segment | static image (on disk) | video source |
|---|---|---|---|---|---|
| sc01 | 0-6s | broll | s01 Hook | `01-hero.png` | zoompan slow push-in |
| sc02 | 6-11s | broll | s01 Hook (continues) | `09-detail.png` | zoompan slow push-in (texture + brand mark) |
| **sc03** | **11-17s** | **generated** | **s02 Promise** | **`06-walk.png` (uploaded to ocbot)** | **H3-Max I2V, 6s** |
| sc04 | 17-23s | broll | s03 Ride demo | `02-controls.png` | zoompan slow push-in |
| sc05 | 23-29s | broll | s04 Pull mode | `03-charge.png` | zoompan slow push-in |
| sc06 | 29-35s | broll | s05 Charging | `04-storage.png` | zoompan slow push-in |
| sc07 | 35-41s | broll | s06 Storage | `05-car.png` | zoompan slow push-in |
| sc08 | 41-47s | broll | s07 Travel contexts (1/3) | `06-walk.png` | zoompan slow push-in |
| sc09 | 47-53s | broll | s07 Travel contexts (2/3, 3/3) | `07-hotel.png` → `08-cafe.png` | crossfade between two zoompans (1s each) |
| sc10 | 53-57s | broll | s08 CTA (1/2) | `09-detail.png` | zoompan slow push-in |
| sc11 | 57-60s | broll | s08 CTA (2/2) | `10-cta.png` | static hold (no zoompan) |

**Audio timing** (single concatenated Kokoro call):
- s01 line: 0-11s (overlaid on sc01 + sc02)
- s02 line: 11-17s (overlaid on sc03)
- s03 line: 17-23s (overlaid on sc04)
- s04 line: 23-29s (overlaid on sc05)
- s05 line: 29-35s (overlaid on sc06)
- s06 line: 35-41s (overlaid on sc07)
- s07 line: 41-53s (overlaid on sc08 + sc09)
- s08 line: 53-60s (overlaid on sc10 + sc11)

Total narration text length: 8 lines × ~15 chars = ~120 chars. Well within Kokoro's 7000-char limit and within the 150-char cap from the original prompt template.

---

## §3 — Asset Inventory (what's where)

### On disk (do NOT re-upload)

```
projects/xiaopengge-rideable-luggage-demo/assets/images/
  01-hero.png        1.5 MB
  02-controls.png    1.9 MB
  03-charge.png      1.8 MB
  04-storage.png     2.5 MB
  05-car.png         2.1 MB
  06-walk.png        1.9 MB   ← also used as sc03 I2V first frame
  07-hotel.png       1.9 MB
  08-cafe.png        2.2 MB
  09-detail.png      1.7 MB
  10-cta.png         1.5 MB
```

### To upload (exactly one)

```
06-walk.png → https://ocbot.aixifs.com/videopic/06-walk.png
```

Upload via:

```python
# from OM python (recommended path; uses 7890 HTTPS proxy automatically)
from tools.tool_registry import registry
registry.discover()
registry._tools['minimax_video_direct'].execute({
    'first_frame_image': '<will be replaced>',
    ...
})  # ← not directly usable here; use imgbb upload helper
```

Practical path (works today, proven on this host):

```bash
# Upload via imgbb with HTTPS proxy (7890)
python3 << 'PY'
import requests, os
PROXY = 'http://127.0.0.1:7890'
imgbb_key = '0381f600a61fbb34ead6bc9f9ee30e4b'
with open('projects/xiaopengge-rideable-luggage-demo/assets/images/06-walk.png','rb') as f:
    r = requests.post(
        'https://api.imgbb.com/1/upload',
        params={'key': imgbb_key, 'expiration': 3600},
        files={'image': f},
        proxies={'http': PROXY, 'https': PROXY},
        timeout=(10, 120),
    )
url = r.json()['data']['url']
print(url)
# Then mirror to ocbot.aixifs.com (or use imgbb URL directly — kapon DOES accept imgbb URLs
# as long as the proxy-from-OM path works. Today's success on clip_4/clip_5 used
# ocbot URLs; imgbb may or may not work depending on kapon's anti-bot update.)
PY
```

**Fallback**: if imgbb returns 403 to kapon (it did in today's session), upload to ocbot.aixifs.com instead via whatever mirror mechanism is configured for that host (nginx + scp, per the `first_frame_relay` bundle documented in `docs/`).

### Audio assets

```
/tmp/pixabay_happy.mp3            3.8 MB, 124.6 s, CC0, primary BGM pick
/tmp/kokoro_test.wav              0.3 MB, 6.33 s, Mandarin sample (use for warmup)
/opt/OpenMontage_Voicebox/projects/xiaohongshu-neon-night-30s/renders/final_30s_v1.mp4
                                  10.5 MB, NOT used in this render (different project)
```

`/tmp/pixabay_happy.mp3` needs to be looped to 60s with afade (recipe in `MUSIC-ISSUES-AND-FIXES.md` §4 Recipe A).

---

## §4 — Render Pipeline (Concrete Steps)

### Step 1 — Upload 1 PNG, generate 1 video

```bash
# 1a. Upload 06-walk.png (only this one — via imgbb + 7890 proxy, or via ocbot mirror)
# 1b. Generate the I2V clip via MCP execute_tool
SID=$(curl -sS -i -X POST http://127.0.0.1:8900/mcp \
  -H "Authorization: Bearer h6LQUTVPA5vBmqXijUydpockVrPx2ruUqPaVQRT6WJE" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"cli","version":"0"}}}' 2>&1 | grep -i 'mcp-session-id' | head -1 | sed -E 's/.*: *//;s/\r//g')

curl -sS -X POST http://127.0.0.1:8900/mcp \
  -H "Authorization: Bearer h6LQUTVPA5vBmqXijUydpockVrPx2ruUqPaVQRT6WJE" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SID" \
  -d "$(python3 -c "
import json
prompt = '''Cinematic tracking shot following a young woman in beige shirt, white tee, jeans, and white shoes wheeling then briefly riding the silver pearl rideable luggage on a flat hotel sidewalk. Slow push-in, shallow DOF, bright daylight. Stable side-three-quarter angle. No text, no logo, no dangerous motion, no high speed.'''
print(json.dumps({
    'jsonrpc':'2.0','id':2,
    'method':'tools/call',
    'params':{'name':'execute_tool','arguments':{
        'tool_name':'minimax_h3_video',
        'inputs':{
            'model':'MiniMax-H3-Max',
            'duration':6,
            'resolution':'768P',
            'ratio':'9:16',
            'first_frame_image':'https://ocbot.aixifs.com/videopic/06-walk.png',
            'prompt': prompt,
            'output_path':'projects/xiaopengge-rideable-luggage-demo/renders/sc03_ride_demo.mp4'
        }
    }}
}))
")"
```

### Step 2 — Generate 10 static-on-motion clips (zoompan + ffmpeg)

For each PNG, generate a `mp4` clip of the right duration using `zoompan`:

```bash
# Example for sc01 (0-6s, 9:16, slow push-in on 01-hero.png)
ffmpeg -y -v error -loop 1 -t 6 -i assets/images/01-hero.png \
  -vf "scale=4000:-2,zoompan=z='min(zoom+0.001,1.2)':d=144:s=768x1364,fps=24" \
  -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p \
  -an renders/sc01_hero.mp4
```

Apply to all 10 PNGs with scene-specific durations: 6s, 5s, 6s, 6s, 6s, 6s, 6s, 6s, 4s, 3s.

### Step 3 — Synthesize voiceover (single Kokoro call)

```python
# All 8 lines joined with [pause] markers, single Kokoro call
narration = "出门旅行，行李箱也可以更聪明。[pause:0.4]小鹏哥，把收纳、移动和骑行装进一只箱子。[pause:0.3]打开座垫，握住车把，穿过平整路面。[pause:0.3]不骑时，收起车把，照常拖行。[pause:0.3]接上电源，为下一段旅程补充能量。[pause:0.3]侧开空间，衣物和随身物品一目了然。[pause:0.4]放进后备箱，抵达酒店，咖啡街角，随时切换。[pause:0.4]小鹏哥，让每次出发更轻松。"
# write to renders/voiceover_full.wav via kokoro_tts tool
```

### Step 4 — Loop BGM to 60 s

```bash
ffmpeg -y -v error -stream_loop -1 -i /tmp/pixabay_happy.mp3 -t 60 \
  -af "aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo,afade=t=in:st=0:d=0.5,afade=t=out:st=59:d=1.5" \
  -c:a pcm_s16le renders/bgm_60s.wav
```

### Step 5 — Concat all 11 video clips

```bash
# Build concat list matching §2 column order (sc01, sc02, sc03, sc04, sc05, sc06, sc07, sc08, sc09, sc10, sc11)
cat > /tmp/concat60.txt << 'EOF'
file 'renders/sc01_hero.mp4'
file 'renders/sc02_detail.mp4'
file 'renders/sc03_ride_demo.mp4'
file 'renders/sc04_controls.mp4'
file 'renders/sc05_charge.mp4'
file 'renders/sc06_storage.mp4'
file 'renders/sc07_car.mp4'
file 'renders/sc08_walk.mp4'
file 'renders/sc09_hotel_cafe.mp4'
file 'renders/sc10_detail_cta.mp4'
file 'renders/sc11_cta_hero.mp4'
EOF

ffmpeg -y -v error -f concat -safe 0 -i /tmp/concat60.txt -c copy renders/raw_video_60s.mp4
```

### Step 6 — Mux audio + burn subtitles + apply color grade

```bash
ffmpeg -y -v error \
  -i renders/raw_video_60s.mp4 \
  -i renders/voiceover_full.wav \
  -i renders/bgm_60s.wav \
  -filter_complex "
    [1:a]volume=1.0[voice];
    [2:a]volume=0.35[bgm];
    [voice][bgm]sidechaincompress=threshold=0.05:ratio=8:attack=5:release=1000[ducked];
    [0:v]curves=all='0/0.05 0.25/0.30 0.5/0.55 0.75/0.80 1/1.0',eq=contrast=1.0:saturation=1.15:brightness=0.02,drawtext=text='出门旅行，行李箱也可以更聪明。':fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc:fontsize=44:fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=12:x=(w-text_w)/2:y=h-th-80:enable='between(t,0.5,10.5)',drawtext=...ALL_OTHER_SUBTITLES...[v];
    [ducked][v]concat=n=1:v=1:a=1[outv][outa]
  " \
  -map "[outv]" -map "[outa]" \
  -c:v libx264 -preset medium -crf 18 -c:a aac -b:a 192k \
  -shortest \
  projects/xiaopengge-rideable-luggage-demo/renders/final_60s_v1.mp4
```

(Subtitle `drawtext` chains per scene with per-scene `enable='between(t,X,Y)'` time gates; full chain in `docs/MUSIC-ISSUES-AND-FIXES.md` §1 reference if needed.)

### Step 7 — QA

- Mean luminance 80-110 / 255 (bright_clean target)
- RMS audio peak -3 to -1 dBFS (no clipping)
- Total duration 60.0 s ±0.5 s
- 5-frame spot check: sc03 (ride demo) should show actual riding motion; sc11 (CTA) should show product with whitespace for overlay

---

## §5 — Risk Register

| Risk | Mitigation |
|---|---|
| imgbb 403 to kapon (datacenter IP blocked) | Mirror via ocbot.aixifs.com (proven path from today's clip_4/clip_5 run) |
| I2V fails on sc03 | Retry once; if 2nd fails, fall back to T2V with a longer prompt encoding all product details (acknowledging drift per ecommerce-coverage doc) |
| BGM `/tmp/pixabay_happy.mp3` cleared by reboot | Re-download via OM `pixabay_music` tool with query="bright positive optimistic happy" |
| Kokoro 40s warm-up per call | Single call with all narration joined by `[pause]` markers (one warm-up amortizes over all 8 lines) |
| WhisperX base-model homophone errors on Chinese subtitles | Use `large-v3-turbo` explicitly if transcribing for QC; but here we hand-write subtitles so no ASR needed |
| Color grade over-saturates / crushes shadows | `bright_clean` curves are conservative; QA spot-check after first render, adjust eq saturation 1.10-1.20 range if needed |
| T2V fallback produces "drifted" product shape | Ecommerce-coverage doc accepts this; scene-level subtitle "小鹏哥" overlays can compensate for missing brand mark |

---

## §6 — Out of Scope (Not Doing)

- **Voice cloning** — not requested by brief; using stock `zf_xiaobei`
- **Multi-language subtitles** — brief is Chinese-only; English overlay not requested
- **CTA click-through / QR code generation** — overlay placeholder accepted; no asset provided in repo
- **Music duck automation** — manual `sidechaincompress` parameters documented; not an automated OM tool yet
- **Crossfade transitions** — all transitions are hard cut except sc09→sc10→sc11 region (where static-image crossfade suffices); no crossfade transitions on the generated clip

---

## §7 — Action Checklist (for the renderer to execute)

- [ ] Step 0: Confirm `/tmp/pixabay_happy.mp3` exists; re-download if not
- [ ] Step 1a: Upload `06-walk.png` to `https://ocbot.aixifs.com/videopic/06-walk.png`
- [ ] Step 1b: Submit H3-Max I2V for sc03 (6s, 768P, 9:16, with the action-focused prompt in §4)
- [ ] Step 2: Generate 10 zoompan static-on-motion clips (one per PNG, durations per §2)
- [ ] Step 3: Synthesize Kokoro voiceover (single call, all 8 lines with `[pause]` markers)
- [ ] Step 4: Loop BGM to 60s with afade
- [ ] Step 5: Concat 11 video clips
- [ ] Step 6: Mux audio + burn subtitles + apply bright_clean color grade
- [ ] Step 7: QA spot-check; output to `projects/xiaopengge-rideable-luggage-demo/renders/final_60s_v1.mp4`
- [ ] Report path + summary metrics to user

---

## Appendix — Why the other 9 PNGs are NOT uploaded to ocbot

For every scene except `sc03`, the video is constructed locally via ffmpeg `zoompan` on the PNG. No kapon / H3-Max call is made. The PNG never leaves the host. Uploading them to ocbot.aixifs.com would:

- Waste 18 MB of outbound bandwidth (10 PNGs × ~1.8 MB avg)
- Create 9 stale public HTTPS URLs that become attack surface
- Cost nothing extra (ocbot is free nginx static hosting) but offers no benefit

The only PNG that **must** leave the host is `06-walk.png`, because it's the first frame for an I2V call — and kapon runs in datacenter IPs that get anti-bot-blocked from imgbb and other public CDNs. The fix is to mirror via a non-datacenter host (ocbot.aixifs.com). One PNG, one URL, one purpose.
