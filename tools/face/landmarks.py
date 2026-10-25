#!/usr/bin/env python3
"""Detect MediaPipe FaceLandmarker 478-point landmarks on images.

Run with the mediapipe venv:  /opt/tools/mpvenv/bin/python tools/face/landmarks.py img1.png [img2 ...] --out lm.json [--overlay dir]
Output JSON: {image: {"w","h","landmarks": [[x_px, y_px, z_rel], ...] (478), "blendshapes": {...}, "matrix": 4x4}}
z_rel is MediaPipe's relative depth (same scale as x in normalized units * image width).
"""
import argparse
import json
import os

import mediapipe as mp
from mediapipe.tasks import python as mpt
from mediapipe.tasks.python import vision

MODEL = os.environ.get("FACE_LANDMARKER", "/opt/tools/face_landmarker.task")


def detect(paths, overlay=None):
    opts = vision.FaceLandmarkerOptions(
        base_options=mpt.BaseOptions(model_asset_path=MODEL),
        output_face_blendshapes=True, output_facial_transformation_matrixes=True, num_faces=1)
    res = {}
    with vision.FaceLandmarker.create_from_options(opts) as lm:
        for p in paths:
            img = mp.Image.create_from_file(p)
            r = lm.detect(img)
            if not r.face_landmarks:
                res[p] = None
                continue
            w, h = img.width, img.height
            pts = [[q.x * w, q.y * h, q.z * w] for q in r.face_landmarks[0]]
            res[p] = {"w": w, "h": h, "landmarks": pts,
                      "blendshapes": {b.category_name: float(b.score) for b in r.face_blendshapes[0]},
                      "matrix": [[float(x) for x in row] for row in r.facial_transformation_matrixes[0]]}
            if overlay:
                from PIL import Image, ImageDraw
                im = Image.open(p).convert("RGB")
                d = ImageDraw.Draw(im)
                for x, y, _ in pts:
                    d.ellipse([x - 1.5, y - 1.5, x + 1.5, y + 1.5], fill=(0, 255, 0))
                os.makedirs(overlay, exist_ok=True)
                im.save(os.path.join(overlay, os.path.basename(p)))
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--overlay")
    a = ap.parse_args()
    r = detect(a.images, a.overlay)
    json.dump(r, open(a.out, "w"))
    for k, v in r.items():
        print(k, "OK" if v else "NO FACE")
