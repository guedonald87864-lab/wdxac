"""Unit tests for tools/wrap (fast, < 1 min): geometry kernels, frames, GLB io, NICP on an
analytic shape, LOD transfer, texture sampling/baking conventions, colour difference, tangents."""
import os
import shutil
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "wrap"))
import geom as G  # noqa: E402
import glb as GLB  # noqa: E402
import meshdata as MD  # noqa: E402
import transfer_texture as TT  # noqa: E402
import wrap as WR  # noqa: E402


def icosphere(level=3, r=1.0):
    t = (1 + 5 ** 0.5) / 2
    V = [[-1, t, 0], [1, t, 0], [-1, -t, 0], [1, -t, 0], [0, -1, t], [0, 1, t], [0, -1, -t], [0, 1, -t],
         [t, 0, -1], [t, 0, 1], [-t, 0, -1], [-t, 0, 1]]
    F = [[0, 11, 5], [0, 5, 1], [0, 1, 7], [0, 7, 10], [0, 10, 11], [1, 5, 9], [5, 11, 4], [11, 10, 2], [10, 7, 6],
         [7, 1, 8], [3, 9, 4], [3, 4, 2], [3, 2, 6], [3, 6, 8], [3, 8, 9], [4, 9, 5], [2, 4, 11], [6, 2, 10],
         [8, 6, 7], [9, 8, 1]]
    V = [np.array(v, float) / np.linalg.norm(v) for v in V]
    for _ in range(level):
        cache, F2 = {}, []

        def mid(a, b):
            k = (min(a, b), max(a, b))
            if k not in cache:
                m = V[a] + V[b]
                V.append(m / np.linalg.norm(m))
                cache[k] = len(V) - 1
            return cache[k]
        for a, b, c in F:
            ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
            F2 += [[a, ab, ca], [b, bc, ab], [c, ca, bc], [ab, bc, ca]]
        F = F2
    return np.array(V) * r, np.array(F)


def brute_closest(P, V, F):
    T = V[F]
    best = np.full(len(P), np.inf)
    for i, p in enumerate(P):
        Q, _ = G.closest_on_triangles(np.repeat(p[None], len(F), 0), T[:, 0], T[:, 1], T[:, 2])
        best[i] = np.linalg.norm(Q - p, axis=1).min()
    return best


def test_closest_point_is_exact_and_flags_border():
    V, F = icosphere(2, 0.1)
    keep = V[F].mean(1)[:, 2] > -0.03                  # open the sphere: one border loop
    F = F[keep]
    rng = np.random.default_rng(0)
    P = rng.normal(size=(400, 3)) * 0.08
    q = G.TriMeshQuery(V, F)
    r = q.closest(P)
    assert np.allclose(r["dist"], brute_closest(P, V, F), atol=1e-12)
    below = np.array([[0, 0, -0.2]])                   # nearest point is on the rim -> border
    assert q.closest(below)["border"][0]
    assert not q.closest(np.array([[0, 0, 0.2]]))["border"][0]
    loops = G.boundary_loops(F, len(V))
    assert len(loops) == 1


def test_weld_groups_gpu_duplicates():
    V, F = icosphere(1)
    dup = np.concatenate([V, V[:5]])                   # 5 seam duplicates
    wid, rep = G.weld(dup)
    assert len(rep) == len(V)
    assert np.array_equal(wid[len(V):], wid[:5])


@pytest.mark.parametrize("name", ["blender", "gltf", "ue", "ue_yfwd"])
def test_frames_roundtrip_and_winding(name):
    V, F = icosphere(1, 0.1)
    fd = MD.frame_def(name)
    P = MD.blender_to_frame(V + [0.01, 0.02, 1.5], fd)
    assert np.allclose(MD.frame_to_blender(P, fd), V + [0.01, 0.02, 1.5])
    assert G.orientation_sign(P, MD.faces_for_frame(F, fd)) > 0   # cross products still point outward
    c = MD.frame_def(axes="-y,-x,z", scale=100)
    assert c["det"] == -1 and np.allclose(np.asarray(c["A"]), np.asarray(MD.frame_def("ue")["A"]))


