"""Pure parts of scripts/member_transcribe.py (the model itself only runs on the runner)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import member_transcribe as mt  # noqa: E402

W = lambda t, s, e: {"word": t, "start": s, "end": e}
# a sentence, a pause, then another sentence
words = [W(" Hello", 0, .4), W(" there", .4, .8), W(" friends.", .8, 1.3), W(" Today", 3.0, 3.3), W(" we", 3.3, 3.5), W(" learn", 3.5, 3.9), W(" about", 3.9, 4.2), W(" cats", 4.2, 4.6)]
lines = mt.lines_from_words(words)
assert [l["text"] for l in lines] == ["Hello there friends.", "Today we learn about cats"], lines
assert lines[0]["start"] == 0 and lines[0]["end"] == 1.3

# long runs are split by word count and by length
many = [W(f" w{i}", i * .3, i * .3 + .25) for i in range(20)]
ls = mt.lines_from_words(many)
assert all(len(l["text"].split()) <= 7 for l in ls) and sum(len(l["text"].split()) for l in ls) == 20, ls
# Hindi sentence end (danda) closes a line
hi = [W(" नमस्ते", 0, .5), W(" दोस्तों।", .5, 1.0), W(" आज", 1.1, 1.4), W(" का", 1.4, 1.6), W(" विषय", 1.6, 2.0)]
assert [l["text"] for l in mt.lines_from_words(hi)] == ["नमस्ते दोस्तों।", "आज का विषय"]
assert mt.lines_from_words([]) == []
print("ok")
