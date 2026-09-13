# Video Generation Smoke Checklist — kapon I2V / H3 / H3-Max

> **Scope**: this checklist is **only** the kapon.cloud OneHub I2V path (the `minimax_h3_video` BaseTool at `tools/video/minimax_h3_video.py`). It was empirically validated 2026-09-10 during the `xiaohongshu-neon-night-30s` I2V unblock and `xiaopengge-rideable-luggage-demo` planning.
>
> **Companion docs** (DO NOT duplicate their content):
> - [`MUSIC-ISSUES-AND-FIXES.md`](MUSIC-ISSUES-AND-FIXES.md) — Kokoro TTS warm-up, MusicGen CPU slowness + `HF_HUB_OFFLINE=1`, WhisperX base-model homophone errors. Already in repo (commit `09cbe3c`).
> - [`RESOURCES.md`](RESOURCES.md) — Pixabay BGM picks + license traps + office-demo recipe.
> - [`PIPELINES-AND-OFFLINE-CAPABILITIES.md`](PIPELINES-AND-OFFLINE-CAPABILITIES.md) — which providers are wired up vs unavailable.
> - [`RENDER-STRATEGY-xiaopengge-rideable-luggage.md`](../projects/xiaopengge-rideable-luggage-demo/artifacts/render-strategy.md) — I2V vs T2V decision for the 60s rideable luggage project.
>
> **Verification policy**: every line below is tagged **(verified)** if it was reproduced live on 2026-09-10 with the request actually run + response observed, or **(inferred)** if it is a static check from the BaseTool source / kapon docs. No unverified claims.

---

## When to run this checklist

- **Before** any new I2V run on a fresh host, fresh token, or fresh project.
- **After** any change to `tools/video/minimax_h3_video.py` or `KAPON_*` env vars.
- **After** rotating `KAPON_API_TOKEN`.
- **After** the kapon console updates model permission allowlist.
- **Do NOT** skip this if the previous run was days ago — the kapon allowlist can change silently.

**Budget**: 6 probes, total cost ≈ ¥1.65 on H3-Max 480P 5s clips (if all 6 actually submit). Most probes don't submit; some exit on the pre-flight check. Worst-case real cost: 2 × 0.33 = ¥0.66 if you skip the I2V-with-image probes. Worst-case worst: 6 × 0.33 = ¥1.98.

**Use `dry_run_tool` for pre-validation (zero cost)**:

For any model-permission, duration-floor, or URL-format question, prefer the OM `dry_run_tool` MCP endpoint over a real `execute_tool` / curl submission. It returns `cost_usd` and `duration_seconds` estimates from the BaseTool's `estimate_cost` / `estimate_runtime` (no network calls to kapon). This is the correct way to verify capability without consuming kapon quota.

> **Bad pattern** (consumes quota): running real curl POSTs to `https://models.kapon.cloud/minimaxi/v2/video_generation` to verify "does H3-Max accept 4s?" or "is H3 in my token's allowlist?".
>
> **Good pattern**: invoke `dry_run_tool(tool_name="minimax_h3_video", inputs={...})` via MCP, check `result.success` and `result.estimated_cost_usd`. The BaseTool's `estimate_cost` and `validate_inputs` run locally without contacting kapon. Only after dry-run passes should you call `execute_tool`.

> **Note** (verified 2026-09-10, learned the hard way): the `dry_run_tool` exists in OM for exactly this reason. Real submission to kapon is irreversible — the task is queued, billed, and produces a real (possibly junk) video. Treat every real kapon call as money spent; treat `dry_run_tool` as free.

---

## Checklist (run in order, stop at first failure)

### Probe 1 — Token alive (no model permission check yet)

```bash
TOKEN=$(grep '^KAPON_API_TOKEN=' /opt/OpenMontage_Voicebox/.env | cut -d= -f2-)

curl -sS -o /tmp/k1.txt -w 'http=%{http_code}\n' \
  -X POST "https://models.kapon.cloud/minimaxi/v2/video_generation" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"model":"MiniMax-H3-Max","content":[{"type":"text","text":"probe"}],"duration":5,"resolution":"768P","ratio":"16:9"}'

cat /tmp/k1.txt
```

