# Voicebox Integration Prerequisites

> Audience: AI coding agents (Claude Code, Cursor, Cline, …) running voicebox-backed
> TTS in this OpenMontage repo at `/opt/OpenMontage_Voicebox/`, optionally against
> a local voicebox backend at `/opt/voicebox/`.

This document exists because voicebox's TTS engines — including Kokoro, the only
fully-bundled one — still need model weights on disk before they can synthesize
audio. The download happens once per host. After it completes, voicebox runs
fully offline (`HF_HUB_OFFLINE=1` is honored).

## TL;DR — minimum to make voicebox TTS work

```bash
# 1. Make HF reachable through the local proxy
export HTTPS_PROXY=http://127.0.0.1:7890
export HTTP_PROXY=http://127.0.0.1:7890

# 2. Download Kokoro-82M weights + voices (~340 MB total)
/root/.pyenv/versions/3.11.8/bin/python <<'PY'
import os
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='hexgrad/Kokoro-82M',
    cache_dir=os.path.expanduser('~/.cache/huggingface'),
)
PY

# 3. Confirm voicebox sees the cache
curl -s -H 'X-Voicebox-Client-Id: probe' http://127.0.0.1:17493/health
# Expected: "status":"healthy" — health endpoint doesn't report model state,
# but voicebox will lazy-load Kokoro on first POST /generate with engine=kokoro.
```

If `/opt/voicebox/backend/main.py` isn't already running, start it:

```bash
cd /opt/voicebox && python -m backend.main --host 127.0.0.1 --port 17493 \
  --data-dir /opt/voicebox/data
```

If `/opt/OpenMontage_Voicebox/mcp_server.py` isn't already running, start it:

```bash
cd /opt/OpenMontage_Voicebox && python mcp_server.py
```

Both servers expose `/health`. They return 200 when ready.

## Why this is necessary

Voicebox exposes 7 TTS engines (per `/opt/voicebox/README.md`):

| Engine | HuggingFace repo | Approx. size | Pre-bundled? |
| --- | --- | --- | --- |
| Kokoro-82M | `hexgrad/Kokoro-82M` | 340 MB | ❌ (smallest) |
| LuxTTS | `YatharthS/LuxTTS` | 1 GB | ❌ |
| Chatterbox Turbo | `ResembleAI/chatterbox-turbo` | 1 GB | ❌ |
| Chatterbox Multilingual | `ResembleAI/chatterbox` | 2 GB | ❌ |
| HumeAI TADA | (4 repos) | 1–2 GB | ❌ |
| Qwen CustomVoice | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` | 3–4 GB | ❌ |
| Qwen3-TTS (0.6B / 1.7B) | `Qwen/Qwen3-TTS-12Hz-1.7B-Base` | 1.5–4 GB | ❌ |

**No voicebox engine ships pre-bundled.** Each one calls
`snapshot_download(repo_id=...)` on first use (see
`/opt/voicebox/backend/backends/{kokoro,luxtts,chatterbox_*}_backend.py`).
Once cached, voicebox honors `HF_HUB_OFFLINE=1` and runs without network —
proven by `/opt/voicebox/backend/tests/test_offline_guard.py`.

**Kokoro is the right minimum.** At 340 MB it's the smallest, runs on CPU at
real-time, and has 50+ preset voices across 8 languages
(`/opt/voicebox/backend/backends/kokoro_backend.py:42`).

## Network access

HuggingFace is **not directly reachable** from this host. Verified failure
mode: `HTTPSConnectionPool(host='huggingface.co', port=443): Network is unreachable`.

The local HTTP proxy at `http://127.0.0.1:7890` is reachable and works for HF
(verified `GET https://huggingface.co/api/models/hexgrad/Kokoro-82M` → `200`).

`huggingface_hub` does **not** automatically read `HTTP_PROXY`/`HTTPS_PROXY`
in all cases. Set them explicitly before any download call:

```bash
export HTTPS_PROXY=http://127.0.0.1:7890
export HTTP_PROXY=http://127.0.0.1:7890
export HF_HUB_DOWNLOAD_TIMEOUT=120
```

If the proxy is unreachable, the download will fail with a connection error —
confirm first with:

```bash
curl -s -m 5 -o /dev/null -w '%{http_code}\n' \
  -x http://127.0.0.1:7890 https://huggingface.co/api/models/hexgrad/Kokoro-82M
# Expected: 200
```

## Where the cache lives