def test_umeyama_recovers_similarity():
    rng = np.random.default_rng(1)
    X = rng.normal(size=(12, 3))
    a = 0.4
    R = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    Y = 1.7 * X @ R.T + [1, 2, 3]
    s, R2, t = G.umeyama(X, Y)
    assert abs(s - 1.7) < 1e-9 and np.allclose(R2, R) and np.allclose(t, [1, 2, 3])


def test_glb_write_read_patch(tmp_path):
    V, F = icosphere(1)
    uv = np.c_[V[:, 0] * 0.5 + 0.5, V[:, 1] * 0.5 + 0.5]
    J = np.tile([0, 1, 2, 3], (len(V), 1))
    Wt = np.tile([0.4, 0.3, 0.2, 0.1], (len(V), 1))
    p = tmp_path / "m.glb"
    GLB.write_glb(str(p), [{"name": "lod0", "positions": V, "faces": F, "normals": V, "uv0": uv,
                            "joints": J, "weights": Wt}])
    g = GLB.read_glb(str(p))
    pr = g["meshes"][0]["primitives"][0]
    assert np.allclose(pr["attributes"]["POSITION"], V, atol=1e-6)
    assert np.array_equal(pr["faces"], F)
    assert np.allclose(pr["attributes"]["TEXCOORD_0"], uv, atol=1e-6)
    p2 = tmp_path / "m2.glb"
    GLB.patch_glb(str(p), str(p2), {pr["accessors"]["POSITION"]: (V * 2).astype(np.float32)})
    g2 = GLB.read_glb(str(p2))
    pr2 = g2["meshes"][0]["primitives"][0]
    assert np.allclose(pr2["attributes"]["POSITION"], V * 2, atol=1e-6)
    assert np.allclose(pr2["attributes"]["TEXCOORD_0"], uv, atol=1e-6)     # untouched accessor kept
    assert np.array_equal(pr2["faces"], F)


def test_nicp_open_sphere_onto_ellipsoid_keeps_locked_ring():
    V, F = icosphere(3, 0.1)
    keep = V[F].mean(1)[:, 2] > -0.06
    F = F[keep]
    used = np.unique(F)
    remap = -np.ones(len(V), int)
    remap[used] = np.arange(len(used))
    V, F = V[used], remap[F]
    adj, edges = G.vertex_adjacency(F, len(V))
    lock = np.zeros(len(V), bool)
    lock[G.boundary_loops(F, len(V))[0]] = True
    lock = G.grow(lock, adj, 1)
    Vt, Ft = icosphere(4, 0.1)
    Vt = Vt * [1.15, 0.95, 1.05]
    Vt[:, 2] += 0.004 * np.sin(Vt[:, 0] * 60)          # some shape detail
    tq = G.TriMeshQuery(Vt, Ft)
    Nt = MD.vertex_normals(Vt, Ft)
    out, hist, _ = WR.nicp(V, F, edges, tq, Nt, {"schedule": [20, 5, 2, 1], "max_inner": 4}, locked=lock,
                           verbose=False)
    out, _ = WR.refine_projection(out, F, edges, V, tq, Nt, lock, ~lock, {"refine_iters": 2}, verbose=False)
    free = ~lock
    d = tq.closest(out[free & (V[:, 2] > -0.03)])["dist"]
    assert d.mean() < 0.3e-3 and np.percentile(d, 95) < 1.0e-3, (d.mean(), np.percentile(d, 95))
    assert np.abs(out[lock] - V[lock]).max() < 1e-12
    n0, n1 = MD.tri_normals(V, F), MD.tri_normals(out, F)
    assert ((n0 * n1).sum(1) < 0).sum() == 0


