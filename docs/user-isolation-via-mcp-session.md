# User Isolation via MCP Session — v2 Revision

> Date: 2026-09-02
> Status: Plan v2. Phase A/B1–B3 (vclaw stage 1/2/3) **implemented and tested**. Phase C/D pending.
> Goal: Isolate uploads, session state, projects, jobs, renders, and reads by authenticated principal from the first write.

---

## v1 → v2 corrections

The v1 direction was right (vclaw emits trusted principal headers → OM consumes them → tools write under per-principal namespace). v2 fixes three real defects that v1 had:

1. **FastMCP ContextVar does not cross ASGI task boundary.** `BearerTokenAuthMiddleware` sets `current_user_id` in the per-request ASGI task, but stateful Streamable HTTP runs tools in a per-session background task. A ContextVar set in middleware is invisible to the tool. Fix: use `Mcp-Session-Id` as the lookup key against a durable session→principal registry.
2. **Path whitelist permits `.` / `..`.** v1's `[a-zA-Z0-9\-_.]{1,128}` accepts `.` and `..`. In practice `users.id = newID()` never produces them so the collision risk is zero, but it's a theoretical hole. Fix: derive `namespace_key = HMAC(secret, principal_id)`. The output has no `.`, no separator, no leading char class issue.
3. **Phase 4 file list was incomplete.** v1 estimated "~150 lines" based on the few write paths I had spotted. Reality is a cross-cutting workspace migration that touches every reader/writer/job/render code path that names `projects/`. Fix: a single `PrincipalResolver` + namespace-aware `ProjectWorkspace` used everywhere.

---

## What's already shipped (compatible with v2)

| Stage | Commit | What it does |
|---|---|---|
| A1 (vclaw stage 1) | `f3b775d` | `MCPProxyHandler` forwards `X-VClaw-User-Id`; `MCPRawProxyHandler` looks up session → user binding in `mcp_sessions` table and forwards the header |
| A2 (vclaw stage 2) | `d7b70bb` | `MCPRawProxyHandler` rejects unbound sessions with 401 SESSION_NOT_BOUND; DB errors → 500 SESSION_LOOKUP_FAILED; missing session id → allowed (handshake) |
| B1 (OM stage 3) | `242940e` | `BearerTokenAuthMiddleware` reads `x-vclaw-user-id` header → `_user_id_ctx` ContextVar (only after MCP_API_TOKEN passes); 20 ASGI unit tests |

These already match v2's intent. Phase C must extend them with the durable registry path; nothing is invalidated.

---

## Phase plan

| Phase | Scope | Repo | Done? |
|---|---|---|---|
| **A** | vclaw JWT scopes + tenant-preserving refresh + initialize binds session | vclaw | yes |
| **B** | OM trusted-proxy boundary + durable `session_id → principal` registry | OM | partial (ContextVar only) |
| **C** | Central `PrincipalResolver` + `namespace_key = HMAC(...)` + full audit of every read/write/path | OM | pending |
| **D** | Legacy migration + strict enforcement + remove unsafe compatibility paths | OM | pending |

Phase B in v2 is a superset of OM stage 3: keep the ContextVar as a fast-path cache, add the durable registry as the authoritative source.

---

## Phase B — durable session→principal registry

**Why**: A `ContextVar` set in middleware does not reach the FastMCP tool execution context. We need a registry that the tool can consult via the session id it already has.

**Module**: `lib/principal_registry.py` (new). API:

```python
@dataclass(frozen=True)
class Principal:
    kind: Literal["user", "service"]
    principal_id: str         # users.id or service_id
    tenant_id: str | None
    namespace_key: str        # HMAC(secret, principal_id)

def bind(session_id: str, principal: Principal) -> None
def lookup(session_id: str) -> Principal | None
def require(session_id: str) -> Principal  # raises when missing
```

**Storage**: SQLite table (mirrors `mcp_sessions` pattern in vclaw). Multi-worker safe via the existing fcntl-fallback pattern in `lib/workbuddy_session.py` (Windows fallback: retry on `os.replace` PermissionError, document the gap).

**Lifecycle**:
- OM middleware calls `bind(session_id, principal)` after MCP_API_TOKEN check + sanitised header read.
- Tool execution calls `require(get_mcp_session_id())` and gets the principal back.
- Session timeout (already enforced by vclaw stage 2) → binding expires.

**Header contract** (already in place from vclaw stage 1/2 + OM stage 3):

| Header | Set when | Value |
|---|---|---|
| `X-VClaw-User-Id` | user principal | `<users.id>` |
| (future) `X-VClaw-Principal-Kind` | both | `user \| service` |
| (future) `X-VClaw-Service-Id` | service principal | `<stable service id>` |

For now we only have user header in production; service principal is a v2.1 addition.

