#!/usr/bin/env python3
"""Head silhouette masks from photos with MediaPipe's selfie multiclass segmenter.

  /opt/tools/mpvenv/bin/python tools/face/segment.py img1 [img2 ...] --outdir D
Writes D/<name>_seg.png: R = hair, G = face skin, B = body skin (0/255), plus D/<name>_head.png
(hair OR face-skin = head silhouette). Classes of selfie_multiclass_256x256:
0 background, 1 hair, 2 body-skin, 3 face-skin, 4 clothes, 5 others (accessories).
"""
import argparse
import os

import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mpt
from mediapipe.tasks.python import vision
from PIL import Image

MODEL = os.environ.get("SELFIE_MULTICLASS", "/opt/tools/selfie_multiclass.tflite")

ap = argparse.ArgumentParser()
ap.add_argument("images", nargs="+")
ap.add_argument("--outdir", required=True)
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
opts = vision.ImageSegmenterOptions(base_options=mpt.BaseOptions(model_asset_path=MODEL),
                                    output_category_mask=True)
with vision.ImageSegmenter.create_from_options(opts) as seg:
    for p in a.images:
        r = seg.segment(mp.Image.create_from_file(p))
        cat = r.category_mask.numpy_view().squeeze()
        rgb = np.stack([(cat == 1), (cat == 3), (cat == 2)], -1).astype(np.uint8) * 255
        base = os.path.splitext(os.path.basename(p))[0]
        Image.fromarray(rgb).save(os.path.join(a.outdir, base + "_seg.png"))
        Image.fromarray((((cat == 1) | (cat == 3)) * 255).astype(np.uint8)).save(os.path.join(a.outdir, base + "_head.png"))
        print(base, "hair %.3f face %.3f body %.3f" % ((cat == 1).mean(), (cat == 3).mean(), (cat == 2).mean()))
