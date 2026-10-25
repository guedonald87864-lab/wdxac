#!/bin/bash
# Reproducible build of every R9 3D deliverable from the parameter files.
#
#   tools/build_all.sh [--quick]        (START=N tools/build_all.sh resumes at step N)
#
# Inputs : model/source/r9_params.json (+ r9_hair_params.json, r9_teeth_params.json,
#          r9_skin_palette.json, face_corr_mediapipe478.json), MakeHuman CC0 data (see
#          tools/setup_env.sh)
# Outputs: model/source/r9_2002.blend           editable rigged character (head, body, eyes, teeth, hair)
#          model/source/r9_2002_kit_preview.blend  + 2002 final kit preview shells
#          model/export/r9_2002_hd.glb          GLB with skin + armature (HD, MakeHuman rig)
#          model/textures/*.png                 2048 head albedo/normal/rough/masks, body albedo, hair
#          renders/hd/*.png                     head 7 views + body 4 views (kit) at HD precision
#          data/body/r9_measurements.json       measured cm/kg of the built model
#          card/layers/*.png, card/out/*          card portrait layers and composited card
set -euo pipefail
R=$(cd "$(dirname "$0")/.." && pwd)
cd "$R"
SIZE=2048; SAMPLES=48; RES=1000; CARD=1536; CARD_SAMPLES=64
if [[ "${1:-}" == "--quick" ]]; then SIZE=512; SAMPLES=8; RES=400; CARD=512; CARD_SAMPLES=8; fi
mkdir -p model/source model/export model/textures renders/hd
START=${START:-1}
step() { [[ "$1" -ge "$START" ]]; }

if step 1; then
echo "[1/7] build rigged character"
blender -b --python tools/model/blender_build.py -- --params model/source/r9_params.json \
  --out model/source/r9_2002.blend --glb model/export/r9_2002_hd.glb > /tmp/r9_build.log 2>&1 || { tail -30 /tmp/r9_build.log; exit 1; }
sed -n 's/^R9_BUILD_META \({.*}\).*$/\1/p' /tmp/r9_build.log | head -1 > model/export/build_meta.json
fi

if step 2; then
echo "[2/7] procedural textures ($SIZE px)"
blender -b model/source/r9_2002.blend --python tools/model/texture_build.py -- \
  --corr model/source/face_corr_mediapipe478.json --palette model/source/r9_skin_palette.json \
  --out model/textures --size "$SIZE" > /tmp/r9_tex.log 2>&1 || { tail -30 /tmp/r9_tex.log; exit 1; }
fi

if step 3; then
echo "[3/7] kit preview"
blender -b model/source/r9_2002.blend --python tools/model/kit_preview.py -- \
  --out model/source/r9_2002_kit_preview.blend > /tmp/r9_kit.log 2>&1 || { tail -30 /tmp/r9_kit.log; exit 1; }
fi

if step 4; then
echo "[4/7] head renders"
blender -b model/source/r9_2002.blend --python tools/model/blender_render.py -- --outdir renders/hd \
  --views head_front,head_l45,head_r45,head_lprof,head_rprof,head_back,head_top,head_low \
  --res "$RES" --samples "$SAMPLES" --textures model/textures > /tmp/r9_render.log 2>&1 || { tail -30 /tmp/r9_render.log; exit 1; }
fi

if step 5; then
echo "[5/7] body renders (kit preview)"
blender -b model/source/r9_2002_kit_preview.blend --python tools/model/blender_render.py -- --outdir renders/hd \
  --views body_front,body_side,body_back,body_34 --res "$RES" --samples "$SAMPLES" --textures model/textures \
  > /tmp/r9_render2.log 2>&1 || { tail -30 /tmp/r9_render2.log; exit 1; }
fi

if step 6; then
echo "[6/7] measurements"
python3 tools/model/body_fit.py --params model/source/r9_params.json --measure > data/body/r9_measurements.json
fi

if step 7; then
echo "[7/7] card"
blender -b model/source/r9_2002_kit_preview.blend --python tools/card/render_portrait.py -- --outdir card/layers \
  --textures model/textures --size "$CARD" --samples "$CARD_SAMPLES" > /tmp/r9_card.log 2>&1 || { tail -30 /tmp/r9_card.log; exit 1; }
python3 tools/card/compose_card.py --portrait card/layers/portrait_34.png --player data/game/r9_2002_player.json \
  --outdir card/out
fi

echo "done: $(ls renders/hd | wc -l) renders, textures in model/textures, GLB in model/export"
