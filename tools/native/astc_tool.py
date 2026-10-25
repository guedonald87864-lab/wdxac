#!/usr/bin/env python3
"""ASTC texture tooling for UE-style cooked mobile textures (raw block payloads, no .astc header).

Unreal stores ASTC mips as raw 16-byte blocks (no 16-byte .astc file header). This tool:
  encode   PNG -> raw ASTC mip chain (+ JSON manifest with per-mip offset/size/dims/hash)
  decode   raw ASTC mip (or .astc file) -> PNG, via the reference ARM astcenc decoder
  roundtrip PNG -> ASTC -> PNG and report PSNR per channel (quality gate)
  info     print block math for a given size/block footprint

The reference encoder/decoder is ARM astcenc (Apache-2.0), expected on PATH as `astcenc`
(tools/setup_env.sh installs 4.8.0). All payload hashes are SHA-256.
"""
import argparse
import hashlib
import json
import math
import os
import struct
import subprocess
import tempfile

import numpy as np
from PIL import Image

ASTC_MAGIC = 0x5CA1AB13
ASTCENC = os.environ.get("ASTCENC", "astcenc")


def blocks(w, h, bx, by):
    return math.ceil(w / bx), math.ceil(h / by)


def payload_size(w, h, bx, by):
    nx, ny = blocks(w, h, bx, by)
    return nx * ny * 16


def make_header(w, h, bx, by):
    def u24(v):
        return struct.pack("<I", v)[:3]
    return struct.pack("<I", ASTC_MAGIC) + bytes([bx, by, 1]) + u24(w) + u24(h) + u24(1)


def parse_header(b):
    magic, = struct.unpack("<I", b[:4])
    if magic != ASTC_MAGIC:
        raise ValueError("not an .astc file")
    bx, by, bz = b[4], b[5], b[6]
    w = int.from_bytes(b[7:10], "little")
    h = int.from_bytes(b[10:13], "little")
    return w, h, bx, by


def _run(args):
    r = subprocess.run([ASTCENC] + args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"astcenc failed: {' '.join(args)}\n{r.stdout}\n{r.stderr}")
    return r.stdout


def encode_image(img, bx, by, srgb=True, quality="-thorough", normal=False):
    """PIL image -> raw ASTC block bytes."""
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in.png")
        dst = os.path.join(td, "out.astc")
        img.save(src)
        mode = "-cs" if srgb else "-cl"
        extra = ["-normal"] if normal else []
        _run([mode, src, dst, f"{bx}x{by}", quality] + extra)
        data = open(dst, "rb").read()
    w, h, _, _ = parse_header(data)
    assert (w, h) == img.size
    raw = data[16:]
    assert len(raw) == payload_size(w, h, bx, by)
    return raw


def decode_raw(raw, w, h, bx, by, srgb=True):
    if len(raw) != payload_size(w, h, bx, by):
        raise ValueError(f"payload {len(raw)} != expected {payload_size(w, h, bx, by)} for {w}x{h} {bx}x{by}")
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in.astc")
        dst = os.path.join(td, "out.png")
        open(src, "wb").write(make_header(w, h, bx, by) + raw)
        _run(["-ds" if srgb else "-dl", src, dst])
        return Image.open(dst).convert("RGBA").copy()


