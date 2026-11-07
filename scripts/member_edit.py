#!/usr/bin/env python3
"""
Member video editor, run on a GitHub runner by member-edit.yml.

The job arrives ENCRYPTED (this repo is public, so workflow inputs are readable by anyone): AES-256-GCM, key in the
MEMBER_EDIT_KEY secret. It holds presigned links to the member's video in a private bucket and the edit settings:

  segments [{start,end,speed,volume}] (the kept parts of the source, in order; older jobs have one trim {start,end}),
  aspect 9:16|1:1|16:9|original, fit fit|fill, captions [{start,end,text}] and texts [text layers] (times on the EDITED timeline),
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
FONT_PACKAGES = {"Montserrat": "fonts-montserrat", "Roboto": "fonts-roboto", "Open Sans": "fonts-open-sans", "Oswald": "fonts-oswald", "Noto Sans": "fonts-noto-core"}


def segments_of(spec: dict) -> list:
    """The kept parts of the source. Older jobs carry a single trim."""
    segs = spec.get("segments")
    if not segs:
        t = spec.get("trim") or {"start": 0, "end": 0}
        segs = [{"start": t["start"], "end": t["end"]}]
    return [{"start": float(g["start"]), "end": float(g["end"]), "speed": float(g.get("speed", 1) or 1), "volume": float(g.get("volume", 1))} for g in segs]


def edited_length(segs: list) -> float:
    return round(sum((g["end"] - g["start"]) / g["speed"] for g in segs), 3)


def target_size(aspect: str, w: int, h: int):
    if aspect == "9:16":
        return 1080, 1920
    if aspect == "1:1":
        return 1080, 1080
    if aspect == "16:9":
        return 1920, 1080
    scale = min(1.0, 1920 / max(w, h))  # original: same shape, at most 1920 on the long side, even numbers
    return max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)


def segment_graph(segs: list, has_audio: bool, loudnorm: bool) -> str:
    """Cuts the kept parts out of the source, changes their speed and volume and joins them: ends in [vc] and, with sound, [ac]."""
    parts, labels = [], []
    for i, g in enumerate(segs):
        parts.append(f"[0:v]trim=start={g['start']}:end={g['end']},setpts=(PTS-STARTPTS)/{g['speed']}[v{i}]")
        if has_audio:
            af = f"[0:a]atrim=start={g['start']}:end={g['end']},asetpts=PTS-STARTPTS"
            if g["speed"] != 1:
                af += f",atempo={g['speed']}"
            if g["volume"] != 1:
                af += f",volume={g['volume']}"
            parts.append(af + f"[a{i}]")
            labels.append(f"[v{i}][a{i}]")
        else:
            labels.append(f"[v{i}]")
    parts.append("".join(labels) + f"concat=n={len(segs)}:v=1:a={1 if has_audio else 0}" + ("[vc][ac]" if has_audio else "[vc]"))
    if has_audio:
        parts.append("[ac]" + ("loudnorm=I=-14:TP=-1.5:LRA=11" if loudnorm else "anull") + "[a]")
    return ";".join(parts)


def video_graph(spec: dict, w: int, h: int, ass_path, inp: str = "[vc]"):
    """Returns (filter_complex from `inp` to [v], out_w, out_h): the shape (fit/fill), then the subtitles."""
    tw, th = target_size(spec["aspect"], w, h)
    same_shape = abs(w / h - tw / th) < 0.01
    if same_shape or spec["aspect"] == "original":
        base = f"{inp}scale={tw}:{th}:flags=lanczos,setsar=1[b]"
    elif spec.get("fit") == "fill":
        base = f"{inp}scale={tw}:{th}:force_original_aspect_ratio=increase:flags=lanczos,crop={tw}:{th},setsar=1[b]"
    else:  # fit: the whole picture over a blurred, enlarged copy of itself
        base = (
            f"{inp}split[s1][s2];"
            f"[s1]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},boxblur=24:6,setsar=1[bg];"
            f"[s2]scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[b]"
        )
    tail = f";[b]ass='{ass_path}'[c];[c]format=yuv420p[v]" if ass_path else ";[b]format=yuv420p[v]"
    return base + tail, tw, th


def ass_time(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_text(t: str, multiline: bool = False) -> str:
    t = t.replace("\\", "").replace("{", "(").replace("}", ")")
    return t.replace("\n", "\\N") if multiline else t.replace("\n", " ")


def ass_colour(hex_rgb: str, alpha: int = 0) -> str:
    """#rrggbb to ASS &HAABBGGRR (alpha 0 = opaque, 255 = clear)."""
    h = (hex_rgb or "#ffffff").lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        h = "ffffff"
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper().replace("&H", "&H", 1)


