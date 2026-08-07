# Video Delivery Strategy Skill

## When to Use

Apply this skill when the rendered video(s) from a production need to be delivered
to a client or end-user over the network. This covers:

- Uploading finished videos to an S3-compatible object store (Aliyun OSS, Tencent
  COS, MinIO, Cloudflare R2, Backblaze B2, etc.)
- Generating download links for clients (public permanent links or time-limited
  pre-signed URLs)
- Providing a branded HTML download page when multiple assets or assets with
  metadata need to be presented together

**Why this skill exists:** Clients should not receive local filesystem paths —
they need URLs they can open in a browser or a download manager. The skill
decides between three delivery modes and emits a schema-valid `publish_log` so
downstream systems (dashboards, email, Slack) can reference the final URLs
without parsing HTML or guessing.

## Tools

| Tool | Role |
|------|------|
| `s3_upload` | Upload video(s) to S3-compatible storage, generate public/pre-signed URLs, optionally build a download page |
| `export_bundle` | Package local renders into a distributable archive (when network delivery is not needed or storage is unavailable) |

## Delivery Strategy Decision Tree

```
How does the client need to access the video?
│
├── "I need a direct link I can paste into an email / chat / LMS"
│   └── USE: public URL (visibility=public)
│       → S3_PUBLIC_BASE_URL must be set (your CDN/public bucket domain)
│       → Tool returns: { url: "https://cdn.example.com/videos/..." }
│
├── "I need a link that expires after N days / hours"
│   └── USE: pre-signed URL (visibility=private)
│       → Default expire_seconds=604800 (7 days)
│       → Tool returns: { url: "https://bucket.endpoint/...?X-Amz-..." }
│       → Client can open in browser immediately; link dies after expiry
│
└── "I have multiple videos / I want a branded download portal"
    └── USE: download page (make_download_page=true)
        → Tool uploads all files, builds a single-page HTML delivery portal,
          returns both the page URL and per-file URLs
        → Great for: campaign deliverables, client review packages,
          multi-language cuts, vertical + horizontal variants
```

**Mode selection rules:**
- Always prefer `public` over `private` when the client is external and you
  control the CDN — pre-signed URLs add friction (expired links, no deep-link
  sharing).
- Use `private` + `expire_seconds` when delivering sensitive/restricted content
  (embargoed assets, NDA-protected material, internal review cuts).
- Always set `make_download_page=true` when `additional_files` is non-empty or
  when the client needs metadata alongside the download (project name, duration,
  resolution).

## Prerequisites

### Environment Variables

All S3-compatible storage uses the same AWS SigV4 signing protocol. Set these
in `.env` (or your deployment config):

| Variable | Required | Example | Description |
|----------|----------|---------|-------------|
| `S3_ENDPOINT_URL` | Yes | `https://s3.aliyuncs.com` | Full endpoint (include scheme) |
| `S3_ACCESS_KEY` | Yes | `LTAI5t...` | IAM / STS access key ID |
| `S3_SECRET_KEY` | Yes | `xxxx...` | IAM / STS secret access key |
| `S3_BUCKET` | Yes | `my-bucket` | Target bucket name |
| `S3_REGION` | Optional | `cn-hangzhou` | Default `us-east-1` if omitted |
| `S3_DEFAULT_VISIBILITY` | Optional | `public` or `private` | Override default per-upload visibility |
| `S3_PUBLIC_BASE_URL` | Conditional* | `https://cdn.example.com` | Required when `visibility=public`; used as the CDN origin so returned URLs are publicly reachable |

*\*When `visibility=public` and `S3_PUBLIC_BASE_URL` is missing, the tool falls
back to `<endpoint>/<bucket>/<key>` — this only works when the endpoint itself
is publicly reachable (e.g. a hosted S3 bucket). For a Docker-local MinIO test
setup, the fallback URL is not usable by external clients.*

### Bucket Requirements

- **Public uploads:** Bucket must allow public reads (bucket policy / CORS).
  Without this, a `visibility=public` upload will return a 403 when the client
  opens the link.
- **Private uploads:** Bucket should deny public reads; the pre-signed URL
  bypasses the bucket policy for the duration of the signature.
- **Cross-origin:** If the download page will be served from a different origin
  than the storage endpoint, ensure the bucket has permissive CORS headers
  (most S3-compatible stores support this in bucket policy).

## Process

### 1. Prepare the Render Output

Confirm the rendered video exists and is well-formed before uploading:

```bash
ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,r_frame_rate,codec_name \
  -of csv=p=0 /opt/OpenMontage/demo/out/luggage_promo.mp4
```

Expected: one line like `1920,1080,30/1,h264`. If `ffprobe` fails or reports
multiple video streams, fix the render before proceeding — the upload tool
does not re-encode.

### 2. Decide Delivery Mode

Use the decision tree above. For a single final cut sent to one client, pick
either `public` (easiest) or `private` (most secure). For a package with
multiple variants (e.g. portrait + landscape), pick `download_page`.

### 3. Run the Upload

```python
from tools.uploads.s3_upload import S3Upload

t = S3Upload()
result = t.execute({
    "video_path": "/opt/OpenMontage/demo/out/luggage_promo.mp4",
    "project_id": "luggage_promo",
    "visibility": "public",              # or "private"
    "expire_seconds": 604800,           # only matters for private
    "make_download_page": False,         # True for multi-file packages
    "page_title": "Luggage Promo — Download",
})

print(result.data["url"])              # the download link
print(result.data["publish_log"])      # schema-valid delivery record
```

