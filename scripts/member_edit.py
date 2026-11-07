#!/usr/bin/env python3
"""
Member video editor, run on a GitHub runner by member-edit.yml.

The job arrives ENCRYPTED (this repo is public, so workflow inputs are readable by anyone): AES-256-GCM, key in the
MEMBER_EDIT_KEY secret. It holds presigned links to the member's video in a private bucket and the edit settings:

  trim {start,end}, aspect 9:16|1:1|16:9|original, fit fit|fill, captions [{start,end,text}] (times on the trimmed timeline),
  watermark {text,position}, thumbnailAt, loudnorm

Output: an edited mp4, a thumbnail and a small result.json, each PUT to its presigned link. Nothing here prints a link.
Test hooks: FFMPEG / FFPROBE environment variables.
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
MAX_SOURCE_BYTES = 600 * 1024 * 1024
DEVANAGARI = re.compile(r"[ऀ-ॿ]")


class EditError(Exception):
    """A problem worth telling the member about (safe to show: never contains a link)."""


# ── the encrypted job ──────────────────────────────────────────────────────────────────────────
def b64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def open_job(sealed: str, key_b64: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    v, iv, data = sealed.split(".")
    if v != "v1":
        raise EditError("Unknown job format.")
    key = base64.b64decode(key_b64)
    if len(key) != 32:
        raise EditError("The editor key is not configured correctly.")
    return json.loads(AESGCM(key).decrypt(b64u(iv), b64u(data), None))


# ── ffmpeg graph and subtitles (pure, tested) ──────────────────────────────────────────────────
def target_size(aspect: str, w: int, h: int):
    if aspect == "9:16":
        return 1080, 1920
    if aspect == "1:1":
        return 1080, 1080
    if aspect == "16:9":
        return 1920, 1080
    scale = min(1.0, 1920 / max(w, h))  # original: same shape, at most 1920 on the long side, even numbers
    return max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)


def video_graph(spec: dict, w: int, h: int, ass_path):
    """Returns (filter_complex ending in [v], out_w, out_h)."""
    tw, th = target_size(spec["aspect"], w, h)
    same_shape = abs(w / h - tw / th) < 0.01
    if same_shape or spec["aspect"] == "original":
        base = f"[0:v]scale={tw}:{th}:flags=lanczos,setsar=1[b]"
    elif spec.get("fit") == "fill":
        base = f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase:flags=lanczos,crop={tw}:{th},setsar=1[b]"
    else:  # fit: the whole picture over a blurred, enlarged copy of itself
        base = (
            f"[0:v]split[s1][s2];"
            f"[s1]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},boxblur=24:6,setsar=1[bg];"
            f"[s2]scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[b]"
        )
    tail = f";[b]ass='{ass_path}'[c];[c]format=yuv420p[v]" if ass_path else ";[b]format=yuv420p[v]"
    return base + tail, tw, th


def ass_time(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_text(t: str) -> str:
    t = t.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")
    return DEVANAGARI.sub(lambda m: m.group(0), t)  # kept as is; font chosen per line below


def make_ass(spec: dict, w: int, h: int, length: float):
    """The .ass file text for captions and the watermark, or None when the edit has neither."""
    caps, wm = spec.get("captions") or [], spec.get("watermark")
    if not caps and not wm:
        return None
    side = int(w * 0.06)
    size = max(24, int(h * 0.042)) if h >= w else max(24, int(h * 0.055))
    wm_size = max(16, int(min(w, h) * 0.03))
    corner = {"tl": 7, "tr": 9, "bl": 1, "br": 3}
    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,Noto Sans,{size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(2, size // 14)},1,2,{side},{side},{int(h * 0.12)},1",
        f"Style: Mark,Noto Sans,{wm_size},&H99FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1,0,"
        f"{corner.get((wm or {}).get('position', 'tr'), 9)},{int(w * 0.03)},{int(w * 0.03)},{int(h * 0.03)},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for c in caps:
        font = r"{\fnNoto Sans Devanagari}" if DEVANAGARI.search(c["text"]) else ""
        out.append(f"Dialogue: 0,{ass_time(c['start'])},{ass_time(min(c['end'], length))},Cap,,0,0,0,,{font}{ass_text(c['text'])}")
    if wm:
        out.append(f"Dialogue: 1,{ass_time(0)},{ass_time(length)},Mark,,0,0,0,,{ass_text(wm['text'])}")
    return "\n".join(out) + "\n"


def ffmpeg_args(src: str, out: str, spec: dict, graph: str, has_audio: bool) -> list:
    start, end = spec["trim"]["start"], spec["trim"]["end"]
    a = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start), "-t", str(round(end - start, 3)), "-i", src,
         "-filter_complex", graph, "-map", "[v]"]
    if has_audio:
        a += ["-map", "0:a:0"]
        if spec.get("loudnorm", True):
            a += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
        a += ["-c:a", "aac", "-b:a", "160k", "-ar", "44100"]
    a += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-r", "30", out]
    return a


# ── plumbing ───────────────────────────────────────────────────────────────────────────────────
def run(cmd, what, timeout=1500):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise EditError(f"{what} took too long.")
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()[-3:]
        print(f"{what} failed:", " | ".join(tail), file=sys.stderr)
        raise EditError(f"{what} failed.")
    return p.stdout


def probe(path: str):
    info = json.loads(run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path], "Reading the video"))
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not v:
        raise EditError("There is no video track in this file.")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if abs(rot) % 180 == 90:
        w, h = h, w  # ffmpeg applies the rotation, so the picture is turned
    duration = float(info["format"].get("duration") or v.get("duration") or 0)
    return w, h, duration, any(s["codec_type"] == "audio" for s in info["streams"])


def curl_get(url, dest):
    r = subprocess.run(["curl", "-sS", "-f", "-L", "--retry", "3", "--connect-timeout", "20", "--max-time", "900", "-o", dest, url], capture_output=True, text=True)
    if r.returncode != 0:
        raise EditError("Could not download your video.")
    if os.path.getsize(dest) > MAX_SOURCE_BYTES:
        raise EditError("The video is too large.")


def curl_put(url, path, content_type):
    r = subprocess.run(["curl", "-sS", "-f", "--retry", "3", "--connect-timeout", "20", "--max-time", "900", "-X", "PUT", "-H", f"Content-Type: {content_type}",
                        "--upload-file", path, url], capture_output=True, text=True)
    if r.returncode != 0:
        raise EditError("Could not save the edited video.")


def ensure_fonts(need_subtitles: bool):
    """The runner image has no Noto fonts (Latin and Devanagari captions need them). Best effort, never fatal."""
    if not need_subtitles or os.environ.get("SKIP_FONT_INSTALL"):
        return
    try:
        have = subprocess.run(["fc-list", "Noto Sans"], capture_output=True, text=True, timeout=30).stdout.strip()
        if have:
            return
        for cmd in (["sudo", "apt-get", "-o", "Acquire::Retries=2", "update", "-qq"], ["sudo", "apt-get", "-o", "Acquire::Retries=2", "install", "-y", "-qq", "fonts-noto-core"], ["sudo", "fc-cache", "-f"]):
            subprocess.run(cmd, timeout=180, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001
        print(f"font setup skipped: {exc}")


def render(job: dict, work: str) -> dict:
    spec = job["spec"]
    src = os.path.join(work, "src")
    curl_get(job["srcUrl"], src)
    w, h, duration, has_audio = probe(src)
    if spec["trim"]["start"] >= duration - 0.2:
        raise EditError("The trim start is past the end of the video.")
    spec["trim"]["end"] = min(spec["trim"]["end"], round(duration, 3))
    length = round(spec["trim"]["end"] - spec["trim"]["start"], 3)

    tw, th = target_size(spec["aspect"], w, h)
    ass = make_ass(spec, tw, th, length)
    ensure_fonts(bool(ass))
    ass_path = None
    if ass:
        ass_path = os.path.join(work, "sub.ass")
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass)
    graph, ow, oh = video_graph(spec, w, h, ass_path.replace("\\", "/").replace(":", "\\:") if ass_path else None)
    out = os.path.join(work, "out.mp4")
    run(ffmpeg_args(src, out, spec, graph, has_audio), "Editing the video")

    thumb = os.path.join(work, "thumb.jpg")
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(min(spec.get("thumbnailAt", 0), max(0, length - 0.1))), "-i", out, "-frames:v", "1", "-vf", "scale=720:-2", "-q:v", "3", thumb], "Making the thumbnail", 120)

    _, _, out_duration, _ = probe(out)
    curl_put(job["dstUrl"], out, "video/mp4")
    curl_put(job["thumbUrl"], thumb, "image/jpeg")
    return {"ok": True, "durationSec": round(out_duration, 1), "width": ow, "height": oh}


def main() -> int:
    sealed, key = os.environ.get("JOB", ""), os.environ.get("MEMBER_EDIT_KEY", "")
    if not sealed or not key:
        print("JOB or MEMBER_EDIT_KEY missing", file=sys.stderr)
        return 2
    res_url = None
    try:
        job = open_job(sealed, key)
        for k in ("srcUrl", "dstUrl", "thumbUrl", "resUrl"):
            print(f"::add-mask::{job[k]}")  # never let a link reach the public log
        res_url = job["resUrl"]
        with tempfile.TemporaryDirectory() as work:
            result = render(job, work)
            rp = os.path.join(work, "result.json")
            json.dump(result, open(rp, "w"))
            curl_put(res_url, rp, "application/json")
        print("edit done")
        return 0
    except EditError as e:
        result = {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        print(f"unexpected error: {type(e).__name__}", file=sys.stderr)
        result = {"ok": False, "error": "The editor could not process this video."}
    if res_url:
        try:
            with tempfile.TemporaryDirectory() as work:
                rp = os.path.join(work, "result.json")
                json.dump(result, open(rp, "w"))
                curl_put(res_url, rp, "application/json")
        except Exception:  # noqa: BLE001
            pass
    return 1


if __name__ == "__main__":
    sys.exit(main())
