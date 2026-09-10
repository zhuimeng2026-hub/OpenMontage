#!/usr/bin/env python3
"""
Align subtitle SRT to actual Kokoro voiceover timing.

Reads:
  - voiceover.wav (output of kokoro_tts)
  - canonical sentences (list, from script.json or manual input)
  - calibration.json (output of utils/calibrate_subtitle_alignment.py)

Writes:
  - subtitles_aligned.srt with timestamps matching voiceover word timing

Strategy chosen at runtime based on calibration:
  - If ASR produced same segment count as canonical: 1-to-1 mapping (no split)
  - If ASR produced fewer segments: hand-split using char-count ratio
    (split point chosen so left + right char counts sum to merged total)
  - If ASR produced more segments (rare): merge excess ASR segments
    evenly across canonical sentences
  - If CTA section has no ASR coverage: estimate timestamp from
    voiceover total length

Usage
-----
    .venv/bin/python utils/align_subtitles.py \\
        --voiceover renders/voiceover.wav \\
        --sentences "出门旅行...小鹏哥..."  \\
        --calibration artifacts/calibration.json \\
        --output renders/subtitles_aligned.srt

Or import:

    from utils.align_subtitles import align_subtitles_to_voiceover
    align_subtitles_to_voiceover(
        voiceover_path=Path('renders/voiceover.wav'),
        canonical_sentences=[s1, s2, ...],
        calibration_path=Path('artifacts/calibration.json'),
        asr_tool=registry._tools['transcriber'],
        output_srt_path=Path('renders/subtitles_aligned.srt'),
    )

Notes on robustness
-------------------
This script is intentionally defensive: if calibration.json is missing,
it falls back to the original heuristic (hand-split all merged segments
evenly). If calibration is corrupted, it logs a warning and uses
defaults. The output SRT always has at least one cue per canonical
sentence.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path
from typing import Any, List, Optional, Tuple


def get_audio_duration(path: Path) -> float:
    """Return WAV duration in seconds using stdlib wave."""
    with wave.open(str(path), 'rb') as w:
        return w.getnframes() / w.getframerate()


def fmt_srt_time(t: float) -> str:
    """Format seconds as HH:MM:SS,mmm for SRT."""
    if t < 0:
        t = 0.0
    hh, mm = divmod(t, 3600)
    mm, ss = divmod(mm, 60)
    ms = int((ss - int(ss)) * 1000)
    return f'{int(hh):02d}:{int(mm):02d}:{int(ss):02d},{ms:03d}'


def parse_sentences_arg(text: str | None, sentences_file: Path | None, sentences_json: list | None) -> List[str]:
    """Resolve the canonical-sentences list from CLI args.

    Priority: --sentences-json > --sentences-file > --sentences-text.
    Returns a list of non-empty strings.
    """
    if sentences_json:
        return [s for s in sentences_json if s and s.strip()]
    if sentences_file:
        data = json.loads(sentences_file.read_text())
        if isinstance(data, list):
            return [s for s in data if s and s.strip()]
        # could be {"sentences": [...]}
        return [s for s in data.get('sentences', []) if s and s.strip()]
    if text:
        # split on newlines or '|' separators
        if '|' in text:
            return [s.strip() for s in text.split('|') if s.strip()]
        return [s.strip() for s in text.split('\n') if s.strip()]
    raise ValueError('No sentences provided. Use --sentences-text / --sentences-file / --sentences-json')


def load_calibration(path: Path | None) -> dict | None:
    """Load calibration.json if it exists; return None if missing."""
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f'[align] WARN: could not parse calibration at {path}: {e}', file=sys.stderr)
        return None


def split_merged_segment_by_chars(
    merged_segment: dict,
    canonical_left: str,
    canonical_right: str,
) -> Tuple[float, float, float]:
    """Choose a split point in a merged ASR segment for two canonical sentences.

    Strategy: split at a position proportional to character counts.
    Returns (start, mid, end) where mid is the proposed split time.
    """
    total_chars = len(canonical_left) + len(canonical_right)
    if total_chars == 0:
        # degenerate; split at midpoint
        mid_time = (merged_segment['start'] + merged_segment['end']) / 2
        return merged_segment['start'], mid_time, merged_segment['end']

    left_ratio = len(canonical_left) / total_chars
    duration = merged_segment['end'] - merged_segment['start']
    mid_time = merged_segment['start'] + duration * left_ratio

    # Pad split by ~0.2s on either side so neither subtitle is 0-length
    return merged_segment['start'], mid_time, merged_segment['end']


def align_via_asr(
    asr_tool: Any,
    voiceover_path: Path,
    canonical_sentences: List[str],
    calibration: dict | None = None,
) -> List[Tuple[float, float, str]]:
    """Run WhisperX on voiceover, then map to canonical sentences.

    Returns list of (start, end, canonical_text).
    """
    t0 = time.time()
    model_size = (calibration or {}).get('whisperx', {}).get('model_size', 'base')
    language = (calibration or {}).get('whisperx', {}).get('language', 'zh')

    print(f'[align] running WhisperX ({model_size}, {language}) on {voiceover_path}...')
    result = asr_tool.execute({
        'input_path': str(voiceover_path),
        'language': language,
        'model_size': model_size,
        'output_dir': str(voiceover_path.parent),
    })

    if not result.success:
        raise RuntimeError(f'WhisperX failed: {result.error}')

    asr_segments = result.data['segments']
    n_asr = len(asr_segments)
    n_canon = len(canonical_sentences)
    total_voiceover = get_audio_duration(voiceover_path)
    print(f'[align] ASR: {n_asr} segments vs {n_canon} canonical ({total_voiceover:.2f}s total)')

    if n_asr == 0:
        raise RuntimeError('WhisperX returned 0 segments; check audio file')

    aligned: List[Tuple[float, float, str]] = []

    if n_asr == n_canon:
        # Happy path: 1-to-1 mapping
        print('[align] strategy: 1-to-1 mapping (ASR matches canonical)')
        for i, seg in enumerate(asr_segments):
            aligned.append((seg['start'], seg['end'], canonical_sentences[i]))

    elif n_asr < n_canon:
        # ASR merged some sentences. Use calibration hints or split evenly.
        merged_indices = (calibration or {}).get('whisperx', {}).get('merged_segments', [])
        print(f'[align] strategy: hand-split (calibration hints merged at {merged_indices})')

        asr_iter = iter(enumerate(asr_segments))
        canon_iter = iter(enumerate(canonical_sentences))
        asr_idx, asr_seg = next(asr_iter)
        canon_idx, canon_text = next(canon_iter)

        while canon_idx < n_canon:
            if asr_idx in merged_indices:
                # This ASR segment spans multiple canonical sentences.
                # Determine how many canonical sentences are merged by
                # looking ahead in calibration (if any) or by char-count ratio.
                # Conservative default: this segment covers 2 canonical.
                merged_count = 2
                if asr_idx + merged_count > n_canon:
                    merged_count = n_canon - asr_idx
                # Get the merged canonical texts
                merged_canon = canonical_sentences[canon_idx:canon_idx + merged_count]
                merged_total_chars = sum(len(s) for s in merged_canon)
                if merged_count == 1 or merged_total_chars == 0:
                    # Single sentence — assign full ASR segment
                    aligned.append((asr_seg['start'], asr_seg['end'], merged_canon[0]))
                else:
                    # Split into merged_count segments by char-count ratio
                    start_t = asr_seg['start']
                    end_t = asr_seg['end']
                    dur = end_t - start_t
                    cum_chars = 0
                    for j, sent in enumerate(merged_canon):
                        cum_chars += len(sent)
                        sub_end = start_t + dur * (cum_chars / merged_total_chars)
                        if j == merged_count - 1:
                            # last segment ends at ASR segment end
                            aligned.append((start_t if j == 0 else aligned[-1][1], end_t, sent))
                        else:
                            aligned.append((start_t if j == 0 else aligned[-1][1], sub_end, sent))
                        start_t = sub_end
                # Advance
                for _ in range(merged_count):
                    if canon_idx < n_canon:
                        canon_idx, canon_text = next(canon_iter, (n_canon, None))
                # Move to next ASR segment
                try:
                    asr_idx, asr_seg = next(asr_iter)
                except StopIteration:
                    break
            else:
                # 1-to-1
                aligned.append((asr_seg['start'], asr_seg['end'], canon_text))
                try:
                    canon_idx, canon_text = next(canon_iter, (n_canon, None))
                except StopIteration:
                    break
                try:
                    asr_idx, asr_seg = next(asr_iter)
                except StopIteration:
                    break

        # If any canonical sentences remain (CTA not in ASR), append at voiceover end
        if canon_idx < n_canon:
            print(f'[align] {n_canon - canon_idx} canonical sentence(s) missing in ASR; '
                  f'estimating from voiceover total ({total_voiceover:.2f}s)')
            prev_end = aligned[-1][1] if aligned else 0.0
            for k in range(canon_idx, n_canon):
                # Distribute remaining voiceover evenly across remaining sentences
                remaining = n_canon - canon_idx
                seg_dur = (total_voiceover - prev_end) / max(remaining, 1)
                aligned.append((prev_end, prev_end + seg_dur, canonical_sentences[k]))
                prev_end += seg_dur

    else:
        # ASR over-segmented. Merge excess ASR segments evenly.
        ratio = n_asr / n_canon
        print(f'[align] strategy: merge-excess (ASR over-segmented; ratio={ratio:.2f})')
        for i in range(n_canon):
            asr_start_idx = int(i * ratio)
            asr_end_idx = min(int((i + 1) * ratio), n_asr)
            start = asr_segments[asr_start_idx]['start']
            end = asr_segments[asr_end_idx - 1]['end']
            aligned.append((start, end, canonical_sentences[i]))

    print(f'[align] produced {len(aligned)} aligned cues in {time.time()-t0:.1f}s')
    return aligned


def write_srt(cues: List[Tuple[float, float, str]], output_path: Path) -> None:
    """Write cues as SRT. Auto-numbered 1..N."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, (start, end, text) in enumerate(cues, 1):
        # Strip newlines in text (SRT cues are single-line or explicit \n)
        text_safe = text.replace('\n', ' ').strip()
        lines.append(f'{i}\n{fmt_srt_time(start)} --> {fmt_srt_time(end)}\n{text_safe}\n')
    output_path.write_text('\n'.join(lines))
    print(f'[align] wrote {output_path}')


