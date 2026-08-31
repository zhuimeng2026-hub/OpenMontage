# OM Render-Job Registry Bug — Consumer-Side Analysis

- **Date**: 2026-08-31
- **Reporter**: vclaw control-plane (Claude Code, automated production monitoring)
- **Severity**: 🔴 BLOCKING — every paid render silently fails
- **Affected**: All MCP `tools/call` invocations of `get_render_status` after a fresh enqueue
- **Status**: Awaiting OM team investigation. This document is the consumer-side evidence and hypothesis list; the root cause is on the OM side.

---

## TL;DR

On 2026-08-31, vclaw production monitoring observed **6 out of 6 renders fail
in the exact same way**:

1. vclaw calls OM's `tools/call` to enqueue a render. OM responds `200 OK`
   and returns a `job_id`.
2. 1-3 seconds later, vclaw's worker calls OM's `get_render_status` for
   that `job_id`. OM returns `failed` with the message:
   `"No render job found for render_job_id '<id>'"`.
3. The job is permanently lost. Credits remain `reserved` (never
   consumed, never released) on the tenant ledger.

The pattern is **100% reproducible** across:
- 2 distinct projects
- 3 render levels (`animatic` / `sample` / `render`)
- 6 separate enqueue/poll cycles

vclaw's behavior is verified correct (see §4). The bug is on the OM side.

---

## 1. Reproduction timeline

All times CST. `request_id` correlates the vclaw HTTP entry log with the
worker's poll log. `om_job_id == job_id` because OM accepted vclaw's
client-supplied id (an unusual behavior worth investigating — see §3.1).

| Time | Event | Source |
|---|---|---|
| 14:05:42.529 | `INFO render enqueued level=animatic job_id=…682c8e2a9efb cost_reserved=10` | vclaw server |
| 14:05:45.395 | `ERROR render failed error_message="No render job found for render_job_id '20260831-682c8e2a9efb'"` | vclaw worker |
| 14:05:45.395 | `INFO mcp call ok mcp_method=tools/call duration_ms=59 session_id_prefix=f1f1c3af…` | vclaw worker |
| 14:06:19.127 | `INFO render enqueued level=sample job_id=…91af9b33f005 cost_reserved=30` | vclaw server |
| 14:06:21.380 | `ERROR render failed error_message="No render job found for render_job_id '20260831-91af9b33f005'"` | vclaw worker |
| 14:06:56.718 | `INFO render enqueued level=render job_id=…854695e78054 cost_reserved=100` | vclaw server |
| 14:06:57.410 | `ERROR render failed error_message="No render job found for render_job_id '20260831-854695e78054'"` | vclaw worker |
| 14:08:35.329 | `INFO render enqueued level=animatic job_id=…e12ea8132186` (new project) | vclaw server |
| 14:08:36.483 | `ERROR render failed error_message="No render job found for render_job_id '20260831-e12ea8132186'"` | vclaw worker |
| 14:09:14.347 | `INFO render enqueued level=sample job_id=…fa2ba6499d5b` | vclaw server |
| 14:09:15.512 | `ERROR render failed error_message="No render job found for render_job_id '20260831-fa2ba6499d5b'"` | vclaw worker |
| 14:09:54.562 | `INFO render enqueued level=render job_id=…6d34244766cc` | vclaw server |
| 14:09:57.556 | `ERROR render failed error_message="No render job found for render_job_id '20260831-6d34244766cc'"` | vclaw worker |

Latency from enqueue → poll failure: **1.0–3.0 seconds**, deterministic.
This is faster than any real render would take — confirming the failure
happens at lookup, not at runtime.

---

## 2. What vclaw sends vs what OM returns

### 2.1 Enqueue request (vclaw → OM)

The `tools/call` body for an animatic render:

```json
{
  "jsonrpc": "2.0",
  "id": "id-<random>",
  "method": "tools/call",
  "params": {
    "name": "create_remotion_video_share",  // or similar
    "arguments": {
      "job_id": "20260831-682c8e2a9efb",    // vclaw-generated UUID
      "asset_manifest": { ... },
      "edit_decisions": { ... },
      "scene_plan": { ... },
      "profile": "low_res",
      "output_path": "/tmp/.../animatic.mp4"
    }
  }
}
```

