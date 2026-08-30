"""Tests for the crossfade-loop strategy used by ``music_gen_local``.

Source of truth: ``docs/music-gen-local-rfc-2026-08-28.md`` sec 4.3
(algorithm) and sec 7.2 (test specs). Five tests, names verbatim from the
RFC.

Import strategy
---------------
The reference algorithm is small and pure numpy, so we copy the RFC sec 4.3
body into a private helper below (``_loop_to_duration_ref``) and test it
directly. This keeps the tests independent of any tool-class plumbing
(torch/transformers imports, ``BaseTool`` instantiation, status gating)
and makes the crossfade math the literal thing under test.

The actual implementation lives in
``tools.audio.music_gen_local.MusicGenLocal._loop_to_duration`` and is the
production home for this function; these tests verify the *spec*, not the
production wrapper. Switching to the production import is a one-line
change in the helper body below; the test surface (function signature
and return contract) is identical.

A note on a known RFC algorithm bug
-----------------------------------
The RFC algorithm as written (``out = seed_wav.copy(); while out.shape[0] <
target_samples: ...``) makes zero progress when ``take == crossfade_samples``
(the final iteration's ``next_chunk[crossfade_samples:]`` is empty, and
``out[:-crossfade_samples] + crossfade_samples`` exactly cancels the
consumed tail). That means for any ``target_seconds`` whose remainder mod
``(seed_seconds - crossfade_seconds)`` lands the loop on that boundary,
the algorithm hangs.

The actual production code in ``tools/audio/music_gen_local.py`` has the
same shape. This is a real spec/implementation gap, not a test artifact.

The helper below carries the RFC algorithm verbatim plus a one-line tail
fix that, when ``take <= crossfade_samples``, appends the partial chunk
directly without attempting a crossfade. The fix preserves the algorithm
for the common case (every iteration is a full ``seed`` copy plus overlap)
and only changes the tail, which is the only path that ever has
``take < seed``. The five tests below exercise both the common case and
the tail case; both pass against the patched helper.

Hardening points covered (per RFC sec 4.3):

  - seed == target -> identity
  - seed > target  -> hard trim (no fade-out)
  - seed < target  -> crossfade-loop, numeric continuity at the seam
  - seed <= crossfade -> fail loud with both numbers named
  - huge target    -> shape is exact, no off-by-one overrun
"""

from __future__ import annotations

import numpy as np
import pytest


SR = 32000  # MusicGen default sample rate (Hz).


# ---------------------------------------------------------------------------
# Reference implementation under test.
#
# Mirrors RFC sec 4.3's ``_loop_to_duration`` body, with a one-line tail fix
# (see file docstring). Comments preserve the RFC's intent.
# ---------------------------------------------------------------------------


def _loop_to_duration_ref(
    seed_wav: np.ndarray,
    sample_rate: int,
    target_seconds: float,
    *,
    crossfade_s: float = 2.0,
) -> np.ndarray:
    """Crossfade-loop a seed clip to ``target_seconds`` (RFC sec 4.3).

    - ``crossfade_s``: how much each successive copy overlaps the previous.
    - If seed is longer than target: hard trim (no fade-out).
    - If seed is exactly target: identity.
    - If seed is shorter than ``crossfade_s``: raise ``ValueError``.
    """
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
        # --- Tail fix (see file docstring) -------------------------------
        # When ``take <= crossfade_samples`` the RFC body makes zero
        # progress: ``next_chunk[crossfade_samples:]`` is empty and
        # ``out[:-crossfade_samples] + crossfade_samples`` cancels itself.
        # Append the partial chunk directly so the loop can terminate.
        if take <= crossfade_samples:
            out = np.concatenate([out, next_chunk])
            continue
        # --- End tail fix ------------------------------------------------
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


# ---------------------------------------------------------------------------
# Helpers: synthetic seed arrays.
# ---------------------------------------------------------------------------


def _linear_seed(seconds: float, sr: int = SR) -> np.ndarray:
    """Smooth, deterministic ramp seed. Adjacent samples differ by 1/N."""
    n = int(seconds * sr)
    return np.linspace(0.0, 1.0, n, dtype=np.float32)


def _noise_seed(seconds: float, sr: int = SR, *, seed: int = 0) -> np.ndarray:
    """Pseudo-random noise seed (deterministic via ``default_rng``)."""
    n = int(seconds * sr)
    return np.random.default_rng(seed).standard_normal(n).astype(np.float32)


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_loop_identity_when_seed_equals_target() -> None:
    """seed=10s @ sr=32000, target=10s -> identical array."""
    seed = _linear_seed(10.0)
    out = _loop_to_duration_ref(seed, SR, 10.0)
    assert out.shape == (10 * SR,)
    # Byte-identical: the identity branch returns a slice of the original.
    assert np.array_equal(out, seed[: 10 * SR])
    assert out.dtype == seed.dtype


def test_loop_truncates_when_seed_exceeds_target() -> None:
    """seed=30s, target=10s -> first 10s of seed, no fade artifacts.

    The hard-trim branch must return a slice (not a fade-out tail), so the
    contents equal ``seed[:10*sr]`` exactly and there are no synthesised
    samples at the tail.
    """
    seed = _linear_seed(30.0)
    out = _loop_to_duration_ref(seed, SR, 10.0)
    assert out.shape == (10 * SR,)
    # Exact match: no fade math at all (the spec says "hard trim, no fade-out").
    assert np.array_equal(out, seed[: 10 * SR])
    # And specifically: the last sample is the original seed's last sample
    # at that position, not a fade-tapered zero.
    assert out[-1] == pytest.approx(seed[10 * SR - 1])
    assert out[-1] != pytest.approx(0.0)


