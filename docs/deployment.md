# Voicebox × OpenMontage MCP — Deployment Runbook

> Date: 2026-08-19 · Status: **live locally, awaiting production deploy on VPS**
>
> This document is the deployment counterpart to `scenarios.md`. It records
> exactly which files were modified, where the running artifacts live, and how
> to verify the chain end-to-end after deploy.

---

## What Was Built

Voicebox MCP is now served from the **same port** as OpenMontage MCP
(8900) via an internal ASGI reverse-proxy mount. This means there is one
IPv6 ingress (`lanes.ymxt.top:8900`), one TLS cert, one Bearer auth layer.
The standalone `mcp-proxy-multi` Go binary is now optional fallback only —
the main path is `BFF :8090 → lanes.ymxt.top:8900/voicebox/mcp/`.

```
OpenClaw / Claude Code (remote)
        │
        ├──> https://render.mengxa.com/api/voicebox-mcp   (or :8090 locally)
        │       └──> [FrameFlow BFF :8090, Bearer auth, stateless proxy]
        │              └──> http://lanes.ymxt.top:8900/voicebox/mcp/
        │                     └──> [OpenMontage :8900 internal reverse proxy]
        │                            └──> http://127.0.0.1:17493/mcp/
        │
        └──> https://render.mengxa.com/api/mcp-raw       (or :8090 locally)
                └──> [FrameFlow BFF :8090, Bearer auth, SessionStore-backed]
                       └──> http://lanes.ymxt.top:8900/mcp

Optional fallback (still wired):
        relay :18800 (mcp-proxy-multi) — same multi-upstream capability,
        retained as hot standby. NOT on the production hot path.
```

