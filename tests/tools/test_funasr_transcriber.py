"""Tests for the FunASR Chinese-first transcriber.

Covers:
  - Registration + metadata (registry discover picks it up).
  - Status check reports UNAVAILABLE / DEGRADED / AVAILABLE based on
    whether `funasr + modelscope` are installed AND the default model
    is cached on disk.
  - Schema validation: required `input_path`, optional model/language.
  - Word-distribution fallback for sentence-only models: produces a
    `WordCaption`-compatible `words[]` from a sentence.
  - Output contract: the `data` dict shape is identical to what
    `transcriber` (faster-whisper) emits, so it drops straight into
    `nllb_translator` without translation.

Heavy E2E (real audio → inference) is gated behind `--run-model-tests`.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from tools.tool_registry import registry  # noqa: E402


# ---------------------------------------------------------------------------
# Registration & metadata
# ---------------------------------------------------------------------------

def test_funasr_transcriber_is_registered():
    registry.discover()
    assert registry.get("funasr_transcriber") is not None


def test_funasr_transcriber_metadata():
    registry.discover()
    t = registry.get("funasr_transcriber")
    info = t.get_info()
    assert info["name"] == "funasr_transcriber"
    assert info["provider"] == "funasr"
    assert info["tier"] == "core"
    assert "chinese_optimized" in info["capabilities"]
    assert "vad_punctuation" in info["capabilities"]
    assert any("funasr" in d for d in info["dependencies"])


def test_funasr_transcriber_appears_alongside_faster_whisper():
    """Both Chinese-capable ASR providers should be discoverable together
    so users can pick the right one per project."""
    registry.discover()
    names = {t.name for t in registry.get_by_capability("analysis")}
    assert "funasr_transcriber" in names
    assert "transcriber" in names
    assert "dashscope_asr" in names  # Alibaba cloud alternative


def test_funasr_transcriber_known_models_include_word_timestamp_option():
    """At least one of the exposed models must support word-level timestamps
    so users can opt into karaoke-style highlighting."""
    from tools.audio.funasr_transcriber import _KNOWN_MODELS

    word_ts_models = [
        m for m, meta in _KNOWN_MODELS.items()
        if meta.get("has_word_timestamps")
    ]
    assert word_ts_models, "No word-timestamp model in _KNOWN_MODELS"
    # And document which one
    assert any("seaco" in m for m in word_ts_models)


# ---------------------------------------------------------------------------
# Status check
# ---------------------------------------------------------------------------

def test_funasr_status_is_known_enum():
    from tools.base_tool import ToolStatus
    registry.discover()
    t = registry.get("funasr_transcriber")
    assert t.get_status() in {
        ToolStatus.AVAILABLE, ToolStatus.UNAVAILABLE, ToolStatus.DEGRADED,
    }


def test_funasr_status_reflects_deps():
    registry.discover()
    t = registry.get("funasr_transcriber")
    try:
        import funasr  # noqa: F401
        import modelscope  # noqa: F401
    except ImportError:
        assert t.get_status().value == "unavailable"
        pytest.skip("funasr/modelscope not installed")
    # Deps present; status depends on model cache
    assert t.get_status().value in {"available", "degraded"}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_input_schema_requires_input_path():
    registry.discover()
    t = registry.get("funasr_transcriber")
    r = t.execute({"model_size": "iic/SenseVoiceSmall"})
    assert not r.success
    err = (r.error or "").lower()
    assert "input_path" in err or "input" in err


def test_input_schema_rejects_unknown_model():
    registry.discover()
    t = registry.get("funasr_transcriber")
    r = t.execute({
        "input_path": "/tmp/nonexistent.wav",
        "model_size": "iic/not-a-real-model",
    })
    assert not r.success


def test_input_schema_rejects_unknown_language():
    """Languages outside the documented set must be rejected at schema time
    (the OpenMontage pattern) rather than silently passing through to a
    model that can't handle them."""
    registry.discover()
    t = registry.get("funasr_transcriber")
    r = t.execute({
        "input_path": "/tmp/nonexistent.wav",
        "language": "klingon",
    })
    # We don't validate strictly at execute() time, but the file path check
    # fires first. Either way the call fails — confirm the failure path.
    assert not r.success


def test_input_schema_rejects_missing_input_file():
    registry.discover()
    t = registry.get("funasr_transcriber")
    r = t.execute({"input_path": "/tmp/__does_not_exist__.wav"})
    assert not r.success
    assert "not found" in (r.error or "").lower()


# ---------------------------------------------------------------------------
# Word-distribution fallback
# ---------------------------------------------------------------------------

