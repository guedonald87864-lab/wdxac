#!/bin/bash
# Rebuild the model with the current hair params and compare at the fitted photo cameras.
# usage: hair_iterate.sh <params.json> <cams.json> <lm.json> <photos dir> <workdir> <tag>
set -e
P=$1; CAMS=$2; LM=$3; PH=$4; W=$5; TAG=$6
R=$(cd "$(dirname "$0")/../.." && pwd)
mkdir -p "$W"
blender -b --python "$R/tools/model/blender_build.py" -- --params "$P" --out "$W/$TAG.blend" > /dev/null 2>&1
blender -b "$W/$TAG.blend" --python "$R/tools/model/texture_build.py" -- --corr "$R/model/source/face_corr_mediapipe478.json" --palette "$R/model/source/r9_skin_palette.json" --out "$W/tex_$TAG" --size 512 > /dev/null 2>&1
python3 -c "
import json,sys; c=[x for x in json.load(open('$CAMS')) if x['name'] in ('g890017.png','g891445.png','g78971169.png','g923205.png')]; json.dump(c,open('$W/cams_sub.json','w'))"
blender -b "$W/$TAG.blend" --python "$R/tools/face/blender_render_cams.py" -- --cams "$W/cams_sub.json" --outdir "$W/rv_$TAG" --textures "$W/tex_$TAG" --samples 8 > /dev/null 2>&1
python3 "$R/tools/review/photo_compare.py" --lm "$LM" --photos "$PH" --renders "$W/rv_$TAG" --out "$W/cmp_$TAG.png" --names g890017.png,g891445.png,g78971169.png,g923205.png --tile 200
