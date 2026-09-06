"""Piper local text-to-speech provider tool."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


DEFAULT_VOICE = "en_US-lessac-medium"
DEFAULT_DATA_DIR = Path.home() / ".piper" / "models"


def _resolve_data_dir(inputs: dict[str, Any] | None = None) -> Path:
    """Where voice .onnx files live: explicit input > PIPER_DATA_DIR > default.

    piper >=1.7 defaults `--data-dir` to the *current directory*, which makes
    generation depend on the caller's cwd. We always resolve and pass an
    absolute location instead.
    """
    raw = (inputs or {}).get("data_dir") or os.environ.get("PIPER_DATA_DIR")
    return Path(raw).expanduser() if raw else DEFAULT_DATA_DIR


def _resolve_voice(model: str, data_dir: Path) -> Path | None:
    """Resolve `model` to an on-disk .onnx, or None if it isn't downloaded.

    `model` may be a bare voice name (``en_US-lessac-medium``) or a path to an
    .onnx file. Piper picks up the sidecar ``<model>.onnx.json`` config itself.
    """
    candidate = Path(model).expanduser()
    if candidate.suffix == ".onnx" and candidate.is_file():
        return candidate
    onnx = data_dir / f"{model}.onnx"
    return onnx if onnx.is_file() else None


class PiperTTS(BaseTool):
    name = "piper_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "piper"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["python:piper"]
    install_instructions = (
        "Install Piper TTS:\n"
        "  pip install piper-tts\n"
        "Then download a voice model (piper >=1.7 no longer auto-downloads):\n"
        f"  python -m piper.download_voices --download-dir {DEFAULT_DATA_DIR} {DEFAULT_VOICE}\n"
        "The first download needs network access; export HTTPS_PROXY first if\n"
        "this host reaches huggingface.co only through a proxy. Override the\n"
        "voice location with PIPER_DATA_DIR or the 'data_dir' input."
    )
    agent_skills = ["text-to-speech"]

    capabilities = [
        "text_to_speech",
        "offline_generation",
    ]
    supports = {
        "voice_cloning": False,
        "multilingual": False,
        "offline": True,
        "native_audio": True,
    }
    best_for = [
        "offline narration fallback",
        "privacy-sensitive local-only workflows",
    ]
    not_good_for = [
        "best-in-class expressive voice quality",
        "voice clone matching",
    ]

    input_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string"},
            "model": {
                "type": "string",
                "default": DEFAULT_VOICE,
            },
            "data_dir": {
                "type": "string",
                "description": "Directory holding voice .onnx files (default: PIPER_DATA_DIR or ~/.piper/models)",
            },
            "speaker_id": {
                "type": "integer",
                "default": 0,
            },
            "length_scale": {
                "type": "number",
                "default": 1.0,
            },
            "sentence_silence": {
                "type": "number",
                "default": 0.3,
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2, ram_mb=512, vram_mb=0, disk_mb=200, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=[])
    idempotency_key_fields = ["text", "model", "speaker_id", "length_scale"]
    side_effects = ["writes audio file to output_path"]
    user_visible_verification = ["Listen to generated audio for intelligibility"]

    def get_status(self) -> ToolStatus:
        # Two independent things must hold, and neither is what the obvious
        # check would test.
        #
        # 1. The package must be importable. Checking `shutil.which("piper")`
        #    is unreliable on hosts where pyenv shims come first on PATH: the
        #    shim resolves to a Python version whose site-packages may not
        #    contain piper-tts, so `piper` is on PATH yet the subprocess exits
        #    127 with "command not found". Import-checking is the canonical
        #    "is it installed" question, and `_generate` recovers the
        #    executable by invoking the same Python via `python -m piper`.
        #
        # 2. A voice must be on disk. piper >=1.7 dropped voice auto-download
        #    (moved to `python -m piper.download_voices`), so an importable
        #    package with no .onnx reports available and then fails at execute
        #    time. That false positive is exactly what F-12 was written to
        #    prevent -- "importable is not enough" -- so the intent is kept
        #    here with a check that survives the pyenv shim problem.
        try:
            import piper  # noqa: F401
        except ImportError:
            return ToolStatus.UNAVAILABLE

        if not any(_resolve_data_dir().glob("*.onnx")):
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if self.get_status() != ToolStatus.AVAILABLE:
            return ToolResult(success=False, error="Piper TTS not available. " + self.install_instructions)

        start = time.time()
        try:
            result = self._generate(inputs)
        except Exception as exc:
            return ToolResult(success=False, error=f"Local TTS generation failed: {exc}")

        result.duration_seconds = round(time.time() - start, 2)
        return result

    def _generate(self, inputs: dict[str, Any]) -> ToolResult:
        output_path = Path(inputs.get("output_path", "tts_output.wav"))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        model = inputs.get("model", DEFAULT_VOICE)
        data_dir = _resolve_data_dir(inputs)
        voice_path = _resolve_voice(model, data_dir)
        if voice_path is None:
            return ToolResult(
                success=False,
                error=(
                    f"Piper voice {model!r} not found in {data_dir}. Download it with:\n"
                    f"  {sys.executable} -m piper.download_voices "
                    f"--download-dir {data_dir} {model}\n"
                    "(needs network; export HTTPS_PROXY first if this host requires a proxy)"
                ),
            )

        # Invoke `python -m piper` directly so we always hit the same Python
        # interpreter whose site-packages we just verified import-clean in
        # `get_status()`. Bypasses PATH resolution (pyenv shims, conda
        # activation, /usr/local/bin overrides) which otherwise can yield a
        # different binary that exits 127.
        #
        # Pass the resolved absolute .onnx rather than the voice name: piper's
        # own --data-dir defaults to the current directory, which would make
        # generation succeed or fail depending on the caller's cwd.
        proc = subprocess.run(
            [
                sys.executable, "-m", "piper",
                "--model", str(voice_path),
                "--speaker", str(inputs.get("speaker_id", 0)),
                "--length-scale", str(inputs.get("length_scale", 1.0)),
                "--sentence-silence", str(inputs.get("sentence_silence", 0.3)),
                "--output_file", str(output_path),
            ],
            input=inputs["text"],
            capture_output=True,
            text=True,
            timeout=300,
        )

        if proc.returncode != 0:
            return ToolResult(success=False, error=f"Piper failed (exit {proc.returncode}): {proc.stderr}")
        if not output_path.exists():
            return ToolResult(success=False, error=f"Piper output file missing: {output_path}")

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "model": model,
                "voice_path": str(voice_path),
                "speaker_id": inputs.get("speaker_id", 0),
                "text_length": len(inputs["text"]),
                "output": str(output_path),
                "format": "wav",
            },
            artifacts=[str(output_path)],
            model=model,
        )