`huggingface_hub` defaults to `~/.cache/huggingface/hub/` (the
`HF_HUB_CACHE` environment variable can override). On this host that's
`/root/.cache/huggingface/hub/`. voicebox's `is_model_cached()` function
(`/opt/voicebox/backend/backends/base.py:25`) reads from the same path via
`huggingface_hub.constants.HF_HUB_CACHE`.

Do **not** symlink or move the cache — voicebox verifies cache state via
`HF_HUB_CACHE` + a snapshot-directory glob. A symlinked cache works as long
as the symlink target contains the same `models--<repo>/snapshots/<sha>/`
layout.

Disk budget per engine (rough):

| Engine | Snapshot size |
| --- | --- |
| Kokoro-82M | 340 MB |
| LuxTTS | 1 GB |
| Chatterbox (each variant) | 1–2 GB |
| Qwen3-TTS 1.7B | 3–4 GB |
| Qwen CustomVoice 1.7B | 3–4 GB |
| HumeAI TADA | 1–2 GB |

`/root` has 283 GB free. Multi-engine installations fit comfortably.

## Step-by-step: download Kokoro-82M via the proxy

```bash
export HTTPS_PROXY=http://127.0.0.1:7890
export HTTP_PROXY=http://127.0.0.1:7890

/root/.pyenv/versions/3.11.8/bin/python <<'PY'
import os
from huggingface_hub import snapshot_download
path = snapshot_download(
    repo_id='hexgrad/Kokoro-82M',
    cache_dir=os.path.expanduser('~/.cache/huggingface'),
)
print('cached at:', path)
PY
```

Total wall-clock: ~90 seconds for the full model (≈340 MB).

**Important:** when filtering files, do **not** restrict to `*.pt` — Kokoro's
main weights file is named `kokoro-v1_0.pth` (the extension is `.pth`, not
`.pt`). Either download everything (default) or include both globs.

## Step-by-step: start the servers

```bash
# Voicebox backend (REST + MCP on the same port)
/opt/voicebox/.venv/bin/python -m backend.main \
  --host 127.0.0.1 --port 17493 --data-dir /opt/voicebox/data &

# OpenMontage MCP server (REST + reverse-proxy at /voicebox/mcp/*)
/opt/OpenMontage_Voicebox/.venv/bin/python mcp_server.py &
```

Both expose `/health`:

```bash
curl -s http://127.0.0.1:17493/health | python3 -m json.tool
curl -s http://127.0.0.1:8900/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Authorization: Bearer $MCP_API_TOKEN' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
```

## Step-by-step: verify Kokoro actually generates audio

Once the cache is warm, kick off a generation through voicebox REST:

```bash
# Find a Kokoro preset voice id
curl -s -H 'X-Voicebox-Client-Id: probe' \
  http://127.0.0.1:17493/profiles/presets/kokoro | python3 -m json.tool
# Pick one, e.g. af_heart

# Resolve voicebox's internal "preset profile" name. Voicebox stores
# preset voices under a single synthetic profile named for the engine;
# the exact mechanism is in /opt/voicebox/backend/services/profiles.py.
# Easiest path: use list_profiles to find a preset profile, or call MCP speak:
```

The reliable path is MCP:

```bash
curl -s -X POST http://127.0.0.1:17493/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'X-Voicebox-Client-Id: probe' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
# Capture the Mcp-Session-Id header, then:
curl -s -X POST http://127.0.0.1:17493/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'X-Voicebox-Client-Id: probe' \
  -H "Mcp-Session-Id: <id-from-above>" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"voicebox.speak","arguments":{"profile":"af_heart","text":"hello world","engine":"kokoro","language":"en"}}}'
```

The response contains a `generation_id`. Poll its status:

```bash
curl -s -H 'X-Voicebox-Client-Id: probe' -H 'Accept: text/event-stream' \
  http://127.0.0.1:17493/generate/<gen_id>/status
```

Then download the audio:

```bash
curl -s -o /tmp/kokoro_smoke.wav \
  -H 'X-Voicebox-Client-Id: probe' \
  http://127.0.0.1:17493/audio/<gen_id>
file /tmp/kokoro_smoke.wav  # Expected: "WAVE audio"
```

## Mirroring the cache to `/opt/voicebox/`

`/opt/voicebox/` does not store TTS model weights — voicebox reads them from
`~/.cache/huggingface/hub/` (the global HuggingFace cache) via the
`HF_HUB_CACHE` constant. **The cache lives in the user home, not inside
`/opt/voicebox/`.** This is the same convention as every other HuggingFace
downstream consumer.

If you need a portable cache that travels with the voicebox checkout, set
`HF_HUB_CACHE=/opt/voicebox/data/hf_cache` in the voicebox startup
environment, then download into that path:

