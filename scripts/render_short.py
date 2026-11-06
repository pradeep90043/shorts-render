#!/usr/bin/env python3
"""
Single-Pass Cloud Renderer for Shorts/Reels
-------------------------------------------
Renders a complete vertical short in one single FFmpeg pass:
- Video cropping / scaling to 1080x1920
- Brand watermark overlay
- Cover card (3s intro)
- Reaction sticker overlay (with sustained loop)
- Timed animated captions (libass)
- Voiceover & background music mixing
Runs on GitHub Actions (4 vCPUs, 7GB RAM).
"""

import sys
import os
import json
import argparse
import subprocess
import urllib.request
import tarfile
import shutil
import tempfile
import glob


def ensure_devanagari_font():
    """Hindi captions need a Devanagari font; the GitHub runner image has none, so libass would draw empty boxes.
    Installs Noto (time-limited, never fatal: without it the render still finishes, only the Hindi glyphs are missing)."""
    try:
        have = subprocess.run(["fc-list", ":lang=hi"], capture_output=True, text=True, timeout=30).stdout.strip()
        if have:
            return
        print("🔤 Installing a Devanagari font for Hindi captions...")
        for cmd in (["sudo", "apt-get", "-o", "Acquire::Retries=2", "-o", "Acquire::http::Timeout=20", "update", "-qq"],
                    ["sudo", "apt-get", "-o", "Acquire::Retries=2", "install", "-y", "-qq", "fonts-noto-core"],
                    ["sudo", "fc-cache", "-f"]):
            subprocess.run(cmd, timeout=150, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok = subprocess.run(["fc-list", ":lang=hi"], capture_output=True, text=True, timeout=30).stdout.strip()
        print("✓ Devanagari font ready" if ok else "⚠ Devanagari font not available: Hindi captions may show empty boxes")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠ font setup skipped: {exc}")


def download_and_extract_bundle(bundle_url: str, dest_dir: str):
    print(f"📥 Downloading bundle from: {bundle_url}")
    tar_path = os.path.join(dest_dir, "bundle.tar.gz")
    
    # Download with custom User-Agent
    req = urllib.request.Request(bundle_url, headers={"User-Agent": "shorts-automation-renderer"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tar_path, "wb") as f:
        shutil.copyfileobj(resp, f)
        
    print(f"📦 Extracting bundle ({os.path.getsize(tar_path)} bytes)...")
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(dest_dir)
    print("✓ Bundle extracted successfully.")


def find_bg_music(repo_root: str, mood: str) -> str:
    """Finds an appropriate background music track for the given mood."""
    music_dir = os.path.join(repo_root, "assets", "music")
    ambient_dir = os.path.join(repo_root, "assets", "ambient")
    
    # Try mood folder first
    mood_dir = os.path.join(music_dir, mood)
    if os.path.isdir(mood_dir):
        tracks = [os.path.join(mood_dir, f) for f in os.listdir(mood_dir) if f.endswith((".mp3", ".wav"))]
        if tracks:
            return tracks[0]
            
    # Try any track in music dir
    all_tracks = glob.glob(os.path.join(music_dir, "**", "*.mp3"), recursive=True)
    if all_tracks:
        return all_tracks[0]
        
    # Ambient fallback
    ambient_tracks = glob.glob(os.path.join(ambient_dir, "*.mp3"))
    if ambient_tracks:
        return ambient_tracks[0]
        
    return ""


def get_media_duration(file_path: str) -> float:
    """Uses ffprobe to inspect exact duration in seconds."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception as e:
        print(f"⚠️ Warning: could not probe duration for {file_path}: {e}")
        return 0.0


def build_and_run_ffmpeg(work_dir: str, repo_root: str, out_path: str):
    spec_path = os.path.join(work_dir, "short_spec.json")
    if not os.path.exists(spec_path):
        raise FileNotFoundError(f"Missing short_spec.json in {work_dir}")
        
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    profile = spec.get("profile", "codeorcap").lower()
    mood = spec.get("mood", "technical").lower()
    crop_top = float(spec.get("cropTopPct", 0)) / 100.0
    crop_bottom = float(spec.get("cropBottomPct", 0)) / 100.0
    use_cover = spec.get("cover", True)
    use_watermark = spec.get("watermark", True)
    use_sticker = spec.get("sticker", True)
    use_captions = spec.get("captions", True)
    use_voiceover = spec.get("useVoiceover", True)
    original_audio = spec.get("originalAudio", False)
    
    # Input files in work_dir
    raw_video = os.path.join(work_dir, "reel_video.mp4")
    if not os.path.exists(raw_video):
        # Fallback to pristine_video or any mp4
        mp4s = [os.path.join(work_dir, f) for f in os.listdir(work_dir) if f.endswith(".mp4") and "video" in f]
        raw_video = mp4s[0] if mp4s else ""

    if not raw_video or not os.path.exists(raw_video):
        raise FileNotFoundError(f"Input video not found in bundle: {work_dir}")

    # Determine base video duration so audio and video stop cleanly together
    video_duration = float(spec.get("durationSeconds", 0))
    if video_duration <= 0:
        video_duration = get_media_duration(raw_video)
    if video_duration <= 0:
        video_duration = 90.0
    print(f"⏱️ Base video duration determined as: {video_duration:.3f}s")

    # Build FFmpeg command inputs
    inputs = ["-i", raw_video]
    next_input_idx = 1

    # InfoOnSpot broadcast template: the video sits in a 1140 px window and the branded frame (made by the app, in the bundle) goes over it
    info_idx = -1
    info_overlay = spec.get("infoOverlay")
    if info_overlay and os.path.exists(os.path.join(work_dir, info_overlay)):
        inputs.extend(["-i", os.path.join(work_dir, info_overlay)])
        info_idx = next_input_idx
        next_input_idx += 1
        use_watermark = False  # the template carries the brand
        use_cover = False
    
    # Watermark
    wm_idx = -1
    if use_watermark:
        # the channel's own watermark comes in the bundle; else its committed asset; CodeOrCap's generic one only for CodeOrCap
        wm_file = os.path.join(work_dir, "brand_watermark.png")
        if not os.path.exists(wm_file):
            wm_file = os.path.join(repo_root, "assets", f"watermark_{profile}.png")
        if not os.path.exists(wm_file) and profile == "codeorcap":
            wm_file = os.path.join(repo_root, "assets", "watermark.png")
        if os.path.exists(wm_file):
            inputs.extend(["-i", wm_file])
            wm_idx = next_input_idx
            next_input_idx += 1

    # Cover card (3s intro overlay)
    cov_idx = -1
    cover_file = os.path.join(work_dir, "infoonspot_cover.png")
    if not os.path.exists(cover_file):
        cover_file = os.path.join(work_dir, "cover.png")
    if use_cover and os.path.exists(cover_file):
        inputs.extend(["-i", cover_file])
        cov_idx = next_input_idx
        next_input_idx += 1

    # Sticker
    sticker_idx = -1
    sticker_tail_idx = -1
    if use_sticker:
        sticker_file = os.path.join(repo_root, "assets", "stickers", profile, f"{mood}.mov")
        if os.path.exists(sticker_file):
            # Pre-cut tail for seamless looping
            tail_file = os.path.join(work_dir, "sticker_tail.mov")
            cut_cmd = ["ffmpeg", "-y", "-ss", "5", "-t", "5", "-i", sticker_file, "-c", "copy", "-an", tail_file]
            subprocess.run(cut_cmd, check=False)
            
            inputs.extend(["-i", sticker_file])
            sticker_idx = next_input_idx
            next_input_idx += 1
            
            if os.path.exists(tail_file):
                inputs.extend(["-stream_loop", "-1", "-i", tail_file])
                sticker_tail_idx = next_input_idx
                next_input_idx += 1

    # Voiceover audio
    voice_idx = -1
    voice_file = os.path.join(work_dir, "voiceover.mp3")
    if use_voiceover and os.path.exists(voice_file):
        inputs.extend(["-i", voice_file])
        voice_idx = next_input_idx
        next_input_idx += 1

    # Background music
    music_idx = -1
    if not original_audio:
        music_file = find_bg_music(repo_root, mood)
        if music_file and os.path.exists(music_file):
            inputs.extend(["-stream_loop", "-1", "-i", music_file])
            music_idx = next_input_idx
            next_input_idx += 1

    # Captions
    ass_file = os.path.join(work_dir, "captions.ass")
    has_captions = use_captions and os.path.exists(ass_file)

    # ── BUILD FILTER GRAPH ───────────────────────────────────────────────────
    v_filters = []
    
    # 1. Base video crop + scale to 1080x1920
    # Crop calculation: remove crop_top and crop_bottom percent
    crop_h = f"ih*(1-{crop_top:.3f}-{crop_bottom:.3f})"
    crop_y = f"ih*{crop_top:.3f}"
    if info_idx != -1:
        v_filters.append("[0:v]scale=1080:1140:force_original_aspect_ratio=increase,crop=1080:1140,pad=1080:1920:0:446:black[tbg]")
        v_filters.append(f"[tbg][{info_idx}:v]overlay=0:0[base]")
    else:
        v_filters.append(f"[0:v]crop=iw:{crop_h}:0:{crop_y},scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black[base]")
    current_v = "[base]"

    # 2. Watermark overlay (bottom right)
    if wm_idx != -1:
        v_filters.append(f"{current_v}[{wm_idx}:v]overlay=W-w-40:H-h-40[wm]")
        current_v = "[wm]"

    # 3. Cover card overlay (first 3 seconds)
    if cov_idx != -1:
        v_filters.append(f"{current_v}[{cov_idx}:v]overlay=0:0:enable='lte(t,3)'[cov]")
        current_v = "[cov]"

    # 4. Reaction sticker overlay (bottom left)
    if sticker_idx != -1:
        # the cover card owns the first 3 s (its headline sits where the sticker does): the sticker's own timeline starts after it
        shift = "+3/TB" if cov_idx != -1 else ""
        if sticker_tail_idx != -1:
            v_filters.append(f"[{sticker_idx}:v]setpts=PTS-STARTPTS[stk_full];[{sticker_tail_idx}:v]setpts=PTS-STARTPTS[stk_tail];[stk_full][stk_tail]concat=n=2:v=1:a=0,setpts=PTS-STARTPTS{shift}[stickerloop]")
            v_filters.append(f"{current_v}[stickerloop]overlay=40:H-h-40:shortest=1:eof_action=pass[stk]")
        else:
            v_filters.append(f"[{sticker_idx}:v]setpts=PTS-STARTPTS{shift}[stkone]")
            v_filters.append(f"{current_v}[stkone]overlay=40:H-h-40:shortest=1:eof_action=pass[stk]")
        current_v = "[stk]"

    # 5. Burn animated subtitles (ASS)
    if has_captions:
        escaped_ass = ass_file.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        v_filters.append(f"{current_v}subtitles='{escaped_ass}'[vfinal]")
        current_v = "[vfinal]"

    # ── BUILD AUDIO GRAPH ────────────────────────────────────────────────────
    a_filters = []
    has_audio_out = False
    
    if voice_idx != -1 and music_idx != -1:
        # Voiceover + background music:
        # apad ensures voiceover covers full duration; atrim prevents infinite looped music
        # Voice: volume 1.35, Music: volume 0.18
        a_filters.append(
            f"[{voice_idx}:a]apad=whole_dur={video_duration:.3f},volume=1.35,atrim=0:{video_duration:.3f}[vox];"
            f"[{music_idx}:a]volume=0.18,atrim=0:{video_duration:.3f}[bgm];"
            f"[vox][bgm]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[afinal]"
        )
        has_audio_out = True
    elif voice_idx != -1:
        # Voiceover only
        a_filters.append(
            f"[{voice_idx}:a]apad=whole_dur={video_duration:.3f},volume=1.35,atrim=0:{video_duration:.3f}[afinal]"
        )
        has_audio_out = True
    elif music_idx != -1:
        # Music only
        a_filters.append(
            f"[{music_idx}:a]volume=0.45,atrim=0:{video_duration:.3f}[afinal]"
        )
        has_audio_out = True
    elif original_audio:
        # Original video audio
        a_filters.append(
            f"[0:a]volume=1.0,atrim=0:{video_duration:.3f}[afinal]"
        )
        has_audio_out = True

    filter_complex_parts = v_filters
    if a_filters:
        filter_complex_parts.extend(a_filters)
    filter_complex = "; ".join(filter_complex_parts)

    cmd = ["ffmpeg", "-y"]
    cmd.extend(inputs)
    cmd.extend(["-filter_complex", filter_complex])
    cmd.extend(["-map", current_v])
    
    if has_audio_out:
        cmd.extend(["-map", "[afinal]", "-c:a", "aac", "-b:a", "192k", "-ar", "44100"])
    else:
        cmd.extend(["-an"])
        
    cmd.extend([
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-shortest",
        "-t", f"{video_duration:.3f}",
        "-movflags", "+faststart",
        out_path
    ])

    print("🚀 Executing single-pass cloud FFmpeg command:")
    print(" ".join(cmd))
    
    # Run FFmpeg without capturing output so progress is streamed live to GitHub Actions runner logs
    res = subprocess.run(cmd)
    if res.returncode != 0:
        print(f"❌ FFmpeg error (code {res.returncode})")
        raise RuntimeError(f"FFmpeg render failed with exit code {res.returncode}")

    print(f"✅ Cloud video render completed successfully: {out_path} ({os.path.getsize(out_path)} bytes)")


def main():
    parser = argparse.ArgumentParser(description="Single-Pass Cloud Short Renderer")
    parser.add_argument("--bundle", required=True, help="URL of bundle tar.gz")
    parser.add_argument("--out", default="video_final.mp4", help="Output video path")
    args = parser.parse_args()

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    temp_dir = tempfile.mkdtemp(prefix="short_cloud_")
    try:
        download_and_extract_bundle(args.bundle, temp_dir)
        ensure_devanagari_font()
        build_and_run_ffmpeg(temp_dir, repo_root, args.out)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
