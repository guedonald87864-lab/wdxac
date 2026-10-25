"""Neutral mesh interchange for the donor-wrap tools (numpy only: importable inside Blender too).

Coordinate frames
-----------------
Every array written by tools/wrap is tagged with a *frame*. The internal working frame is
Blender's world frame of the R9 build ("blender"): right-handed, metres, +Z up, the face looks
along -Y, the subject's LEFT side is +X. A frame is a linear map  p_frame = scale * A @ p_blender
with A a signed permutation (det(A) = +1 right-handed, -1 left-handed):

  blender   metres, Z-up, face -Y, subject-left +X          (R9 build frame, right-handed)
  gltf      metres, Y-up, face +Z, subject-left +X          (glTF 2.0 asset convention, RH)
  ue        centimetres, Z-up, face +X, subject-right +Y    (UE world axes "X forward", LH)
  ue_yfwd   centimetres, Z-up, face +Y, subject-left +X     (typical UE skeletal-mesh asset: the
            mannequin faces +Y in its own space; LH)
  custom    "--axes '-y,-x,z' --scale 100" : output axis k = signed blender axis

Winding: when det(A) < 0 the triangle index order is reversed on export so that the
right-hand-rule cross product (b-a)x(c-a) still points OUT of the surface numerically in the new
frame. Readers never rely on winding: wrap.py re-checks the orientation of every input mesh.

UV convention: arrays named ``uv*`` store (u, v) with v measured from the BOTTOM of the image
(Blender/OpenGL). ``meta['uv_origin']`` says 'bottom-left' or 'top-left' (glTF/DirectX/UE); use
uv_to_bottom_left() before sampling images.
"""
import json

import numpy as np

FRAMES = {
    # name: (A rows = output axes expressed in blender axes, scale = units per metre)
    "blender": (np.eye(3), 1.0),
    "gltf": (np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], float), 1.0),
    "ue": (np.array([[0, -1, 0], [-1, 0, 0], [0, 0, 1]], float), 100.0),
    "ue_yfwd": (np.array([[1, 0, 0], [0, -1, 0], [0, 0, 1]], float), 100.0),
}


def parse_axes(spec):
    """'-y,-x,z' -> 3x3 signed permutation (output axis k = signed blender axis)."""
    rows = []
    for tok in spec.split(","):
        tok = tok.strip().lower()
        sgn = -1.0 if tok.startswith("-") else 1.0
        ax = "xyz".index(tok.lstrip("+-"))
        r = np.zeros(3)
        r[ax] = sgn
        rows.append(r)
    A = np.array(rows)
    if abs(abs(np.linalg.det(A)) - 1) > 1e-9:
        raise ValueError(f"axes spec {spec!r} is not a signed permutation")
    return A


def frame_def(name="blender", axes=None, scale=None):
    """Return dict(name, A (3x3), scale, det, units_m) for a preset or custom frame."""
    if axes:
        A = parse_axes(axes)
        s = float(scale) if scale else 1.0
        name = "custom"
    else:
        if name not in FRAMES:
            raise KeyError(f"unknown frame {name!r}; presets: {sorted(FRAMES)}")
        A, s = FRAMES[name]
        if scale:
            s = float(scale)
    return {"name": name, "A": np.asarray(A, float).tolist(), "scale": s,
            "det": int(round(np.linalg.det(A))), "units_m": 1.0 / s}


def _A(fd):
    return np.asarray(fd["A"], float), float(fd["scale"])


def blender_to_frame(P, fd):
    A, s = _A(fd)
    return s * (np.asarray(P, float) @ A.T)


def frame_to_blender(P, fd):
    A, s = _A(fd)
    return (np.asarray(P, float) @ A) / s     # A orthogonal: inverse = A.T


def dir_blender_to_frame(N, fd):
    """Directions/normals (unit) - no scale; reflection handled geometrically (outward stays out)."""
    A, _ = _A(fd)
    return np.asarray(N, float) @ A.T


def dir_frame_to_blender(N, fd):
    A, _ = _A(fd)
    return np.asarray(N, float) @ A


def faces_for_frame(F, fd):
    """Reverse winding for left-handed frames (see module doc)."""
    F = np.asarray(F)
    return F[:, ::-1].copy() if fd["det"] < 0 else F.copy()


def uv_to_bottom_left(uv, origin):
    uv = np.array(uv, np.float64, copy=True)
    if origin == "top-left":
        uv[..., 1] = 1.0 - uv[..., 1]
    elif origin != "bottom-left":
        raise ValueError(f"unknown uv origin {origin!r}")
    return uv


def uv_from_bottom_left(uv, origin):
    return uv_to_bottom_left(uv, origin)     # the flip is an involution


# ----------------------------------------------------------------------------- geometry basics
def tri_normals(V, F, normalize=True):
    V = np.asarray(V, float)
    n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    if normalize:
        n = n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-30)
    return n


def vertex_normals(V, F, weighting="angle"):
    """Smooth vertex normals. weighting 'angle' (default, Blender-like) or 'area'."""
    V = np.asarray(V, float)
    F = np.asarray(F)
    fn = tri_normals(V, F, normalize=False)
    if weighting == "area":
        w = np.ones((len(F), 3))
        fnu = fn
    else:
        fnu = fn / np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-30)
        w = np.zeros((len(F), 3))
        for k in range(3):
            a = V[F[:, (k + 1) % 3]] - V[F[:, k]]
            b = V[F[:, (k + 2) % 3]] - V[F[:, k]]
            cos = (a * b).sum(1) / np.maximum(np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1), 1e-30)
            w[:, k] = np.arccos(np.clip(cos, -1, 1))
    out = np.zeros_like(V)
    for k in range(3):
        np.add.at(out, F[:, k], fnu * w[:, k:k + 1])
    nrm = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.maximum(nrm, 1e-30)


def bounds(V):
    V = np.asarray(V, float)
    mn, mx = V.min(0), V.max(0)
    c = (mn + mx) / 2
    return {"min": mn.tolist(), "max": mx.tolist(), "center": c.tolist(),
            "extent": ((mx - mn) / 2).tolist(),
            "sphere_radius": float(np.linalg.norm(V - c, axis=1).max())}


# ----------------------------------------------------------------------------- npz io
def save_npz(path, arrays, meta):
    out = {k: np.asarray(v) for k, v in arrays.items()}
    out["meta"] = np.array(json.dumps(meta, sort_keys=True))
    np.savez_compressed(path, **out)


def load_npz(path):
    z = np.load(path, allow_pickle=False)
    arrays = {k: z[k] for k in z.files if k != "meta"}
    meta = json.loads(str(z["meta"])) if "meta" in z.files else {}
    return arrays, meta
