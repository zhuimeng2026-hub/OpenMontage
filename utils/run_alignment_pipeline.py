#!/usr/bin/env python3
"""
End-to-end subtitle alignment pipeline.

Wraps the two-step process (calibrate → align) into a single command.
Use this when you want to align a freshly-produced Kokoro voiceover
without writing any glue code.

Steps performed:
  1. Run calibrate_subtitle_alignment.py logic (or load existing)
  2. Run WhisperX on voiceover.wav
  3. Map to canonical sentences (using calibration hints)
  4. Write aligned SRT

Usage
-----
    .venv/bin/python utils/run_alignment_pipeline.py \\
        --voiceover renders/voiceover.wav \\
        --sentences-file renders/sentences.json \\
        --calibration artifacts/calibration.json \\
        --output renders/subtitles_aligned.srt

Where --sentences-file is a JSON list like:
    ["出门旅行...小聪明。", "小鹏哥...一只箱子。", ...]

This is essentially a thin wrapper that calls utils/align_subtitles.py.
It exists so users don't have to remember to import the registry or
configure WhisperX model size manually — everything is read from
calibration.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='End-to-end subtitle alignment pipeline',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--voiceover', type=Path, required=True,
                        help='Path to Kokoro voiceover WAV')
    parser.add_argument('--sentences-file', type=Path, required=True,
                        help='JSON file with canonical sentences (list)')
    parser.add_argument('--calibration', type=Path,
                        help='calibration.json (auto-detect if not given)')
    parser.add_argument('--output', type=Path, required=True,
                        help='Where to write the aligned SRT')
    parser.add_argument('--auto-calibrate', action='store_true',
                        help='Run calibration before align (if --calibration missing or stale)')

    args = parser.parse_args(argv)

    # Resolve calibration path
    cal_path = args.calibration
    if cal_path is None:
        # Try common locations
        candidates = [
            Path('artifacts/calibration.json'),
            Path('../artifacts/calibration.json'),
            args.voiceover.parent / 'calibration.json',
        ]
        for c in candidates:
            if c.exists():
                cal_path = c
                print(f'[pipeline] using calibration at {cal_path}')
                break
        if cal_path is None:
            if args.auto_calibrate:
                print('[pipeline] no calibration found; running calibration now...')
                # Re-exec self with --calibrate flag
                import subprocess
                rc = subprocess.run([
                    sys.executable,
                    str(Path(__file__).parent / 'calibrate_subtitle_alignment.py'),
                    '--output', str(args.voiceover.parent / 'calibration.json'),
                ], check=False)
                if rc.returncode != 0:
                    print('[pipeline] calibration failed; aborting', file=sys.stderr)
                    return 1
                cal_path = args.voiceover.parent / 'calibration.json'
            else:
                print('[pipeline] WARN: no calibration.json found; '
                      'align_subtitles.py will use defaults', file=sys.stderr)

    # Load sentences
    sentences_data = json.loads(args.sentences_file.read_text())
    if isinstance(sentences_data, dict):
        sentences = sentences_data.get('sentences', [])
    else:
        sentences = sentences_data
    sentences = [s for s in sentences if s and s.strip()]
    if not sentences:
        print('ERROR: no sentences loaded', file=sys.stderr)
        return 1
    print(f'[pipeline] {len(sentences)} canonical sentences loaded')

    # Import registry
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    try:
        from tools.tool_registry import registry
    except ImportError as e:
        print(f'ERROR: cannot import tools.tool_registry: {e}', file=sys.stderr)
        return 1
    registry.discover()
    asr_tool = registry._tools.get('transcriber')
    if not asr_tool:
        print('ERROR: transcriber tool not found in registry', file=sys.stderr)
        return 1

    # Delegate to align_subtitles
    from utils.align_subtitles import align_subtitles_to_voiceover

    align_subtitles_to_voiceover(
        voiceover_path=args.voiceover,
        canonical_sentences=sentences,
        output_srt_path=args.output,
        asr_tool=asr_tool,
        calibration_path=cal_path,
    )

    print('[pipeline] done. Next steps:')
    print('  ffmpeg -i final.mp4 -vf "subtitles=...srt:force_style=..." -c:a copy final_aligned.mp4')
    return 0


if __name__ == '__main__':
    sys.exit(main())