def test_transfer_lod_follows_lod0():
    V0, F0 = icosphere(3, 0.1)
    V1, _ = icosphere(1, 0.1)
    V1 = V1 * 1.002                                    # LOD1 slightly off the LOD0 surface
    same, _ = WR.transfer_lod(V0, V0.copy(), F0, V1)
    assert np.abs(same - V1).max() < 1e-12
    moved, _ = WR.transfer_lod(V0, V0 * 1.1, F0, V1)
    assert np.abs(np.linalg.norm(moved, axis=1) - 0.1102).max() < 5e-4   # radius 0.11 + normal offset kept


def test_delta_e2000_reference_values():
    # Sharma, Wu & Dalal (2005) test data, pairs 1-3 and 7
    a = np.array([[50, 2.6772, -79.7751], [50, 3.1571, -77.2803], [50, 2.8361, -74.0200], [50, 0, 0]])
    b = np.array([[50, 0, -82.7485], [50, 0, -82.7485], [50, 0, -82.7485], [50, -1, 2]])
    assert np.allclose(TT.delta_e2000(a, b), [2.0425, 2.8615, 3.4412, 2.3669], atol=1e-4)


def test_push_pull_fills_only_holes():
    img = np.zeros((64, 64, 3))
    cov = np.zeros((64, 64), bool)
    cov[10:20, 10:30] = True
    img[cov] = [0.2, 0.4, 0.6]
    out = TT.dilate(img, cov, 2)
    assert np.allclose(out[cov], [0.2, 0.4, 0.6])
    assert np.allclose(out, [0.2, 0.4, 0.6])           # one colour in -> one colour everywhere


def _plane(n, jitter=0.0, seed=0):
    xs = np.linspace(0, 1, n)
    X, Y = np.meshgrid(xs, xs)
    P = np.c_[X.ravel(), Y.ravel(), np.zeros(n * n)]
    if jitter:
        rng = np.random.default_rng(seed)
        inner = (P[:, 0] > 0) & (P[:, 0] < 1) & (P[:, 1] > 0) & (P[:, 1] < 1)
        P[inner, :2] += rng.uniform(-jitter, jitter, (inner.sum(), 2)) / n
    F = []
    for j in range(n - 1):
        for i in range(n - 1):
            a = j * n + i
            F += [[a, a + 1, a + n + 1], [a, a + n + 1, a + n]]
    return P, np.array(F)


def test_bake_reproduces_texture_through_a_different_uv_layout(tmp_path):
    # target: unit plane (blender frame, metres... scaled 0.1 m), UV = xy; texture = smooth pattern
    Pt, Ft = _plane(40)
    Pt = Pt * 0.1
    uvt = (Pt[:, :2] / 0.1)[Ft].astype(np.float32)
    yy, xx = np.mgrid[0:256, 0:256] / 255.0
    tex = np.stack([0.5 + 0.4 * np.sin(6 * xx), 0.5 + 0.4 * np.cos(5 * yy), 0.3 + 0.3 * xx * yy], 2)
    TT.save_png(str(tmp_path / "alb.png"), tex)
    arrays = {"head.positions": Pt, "head.normals": np.tile([0, 0, 1.0], (len(Pt), 1)), "head.faces": Ft,
              "head.uv.HeadUV": uvt, "head.mh_index": -np.ones(len(Pt), int)}
    MD.save_npz(str(tmp_path / "t.npz"), arrays, {"frame": MD.frame_def("blender"), "head_uv": "HeadUV"})
    src = TT.HDSource(str(tmp_path / "t.npz"))
    # donor: a different (jittered) triangulation, UV rotated by 90 degrees and shrunk into [0.1,0.9]
    Pd, Fd = _plane(23, jitter=0.3)
    Pd = Pd * 0.1
    uvd = np.c_[1 - Pd[:, 1] / 0.1, Pd[:, 0] / 0.1] * 0.8 + 0.1
    Nd = np.tile([0, 0, 1.0], (len(Pd), 1))
    alb_lin = TT.load_image(str(tmp_path / "alb.png"), linear=True)
    b = TT.bake(src, Pd, Nd, Fd, uvd, 256, 0.003, alb_lin)
    assert b["stats"]["ray_hit_fraction"] > 0.99
    # expected: texel (u,v) of the donor map shows the target texture at the 3D point it maps to
    cov = b["cov"]
    js, is_ = np.nonzero(cov)
    u = (is_ + 0.5) / 256
    v = 1 - (js + 0.5) / 256
    x, y = (v - 0.1) / 0.8, 1 - (u - 0.1) / 0.8
    expect = TT.bilinear(alb_lin, np.c_[x, y])
    assert TT.psnr(TT.lin_to_srgb(b["albedo"][cov]), TT.lin_to_srgb(expect)) > 40


