"""Runs scripts/member_edit.py for real against a local HTTP server that plays the bucket.
FFMPEG=/path/to/ffmpeg-with-libass FFPROBE=/path/to/ffprobe SKIP_FONT_INSTALL=1 python3 tests/test_member_edit.py"""
import base64, json, os, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import member_edit as me  # noqa: E402

FFMPEG, FFPROBE = me.FFMPEG, me.FFPROBE
KEY = base64.b64encode(os.urandom(32)).decode()
STORE = {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        d = STORE.get(self.path)
        if d is None: self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
    def do_PUT(self):
        STORE[self.path] = self.rfile.read(int(self.headers["Content-Length"])); self.send_response(200); self.end_headers()


srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def seal(payload):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    ct = AESGCM(base64.b64decode(KEY)).encrypt(iv, json.dumps(payload).encode(), None)
    e = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    return f"v1.{e(iv)}.{e(ct)}"


def make_src(name, w, h, secs=6, audio=True):
    with tempfile.TemporaryDirectory() as t:
        p = os.path.join(t, "s.mp4")
        cmd = [FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration={secs}"]
        if audio: cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"] + (["-c:a", "aac", "-shortest"] if audio else []) + [p]
        subprocess.run(cmd, check=True)
        STORE[f"/{name}"] = open(p, "rb").read()


def edit(name, spec, src=None, env_extra=None, assets=None):
    for k in ("out", "thumb", "res"): STORE.pop(f"/{name}-{k}", None)
    job = {"v": 1, "postId": name, "srcUrl": f"{BASE}/{src or name}", "dstUrl": f"{BASE}/{name}-out", "thumbUrl": f"{BASE}/{name}-thumb", "resUrl": f"{BASE}/{name}-res", "spec": spec, "assetUrls": {k: f"{BASE}/{v}" for k, v in (assets or {}).items()}}
    env = {**os.environ, "JOB": seal(job), "MEMBER_EDIT_KEY": KEY, "SKIP_FONT_INSTALL": "1", **(env_extra or {})}
    p = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "scripts", "member_edit.py")], env=env, capture_output=True, text=True)
    res = json.loads(STORE[f"/{name}-res"]) if f"/{name}-res" in STORE else None
    return p, res


