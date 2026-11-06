"""
Emotion for a spoken line, applied on the runner (see src/audio/emotion.ts, which tags each line).

  1. Chatterbox expressiveness (exaggeration / cfg_weight / temperature): written to /tmp/vb_emotion.json inside the Voicebox container
     before the line is spoken; scripts/voicebox_emotion_patch.py makes the backend read it (best effort: without the patch the
     numbers are simply ignored).
  2. Prosody of the finished clip with ffmpeg: pitch, pace and loudness. Works for every engine, so the English Turbo voice gets it too.

"neutral" and unknown emotions change nothing.
"""
import json
import os
import subprocess

# emotion -> (pitch semitones, tempo, gain dB)
PROSODY = {
    "surprise": (2.5, 1.05, 2.0),
    "energetic": (1.0, 1.10, 2.5),
    "sad": (-1.5, 0.88, -2.5),
    "serious": (-0.8, 0.96, 0.0),
    "warm": (0.0, 0.97, 0.5),
}
# emotion -> Chatterbox sampling settings (defaults of the backend are exaggeration 0.4-0.5, cfg_weight 0.5-0.7, temperature 0.65-0.8)
CHATTERBOX = {
    "surprise": {"exaggeration": 0.8, "cfg_weight": 0.3, "temperature": 0.9},
    "energetic": {"exaggeration": 0.75, "cfg_weight": 0.35, "temperature": 0.85},
    "sad": {"exaggeration": 0.3, "cfg_weight": 0.6, "temperature": 0.6},
    "serious": {"exaggeration": 0.45, "cfg_weight": 0.6, "temperature": 0.6},
    "warm": {"exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.7},
}


def enabled():
    return os.environ.get("VOICE_EMOTION", "on").strip().lower() not in ("off", "false", "0", "no")


def set_chatterbox(container, emotion):
    """Tell the patched backend how expressive the next line is; no entry = its own defaults. Never raises."""
    try:
        cmd = ["docker", "exec", "-u", "root", container, "sh", "-c"]
        params = CHATTERBOX.get(emotion)
        cmd.append(f"echo '{json.dumps(params)}' > /tmp/vb_emotion.json" if params else "rm -f /tmp/vb_emotion.json")
        subprocess.run(cmd, check=False, timeout=20, capture_output=True)
    except Exception as e:  # noqa: BLE001 - emotion is an extra, never a reason to fail a line
        print(f"emotion: could not set chatterbox params ({e})", flush=True)


def filter_chain(emotion, sample_rate):
    if emotion not in PROSODY:
        return None
    semitones, tempo, gain = PROSODY[emotion]
    p = 2 ** (semitones / 12.0)
    parts = []
    if abs(semitones) > 1e-6:
        parts.append(f"asetrate={sample_rate * p:.0f}")  # raises pitch AND speed ...
        parts.append(f"aresample={sample_rate}")
    parts.append(f"atempo={tempo / p:.4f}")  # ... so the tempo is corrected to land on `tempo` overall
    if abs(gain) > 1e-6:
        parts.append(f"volume={gain}dB")
    parts.append("alimiter=limit=0.95")
    return ",".join(parts)


def apply_prosody(path, emotion):
    """Rewrites the wav at `path` in place. Returns True when it was changed; on any problem the original stays."""
    try:
        rate = int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate", "-of", "csv=p=0", path],
                                  capture_output=True, text=True, timeout=30).stdout.strip() or 0)
        chain = filter_chain(emotion, rate) if rate else None
        if not chain:
            return False
        tmp = path + ".emo.wav"
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-af", chain, tmp], capture_output=True, timeout=120)
        if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 1000:
            print(f"emotion: ffmpeg failed for {emotion}: {r.stderr.decode()[:200]}", flush=True)
            if os.path.exists(tmp):
                os.remove(tmp)
            return False
        os.replace(tmp, path)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"emotion: prosody skipped ({e})", flush=True)
        return False
