# OM-Side Audit of `render-job-registry-bug-2026-08-31.md`

- **Date**: 2026-08-31
- **Reviewer**: OpenMontage maintainer (this workspace)
- **Subject**: Consumer-side bug report `docs/bugs/render-job-registry-bug-2026-08-31.md` (committed by `425d54c` on `release/mvp-v0.1-phase-0-5`, author 谢生)
- **Verdict**: Phenomenon description is credible; root-cause hypotheses (§3.1–§3.4) are largely incorrect. **No OM-side bug found.** The most likely root cause is on the vclaw client: a job-id mismatch between the id vclaw generates locally and the id OM registers in its job-index.

---

## TL;DR

vclaw reports 6/6 renders failing with `"No render job found for render_job_id '<id>'"`
exactly 1–3 s after enqueue. The error string **is** produced by OM's
`get_render_status` tool (`mcp_server.py:2152`), and OM does return it as a
tool-level failure (`success: false`), matching vclaw's wire-shape observation.
That part is correct.

The rest is wrong:

1. OM's `create_remotion_video_share` does **not** accept a caller-supplied
   `job_id` argument. The `job_id` field in vclaw's documented enqueue payload
   (`§2.1`) is invented — that parameter does not exist on the OM side. OM
   always generates its own `render_job_id` server-side via
   `uuid.uuid4().hex` (`lib/workbuddy_session.py:419`).
2. vclaw's claim "OM echoed vclaw's id back" (`§2.2`) is false. OM has no
   record of vclaw's id because OM never received it.
3. The OM storage backend for jobs is **not** an in-memory dict that could
   be evicted (`§3.2`). It is a disk-persisted job→digest index
   (`projects/.mcp_sessions/.job_index.json`) plus per-session JSON files
   (`projects/.mcp_sessions/<digest>.json`), with atomic write + POSIX
   advisory lock + Windows-retry. `_index_upsert` runs synchronously inside
   the same `_write` flow that returns success to the client.
4. Therefore: `find_session_by_job_id(<id>)` for any id OM never indexed
   returns `None` in O(1) — exactly what vclaw sees. The 1–3 s latency
   matches vclaw's poll cadence, not an OM-side timing bug.

The most plausible real cause: vclaw calls OM's `get_render_status` with the
id it generated locally (e.g. `20260831-682c8e2a9efb`), but that id was
never registered in OM's index because OM rejected it as an unknown
parameter on the enqueue path. OM's response rendered a fresh uuid that
vclaw did not use.

---

## 1. What OM actually does

### 1.1 Enqueue: `create_remotion_video_share`

Signature (`mcp_server.py:1507-1518`):

```python
async def create_remotion_video_share(
    project_id: Optional[str] = None,
    script_id: str = "photo-ken-burns",
    duration_per_image: float = 3.0,
    aspect_ratio: str = "9:16",
    title: Optional[str] = None,
    code: Optional[str] = None,
    queue_owner_id: Optional[str] = None,
    delivery_promise_override: Optional[dict] = None,
    effects: Optional[str] = None,
    subtitles: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
```

There is **no `job_id` parameter**. Any caller-supplied `job_id` is silently
ignored by the MCP schema. `render_job_id` is created server-side:

```python
# lib/workbuddy_session.py:417-426
old_job_id = state.get("render_job_id")
state["status"] = "rendering"
state["render_job_id"] = uuid.uuid4().hex      # <-- OM generates its own
state["failure_stage"] = None
state["error"] = None
_write(_state_path(digest), state)
_index_upsert(state["render_job_id"], digest, old_job_id=old_job_id)
```

The returned id (line 1758) is `state["render_job_id"]` — the OM-generated
uuid, not anything the caller supplied.

### 1.2 Status: `get_render_status`

```python
# mcp_server.py:2140-2152
def get_render_status(render_job_id: str) -> dict[str, Any]:
    state = find_session_by_job_id(render_job_id)
    if not state:
        return {"success": False, "error": f"No render job found for render_job_id '{render_job_id}'"}
```

`find_session_by_job_id` (`lib/workbuddy_session.py:188-200`):

```python
def find_session_by_job_id(job_id: str) -> dict[str, Any] | None:
    if not job_id:
        return None
    with _index_lock:
        digest = _read_index().get(job_id)
    if not digest:
        return None
    return _read(digest)
```

So an unknown id cleanly produces the observed error string.

### 1.3 Storage topology

```
projects/.mcp_sessions/
├── <digest>.json           # per-MCP-session state (assets, status, render_job_id, ...)
├── .job_index.json         # render_job_id -> digest (O(1) lookup)
├── .locks/<digest>.lock    # cross-process POSIX flock (advisory)
└── .render_jobs.json       # durable record written by lib/render_queue (separate path)
```

Both enqueue and status go through the same `.job_index.json` lookup. There
is no second backend, no in-memory dict that could drift out of sync.

---

## 2. Mapping vclaw's hypotheses to OM reality

