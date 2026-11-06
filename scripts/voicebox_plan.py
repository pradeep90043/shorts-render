"""
Splits a long script into short chunks and shares them across up to 20 shards, so voicebox-narration.yml can speak them on 20
runners at once (like the my-video cloud render). Chunks are whole sentences (never cut mid-sentence), about 15 s of speech
each, and stay in order: shard files are named s<shard>_c<chunk>.wav and sort back into the script.

  python3 scripts/voicebox_plan.py --script script.txt --out plan [--max-shards 20] [--shard-chars 1000] [--chunk-chars 240]
  python3 scripts/voicebox_plan.py --lines lines.json  --out plan [--max-shards 20] [--shard-chars 1000]

--lines is for a video: lines.json is [{"name": "segment_0_s01", "text": "..."}, ...], one line per scene. The lines are NOT
re-chunked (a scene is one audio file whose length sets the scene length); they are only shared out, in order, across shards.

Writes plan/shard-<n>.json (a list of {"name","text"}) and prints the shard indexes as JSON for the workflow matrix.
"""
import argparse
import json
import math
import os
import re

SENTENCE_END = re.compile(r"(?<=[।.!?॥])\s+|\n+")
SOFT_BREAK = re.compile(r"(?<=[,;:—–])\s+")


def sentences(text):
    return [s.strip() for s in SENTENCE_END.split(text) if s and s.strip()]


def split_long(sentence, limit):
    """A sentence longer than `limit` is cut at commas, then at spaces, never inside a word."""
    if len(sentence) <= limit:
        return [sentence]
    parts, cur = [], ""
    pieces = SOFT_BREAK.split(sentence)
    for piece in pieces:
        if len(piece) > limit:
            words, buf = piece.split(" "), ""
            for w in words:
                if buf and len(buf) + 1 + len(w) > limit:
                    parts.append(buf)
                    buf = w
                else:
                    buf = f"{buf} {w}".strip()
            piece = buf
        if cur and len(cur) + 1 + len(piece) > limit:
            parts.append(cur)
            cur = piece
        else:
            cur = f"{cur} {piece}".strip()
    if cur:
        parts.append(cur)
    return parts


def chunk(text, chunk_chars):
    """Whole sentences packed up to `chunk_chars`."""
    out, cur = [], ""
    for s in (p for sent in sentences(text) for p in split_long(sent, chunk_chars)):
        if cur and len(cur) + 1 + len(s) > chunk_chars:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def shard(chunks, max_shards, shard_chars, size_of=len):
    """Consecutive runs of chunks, balanced by characters; as many shards as the text needs, capped at `max_shards`."""
    total = sum(size_of(c) for c in chunks)
    n = max(1, min(max_shards, len(chunks), math.ceil(total / shard_chars)))
    target = total / n
    shards, cur, size = [], [], 0
    for i, c in enumerate(chunks):
        cur.append(c)
        size += size_of(c)
        left_chunks = len(chunks) - i - 1
        left_shards = n - len(shards) - 1
        if len(shards) < n - 1 and (size >= target or left_chunks == left_shards):
            shards.append(cur)
            cur, size = [], 0
    if cur:
        shards.append(cur)
    return shards


def plan(text, max_shards=20, shard_chars=1000, chunk_chars=240):
    shards = shard(chunk(text, chunk_chars), max_shards, shard_chars)
    return [[{"name": f"s{si:02d}_c{ci:03d}", "text": t} for ci, t in enumerate(sh)] for si, sh in enumerate(shards)]


def plan_lines(lines, max_shards=20, shard_chars=1000):
    """Explicit {"name","text"} lines (one per video scene), kept as they are and shared out in order."""
    return shard(lines, max_shards, shard_chars, size_of=lambda l: len(l["text"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script")
    ap.add_argument("--lines")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-shards", type=int, default=20)
    ap.add_argument("--shard-chars", type=int, default=1000)
    ap.add_argument("--chunk-chars", type=int, default=240)
    a = ap.parse_args()
    if a.lines:
        shards = plan_lines(json.load(open(a.lines, encoding="utf-8")), a.max_shards, a.shard_chars)
    elif a.script:
        shards = plan(open(a.script, encoding="utf-8").read(), a.max_shards, a.shard_chars, a.chunk_chars)
    else:
        ap.error("give --script or --lines")
    os.makedirs(a.out, exist_ok=True)
    for i, lines in enumerate(shards):
        with open(os.path.join(a.out, f"shard-{i}.json"), "w", encoding="utf-8") as f:
            json.dump(lines, f, ensure_ascii=False)
    print(json.dumps(list(range(len(shards)))))


if __name__ == "__main__":
    main()