def test_numpy_tangents_on_a_plane():
    P, F = _plane(5)
    T = WR.tangents_numpy(P, F, P[:, :2], np.tile([0, 0, 1.0], (len(P), 1)))
    assert np.allclose(T[:, :3], [1, 0, 0]) and np.all(T[:, 3] == 1)


@pytest.mark.skipif(shutil.which("blender") is None, reason="blender not on PATH")
def test_blender_mikktspace_tangents_on_a_plane():
    P, F = _plane(5)
    T, info = WR.tangents_blender(P, F, P[:, :2], np.tile([0, 0, 1.0], (len(P), 1)))
    assert T is not None, info
    assert np.allclose(T[:, :3], [1, 0, 0], atol=1e-5) and np.all(T[:, 3] == 1)
    T2, _ = WR.tangents_blender(P, F, np.c_[P[:, 0], 1 - P[:, 1]], np.tile([0, 0, 1.0], (len(P), 1)))
    assert np.all(T2[:, 3] == -1)                       # flipped V -> flipped bitangent sign


def test_normal_map_is_reexpressed_in_the_donor_tangent_frame(tmp_path):
    """A tilt of +0.3 along our tangent (+X, increasing u) must come out along the donor's bitangent
    when the donor UV is rotated by 90 degrees (donor u runs along -Y, donor v along +X)."""
    Pt, Ft = _plane(30)
    Pt = Pt * 0.1
    uvt = (Pt[:, :2] / 0.1)[Ft].astype(np.float32)
    n = np.array([0.3, 0.0, np.sqrt(1 - 0.09)])
    TT.save_png(str(tmp_path / "alb.png"), np.full((64, 64, 3), 0.5))
    TT.save_png(str(tmp_path / "nrm.png"), np.tile(n * 0.5 + 0.5, (64, 64, 1)))
    arrays = {"head.positions": Pt, "head.normals": np.tile([0, 0, 1.0], (len(Pt), 1)), "head.faces": Ft,
              "head.uv.HeadUV": uvt, "head.mh_index": -np.ones(len(Pt), int)}
    MD.save_npz(str(tmp_path / "t.npz"), arrays, {"frame": MD.frame_def("blender"), "head_uv": "HeadUV"})
    src = TT.HDSource(str(tmp_path / "t.npz"))
    Pd, Fd = _plane(17, jitter=0.3)
    Pd = Pd * 0.1
    uvd = np.c_[1 - Pd[:, 1] / 0.1, Pd[:, 0] / 0.1] * 0.8 + 0.1
    Nd = np.tile([0, 0, 1.0], (len(Pd), 1))
    Td = WR.tangents_numpy(Pd, Fd, uvd, Nd)
    assert np.allclose(Td[:, :3], [0, -1, 0], atol=1e-6)
    b = TT.bake(src, Pd, Nd, Fd, uvd, 128, 0.003, TT.load_image(str(tmp_path / "alb.png"), True),
                TT.load_image(str(tmp_path / "nrm.png"), False), Td)
    enc = b["normal"][b["cov"]] * 2 - 1
    q = (np.round((n * 0.5 + 0.5) * 255) / 255) * 2 - 1      # 8-bit quantised input
    assert np.allclose(enc, [0.0, q[0], q[2]], atol=0.01)


# ----------------------------------------------------------------------------- review additions (adversarial inputs)
class _FakeLod:
    def __init__(self, Pw):
        self.Pw = np.asarray(Pw, float)


