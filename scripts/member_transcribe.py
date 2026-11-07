#!/usr/bin/env python3
"""
Automatic captions for a member's video, run on a GitHub runner by member-transcribe.yml.

Same encrypted-job contract as member_edit.py (the repo is public): the job holds a presigned link to the source video, a presigned
link for the result, and an optional language code. Output result.json: {ok, language, lines:[{start,end,text}]} with times on the
SOURCE video's timeline (the app moves them onto the trimmed timeline). Speech-to-text is faster-whisper (CPU, int8).
"""
import json
import os
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(__file__))
from member_edit import EditError, FFMPEG, curl_get, curl_put, open_job  # noqa: E402

MODEL = os.environ.get("WHISPER_MODEL", "small")
MAX_SECONDS = 12 * 60
MAX_LINES = 400


def lines_from_words(words, max_words=7, max_chars=44, max_secs=3.6, pause=0.7):
    """Groups timed words into caption-sized lines: a line ends at sentence punctuation, a pause, or when it is long enough."""
    lines, cur = [], []

    def flush():
        if cur:
            text = " ".join(w["word"].strip() for w in cur).strip()
            if text:
                lines.append({"start": round(cur[0]["start"], 2), "end": round(cur[-1]["end"], 2), "text": text})
            cur.clear()

    for i, w in enumerate(words):
        if cur and (w["start"] - cur[-1]["end"] >= pause):
            flush()
        cur.append(w)
        text = " ".join(x["word"].strip() for x in cur)
        nxt = words[i + 1] if i + 1 < len(words) else None
        ends_sentence = text.rstrip().endswith((".", "?", "!", "।", "…"))
        too_long = len(cur) >= max_words or len(text) >= max_chars or (w["end"] - cur[0]["start"]) >= max_secs
        if ends_sentence and len(cur) >= 2 or too_long or nxt is None:
            flush()
    return lines[:MAX_LINES]


def load_pcm16_mono(path):
    import numpy as np
    with wave.open(path, "rb") as wf:
        assert wf.getnchannels() == 1 and wf.getframerate() == 16000 and wf.getsampwidth() == 2
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0


def transcribe(audio, language=None):
    from faster_whisper import WhisperModel
    model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=4)
    segments, info = model.transcribe(audio, language=language or None, vad_filter=True, word_timestamps=True, beam_size=1, condition_on_previous_text=False)
    words = []
    for seg in segments:
        for w in (seg.words or []):
            if w.end > w.start or w.word.strip():
                words.append({"word": w.word, "start": float(w.start), "end": float(w.end)})
    return info.language, words


def work(job, tmp):
    src = os.path.join(tmp, "src")
    curl_get(job["srcUrl"], src)
    wav = os.path.join(tmp, "a.wav")
    p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-t", str(MAX_SECONDS), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], capture_output=True, text=True)
    if p.returncode != 0 or not os.path.exists(wav):
        raise EditError("This video has no audio to transcribe.")
    audio = load_pcm16_mono(wav)
    if len(audio) < 16000 * 0.5:
        raise EditError("This video has no audio to transcribe.")
    language, words = transcribe(audio, job.get("language"))
    lines = lines_from_words(words)
    if not lines:
        raise EditError("No speech was found in this video.")
    return {"ok": True, "language": language, "lines": lines}


def main():
    sealed, key = os.environ.get("JOB", ""), os.environ.get("MEMBER_EDIT_KEY", "")
    if not sealed or not key:
        print("JOB or MEMBER_EDIT_KEY missing", file=sys.stderr)
        return 2
    res_url, result, code = None, None, 0
    try:
        job = open_job(sealed, key)
        for k in ("srcUrl", "resUrl"):
            print(f"::add-mask::{job[k]}")
        res_url = job["resUrl"]
        with tempfile.TemporaryDirectory() as tmp:
            result = work(job, tmp)
    except EditError as e:
        result, code = {"ok": False, "error": str(e)}, 1
    except Exception as e:  # noqa: BLE001
        print(f"unexpected error: {type(e).__name__}", file=sys.stderr)
        result, code = {"ok": False, "error": "Automatic captions could not be made for this video."}, 1
    if res_url:
        with tempfile.TemporaryDirectory() as tmp:
            rp = os.path.join(tmp, "result.json")
            json.dump(result, open(rp, "w"))
            try:
                curl_put(res_url, rp, "application/json")
            except Exception:  # noqa: BLE001
                code = code or 1
    return code


if __name__ == "__main__":
    sys.exit(main())
