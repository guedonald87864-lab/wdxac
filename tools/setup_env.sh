#!/bin/bash
# Recreate the toolchain used for the R9 project (Linux x86-64; tested on Ubuntu 24.04 in the
# Claude Code cloud container, 2026-10-25). Everything goes to /opt/tools unless TOOLS is set.
#
#   tools/setup_env.sh
#
# Installs: Blender 4.2.23 LTS, ARM astcenc 4.8.0, MakeHuman 1.x CC0 data (pinned commit),
# a Python 3.12 venv with MediaPipe (+ face landmarker and selfie multiclass models),
# Python packages from tools/requirements.txt, EGL runtime for MediaPipe.
set -euo pipefail
TOOLS=${TOOLS:-/opt/tools}
MH_COMMIT=a8bc2d54ff0ac92e78ff71431b1023eda42bf482
mkdir -p "$TOOLS"
cd "$TOOLS"

if [ ! -x "$TOOLS/blender-4.2.23-linux-x64/blender" ]; then
  curl -sSL -o blender.tar.xz https://download.blender.org/release/Blender4.2/blender-4.2.23-linux-x64.tar.xz
  tar -xf blender.tar.xz && rm blender.tar.xz
fi
ln -sf "$TOOLS/blender-4.2.23-linux-x64/blender" /usr/local/bin/blender

if [ ! -x "$TOOLS/astc/bin/astcenc-avx2" ]; then
  curl -sSL -o astc.zip https://github.com/ARM-software/astc-encoder/releases/download/4.8.0/astcenc-4.8.0-linux-x64.zip
  python3 -c "import zipfile; zipfile.ZipFile('astc.zip').extractall('astc')" && rm astc.zip
  chmod +x astc/bin/*
fi
ln -sf "$TOOLS/astc/bin/astcenc-avx2" /usr/local/bin/astcenc

if [ ! -d "$TOOLS/mh/makehuman" ]; then
  mkdir -p mh && cd mh
  git clone -q --filter=blob:none --sparse https://github.com/makehumancommunity/makehuman.git
  cd makehuman
  git checkout -q "$MH_COMMIT"
  git sparse-checkout set makehuman/data/targets makehuman/data/3dobjs makehuman/data/rigs \
      makehuman/data/modifiers makehuman/data/eyes makehuman/data/skins
  cd "$TOOLS"
fi

apt-get install -y -q libegl1 libgles2 libgl1 > /dev/null 2>&1 || true
if [ ! -x "$TOOLS/mpvenv/bin/python" ]; then
  uv venv -p 3.12 "$TOOLS/mpvenv" -q
  VIRTUAL_ENV="$TOOLS/mpvenv" uv pip install -q mediapipe opencv-python-headless numpy
fi
[ -f face_landmarker.task ] || curl -sS -o face_landmarker.task \
  https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task
[ -f selfie_multiclass.tflite ] || curl -sS -o selfie_multiclass.tflite \
  https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_multiclass_256x256/float32/latest/selfie_multiclass_256x256.tflite

pip install -q -r "$(dirname "$0")/requirements.txt" scipy trimesh pygltflib scikit-image opencv-python-headless pytest
echo "toolchain ready in $TOOLS (MH_DATA=$TOOLS/mh/makehuman/makehuman/data)"