---

## Phase C — `PrincipalResolver` + namespace_key + full audit

**Abstractions** (the v2 contract for every read/write):

```python
@dataclass(frozen=True)
class Principal:
    kind: Literal["user", "service"]
    principal_id: str
    tenant_id: str | None
    namespace_key: str

@dataclass
class ProjectWorkspace:
    principal: Principal
    project_id: str
    root: Path
    assets: Path
    artifacts: Path
    renders: Path
    checkpoints: Path
    session_state: Path
    upload_state: Path
```

**Layout**:

```
projects/
    users/<user_namespace_key>/<project_id>/assets/
    users/<user_namespace_key>/<project_id>/artifacts/
    users/<user_namespace_key>/<project_id>/renders/
    users/<user_namespace_key>/<project_id>/checkpoints/
    services/<service_namespace_key>/<project_id>/...
    _system/identity_sessions/<session_id>.json
```

**Audit checklist** (every file that names `projects/` MUST be migrated):

- `lib/workbuddy_session.py` — `_state_path`, session lock, global job index, render status lookup, orphan recovery
- `tools/asset_upload_chunk.py` — `projects/.uploads` state, `.part` files, upload-id/session ownership, final asset paths
- `tools/asset_upload.py` + every reader
- `mcp_server.py` — upload tools, `read_session_asset`, `create_remotion_video_share`, status / SSE / share paths, render dispatch, durable background records
- Render queues, job registries, startup re-dispatch, retry, cancellation
- Pipeline workspace init, checkpoints, artifacts, renders, publish logs
- Backlot project discovery / open / history
- Web/BFF project, thumbnail, download APIs
- Cleanup, expiry, migration, backup, rollback logic
- Every tool that joins `projects/` or accepts an output path

**`read_session_asset` must independently verify the requested resolved path stays inside the current principal's namespace**. BFF whitelisting is not a security boundary.

---

## Phase D — Legacy migration + strict enforcement

**Why mandatory**: the path change is not additive. Existing Backlot / checkpoint / reader / job-recovery won't discover nested workspaces without resolver support.

Requirements:

1. Feature flag `USER_NAMESPACE_V2`. Default off; new sessions write v2 only.
2. Namespace version is fixed at session/job creation; restart recovery is deterministic.
3. Legacy sessions during the bounded migration window may use an owner-verified legacy resolver. Raw unauthenticated calls must NOT be able to read legacy data.
4. Durable jobs store namespace version + key so restart recovery works.
5. Backup `projects/.mcp_sessions/` before cleanup; do not `rm` outright.
7. A rollback build must understand existing v2 paths. Otherwise rollback needs data migration and is not one-commit revert.

---

## Compatibility policy

| Principal | User header | Required namespace | User-only tools |
|---|---|---|---|
| Authenticated user | yes | `projects/users/<HMAC(uid)>/...` | allowed |
| Authenticated service | no user id; service id required | `projects/services/<HMAC(sid)>/...` | denied unless explicitly supported |
| Missing / unknown principal | no | (none) | denied before filesystem access |

**No implicit fallback to `projects/<project_id>/` is allowed** once strict isolation is enabled (Phase D).

---

## Test requirements (v2)

### Auth + bootstrap
- Real WeChat QR callback → approved device login → JWT exchange
- JWT contains correct `uid/tid/did/mcp:use`
- Refresh preserves `uid/tid/scopes`
- Authenticated `initialize` without session succeeds; response session atomically bound
- User A presenting user B's session is rejected
- Missing / expired / forged / wrong-audience JWT rejected
- Raw MCP without service credential rejected and never contacts OM

### OM transport + storage
- Full Streamable HTTP lifecycle: initialize → session-bearing request → real tool execution
- **Tool background task resolves the principal** — an ASGI-only assertion does NOT count
- Two users upload the same filename under the same project id; every physical path is disjoint
- Conflicting session rebind rejected and security-logged
- Forged identity header + direct OM access rejected
- `.` / `..` / reserved names / separators / overlong ids rejected
- Upload temp state, session state, locks, job index, render output, status, SSE, read, cleanup all isolated
- Service principal uses only its service namespace
- Multi-thread / multi-process / restart / retry / cancel / job re-dispatch keep the correct principal
- Legacy → v2 migration + rollback drill pass

---

## Implementation priority

1. Phase A — done ✅
2. Phase B — main work in OM (durable registry)
3. Phase C — cross-cutting, biggest
4. Phase D — deployment

Every phase independently rollbackable.

---

## Cross-references

- vclaw repo: `docs/user-isolation-via-mcp-session.md`
- vclaw stage 1 commit: `f3b775d`
- vclaw stage 2 commit: `d7b70bb`
- OM stage 3 commit: `242940e`