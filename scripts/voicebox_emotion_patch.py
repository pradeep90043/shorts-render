"""
Run INSIDE the Voicebox container:  docker exec -i voicebox python3 - < scripts/voicebox_emotion_patch.py

Makes Chatterbox's generate() read exaggeration / cfg_weight / temperature from /tmp/vb_emotion.json when that file exists
(scripts/voicebox_emotion.py writes it before each line). Backs the file up, checks the result compiles, and restores the backup
if anything looks wrong, so a failed patch leaves Voicebox exactly as it was. Prints PATCHED / SKIPPED.
"""
import os
import py_compile
import re
import shutil
import sys

HELPER = '''
def _emo(key, default):
    """exaggeration / cfg_weight / temperature override for the current line (written by the runner); default when absent"""
    try:
        import json as _json
        return float(_json.load(open("/tmp/vb_emotion.json")).get(key, default))
    except Exception:
        return default

'''

def find_backend():
    """Look only where Voicebox lives, never across the whole filesystem (/proc, the model cache: that scan hung the step for 15+ minutes)."""
    skip = {"proc", "sys", "dev", ".cache", "huggingface", "node_modules", "__pycache__", "models"}
    for root in ("/app", "/opt", "/home", "/usr/local/lib", "/srv", "/voicebox"):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip]
            if dirpath.endswith("backends") and "chatterbox_backend.py" in filenames:
                return [os.path.join(dirpath, "chatterbox_backend.py")]
    return []


cands = find_backend()
if not cands:
    print("SKIPPED: chatterbox_backend.py not found")
    sys.exit(0)
path = cands[0]
src = open(path).read()
if "_emo(" in src:
    print("SKIPPED: already patched")
    sys.exit(0)
new, n = re.subn(r'(exaggeration|cfg_weight|temperature)=(lang_defaults\["\1"\])', r'\1=_emo("\1", \2)', src)
if n < 3:
    print(f"SKIPPED: expected 3 call sites, found {n}")
    sys.exit(0)
m = re.search(r"^class ", new, re.M)
new = new[: m.start()] + HELPER + new[m.start():]
shutil.copy(path, path + ".bak")
open(path, "w").write(new)
try:
    py_compile.compile(path, doraise=True)
    print("PATCHED", path, n)
except Exception as e:  # noqa: BLE001
    shutil.copy(path + ".bak", path)
    print("SKIPPED: patched file did not compile, restored", e)