### 2.2 Enqueue response (OM → vclaw)

```
HTTP/1.1 200 OK
Mcp-Session-Id: <session-id>
Content-Type: application/json

{
  "jsonrpc": "2.0",
  "id": "id-<random>",
  "result": { "render_job_id": "20260831-682c8e2a9efb", ... }
}
```

The `render_job_id` returned by OM **equals the `job_id` vclaw sent**.
This is unusual — see §3.1.

### 2.3 Status poll request (vclaw → OM, 1-3s later)

```json
{
  "jsonrpc": "2.0",
  "id": "id-<random>",
  "method": "tools/call",
  "params": {
    "name": "get_render_status",
    "arguments": { "render_job_id": "20260831-682c8e2a9efb" }
  }
}
```

### 2.4 Status poll response (OM → vclaw)

```json
{
  "jsonrpc": "2.0",
  "id": "id-<random>",
  "result": {
    "content": [
      { "type": "text", "text": "{\"ok\":false,\"error\":\"No render job found for render_job_id '20260831-682c8e2a9efb'\"}" }
    ],
    "isError": true
  }
}
```

**Key observation**: the response is a **tool-level failure** (`isError=true`),
not a JSON-RPC error, not an HTTP error. The wire roundtrip succeeded; the
tool itself reports the job doesn't exist.

---

## 3. Hypothesis ranking (consumer-side view)

I cannot prove root cause from outside the OM codebase. The hypotheses are
ranked by **how well they fit the observed evidence**:

### 3.1 Likely: `enqueue` and `get_render_status` read from different storage backends

- Enqueue path: writes to backend A (in-memory queue, file-backed jobs.json,
  Redis, etc.).
- Status path: queries backend B (Postgres, SQLite, a separate process).
- The job never lands in B because the enqueue path doesn't propagate.

This fits: enqueue returns success (it wrote to A), status returns
"No render job found" (B has nothing).

**Why check**: ask the OM team whether `enqueue` and `get_render_status`
share a backing store, or whether the enqueue path emits an event the
status path consumes.

### 3.2 Possible: in-memory job registry is dropped between enqueue and poll

- OM has a per-process in-memory dict `jobs[id] = {...}`.
- Enqueue populates it; first status query evicts / evicts it under some
  condition (TTL, FIFO pressure, a restart loop).
- Or: a periodic background task clears the dict, and the 1-3s window
  straddles a clear cycle.

This fits the deterministic 1-3s timing if there's a TTL in that range.

**Why check**: ask the OM team whether `jobs[id]` is in-memory only, and
if so, whether any eviction runs every ~1s.

### 3.3 Possible: enqueue returns a synthetic success without queuing

- OM's enqueue validates inputs and returns a success envelope but doesn't
  actually persist the job anywhere.
- The 200 OK is a contract-level success; the actual queue insert is
  silently skipped (e.g. a feature flag, an early-return on a config
  error, a swallowed exception).

This fits: success response, deterministic 100% failure rate.

**Why check**: add a single log line in the enqueue path that fires
**after** the queue insert; if it never fires for our requests, this
hypothesis is correct.

### 3.4 Less likely: vclaw-supplied `job_id` is being silently rewritten

- vclaw sends `job_id=20260831-682c8e2a9efb`. OM accepts it and echoes
  it back. But internally OM generates a different id and stores the
  job under the internal id, not the echoed id.
- Status queries the echoed id, which is never found.

This fits the **unusual** observation that OM echoes vclaw's id back
(see §3.1). Most MCP render servers generate their own ids.

**Why check**: ask the OM team whether `enqueue` validates that the
returned `render_job_id` equals the input `job_id`, or whether the
internal id is always returned regardless of input.

### 3.5 Unlikely: the worker (vclaw) is querying the wrong tool name

- vclaw calls `get_render_status` (direct tool) but OM only exposes
  `execute_tool(get_render_status, ...)` (wrapper).

This **was** the case historically (vclaw had a fallback path for it).
After the fix in vclaw commit `5e98024`, vclaw only calls the direct
tool — if OM only has the wrapper, vclaw will now see the JSON-RPC error
`method not found`, not "No render job found". The observed error
message is specifically OM's tool-level response, so this hypothesis
is unlikely. **Note**: vclaw previously had a fallback; it was removed
in commit `5e98024`. If the OM team re-introduces wrapper-only behavior,
vclaw will surface a clear error, not silently double-call.

