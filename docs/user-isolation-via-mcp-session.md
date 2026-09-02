# User Isolation via MCP Session — OM-side review revision

> Date: 2026-09-02
> Status: Plan v2, **under review; do not implement the old Phase 3/4 design**
> Depends on: vclaw authenticated MCP bootstrap and immutable session ownership
> Goal: Isolate uploads, session state, projects, jobs, renders, and reads by authenticated principal from the first write.

---

## Executive verdict

The original direction is sound:

- WeChat Open Platform authenticates the person.
- vclaw maps the WeChat identity to its internal `users.id` and issues a vclaw JWT.
- vclaw is the identity authority; OM does not call WeChat or vclaw to resolve a user.
- OM uses an authenticated principal to select a physical storage namespace.

The v1 implementation plan is not safe to start. Its Phase 3 `ContextVar` design does not cross FastMCP's stateful transport boundary, and its Phase 4 file list is far smaller than the real storage surface. The plan also assumed that a bound MCP session proves the current caller's identity; it does not when requests enter through an unauthenticated raw proxy.

The vclaw Phase 1 header-forwarding change may remain as preparatory work. It proves only that a header can reach OM; it does not establish user isolation.

---

## Authentication contract

### User principal

The supported user flow is:

1. User completes WeChat Open Platform QR authentication.
2. vclaw validates the OAuth transaction and maps `openid/unionid` to internal `users.id`.
3. Claw Studio exchanges the approved device login for a short-lived vclaw JWT and an opaque refresh token.
4. The JWT contains verified `uid`, `tid`, `did`, and scopes including `mcp:use`.
5. Every desktop MCP request enters vclaw's JWT-protected `/api/mcp/proxy`.
6. vclaw validates the JWT and session owner, strips client-supplied internal identity headers, then emits trusted OM headers.

WeChat access tokens, openids, and unionids must never be OM storage identities. OM receives only the vclaw internal principal.

### Service principal

Service calls are a separate identity class:

- They authenticate to vclaw with an explicit service credential.
- vclaw emits `principal_kind=service` plus a stable service id.
- OM writes service data under `projects/services/<service_namespace_key>/...`.
- An unauthenticated raw caller must never receive vclaw's shared upstream `MCP_API_TOKEN` as an automatic fallback.

### Suggested internal headers

- `X-VClaw-Principal-Kind: user | service`
- `X-VClaw-User-Id: <users.id>` for users
- `X-VClaw-Tenant-Id: <tenants.id>` when tenant policy is required
- `X-VClaw-Service-Id: <stable service id>` for services

OM may trust these headers only when the request is authenticated as coming from vclaw. A valid shared token alone does not prove that an arbitrary user id is legitimate if other clients know that token or can reach OM directly.

OM currently binds FastMCP to all interfaces (`host="::"`). Production must bind the internal MCP listener to loopback/private infrastructure, enforce a firewall or mTLS-equivalent boundary, and expose only vclaw's authenticated entry point to desktop users.

---

## MCP session bootstrap

The old “raw first, bind later with a warm-up call” sequence is rejected.

Required sequence:

1. Claw Studio sends `initialize` through `/api/mcp/proxy` with a valid vclaw JWT. No `Mcp-Session-Id` exists yet.
2. OM returns a new `Mcp-Session-Id`.
3. vclaw atomically binds that response session id to the JWT `uid` before returning it to the client.
4. Every later request validates JWT and verifies that the supplied session belongs to the same `uid`.
5. vclaw sends the trusted principal headers on every forwarded request.
6. OM immutably binds the session to that principal. A conflicting rebind is a security error.

An MCP session id is an affinity/correlation identifier, not a replacement for user authentication.

---

## Critical FastMCP propagation constraint

Do not implement user identity as only a `ContextVar` set by `BearerTokenAuthMiddleware`.

