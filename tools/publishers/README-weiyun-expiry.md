# Weiyun share-link expiry (client-side retention)

## Why this exists

The official Tencent Weiyun MCP `gen_share_link` tool exposes only **four**
parameters (`file_list`, `dir_list`, `passwd`, `share_name`). It does NOT
expose any expiration / retention / `retain_days` field. Verified by querying
the MCP `tools/list` endpoint:

```json
{
  "name": "weiyun.gen_share_link",
  "inputSchema": {
    "properties": {
      "dir_list":   [...],
      "file_list":  [...],
      "passwd":     "分享密码,...",
      "share_name": "分享名称,..."
    }
  }
}
```

OpenMontage simulates retention by deleting the underlying file when a
configured window elapses — the share URL 404s because the source file is
gone.

## Components

| File | Role |
|------|------|
| `tools/publishers/weiyun_share_link.py` | Adds optional `retain_days` (1..365), `pdir_key`, `project_id` inputs. On success, appends one row to `projects/_share_expiry/index.jsonl` with `expires_at = now + retain_days`. |
| `tools/publishers/weiyun_delete.py` | New `WeiyunDelete` BaseTool wrapping `weiyun.delete` MCP. Requires `WEIYUN_MCP_TOKEN`. |
| `tools/publishers/weiyun_expiry_sweep.py` | CLI sweeper. Reads the index, finds rows where `expires_at <= now`, calls `WeiyunDelete` for the captured `(file_id, pdir_key)` pairs, marks rows `status=deleted` in place. Idempotent — running twice does not double-delete. |

## Index format

`projects/_share_expiry/index.jsonl` — one JSON row per registered share:

```json
{
  "short_url":   "https://share.weiyun.com/XXXXXXXX",
  "file_ids":    ["cca7a40777c54927b7cfe765cd61b22a"],
  "pdir_keys":   ["hex-encoded-dir-key"],
  "share_name":  "render-2026-08-20.mp4",
  "created_at":  "2026-08-20T13:35:50Z",
  "expires_at":  "2026-08-23T13:35:50Z",
  "retain_days": 3,
  "project_id":  "frameflow-default",
  "status":      "active",
  "deleted_at":  null
}
```

## Usage

### Create a share with retention

```python
from tools.publishers.weiyun_share_link import WeiyunShareLink

result = WeiyunShareLink().execute({
    "file_list":   [file_id],          # returned by weiyun_upload
    "pdir_key":    pdir_key,            # optional, required if retain_days is set
    "retain_days": 3,                   # <-- the new field
    "project_id":  "frameflow-default",
})
# result.data["short_url"], result.data["expires_at"]
```

### Run the sweeper

```bash
# Dry-run, no deletes
python -m tools.publishers.weiyun_expiry_sweep --dry-run

# Actually delete (move to Weiyun trash by default)
python -m tools.publishers.weiyun_expiry_sweep

# Irreversible: completely remove files
python -m tools.publishers.weiyun_expiry_sweep --completely

# Cap rows touched per run (defensive against huge backlogs)
python -m tools.publishers.weiyun_expiry_sweep --limit 50

# Only sweep one project
python -m tools.publishers.weiyun_expiry_sweep --project-id frameflow-default
```

Exit code:
- `0` — success (including "nothing to do")
- `1` — at least one row failed (partial sweep; subsequent runs retry)

### Scheduling

#### Option A — cron

```cron
# Every hour, sweep expired shares
0 * * * *  cd /opt/OpenMontage_Voicebox && python -m tools.publishers.weiyun_expiry_sweep >> /var/log/weiyun-sweep.log 2>&1
```

#### Option B — systemd timer (preferred)

Two ready-made unit files ship in `deploy/`:

- `deploy/weiyun-expiry-sweep.service` — `Type=oneshot`, runs the sweep CLI.
- `deploy/weiyun-expiry-sweep.timer` — fires every hour with a 60s jitter.

Install on a fresh machine:

```bash
# 1. Create the unprivileged user the service runs as
sudo useradd --system --no-create-home --shell /usr/sbin/nologin weiyun-sweeper

# 2. Drop the MCP token in a protected env file
sudo install -d -m 0750 -o weiyun-sweeper -g weiyun-sweeper /etc/weiyun-sweeper
sudo tee /etc/weiyun-sweeper/sweeper.env > /dev/null <<'EOF'
WEIYUN_MCP_TOKEN=<paste the same token from /opt/OpenMontage_Voicebox/.env>
EOF
sudo chmod 0640 /etc/weiyun-sweeper/sweeper.env
sudo chown root:weiyun-sweeper /etc/weiyun-sweeper/sweeper.env

# 3. Make sure the index dir is writable by the service user
sudo chown -R weiyun-sweeper:weiyun-sweeper /opt/OpenMontage_Voicebox/projects/_share_expiry

# 4. Install + enable
sudo install -m 0644 deploy/weiyun-expiry-sweep.service /etc/systemd/system/
sudo install -m 0644 deploy/weiyun-expiry-sweep.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now weiyun-expiry-sweep.timer

# 5. Verify
systemctl list-timers weiyun-expiry-sweep.timer
journalctl -u weiyun-expiry-sweep.service -n 50 --no-pager
```

The service uses `Type=oneshot`, so each tick runs **one** sweep process and
exits. `RandomizedDelaySec=60` spreads the load on the Weiyun MCP endpoint if
multiple machines run the same timer. `Persistent=true` means missed windows
(e.g. the host was off) catch up at next boot.

To run a one-off manual sweep under systemd:

```bash
sudo systemctl start weiyun-expiry-sweep.service
journalctl -u weiyun-expiry-sweep.service -n 30 --no-pager
```

## Caveats

- **No native protection** — a customer who has already downloaded the file
  keeps their copy. Deletion only invalidates the *share URL*, not copies
  already in the wild.
- **`pdir_key` is required for retention** — without it, the sweeper can't
  call `weiyun.delete` (the MCP requires `pdir_key` per file). If you call
  `share_link` with `retain_days` but no `pdir_key`, the tool still succeeds
  but the sweeper will log a skip warning for that row.
- **`weiyun.delete` move-to-trash** is the default (`delete_completely=false`)
  so a misconfigured sweep can be reversed from the Weiyun web UI. Pass
  `--completely` only when you're sure.
- **Time skew** — `expires_at` is in UTC. If the sweeper host clock drifts,
  rows may be processed late/early. Use NTP.

## Files added / changed

- **NEW** `tools/publishers/weiyun_delete.py`
- **NEW** `tools/publishers/weiyun_expiry_sweep.py`
- **MODIFIED** `tools/publishers/weiyun_share_link.py` (added `retain_days`,
  `pdir_key`, `project_id`; new `_append_expiry_entry` helper)
- **NEW** `deploy/weiyun-expiry-sweep.service`
- **NEW** `deploy/weiyun-expiry-sweep.timer`
- **NEW (runtime)** `projects/_share_expiry/index.jsonl` (created on first
  retained share; lives under gitignored `projects/`)