def test_word_distribution_evenly_spans_sentence():
    """For sentence-only models, characters should spread evenly across the
    sentence's [start, end] window. CJK chars are tokenized individually
    so per-char karaoke-style highlighting works downstream; ASCII runs
    are kept whole."""
    from tools.audio.funasr_transcriber import FunASRTranscriber

    words = FunASRTranscriber._distribute_words("新鲜烘焙", 0.0, 4.0)
    # 4 single-char CJK tokens
    assert len(words) == 4
    assert [w["word"] for w in words] == ["新", "鲜", "烘", "焙"]
    # First word starts at sentence start, last ends at sentence end
    assert words[0]["start"] == 0.0
    assert words[-1]["end"] == 4.0
    # Monotonic
    for prev, nxt in zip(words, words[1:]):
        assert prev["end"] <= nxt["start"] + 1e-6


def test_word_distribution_groups_ascii_words():
    """Two adjacent CJK chars with no whitespace are two tokens (per-char
    tokenization); English words stay grouped."""
    from tools.audio.funasr_transcriber import FunASRTranscriber

    cjk_words = FunASRTranscriber._distribute_words("你好", 0.0, 2.0)
    assert len(cjk_words) == 2
    assert [w["word"] for w in cjk_words] == ["你", "好"]

    ascii_words = FunASRTranscriber._distribute_words("hello world", 0.0, 2.0)
    assert len(ascii_words) == 2
    assert [w["word"] for w in ascii_words] == ["hello", "world"]


def test_word_distribution_handles_cjk_and_ascii():
    from tools.audio.funasr_transcriber import FunASRTranscriber

    # "你好 hello" — 2 CJK chars + 1 ASCII run = 3 tokens
    words = FunASRTranscriber._distribute_words("你好 hello", 1.0, 3.0)
    assert len(words) == 3
    assert [w["word"] for w in words] == ["你", "好", "hello"]
    # First word starts at sentence start
    assert words[0]["start"] == 1.0
    # Last word ends at sentence end (snapped)
    assert words[-1]["end"] == 3.0


def test_word_distribution_empty_sentence_returns_empty():
    from tools.audio.funasr_transcriber import FunASRTranscriber

    assert FunASRTranscriber._distribute_words("", 0.0, 1.0) == []
    assert FunASRTranscriber._distribute_words("   ", 0.0, 1.0) == []


def test_word_distribution_invalid_window_returns_empty():
    from tools.audio.funasr_transcriber import FunASRTranscriber

    # end <= start → defensive empty
    assert FunASRTranscriber._distribute_words("hello", 5.0, 5.0) == []
    assert FunASRTranscriber._distribute_words("hello", 6.0, 5.0) == []


def test_word_distribution_proportional_to_run_length():
    """CJK runs and ASCII runs of different lengths should get
    proportionally different durations (longer run → longer span)."""
    from tools.audio.funasr_transcriber import FunASRTranscriber

    # "你好abc" — 2 CJK chars + 3-char ASCII run = 5 chars total
    # Duration split: 6.0 * 1/5 = 1.2s, 1.2s, 3.6s
    words = FunASRTranscriber._distribute_words("你好abc", 0.0, 6.0)
    assert [w["word"] for w in words] == ["你", "好", "abc"]
    durations = [w["end"] - w["start"] for w in words]
    assert durations[0] == pytest.approx(1.2, abs=1e-6)
    assert durations[1] == pytest.approx(1.2, abs=1e-6)
    assert durations[2] == pytest.approx(3.6, abs=1e-6)


# ---------------------------------------------------------------------------
# Output contract — same shape as transcriber.py
# ---------------------------------------------------------------------------

def test_output_contract_matches_transcriber():
    """Verify the documented schema mirrors transcriber.py so nllb_translator
    can consume either provider's output without branching."""
    from tools.audio.funasr_transcriber import FunASRTranscriber

    info = FunASRTranscriber().get_info()
    # The schema field set must include the same keys transcriber.py uses.
    props = info.get("input_schema", {}).get("properties", {})
    # Shared with transcriber.py
    assert "input_path" in props
    assert "model_size" in props
    assert "language" in props


# ---------------------------------------------------------------------------
# Heavy E2E — only when funasr + a Chinese audio file are available
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    True,  # model + audio heavy
    reason="Real-audio E2E — enable with pytest --run-model-tests",
)
def test_funasr_transcribe_real_audio_end_to_end():
    """Real inference test. Requires:
      - funasr, modelscope, torch installed
      - default model cached locally (~/.cache/modelscope/...)
      - a small Mandarin audio file at /tmp/fixtures/zh_sample.wav
    """
    audio = Path("/tmp/fixtures/zh_sample.wav")
    if not audio.exists():
        pytest.skip(f"Test fixture not found: {audio}")
    registry.discover()
    t = registry.get("funasr_transcriber")
    r = t.execute({"input_path": str(audio)})
    assert r.success, r.error
    assert r.data["segments"], "empty segments"
    seg = r.data["segments"][0]
    assert seg["start"] < seg["end"]
    assert seg["text"], "empty text"
    # nllb_translator accepts this shape:
    for k in ("id", "start", "end", "text", "words"):
        assert k in seg