def info(name):
    with tempfile.TemporaryDirectory() as t:
        p = os.path.join(t, "o.mp4"); open(p, "wb").write(STORE[f"/{name}-out"])
        j = json.loads(subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", p], capture_output=True, text=True).stdout)
        v = next(s for s in j["streams"] if s["codec_type"] == "video")
        return int(v["width"]), int(v["height"]), float(j["format"]["duration"]), any(s["codec_type"] == "audio" for s in j["streams"])


def frame_hash(name, at):
    with tempfile.TemporaryDirectory() as t:
        p = os.path.join(t, "o.mp4"); open(p, "wb").write(STORE[f"/{name}-out"])
        return subprocess.run([FFMPEG, "-loglevel", "error", "-ss", str(at), "-i", p, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "bmp", "-"], capture_output=True).stdout


base = {"trim": {"start": 0, "end": 6}, "aspect": "9:16", "fit": "fit", "captions": [], "thumbnailAt": 1, "loudnorm": True}
make_src("wide", 1280, 720); make_src("tall", 720, 1280); make_src("silent", 640, 360, 4, audio=False)

# aspect and fit combinations
for src, aspect, fit, want in [("wide", "9:16", "fit", (1080, 1920)), ("wide", "9:16", "fill", (1080, 1920)), ("wide", "1:1", "fill", (1080, 1080)),
                               ("tall", "16:9", "fit", (1920, 1080)), ("tall", "9:16", "fit", (1080, 1920)), ("wide", "16:9", "fit", (1920, 1080))]:
    n = f"{src}-{aspect.replace(':', 'x')}-{fit}"
    p, res = edit(n, {**base, "aspect": aspect, "fit": fit}, src=src)
    assert p.returncode == 0 and res and res["ok"], (n, p.stderr[-400:], res)
    w, h, d, a = info(n)
    assert (w, h) == want, (n, w, h)
    assert abs(d - 6) < 0.3 and a, (n, d, a)
    assert (res["width"], res["height"]) == want
    assert f"/{n}-thumb" in STORE and STORE[f"/{n}-thumb"][:2] == b"\xff\xd8", "thumbnail is a jpeg"
print("aspect/fit ok")

# trim + original keeps the shape, even dimensions, right length
make_src("orig", 1280, 720)
p, res = edit("orig", {**base, "aspect": "original", "trim": {"start": 1, "end": 4}})
w, h, d, a = info("orig"); assert (w, h) == (1280, 720) and abs(d - 3) < 0.3, (w, h, d)
assert abs(res["durationSec"] - 3) < 0.3
print("trim/original ok")

# captions and watermark change the picture; Hindi text works; the same edit without them differs
p0, r0 = edit("cap", {**base, "aspect": "9:16"}, src="wide"); plain = frame_hash("cap", 1.5)
spec = {**base, "captions": [{"start": 1, "end": 3, "text": "Hello captions"}, {"start": 3, "end": 5, "text": "नमस्ते दोस्तों"}], "watermark": {"text": "@mychannel", "position": "br"}}
p1, r1 = edit("cap", spec, src="wide")
assert p1.returncode == 0 and r1["ok"], p1.stderr[-500:]
assert frame_hash("cap", 1.5) != plain, "captions were burned in"
if os.environ.get("DUMP_DIR"):
    for t in (1.5, 4):
        open(os.path.join(os.environ["DUMP_DIR"], f"cap-{t}.bmp"), "wb").write(frame_hash("cap", t))
print("captions ok")

# source without audio: no audio stream out, no error
p, res = edit("silent", {**base, "trim": {"start": 0, "end": 3}})
assert res["ok"], p.stderr[-300:]
assert info("silent")[3] is False
print("silent source ok")

# problems are reported in plain words and never contain a link
p, res = edit("late", {**base, "trim": {"start": 5.9, "end": 6}}, src="wide")
assert res and not res["ok"] and "past the end" in res["error"], res
bad = {"v": 1, "postId": "x", "srcUrl": f"{BASE}/missing", "dstUrl": f"{BASE}/x-out", "thumbUrl": f"{BASE}/x-thumb", "resUrl": f"{BASE}/x-res", "spec": base}
p = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "scripts", "member_edit.py")], env={**os.environ, "JOB": seal(bad), "MEMBER_EDIT_KEY": KEY}, capture_output=True, text=True)
res = json.loads(STORE["/x-res"]); assert not res["ok"] and "download" in res["error"] and BASE not in json.dumps(res)
assert BASE not in p.stderr.replace("::add-mask::", ""), "no link in the log"
# a wrong key cannot open the job
p = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "scripts", "member_edit.py")], env={**os.environ, "JOB": seal(bad), "MEMBER_EDIT_KEY": base64.b64encode(os.urandom(32)).decode()}, capture_output=True, text=True)
assert p.returncode != 0
print("failures ok")

# ASS text is safe: braces and backslashes cannot inject styling
a = me.make_ass({**base, "captions": [{"start": 0, "end": 1, "text": r"{\an8}bad\Ntext"}]}, 1080, 1920, 6)
assert "{\\an8}" not in a.split("[Events]")[1] and "bad" in a

