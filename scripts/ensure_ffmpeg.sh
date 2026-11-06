#!/bin/bash
# Makes sure ffmpeg and ffprobe exist on a GitHub runner. The ubuntu runner image has no ffmpeg.
#
# History: a bare `apt-get install ffmpeg` hung for 18 minutes on one runner of twenty and stalled a 20-minute tutorial; adding
# timeouts and retries (3 x ~4 min) was not enough: on the next run two runners failed the same way at the same moment, so the
# package mirror simply does not answer when many runners install at once. So the primary source is now a static build from
# GitHub's own release storage (BtbN/FFmpeg-Builds), verified against the checksum file published with it; apt is the fallback.
#
# Test hooks: FFMPEG_BIN_DIR (default /usr/local/bin) and FFMPEG_SUDO (default sudo; empty to run without).
set -u
BIN_DIR="${FFMPEG_BIN_DIR:-/usr/local/bin}"
SUDO="${FFMPEG_SUDO-sudo}"
BASE="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
ARCHIVE="ffmpeg-master-latest-linux64-gpl.tar.xz"

have() { command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; }

static_build() {
  local tmp
  tmp="$(mktemp -d)"
  echo "ffmpeg missing: downloading the static build from GitHub releases"
  curl -fsSL --retry 5 --retry-delay 3 --connect-timeout 20 --max-time 300 -o "$tmp/$ARCHIVE" "$BASE/$ARCHIVE" || { echo "download failed"; rm -rf "$tmp"; return 1; }
  curl -fsSL --retry 5 --retry-delay 3 --connect-timeout 20 --max-time 60 -o "$tmp/checksums.sha256" "$BASE/checksums.sha256" || { echo "checksum file download failed"; rm -rf "$tmp"; return 1; }
  local want got
  want="$(grep " $ARCHIVE\$" "$tmp/checksums.sha256" | awk '{print $1}')"
  got="$( (sha256sum "$tmp/$ARCHIVE" 2>/dev/null || shasum -a 256 "$tmp/$ARCHIVE") | awk '{print $1}')"
  if [ -z "$want" ] || [ "$want" != "$got" ]; then echo "checksum mismatch (want '$want', got '$got')"; rm -rf "$tmp"; return 1; fi
  local dir="${ARCHIVE%.tar.xz}"
  tar -xJf "$tmp/$ARCHIVE" -C "$tmp" "$dir/bin/ffmpeg" "$dir/bin/ffprobe" || { echo "unpack failed"; rm -rf "$tmp"; return 1; }
  $SUDO mkdir -p "$BIN_DIR"
  $SUDO install -m 0755 "$tmp/$dir/bin/ffmpeg" "$tmp/$dir/bin/ffprobe" "$BIN_DIR/" || { rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
  export PATH="$BIN_DIR:$PATH"
  have
}

have && exit 0
static_build && { echo "ffmpeg ready (static build)"; exit 0; }

# fallback: apt, with a time limit and a retry on every call
APT=($SUDO apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 -o DPkg::Lock::Timeout=60)
for attempt in 1 2; do
  have && exit 0
  echo "falling back to apt (attempt $attempt/2)"
  timeout 120 "${APT[@]}" update -qq || echo "apt update did not finish"
  timeout 180 "${APT[@]}" install -y -qq ffmpeg && have && exit 0
  sleep 5
done
echo "could not get ffmpeg" >&2
exit 1