| Hypothesis | OM reality | Verdict |
|---|---|---|
| **§3.1** enqueue writes to backend A, status queries backend B | Both share `_index_upsert` / `_read_index` (single file). Enqueue and `get_render_status` resolve through the same `_read(digest)` call. | **Not consistent with code.** |
| **§3.2** in-memory registry is evicted in 1–3 s | No in-memory dict. Index is disk JSON with atomic `os.replace` + Windows-retry (`workbuddy_session.py:91-108`). | **Not consistent with code.** |
| **§3.3** enqueue returns synthetic success without persisting | `_run_render_job` is dispatched to a background thread (`mcp_server.py:1777-1828`). The state write + `_index_upsert` happen synchronously before the success response is built (line 1758). A persistence failure has a dedicated error path returning `stage: "queue_persistence"` (line 1745). | **Not consistent with code.** |
| **§3.4** vclaw-supplied `job_id` silently rewritten | OM does not silently rewrite — it ignores the unknown parameter and generates its own uuid. The "echo" vclaw reports is not an echo of its id; vclaw likely re-read the id it sent, not the id OM returned. | **Half-consistent** (rewrite is wrong; "OM ignores caller id" is correct). |
| **§3.5** vclaw calling the wrong tool name | vclaw is calling the right tool (`get_render_status`); the wrongness is in the id, not the tool name. | **Tool name is correct; underlying cause is id mismatch.** |

---

## 3. Most plausible real cause

vclaw generates a job id locally (e.g. `20260831-682c8e2a9efb`), puts it
into the enqueue arguments under a `job_id` field that OM does not know
about, and receives back an OM-generated uuid that vclaw apparently
ignores. vclaw then polls `get_render_status(render_job_id=<vclaw's id>)`,
which never lands in `.job_index.json`. Result: 6/6 deterministic "No
render job found".

If the *only* mismatch is the id, the fix lives on the vclaw side: read
`render_job_id` out of the enqueue response and use it on subsequent polls.
No change needed in OM.

If vclaw already does that and the id still does not appear in
`.job_index.json` after enqueue, then the failure is genuinely in OM and
the suggested debug is:

1. Inspect `projects/.mcp_sessions/.job_index.json` after a vclaw enqueue:
   confirm whether the OM-generated id appears.
3. If absent, check `_index_upsert` logs in OM (search `workbuddy_session.py`
   for any swallowing of exceptions inside `_write_index` / `_write`).
4. If present but `find_session_by_job_id` returns None, the
   `.job_index.json` was overwritten between writes — investigate
   `_write_index` ordering and any concurrent writer.

None of these need to be addressed before the vclaw side is verified.

---

## 4. Secondary red flag — mvpclient.go uses an OM-non-existent MCP tool

`frameflow/bff/internal/mvpclient/client.go:101`:

```go
resp, err := c.mcp.CallTool("video_compose", args)
```

`frameflow/bff/internal/mvpclient/poller.go:69`:

```go
resp, err := p.mcp.CallTool("video_compose", map[string]interface{}{
    "operation":       "status",
    "external_run_id": externalRunID,
})
```

Two issues here:

1. `video_compose` is **not** exposed as an MCP tool. The list of
   `@mcp.tool()`-decorated functions in `mcp_server.py` does not include
   `video_compose`; vclaw would have to call it through the generic
   `execute_tool(name="video_compose", ...)` wrapper.
2. `video_compose.execute` (`tools/video/video_compose.py:514-533`) only
   accepts operations `compose / render / remotion_render / burn_subtitles /
   overlay / encode / remotion_bilingual_overlay`. **`status` is not one of
   them.**

This means the `mvpclient` path as written would always fail with "Tool
'video_compose' not found" or "Unknown operation: status". Since vclaw
reports a different error message ("No render job found for render_job_id
..."), vclaw must be using a different code path than `mvpclient` for this
production traffic — likely the direct `create_remotion_video_share` +
`get_render_status` path that the bug report describes. The mvpclient
package is dead code in this deployment (or under construction for a
different integration).

---

## 5. OM-side design gap (not a bug, but worth a fix)

OM's `create_remotion_video_share` does not accept a caller-supplied
`job_id`. This is the design choice that produces the mismatch observed by
vclaw. Two options if the OM team wants to reduce this class of bug:

- **Accept optional `client_supplied_job_id`**: when provided, write it to
  `.job_index.json` instead of the generated uuid; return it in the
  response. Backward-compatible (default remains generated uuid).
- **Reject obviously-vclaw-shaped ids early**: not a fix; just a band-aid.

Both are low-priority. Fix the vclaw client first.

---

## 6. Next steps

1. vclaw team: confirm whether the in-flight production worker actually
   reads `result.render_job_id` from the OM enqueue response before
   polling `get_render_status`.
2. If yes: inspect OM's `.job_index.json` after one vclaw enqueue cycle to
   see whether the OM-generated id was registered. If absent, the bug is
   in OM (Section 3 above).
3. If no: the bug is in vclaw; fix vclaw to use the id OM returns. The
   `425d54c` bug report can then be closed with a root-cause addendum.
4. Optional OM-side improvement: accept `client_supplied_job_id` on
   `create_remotion_video_share` to remove the gap entirely.

---

## 7. References

- **Reviewed document**: `docs/bugs/render-job-registry-bug-2026-08-31.md` (commit `425d54c`)
- **OM enqueue tool**: `mcp_server.py:1507-1828` (`create_remotion_video_share`)
- **OM status tool**: `mcp_server.py:2140-2175` (`get_render_status`)
- **OM job registry**: `lib/workbuddy_session.py:139-200` (`_INDEX_FILENAME`,
  `_index_upsert`, `find_session_by_job_id`)
- **OM job id generation**: `lib/workbuddy_session.py:402-427` (`begin_render`)
- **OM storage path**: `projects/.mcp_sessions/` (per-session JSON,
  `.job_index.json`, `.locks/<digest>.lock`)
- **vclaw Go client**: `frameflow/bff/internal/mvpclient/client.go:50-106`,
  `poller.go:57-133` (these paths appear unused in current production)