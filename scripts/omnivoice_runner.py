"""
Narration in OmniVoice (k2-fsa/OmniVoice): one audio file per line, in the channel's narrator voice.

Runs on Kaggle (GPU, src/audio/omnivoice/kaggleRunner.ts pushes it as a script kernel with the job embedded in JOB_JSON)
and on GitHub Actions (CPU fallback, .github/workflows/omnivoice-scenes.yml: --job <shard.json> --sample <wav>).

The job: {"tag", "language", "speed", "format", "narrator": "<voice design>" | "sample": {"file", "start", "end", "text"},
          "characters": {"<speaker>": "<voice design>"}, "lines": [{"name", "text", "emotion"?, "speaker"?}]}

The narrator is a designed voice (the default) or, with VOICE_CLONE=on, a clone of the channel's recorded sample.
OmniVoice has no emotion switch, so a line's emotion (src/audio/emotion.ts) becomes its pace. A line spoken by a character
(Storiyum dialogue) uses a designed voice: one anchor clip per character, cloned for all of that character's lines so the
voice stays the same from line to line. Writes <name>.mp3 (or .wav) per line and manifest.json (tag + seconds per line).
"""
import argparse
import json
import os
import subprocess
import sys
import time
import zlib

JOB_JSON = None  # kaggleRunner.ts replaces this with the job

EMOTION_SPEED = {"neutral": 1.0, "warm": 0.95, "serious": 0.92, "sad": 0.85, "surprise": 1.05, "energetic": 1.08}
# Hindi is spoken at about 13 characters per second (measured on the Voicebox runs, src/my-video/voiceboxNarration.ts);
# a line that comes out far shorter than that was rushed or cut, and is spoken again with a fixed length
CHARS_PER_SEC = 13.0
MIN_FRACTION = 0.55
# what a character's anchor clip says (only its voice matters, not the words)
ANCHOR_TEXT = "नमस्ते, आज मैं आपको अपनी एक बात बताना चाहता हूँ, ध्यान से सुनिए।"


def log(msg):
    print(f"[omnivoice] {msg}", flush=True)


def on_kaggle():
    return os.path.isdir("/kaggle/working")


def find_file(name, root="/kaggle/input"):
    """A dataset file wherever Kaggle mounted the dataset."""
    for dirpath, _, files in os.walk(root):
        if name in files:
            return os.path.join(dirpath, name)
    raise FileNotFoundError(f"{name} not found under {root} (is the voice-samples dataset attached?)")


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


RNNOISE_URL = "https://raw.githubusercontent.com/GregorR/rnnoise-models/master/beguiling-drafter-2018-08-30/bd.rnnn"
_denoise_chain = None


def denoise_chain(work):
    """scripts/voice_denoise.sh's chain: rumble high-pass, RNNoise (speech-aware), a light spectral denoiser for what is left.
    Without the RNNoise model (no network) only the spectral denoiser runs."""
    global _denoise_chain
    if _denoise_chain is None:
        model = os.path.join(work, "bd.rnnn")
        try:
            if not os.path.exists(model):
                subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", model, RNNOISE_URL], check=True, timeout=60)
            _denoise_chain = f"highpass=f=70,arnndn=m={model},afftdn=nr=12:nf=-55:tn=1"
        except Exception as err:
            log(f"RNNoise model unavailable ({err}): spectral denoise only")
            _denoise_chain = "highpass=f=70,afftdn=nr=18:nf=-50:tn=1"
    return _denoise_chain


_nr = None  # the noisereduce module once imported, False when it is unavailable


def deep_clean(src, out):
    """Stationary spectral gating (noisereduce): learns the steady hiss the clone copied from the voice sample and removes it from the
    whole line, gaps included, which RNNoise alone leaves audible. Returns False when it is not installed or fails; the caller then keeps
    the ffmpeg chain only."""
    global _nr
    if _nr is False:
        return False
    try:
        import numpy as np
        import soundfile as sf
        if _nr is None:
            import noisereduce
            _nr = noisereduce
        a, sr = sf.read(src, dtype="float32")
        a = a if a.ndim == 1 else a.mean(axis=1)
        clean = _nr.reduce_noise(y=a, sr=sr, stationary=True, prop_decrease=0.75)
        sf.write(out, np.clip(clean, -1, 1).astype("float32"), sr)
        return True
    except Exception as err:
        log(f"noisereduce unavailable ({str(err)[:160]}): ffmpeg denoise only")
        _nr = False
        return False


def cut_sample(src, start, end, out, work):
    """The voice sample, cut and cleaned: its background hiss would otherwise be cloned as part of the voice."""
    ffmpeg("-ss", str(start), "-to", str(end), "-i", src, "-vn", "-ac", "1", "-ar", "24000", "-af", denoise_chain(work), out)
    return out


