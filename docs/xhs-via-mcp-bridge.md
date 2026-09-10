# Importing xiaohongshu videos into OpenMontage

> How to land xhs video notes inside an OpenMontage project so the
> voicebox / video_compose / remix pipeline can pick them up.

## TL;DR

**No MCP integration required.** A standalone bridge script in
MediaCrawler writes xhs videos directly into OpenMontage's per-user
workspace. OpenMontage's own tools find them on the next discovery
pass.

The bridge:

```
/opt/MediaCrawler/tools/cdp_xhs_to_openmontage_bridge.py
```

Docs:

```
/opt/MediaCrawler/docs/xhs_to_openmontage_bridge.md
```

## Why no MCP?

Three reasons:

1. **xhs video fetching requires a remote Chrome CDP session.** OpenMontage
   doesn't have one — the bridge lives on the MediaCrawler host that does.
   Wrapping it in MCP would just be a subprocess shell that defers
   fetching to the same place the bridge already runs.

2. **Cross-filesystem writes need direct disk access.** The bridge writes
   to `projects/users/<namespace_key>/references/`. It needs
   `compute_namespace_key()` (HMAC of `OPENMONTAGE_PRINCIPAL_HASH_SECRET`),
   which is the same derivation OpenMontage's `Principal` dataclass uses
   — re-implemented in the bridge so it can land files without booting
   the full OpenMontage runtime.

3. **The output is already in OpenMontage's native schema.**
   `_meta.json` matches `video_downloader`'s output exactly
   (`video_path`, `audio_path`, `metadata.title`, `metadata.duration`,
   `platform`, `workspace_root`). Once it's on disk, OpenMontage picks
   it up the same way it picks up its own downloads.

## When MCP *would* make sense

Add an MCP wrapper if you want an OpenMontage agent to autonomously
trigger xhs fetching without leaving the agent context. Sketch (NOT
implemented — add only if you need agent-side triggering):

```python
# A hypothetical future tool — do NOT implement unless needed
@mcp.tool()
async def cdp_xhs_bridge_download(
    note_url: str, userid: str, project_id: str = "references",
) -> dict[str, Any]:
    """Delegate to MediaCrawler's bridge script via subprocess."""
    proc = await asyncio.create_subprocess_exec(
        "python", "/opt/MediaCrawler/tools/cdp_xhs_to_openmontage_bridge.py",
        "--userid", userid,
        "--keyword", "PLACEHOLDER",  # single-URL mode would need a new flag
        "--project-id", project_id,
        "--max-videos", "1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    ...
```

This is straightforward to add — about 30 lines — but unnecessary for
the current "kick off the bridge manually, OpenMontage picks up the
files" flow.

## The two paths into OpenMontage, side by side

| | OpenMontage `video_downloader` (yt-dlp) | MediaCrawler bridge |
|---|---|---|
| Sites | 1000+ (YouTube / Bilibili / Weibo / IG / TikTok / …) | xhs only |
| Auth model | None (public URLs) | Required (remote logged-in Chrome) |
| Output | `projects/users/<ns>/references/reference_video_<urlhash>.<ext>` | same |
| Metadata | yt-dlp native (title/uploader/upload_date/view/like) | xhs via `__INITIAL_STATE__` + bridge fallback (title/author/liked/duration via ffprobe) |
| When to use | Default for everything except xhs | When yt-dlp has no extractor |

## What the bridge writes

`projects/users/<namespace_key>/<project_id>/reference_video_<urlhash>.<ext>`
plus a sibling `<basename>_meta.json` matching `video_downloader`'s
schema, plus a `_bridge.*` provenance block:

```json
{
  "video_path": "projects/users/<ns>/references/reference_video_<hash>.mp4",
  "audio_path": null,
  "subtitle_path": null,
  "metadata": {
    "title": "...",
    "duration": 79.0,
    "uploader": "...",
    "upload_date": "",
    "description": "xhs note <note_id> (liked: 80)",
    "like_count": 80,
    "resolution": "1280x720"
  },
  "platform": "xiaohongshu",
  "workspace_root": "projects/users/<ns>/references",
  "_bridge": {
    "source_url": "https://www.xiaohongshu.com/explore/<id>?xsec_token=…",
    "video_cdn_url": "http://sns-video-bd.xhscdn.com/…",
    "note_id": "...",
    "xsec_token": "...",
    "xsec_source": "pc_search",
    "downloaded_via": "cdp_page_driver"
  }
}
```

`_bridge.*` keys are reserved for the bridge — OpenMontage core
ignores them, but downstream agents can read them to re-resolve the
original xhs source (e.g. for re-fetch if the local mp4 is lost).

## Operational steps

1. **Run the bridge** on the MediaCrawler host (must have a remote
   Chrome with an xhs tab open and the user logged in):

   ```bash
   cd /opt/MediaCrawler
   PYTHONPATH=. .venv/bin/python tools/cdp_xhs_to_openmontage_bridge.py \
       --userid webheat \
       --keyword "箱包推荐" \
       --max-videos 3
   ```

2. **Verify on OpenMontage side**:

   ```bash
   ls projects/users/<namespace_key>/references/
   # reference_video_<hash>.mp4
   # reference_video_<hash>_meta.json
   ```

3. **Consume** with the normal OpenMontage tools:

   ```python
   # MCP or programmatic — these find bridge output the same way they
   # find yt-dlp output.
   await scene_detect(project_id="references", …)
   await transcriber(input_path="…/reference_video_<hash>.mp4", …)
   await voicebox_clone_voice(input_path="…/reference_video_<hash>.mp4", …)
   await create_reference_remix_video_share(edit_decisions=…, asset_manifest=…, …)
   ```

## Compatibility notes

* **HMAC secret**: the bridge defaults to
  `openmontage-principal-namespace-v2` (OpenMontage's fallback when
  `OPENMONTAGE_PRINCIPAL_HASH_SECRET` is unset). If your deployment
  sets a non-default secret, the bridge picks it up via
  `--hmac-secret` or the `OPENMONTAGE_PRINCIPAL_HASH_SECRET` env var.
  Mismatched secret = the bridge lands files in a different user's
  namespace; always test with one note first.

* **Workspace root**: defaults to `/opt/OpenMontage_Voicebox`. Override
  with `--om-root` if your install lives elsewhere.

* **Hardlinks vs copies**: the bridge tries `os.link()` first (free,
  same filesystem). If OpenMontage and MediaCrawler live on different
  filesystems, it falls back to `shutil.copy2()` (slower, doubles disk
  usage). Verify the path before assuming one or the other.