class _FakeTarget:
    def __init__(self, pts):
        self.pts = pts

    def has(self, k):
        return k in self.pts

    def point(self, k):
        return self.pts[k]


def _feature_setup(n=14, seed=3):
    rng = np.random.default_rng(seed)
    names = WR.FT.BASIC + [f"mp:{i}" for i in range(n - len(WR.FT.BASIC))]
    X = rng.normal(size=(len(names), 3)) * 0.05                     # asymmetric 'face' points (m)
    a = 0.3
    R = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    Y = 0.9 * X @ R.T + [0.01, -0.02, 1.6] + rng.normal(size=X.shape) * 0.002   # 2 mm shape noise
    fres = {k: (np.array([i, i, i]), np.array([1.0, 0, 0])) for i, k in enumerate(names)}
    return names, X, Y, fres


def test_alignment_gates_mirror_outliers_and_systematic_errors():
    names, X, Y, fres = _feature_setup()
    tgt = _FakeTarget(dict(zip(names, Y)))
    sim, info = WR.similarity_from_features(_FakeLod(X), tgt, fres)
    assert info["rms_mm"] < 4 and not info["rejected_outliers_mm"] and abs(info["scale"] - 0.9) < 0.02
    # mirrored donor (labels true): refused
    with pytest.raises(SystemExit, match="MIRRORED"):
        WR.similarity_from_features(_FakeLod(X * [-1, 1, 1]), tgt, fres)
    # one wrong id (60 mm off): rejected, the rest aligns
    Xb = X.copy()
    Xb[names.index("chin")] += [0.06, 0, 0]
    sim, info = WR.similarity_from_features(_FakeLod(Xb), tgt, fres)
    assert list(info["rejected_outliers_mm"]) == ["chin"] and "chin" not in info["used"] and info["rms_mm"] < 4
    # many wrong ids: refused as a systematic problem
    Xc = X.copy()
    Xc[:5] += np.random.default_rng(0).normal(size=(5, 3)) * 0.06
    with pytest.raises(SystemExit):
        WR.similarity_from_features(_FakeLod(Xc), tgt, fres)
    # rigid alignment of a donor in centimetres declared as metres: refused
    with pytest.raises(SystemExit, match="units"):
        WR.similarity_from_features(_FakeLod(X * 100), tgt, fres, mode="rigid")


def test_bone_side_names():
    for n, s in [("eye_l", "L"), ("L_Eye", "L"), ("Bip01 R Clavicle", "R"), ("eye.R", "R"), ("LeftEye", "L"),
                 ("eyeRight", "R"), ("jaw", None), ("head", None), ("neck_01", None), ("Rib", None)]:
        assert WR.bone_side(n) == s, n


def test_skin_side_check_flags_a_mirrored_donor():
    L = np.zeros((478, 3))
    for a, b in WR.FT.SYM_PAIRS:                                    # subject left = +X (blender frame)
        L[a] = [0.03, -0.08, 1.6]
        L[b] = [-0.03, -0.08, 1.6]
    L[WR.FT.BASIC_MP["chin"]] = [0, -0.07, 1.5]
    tgt = type("T", (), {"L": L})()
    P = np.array([[0.03, -0.07, 1.6]] * 5 + [[-0.03, -0.07, 1.6]] * 5)
    lod = type("Lod", (), {})()
    lod.bone_names = ["head", "eye_l", "eye_r"]
    lod.joints = np.array([[1, 0, 0, 0]] * 5 + [[2, 0, 0, 0]] * 5)
    lod.weights = np.array([[1.0, 0, 0, 0]] * 10)
    lod.wid = np.arange(10)
    ok = WR.skin_side_check(lod, P, tgt)
    assert ok["checked"] and not ok["mirrored"] and ok["consistent"] == 2
    bad = WR.skin_side_check(lod, P * [-1, 1, 1], tgt)
    assert bad["mirrored"] and bad["inconsistent"] == 2


