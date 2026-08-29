# Video Template Remix — Edit Director

## When to Use

Use this stage to turn the approved template slots and asset manifest into executable edit decisions.

## Prerequisites
Read `scene_plan`, `asset_manifest`, and source policy. Treat the source timeline as immutable reference.

## Process
Emit edit decisions for every scene: source clip/time range, replacement asset if approved, preserve/delete action, exact duration, transition, subtitle track, audio routing, and crop. Add an audit summary counting preserved/replaced/deleted scenes and list every deviation. Default to source audio and subtitles.

## Self-Evaluate
5 means decisions are executable and one-to-one with the source timeline; 3 means only non-material metadata warnings; 0 means any silent timing or policy drift.

## Pitfalls
Do not re-cut for “better rhythm.” Do not delete filler, subtitles, or ambience without an explicit decision.