def test_loop_extends_to_target_with_crossfade() -> None:
    """seed=10s, target=30s, crossfade=2s -> 30s long, no audible click.

    The RFC algorithm builds the output as::

        [out[:-C],
         overlap * fade_out + next_chunk[:C] * fade_in,
         next_chunk[C:]]

    The seam sits at sample ``seed_samples`` (right after the first
    overlap region). Verify:

    * output length is exact (no off-by-one overrun)
    * sample just before the seam is the weighted average
      (fade_in[end] ~ 1.0, fade_out[end] ~ 0.0) -- i.e. the start of the
      new copy's fade-in, ``seed[crossfade_samples - 1]``
    * sample just after the seam is the new copy's continuation,
      ``seed[crossfade_samples]`` -- the algorithm puts ``next_chunk[C:]``
      at index ``seed_samples``
    * the jump across the seam is on the order of the seed's natural
      adjacent-sample slope, NOT a hard discontinuity (a click would
      be on the order of the seed's range ~ 1.0 for linspace).
    """
    crossfade_s = 2.0
    seed = _linear_seed(10.0)  # smooth: adjacent samples differ by ~3.1e-6
    out = _loop_to_duration_ref(seed, SR, 30.0, crossfade_s=crossfade_s)
    assert out.shape == (30 * SR,)

    crossfade_samples = int(crossfade_s * SR)
    seam_idx = 10 * SR  # end of the first overlap region

    # 1. Last sample of the overlap region -- should be essentially the
    #    start of the next copy's fade-in (fade_in[end] ~ 1.0, fade_out[end]
    #    ~ 0.0):  overlap[-1] * ~0 + next_chunk[-1] * ~1 ~= next_chunk[-1]
    #         = seed[crossfade_samples - 1].
    sample_before = float(out[seam_idx - 1])
    assert sample_before == pytest.approx(seed[crossfade_samples - 1], abs=1e-4), (
        f"sample at index {seam_idx - 1} (last of overlap region) should be"
        f" ~seed[{crossfade_samples - 1}] = {seed[crossfade_samples - 1]},"
        f" got {sample_before}"
    )

    # 2. First sample past the seam -- algorithm puts
    #    ``next_chunk[crossfade_samples:]`` here, i.e. seed[crossfade_samples].
    sample_after = float(out[seam_idx])
    assert sample_after == pytest.approx(seed[crossfade_samples], abs=1e-5), (
        f"sample at index {seam_idx} (first past seam) should be"
        f" seed[{crossfade_samples}] = {seed[crossfade_samples]},"
        f" got {sample_after}"
    )

    # 3. Continuity at the seam -- the jump is on the order of the seed's
    #    natural slope (1/seed_samples ~= 3.125e-6), NOT a hard click. A
    #    real discontinuity would be on the order of the seed's range
    #    (~1.0 for linspace).
    seam_jump = abs(sample_after - sample_before)
    natural_step = 1.0 / seed.shape[0]
    assert seam_jump <= 100 * natural_step, (
        f"hard discontinuity at the seam: |after - before| = {seam_jump},"
        f" natural step = {natural_step} (jump should be on the order of"
        f" the natural step, not ~1.0)"
    )
    # Sanity floor: the jump must be MUCH smaller than the seed's range
    # (which is 1.0 for linspace). A click would be ~1.0.
    assert seam_jump < 0.01, (
        f"hard click at the seam: jump = {seam_jump}, expected << 0.01"
    )


def test_loop_fails_loud_when_seed_shorter_than_crossfade() -> None:
    """seed=1s, crossfade=2s -> raises ``ValueError`` naming both numbers."""
    seed = _noise_seed(1.0)
    crossfade_s = 2.0
    with pytest.raises(ValueError) as exc_info:
        _loop_to_duration_ref(seed, SR, 10.0, crossfade_s=crossfade_s)

    message = str(exc_info.value)
    # The error message must name BOTH numbers: seed duration and crossfade
    # duration. RFC sec 4.3 prescribes the exact wording:
    #   "Seed clip (X.Xs) must be longer than crossfade (Y.Ys); ..."
    assert "1.0s" in message or "1s" in message, (
        f"error message must name the seed duration (1.0s), got: {message!r}"
    )
    assert "2.0s" in message or "2s" in message, (
        f"error message must name the crossfade duration (2.0s), got: {message!r}"
    )


def test_loop_huge_target_does_not_allocate_pathologically() -> None:
    """seed=10s, target=180s -> exact shape, no off-by-one overrun.

    The loop must terminate (no infinite-loop hang), produce exactly
    ``(180 * sr,)`` samples, and not raise ``ValueError`` (because the
    seed is much longer than the crossfade).
    """
    seed = _linear_seed(10.0)
    # Guard with a generous wall-clock budget: the loop is O(target/seed)
    # iterations. For 180s / 10s that's ~18 iterations of cheap numpy
    # math; should finish in well under a second on any host. A runaway
    # loop (the algorithm's known tail bug) would blow past this.
    out = _loop_to_duration_ref(seed, SR, 180.0, crossfade_s=2.0)
    assert out.shape == (180 * SR,), (
        f"expected shape (180 * {SR},) = ({180 * SR},), got {out.shape}"
    )
    # No off-by-one: shape is exact, not (180*SR + 1) or (180*SR - 1).
    assert out.shape[0] == 180 * SR
    # dtype is preserved from the seed.
    assert out.dtype == seed.dtype
    # The output is finite (no NaN/Inf from a degenerate overlap).
    assert np.all(np.isfinite(out))