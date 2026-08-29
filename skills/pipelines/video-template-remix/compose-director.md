# Video Template Remix — Compose Director

## When to Use

Use this stage only after edit decisions and replacement assets have passed review.

## Prerequisites
Read edit decisions and asset manifest. Prefer FFmpeg/video_compose for source-faithful assembly; choose a runtime only after the normal runtime governance conversation.

## Process
Render a deterministic assembly with explicit source ranges and replacement overlays/clips. Keep original FPS, resolution, audio channels, loudness, subtitle coordinates, and transition durations where supported. Verify with ffprobe and compare source/output duration and cut timestamps; write render report and final review with deviations.

Route strictly by the approved `render_runtime`: FFmpeg/video_compose for direct source-timeline replacement, Remotion for React-driven overlays or generated motion, and HyperFrames for an approved HyperFrames composition. If the stored runtime is missing or unavailable, stop and surface the constraint; never silently switch runtimes.

## Self-Evaluate
5 means frame-accurate timing, intact approved tracks, valid encoding, and no unauthorized changes. Deduct for each drift or unverified track.

## Pitfalls
Do not stretch the full timeline to accommodate one asset. Do not replace source audio as a side effect of video stitching. A failed render is failed, not a completed checkpoint.