def test_repair_flips_and_duplicate_triangles():
    P, F = _plane(6)
    adj, edges = G.vertex_adjacency(F, len(P))
    V = P.copy()
    V[14, :2] += [0.5, 0.45]                                        # pushes an interior vertex across its ring
    assert ((MD.tri_normals(V, F) * MD.tri_normals(P, F)).sum(1) < 0).any()
    lock = np.zeros(len(P), bool)
    lock[0] = True
    V2, info = WR.repair_flips(P, V.copy(), F, adj, lock)
    assert info["flipped_before"] > 0 and info["flipped_after"] == 0
    untouched = np.linalg.norm(V2 - V, axis=1) == 0
    assert untouched.sum() >= len(P) - info["vertices_adjusted"]
    Fd = np.concatenate([F, F[:3, ::-1]])                           # double-sided copy after welding
    F2, k = G.drop_duplicate_triangles(Fd)
    assert k == 3 and len(F2) == len(F)
    n, lab = G.components(np.concatenate([F, F[:2] + len(P)]), 2 * len(P))
    assert n >= 2


def test_nicp_component_without_data_keeps_its_place():
    V1, F1 = icosphere(2, 0.1)
    V2, F2 = icosphere(1, 0.02)
    V2 = V2 + [0.3, 0, 0]
    V = np.concatenate([V1, V2])
    F = np.concatenate([F1, F2 + len(V1)])
    adj, edges = G.vertex_adjacency(F, len(V))
    tq = G.TriMeshQuery(V1 * 1.05, F1)
    data = np.r_[np.ones(len(V1), bool), np.zeros(len(V2), bool)]      # small sphere: no data, no landmarks
    out, _, _ = WR.nicp(V, F, edges, tq, MD.vertex_normals(V1 * 1.05, F1), {"schedule": [10, 2], "max_inner": 2},
                        data_mask=data, verbose=False)
    assert np.isfinite(out).all()
    assert np.abs(out[len(V1):] - V2).max() < 1e-6                    # stays (was 'Factor is exactly singular')
    assert abs(np.linalg.norm(out[:len(V1)], axis=1).mean() - 0.105) < 2e-3


def test_uv_overlap_is_detected():
    Vw = np.array([[0, 0, 0], [0.01, 0, 0], [0, 0.01, 0], [0, 0, 0.05], [0.01, 0, 0.05], [0, 0.01, 0.05]], float)
    F = np.array([[0, 1, 2], [3, 4, 5]])
    uv = np.array([[0.1, 0.1], [0.9, 0.1], [0.1, 0.9]] * 2)          # both triangles on the same texels
    pix, tri, bary = TT.rasterise(uv[F], 32, keep_all=True)
    st, sel, bad = TT.uv_overlap(pix, tri, bary, Vw, F)
    assert st["conflict_fraction"] > 0.95
    st2, _, _ = TT.uv_overlap(*TT.rasterise(uv[F[:1]], 32, keep_all=True), Vw, F[:1])
    assert st2["conflicting_texels"] == 0


def test_axis_rotations():
    Rs = G.axis_rotations()
    assert len(Rs) == 24 and all(abs(np.linalg.det(R) - 1) < 1e-12 for R in Rs)


@pytest.mark.skipif(shutil.which("blender") is None, reason="blender not on PATH")
def test_tangent_convention_check_detects_the_sign_convention():
    P, F = _plane(6)
    uv = P[:, :2].copy()
    N = np.tile([0, 0, 1.0], (len(P), 1))
    T, _ = WR.tangents_blender(P, F, uv, N)
    lod = type("Lod", (), {})()
    lod.P_native, lod.F, lod.uv, lod.normals = P, F, uv, N
    lod.tangents = T
    assert WR.tangent_convention_check(lod, "blender")["verdict"] == "match"
    lod.tangents = T * [1, 1, 1, -1]
    c = WR.tangent_convention_check(lod, "blender")
    assert c["verdict"] == "sign_flipped" and c["flip_sign"]
    # MikkTSpace's sign follows the winding: the same buffers with the other winding give -1
    T2, _ = WR.tangents_blender(P, F[:, ::-1], uv, N)
    assert np.all(T2[:, 3] == -T[:, 3])