def mip_chain(img, min_size=1):
    out = [img]
    while min(out[-1].size) > min_size and max(out[-1].size) > 1:
        w, h = out[-1].size
        out.append(out[-1].resize((max(1, w // 2), max(1, h // 2)), Image.Resampling.LANCZOS))
    return out


def psnr(a, b):
    a = np.asarray(a, np.float64)
    b = np.asarray(b, np.float64)
    res = {}
    for i, ch in enumerate("RGBA"[:a.shape[2]]):
        mse = np.mean((a[..., i] - b[..., i]) ** 2)
        res[ch] = float("inf") if mse == 0 else 10 * math.log10(255 ** 2 / mse)
    return res


def cmd_encode(a):
    img = Image.open(a.input).convert("RGBA")
    if a.size:
        img = img.resize((a.size, a.size), Image.Resampling.LANCZOS)
    bx, by = (int(x) for x in a.block.split("x"))
    mips = mip_chain(img) if a.mips else [img]
    os.makedirs(a.outdir, exist_ok=True)
    manifest = {"source": os.path.basename(a.input), "block": [bx, by], "srgb": not a.linear,
                "normal": a.normal, "mips": []}
    blob = bytearray()
    for i, m in enumerate(mips):
        raw = encode_image(m.convert("RGBA"), bx, by, not a.linear, a.quality, a.normal)
        manifest["mips"].append({"level": i, "w": m.width, "h": m.height, "offset": len(blob),
                                 "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        blob += raw
    base = os.path.splitext(os.path.basename(a.input))[0]
    open(os.path.join(a.outdir, base + f".astc{bx}x{by}.bin"), "wb").write(blob)
    manifest["total_size"] = len(blob)
    manifest["sha256"] = hashlib.sha256(blob).hexdigest()
    json.dump(manifest, open(os.path.join(a.outdir, base + f".astc{bx}x{by}.json"), "w"), indent=1)
    print(json.dumps({k: manifest[k] for k in ("block", "total_size", "sha256")}), len(mips), "mips")


def cmd_decode(a):
    data = open(a.input, "rb").read()
    if data[:4] == struct.pack("<I", ASTC_MAGIC):
        w, h, bx, by = parse_header(data)
        raw = data[16:]
    else:
        bx, by = (int(x) for x in a.block.split("x"))
        w, h = a.width, a.height
        raw = data[a.offset:a.offset + payload_size(w, h, bx, by)]
    decode_raw(raw, w, h, bx, by, not a.linear).save(a.output)
    print("decoded", w, h, f"{bx}x{by}", "->", a.output)


def cmd_roundtrip(a):
    img = Image.open(a.input).convert("RGBA")
    if a.size:
        img = img.resize((a.size, a.size), Image.Resampling.LANCZOS)
    bx, by = (int(x) for x in a.block.split("x"))
    raw = encode_image(img, bx, by, not a.linear, a.quality, a.normal)
    back = decode_raw(raw, img.width, img.height, bx, by, not a.linear)
    if a.output:
        back.save(a.output)
    rep = {"input": a.input, "size": img.size, "block": f"{bx}x{by}", "bytes": len(raw),
           "bpp": 8 * len(raw) / (img.width * img.height), "psnr": psnr(img, back),
           "sha256": hashlib.sha256(raw).hexdigest()}
    print(json.dumps(rep))
    return rep


def cmd_info(a):
    bx, by = (int(x) for x in a.block.split("x"))
    w, h = a.width, a.height
    print(json.dumps({"blocks": blocks(w, h, bx, by), "payload": payload_size(w, h, bx, by),
                      "bpp": 128 / (bx * by)}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name in ("encode", "roundtrip"):
        p = sp.add_parser(name)
        p.add_argument("input")
        p.add_argument("--block", default="4x4")
        p.add_argument("--size", type=int)
        p.add_argument("--linear", action="store_true")
        p.add_argument("--normal", action="store_true")
        p.add_argument("--quality", default="-thorough")
        if name == "encode":
            p.add_argument("--outdir", required=True)
            p.add_argument("--mips", action="store_true")
        else:
            p.add_argument("--output")
    p = sp.add_parser("decode")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--block", default="4x4")
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--linear", action="store_true")
    p = sp.add_parser("info")
    p.add_argument("--block", default="4x4")
    p.add_argument("--width", type=int, required=True)
    p.add_argument("--height", type=int, required=True)
    a = ap.parse_args()
    {"encode": cmd_encode, "decode": cmd_decode, "roundtrip": cmd_roundtrip, "info": cmd_info}[a.cmd](a)