The current source already documents the failure mode: stateful Streamable HTTP executes tools inside a per-session background task, not the per-request ASGI task. A ContextVar created in middleware does not naturally reach the tool. The existing MCP session id works because `mcp_server.py` patches `StreamableHTTPServerTransport.connect()` and sets the session ContextVar inside the background task.

An ASGI test that observes `current_user_id()` inside the downstream ASGI app is insufficient. It can pass while real MCP tools still see no user.

### Revised principal propagation

Use the reliable session id as the lookup key:

1. The authenticated OM request layer validates and sanitizes vclaw's principal headers.
2. When the request carries `Mcp-Session-Id`, bind `session_id → principal` in an immutable principal registry.
3. At tool execution, call the already-working `get_mcp_session_id()` and resolve the principal from that registry.
4. Reject missing principals for user-required tools and reject all owner conflicts.
5. Copy immutable `principal_kind` and `namespace_key` into every durable background job record.

A process-local dict is insufficient for multi-worker deployments and restart recovery. Use SQLite, locked durable state, or another process-shared store. Bindings must have lifecycle/expiry cleanup and must never log raw session ids.

---

## Input and path safety

The v1 validation `[a-zA-Z0-9\-_.]{1,128}` is not safe for a path segment because `.` and `..` pass it.

Preferred approach:

```text
user_namespace_key = stable encoded HMAC(storage_namespace_secret, users.id)
service_namespace_key = stable encoded HMAC(storage_namespace_secret, service_id)
```

Recommended layout:

```text
projects/
  users/<user_namespace_key>/<project_id>/...
  services/<service_namespace_key>/<project_id>/...
  _system/identity_sessions/...
```

If raw ids are ever used as path segments:

- require an alphanumeric first character;
- allow only the minimum character set, preferably `[A-Za-z0-9_-]`;
- reject `.`, `..`, reserved system directory names, separators, whitespace, and overlong values;
- resolve the final path and prove it remains below the selected namespace root.

MCP arguments such as `caller_id` or `user_id` must never override the authenticated principal. For compatibility they may only be accepted when equal to the verified principal; otherwise reject the call.

---

## Storage resolver requirement

Do not scatter `projects / user_id / ...` string changes across tools. Introduce one resolver used by all readers and writers, for example:

```text
Principal(kind, user_id, tenant_id, service_id, namespace_key)
ProjectWorkspace(principal, project_id)
  .root
  .assets
  .artifacts
  .renders
  .checkpoints
  .session_state
  .upload_state
```

Authorization uses original `uid/tid`; `namespace_key` is only a filesystem key.

If tenant collaboration is required, select and document the policy before implementation. A possible structure is `projects/tenants/<tenant_key>/users/<user_key>/<project_id>`, but user-private and tenant-shared assets must remain explicit rather than inferred from path shape.

---

## Mandatory full audit

The v1 file list was illustrative, not implementation-complete. At minimum audit and migrate:

- `lib/workbuddy_session.py`
  - session state paths;
  - cross-process locks;
  - global job index;
  - render status lookup and orphan recovery.
- `tools/asset_upload_chunk.py`
  - global `projects/.uploads` state and `.part` files;
  - upload-id/session ownership;
  - final asset paths.
- `tools/asset_upload.py` and every asset reader.
- `mcp_server.py`
  - upload tools;
  - `read_session_asset`;
  - `create_remotion_video_share`;
  - status/SSE/share paths;
  - render dispatch and durable background records.
- Render queues, job registries, startup re-dispatch, retry and cancellation.
- Pipeline workspace initialization, checkpoints, artifacts, renders, publish logs.
- Backlot project discovery/open/history behavior.
- Web/BFF project, thumbnail, and download APIs.
- Cleanup, expiry, migration, backup, and rollback logic.
- Every tool that directly joins `projects/` or accepts an output path.

`read_session_asset` must independently verify that the requested resolved path is inside the current principal's namespace. BFF-side whitelisting alone is not a security boundary.

The repository currently has many Python files referencing `projects/`; Phase 4 is a cross-cutting workspace migration, not an approximately 150-line tool patch.

