#!/usr/bin/env python3
"""
Mutes the noise the TTS model keeps generating after a sentence is finished.

Chatterbox sometimes emits loud, unvoiced noise (0.4-1.7 s, as loud as speech) at the end of a chunk, so level gates and
rnnoise cannot tell it from the voice. Real speech has a pitch (periodic, 70-400 Hz); that noise has none. Any run of loud
frames without pitch lasting >= MIN_BLOCK_MS is muted (with short fades); the silence trim that follows then removes it
from the ends of the scene.

  python3 scripts/voice_strip_noise_blocks.py in.wav out.wav        # prints how many blocks / seconds were muted
"""
import subprocess
import sys

import numpy as np

SR = 24000
HOP = int(SR * 0.010)          # 10 ms
WIN = int(SR * 0.030)          # 30 ms analysis window
LAG_MIN, LAG_MAX = int(SR / 400), int(SR / 70)
VOICED_AC = 0.45               # normalised autocorrelation peak: speech > ~0.5, noise < ~0.3
LOUD_DB = -45.0
MIN_BLOCK_MS = 300
BRIDGE_FRAMES = 8              # keep gaps of up to 80 ms inside voiced speech
KEEP_TAIL_FRAMES = 6           # keep 60 ms of the previous word's decay


def read_wav(path):
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-af", "highpass=f=70", "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def write_wav(path, x):
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "-", path],
        input=pcm, check=True,
    )


def find_blocks(x):
    n = (len(x) - WIN) // HOP
    if n <= 0:
        return [], n
    lvl = np.full(n, -120.0)
    voi = np.zeros(n)
    for i in range(n):
        f = x[i * HOP:i * HOP + WIN]
        f = f - f.mean()
        e = float((f * f).sum())
        lvl[i] = 10 * np.log10(e / WIN + 1e-12)
        if e < 1e-6:
            continue
        F = np.fft.rfft(f, 2 * WIN)
        ac = np.fft.irfft(F * np.conj(F))[:LAG_MAX + 1]
        voi[i] = ac[LAG_MIN:LAG_MAX + 1].max() / ac[0]
    voiced = (voi > VOICED_AC) & (lvl > -48)
    gap = 0
    for i in range(n):
        if voiced[i]:
            if 0 < gap <= BRIDGE_FRAMES:
                voiced[i - gap:i] = True
            gap = 0
        else:
            gap += 1
    cand, i = [], 0
    while i < n:
        if lvl[i] > LOUD_DB and not voiced[i]:
            j = i
            while j < n and lvl[j] > LOUD_DB and not voiced[j]:
                j += 1
            if j - i >= 12:
                cand.append([i, j])
            i = j
        else:
            i += 1
    merged = []
    for a, b in cand:                                  # pieces split by one stray voiced frame are one block
        if merged and a - merged[-1][1] <= 6:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if (b - a) * 10 >= MIN_BLOCK_MS], n


def main():
    src, dst = sys.argv[1], sys.argv[2]
    x = read_wav(src)
    blocks, _ = find_blocks(x)
    g = np.ones(len(x), dtype=np.float32)
    for a, b in blocks:
        g[(a + KEEP_TAIL_FRAMES) * HOP:b * HOP + WIN // 2] = 0
    fade = np.hanning(int(SR * 0.020))
    g = np.convolve(g, fade / fade.sum(), mode="same")
    write_wav(dst, x * g)
    print(f"{src}: {len(blocks)} noise block(s), {sum(b - a for a, b in blocks) * 0.01:.2f}s muted of {len(x) / SR:.1f}s")


if __name__ == "__main__":
    main()