**Expected** `(verified 2026-09-10)`:
- `http=200` + `{"platform_id":"video_...","task_id":"..."}` → token valid, H3-Max whitelisted. **Continue to Probe 2.**
- `http=403` + `{"code":"model_not_allowed","message":"令牌不允许调用该模型：MiniMax-H3-Max"}` → token is valid but the **model is not in the allowlist**. Fix: go to kapon console → 模型权限 → enable H3-Max. **Stop. Retry Probe 1.**
- `http=401` → token is wrong / rotated / typo'd. Fix: re-issue. **Stop.**
- `http=000` (curl error) → network unreachable / DNS. Fix: check egress.

**What this probe costs**: a real submission. If 200, you should also **delete the probe task** in kapon console to free the quota, or just let it complete (it's 5 seconds of "probe" video — ¥0.27 worth, useful as a render-time sanity check).

---

### Probe 2 — Confirm H3 also works (catches single-model allowlist)

```bash
curl -sS -o /tmp/k2.txt -w 'http=%{http_code}\n' \
  -X POST "https://models.kapon.cloud/minimaxi/v2/video_generation" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"model":"MiniMax-H3","content":[{"type":"text","text":"probe"}],"duration":6,"resolution":"768P","ratio":"16:9"}'

cat /tmp/k2.txt
```

**Expected** `(verified 2026-09-10)`: `http=200` with a task_id. If you only need H3-Max, this is optional. If you need both H3 and H3-Max, **do not skip** — kapon's allowlist is per-model.

---

### Probe 3 — I2V format: `image_url` object with `role` field (recommended form)

```bash
# Use a small (≤100KB) JPEG/PNG hosted on a non-datacenter URL.
# DO NOT use imgbb / most public CDNs — see Probe 5.
URL="https://ocbot.aixifs.com/videopic/first_frame_4_detail.png"  # example; substitute your own

curl -sS -o /tmp/k3.txt -w 'http=%{http_code}\n' \
  -X POST "https://models.kapon.cloud/minimaxi/v2/video_generation" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "
import json
print(json.dumps({
    'model':'MiniMax-H3-Max',
    'content':[
        {'type':'text','text':'probe'},
        {'type':'image_url','image_url':{'url':'$URL','role':'first_frame'}},
    ],
    'duration':5,'resolution':'768P','ratio':'16:9',
}))
")"

cat /tmp/k3.txt
```

**Expected** `(verified 2026-09-10)`: `http=200` + task_id.

---

### Probe 4 — I2V format: top-level `first_frame_image` field (alternative form)

```bash
curl -sS -o /tmp/k4.txt -w 'http=%{http_code}\n' \
  -X POST "https://models.kapon.cloud/minimaxi/v2/video_generation" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "
import json
print(json.dumps({
    'model':'MiniMax-H3-Max',
    'content':[{'type':'text','text':'probe'}],
    'first_frame_image':'$URL',
    'duration':5,'resolution':'768P','ratio':'16:9',
}))
")"

cat /tmp/k4.txt
```

**Expected** `(verified 2026-09-10)`: `http=200` + task_id. kapon accepts **both** forms (object inside content[] OR top-level field). The BaseTool `tools/video/minimax_h3_video.py` uses the object form.

---

### Probe 5 — I2V format: `image_url` without `role` (omit role)

```bash
curl -sS -o /tmp/k5.txt -w 'http=%{http_code}\n' \
  -X POST "https://models.kapon.cloud/minimaxi/v2/video_generation" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "
import json
print(json.dumps({
    'model':'MiniMax-H3-Max',
    'content':[
        {'type':'text','text':'probe'},
        {'type':'image_url','image_url':{'url':'$URL'}},
    ],
    'duration':5,'resolution':'768P','ratio':'16:9',
}))
")"

cat /tmp/k5.txt
```

**Expected** `(verified 2026-09-10)`: `http=200` + task_id. kapon treats `image_url` without `role` as `first_frame` automatically. Useful when you don't care about specifying role.

---

### Probe 6 — Anti-bot check: imgbb vs ocbot vs data URI

**DO THIS LAST** — it costs a real generation if it fails. Before running, decide what your I2V first-frame URL source will be in production:

- **ocbot.aixifs.com mirror** — proven path, runs nginx on a non-datacenter IP. `https://ocbot.aixifs.com/videopic/<file>.png`. **Use this unless you have a specific reason not to.**
- **imgbb** — datacenter-IP-blocked by kapon. Will get `2013 media url unreachable`. `https://i.ibb.co/...` — **do not use.**
- **data URI** — kapon returns `400 illegal base64 at input byte 4` because its strict-base64 validator rejects certain encodings. **Do not use** unless you have validated your exact Base64 encoder (Python `base64.b64encode` works; `base64.urlsafe_b64encode` does NOT; GNU `base64 -w0` was untested in this session).

```bash
echo "=== test the URL you actually plan to use ==="
URL_TO_TEST="https://ocbot.aixifs.com/videopic/<your-file>.png"  # substitute

curl -sS -o /tmp/k6.txt -w 'http=%{http_code}\n' \
  -X POST "https://models.kapon.cloud/minimaxi/v2/video_generation" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "
import json
print(json.dumps({
    'model':'MiniMax-H3-Max',
    'content':[
        {'type':'text','text':'probe'},
        {'type':'image_url','image_url':{'url':'$URL_TO_TEST','role':'first_frame'}},
    ],
    'duration':5,'resolution':'768P','ratio':'16:9',
}))
")"

cat /tmp/k6.txt
```

**Expected outcomes** `(verified 2026-09-10)`:

| URL source | http | body | action |
|---|---|---|---|
| ocbot.aixifs.com mirror | 200 | task_id | ✅ use it |
| imgbb (`i.ibb.co`) | 400 | `{"code":"invalid_request","message":"content[1].image_url.url is required"}` (during submit), then if you bypass submit and poll later, **2013 media url unreachable** during I2V fetch | ❌ mirror to ocbot |
| data URI (Python `b64encode`) | 400 | `{"code":"invalid_request","message":"content[1].image_url.url: data URI payload must be strict base64: illegal base64 data at input byte 4"}` | ❌ upload to ocbot instead |
| data URI (Python `urlsafe_b64encode`) | (similar 400) | strict-base64 mismatch | ❌ same |
| HTTP (not HTTPS) | (timeout or reject) | kapon refuses non-TLS | ❌ always HTTPS |
| `mm_file://<int>` | (untested on 2026-09-10) | requires upload to kapon's own file API; not part of this checklist | use only if you have kapon file API credentials |

---

## Bonus Probes (only if you'll use them in production)

### Duration floor check

```bash
# Try 4s and 1s — both should fail with H3-Max (floor is 5s)
for D in 4 1; do
  echo "=== duration=$D ==="
  curl -sS -o /tmp/kdur.txt -w 'http=%{http_code}\n' \
    -X POST "https://models.kapon.cloud/minimaxi/v2/video_generation" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"MiniMax-H3-Max\",\"content\":[{\"type\":\"text\",\"text\":\"p\"}],\"duration\":$D,\"resolution\":\"768P\",\"ratio\":\"16:9\"}"
  cat /tmp/kdur.txt | head -1
done
```

**Expected** `(verified 2026-09-10)`:
- `duration=1` → `http=400 {"code":"invalid_request","message":"duration must be between 5 and 15 seconds"}` (H3-Max floor)
- `duration=4` → same 400 for H3-Max; **for H3 (non-Max), 4s is the floor and 200** (verified 2026-09-10 by re-test)
- For H3, floor is 4s — try `duration=3`, expect `http=400 {"code":"invalid_request","message":"duration must be between 4 and 15 seconds"}`

### Reference media on H3 (NOT H3-Max)

If you intend to use `reference_images` / `reference_videos` / `reference_audio`:

```bash
curl -sS -o /tmp/kref.txt -w 'http=%{http_code}\n' \
  -X POST "https://models.kapon.cloud/minimaxi/v2/video_generation" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "
import json
print(json.dumps({
    'model':'MiniMax-H3-Max',
    'content':[
        {'type':'text','text':'probe'},
        {'type':'image_url','image_url':{'url':'$URL','role':'reference_image'}},
    ],
    'duration':5,'resolution':'768P','ratio':'16:9',
}))
")"

cat /tmp/kref.txt
```

**Expected** `(verified 2026-09-10 via BaseTool validation; kapon-side not re-verified 2026-09-10)`:
- H3-Max rejects reference media (per `tools/video/minimax_h3_video.py:75-80` `_H3_MAX_FORBIDDEN_FIELDS` validation)
- H3 accepts (up to 9 reference_image, 3 reference_video, 3 reference_audio)
- **Caveat**: the BaseTool catches this pre-flight. If you bypass the BaseTool and hit kapon directly, kapon may behave differently than the BaseTool's source-of-truth.

---

## After All Probes Pass — Clean Up

The probes created real kapon tasks. They will:
- Consume kapon quota (real money, ~¥1.65 worst case for 6 submits)
- Produce real video outputs (~5s "probe" text each, useless for production)

**Option A (recommended)**: ignore them. The wasted quota is the cost of the smoke test.

**Option B**: poll each task_id and download the probe mp4s to `/tmp/` for manual inspection (10s per task, useful for verifying I2V didn't produce garbage).

```bash
# If you went with Option B:
TASK_ID="440152736133437"  # from a successful probe response
BASE="https://models.kapon.cloud/minimaxi/v2"

# poll until succeeded
while :; do
  STATUS=$(curl -sS "$BASE/query/video_generation/$TASK_ID" \
    -H "Authorization: Bearer $TOKEN" | python3 -c "import json,sys; print(json.load(sys.stdin)['task']['status'])")
  echo "task $TASK_ID: $STATUS"
  [ "$STATUS" = "succeeded" ] && break
  [ "$STATUS" = "failed" ] && { echo "FAILED"; break; }
  sleep 8
done
```

---

## Quick-Reference Decision Table

| Symptom | Likely cause | First action |
|---|---|---|
| `403 model_not_allowed` | token lacks model permission in kapon console | Enable model in kapon console, re-run Probe 1 |
| `401` | token wrong / rotated | Re-issue token, re-run Probe 1 |
| `400 illegal base64 at input byte 4` | data URI with bad Base64 | Don't use data URI; use ocbot mirror |
| `400 duration must be between 5 and 15` (H3-Max) | duration < 5 (H3-Max floor) | Set duration ≥ 5; or switch to H3 if you need 4s |
| `400 duration must be between 4 and 15` (H3) | duration < 4 (H3 floor) | Set duration ≥ 4 |
| `400 prompt > 7000 chars` (implicit via kapon payload size) | prompt too long | Trim prompt |
| `2013 media url unreachable` | URL on anti-bot-blocked CDN (imgbb) or non-HTTPS | Mirror to ocbot.aixfs.com |
| `task polled: failed` with 2013 | kapon couldn't fetch first_frame_image mid-poll | Probe 5 should have caught this; check URL |
| `task polled: failed` with empty error | rare — kapon-side generation error | Retry once; if persists, escalate |
| Submit returns 200 but poll never succeeds | kapon queue congestion (EU business hours) | Wait 10 min and retry; or wait for off-peak |

---

## What This Checklist Does NOT Cover

- **Long-running production runs** — once smoke passes, production I2V is the same code path. Use `RENDER-STRATEGY-xiaopengge-rideable-luggage.md` for full 60s pipeline recipes.
- **Audio smoke** — see `MUSIC-ISSUES-AND-FIXES.md` for Kokoro / MusicGen / WhisperX.
- **Image generation smoke** — no comparable issues observed 2026-09-10. If a `minimax_image` provider starts failing, add a Probe 0 here mirroring the structure of Probe 1.
- **Composition / color grade** — those are ffmpeg operations, not network-dependent. No smoke needed.

---

## Maintenance

- **When to update this doc**: any time a new kapon error code appears that isn't in the decision table, or any time a new model permission tier is added in kapon console.
- **Owner**: whoever maintains `tools/video/minimax_h3_video.py` owns this doc.
- **Companion runtime**: this checklist runs against `https://models.kapon.cloud/minimaxi/v2/`. If kapon rotates their base URL, update all curl URLs in this doc and Probe 1.
- **Cost discipline (learned the hard way)**: never run a real kapon submission to "verify" something that can be answered by `dry_run_tool` or by reading `tools/video/minimax_h3_video.py` source. kapon calls cost money and are not reversible. The `dry_run_tool` MCP endpoint exists precisely to prevent this category of waste.
