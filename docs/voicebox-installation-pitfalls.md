# Voicebox Installation Pitfalls — `/opt/voicebox/` + `/opt/OpenMontage_Voicebox/`

> Captured during the 2026-08-21 install of `Qwen/Qwen3-TTS-12Hz-1.7B-Base`
> against the local voicebox stack (`:17493`) and OpenMontage's reverse-proxy
> MCP layer (`:8900/voicebox/mcp/`). Each pitfall records the symptom, root
> cause, and the fix that worked. Read this BEFORE repeating the install on a
> fresh host.

This document is the troubleshooting companion to
[`voicebox-prerequisites.md`](voicebox-prerequisites.md). That doc covers the
happy path; this one covers what goes wrong.

---

## TL;DR — the four landmines

| # | Symptom | One-line fix |
|---|---|---|
| 1 | `huggingface_hub.snapshot_download` hangs at 0–5 % for hours | Bypass it: `requests.get(stream=True, chunk_size=8 MB)` |
| 2 | `RuntimeError: voicebox generation failed: ... not found in directory .../speech_tokenizer/` | Qwen3-TTS has **two** weights — main + speech_tokenizer. Both are required |
| 3 | Files appear in `~/.cache/huggingface/` but voicebox still says `model_downloaded:false` | `HF_HUB_CACHE` defaults to `.../hub/`. `snapshot_download(cache_dir=...)` does NOT auto-add `/hub/` — move the cache |
| 4 | Qwen model loads, integration test gets `Profile not found (404)` even though `shared_clone_profile` fixture ran | Session-scoped fixture was deleting the profile **before** `yield profile` |

---

## 1. `snapshot_download` hangs through the local proxy

**Symptom.** `snapshot_download` from `huggingface_hub` either:

- Stalls at 0 % / a few % for tens of minutes with `CLOSE_WAIT` connections
  piling up to `127.0.0.1:7890` and no bytes moving, OR
- Reports `Fetching 13 files: 8%|▊|1/13 [00:01<00:16, 1.41s/it]` and never
  advances.

`/opt/voicebox/backend/backends/{kokoro,luxtts,...}_backend.py` calls
`snapshot_download` on first use. If the worker subprocess can't reach HF,
the synthesis call hangs and eventually times out.

**Root cause.** Two layers of weirdness:

1. Voicebox's worker process may not inherit `HTTPS_PROXY` set in the parent
   shell (verified: `ps eww <pid>` on the voicebox worker shows no proxy
   vars). The worker's connection to HF goes through the default route, which
   is `Network is unreachable` on this host.
2. Even when the proxy IS set, the request path is `host:port` →
   `mihomo (:7890)` → `huggingface.co` → AWS CDN
   (`us.aws.cdn.hf.co/xet-bridge-us/...`). The xet-bridge CDN uses
   chunked-by-content-hash downloads; concurrent workers and keepalive
   sockets on this proxy cause `snapshot_download`'s parallel strategy
   (`max_workers=8` default) to thrash.

**Fix (worked):** bypass `snapshot_download` entirely. Direct `requests` with
streaming + a single connection + 8 MB chunks:

```python
import requests
URL = 'https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base/resolve/main/model.safetensors'
proxies = {'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}
with requests.get(URL, stream=True, allow_redirects=True,
                  proxies=proxies, timeout=(30, 600)) as r:
    r.raise_for_status()
    with open(target, 'wb') as fh:
        for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
            if chunk:
                fh.write(chunk)
```

Observed throughput: ~1.2 MB/s for 3.86 GB → ~50 min, and ~0.9 MB/s for
682 MB → ~12 min. Total wall-clock: under an hour. Versus `snapshot_download`
which never finished a single file in 20+ min.

**Fix (worth adding):** start the voicebox worker with proxy env vars in
`/etc/systemd/system/voicebox.service` (or whatever supervises it) so the
worker doesn't need pre-downloading:

```ini
[Service]
Environment="HTTPS_PROXY=http://127.0.0.1:7890"
Environment="HTTP_PROXY=http://127.0.0.1:7890"
```

Voicebox's Python worker inherits systemd's env, so `huggingface_hub` and
`urllib3` will see the proxy.

---

## 2. Qwen3-TTS has TWO weights, not one

**Symptom.** After downloading only the top-level `model.safetensors`,
voicebox reports `model_downloaded: true`, but `POST /generate` returns:

```
RuntimeError: voicebox generation failed:
Error no file named pytorch_model.bin, model.safetensors, tf_model.h5,
model.ckpt.index or flax_model.msgpack found in directory
.../snapshots/<sha>/speech_tokenizer.
```

