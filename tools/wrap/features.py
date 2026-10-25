"""Named head feature points shared by export_head.py (Blender), wrap.py and the tests (numpy only).

Side convention: _R / _L are the SUBJECT's right / left (MediaPipe naming), i.e. in the blender
frame _R is at -X and _L at +X.

The 11 BASIC features drive the similarity alignment (>= 8 required):
  eye_outer_R/L, eye_inner_R/L (canthi), nose_tip, mouth_corner_R/L, chin (menton),
  ear_top_R/L (highest point of the helix), crown (highest midline point of the head).
The first eight come from MediaPipe FaceMesh landmarks (model/source/face_corr_mediapipe478.json
maps every landmark to a barycentric point of the MakeHuman base mesh); ear tops and crown are not
part of the FaceMesh and are defined geometrically (see ear_top / crown below).
Any MediaPipe landmark can also be used by name as 'mp:<index>' (e.g. 'mp:13' = upper lip inner).
"""
import numpy as np

BASIC_MP = {
    "eye_outer_R": 33, "eye_inner_R": 133, "eye_inner_L": 362, "eye_outer_L": 263,
    "nose_tip": 1, "mouth_corner_R": 61, "mouth_corner_L": 291, "chin": 152,
}
BASIC = ["eye_outer_R", "eye_inner_R", "eye_inner_L", "eye_outer_L", "nose_tip",
         "mouth_corner_R", "mouth_corner_L", "chin", "ear_top_R", "ear_top_L", "crown"]

# extra points a person would typically also click (used as NICP landmarks when available)
EXTRA_MP = {
    "mp:0": "upper lip, outer midline", "mp:17": "lower lip, outer midline",
    "mp:13": "upper lip, inner midline", "mp:14": "lower lip, inner midline",
    "mp:159": "upper lid R", "mp:145": "lower lid R", "mp:386": "upper lid L", "mp:374": "lower lid L",
    "mp:98": "nose wing R", "mp:327": "nose wing L", "mp:2": "subnasale", "mp:168": "nasion",
    "mp:105": "brow R", "mp:334": "brow L", "mp:10": "forehead top of face oval",
    "mp:234": "face oval R (tragus level)", "mp:454": "face oval L (tragus level)",
    "mp:172": "jaw angle R", "mp:397": "jaw angle L",
}

# MediaPipe FaceMesh contours (same lists as tools/model/texture_build.py)
EYE_R = [33, 246, 161, 160, 159, 158, 157, 173, 133, 155, 154, 153, 145, 144, 163, 7]
EYE_L = [263, 466, 388, 387, 386, 385, 384, 398, 362, 382, 381, 380, 374, 373, 390, 249]
LIP_OUTER = [61, 185, 40, 39, 37, 0, 267, 269, 270, 409, 291, 375, 321, 405, 314, 17, 84, 181, 91, 146]
LIP_INNER = [78, 191, 80, 81, 82, 13, 312, 311, 310, 415, 308, 324, 318, 402, 317, 14, 87, 178, 88, 95]
FACE_OVAL = [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377,
             152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109]

REGIONS = ["other", "face", "eye_R", "eye_L", "mouth_inner", "nose_cavity", "ear_R", "ear_L",
           "scalp", "neck", "neck_ring"]
REGION_ID = {n: i for i, n in enumerate(REGIONS)}
NO_DATA_REGIONS = ("eye_R", "eye_L", "mouth_inner", "nose_cavity")   # never pulled onto the target


def ear_mh_ids(side, frac=0.7):
    """MakeHuman base-mesh vertex ids of one ear ('r' = subject's right): vertices that the
    ear-translation targets move by more than `frac` of their maximum (excludes the falloff ring
    on the skull). Needs tools/model/mh_engine.py on sys.path."""
    import os
    import mh_engine as E
    ids = None
    for fn in (f"{side}-ear-trans-up.target", f"{side}-ear-trans-forward.target"):
        i, off = E.load_target(os.path.join(E.TARGETS, "ears", fn))
        m = np.linalg.norm(off, axis=1)
        s = set(i[m > frac * m.max()].tolist())
        ids = s if ids is None else ids & s
    return ids


