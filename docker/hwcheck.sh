#!/usr/bin/env sh
set -eu

echo "== MediaShrinker hardware check =="
echo

echo "FFmpeg:"
ffmpeg -hide_banner -version | head -n 1 || true
echo

echo "Available HEVC encoders:"
ffmpeg -hide_banner -encoders 2>/dev/null | grep -E 'hevc_(nvenc|qsv|vaapi)|libx265' || true
echo

echo "NVIDIA:"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi || true
elif [ -e /dev/nvidia0 ] || [ -e /proc/driver/nvidia/version ]; then
  echo "NVIDIA device files are visible, but nvidia-smi is not installed in the image."
else
  echo "No NVIDIA device visible."
fi
echo

echo "VAAPI / Intel / AMD:"
if [ -e /dev/dri ]; then
  ls -la /dev/dri || true
  if command -v vainfo >/dev/null 2>&1; then
    vainfo 2>/dev/null | sed -n '1,20p' || true
  fi
else
  echo "No /dev/dri device visible."
fi
echo

echo "Recommended encoder:"
if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'hevc_nvenc' && { [ -e /dev/nvidia0 ] || [ -e /proc/driver/nvidia/version ]; }; then
  echo "MEDIA_ENCODER=hevc_nvenc"
elif ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'libx265'; then
  echo "MEDIA_ENCODER=libx265"
else
  echo "No supported HEVC encoder found."
  exit 1
fi