```bash
export HF_HUB_CACHE=/opt/voicebox/data/hf_cache
export HTTPS_PROXY=http://127.0.0.1:7890

/root/.pyenv/versions/3.11.8/bin/python <<'PY'
import os
from huggingface_hub import snapshot_download
snapshot_download(repo_id='hexgrad/Kokoro-82M')  # honors HF_HUB_CACHE
PY
```

…and add `--hf-cache /opt/voicebox/data/hf_cache` to voicebox's startup if
voicebox exposes such a flag (or wrap with `HF_HUB_CACHE=...`).

## Verification script

Run this to confirm the whole chain is healthy:

```bash
/root/.pyenv/versions/3.11.8/bin/python <<'PY'
import os, sys, json, requests

# 1. Cache
cache = os.path.expanduser('~/.cache/huggingface/hub/models--hexgrad--Kokoro-82M')
weights = os.path.join(cache, 'snapshots')
ok = os.path.isdir(weights) and any(
    f.endswith('.pth') for root,_,files in os.walk(weights) for f in files
)
print(f"[1] Kokoro cache present: {ok}")

# 2. Proxy
try:
    r = requests.get('https://huggingface.co/api/models/hexgrad/Kokoro-82M',
                     timeout=5,
                     proxies={'http':'http://127.0.0.1:7890','https':'http://127.0.0.1:7890'})
    print(f"[2] Proxy HF reach: status={r.status_code}")
except Exception as e:
    print(f"[2] Proxy HF reach: FAILED ({type(e).__name__})")

# 3. Voicebox REST
try:
    r = requests.get('http://127.0.0.1:17493/health', timeout=3,
                     headers={'X-Voicebox-Client-Id':'prereq-check'})
    print(f"[3] Voicebox REST: status={r.status_code} body={r.text[:120]}")
except Exception as e:
    print(f"[3] Voicebox REST: FAILED ({e})")

# 4. OpenMontage MCP
try:
    r = requests.post('http://127.0.0.1:8900/mcp/',
                      headers={'Content-Type':'application/json',
                               'Accept':'application/json, text/event-stream',
                               'Authorization':f'Bearer {os.environ["MCP_API_TOKEN"]}'},
                      json={'jsonrpc':'2.0','id':1,'method':'initialize',
                            'params':{'protocolVersion':'2024-11-05','capabilities':{},
                                      'clientInfo':{'name':'prereq','version':'0'}}},
                      timeout=3)
    print(f"[4] OpenMontage MCP: status={r.status_code} session_header={'mcp-session-id' in {k.lower() for k in r.headers.keys()}}")
except Exception as e:
    print(f"[4] OpenMontage MCP: FAILED ({e})")
PY
```

Expected output: all four checks pass. Any failure blocks voicebox-backed TTS
for `OpenMontage_Voicebox` pipelines.

## When agents need MORE engines

If a pipeline requests a non-Kokoro engine (LuxTTS, Chatterbox, Qwen, …) and
that engine's repo isn't cached, voicebox will fail synthesis with:

```
We couldn't connect to 'https://huggingface.co' to load the files,
and couldn't find them in the cached files.
```

Repeat the snapshot_download dance with the matching repo id (see the engine
table above). Voicebox checks via `backends/base.py:is_model_cached()` on
every generation.

## Files in this repo that depend on the prerequisite

- `tools/audio/voicebox_tts.py` — `voicebox_tts` BaseTool, auto-registered by
  `tools/tool_registry.py`. Calls voicebox REST directly. Depends on the cache
  via voicebox's `is_model_cached` check.
- `tools/audio/tts_selector.py` — runtime TTS provider selector. Routes to
  `voicebox_tts` when intent matches (cloning/privacy/local) or when caller
  sets `preferred_provider="voicebox"`. Will fail synthesis if no engine is
  cached.
- `mcp_server.py` — exposes `voicebox_clone_voice`, `voicebox_tts`,
  `voicebox_list_cloned_voices` MCP tools (lines 708-870). All three depend
  on voicebox REST being live and, for `text_to_speech`, on at least one
  cached engine.
- `tests/integration/test_voicebox_rest.py` and the two MCP test files —
  require voicebox to be live; the TTS roundtrip test additionally requires
  a cached model.
- `.mcp.json` — wires Claude Code's MCP client to `http://127.0.0.1:17493/mcp`,
  so voicebox must be reachable on loopback for the agent's tools to work.
- `.agents/skills/voicebox/SKILL.md` — Layer-3 documentation for the agent
  (when to pick voicebox over cloud providers, engine selection matrix).