The full IPv6-direct path (Scenario 1) is unchanged: any IPv6 client can
hit `lanes.ymxt.top:8900/voicebox/mcp/` or `lanes.ymxt.top:8900/mcp`
directly with `Authorization: Bearer <MCP_API_TOKEN>` — no BFF needed.
```

Three pieces:

| Piece | Lives on | Repo |
|-------|----------|------|
| Voicebox body cap (256 MB) | voicebox backend | `/opt/voicebox` |
| Multi-upstream MCP relay (Go) | this host, port :18800 | `/opt/OpenMontage` (`OpenMontage-mcp-proxy/`) |
| FrameFlow BFF + Bearer auth | this host (test :8090) or Tencent VPS (prod) | `/opt/OpenMontage` (`frameflow/bff/`) |

---

## Files Changed (Uncommitted on Source Repos)

These are real working changes on the source repos. They are NOT yet
committed to voicebox or OpenMontage main — review and commit on those
repos after this runbook is verified in production.

### `/opt/voicebox` — body cap lift

**`backend/main.py`** — `uvicorn.Config(client_max_size=256*1024*1024)` set
explicitly. Default uvicorn cap was 16 MB, which clipped
`voicebox.transcribe` / `voicebox.analyze_sample` audio uploads from
external callers.

### `/opt/OpenMontage/OpenMontage-mcp-proxy/main.go` — multi-upstream

Refactored from single-upstream (`/mcp` only) to a path-dispatching relay
supporting:

- `/mcp` + `/mcp/` → OpenMontage MCP (Bearer `MCP_API_TOKEN`, path-rewriting,
  `/mcp` no-trailing-slash because Starlette `mount("/mcp", ...)` routes
  there)
- `/render-progress/*` → OpenMontage SSE (path-preserving)
- `/voicebox` + `/voicebox/` → Voicebox MCP (NO Authorization header;
  pass-through caller `X-Voicebox-Client-Id`, default fallback
  `voicebox-relay`; trailing-slash because FastMCP `mount("/mcp", ...)`
  requires it)

All routes share `Authorization: Bearer PROXY_CLIENT_TOKEN` as the proxy's
own auth. The new binary is `mcp-proxy-multi` (9.5 MB Go binary).

### `/opt/OpenMontage/frameflow/bff/` — Bearer auth + raw route

| File | Change |
|------|--------|
| `internal/config/config.go` | Added `ExternalAgentToken` (env `EXTERNAL_AGENT_TOKEN`) |
| `handlers/auth.go` | Added `RequireBearer()` middleware + `renderQueueOwnerIDForAgent(token)` |
| `handlers/mcp.go` | Added `MCPRawProxy(c)` — transparent JSON-RPC passthrough (256 MB body cap) |
| `internal/mcp/client.go` | Added `Client.RawSend(method, body)` — forwards arbitrary JSON-RPC envelopes, captures rotating `Mcp-Session-Id` |
| `internal/mcp/session.go` | Added `RawCall(sessionID, method, body)` + `SessionIDForOwner(sessionID)` |
| `main.go` | Registers `POST /api/mcp-raw` only when `EXTERNAL_AGENT_TOKEN` is set |

The route is **mount-on-demand**: if `EXTERNAL_AGENT_TOKEN` is unset, the
route is not registered at all (fail-closed default).

---

## Live State on This Host

| Service | URL | PID | Process |
|---------|-----|-----|---------|
| Voicebox FastMCP | `http://127.0.0.1:17493/mcp/` | 2019723 | `python -m backend.main` |
| OpenMontage MCP | `http://127.0.0.1:8900/mcp` | (systemd: openmontage-mcp.service) | `mcp_server.py` |
| Voicebox relay | `http://127.0.0.1:18800/mcp` + `/voicebox/...` | 2026914 | `./mcp-proxy-multi` |
| FrameFlow BFF (test) | `http://127.0.0.1:8090/api/mcp-raw` | 2050538 | `./frameflow-bff` |

### Health checks

```bash
curl -s http://127.0.0.1:18800/health
# {"client_auth":true,"status":"ok","upstreams":[
#   {"name":"openmontage","listen_prefix":"/mcp",...},
#   {"name":"voicebox","listen_prefix":"/voicebox",...}]}

curl -s http://127.0.0.1:17493/health
# {"status":"healthy",...}

curl -s http://127.0.0.1:8900/openapi.json | python3 -c "import json,sys; print('paths:', len(json.load(sys.stdin)['paths']))"
# paths: 100+

curl -s http://127.0.0.1:8090/api/me
# {"authenticated":false}  (because AUTH_REQUIRED=false in test config)
```

### End-to-end smoke (verified)

```bash
RELAY=http://127.0.0.1:18800
TOKEN=test-relay-token-abc123
BFF=http://127.0.0.1:8090
BEARER=test-bff-bearer-xyz789

# Voicebox via relay
SID=$(curl -s -m 5 -i -X POST $RELAY/voicebox/mcp/ \
  -H "Authorization: Bearer $TOKEN" -H "X-Voicebox-Client-Id: e2e" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"e2e","version":"0"}}}' \
  | grep -i 'mcp-session-id:' | awk '{print $2}' | tr -d '\r')
curl -s -X POST $RELAY/voicebox/mcp/ -H "Authorization: Bearer $TOKEN" \
  -H "mcp-session-id: $SID" -H "X-Voicebox-Client-Id: e2e" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | grep -o '"name":"[^"]*"' | head -5
# voicebox.speak, voicebox.transcribe, voicebox.list_captures, voicebox.list_profiles, voicebox.analyze_sample

# OpenMontage via BFF
SID=$(curl -s -m 5 -i -X POST $BFF/api/mcp-raw \
  -H "Authorization: Bearer $BEARER" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"e2e","version":"0"}}}' \
  | grep -i 'mcp-session-id:' | awk '{print $2}' | tr -d '\r')
curl -s -X POST $BFF/api/mcp-raw -H "Authorization: Bearer $BEARER" \
  -H "mcp-session-id: $SID" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | python3 -c "import json,sys; print('om tools:', len(json.loads(sys.stdin.read())['result']['tools']))"
# om tools: 25
```

---

## Production Deployment Steps

### Step 1: Build the multi-upstream relay binary

```bash
cd /opt/OpenMontage/OpenMontage-mcp-proxy
go build -o mcp-proxy-multi .
cp mcp-proxy-multi /opt/voicebox-relay/bin/  # pick a permanent location
```

The relay is single-binary, single-`.env`, no DB. Runs anywhere that can
reach `127.0.0.1:17493` (voicebox) and the BFF's MCP path.

### Step 2: Decide where the relay lives

Two options:

- **Option A** — relay on this host, BFF on the Tencent VPS, agents reach
  the relay via the BFF (cleanest; BFF does both auth and routing)
- **Option B** — relay on the Tencent VPS alongside BFF, BFF reverse-proxies
  `/voicebox/*` → relay on the VPS loopback

Option A is recommended: relay is local to voicebox, BFF stays simple.

### Step 3: Configure the relay `.env`

```
UPSTREAM_MCP_URL=http://127.0.0.1:8900/mcp
UPSTREAM_MCP_TOKEN=<OpenMontage MCP_API_TOKEN>
PROXY_CLIENT_TOKEN=<generate-fresh-32-bytes-hex>
PORT=18800
VOICEBOX_UPSTREAM_URL=http://127.0.0.1:17493/mcp
VOICEBOX_LISTEN_PREFIX=/voicebox
VOICEBOX_DEFAULT_CLIENT_ID=voicebox-relay
LOG_FILE=/var/log/voicebox-relay/proxy.log
```

### Step 4: Configure the BFF on the Tencent VPS

Add to `/opt/OpenMontage/frameflow/bff/.env` on `1.14.182.208`:

```
EXTERNAL_AGENT_TOKEN=<generate-fresh-32-bytes-hex>
```

That's it. The BFF picks up `ExternalAgentToken` automatically and mounts
`POST /api/mcp-raw` guarded by `RequireBearer()`.

### Step 5: Reverse proxy `/voicebox/*` from the BFF to the relay

Add to `/etc/nginx/sites-enabled/render.mengxa.com.conf` on the VPS:

```nginx
location /voicebox/ {
    # External clients (Claude Code, OpenClaw) hit the relay through the BFF
    # origin. The relay enforces its own PROXY_CLIENT_TOKEN.
    proxy_pass http://<this-host-public-or-lan>:18800/voicebox/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    # SSE-friendly timeouts so tool/streaming calls don't get killed
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```

`<this-host-public-or-lan>` must be reachable from the VPS. Today this host
has no inbound reachability (operator NAT); the simplest workaround is to
keep the relay on the VPS loopback and run it as a sidecar to the BFF
(Option B in Step 2).

### Step 6: Client `.mcp.json`

```jsonc
// ~/.openclaw/openclaw.json  or  .mcp.json
{
  "mcpServers": {
    "voicebox": {
      "type": "http",
      "url": "https://render.mengxa.com/voicebox/mcp/",
      "headers": {
        "Authorization": "Bearer <PROXY_CLIENT_TOKEN>",
        "X-Voicebox-Client-Id": "openclaw-<host-id>"
      }
    },
    "openmontage": {
      "type": "http",
      "url": "https://render.mengxa.com/api/mcp-raw",
      "headers": {
        "Authorization": "Bearer <EXTERNAL_AGENT_TOKEN>"
      }
    }
  }
}
```

---

## Risks & Rollback

| Risk | Mitigation |
|------|------------|
| `EXTERNAL_AGENT_TOKEN` leak | Bearer is constant-time compared; rotate by editing `.env` + restart |
| `PROXY_CLIENT_TOKEN` leak | Same; rotate via `.env` |
| Voicebox `X-Voicebox-Client-Id` forged client id | Always go through `/voicebox/*` (relay enforces Bearer); never expose `:17493` directly |
| OpenMontage session loss mid-render | BFF's `RawCall` already retries once on `IsSessionTransportError`; the relay doesn't retry (single upstream) |
| Body cap 256 MB at voicebox, 256 MB at BFF — anything bigger? | Lower bound: voicebox 200 MB declared, BFF passes through. Both caps align with the realistic voicebox upload ceiling |

Rollback: revert the `.env` change on the BFF (drops `/api/mcp-raw` route),
stop the relay binary. The voicebox body cap is safe to keep even if unused.

---

## Token Generation

```bash
# Generate fresh tokens
openssl rand -hex 32   # PROXY_CLIENT_TOKEN
openssl rand -hex 32   # EXTERNAL_AGENT_TOKEN
```

Treat both as production secrets. Do not commit `.env` files. Rotate by
editing `.env` and restarting the respective service.

---

## Files Inventory

| Path | Status |
|------|--------|
| `/opt/voicebox/backend/main.py` | modified (uncommitted) |
| `/opt/OpenMontage/OpenMontage-mcp-proxy/main.go` | modified (uncommitted) |
| `/opt/OpenMontage/OpenMontage-mcp-proxy/mcp-proxy-multi` | built binary |
| `/opt/OpenMontage/frameflow/bff/internal/config/config.go` | modified (uncommitted) |
| `/opt/OpenMontage/frameflow/bff/handlers/auth.go` | modified (uncommitted) |
| `/opt/OpenMontage/frameflow/bff/handlers/mcp.go` | modified (uncommitted) |
| `/opt/OpenMontage/frameflow/bff/internal/mcp/client.go` | modified (uncommitted) |
| `/opt/OpenMontage/frameflow/bff/internal/mcp/session.go` | modified (uncommitted) |
| `/opt/OpenMontage/frameflow/bff/main.go` | modified (uncommitted) |
| `/opt/OpenMontage/frameflow/bff/frameflow-bff` | built binary |
| `/opt/OpenMontage_Voicebox/docs/scenarios.md` | committed (analysis) |
| `/opt/OpenMontage_Voicebox/docs/deployment.md` | this file |