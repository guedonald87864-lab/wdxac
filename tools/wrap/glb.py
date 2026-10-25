"""Minimal GLB (binary glTF 2.0) reader/writer, numpy only (works inside Blender too).

Reader: returns every mesh primitive's attributes exactly as stored (vertex ORDER is the
accessor order - nothing is welded, re-indexed or re-ordered), plus node world matrices and
skins, so wrap.py can treat a game-exported GLB as a donor and write a patched copy back.
Writer: builds a GLB from plain arrays; `patch_glb()` rewrites selected accessors of an existing
GLB in place-compatible fashion (new buffer, everything else - skins, joints, materials - kept).
Not supported: sparse accessors, Draco/meshopt compression, external .bin URIs (error raised).
"""
import json
import struct

import numpy as np

CT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
NORM_DIV = {5120: 127.0, 5121: 255.0, 5122: 32767.0, 5123: 65535.0}


def read_glb_raw(path):
    data = open(path, "rb").read()
    magic, ver, length = struct.unpack("<III", data[:12])
    if magic != 0x46546C67:
        raise ValueError(f"{path}: not a GLB")
    off = 12
    js, binc = None, b""
    while off < length:
        clen, ctype = struct.unpack("<II", data[off:off + 8])
        chunk = data[off + 8:off + 8 + clen]
        if ctype == 0x4E4F534A:
            js = json.loads(chunk.decode("utf8"))
        elif ctype == 0x004E4942:
            binc = chunk
        off += 8 + clen
    return js, binc


def accessor(js, binc, idx):
    a = js["accessors"][idx]
    if "sparse" in a:
        raise NotImplementedError("sparse accessors are not supported")
    n, nc = a["count"], NCOMP[a["type"]]
    dt = np.dtype(CT[a["componentType"]])
    if "bufferView" not in a:
        return np.zeros((n, nc) if nc > 1 else n, dt)
    bv = js["bufferViews"][a["bufferView"]]
    if bv.get("buffer", 0) != 0 or "uri" in js["buffers"][bv.get("buffer", 0)]:
        raise NotImplementedError("external buffers are not supported")
    start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = bv.get("byteStride", 0) or dt.itemsize * nc
    raw = np.frombuffer(binc, np.uint8, count=stride * (n - 1) + dt.itemsize * nc, offset=start)
    if stride == dt.itemsize * nc:
        arr = raw.view(dt).reshape(n, nc).copy()
    else:
        arr = np.lib.stride_tricks.as_strided(raw, (n, dt.itemsize * nc), (stride, 1)).copy().view(dt).reshape(n, nc)
    if a.get("normalized") and a["componentType"] in NORM_DIV:
        arr = np.maximum(arr.astype(np.float64) / NORM_DIV[a["componentType"]], -1.0)
    return arr if nc > 1 else arr[:, 0]