**Root cause.** Qwen3-TTS is a composite model — main TTS weights at the top
level plus a separate speech tokenizer (audio codec) under `speech_tokenizer/`.
Voicebox checks BOTH directories and fails if either is missing. The HF tree
API only shows the top-level file count (9 files), so it's easy to miss the
nested weights.

| File | LFS SHA | Size |
|---|---|---|
| `model.safetensors` | `38fc7fc51c5e776e840414b6fd443962e9411b9654888fd7913e4da643cb857c` | 3.86 GB |
| `speech_tokenizer/model.safetensors` | `836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258` | 682 MB |

**Source of truth for the LFS SHAs** (always read this before placing blobs):

```bash
curl -s 'https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base/raw/main/model.safetensors'
# version https://git-lfs.github.com/spec/v1
# oid sha256:38fc7fc...d571258
# size 3857413744
```

**Fix:** download both, then verify each SHA matches the LFS pointer before
placing it in the cache.

---

## 3. `HF_HUB_CACHE` includes `/hub/`; `snapshot_download(cache_dir=...)` does NOT

**Symptom.** Voicebox's `is_model_cached()` reads `HF_HUB_CACHE` (defaults to
`~/.cache/huggingface/hub/`) and reports `model_downloaded:false` even though
the model files are sitting right there in `~/.cache/huggingface/`.

**Root cause.** `huggingface_hub.constants.HF_HUB_CACHE` resolves to
`~/.cache/huggingface/hub/`. But `snapshot_download(cache_dir=...)` writes
directly to the `cache_dir` you pass — it does NOT auto-append `/hub/`. So if
you pass `cache_dir='~/.cache/huggingface'`, you get files at
`~/.cache/huggingface/models--.../...` while voicebox looks at
`~/.cache/huggingface/hub/models--.../...`. Invisible to voicebox.

**Fix (worked):** after the download, move the cache to the canonical path:

```bash
mv ~/.cache/huggingface/models--Qwen--Qwen3-TTS-12Hz-1.7B-Base \
   ~/.cache/huggingface/hub/
```

**Fix (worth adding):** pass `cache_dir=os.path.expanduser('~/.cache/huggingface/hub')`
explicitly to `snapshot_download` so the cache lands in the right place on
the first try.

---

## 4. Blob filenames MUST be the LFS SHA, never a placeholder

**Symptom.** (This one burned us.) After downloading `model.safetensors` (3.86
GB) by hand and dropping it at `blobs/836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258`
(a placeholder name picked because `snapshot_download` had left an empty file
there earlier), we then downloaded `speech_tokenizer/model.safetensors` (682
MB) — and its real LFS SHA turned out to ALSO be `836b7b...`. The
`mv 836b7b...partial 836b7b...` step silently overwrote the 3.86 GB file
with the 682 MB file.

The main model was lost. SHA verification per file is what catches this:

```python
import hashlib
h = hashlib.sha256()
with open(target, 'rb') as fh:
    while True:
        c = fh.read(64 * 1024 * 1024)
        if not c: break
        h.update(c)
assert h.hexdigest() == EXPECTED_SHA, f"corrupt: {h.hexdigest()}"
```

**Lesson:** the LFS pointer is the only safe source of truth for the blob
filename. Don't reuse an empty `.incomplete` slot's name — `rm -f` empty
placeholders and always rename to the LFS SHA.

---

## 5. Test fixtures that DELETE before yield

**Symptom.** `test_text_to_speech_synthesizes_audio` failed with
`AssertionError: {"detail":"Profile not found"}` at the `POST /generate` call,
even though `shared_clone_profile["id"]` returned a valid UUID. Other tests in
the same run that scanned for a `pytest-shared-*` profile by name **passed**
(because they found a leftover profile from a previous run).

**Root cause.** The `shared_clone_profile` session-scoped fixture did:

```python
# teardown (BEFORE yield — bug)
try:
    requests.delete(f"{base}/profiles/{profile_id}", ...)
except requests.RequestException:
    pass

yield profile  # ← profile_id is now invalid; voicebox returns 404
```

The teardown DELETE was positioned before `yield`, not after. Tests consuming
the fixture received a profile_id that voicebox had already wiped.

**Fix:**

```python
yield profile  # tests see a live profile

try:
    requests.delete(f"{base}/profiles/{profile_id}", ...)
except requests.RequestException:
    pass
```

