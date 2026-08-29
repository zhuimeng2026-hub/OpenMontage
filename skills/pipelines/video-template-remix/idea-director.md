# Video Template Remix — Idea Director

## When to Use

Use this stage after a reference video is available and before any replacement asset is selected. Its job is to turn the user's request into an explicit preserve/replace/delete contract.

## Prerequisites
Read the manifest, source-media review, and rights/provenance notes. Require a source URL/file, target deliverable, and an explicit list of visual elements the user may replace.

## Process
Write a brief naming source, audience, target duration/aspect ratio, and the approved slot list. Classify every intended change as `preserve`, `replace`, or `delete`; default unspecified elements to preserve. Record original audio, subtitle, transition, and timing policies in `decision_log`.

Record a `render_runtime_selection` decision during planning. When both Remotion and hyperframes are available, **Present both** to the user with FFmpeg as the source-faithful assembly option; do not silently choose one. Persist the approved `render_runtime` for later stages.

## Self-Evaluate
5 means scope is explicit, source rights are known, and no replacement can be inferred ambiguously. Deduct 1 for each missing policy or unsupported promise.

## Pitfalls
Never propose a whole-video rewrite when the request is a slot remix. Flag copyrighted source material and require user confirmation of permission.
