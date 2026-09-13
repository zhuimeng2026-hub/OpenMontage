# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run

```bash
go build -o wechat-auth-pay .    # build
go run .                          # run (needs .env with required env vars)
go test ./...                     # run all tests (currently none)
./dev.sh                          # tmux dev session (auto-starts server + logs + shell)
```

No Makefile or Dockerfile exists — it's plain Go with systemd supervision. See `systemd.md` for service management commands.

`dev.sh` creates a single-pane detached tmux session running `go run .` with timestamped output tee'd to `dev.log`. Accepts optional session name arg.

## Architecture

This is a **multi-tenant WeChat OAuth + Payment gateway** — a thin middleman that front-end apps call to authenticate users via WeChat and create/pay orders with WeChat Pay V3.

### Layers

```
main.go          — wiring: loads config, initializes store/services/handlers, registers routes
config.go        — env parsing: builds Provider + App maps from WX_*, WXWORK_*, WXOPEN_*, APP_* env vars
handler/         — HTTP handlers (gin), thin — delegate to service/store
service/         — business logic: JWT signing (jwt.go), OAuth URL building & code exchange (oauth_wx.go, oauth_wxwork.go, wechat.go), WeChat Pay V3 (payment.go: JSAPI + Native + notify decrypt)
store/           — SQLite data access: user/identity/order/plan CRUD, schema migration
model/           — shared structs (User, UserIdentity, Order, Plan, request types)
```

### Route table

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/health` | none | Health check |
| GET | `/auth/:appId` | none | Initiate OAuth (302 to WeChat) |
| GET | `/callback` | none | OAuth callback (code→openid, issues JWT, 302 back to app) |
| POST | `/api/auth/login` | none | Silent login (code→JWT, for mini-programs/backends) |
| POST | `/api/orders` | none | Create order (openid + app_id + plan_code) |
| POST | `/api/pay/checkout` | none | Initiate WeChat JSAPI payment |
| POST | `/api/pay/notify` | none | WeChat Pay V3 async callback |
| GET | `/api/user/me` | JWT | Get current user profile |
| PUT | `/api/user/me` | JWT | Update user profile |
| GET | `/api/user/orders` | JWT | List user's paid orders |
| GET | `/api/plans` | JWT | List active plans for current app |

### Multi-tenancy model

Apps are configured via env vars. Each app has a **provider key** (maps to a WX_*, WXWORK_*, or WXOPEN_* credential) and a **redirect base URL**.

- `WX_{appId}_SECRET=...` → WeChat service account OAuth provider
- `WXWORK_{corpId}_SECRET=...` → WeChat Work OAuth provider
- `WXOPEN_{appId}_SECRET=...` → WeChat Open Platform (qrconnect) website app OAuth provider
- `APP_{name}={provider_key},{redirect_base}` → registered application

OAuth state parameters carry `app_id` + `redirect` in a JWT to survive the WeChat round-trip. All orders and plans are scoped to `app_id`.

### Data model

- **User** + **UserIdentity**: internal user linked to external provider identity (`wx_oa`, `wxwork`, or `wx_open` by openid/userid). `UNIQUE(provider, external_id)` ensures one identity per provider. Unionid stored in `meta` JSON enables cross-provider identity linking (`FindIdentityByUnionID`).
- **AppUser**: user-role binding per app.
- **Order**: created via `/api/orders`, paid after `/api/pay/notify` confirms. Supports both JSAPI (openid required) and Native (QR code) payment modes. Attach field carries `plan_code` as JSON.
- **Plan**: per-app pricing plans. `SeedDefaultPlans()` auto-creates defaults (single/pack_5/pack_10) on startup if an app has none.

### Key implementation details

- JWT is self-signed HMAC-SHA256, 24h expiry. Secret from `JWT_SECRET` env.
- WeChat Pay V3 uses RSA signing with merchant private key, AES-256-GCM for callback decryption.
- `PaymentService.missingConfig()` is called at checkout — returns an error listing missing env vars if payment isn't fully configured.
- DB auto-migrates on startup (`store.Migrate()`), creating tables with `IF NOT EXISTS`. SQLite uses WAL mode with `MaxOpenConns(1)` — single-writer, no concurrent writes.
- `WX_OAUTH_BASE_URL` and `WX_API_BASE_URL` env vars override WeChat API endpoints (for proxy setups). Defaults to official `open.weixin.qq.com` / `api.weixin.qq.com`.
