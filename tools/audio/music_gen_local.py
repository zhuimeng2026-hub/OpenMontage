"""Local open-source music generation via Meta MusicGen.

Runs entirely on-device (transformers + torch + soundfile). Produces
instrumental-only BGM. Free; no API key. Complements the ElevenLabs
``music_gen`` tool as a last-hop fallback before ``unavailable``.

Honesty contract (mirrors ``piper_tts``):

* :meth:`get_status` returns ``AVAILABLE`` only when transformers/torch/soundfile
  import AND the ``facebook/musicgen-small`` weights are cached locally. It
  never calls ``pipeline()`` — there is no silent first-call download.
* ``force_instrumental=False`` is a hard failure (MusicGen has no vocal path;
  vocal intent must go to ``suno_music``).
* Duration > 30s uses a documented crossfade-loop strategy; no silent
  truncation.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
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


# Default crossfade window for _loop_to_duration. 2.0s matches the
# HyperFrames bgm.md "no per-segment seams" target.
DEFAULT_CROSSFADE_SECONDS = 2.0


def _hf_cache_root() -> Path:
    """Resolve the Hugging Face cache root, honoring ``HF_HOME``.

    Mirrors the convention used by ``kokoro_tts``: respect the override env
    var, otherwise default to ``~/.cache/huggingface``.
    """
    raw = os.environ.get("HF_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cache" / "huggingface"


class MusicGenLocal(BaseTool):
    name = "music_gen_local"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "music_generation"
    provider = "local"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.LOCAL

    install_instructions = (
        "1. pip install 'transformers>=4.40' torch soundfile numpy\n"
        "2. Pre-fetch weights (one-time):\n"
        "     python -c \"from transformers import pipeline; \\\n"
        "       pipeline('text-to-audio', model='facebook/musicgen-small', \\\n"
        "       cache_dir='~/.cache/huggingface')\"\n"
        "3. Optional GPU: pip install accelerate (auto-detected)"
    )

    dependencies = ["transformers", "torch", "soundfile"]
    fallback_tools = ["music_gen"]
    agent_skills = ["music"]

    capabilities = ["generate_background_music"]

    supports = {
        "instrumental": True,
        "vocals": False,
        "custom_lyrics": False,
        "long_form": True,
    }

    best_for = [
        "fully offline BGM with no API key and no cost",
        "instrumental-only BGM (model has no vocal path; mandate is automatic)",
        "budget-bound or vendor-risk-bound long-form pipelines",
    ]
    not_good_for = [
        "sound effects (use music_gen or freesound_music)",
        "vocal tracks (use suno_music)",
        "strict quality bar - ElevenLabs/Lyria still win on quality",
    ]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Music description (mood, genre, instruments, tempo). "
                    "Vocal/Lyrics instructions are ignored - MusicGen is "
                    "instrumental-only."
                ),
            },
            "duration_seconds": {
                "type": "number",
                "minimum": 5,
                "maximum": 30,
                "default": 10,
                "description": (
                    "MusicGen single-pass duration (decoder positional limit). "
                    "For longer BGM, set loop_to_duration_seconds instead."
                ),
            },
            "loop_to_duration_seconds": {
                "type": "number",
                "minimum": 5,
                "description": (
                    "If set, the generated seed clip is crossfade-looped (see "
                    "RFC sec 4.3) up to this total duration. Omit when you want "
                    "the raw single-pass clip. Mutually exclusive with "
                    "duration_seconds - duration_seconds wins if both are set."
                ),
            },
            "model_variant": {
                "type": "string",
                "enum": ["small", "medium", "large", "melody"],
                "default": "small",
                "description": (
                    "MusicGen size. small = 300M, CPU-friendly. medium/large "
                    "require 6+/16+ GB VRAM. melody adds reference-audio "
                    "conditioning (not wired in this RFC)."
                ),
            },
            "force_instrumental": {
                "type": "boolean",
                "default": True,
                "description": (
                    "Mirrors music_gen's API. MusicGen is instrumental-only by "
                    "construction; this field is preserved for schema symmetry "
                    "and is honored by failing the call (not by ignoring it) if "
                    "set to false - explicit vocal intent must use suno_music."
                ),
            },
            "output_path": {
                "type": "string",
                "default": "music_output.wav",
                "description": (
                    "Output file path. If it ends in .mp3, the WAV is transcoded "
                    "with FFmpeg (libmp3lame, -q:a 2)."
                ),
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2, ram_mb=4096, vram_mb=2048, disk_mb=400, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["timeout"])
    idempotency_key_fields = ["prompt", "duration_seconds", "model_variant"]
    side_effects = ["writes audio file to output_path"]
    user_visible_verification = [
        "Listen to generated music for mood and seam audibility (if looped)",
    ]

    # Per-instance caches (RFC §4.5).
    _device_cached: str | None = None
    _pipeline = None

    # ------------------------------------------------------------------
    # Status / device
    # ------------------------------------------------------------------

    def _device(self) -> str:
        """Pick best available device. Cached on the instance.

        Order: cuda -> mps -> cpu. Cached because the check is non-trivial
        and the result cannot change for a given process.
        """
        if self._device_cached is not None:
            return self._device_cached
        import torch

        if torch.cuda.is_available():
            self._device_cached = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            self._device_cached = "mps"
        else:
            self._device_cached = "cpu"
        return self._device_cached

    def get_status(self) -> ToolStatus:
        """Three honest gates, in order.

        (a) transformers/torch/soundfile must be importable,
        (b) the ``facebook/musicgen-small`` snapshot must exist in the
            Hugging Face cache (respecting ``HF_HOME``),
        (c) return AVAILABLE.

        Never calls ``pipeline()`` - no silent first-call download.
        """
        try:
            import transformers  # noqa: F401
            import torch  # noqa: F401
            import soundfile  # noqa: F401
        except ImportError as e:
            return ToolStatus(
                status="unavailable",
                reason=f"missing dependency: {e}",
                install_instructions=self.install_instructions,
            )

        try:
            cache = _hf_cache_root()
            marker = cache / "hub" / "models--facebook--musicgen-small"
            if not marker.exists():
                return ToolStatus(
                    status="unavailable",
                    reason=(
                        "weights not cached; run the bootstrap in "
                        "install_instructions"
                    ),
                    install_instructions=self.install_instructions,
                )
        except Exception as e:
            return ToolStatus(status="unavailable", reason=str(e))

        return ToolStatus.AVAILABLE

    # ------------------------------------------------------------------
    # Cost / runtime estimation
    # ------------------------------------------------------------------

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # Free, always. cost_tracker and budget warn need a non-None float;
        # 0.0 is the truthful answer.
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        variant = inputs.get("model_variant", "small")
        duration = inputs.get("duration_seconds", 10)
        device = self._device() if self.get_status() == ToolStatus.AVAILABLE else "cpu"
        # Heuristic, not a promise. CPU small is ~duration * 2-3x realtime.
        # MPS/CUDA small is ~duration * 0.3-0.5x realtime. medium/large are
        # not benchmarked here; the small heuristic is still a usable upper
        # bound for warning purposes.
        if device == "cpu":
            return float(duration * 3)
        return float(duration * 0.5)

    # ------------------------------------------------------------------
    # Core: loop, write, execute
    # ------------------------------------------------------------------

    def _loop_to_duration(
        self,
        seed_wav: "np.ndarray",
        sample_rate: int,
        target_seconds: float,
        *,
        crossfade_s: float = DEFAULT_CROSSFADE_SECONDS,
    ) -> "np.ndarray":
        """Crossfade-loop a seed clip to ``target_seconds`` (RFC §4.3).

        - ``crossfade_s``: how much each successive copy overlaps the previous.
          2.0s matches the bgm.md "no per-segment seams" target.
        - If seed is longer than target: hard trim (no fade-out).
        - If seed is exactly target: identity.
        - If seed is shorter than ``crossfade_s``: raise ``ValueError``; do not
          silently produce a degenerate overlap.
        """
        import numpy as np

        target_samples = int(target_seconds * sample_rate)
        crossfade_samples = int(crossfade_s * sample_rate)

        if seed_wav.shape[0] >= target_samples:
            return seed_wav[:target_samples]

        if seed_wav.shape[0] <= crossfade_samples:
            raise ValueError(
                f"Seed clip ({seed_wav.shape[0] / sample_rate:.1f}s) must be "
                f"longer than crossfade ({crossfade_s}s); raise duration_seconds."
            )

        out = seed_wav.copy()
        while out.shape[0] < target_samples:
            remaining = target_samples - out.shape[0]
            take = min(seed_wav.shape[0], remaining)
            next_chunk = seed_wav[:take]
            # Tail-termination guard (RFC §4.3 patch, 2026-08-28): when
            # ``take <= crossfade_samples`` the previous algorithm attempted
            # full crossfade math with an empty ``next_chunk[crossfade_samples:]``
            # tail AND replaced ``out[:-crossfade_samples]`` with the same
            # length as ``overlap`` — net zero progress, infinite loop. Append
            # the partial chunk directly instead.
            if take <= crossfade_samples:
                out = np.concatenate([out, next_chunk])
                continue
            fade_in = np.linspace(0.0, 1.0, crossfade_samples, dtype=seed_wav.dtype)
            fade_out = np.linspace(1.0, 0.0, crossfade_samples, dtype=seed_wav.dtype)
            overlap = out[-crossfade_samples:].copy()
            out = np.concatenate(
                [
                    out[:-crossfade_samples],
                    overlap * fade_out + next_chunk[:crossfade_samples] * fade_in,
                    next_chunk[crossfade_samples:],
                ]
            )
        return out[:target_samples]

    def _write_output(
        self,
        wav: "np.ndarray",
        sr: int,
        output_path: Path,
    ) -> None:
        """Write ``wav`` to ``output_path``.

        Default: WAV via soundfile. If ``output_path`` ends in ``.mp3``:
        write WAV to a tempfile, transcode via ffmpeg
        (``ffmpeg -y -loglevel error -i tmp.wav -codec:a libmp3lame -q:a 2
        output.mp3``), delete tmp. Uses ``subprocess.run(check=True)``.
        """
        import soundfile as sf

        output_path.parent.mkdir(parents=True, exist_ok=True)

        if output_path.suffix.lower() == ".mp3":
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                sf.write(tmp_path, wav, sr)
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-loglevel",
                        "error",
                        "-i",
                        str(tmp_path),
                        "-codec:a",
                        "libmp3lame",
                        "-q:a",
                        "2",
                        str(output_path),
                    ],
                    check=True,
                )
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            sf.write(output_path, wav, sr)

    def _ensure_pipeline(self, model_variant: str) -> Any:
        """Instantiate the transformers pipeline once and cache on the instance."""
        if self._pipeline is not None:
            return self._pipeline

        from transformers import pipeline

        variant_to_repo = {
            "small": "facebook/musicgen-small",
            "medium": "facebook/musicgen-medium",
            "large": "facebook/musicgen-large",
            "melody": "facebook/musicgen-melody",
        }
        repo = variant_to_repo.get(model_variant, variant_to_repo["small"])
        device = self._device()
        # transformers uses 0/1/2/... -1 for cpu; torch device objects work too.
        device_arg: Any
        if device == "cpu":
            device_arg = -1
        else:
            import torch

            device_arg = 0 if device == "cuda" else "mps"
            # torch device objects are accepted by transformers pipelines.
            try:
                device_arg = torch.device(device)
            except Exception:
                pass

        self._pipeline = pipeline(
            "text-to-audio",
            model=repo,
            device=device_arg,
        )
        return self._pipeline

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        # 1. force_instrumental hard fail (RFC §4.4).
        if inputs.get("force_instrumental", True) is False:
            return ToolResult(
                success=False,
                error=(
                    "music_gen_local: force_instrumental=False is not supported. "
                    "MusicGen is instrumental-only by construction. For vocal "
                    "tracks, use suno_music."
                ),
            )

        # 2. Runtime availability (three honest gates, RFC §4.5).
        status = self.get_status()
        if status != ToolStatus.AVAILABLE:
            return ToolResult(
                success=False,
                error=f"music_gen_local not available: {status.reason}. "
                + self.install_instructions,
            )

        # 3. Normalize inputs.
        prompt = inputs["prompt"]
        duration_seconds = float(inputs.get("duration_seconds", 10))
        # Clamp to the schema's hard ceiling before generation. The schema
        # already enforces this, but defensive-clamp keeps a misbehaving
        # caller from blowing the decoder's positional limit.
        if duration_seconds > 30:
            duration_seconds = 30.0
        if duration_seconds < 5:
            duration_seconds = 5.0

        loop_to = inputs.get("loop_to_duration_seconds")
        if loop_to is not None:
            # RFC §4.2: "duration_seconds wins if both are set." We honor that
            # by ignoring loop_to when duration_seconds was explicitly passed
            # to a value other than the schema default of 10. A caller who
            # wants loop-to behavior passes loop_to and leaves duration_seconds
            # alone.
            passed_duration = "duration_seconds" in inputs
            schema_default_duration = 10
            if passed_duration and duration_seconds != schema_default_duration:
                loop_to = None
            else:
                if float(loop_to) < 5:
                    return ToolResult(
                        success=False,
                        error="loop_to_duration_seconds must be >= 5.",
                    )
                # The seed clip we generate stays at duration_seconds; the
                # loop call stretches it up to loop_to.
                loop_to = float(loop_to)

        model_variant = inputs.get("model_variant", "small")
        output_path = Path(inputs.get("output_path", "music_output.wav"))

        start = time.time()
        try:
            pipe = self._ensure_pipeline(model_variant)
            # transformers text-to-audio pipelines accept a list of prompts;
            # we always pass exactly one. ``generate_kwargs`` carries the
            # length in seconds the decoder should target.
            result = pipe(
                [prompt],
                generate_kwargs={"duration_seconds": duration_seconds},
            )
            # Pipeline returns a list of dicts with 'audio' (np.ndarray) and
            # 'sampling_rate' (int).
            entry = result[0] if isinstance(result, list) else result
            wav = entry["audio"]
            sr = int(entry["sampling_rate"])

            if loop_to is not None and wav.shape[0] / sr < loop_to:
                wav = self._loop_to_duration(wav, sr, loop_to)

            self._write_output(wav, sr, output_path)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"MusicGen local generation failed: {exc}",
            )

        duration_seconds = round(time.time() - start, 2)
        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "model_variant": model_variant,
                "prompt": prompt,
                "duration_seconds": float(inputs.get("duration_seconds", 10)),
                "loop_to_duration_seconds": loop_to,
                "device": self._device(),
                "output": str(output_path),
                "format": output_path.suffix.lstrip("."),
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=duration_seconds,
            model=f"facebook/musicgen-{model_variant}",
        )