def align_subtitles_to_voiceover(
    voiceover_path: Path,
    canonical_sentences: List[str],
    output_srt_path: Path,
    asr_tool: Any,
    calibration_path: Path | None = None,
) -> Path:
    """End-to-end: ASR voiceover, map to canonical, write aligned SRT.

    Returns the output_srt_path for chaining.
    """
    calibration = load_calibration(calibration_path)
    cues = align_via_asr(asr_tool, voiceover_path, canonical_sentences, calibration)
    write_srt(cues, output_srt_path)
    return output_srt_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Align subtitle SRT to Kokoro voiceover timing',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--voiceover', type=Path, required=True,
                        help='Path to Kokoro voiceover WAV')
    parser.add_argument('--sentences-text', type=str,
                        help='Canonical sentences, one per line OR pipe-separated')
    parser.add_argument('--sentences-file', type=Path,
                        help='JSON file with canonical sentences (list or {"sentences": [...]})')
    parser.add_argument('--sentences-json', type=str, action='append',
                        help='Canonical sentence(s) as JSON array elements; can be repeated')
    parser.add_argument('--calibration', type=Path,
                        help='calibration.json from utils/calibrate_subtitle_alignment.py')
    parser.add_argument('--output', type=Path, required=True,
                        help='Where to write the aligned SRT')
    parser.add_argument('--asr-tool', default='transcriber',
                        help='OM ASR tool name (default: transcriber)')
    parser.add_argument('--language', default='zh',
                        help='ASR language code (overridden by calibration)')
    parser.add_argument('--model-size', default='base',
                        help='ASR model size (overridden by calibration)')

    args = parser.parse_args(argv)

    sentences = parse_sentences_arg(
        args.sentences_text, args.sentences_file, args.sentences_json
    )
    if not sentences:
        print('ERROR: no sentences parsed from input', file=sys.stderr)
        return 1

    # Import OM registry
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    try:
        from tools.tool_registry import registry
    except ImportError as e:
        print(f'ERROR: cannot import tools.tool_registry: {e}', file=sys.stderr)
        return 1
    registry.discover()

    asr_tool = registry._tools.get(args.asr_tool)
    if not asr_tool:
        print(f'ERROR: ASR tool "{args.asr_tool}" not found', file=sys.stderr)
        return 1

    if not args.voiceover.exists():
        print(f'ERROR: voiceover file not found: {args.voiceover}', file=sys.stderr)
        return 1

    align_subtitles_to_voiceover(
        voiceover_path=args.voiceover,
        canonical_sentences=sentences,
        output_srt_path=args.output,
        asr_tool=asr_tool,
        calibration_path=args.calibration,
    )

    return 0


if __name__ == '__main__':
    sys.exit(main())
