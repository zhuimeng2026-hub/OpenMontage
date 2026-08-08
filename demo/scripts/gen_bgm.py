#!/usr/bin/env python3
"""
Generate a royalty-free, gentle ambient BGM bed for the luggage promo.
Pure-python (no 3rd-party deps) -> 16-bit PCM WAV, 44.1kHz, stereo.
Chord pad with slow "breathing" LFO, added harmonics (warmer/more
present on small speakers) and a global fade-in/out.

IMPORTANT: output is auto-normalized to TARGET_PEAK so the mix is
actually audible. (Previous version used a 0.16 master gain that made
the bed nearly inaudible.)
Output: public/bgm.wav  (21s, matches the 630-frame / 30fps video)
"""
import math
import struct
import wave
import os

SR = 44100
DUR = 21.0
N = int(SR * DUR)
TARGET_PEAK = 0.9  # normalized peak after master gain

# Calm, non-copyrightable chord progression (Hz). Am - G - F vibe.
chords = [
    [220.00, 277.18, 329.63],  # A3  C#4 E4
    [196.00, 246.94, 293.66],  # G3  B3  D4
    [174.61, 220.00, 261.63],  # F3  A3  C4
]
seg = DUR / len(chords)


def seg_amp(local, seg_dur, fade=1.0):
    """Per-segment envelope; floor 0.5 so chords never fully drop."""
    a = min(1.0, local / fade)
    b = min(1.0, (seg_dur - local) / fade)
    return 0.5 + 0.5 * (a * b)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "..", "public")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "bgm.wav")

    # --- pass 1: build raw mono samples (pre-normalize) ---
    raw = [0.0] * N
    for i in range(N):
        t = i / SR
        seg_idx = min(len(chords) - 1, int(t // seg))
        local = t - seg_idx * seg
        freqs = chords[seg_idx]
        # slow breathing amplitude modulation
        lfo = 0.82 + 0.18 * math.sin(2 * math.pi * 0.13 * t)
        amp = seg_amp(local, seg, fade=1.0) * lfo

        s = 0.0
        for f in freqs:
            # fundamental + 2nd/3rd harmonics for warmth and presence
            s += 1.0 * math.sin(2 * math.pi * f * t)
            s += 0.50 * math.sin(2 * math.pi * 2 * f * t)
            s += 0.22 * math.sin(2 * math.pi * 3 * f * t)
        s /= len(freqs)
        s = math.tanh(s * 1.3)  # soft clip (warm, no harsh digital peaks)

        # overall fade in / out
        fade_in = min(1.0, t / 1.0)
        fade_out = min(1.0, (DUR - t) / 1.5)
        env = fade_in * fade_out
        raw[i] = s * amp * env

    # --- auto-normalize to TARGET_PEAK (fixes the inaudible mix) ---
    peak = max(abs(x) for x in raw) or 1.0
    gain = TARGET_PEAK / peak

    # --- pass 2: render stereo with a touch of width ---
    data = bytearray()
    for i in range(N):
        t = i / SR
        v = raw[i] * gain
        v_r = v + 0.04 * math.sin(2 * math.pi * 0.30 * t)  # slight decorrelation
        L = max(-0.999, min(0.999, v))
        R = max(-0.999, min(0.999, v_r))
        data += struct.pack("<hh", int(L * 32767), int(R * 32767))

    with wave.open(out_path, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(data))
    print(
        f"wrote {out_path} ({len(data)} bytes, {DUR}s) "
        f"peak={peak:.3f} gain={gain:.2f} -> normalized peak {TARGET_PEAK}"
    )


if __name__ == "__main__":
    main()
