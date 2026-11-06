#!/bin/bash
# Removes background hiss ("air") from speech.
#
# Why: the recorded voice sample (assets/verdict-clips/codeorcap_verdict_bg.mp4) has a noise floor only ~8 dB under the speech
# (-27.7 dB vs -19.3 dB). The cloned voice learns that hiss as part of the voice and repeats it in about a third of the
# generated scenes (measured on the 211 scenes of lecture #15: 13% had a noise floor above -40 dB). So the sample is cleaned
# before cloning, and every generated scene is cleaned again.
#
# Chain: high-pass at 70 Hz (rumble), RNNoise (speech-aware, takes ~25 dB off), a light spectral denoiser for what is left.
# Measured on the noisiest scenes: noise floor -30 dB -> -70 dB, speech level unchanged within 0.4 dB.
#
#   scripts/voice_denoise.sh in.wav out.wav
# The RNNoise model (bd.rnnn, from github.com/GregorR/rnnoise-models) is fetched once into $MODEL (default ./bd.rnnn).
set -euo pipefail
in="$1"
out="$2"
MODEL="${MODEL:-bd.rnnn}"
if [ ! -s "$MODEL" ]; then
  curl -fsSL --retry 3 -o "$MODEL" "https://raw.githubusercontent.com/GregorR/rnnoise-models/master/beguiling-drafter-2018-08-30/bd.rnnn"
fi
ffmpeg -hide_banner -loglevel error -y -i "$in" -af "highpass=f=70,arnndn=m=$MODEL,afftdn=nr=12:nf=-55:tn=1" "$out"