TRIM = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.08"
TARGET_SPEECH_DB = -18.0  # speech level of every line (dBFS RMS over the voiced frames)
PEAK_DB = -1.5
FADE_OUT = 0.08  # seconds


def level_gain(wav):
    """One fixed gain per line: speech to TARGET_SPEECH_DB, peaks under PEAK_DB. A fixed gain (unlike loudnorm's dynamic mode) never
    lifts the quiet gaps between words, so it never makes the remaining hiss louder."""
    import numpy as np
    import soundfile as sf
    a, _ = sf.read(wav, dtype="float32")
    a = a if a.ndim == 1 else a.mean(axis=1)
    n = 1200
    frames = a[: len(a) // n * n].reshape(-1, n) if len(a) >= n else a.reshape(1, -1)
    rms = np.sqrt((frames ** 2).mean(axis=1)) + 1e-9
    voiced = rms[20 * np.log10(rms) > 20 * np.log10(rms.max()) - 25]
    speech_db = 20 * np.log10(np.sqrt((voiced ** 2).mean()))
    peak_db = 20 * np.log10(np.abs(a).max() + 1e-9)
    return min(TARGET_SPEECH_DB - speech_db, PEAK_DB - peak_db)


def finish(wav, out_path, fmt, work, cleanup=True):
    """Each line (when `cleanup`: denoised) with the silence around the speech trimmed (the scene lasts as long as its audio), and
    brought to one speech level."""
    clean = wav.replace(".wav", ".clean.wav")
    # trimmed BEFORE the hiss removal: noisereduce softens word edges, and a trim after it cut real speech (lines came out 25-60% shorter)
    pre = f"{denoise_chain(work)}," if cleanup else ""
    ffmpeg("-i", wav, "-af", f"{pre}{TRIM},areverse,{TRIM},areverse", "-ar", "24000", "-ac", "1", clean)
    deep = wav.replace(".wav", ".nr.wav")
    if cleanup and deep_clean(clean, deep):
        os.replace(deep, clean)
    # a short fade at both ends: the model can stop mid-sound, and a hard stop is a click when the scenes play back to back
    length = duration_of(clean)
    fade = f"afade=t=in:d=0.01,afade=t=out:st={max(0.0, length - FADE_OUT):.3f}:d={FADE_OUT}" if length > 2 * FADE_OUT else ""
    chain = ",".join(x for x in (f"volume={level_gain(clean):.2f}dB", "alimiter=limit=0.84:level=0", fade) if x)
    if fmt == "mp3":
        ffmpeg("-i", clean, "-af", chain, "-ar", "44100", "-ac", "1", "-codec:a", "libmp3lame", "-q:a", "2", out_path)
    else:
        ffmpeg("-i", clean, "-af", chain, "-ar", "24000", "-ac", "1", out_path)


def duration_of(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout.strip()
    try:
        return float(out or 0)
    except ValueError:
        # ffprobe says "N/A" for a WAV written through the areverse trim; the samples themselves know their length
        import soundfile as sf
        return sf.info(path).duration


def line_speed(line, base):
    return round(base * EMOTION_SPEED.get(line.get("emotion") or "neutral", 1.0), 3)


def too_short(seconds, text, speed):
    expected = len(text) / CHARS_PER_SEC / speed
    return expected > 1.0 and seconds < expected * MIN_FRACTION, expected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", help="job JSON file (GitHub); on Kaggle the job is embedded")
    ap.add_argument("--lines", help="a shard of lines (overrides the job's lines)")
    ap.add_argument("--sample", help="the voice sample, already cut (GitHub); on Kaggle it is cut from the dataset")
    ap.add_argument("--out", default="/kaggle/working" if on_kaggle() else "out")
    ap.add_argument("--device", default=None)
    a = ap.parse_args()

    job = JOB_JSON if JOB_JSON is not None else json.load(open(a.job, encoding="utf-8"))
    if a.lines:
        job["lines"] = json.load(open(a.lines, encoding="utf-8"))
    fmt = job.get("format", "mp3")
    base_speed = float(job.get("speed") or 1.0)
    language = job.get("language") or "hi"
    os.makedirs(a.out, exist_ok=True)
    work = "/tmp/omnivoice"
    os.makedirs(work, exist_ok=True)

    if on_kaggle():
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "omnivoice"], check=True)
        # best effort: without it the lines are cleaned by the ffmpeg chain only
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "noisereduce"], check=False)

    import numpy as np
    import soundfile as sf
    import torch
    from omnivoice.models.omnivoice import OmniVoice

    device = a.device or ("cuda" if torch.cuda.is_available() else "cpu")
    t0 = time.time()
    model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map=device, dtype=torch.float16 if device == "cuda" else torch.float32)
    sr = model.sampling_rate
    log(f"model on {device} in {time.time() - t0:.0f}s")

    VALID_INSTRUCTS = {
        "american accent", "australian accent", "british accent", "canadian accent",
        "child", "chinese accent", "elderly", "female", "high pitch", "indian accent",
        "japanese accent", "korean accent", "low pitch", "male", "middle-aged",
        "moderate pitch", "portuguese accent", "russian accent", "teenager",
        "very high pitch", "very low pitch", "whisper", "young adult",
    }

    def clean_instruct(inst):
        if not inst:
            return None
        parts = [p.strip().lower() for p in inst.split(",") if p.strip()]
        valid = [p for p in parts if p in VALID_INSTRUCTS]
        return ", ".join(valid) if valid else "male, young adult, indian accent"

    def speak(text, speed, ref, duration=None, instruct=None):
        kw = dict(text=text, language=language, speed=speed)
        if ref:
            kw.update(ref_audio=ref[0], ref_text=ref[1])
        if instruct:
            cleaned = clean_instruct(instruct)
            if cleaned:
                kw.update(instruct=cleaned)
        if duration:
            kw.update(duration=duration)
        audio = model.generate(**kw)[0]
        audio = audio.cpu().numpy() if torch.is_tensor(audio) else np.asarray(audio)
        return audio.squeeze().astype(np.float32)

    def designed(name, design):
        """A designed voice as an anchor clip, cloned for every line of that voice so it stays the same from line to line. The seed comes
        from the design text, so the same design gives the same voice in every run (a channel keeps its narrator across videos)."""
        torch.manual_seed(zlib.crc32(design.encode("utf-8")))
        anchor = f"{work}/voice_{name}.wav"
        sf.write(anchor, speak(ANCHOR_TEXT, 1.0, None, instruct=design), sr)
        log(f"voice '{name}': {design}")
        return (anchor, ANCHOR_TEXT)

    if job.get("narrator"):
        # the default: a generic designed narrator, clean by construction (no recording, no recording hiss)
        voices = {None: designed("narrator", job["narrator"])}
    else:
        sample = job["sample"]
        if a.sample:
            # GitHub: already cut by the workflow, cleaned here like on Kaggle
            narrator_wav = f"{work}/narrator.wav"
            ffmpeg("-i", a.sample, "-ac", "1", "-ar", "24000", "-af", denoise_chain(work), narrator_wav)
        else:
            narrator_wav = cut_sample(find_file(os.path.basename(sample["file"])), sample.get("start", 0), sample.get("end", 15), f"{work}/narrator.wav", work)
        voices = {None: (narrator_wav, sample.get("text") or None)}
    # the hiss removal is for cloned recordings; a designed voice is clean and only gets trimmed, levelled and faded
    cleanup = not job.get("narrator")

    for speaker, design in (job.get("characters") or {}).items():
        if any(l.get("speaker") == speaker for l in job["lines"]):
            voices[speaker] = designed(speaker, design)
    torch.manual_seed(int(time.time()))  # the lines themselves vary naturally

    manifest = {"tag": job.get("tag"), "device": device, "lines": {}}
    for i, line in enumerate(job["lines"], 1):
        text = line["text"].strip()
        speed = line_speed(line, base_speed)
        ref = voices.get(line.get("speaker")) or voices[None]
        raw = f"{work}/{line['name']}.wav"
        for attempt in (1, 2):
            try:
                audio = speak(text, speed, ref)
                short, expected = too_short(len(audio) / sr, text, speed)
                if short:
                    log(f"{line['name']}: {len(audio) / sr:.1f}s for ~{expected:.1f}s of text, speaking it again at a fixed length")
                    audio = speak(text, speed, ref, duration=round(expected, 2))
                sf.write(raw, audio, sr)
                break
            except Exception as err:  # a single bad generation is retried once; a second failure fails the run
                if attempt == 2:
                    raise
                log(f"{line['name']}: {err}; retrying")
        out_path = os.path.join(a.out, f"{line['name']}.{fmt}")
        finish(raw, out_path, fmt, work, cleanup)
        manifest["lines"][line["name"]] = round(duration_of(out_path), 2)
        log(f"[{i}/{len(job['lines'])}] {line['name']} {manifest['lines'][line['name']]}s ({line.get('emotion') or 'neutral'}, {line.get('speaker') or 'narrator'})")

    with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)
    log(f"done: {len(job['lines'])} lines, {sum(manifest['lines'].values()):.0f}s of audio in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
