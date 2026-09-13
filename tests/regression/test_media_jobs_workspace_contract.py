"""Regression test: media_jobs outputs must live under projects/<project_id>/.

Bug observed 2026-09-08: vclaw-driven reference-remix runs wrote
intermediate images to ``projects/_scratch/image_gen/keyframes/`` and
final video to ``/tmp/`` instead of under ``projects/<project_id>/``.

Symptom (frontend error): **"镜头 1 的生成图片无法读取"** — the
``video_compose`` stage raised ``FileNotFoundError`` because the shot
keyframe it expected at
``projects/<user>/<project_id>/references/artifacts/keyframes/remix-001.png``
did not exist; the actual file lived in
``projects/_scratch/image_gen/keyframes/remix-001.png`` (orphan of an
image_selector run that never created its project subdirectory).

Three evening batches (07:16 / 07:43 / 11:06 HKT) all reproduced the same
failure mode. The morning batch (06:00-06:08) created project subdirectories
under ``projects/users/<user>/20260908-*/`` but still routed the final
render to ``/tmp/``. Throughout the 5h+ MCP uptime, **zero** ``init_project``
calls were logged — vclaw systematically skips the workspace bootstrap.

Per CLAUDE.md Core Invariant #6:

    Tool outputs go under ``projects/<project-id>/``. Specifying a path
    outside ``projects/`` is invisible to the Backlot board and violates
    the workspace contract. Outputs with no real project (smoke-test TTS,
    ad-hoc renders, debug dumps) go to ``projects/_scratch/<category>/``
    instead — never to the repo root.

These two tests pin that contract at the disk-state layer so any future
vclaw wrapper drift fails loud here instead of at the front-end.

Run with::

    .venv/bin/python -m pytest tests/regression/test_media_jobs_workspace_contract.py -v
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MEDIA_JOBS = ROOT / "projects" / ".media_jobs.json"
PROJECTS_DIR = ROOT / "projects"
SCRATCH_KEYFRAMES = PROJECTS_DIR / "_scratch" / "image_gen" / "keyframes"

# Project-id substrings that indicate a smoke / test / debug job — these are
# allowed to write outside ``projects/`` per the Core Invariant #6 escape
# clause. Keep this list conservative: when in doubt, do NOT exempt.
SMOKE_HINTS = ("smoke", "test_", "_test", "scratch", "debug", "probe", "verify")


def _is_smoke_project(project_id: str) -> bool:
    pid = (project_id or "").lower()
    return any(h in pid for h in SMOKE_HINTS)


class TestMediaJobsWorkspaceContract(unittest.TestCase):
    """Disk-state contract: production outputs live under projects/."""

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _load_jobs() -> dict:
        if not MEDIA_JOBS.exists():
            return {}
        with MEDIA_JOBS.open() as f:
            return json.load(f)

    @staticmethod
    def _video_path_under_projects(video_path: str, project_id: str) -> bool:
        """True iff ``video_path`` resolves under ``projects/<project_id>/``.

        Two accepted layouts:
          1. flat:   ``projects/<project_id>/renders/...``
          2. nested: ``projects/users/<user>/<project_id>/renders/...``

        The nested match intentionally does not pin the user_id segment,
        because vclaw mints per-tenant user ids and the contract is about
        ``project_id``-rooted containment, not the user-id form.
        """
        if not video_path or not project_id:
            return False
        v = str(Path(video_path).resolve())
        flat = (PROJECTS_DIR / project_id).resolve()
        nested_pattern = PROJECTS_DIR / "users" / "_any_user_" / project_id
        nested_pattern_resolved = nested_pattern.resolve()
        # Replace the synthetic user segment to test containment.
        try:
            v_under_nested = (PROJECTS_DIR / "users").resolve()
            # Walk up to find a parent that ends with /<project_id>
            cur = Path(v)
            while cur != cur.parent:
                if cur.name == project_id and PROJECTS_DIR in cur.parents:
                    return True
                cur = cur.parent
        except Exception:
            pass
        # Flat check: video_path's parent chain must contain the project dir.
        return flat in Path(v).parents or any(
            p.name == project_id and PROJECTS_DIR in p.parents
            for p in Path(v).parents
        )

    # ---- test 1: published video_path is project-rooted --------------------

    def test_published_video_path_under_project(self):
        """Every published media_jobs entry's video_path must live under
        ``projects/<project_id>/``. /tmp/ and _scratch/ are not allowed
        for production (non-smoke) jobs."""
        jobs = self._load_jobs()
        if not jobs:
            self.skipTest(f"media_jobs.json missing or empty at {MEDIA_JOBS}")

        offenders = []
        for job_id, job in jobs.items():
            if job.get("status") != "published":
                continue
            project_id = job.get("project_id", "")
            video_path = job.get("video_path", "")
            if _is_smoke_project(project_id):
                continue
            if not video_path:
                continue  # no claim being made
            if not self._video_path_under_projects(video_path, project_id):
                offenders.append({
                    "job_id": job_id,
                    "project_id": project_id,
                    "video_path": video_path,
                    "vclaw_job_id": job.get("metadata", {}).get("vclaw_job_id"),
                    "metadata_profile": job.get("metadata", {}).get("profile"),
                })

        self.assertEqual(
            offenders, [],
            "Production media_jobs entries must have video_path under "
            "projects/<project_id>/. The following offenders violate "
            "Core Invariant #6 (workspace contract):\n"
            + json.dumps(offenders, indent=2),
        )

    # ---- test 2: no orphan scratch keyframes -------------------------------

    def test_no_orphan_scratch_keyframes(self):
        """No .png older than 5 minutes in ``_scratch/image_gen/keyframes/``
        without a matching reference in ``.media_jobs.json``.

        A stale file here is the exact symptom that produced the
        2026-09-08 "镜头 1 的生成图片无法读取" failure: an ``image_selector``
        call landed in ``_scratch/`` but the downstream ``video_compose``
        stage (and front-end) looked under
        ``projects/<user>/<project_id>/references/artifacts/keyframes/``.
        """
        if not SCRATCH_KEYFRAMES.exists():
            return  # nothing to check; pass

        # Load jobs once and build a filename → referenced set.
        jobs = self._load_jobs()
        referenced_filenames: set[str] = set()
        referenced_project_paths: set[str] = set()
        for job in jobs.values():
            vp = job.get("video_path") or ""
            referenced_filenames.add(Path(vp).name)
            referenced_project_paths.add(str(Path(vp).resolve()))

        cutoff = time.time() - 300  # 5 minutes
        orphans = []
        for png in sorted(SCRATCH_KEYFRAMES.glob("*.png")):
            try:
                mtime = png.stat().st_mtime
            except FileNotFoundError:
                continue
            age_s = int(time.time() - mtime)
            if mtime >= cutoff:
                continue  # fresh — possibly still in flight
            # Stale file: must be referenced somewhere by a published job,
            # otherwise it's an orphan from an abandoned run.
            if png.name in referenced_filenames:
                continue
            orphans.append({
                "file": str(png),
                "age_seconds": age_s,
                "size_bytes": png.stat().st_size,
            })

        self.assertEqual(
            orphans, [],
            "Stale keyframe files in _scratch/image_gen/keyframes/ signal a "
            "production image_selector run that never promoted into a "
            "projects/<project_id>/ tree — the regression for the "
            "2026-09-08 'shot 1 image cannot be read' failure:\n"
            + json.dumps(orphans, indent=2),
        )


if __name__ == "__main__":
    unittest.main()