**Bonus fix:** the same fixture was not `autouse=True`, so MCP tests (which
collect first, alphabetically) ran before the fixture was ever requested and
saw an empty profile list, leading to spurious `SKIPPED` results. Promote to
`autouse=True` so the profile is alive before any test runs.

---

## 6. FastMCP results live in `structuredContent`, not the top-level dict

**Symptom.** `test_speak_*` tests failed with
`AssertionError: speak did not return generation_id: {'content': [...],
'structuredContent': {'generation_id': 'def65229-...'}, 'isError': False}`.
The MCP tool call worked perfectly — it returned a valid `generation_id` —
but the test parsed the response in the wrong place.

**Root cause.** FastMCP ≥ 0.4 wraps tool responses as
`{content, structuredContent, isError}` per the MCP spec change. Tests
written against older FastMCP flattened results were looking at the top level.

**Fix:**

```python
# Before (broken under FastMCP >= 0.4):
gen_id = result.get("generation_id")

# After:
gen_id = (result.get("structuredContent") or {}).get("generation_id") \
       or result.get("generation_id")  # fallback for older servers
```

Production code (`tools/audio/voicebox_tts.py`) talks REST directly to
voicebox and is **not affected** — this only matters for the integration
tests that go through FastMCP's JSON-RPC layer.

---

## 7. Test artifacts leak to repo root

**Symptom.** A `voicebox_54040f91-16c5-45bf-8a39-3527b75a9a70.wav` file
appeared at `/opt/OpenMontage_Voicebox/` after a Kokoro smoke test.

**Root cause.** `voicebox_tts.py`'s `_resolve_output_path` only writes under
`projects/<project_id>/assets/audio/` when a project context can be inferred.
Without one, it falls back to `cwd` (see `lib/events.infer_project_dir`). The
hand-rolled Kokoro verification didn't initialize a project, so the wav
landed in the repo root.

**Fix (immediate):** `rm` the stray file before staging. (Done.)

**Fix (worth adding):** in `voicebox_tts.execute()`, refuse to write outside
`projects/<project_id>/` and return an error if no project context exists.
This would convert the silent fallback into a loud failure, matching the
CLAUDE.md invariant "Tool outputs go under `projects/<project-id>/`."

---

## Quick checklist before declaring setup "done"

Run through these after the model weights land. All four must pass before
the integration tests will go green.

```bash
# 1. Cache layout (HF_HUB_CACHE/hub/ + LFS SHA blobs + symlinks)
ls ~/.cache/huggingface/hub/models--Qwen--Qwen3-TTS-12Hz-1.7B-Base/snapshots/*/
# should show config.json, generation_config.json, merges.txt, ...
# model.safetensors -> ../../blobs/38fc7fc...
# speech_tokenizer/model.safetensors -> ../../../blobs/836b7b...

# 2. Voicebox sees both components
curl -s -H 'X-Voicebox-Client-Id: prereq' http://127.0.0.1:17493/health
# {"status":"healthy","model_loaded":false,"model_downloaded":true, ...}

# 3. End-to-end smoke (use Kokoro — fastest, no cloning needed)
python -c "
from tools.tool_registry import registry
registry.discover()
tool = registry._tools['voicebox_tts']
print('voicebox_tts status:', tool.get_status())
"
# status: available

# 4. Integration tests (should be 11 passed, 0 failed, 0 skipped)
source .env && \
  export VOICEBOX_TEST_TTS_TIMEOUT_S=900 VOICEBOX_TEST_WARMUP_TIMEOUT_S=900 && \
  python -m pytest tests/integration/ -v
```

If step 2 reports `model_downloaded:false`, revisit #3 (cache path). If step
4 reports `Profile not found`, revisit #5 (fixture yield order). If step 4
reports `404 generation failed: ... not found in directory .../speech_tokenizer`,
revisit #2 (you only downloaded the main weights).

---

## Related docs

- [`voicebox-prerequisites.md`](voicebox-prerequisites.md) — the happy-path
  install: prerequisites, proxy setup, model download commands.
- [`voice-cloning-data-requirements-2026-08-21.md`](voice-cloning-data-requirements-2026-08-21.md)
  — what reference audio you need to feed Qwen3-TTS for a usable clone.
- [`tools/audio/voicebox_tts.py`](../tools/audio/voicebox_tts.py) — the
  BaseTool that calls voicebox REST. Talks REST directly so FastMCP's
  structuredContent wrap doesn't affect production code paths.
- `/opt/voicebox/backend/backends/base.py` — `is_model_cached()` source.