#!/bin/bash
# One generated scene: <in.wav> -> $OUT_DIR/<name>.mp3, denoised (when DENOISE=true) and with the silence around the speech trimmed.
# The scene length is the audio length, so long silent tails would be dead air in the video: keep at most ~0.1 s at both ends.
# The silence is trimmed AFTER the denoise: on clean audio the -50 dB threshold finds the real edges of the speech.
set -euo pipefail
in="$1"
name="$(basename "$in" .wav)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
src="$in"
# The model sometimes keeps generating loud pitchless noise after a sentence; level gates and rnnoise cannot see it, so it is
# muted by its missing pitch BEFORE the denoise and the trim (the trim then removes it from the scene ends).
if [ "${STRIP_NOISE:-true}" = "true" ]; then
  python3 "$(dirname "$0")/voice_strip_noise_blocks.py" "$src" "$tmp/stripped.wav"
  src="$tmp/stripped.wav"
fi
if [ "${DENOISE:-true}" = "true" ]; then
  bash "$(dirname "$0")/voice_denoise.sh" "$src" "$tmp/clean.wav"
  src="$tmp/clean.wav"
fi
TRIM="silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.08"
ffmpeg -hide_banner -loglevel error -y -i "$src" -af "$TRIM,areverse,$TRIM,areverse,alimiter=limit=0.84:level=0" -ar 44100 -ac 1 -codec:a libmp3lame -q:a 2 "${OUT_DIR:-final}/$name.mp3"