For multi-file delivery:

```python
result = t.execute({
    "video_path": "/opt/OpenMontage/demo/out/luggage_promo.mp4",
    "additional_files": [
        "/opt/OpenMontage/demo/out/luggage_promo_landscape.mp4",
    ],
    "project_id": "luggage_promo",
    "visibility": "public",
    "make_download_page": True,
    "page_title": "Luggage Promo — All Cuts",
})
# result.data["download_page_url"] points to the HTML portal
# result.data["uploaded_files"] lists every file with its URL
```

### 4. Verify the Output

Check the tool's return value:
- `result.status == "success"` — upload completed
- `result.data.url` — the direct download link
- `result.data.object_key` — the storage path (keep this for audit)
- `result.data.publish_log` — the structured record for downstream systems
- `result.data.download_page_url` — only present when `make_download_page=true`

### 5. Communicate to the Client

- **Public URL:** Paste into email / Slack / ticket. No expiry concern.
- **Pre-signed URL:** Send immediately; note the expiry in your message.
  Re-upload if the client hasn't downloaded within the window.
- **Download page:** Share the page URL. The page is self-contained (single HTML
  file with inline CSS) — no hosting required beyond the S3 bucket.

## Quality Checklist

Before declaring delivery complete, verify every item:

- [ ] **Upload succeeded:** `result.status == "success"` and no errors in logs
- [ ] **URL is reachable:** open `result.data.url` in a browser (or `curl -I`)
- [ ] **Video plays:** the link returns the full file and a media player can
  seek through it without stall
- [ ] **Object key is sensible:** `videos/<project_id>/<stem>` or similar; not
  leaked to a root-level bucket path
- [ ] **Visibility matches intent:** `result.data.visibility` equals what was
  requested (`public` or `private`)
- [ ] **Expiry is correct (private mode):** `result.data.expires_at` is in the
  future and matches `now + expire_seconds`
- [ ] **Download page works (if used):** open the page URL, confirm all files
  are listed with working links
- [ ] **Publish log is schema-valid:** the `publish_log` entry contains only the
  fields defined in `schemas/artifacts/publish_log.schema.json`
- [ ] **No credentials leaked:** the tool's `_safe_error` path has not exposed
  `S3_ACCESS_KEY` or `S3_SECRET_KEY` in logs or output

## Common Pitfalls

### Public Link Returns 403 on a Private Bucket

**Problem:** The tool returned a URL, but opening it in a browser gives 403
Forbidden.

**Cause:** `visibility=public` was set but the bucket policy does not allow
anonymous reads. S3Upload sets the object's ACL correctly, but the bucket-level
policy is the final arbiter.

**Solution:** Add a bucket policy allowing `s3:GetObject` for `*` on the target
prefix, or switch to `visibility=private` with a pre-signed URL. For R2 /
Cloudflare, disable "Block public access" in the dashboard.

### Pre-Signed URL Expires Before Client Opens It

**Problem:** Client reports the link is dead a few hours after receipt.

**Cause:** Default `expire_seconds=604800` (7 days) was overridden to a shorter
value, or the client's system clock is skewed.

**Solution:** Increase `expire_seconds` when calling `execute()`. For embargoed
or time-sensitive deliveries, communicate the expiry explicitly to the client.

### `S3_PUBLIC_BASE_URL` Is Missing for Public Mode

**Problem:** The returned URL points at the S3 endpoint directly
(e.g. `https://s3.aliyuncs.com/bucket/key`) instead of a branded CDN domain.
This works technically but looks unprofessional and bypasses your CDN cache.

**Solution:** Set `S3_PUBLIC_BASE_URL` in `.env` to your CDN origin (e.g.
`https://assets.example.com`). The tool will use `<base>/<key>?<query>` for
public URLs.

### Download Page Shows Expired Links (Private Mode)

**Problem:** The HTML portal lists pre-signed URLs; half the client's links are
dead because they were generated days apart with short expiry.

**Solution:** For download pages that clients will visit over multiple days, use
`visibility=public` for every file on the page. Pre-signed URLs are fine for
single-use direct links but create a poor experience when re-opened.

### Object Key Collision Overwrites Previous Deliverables

**Problem:** Re-running the upload for the same `project_id` overwrites the
previous video in the bucket.

**Solution:** Pass a unique `object_key` (e.g. include a timestamp or UUID), or
rely on the tool's default key derivation which appends the file stem. For
iterative client review, append a version suffix: `videos/<project_id>/<stem>_v2.mp4`.

## Self-Evaluation

After completing a delivery, score the outcome (0-5 per dimension). A score of
3+ across all dimensions indicates a clean handoff; below 3 means re-investigate
the checklist above.

| Dimension | Question | Score (0-5) |
|-----------|----------|-------------|
| **Reachability** | Can the client open the link on their device without special setup? | |
| **Security** | Does the visibility setting match the sensitivity of the content? | |
| **Clarity** | Did the client receive a single unambiguous URL (or a clean portal)? | |
| **Auditability** | Is the `publish_log` record complete and schema-valid? | |
| **Reproducibility** | Can the same upload be re-run deterministically (same key, same content)? | |