def landmarks_from_corr(corr, positions, mh_index):
    """MediaPipe landmark positions on a mesh carrying the MakeHuman 'mh_index' attribute.
    Returns (478,3) with NaN rows where a referenced base vertex is not in the mesh."""
    inv = {int(m): i for i, m in enumerate(mh_index) if m >= 0}
    L = np.full((len(corr), 3), np.nan)
    for i, c in enumerate(corr):
        if all(v in inv for v in c["v"]):
            L[i] = sum(w * positions[inv[v]] for v, w in zip(c["v"], c["w"]))
    return L


SYM_PAIRS = [(263, 33), (362, 133), (291, 61), (454, 234)]   # (subject-left, subject-right)


def head_axes(L):
    """Head frame from landmarks: lateral (subject right->left), up, forward (face direction).
    The lateral axis averages four symmetric landmark pairs (robust to one asymmetric pair)."""
    lat = sum(L[a] - L[b] for a, b in SYM_PAIRS)
    lat = lat / np.linalg.norm(lat)
    eyes = (L[BASIC_MP["eye_outer_L"]] + L[BASIC_MP["eye_outer_R"]]) / 2
    up = eyes - L[BASIC_MP["chin"]]
    up -= lat * (up @ lat)
    up /= np.linalg.norm(up)
    fwd = np.cross(lat, up)        # blender: lat=+X, up=+Z -> X x Z = -Y = face direction
    return lat, up, fwd


def mid_lateral(L, lat):
    return float(np.mean([(L[a] + L[b]) / 2 @ lat for a, b in SYM_PAIRS]))


def ear_top(P, ear_ids, up):
    """Highest point (along the head up axis) of the given ear vertex set."""
    ids = np.asarray(ear_ids)
    return int(ids[np.argmax(P[ids] @ up)])


def crown(P, candidate_ids, lat, up, mid_x, half_width):
    """Highest point along `up` among vertices within half_width of the mid-sagittal plane."""
    ids = np.asarray(candidate_ids)
    off = P[ids] @ lat - mid_x
    sel = ids[np.abs(off) < half_width]
    return int(sel[np.argmax(P[sel] @ up)])


def basic_features(L, P, ear_ids_R, ear_ids_L, head_ids=None):
    """Positions of the 11 BASIC features. L: (478,3) landmarks, P: mesh positions."""
    lat, up, fwd = head_axes(L)
    out = {k: L[i].copy() for k, i in BASIC_MP.items()}
    out["ear_top_R"] = P[ear_top(P, ear_ids_R, up)].copy()
    out["ear_top_L"] = P[ear_top(P, ear_ids_L, up)].copy()
    mid = mid_lateral(L, lat)
    ids = np.arange(len(P)) if head_ids is None else head_ids
    eye_w = np.linalg.norm(L[BASIC_MP["eye_outer_L"]] - L[BASIC_MP["eye_outer_R"]])
    out["crown"] = P[crown(P, ids, lat, up, mid, 0.1 * eye_w)].copy()
    return out, (lat, up, fwd)


def point_in_polygon(pts, poly):
    """Even-odd rule; pts (N,2), poly (M,2) closed implicitly."""
    x, y = pts[:, 0], pts[:, 1]
    inside = np.zeros(len(pts), bool)
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        cond = ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / (yj - yi + 1e-300) + xi)
        inside ^= cond
        j = i
    return inside


def dist_to_polyline(P, pts, closed=False):
    if closed:
        pts = np.vstack([pts, pts[:1]])
    d = np.full(len(P), np.inf)
    for a, b in zip(pts[:-1], pts[1:]):
        ab = b - a
        t = np.clip(((P - a) @ ab) / max(ab @ ab, 1e-30), 0, 1)
        d = np.minimum(d, np.linalg.norm(P - (a + t[:, None] * ab), axis=1))
    return d


def cyl_coords(P, centre, lat, up, fwd):
    """(angle around the head's vertical axis [rad, 0 = straight ahead], height) for face-oval tests."""
    Q = P - centre
    return np.stack([np.arctan2(Q @ lat, Q @ fwd), Q @ up], 1)


def face_mask(P, L, centre, axes):
    lat, up, fwd = axes
    poly = cyl_coords(L[FACE_OVAL], centre, lat, up, fwd)
    q = cyl_coords(P, centre, lat, up, fwd)
    front = (P - centre) @ fwd > 0
    return point_in_polygon(q, poly) & front