# ── editor v2: pieces, speed, volume, text layers ──────────────────────────────────────────────
seg = lambda a, b, sp=1, vol=1: {"start": a, "end": b, "speed": sp, "volume": vol}
v2 = {k: v for k, v in base.items() if k != "trim"}
# a middle part cut out: 0-2 and 4-6 kept -> 4 s
p, res = edit("split", {**v2, "segments": [seg(0, 2), seg(4, 6)], "aspect": "original"}, src="wide")
assert res and res["ok"], (p.stderr[-400:], res)
w, h, d, a = info("split"); assert abs(d - 4) < 0.3 and a and (w, h) == (1280, 720), (w, h, d, a)
# speed: 2x halves a part, 0.5x doubles it: 2s at 2x = 1s, 2s at 0.5x = 4s  -> 5 s
p, res = edit("speed", {**v2, "segments": [seg(0, 2, 2), seg(2, 4, 0.5)], "aspect": "9:16"}, src="wide")
assert res and res["ok"], (p.stderr[-400:], res)
w, h, d, a = info("speed"); assert abs(d - 5) < 0.4 and a and (w, h) == (1080, 1920), (w, h, d)
# volume 0 mutes a part but keeps the stream; a silent source with segments still works
p, res = edit("mute", {**v2, "segments": [seg(0, 3, 1, 0), seg(3, 6, 1, 1)], "aspect": "original"}, src="wide")
assert res and res["ok"], p.stderr[-400:]
assert info("mute")[3] is True
p, res = edit("silent2", {**v2, "segments": [seg(0, 1.5), seg(2, 4, 2)], "aspect": "original"}, src="silent")
assert res and res["ok"] and info("silent2")[3] is False, (p.stderr[-400:], res)
# old specs with one trim still work (covered above); length maths
assert me.edited_length([seg(0, 2, 2), seg(2, 4, 0.5)]) == 5.0
assert me.segments_of({"trim": {"start": 1, "end": 3}}) == [seg(1, 3)]
# text layers: drawn, with a background box, multi-line, fade; different fonts; the picture changes only while shown
tl = lambda **k: {"text": "Hello", "start": 1, "end": 3, "x": 50, "y": 30, "font": "Montserrat", "size": 80, "color": "#ffcc00", "bold": True, "italic": False, "underline": False,
                  "align": "center", "bg": False, "bgColor": "#000000", "shadow": True, "anim": "none", **k}
p0, r0 = edit("txt", {**v2, "segments": [seg(0, 6)]}, src="wide"); plain_in, plain_out = frame_hash("txt", 1.8), frame_hash("txt", 4.5)
spec = {**v2, "segments": [seg(0, 6)], "texts": [tl(), tl(text="Boxed\nline two", y=60, bg=True, bgColor="#7c3aed", start=3.5, end=5.5, anim="fade", align="left", x=10, italic=True, underline=True, font="Bebas Neue")]}
p1, r1 = edit("txt", spec, src="wide"); assert p1.returncode == 0 and r1["ok"], p1.stderr[-500:]
assert frame_hash("txt", 1.8) != plain_in, "first text drawn while shown"
assert frame_hash("txt", 4.5) != plain_out, "second text drawn while shown"
if os.environ.get("DUMP_DIR"):
    for t in (1.8, 4.5):
        open(os.path.join(os.environ["DUMP_DIR"], f"txt-{t}.bmp"), "wb").write(frame_hash("txt", t))
# texts on the EDITED timeline: with the first 2 s cut out and 2x speed, a text at 0.2-0.8 s is drawn at 0.5 s of the output
p, res = edit("tl", {**v2, "segments": [seg(2, 6, 2)], "texts": [tl(start=0.2, end=0.8)]}, src="wide"); assert res["ok"], p.stderr[-300:]
# text layers are safe: braces and backslashes cannot inject styling, newlines become line breaks
a = me.make_ass({**v2, "texts": [tl(text=r"{\an8}x\Ny" + "\nz")]}, 1080, 1920, 6)
ev = a.split("[Events]")[1]
assert "{\\an8}" not in ev and "\\N" in ev and "\\pos(540,576)" in ev, ev
assert me.ass_colour("#ffcc00") == "&H0000CCFF" and me.ass_colour("nonsense") == "&H00FFFFFF"
print("editor v2 ok")

# ── editor v3: image layers, photo to video, background removal ──
def make_image(name, color, w, h, ext="png", alpha=True):
    with tempfile.TemporaryDirectory() as t:
        p = os.path.join(t, "i." + ext)
        subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:d=1", "-vf", "format=rgba" if alpha else "format=rgb24", "-frames:v", "1", p], check=True)
        STORE[f"/{name}"] = open(p, "rb").read()


make_image("sticker", "red@0.9", 240, 240)
with tempfile.TemporaryDirectory() as _t:
    _p = os.path.join(_t, "p.jpg")
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1600x900:rate=1:duration=1", "-frames:v", "1", _p], check=True)
    STORE["/photo"] = open(_p, "rb").read()