def node_world_matrices(js):
    nodes = js.get("nodes", [])
    local = []
    for nd in nodes:
        if "matrix" in nd:
            M = np.array(nd["matrix"], float).reshape(4, 4).T
        else:
            T = np.eye(4)
            T[:3, 3] = nd.get("translation", [0, 0, 0])
            x, y, z, w = nd.get("rotation", [0, 0, 0, 1])
            R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                          [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                          [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
            S = np.diag(list(nd.get("scale", [1, 1, 1])) + [1])
            Rm = np.eye(4)
            Rm[:3, :3] = R
            M = T @ Rm @ S
        local.append(M)
    parent = {}
    for i, nd in enumerate(nodes):
        for c in nd.get("children", []):
            parent[c] = i
    world = [None] * len(nodes)

    def get(i):
        if world[i] is None:
            world[i] = local[i] if i not in parent else get(parent[i]) @ local[i]
        return world[i]
    return [get(i) for i in range(len(nodes))]


def read_glb(path):
    """-> dict(meshes=[{name, node, world, skin, primitives=[{attributes, indices, accessor ids}]}], json, bin)."""
    js, binc = read_glb_raw(path)
    world = node_world_matrices(js)
    users = {}
    for i, nd in enumerate(js.get("nodes", [])):
        if "mesh" in nd:
            users.setdefault(nd["mesh"], (i, nd.get("skin")))
    meshes = []
    for mi, m in enumerate(js.get("meshes", [])):
        node, skin = users.get(mi, (None, None))
        prims = []
        for p in m["primitives"]:
            if p.get("mode", 4) != 4:
                raise NotImplementedError("only TRIANGLES primitives are supported")
            attrs = {k: accessor(js, binc, v) for k, v in p["attributes"].items()}
            n = len(attrs["POSITION"])
            idx = accessor(js, binc, p["indices"]).astype(np.int64) if "indices" in p else np.arange(n)
            prims.append({"attributes": attrs, "faces": idx.reshape(-1, 3),
                          "accessors": dict(p["attributes"]), "indices_accessor": p.get("indices")})
        meshes.append({"name": m.get("name", f"mesh{mi}"), "index": mi, "node": node, "skin": skin,
                       "world": world[node] if node is not None else np.eye(4), "primitives": prims})
    skins = []
    for s in js.get("skins", []):
        ibm = accessor(js, binc, s["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1) \
            if "inverseBindMatrices" in s else np.tile(np.eye(4), (len(s["joints"]), 1, 1))
        skins.append({"joints": s["joints"], "ibm": ibm,
                      "joint_world": np.array([world[j] for j in s["joints"]])})
    return {"meshes": meshes, "skins": skins, "json": js, "bin": binc}


# ----------------------------------------------------------------------------- writer
class _Builder:
    def __init__(self):
        self.bin = bytearray()
        self.views, self.accessors = [], []

    def add(self, arr, ctype, atype, target=None, minmax=False, normalized=False):
        arr = np.ascontiguousarray(arr, CT[ctype])
        while len(self.bin) % 4:
            self.bin.append(0)
        view = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": arr.nbytes}
        if target:
            view["target"] = target
        self.bin += arr.tobytes()
        self.views.append(view)
        acc = {"bufferView": len(self.views) - 1, "componentType": ctype,
               "count": int(arr.shape[0]), "type": atype}
        if normalized:
            acc["normalized"] = True
        if minmax:
            a2 = arr.reshape(arr.shape[0], -1)
            acc["min"] = a2.min(0).astype(float).tolist()
            acc["max"] = a2.max(0).astype(float).tolist()
        self.accessors.append(acc)
        return len(self.accessors) - 1


def _pack(js, binc):
    jb = json.dumps(js, separators=(",", ":")).encode("utf8")
    jb += b" " * (-len(jb) % 4)
    binc = bytes(binc) + b"\0" * (-len(binc) % 4)
    total = 12 + 8 + len(jb) + (8 + len(binc) if binc else 0)
    out = struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(jb), 0x4E4F534A) + jb
    if binc:
        out += struct.pack("<II", len(binc), 0x004E4942) + binc
    return out


def write_glb(path, meshes, extras=None):
    """meshes: list of dict(name, positions (N,3), faces (F,3), normals?, tangents? (N,4),
    uv0? (N,2) glTF convention (top-left origin), extra_attrs? {'_NAME': (N,) or (N,k) float32},
    joints? (N,4) uint16 + weights? (N,4) float -> written as custom _JOINTS_0/_WEIGHTS_0 because no
    skin/joint nodes are written here). Every vertex is written in the given order."""
    b = _Builder()
    js = {"asset": {"version": "2.0", "generator": "wdxac tools/wrap/glb.py"}, "scenes": [{"nodes": []}],
          "scene": 0, "nodes": [], "meshes": []}
    for m in meshes:
        attrs = {"POSITION": b.add(m["positions"], 5126, "VEC3", 34962, minmax=True)}
        if m.get("normals") is not None:
            attrs["NORMAL"] = b.add(m["normals"], 5126, "VEC3", 34962)
        if m.get("tangents") is not None:
            attrs["TANGENT"] = b.add(m["tangents"], 5126, "VEC4", 34962)
        if m.get("uv0") is not None:
            attrs["TEXCOORD_0"] = b.add(m["uv0"], 5126, "VEC2", 34962)
        if m.get("joints") is not None:
            attrs["_JOINTS_0"] = b.add(np.asarray(m["joints"], np.float32), 5126, "VEC4", 34962)
            attrs["_WEIGHTS_0"] = b.add(np.asarray(m["weights"], np.float32), 5126, "VEC4", 34962)
        for k, v in (m.get("extra_attrs") or {}).items():
            v = np.asarray(v, np.float32)
            attrs[k] = b.add(v, 5126, {1: "SCALAR", 2: "VEC2", 3: "VEC3", 4: "VEC4"}[1 if v.ndim == 1 else v.shape[1]], 34962)
        ind = b.add(np.asarray(m["faces"], np.uint32).ravel(), 5125, "SCALAR", 34963)
        js["meshes"].append({"name": m["name"], "primitives": [{"attributes": attrs, "indices": ind, "mode": 4}]})
        js["nodes"].append({"name": m["name"], "mesh": len(js["meshes"]) - 1})
        js["scenes"][0]["nodes"].append(len(js["nodes"]) - 1)
    js["bufferViews"], js["accessors"] = b.views, b.accessors
    js["buffers"] = [{"byteLength": len(b.bin)}]
    if extras:
        js["asset"]["extras"] = extras
    open(path, "wb").write(_pack(js, b.bin))


def patch_glb(src, dst, replacements, extras=None):
    """Copy GLB `src` to `dst`, replacing the data of accessors {accessor_index: array}.
    The replaced accessors get fresh buffer views appended to the binary chunk (min/max refreshed
    when the original had them); every other accessor, node, skin, material and image is carried
    over byte-identical."""
    js, binc = read_glb_raw(src)
    b = _Builder()
    b.bin = bytearray(binc)
    b.views = list(js.get("bufferViews", []))
    for ai, arr in replacements.items():
        a = js["accessors"][ai]
        arr = np.asarray(arr)
        target = js["bufferViews"][a["bufferView"]].get("target") if "bufferView" in a else None
        b.add(arr.reshape(a["count"], -1) if NCOMP[a["type"]] > 1 else arr.reshape(-1), a["componentType"],
              a["type"], target, minmax=("min" in a), normalized=a.get("normalized", False))
        new = b.accessors[-1]
        a2 = dict(a)
        a2["bufferView"] = new["bufferView"]
        a2.pop("byteOffset", None)
        if "min" in new:
            a2["min"], a2["max"] = new["min"], new["max"]
        js["accessors"][ai] = a2
    js["bufferViews"] = b.views
    js["buffers"] = [dict(js["buffers"][0], byteLength=len(b.bin))]
    if extras:
        js.setdefault("asset", {}).setdefault("extras", {}).update(extras)
    open(dst, "wb").write(_pack(js, b.bin))
