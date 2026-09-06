"""Command line interface: `python -m transformer`.

Usage:
    python -m transformer run \\
        --blueprint fixtures/target_blueprint.example.json \\
        --assets fixtures/assets \\
        --workspace data \\
        --workers 4 \\
        --quality draft

    python -m transformer run ... --no-render   # validate + assemble only

Exit codes:
    0 — success
    2 — blueprint validation failed (Pydantic errors)
    3 — render failed (HyperFrames runtime issue)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .orchestrator import result_to_dict, run


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transformer",
        description="Translate target_blueprint.json -> HyperFrames render.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="Translate + optionally render.")
    run_cmd.add_argument(
        "--blueprint",
        required=True,
        type=Path,
        help="Path to target_blueprint.json (MVP doc §11 schema).",
    )
    run_cmd.add_argument(
        "--assets",
        type=Path,
        default=None,
        help="Asset directory (id = filename stem). Auto-detected if absent.",
    )
    run_cmd.add_argument(
        "--workspace",
        type=Path,
        default=Path("data"),
        help="Workspace root. <workspace>/projects/<project_id>/.",
    )
    run_cmd.add_argument(
        "--music",
        type=Path,
        default=None,
        help="Optional background music file.",
    )
    run_cmd.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel scene worker count (default: min(cpu_count(), 8)).",
    )
    run_cmd.add_argument(
        "--quality",
        choices=("draft", "standard", "high"),
        default="draft",
        help="Passed through to `npx hyperframes render --quality`.",
    )
    run_cmd.add_argument(
        "--no-render",
        action="store_true",
        help="Skip npx hyperframes invocation; just emit cuts.json.",
    )
    run_cmd.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable INFO-level logging to stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        stream=sys.stderr,
    )

    if args.command != "run":
        parser.print_help()
        return 1

    try:
        result = run(
            blueprint_path=args.blueprint,
            workspace_root=args.workspace,
            assets_dir=args.assets,
            music_path=args.music,
            workers=args.workers,
            render=not args.no_render,
            quality=args.quality,
        )
    except Exception as exc:  # pragma: no cover - CLI generic catch
        print(
            json.dumps(
                {"status": "error", "phase": "validate_or_map", "error": str(exc)},
                indent=2,
            ),
            file=sys.stdout,
        )
        return 2

    payload = result_to_dict(result)
    payload["status"] = "ok"
    payload["rendered"] = bool(result.render and result.render.success)

    print(json.dumps(payload, indent=2, default=str), file=sys.stdout)

    if not args.no_render and result.render and not result.render.success:
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
