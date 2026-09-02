# User Isolation via MCP Session — v2 Revision

> Date: 2026-09-02
> Status: Plan v2. vclaw A1–A4, the trusted user assertion, and local Studio artifacts are implemented. Raw service identity, TTL enforcement, full path migration, deployment E2E, and production migration drills remain open.
> Goal: Isolate uploads, session state, projects, jobs, renders, and reads by authenticated principal from the first write.

---

## v1 → v2 corrections

The v1 direction was right (vclaw emits trusted principal headers → OM consumes them → tools write under per-principal namespace). v2 fixes four real defects that v1 had:

1. **FastMCP ContextVar does not cross ASGI task boundary.** `BearerTokenAuthMiddleware` sets `current_user_id` in the per-request ASGI task, but stateful Streamable HTTP runs tools in a per-session background task. A ContextVar set in middleware is invisible to the tool. Fix: use `Mcp-Session-Id` as the lookup key against a durable session→principal registry.
2. **Path whitelist permits `.` / `..`.** v1's `[a-zA-Z0-9\-_.]{1,128}` accepts `.` and `..`. In practice `users.id = newID()` never produces them so the collision risk is zero, but it's a theoretical hole. Fix: derive `namespace_key = HMAC(secret, principal_id)`. The output has no `.`, no separator, no leading char class issue.
3. **Phase 4 file list was incomplete.** v1 estimated "~150 lines" based on the few write paths I had spotted. Reality is a cross-cutting workspace migration that touches every reader/writer/job/render code path that names `projects/`. Fix: a single `PrincipalResolver` + namespace-aware `ProjectWorkspace` used everywhere.
4. **Raw `/mcp` session lookup is not caller authentication.** A binding proves who owns a session, not who sent the current request. Desktop traffic must use the JWT-protected vclaw proxy; direct OM/raw traffic needs an explicit service principal and a trusted-proxy boundary.

---

## What's already shipped (compatible with v2)

| Stage | Commit | What it does |
|---|---|---|
| A1 (vclaw stage 1) | `f3b775d` | `MCPProxyHandler` forwards `X-VClaw-User-Id`; `MCPRawProxyHandler` looks up session → user binding in `mcp_sessions` table and forwards the header |
| A2 (vclaw stage 2) | `d7b70bb` | `MCPRawProxyHandler` rejects unbound sessions with 401 SESSION_NOT_BOUND; DB errors → 500 SESSION_LOOKUP_FAILED; missing session id → allowed (handshake) |
| B1 (OM stage 3) | `242940e` | `BearerTokenAuthMiddleware` reads `x-vclaw-user-id` header → `_user_id_ctx` ContextVar (only after MCP_API_TOKEN passes); 20 ASGI unit tests |

These commits remain the base. The current implementation also adds the trusted assertion, durable registry, immutable-owner checks, `ProjectWorkspace`, namespaced upload/read/Claude Video/Remotion paths, and migration scripts. Those additions close the reviewed defects but remain a partial B/C/D implementation.

---

## Phase plan

| Phase | Scope | Repo | Done? |
|---|---|---|---|
| **A** | vclaw JWT scopes + tenant-preserving refresh + initialize binds session + raw service auth | vclaw / Studio | partial (A1–A4, trusted user assertion, local Studio done; deploy/raw service/E2E open) |
| **B** | OM trusted-proxy boundary + durable `session_id → principal` registry | OM | partial (trusted user proxy, registry, immutable owner, old-schema upgrade and key rotation done; service/TTL open) |
| **C** | Central `PrincipalResolver` + `namespace_key = HMAC(...)` + full audit of every read/write/path | OM | partial (upload/read/Claude Video/Remotion main paths done; remaining audited touchpoints open) |
| **D** | Legacy migration + strict enforcement + remove unsafe compatibility paths | OM | partial (flag, v1/v2 reconcile, migration/rollback scripts and focused tests done; production drill/global enforcement open) |

Phase B in v2 is a superset of OM stage 3: the durable registry and trusted user-proxy boundary are now present and reject conflicting owners/unsigned attribution; service identity and TTL enforcement are still missing.

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

**Storage**: SQLite table (mirrors `mcp_sessions` conceptually). The implementation uses WAL, `busy_timeout`, and bounded retry for concurrent writers; the file-lock fallback in `lib/workbuddy_session.py` is unrelated to SQLite consistency.

**Lifecycle**:
- OM middleware calls `bind(session_id, principal)` after MCP_API_TOKEN check + sanitised header read.
- Tool execution calls `require(get_mcp_session_id())` and gets the principal back.
- A binding expiry timestamp is recorded, but lookup does not enforce it yet; timeout enforcement remains Phase D work.

