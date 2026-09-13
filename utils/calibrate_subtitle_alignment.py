#!/usr/bin/env python3
"""
Calibrate the subtitle ↔ voiceover alignment environment.

Run this once per host / new environment BEFORE attempting any
Kokoro → SRT subtitle alignment. The output is a `calibration.json`
file that captures the runtime characteristics of:
  - Kokoro TTS (speed, pause marker behavior)
  - WhisperX / faster-whisper ASR (segment count, merging behavior)

Why this exists
---------------
The alignment strategy documented in `docs/SUBTITLE-VOICEOVER-ALIGNMENT.md`
assumes specific runtime characteristics. Those characteristics are
NOT guaranteed across hosts because:
  - Kokoro version and `misaki[zh]` availability affect speech rate
  - WhisperX model choice (base vs large-v3) affects segment merging
  - Cache state determines which models are usable

Running this script once per environment produces a calibration file
that downstream code reads to choose the right alignment strategy
(hand-split at index N, CTA estimation, etc.).

Usage
-----
    .venv/bin/python utils/calibrate_subtitle_alignment.py \\
        --tts-tool kokoro_tts \\
        --asr-tool transcriber \\
        --output artifacts/calibration.json

Or import programmatically:

    from utils.calibrate_subtitle_alignment import calibrate
    cal = calibrate(tts_tool=registry._tools['kokoro_tts'],
                    asr_tool=registry._tools['transcriber'],
                    output_path=Path('calibration.json'))

Output
------
calibration.json with this shape:

    {
      "captured_at": "2026-09-10T21:30:00+08:00",
      "host": {"hostname": "...", "python_version": "..."},
      "kokoro": {
        "voice_id": "zf_xiaobei",
        "lang_code": "z",
        "8_sentences_with_pause_03_total_duration_sec": 25.15,
        "chars_per_second": 8.36,
        "warning": null
      },
      "whisperx": {
        "model_size": "base",
        "8_sentences_segment_count": 7,
        "merged_segments": [6],
        "merged_at_index": 6,
        "warning": null
      },
      "implications": [
        "hand-split needed at segment 6",
        "CTA section must be estimated from voiceover total length"
      ]
    }

If anything looks off (e.g. Kokoro took >60s for 8 sentences, or
WhisperX produced 1 segment for all 8 sentences), the calibration
record includes a `warning` field describing the issue.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
import wave
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

# Beijing time (matches the host's working timezone in OBSERVABILITY logs)
TZ_BJ = timezone(timedelta(hours=8))


# Canonical 8-sentence probe (same as the 60s rideable-luggage-demo script).
# Update this if your pipeline uses a different probe.
PROBE_SENTENCES = [
    "出门旅行，行李箱也可以更聪明。",
    "小鹏哥，把收纳、移动和骑行装进一只箱子。",
    "打开座垫，握住车把，穿过平整路面。",
    "不骑时，收起车把，照常拖行。",
    "接上电源，为下一段旅程补充能量。",
    "侧开空间，衣物和随身物品一目了然。",
    "放进后备箱，抵达酒店，咖啡街角，随时切换。",
    "小鹏哥，让每次出发更轻松。",
]
PROBE_PAUSE = "[pause:0.3]"  # Same as default Kokoro pause in OM scripts


def get_audio_duration(path: Path) -> float:
    """Return WAV duration in seconds using stdlib wave (no ffmpeg dependency)."""
    with wave.open(str(path), 'rb') as w:
        return w.getnframes() / w.getframerate()


def build_probe_text(sentences: list[str], pause: str = PROBE_PAUSE) -> str:
    """Build a probe text with pause markers between every sentence.

    Using [pause:0.3] (not 0.5) is intentional: shorter pauses are
    more typical for TikTok-style narration and reveal ASR
    sensitivity better. If your project uses different pause lengths,
    override via CLI.
    """
    return pause.join(sentences)


def run_kokoro_probe(
    tts_tool: Any,
    probe_text: str,
    voice_id: str = 'zf_xiaobei',
    output_path: Path = Path('/tmp/kokoro_probe.wav'),
) -> dict:
    """Synthesize the probe text and measure characteristics."""
    t0 = time.time()
    result = tts_tool.execute({
        'text': probe_text,
        'voice_id': voice_id,
        'output_path': str(output_path),
        'speed': 1.0,
    })
    wall = time.time() - t0

    if not result.success:
        return {
            'success': False,
            'error': result.error,
            'wall_seconds': round(wall, 1),
            'warning': 'Kokoro probe failed; alignment tool will not work',
        }

    duration = get_audio_duration(output_path)
    char_count = sum(len(s) for s in PROBE_SENTENCES)
    cps = round(char_count / duration, 2) if duration > 0 else 0.0

    # Sanity check: if 8 short sentences take >60s, something is wrong
    # (cold model load, missing misaki[zh], wrong voice, etc.)
    warning = None
    if duration > 60:
        warning = (
            f"Voiceover took {duration:.1f}s for {char_count} chars "
            f"({cps} chars/sec). Expected 20-30s for healthy Kokoro. "
            "Check: misaki[zh] installed? voice_id correct? cold load?"
        )
    elif duration < 8:
        warning = (
            f"Voiceover unusually fast ({duration:.1f}s for {char_count} chars, "
            f"{cps} chars/sec). Verify voice_id matches expected output."
        )

    return {
        'success': True,
        'voice_id': voice_id,
        'lang_code': 'z',  # hardcoded; Kokoro-82M default for Mandarin
        '8_sentences_with_pause_03_total_duration_sec': round(duration, 2),
        'char_count': char_count,
        'chars_per_second': cps,
        'wall_seconds': round(wall, 1),
        'output_path': str(output_path),
        'warning': warning,
    }


def run_whisperx_probe(
    asr_tool: Any,
    audio_path: Path,
    language: str = 'zh',
    model_size: str = 'base',
) -> dict:
    """Run ASR on the probe audio and analyze segment merging behavior."""
    t0 = time.time()
    result = asr_tool.execute({
        'input_path': str(audio_path),
        'language': language,
        'model_size': model_size,
        'output_dir': str(audio_path.parent),
    })
    wall = time.time() - t0

    if not result.success:
        return {
            'success': False,
            'error': result.error,
            'wall_seconds': round(wall, 1),
            'warning': 'WhisperX probe failed; alignment tool will not work',
        }

    data = result.data
    segments = data.get('segments', [])
    segment_count = len(segments)
    canonical_count = len(PROBE_SENTENCES)

    # Detect merged segments: heuristic — a segment whose text length is
    # significantly longer than the mean sentence length suggests it
    # merged multiple canonical sentences.
    mean_sentence_len = sum(len(s) for s in PROBE_SENTENCES) / canonical_count
    merged_segments = []
    for i, seg in enumerate(segments):
        text_len = len(seg.get('text', ''))
        # Merged segment: text len > 1.5x mean canonical sentence len
        # AND text contains typical multi-sentence punctuation
        if text_len > mean_sentence_len * 1.5 and (
            '，' in seg.get('text', '') or '、' in seg.get('text', '')
        ):
            merged_segments.append(i)

    # Detect first/last segment truncated (text ends without punctuation)
    truncated_first = segments and not segments[0]['text'][0] in '出门小打开不接侧放进'
    # This heuristic is brittle; better to detect by checking if total
    # ASR text length < 80% of canonical char count.
    total_asr_chars = sum(len(seg.get('text', '')) for seg in segments)
    truncation_warning = None
    if total_asr_chars < 0.7 * sum(len(s) for s in PROBE_SENTENCES):
        truncation_warning = (
            f"ASR only captured {total_asr_chars} chars vs {sum(len(s) for s in PROBE_SENTENCES)} canonical. "
            f"Some sentences may be missing (CTA section commonly dropped by base model)."
        )

    warning = None
    if segment_count < canonical_count:
        merged_or_missing = canonical_count - segment_count
        warning = (
            f"ASR produced {segment_count} segments for {canonical_count} canonical sentences. "
            f"Either {merged_or_missing} segments were merged (hand-split needed) "
            f"or some sentences lost to ASR dropout (check transcript)."
        )

    return {
        'success': True,
        'model_size': model_size,
        'language': language,
        'segment_count': segment_count,
        'canonical_count': canonical_count,
        'merged_segments': merged_segments,
        'truncation_warning': truncation_warning,
        'wall_seconds': round(wall, 1),
        'warning': warning,
    }


def derive_implications(kokoro: dict, whisperx: dict) -> list[str]:
    """Produce human-readable hints for downstream alignment code."""
    implications = []

    if not kokoro.get('success'):
        return ['KOKORO FAILED — alignment pipeline cannot run. Check error.']
    if not whisperx.get('success'):
        return ['WHISPERX FAILED — alignment pipeline cannot run. Check error.']

    # CTA estimation needed?
    n_asr = whisperx['segment_count']
    n_canon = whisperx['canonical_count']
    if n_asr < n_canon:
        implications.append(
            f'ASR produced {n_asr}/{n_canon} segments; '
            f'{(n_canon - n_asr)} segment(s) merged or missing; '
            f'CTA section must be estimated from voiceover total length.'
        )
    if whisperx.get('merged_segments'):
        merged_idx = whisperx['merged_segments']
        implications.append(
            f'Hand-split needed at merged segment index(es) {merged_idx}; '
            f'use mid-segment or char-count ratio split.'
        )

    if kokoro.get('chars_per_second', 0) > 10:
        implications.append(
            f'Kokoro speech rate ({kokoro["chars_per_second"]} chars/sec) is fast; '
            f'voiceover may feel rushed; consider adding longer pause markers.'
        )

    if not implications:
        implications.append(
            'Calibration clean: ASR aligns 1-to-1 with canonical sentences, '
            'no hand-split needed; CTA timestamp can be derived from last ASR segment end.'
        )

    return implications


def calibrate(
    tts_tool: Any,
    asr_tool: Any,
    output_path: Path,
    voice_id: str = 'zf_xiaobei',
    language: str = 'zh',
    model_size: str = 'base',
    probe_text: Optional[str] = None,
    probe_audio_path: Path = Path('/tmp/kokoro_probe.wav'),
) -> dict:
    """Run full calibration and write JSON. Returns the calibration dict."""
    if probe_text is None:
        probe_text = build_probe_text(PROBE_SENTENCES)

    print(f'[calibrate] synthesizing Kokoro probe ({len(PROBE_SENTENCES)} sentences)...')
    kokoro_result = run_kokoro_probe(tts_tool, probe_text, voice_id, probe_audio_path)

    if not kokoro_result['success']:
        # Still write what we have
        cal = {
            'captured_at': datetime.now(TZ_BJ).isoformat(),
            'host': {
                'hostname': platform.node(),
                'python_version': platform.python_version(),
            },
            'kokoro': kokoro_result,
            'whisperx': {'success': False, 'error': 'not run because kokoro probe failed'},
            'implications': derive_implications(kokoro_result, {'success': False}),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(cal, indent=2, ensure_ascii=False))
        print(f'[calibrate] wrote {output_path} (kokoro failed)')
        return cal

    print(f'[calibrate] kokoro: {kokoro_result["8_sentences_with_pause_03_total_duration_sec"]}s, '
          f'{kokoro_result["chars_per_second"]} chars/sec')
    if kokoro_result.get('warning'):
        print(f'[calibrate] WARNING: {kokoro_result["warning"]}')

    print(f'[calibrate] running WhisperX ASR ({model_size}, {language})...')
    whisperx_result = run_whisperx_probe(asr_tool, probe_audio_path, language, model_size)

    if not whisperx_result['success']:
        cal = {
            'captured_at': datetime.now(TZ_BJ).isoformat(),
            'host': {
                'hostname': platform.node(),
                'python_version': platform.python_version(),
            },
            'kokoro': kokoro_result,
            'whisperx': whisperx_result,
            'implications': derive_implications(kokoro_result, whisperx_result),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(cal, indent=2, ensure_ascii=False))
        print(f'[calibrate] wrote {output_path} (whisperx failed)')
        return cal

    print(f'[calibrate] whisperx: {whisperx_result["segment_count"]} segments '
          f'(canonical: {whisperx_result["canonical_count"]})')
    if whisperx_result.get('warning'):
        print(f'[calibrate] WARNING: {whisperx_result["warning"]}')

    cal = {
        'captured_at': datetime.now(TZ_BJ).isoformat(),
        'host': {
            'hostname': platform.node(),
            'python_version': platform.python_version(),
        },
        'kokoro': kokoro_result,
        'whisperx': whisperx_result,
        'implications': derive_implications(kokoro_result, whisperx_result),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(cal, indent=2, ensure_ascii=False))
    print(f'[calibrate] wrote {output_path}')
    return cal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Calibrate subtitle alignment environment for OM',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--tts-tool', default='kokoro_tts',
                        help='OM TTS tool name (default: kokoro_tts)')
    parser.add_argument('--asr-tool', default='transcriber',
                        help='OM ASR tool name (default: transcriber)')
    parser.add_argument('--voice-id', default='zf_xiaobei',
                        help='Kokoro voice id (default: zf_xiaobei)')
    parser.add_argument('--language', default='zh',
                        help='ASR language code (default: zh)')
    parser.add_argument('--model-size', default='base',
                        help='WhisperX model size (default: base)')
    parser.add_argument('--probe-audio', type=Path,
                        default=Path('/tmp/kokoro_probe.wav'),
                        help='Where to write the probe audio')
    parser.add_argument('--output', type=Path,
                        default=Path('artifacts/calibration.json'),
                        help='Where to write calibration.json')

    args = parser.parse_args(argv)

    # Import OM registry
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    try:
        from tools.tool_registry import registry
    except ImportError as e:
        print(f'ERROR: cannot import tools.tool_registry: {e}', file=sys.stderr)
        print('Run from the OM repo root: python utils/calibrate_subtitle_alignment.py', file=sys.stderr)
        return 1
    registry.discover()

    tts_tool = registry._tools.get(args.tts_tool)
    asr_tool = registry._tools.get(args.asr_tool)
    if not tts_tool:
        print(f'ERROR: TTS tool "{args.tts_tool}" not found in registry', file=sys.stderr)
        return 1
    if not asr_tool:
        print(f'ERROR: ASR tool "{args.asr_tool}" not found in registry', file=sys.stderr)
        return 1

    cal = calibrate(
        tts_tool=tts_tool,
        asr_tool=asr_tool,
        output_path=args.output,
        voice_id=args.voice_id,
        language=args.language,
        model_size=args.model_size,
        probe_audio_path=args.probe_audio,
    )

    # Print implications
    print('\n[calibrate] IMPLICATIONS:')
    for imp in cal.get('implications', []):
        print(f'  - {imp}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
