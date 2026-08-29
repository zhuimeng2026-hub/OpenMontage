# Video Template Remix — Asset Director

## When to Use

Use this stage after replacement slots are approved and before edit decisions are finalized.

## Prerequisites
Read `scene_plan` and approved slot decisions. Read the relevant provider skill before any image/video generation call.

## Process
Resolve user-provided or licensed assets first. For each replace slot, record source slot, asset path, provenance, rights, aspect ratio, duration, crop/fill behavior, and whether generation was approved. Fit replacement media to the original hold; reject assets that require timing changes unless edit approval explicitly allows it. Preserve source audio and subtitles as assets unless marked replace.

## Self-Evaluate
5 means every replacement is traceable, duration-safe, and visually compatible; 3 means one manual crop warning; 0 means an unapproved or untraceable asset is used.

## Pitfalls
Never use a generated “similar” asset to fill an unapproved slot. Never overwrite source files. Do not claim a local placeholder is final.
