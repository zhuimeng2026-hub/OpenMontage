# Video Template Remix — Scene Director

## When to Use

Use this stage to convert source analysis into the canonical, gap-free shot timeline used by replacement planning.

## Prerequisites
Read `script`, `brief`, and source analysis. Inspect sampled frames and scene-detection output; if tools disagree, preserve the conservative boundary and record uncertainty.

## Process
Create one ordered scene per source shot with `start`, `end`, duration, role, transition-in/out, subtitle safe zone, audio state, and slot policy. Identify replaceable content only from the approved scope; all other pixels are source-preserved. Include a frame/contact-sheet reference for each scene.

Keep each scene object schema-valid. Store per-shot slot policy, source timestamps, subtitle zones, audio state, detector confidence, and contact-sheet references under top-level `scene_plan.metadata.template_slots`, keyed by scene ID; do not add undeclared fields directly to scene objects.

## Self-Evaluate
5 means full time coverage with no overlaps/gaps, exact slot classification, and transition/subtitle metadata for every shot. Deduct for guessed boundaries or missing visual evidence.

## Pitfalls
Uniform sampling is not scene detection. A long source video must not collapse into one scene because a detector returned one interval; retry with frame sampling and report degraded analysis.