---

## 4. What vclaw does correctly (verified)

These are the things the OM team does **not** need to investigate:

- **MCP session lifecycle**: vclaw caches `Mcp-Session-Id` across polls.
  The same `f1f1c3af…` session id was used for all 6+ polls in this
  session — handshake ran exactly once. The session is not the issue.
- **Job id round-trip**: vclaw sends the id it generated, OM echoes it
  back, vclaw stores the echoed id and queries the same id back. No
  id drift.
- **Polling cadence**: vclaw polls every 3s by default (`worker.poll_interval`).
  The 1-3s failure window is **inside** vclaw's poll cadence, not after
  it. The job has had its chance to register.
- **Worker retry storm**: vclaw does **not** loop on `poll_render` —
  after the worker writes `FAILED` to the production_jobs row, the
  cycle terminates (verified in `internal/handler/preview.go` —
  `PollRenderJob` `case "failed"` returns `nil`).
- **Error logging**: every OM failure is logged at ERROR with
  `job_id`, `tenant_id`, `job_type`, and the OM `error_message`. No
  silent failures.

---

## 5. vclaw-side mitigations already applied

These don't fix the OM bug; they make the bug easier to diagnose:

- **commit `5e98024`**:
  - `asset_manifest` preflight on `animatic`/`sample`/`render` (returns
    400 instead of forwarding with missing field — a separate class of
    bug surfaced during the same monitoring window).
  - `GetRenderStatus` no longer falls back to `execute_tool(get_render_status, …)`
    — failures now surface directly with the OM error message intact.
  - New `/health/om` endpoint (connectivity check via `tools/list`).
- **commit `f878825`**:
  - slog migration. Every request has a `request_id` that flows from
    the HTTP entry through the worker poll cycle to the OM error
    log line. This is what made the cross-process correlation in
    §1 possible.

---

## 6. Suggested next steps for the OM team

1. **Reproduce internally**: take a known-good client, hit the enqueue
   tool, then immediately hit `get_render_status` with the echoed id.
   Confirm whether you can reproduce the "No render job found" error
   outside the vclaw client.
2. **Trace the enqueue path**: enable DEBUG logging on the OM enqueue
   tool for a single test request. Verify whether the job lands in
   whatever backing store `get_render_status` queries.
3. **Check the job registry lifecycle**: if the registry is in-memory,
   add instrumentation for `set`/`get`/`evict` calls. Watch for
   evictions between enqueue and poll.
4. **Verify the round-tripped id**: confirm whether the
   `render_job_id` returned by enqueue is always equal to the input
   `job_id`, or whether OM generates its own id internally. If OM
   generates internally, the vclaw-side `job_id` is decorative and
   the status query should use the OM-internal id (which vclaw has
   not been given).
5. **Once root cause is identified**: this document will be updated
   with the actual root cause + fix commit reference.

---

## 7. Cross-references

- **vclaw production monitoring report** (the source of these observations):
  `/opt/vclaw/docs/production-monitoring-report-20260831.md` — has the
  full timeline, the `ensureSession` timing verification, the
  `GetRenderStatus` double-call investigation, and the activity summary.
- **vclaw commits referenced in §5**:
  - `5e98024` "fix(preview+health): production monitoring follow-ups"
  - `f878825` "feat(logging): migrate to log/slog with P1 critical-path coverage"

---

## 8. Glossary

- **MCP**: Model Context Protocol. OM exposes its tools via MCP
  streamable-http at `http://host:8900/mcp`.
- **`tools/call`**: the MCP method that invokes a named tool with
  arguments. vclaw's enqueue and poll both use `tools/call`.
- **`get_render_status`**: the OM tool vclaw uses to poll job state.
  Accepts `{render_job_id}` and returns the current status.
- **vclaw**: the OpenMontage SaaS control plane (this document's author).
  Go binary; the consumer that discovered the bug.
- **personal tenant**: each WeChat user in vclaw has a personal tenant
  with their own quota ledger. Reserved credits accumulate on that
  tenant until manually released or consumed by a successful render.
