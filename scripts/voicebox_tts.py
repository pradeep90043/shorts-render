"""
Drives a running Voicebox container (see .github/workflows/voicebox-tts.yml): clones a voice from one sample, then speaks each line.

  python3 scripts/voicebox_tts.py --base http://127.0.0.1:17493 --sample sample.wav --sample-text "..." \
      --lines lines.json --out out --engine chatterbox --voice codeorcap

lines.json is [{"name": "codeorcap_6", "text": "Six."}, ...]; each line becomes out/<name>.wav.
"""
import argparse
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voicebox_emotion as emo


def wait_health(base, seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if requests.get(f"{base}/health", timeout=5).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(3)
    sys.exit(f"Voicebox did not become healthy within {seconds}s")


def model_entries(base):
    body = requests.get(f"{base}/models/status", timeout=30).json()
    return body.get("models", body) if isinstance(body, dict) else body


def engine_matches(engine, model_name):
    """Weights of exactly this engine. "chatterbox" used to match chatterbox-turbo too: the Hindi voice downloaded (and the runners
    cached and restored) both models, 7.3 GB, when it needs only the multilingual one."""
    want = engine.replace("_", "-")
    name = model_name.replace("_", "-")
    if want == "chatterbox":
        return name.startswith("chatterbox") and "turbo" not in name
    return want in name


def ensure_model(base, engine, wait_seconds):
    """Download the weights of `engine` (all its sizes the API reports) and wait until they are cached."""
    entries = [m for m in model_entries(base) if engine_matches(engine, m["model_name"])]
    print("models for engine:", [(m["model_name"], m.get("downloaded")) for m in entries], flush=True)
    if not entries:
        print("all models:", [m["model_name"] for m in model_entries(base)], flush=True)
        sys.exit(f"No model matches engine '{engine}'")
    todo = [m["model_name"] for m in entries if not m.get("downloaded")]
    for name in todo:
        r = requests.post(f"{base}/models/download", json={"model_name": name}, timeout=60)
        print("download", name, r.status_code, r.text[:200], flush=True)
    end = time.time() + wait_seconds
    while todo and time.time() < end:
        time.sleep(10)
        have = {m["model_name"]: m for m in model_entries(base)}
        todo = [n for n in todo if not have.get(n, {}).get("downloaded")]
        print("still downloading:", todo, flush=True)
    if todo:
        sys.exit(f"Models not downloaded in time: {todo}")


def transcribe(base, path, language, model, wait_seconds):
    """Voicebox's own speech recognition (Whisper): what is said in the voice sample. The first call downloads the model."""
    end = time.time() + min(wait_seconds, 240)  # a transcription that is not ready in 4 minutes is not coming: fail the shard fast
    while True:
        with open(path, "rb") as f:
            r = requests.post(
                f"{base}/transcribe",
                files={"file": (os.path.basename(path), f, "audio/wav")},
                data={**({"language": language} if language else {}), "model": model},
                timeout=600,
            )
        # 200 with the text is the answer; while the Whisper model downloads Voicebox answers 202 "being downloaded, try again"
        # (also a 2xx), so success means: the body really has the text
        if r.status_code == 200:
            try:
                body = r.json()
            except ValueError:
                body = {}
            if isinstance(body, dict) and isinstance(body.get("text"), str):
                return body["text"].strip()
        if time.time() > end:
            sys.exit(f"transcription failed: HTTP {r.status_code} {r.text[:300]}")
        print(f"transcription not ready yet (HTTP {r.status_code}): {r.text[:160]}", flush=True)
        time.sleep(20)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:17493")
    ap.add_argument("--sample", required=True)
    ap.add_argument("--sample-text", required=True, help='exactly what the sample says, or "auto" to transcribe it with Voicebox (Whisper)')
    ap.add_argument("--sample-language", default="", help="language of the sample for the transcription (hi, en, ...); empty = detect")
    ap.add_argument("--whisper-model", default="small", help="Whisper size used for --sample-text auto (base, small, medium, large, turbo)")
    ap.add_argument("--lines", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine", default="chatterbox")
    ap.add_argument("--voice", default="cloned-voice")
    ap.add_argument("--language", default="en")
    ap.add_argument("--speed", type=float, default=1.0, help="speaking speed, 0.25-4.0 (pitch-preserving time stretch); below 1 is slower")
    ap.add_argument("--startup-seconds", type=int, default=240)
    ap.add_argument("--download-seconds", type=int, default=1500)
    a = ap.parse_args()

    lines = json.load(open(a.lines))
    os.makedirs(a.out, exist_ok=True)
    wait_health(a.base, a.startup_seconds)
    ensure_model(a.base, a.engine, a.download_seconds)

    sample_text = a.sample_text
    if sample_text.strip().lower() == "auto":
        sample_text = transcribe(a.base, a.sample, a.sample_language or None, a.whisper_model, a.download_seconds)
        print("sample says:", sample_text, flush=True)
        if len(sample_text) < 8:
            sys.exit("the sample's transcription is almost empty: is there speech in it?")

    r = requests.post(
        f"{a.base}/profiles",
        json={"name": a.voice, "language": a.language, "voice_type": "cloned", "default_engine": a.engine},
        timeout=60,
    )
    r.raise_for_status()
    profile_id = r.json()["id"]
    with open(a.sample, "rb") as f:
        r = requests.post(
            f"{a.base}/profiles/{profile_id}/samples",
            files={"file": (os.path.basename(a.sample), f, "audio/wav")},
            data={"reference_text": sample_text},
            timeout=300,
        )
    r.raise_for_status()
    print("profile", profile_id, "ready", flush=True)

    for line in lines:
        t0 = time.time()
        if len(line["text"]) > 3000:
            # A runaway text (a Short's line is ~150 characters, a video scene under ~600) would keep this runner busy for the full 30
            # minutes and delay the whole batch: stop at once so the app falls back instead of waiting out its timeout.
            sys.exit(f"{line['name']}: {len(line['text'])} characters is not a narration line, refusing to speak it")
        emotion = line.get("emotion") if emo.enabled() else None
        if a.engine == "chatterbox":
            emo.set_chatterbox("voicebox", emotion)  # also clears the previous line's setting when this one is neutral
        for attempt in range(1, 4):
            r = requests.post(
                f"{a.base}/v1/audio/speech",
                json={"model": a.engine, "voice": a.voice, "input": line["text"], "response_format": "wav", **({"speed": a.speed} if a.speed != 1.0 else {})},
                timeout=max(240, min(900, 90 + 3 * len(line["text"]))),  # ~3 s per character: a stuck line is detected in minutes, not 15
            )
            if r.ok:
                break
            print(f"{line['name']}: attempt {attempt} HTTP {r.status_code} {r.text[:400]}", flush=True)
            if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
                break  # the request itself is invalid (e.g. text over 10,000 characters): asking again cannot help; it cost 4.5 minutes
            # The chatterbox download can end with a progress-bar error in the library although the files are complete.
            # Asking again finds them cached, so the model loads without downloading.
            for m in model_entries(a.base):
                if engine_matches(a.engine, m["model_name"]):
                    requests.post(f"{a.base}/models/download", json={"model_name": m["model_name"]}, timeout=60)
            time.sleep(45 * attempt)
        if not r.ok:
            sys.exit(f"{line['name']}: HTTP {r.status_code} {r.text[:400]}")
        path = os.path.join(a.out, f"{line['name']}.wav")
        with open(path, "wb") as f:
            f.write(r.content)
        if emotion and emo.apply_prosody(path, emotion):
            print(f"{line['name']}: emotion {emotion} applied", flush=True)
        print(f"{line['name']}: {len(r.content)} bytes in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