def make_ass(spec: dict, w: int, h: int, length: float):
    """The .ass file text for captions, text layers and the watermark, or None when the edit has none of them."""
    caps, wm, texts = spec.get("captions") or [], spec.get("watermark"), spec.get("texts") or []
    if not caps and not wm and not texts:
        return None
    side = int(w * 0.06)
    size = max(24, int(h * 0.042)) if h >= w else max(24, int(h * 0.055))
    wm_size = max(16, int(min(w, h) * 0.03))
    corner = {"tl": 7, "tr": 9, "bl": 1, "br": 3}
    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
           "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding")
    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]", fmt,
        f"Style: Cap,Noto Sans,{size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(2, size // 14)},1,2,{side},{side},{int(h * 0.12)},1",
        f"Style: Mark,Noto Sans,{wm_size},&H99FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1,0,"
        f"{corner.get((wm or {}).get('position', 'tr'), 9)},{int(w * 0.03)},{int(w * 0.03)},{int(h * 0.03)},1",
    ]
    scale = w / 1080.0
    side_t = int(w * 0.04)
    for i, t in enumerate(texts):
        fs = max(10, int(round(t["size"] * scale)))
        an = {"left": 4, "center": 5, "right": 6}.get(t.get("align", "center"), 5)
        if t.get("bg"):  # an opaque box: libass draws it with the outline colour, the outline width is the padding
            border, outline, shadow, outline_c = 3, max(4, int(fs * 0.22)), 0, ass_colour(t.get("bgColor", "#000000"), 0x33)
        else:
            border, outline, shadow, outline_c = 1, max(2, fs // 16), (max(2, fs // 18) if t.get("shadow", True) else 0), "&H00000000"
        out.append(
            f"Style: T{i},{t.get('font', 'Noto Sans')},{fs},{ass_colour(t.get('color', '#ffffff'))},&H000000FF,{outline_c},&H80000000,"
            f"{-1 if t.get('bold', True) else 0},{-1 if t.get('italic') else 0},{-1 if t.get('underline') else 0},0,100,100,0,0,{border},{outline},{shadow},{an},{side_t},{side_t},0,1")
    out += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for c in caps:
        font = r"{\fnNoto Sans Devanagari}" if DEVANAGARI.search(c["text"]) else ""
        out.append(f"Dialogue: 0,{ass_time(c['start'])},{ass_time(min(c['end'], length))},Cap,,0,0,0,,{font}{ass_text(c['text'])}")
    for i, t in enumerate(texts):
        x, y = round(t.get("x", 50) / 100 * w), round(t.get("y", 50) / 100 * h)
        an = {"left": 4, "center": 5, "right": 6}.get(t.get("align", "center"), 5)
        tags = f"{{\\an{an}\\pos({x},{y})" + ("\\fad(250,250)" if t.get("anim") == "fade" else "") + "}"
        font = r"{\fnNoto Sans Devanagari}" if DEVANAGARI.search(t["text"]) else ""
        out.append(f"Dialogue: 1,{ass_time(t['start'])},{ass_time(min(t['end'], length))},T{i},,0,0,0,,{tags}{font}{ass_text(t['text'], True)}")
    if wm:
        out.append(f"Dialogue: 2,{ass_time(0)},{ass_time(length)},Mark,,0,0,0,,{ass_text(wm['text'])}")
    return "\n".join(out) + "\n"


def ffmpeg_args(src: str, out: str, graph: str, has_audio: bool) -> list:
    a = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-filter_complex", graph, "-map", "[v]"]
    if has_audio:
        a += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-ar", "44100"]
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


def ensure_fonts(spec: dict, need_subtitles: bool):
    """The runner image has no Noto/Montserrat/... fonts. Installs the ones the edit uses. Best effort, never fatal."""
    if not need_subtitles or os.environ.get("SKIP_FONT_INSTALL"):
        return
    try:
        wanted = {"Noto Sans"} | {t.get("font", "Noto Sans") for t in (spec.get("texts") or [])}
        missing = [f for f in sorted(wanted) if f in FONT_PACKAGES and not subprocess.run(["fc-list", f], capture_output=True, text=True, timeout=30).stdout.strip()]
        if not missing:
            return
        pkgs = sorted({FONT_PACKAGES[f] for f in missing})
        for cmd in (["sudo", "apt-get", "-o", "Acquire::Retries=2", "update", "-qq"], ["sudo", "apt-get", "-o", "Acquire::Retries=2", "install", "-y", "-qq", *pkgs], ["sudo", "fc-cache", "-f"]):
            subprocess.run(cmd, timeout=240, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001
        print(f"font setup skipped: {exc}")


def render(job: dict, work: str) -> dict:
    spec = job["spec"]
    src = os.path.join(work, "src")
    curl_get(job["srcUrl"], src)
    w, h, duration, has_audio = probe(src)
    segs = []
    for g in segments_of(spec):
        if g["start"] >= duration - 0.2:
            continue
        g["end"] = min(g["end"], round(duration, 3))
        if g["end"] - g["start"] >= 0.1:
            segs.append(g)
    if not segs:
        raise EditError("The kept parts are past the end of the video.")
    spec["segments"] = segs
    length = edited_length(segs)

    tw, th = target_size(spec["aspect"], w, h)
    ass = make_ass(spec, tw, th, length)
    ensure_fonts(spec, bool(ass))
    ass_path = None
    if ass:
        ass_path = os.path.join(work, "sub.ass")
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass)
    vg, ow, oh = video_graph(spec, w, h, ass_path.replace("\\", "/").replace(":", "\\:") if ass_path else None)
    graph = segment_graph(segs, has_audio, spec.get("loudnorm", True)) + ";" + vg
    out = os.path.join(work, "out.mp4")
    run(ffmpeg_args(src, out, graph, has_audio), "Editing the video")

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