---

## Compatibility policy

The old documents contradicted each other about service-token behavior. This revision defines it explicitly:

| Principal | User header | Required namespace | User-only tools |
|---|---:|---|---|
| Authenticated user | yes | `projects/users/<key>/...` | allowed |
| Authenticated service | no user id; stable service id required | `projects/services/<key>/...` | denied unless explicitly supported |
| Missing/unknown principal | no | none | denied before filesystem access |

No implicit fallback to `projects/<project_id>` is allowed once strict isolation is enabled.

---

## Migration and rollback

The path change is not automatically additive. Existing Backlot, checkpoint, reader, and job-recovery code will not discover nested workspaces without resolver support.

Required migration controls:

- Feature flag such as `USER_NAMESPACE_V2`.
- Namespace version fixed when a session/job is created.
- New sessions write only v2 paths.
- Legacy sessions may use an owner-verified legacy resolver during a bounded migration window.
- Prefer legacy-read/new-write behavior; never write the same job to both layouts.
- Durable jobs store namespace version and key so restart recovery is deterministic.
- Inventory and back up legacy session metadata before cleanup; do not simply delete `projects/.mcp_sessions`.
- A rollback build must understand existing v2 paths. Otherwise rollback requires a data migration and is not a one-commit revert.

---

## Revised delivery phases

| Phase | Scope | Independent release? | Exit criteria |
|---|---|---:|---|
| A | vclaw/Studio JWT scopes, tenant-preserving refresh, authenticated endpoint, initialize-response session bind | No; client + control plane coordinated | Full QR login can initialize MCP; every later call validates JWT and owner |
| B | OM trusted-proxy boundary and durable immutable session→principal registry | No; integrate with A | Real MCP tool resolves principal across transport, worker, and restart boundaries |
| C | Central storage resolver, user/service namespaces, complete read/write/job/Backlot audit behind flag | No; integrate with B | Two users with same project id cannot cross read/write/status paths |
| D | Legacy migration, strict enforcement, removal of unsafe compatibility paths | Yes, after A–C | Production canary, audit review, and rollback drill pass |

Do not describe Phase 1 header injection as functional isolation. Do not start the old middleware-only Phase 3.

---

## Required tests

### Authentication and bootstrap

- Real WeChat QR callback → approved device login → JWT exchange.
- JWT contains correct `uid/tid/did/mcp:use`.
- Refresh preserves `uid/tid/scopes`.
- Authenticated `initialize` without a session succeeds and response session is bound atomically.
- User A presenting user B's session is rejected before OM forwarding.
- Missing, expired, forged, wrong-audience JWT is rejected.
- Raw MCP without a service credential is rejected and does not contact OM.

### OM transport and storage

- Full Streamable HTTP lifecycle: initialize → session-bearing request → real tool execution.
- Tool background task resolves the principal; an ASGI-only assertion does not count.
- Two users upload the same filename under the same project id; every physical path is disjoint.
- Conflicting session rebind is rejected and security-logged.
- Forged identity header and direct OM access are rejected.
- `.`, `..`, reserved names, separators, and overlong ids are rejected.
- Upload temp state, session state, locks, job index, render output, status, SSE, read and cleanup remain isolated.
- Service principal uses only its service namespace.
- Multi-thread, multi-process, restart, retry, cancellation and job re-dispatch retain the correct principal.
- Legacy-to-v2 migration and rollback drill pass.

---

## Final decision

WeChat Open Platform authentication and JWT-based MCP authorization are compatible and already mostly present in vclaw. OM should not validate WeChat or desktop JWTs itself. vclaw must authenticate every user request, bind the OM session to the JWT user during initialization, and emit a trusted internal principal. OM must resolve that principal through a durable session registry at tool execution and route all I/O through one namespace-aware workspace resolver.

Until those transport and storage changes pass end-to-end tests, a forwarded header or middleware-visible ContextVar is not evidence of user isolation.