**Header contract** (already in place from vclaw stage 1/2 + OM stage 3):

`X-VClaw-User-Id` is accepted only alongside `X-VClaw-User-Assertion`, an
HMAC-SHA256 assertion minted by vclaw with the dedicated
`OPENMONTAGE_VCLAW_ASSERTION_SECRET`. Its canonical payload binds version,
user id, timestamp, nonce, HTTP method/path, session id, and SHA-256 of the
request body. OpenMontage rejects missing, stale, malformed, invalid, or
replayed assertions. The secret must be identical in both services; missing
configuration fails closed for user-attributed proxy requests.

| Header | Set when | Value |
|---|---|---|
| `X-VClaw-User-Id` | user principal | `<users.id>` |
| (future) `X-VClaw-Principal-Kind` | both | `user \| service` |
| (future) `X-VClaw-Service-Id` | service principal | `<stable service id>` |

For now only the user header exists. Service principal is required before raw `/mcp` can be considered a safe production boundary; it is not an optional v2.1 follow-up.

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

1. Phase A close-out — SSE fail-closed, deployment endpoint/artifact, raw service-only, real E2E
2. Phase B close-out — trusted proxy/service identity/TTL
3. Phase C continue — remove tool-supplied principal and migrate every audited touchpoint
4. Phase D repair — v1+v2 reconciliation, key-version rotation, migration/rollback drill

Rollback is not yet independent: once v2 paths contain data, an older build cannot discover them without a compatible resolver or reverse migration.

---

## 2026-09-02 code/document alignment review (historical snapshot; superseded below)

Verdict: **do not accept the claim that code and documents are fully aligned.** Four findings from the previous review are now closed, one is source-complete but not deployment-complete, and one is closed only for JSON responses.

### Previous findings

1. **A1 closed** — legacy refresh-token scopes are upgraded and persisted; the JWT and response expose the actual upgraded scopes.
2. **A2 closed for the personal-tenant MVP** — refresh re-resolves a non-empty tenant. A future multi-tenant selection still needs tenant affinity in the refresh session.
3. **A3 source/local configuration complete, deployment artifact not accepted** — Studio source, `.env.example`, and the ignored local `.env` use `/api/mcp/proxy`; the existing `dist` still embeds the raw OM URL and must be rebuilt and checked before deployment.
4. **A4 partial** — bufferable JSON initialize responses fail closed. The SSE branch calls the bind callback before `c.Status`/body streaming but discards its error, so the claim that the response is already committed is incorrect. It can and should fail closed at that point. OM currently uses `json_response=True`, which reduces exposure but does not close the transport contract.
5. **Immutable registry owner closed** — conflicting rebinds are rejected atomically and security-logged; same-owner renewal remains idempotent.
6. **Upload → Remotion root mismatch closed** — the render entry now resolves the current principal's `ProjectWorkspace`.

### Blocking findings and documentation correction

1. **A tool-supplied identity can select another user's namespace.** `claude_video.compose` is auto-discovered and reachable through generic `execute_tool`, but it builds a `Principal` from `inputs["user_openid"]` instead of the authenticated session principal. HMAC obscures the directory name; it does not authenticate the supplied identity. The tool must derive the owner from `current_principal()` and either remove `user_openid` or require an exact equality check.
2. **The advertised legacy read fallback is not wired into production readers.** `NamespaceLayout.candidates` and `existing_root()` exist, but production callers use `ProjectWorkspace.root` (the preferred candidate) and do not call `existing_root()`. In default legacy mode the preferred path is v2, so existing v1 directories are not actually read.
3. **Migration cannot reconcile v1 and v2 data created during rollout.** Default legacy mode already sends new writes to v2, while the migration script skips a principal whenever the v2 target exists. That leaves split histories with no safe merge/reconcile path. The script also treats every 32-lowercase-hex raw identifier as already migrated, which is an unsafe heuristic without an authoritative mapping/audit record.
4. **Upgrading an existing registry database fails.** The schema now references a `key_version` column, but `_ensure_schema()` only runs `CREATE TABLE IF NOT EXISTS`; it does not migrate an older `principal_bindings` table. A reproduced bind against the old schema raises `sqlite3.OperationalError: table principal_bindings has no column named key_version`.
5. **Secret rotation still invalidates old bindings.** Rows store `namespace_key` and `key_version`, but `lookup()` re-derives the key using the current process secret and returns `None` on mismatch. After restarting with a new secret, old rows cannot resolve. A version integer without retained old-key material/resolution is not a rotation implementation.
6. **Phase C is still a subset migration.** `docs/audit-projects-touchpoints.md` identifies direct/shared paths in tweak server, Backlot, checkpoints/events, job indexes, render recovery, publishing, and cleanup. The current patch covers upload/read/one Remotion path, not the full list.
7. **Documentation drift was corrected in this review.** Before review this file still described B as ContextVar-only, C/D as pending, and service principal as v2.1 despite the new registry/workspace/migration code. The status and phase table above now reflect the actual partial implementation.
8. **The newly added tests do not support an “already verified” claim.** Running the new namespace/migration tests together with the related business suite yields **52 failed, 174 passed**. `test_namespace_version.py` reloads the Enum module and poisons already-imported `ProjectWorkspace` class identity, producing widespread `got NamespaceVersion` failures; run alone it is entirely skipped (`10 skipped`) because of the integration autouse fixture. The migration-script file independently remains **2 failed, 5 passed** because its summary/status assertions disagree with the script.