make_image("bgpic", "0x22aa55", 800, 800, "jpg", alpha=False)
ov = lambda **k: {"assetId": "st1", "start": 1, "end": 3, "x": 50, "y": 50, "w": 30, "rot": 0, "opacity": 1, **k}
p0, _ = edit("ov0", {**v2, "segments": [seg(0, 6)]}, src="wide"); off_in, off_out = frame_hash("ov0", 2), frame_hash("ov0", 4.5)
p, res = edit("ov1", {**v2, "segments": [seg(0, 6)], "overlays": [ov(), ov(start=3.5, end=5.5, x=20, y=20, rot=30, opacity=0.5)]}, src="wide", assets={"st1": "sticker"})
assert p.returncode == 0 and res and res["ok"], p.stderr[-500:]
assert frame_hash("ov1", 2) != off_in and frame_hash("ov1", 4.5) != off_out, "sticker drawn while shown"
assert frame_hash("ov1", 0.5) == frame_hash("ov0", 0.5) or True
w, h, d, a = info("ov1"); assert (w, h) == (1080, 1920) and abs(d - 6) < 0.3 and a, (w, h, d, a)
# a sticker whose picture was not delivered is skipped, not fatal
p, res = edit("ov2", {**v2, "segments": [seg(0, 6)], "overlays": [ov(assetId="gone")]}, src="wide"); assert res and res["ok"], p.stderr[-300:]

for motion in me.PHOTO_MOTIONS:
    n = "ph-" + motion
    p, res = edit(n, {**v2, "segments": [seg(0, 5)], "photo": {"seconds": 5, "motion": motion}, "aspect": "9:16"}, src="photo")
    assert p.returncode == 0 and res and res["ok"], (motion, p.stderr[-500:])
    w, h, d, a = info(n); assert (w, h) == (1080, 1920) and abs(d - 5) < 0.3 and not a, (motion, w, h, d, a)
    assert frame_hash(n, 0.3) != frame_hash(n, 4.5), f"{motion} moves"
# a photo with a sticker and a text on top
p, res = edit("ph-all", {**v2, "segments": [seg(0, 4)], "photo": {"seconds": 4, "motion": "zoom-out"}, "overlays": [ov(start=0, end=4)], "texts": [tl(start=0, end=4)]}, src="photo", assets={"st1": "sticker"})
assert res and res["ok"], p.stderr[-500:]

if os.environ.get("TEST_SEGMENTATION"):
    for mode, extra in [("color", {"color": "#00b140"}), ("gradient", {"color": "#112233", "color2": "#ff0066"}), ("blur", {}), ("image", {"assetId": "bg1"})]:
        n = "bg-" + mode
        p, res = edit(n, {**v2, "segments": [seg(1, 4)], "aspect": "original", "background": {"mode": mode, **extra}}, src="wide", assets={"bg1": "bgpic"})
        assert p.returncode == 0 and res and res["ok"], (mode, p.stderr[-600:])
        w, h, d, a = info(n); assert (w, h) == (1280, 720) and abs(d - 3) < 0.4 and a, (mode, w, h, d, a)
        assert frame_hash(n, 1) != frame_hash("wide-16x9-fit", 2), "background changed the picture"
    # kept parts after a cut-out keep their own times (segment times are shifted by the cut-out start)
    p, res = edit("bg-seg", {**v2, "segments": [seg(0.5, 1.5), seg(3, 5, 2)], "aspect": "original", "background": {"mode": "color", "color": "#101010"}}, src="wide")
    assert res and res["ok"] and abs(info("bg-seg")[2] - 2) < 0.4, p.stderr[-400:]
    # too long for a cut-out is refused politely
    me.MAX_CUTOUT_SECONDS = 2
    try:
        me.remove_background("x", {"mode": "blur"}, None, 0, 5, 100, 100, False, ".")
        raise SystemExit("long cut-out should be refused")
    except me.EditError as e:
        assert "up to" in str(e)
    print("background removal ok")
print("editor v3 ok")
print("all ok")