Local verification: vclaw handler/auth/store tests and Studio `vue-tsc --noEmit` passed. The earlier OM subset passed **209 tests** before the new namespace/migration files were included; the expanded current suite is **52 failed, 174 passed**, and migration scripts alone are **2 failed, 5 passed**. An additional old-schema reproduction failed at `bind()` with `no column named key_version`. Missing acceptance coverage includes old-DB upgrade, forged tool principal, secret rotation, simultaneous v1+v2 migration, real legacy reads, and full initialize → tool execution E2E.

This section records the pre-fix result. Findings 1–5 and 8 were closed by the next implementation round; finding 6 (full touchpoint coverage) remains open. Use the current verdict below.

---

## 2026-09-02 implementation re-review (current verdict)

Verdict: **the directly actionable defects from the previous review are fixed and covered by combined tests, but the complete v2 design is not yet production-accepted.**

Closed in this round:

1. User attribution now requires a short-lived HMAC assertion minted by vclaw with the dedicated `OPENMONTAGE_VCLAW_ASSERTION_SECRET`. It binds user, timestamp, nonce, method, actual upstream path, session id and body hash. Missing keys, duplicate identity headers, stale/malformed/tampered assertions and in-process replay fail closed. A bearer plus a known registered user session is not sufficient without a fresh assertion.
2. `current_principal()` is registry-first for an existing session; only a verified, sessionless `initialize` request may use the request ContextVar. Session headers are decoded and validated canonically.
3. The registry idempotently upgrades an old table with `key_version`, keeps the stored namespace on same-owner renewal, and resolves previous secret/key versions after rotation. DB parent creation and concurrent schema setup have bounded retry.
4. `claude_video` derives its owner from the authenticated session. The compatibility `user_openid` field must exactly equal that owner. `project_id`, `video_id`, and all derived targets pass component validation and containment checks.
5. Principal dot segments, separators, and whitespace differences are rejected. Symlinked principal/project roots are rejected, and legacy reads are constrained to v1/v2 roots belonging to the same authenticated principal.
6. Custom `OPENMONTAGE_PROJECTS_DIR` upload/read/renderer resolution and the default session-backup source are consistent. Legacy global chunk state is migrated only after its session/project ownership is checked; test locks no longer leak into the real state directory.
7. Migration reconciles simultaneous v1/v2 project trees and requires an explicit policy for ambiguous 32-hex raw ids. The audit records exact moved entries, so rollback reverses only this migration instead of moving pre-existing/post-migration v2 data. Audit paths are contained, non-object lines are counted and skipped safely, and an empty legacy principal is a no-op.
8. Namespace tests no longer reload and poison Enum identities or get skipped by the voicebox fixture. JSON/YAML used by the Claude Video/checkpoint path is explicitly UTF-8 on Windows.

Local verification: the combined auth, registry, workspace, namespace, migration, upload/read, concurrency-lock, Claude Video, renderer and Remotion suite reports **337 passed**; the corresponding vclaw handler/auth/store suite passes; both repositories pass `git diff --check`.

Open acceptance items: an explicit raw/service principal, binding TTL enforcement, a shared replay store for multi-process/multi-instance OM, all remaining paths in `docs/audit-projects-touchpoints.md` (Backlot, checkpoint/event, job index, render recovery, publish/cleanup), a real WeChat QR → JWT → initialize → tool deployment E2E, and a production migration/rollback drill. Current state remains **A/B/C/D partial; the critical user path is hardened, while full end-to-end v2 acceptance remains open.**

---

## Cross-references

- vclaw repo: `docs/user-isolation-via-mcp-session.md`
- vclaw stage 1 commit: `f3b775d`
- vclaw stage 2 commit: `d7b70bb`
- OM stage 3 commit: `242940e`
