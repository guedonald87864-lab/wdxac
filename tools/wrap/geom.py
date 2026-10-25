"""Geometry kernels for the donor wrap (numpy + scipy): topology, welding, exact closest point on
a triangle mesh, ray casting, surface sampling, barycentric helpers.

Closest point (TriMeshQuery.closest) is EXACT, not approximate: a k-nearest-centroid candidate
set is accepted only when it is certified to contain the true closest triangle
(dist to k-th centroid - max triangle radius >= best distance found); the remaining queries fall
back to a radius search with that bound. Closest-point regions follow Ericson, "Real-Time
Collision Detection" 5.1.5, so points beyond a mesh border land EXACTLY on the border edge or
vertex (barycentric == 0), which is what the boundary-rejection tests rely on.
"""
import numpy as np
from scipy import sparse
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree


# ----------------------------------------------------------------------------- topology
def weld(V, tol_rel=1e-6):
    """Group coincident vertices (UV/normal seam duplicates of a GPU vertex buffer).
    Returns (wid (N,) unique id per input vertex, rep (n,) first input vertex of each group)."""
    V = np.asarray(V, float)
    tol = tol_rel * max(np.ptp(V, axis=0).max(), 1e-12)
    pairs = cKDTree(V).query_pairs(tol, output_type="ndarray")
    n = len(V)
    if len(pairs):
        G = sparse.coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
        _, lab = connected_components(G, directed=False)
    else:
        lab = np.arange(n)
    # renumber groups in order of first appearance (stable, deterministic)
    _, first = np.unique(lab, return_index=True)
    order = np.argsort(first)
    remap = np.empty(len(order), np.int64)
    remap[order] = np.arange(len(order))
    wid = remap[lab]
    rep = np.sort(first)
    return wid, rep


def edges_with_count(F):
    """Unique undirected edges (E,2) and the number of triangles using each."""
    e = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
    ue, cnt = np.unique(e, axis=0, return_counts=True)
    return ue, cnt


def tri_edge_boundary(F, n):
    """(F,3) bool: edge (k, k+1) of triangle t is a border edge; and (n,) border-vertex mask."""
    e = np.sort(np.stack([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]], 1), axis=2)   # (F,3,2)
    key = e[..., 0].astype(np.int64) * (n + 1) + e[..., 1]
    _, inv, cnt = np.unique(key.ravel(), return_inverse=True, return_counts=True)
    tb = (cnt[inv] == 1).reshape(-1, 3)
    vb = np.zeros(n, bool)
    vb[e[tb][:, 0]] = True
    vb[e[tb][:, 1]] = True
    return tb, vb


def boundary_loops(F, n):
    """Ordered border loops (list of vertex arrays), longest-first by vertex count."""
    ue, cnt = edges_with_count(F)
    be = ue[cnt == 1]
    nbr = {}
    for a, b in be:
        nbr.setdefault(a, []).append(b)
        nbr.setdefault(b, []).append(a)
    seen, loops = set(), []
    for s in nbr:
        if s in seen:
            continue
        loop, prev, cur = [s], None, s
        seen.add(s)
        while True:
            nxt = [x for x in nbr[cur] if x != prev and x not in seen]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            seen.add(cur)
            loop.append(cur)
        loops.append(np.array(loop))
    return sorted(loops, key=len, reverse=True)


def loop_length(V, loop):
    return float(np.linalg.norm(V[loop] - V[np.roll(loop, -1)], axis=1).sum())


def vertex_adjacency(F, n):
    ue, _ = edges_with_count(F)
    A = sparse.coo_matrix((np.ones(len(ue)), (ue[:, 0], ue[:, 1])), shape=(n, n))
    return (A + A.T).tocsr(), ue


def grow(mask, adj, rings):
    m = mask.copy()
    for _ in range(rings):
        m = m | (adj @ m.astype(np.float64) > 0)
    return m


def orientation_sign(V, F):
    """+1 if (b-a)x(c-a) points away from the mesh centroid on (area-weighted) average, else -1."""
    n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    c = V[F].mean(1) - V.mean(0)
    return 1 if (n * c).sum() >= 0 else -1


# ----------------------------------------------------------------------------- closest point
def closest_on_triangles(P, A, B, C):
    """Vectorised Ericson closest point. Returns (Q (n,3), bary (n,3))."""
    ab, ac, ap = B - A, C - A, P - A
    d1 = (ab * ap).sum(1)
    d2 = (ac * ap).sum(1)
    bp = P - B
    d3 = (ab * bp).sum(1)
    d4 = (ac * bp).sum(1)
    cp = P - C
    d5 = (ab * cp).sum(1)
    d6 = (ac * cp).sum(1)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    den = va + vb + vc
    den = np.where(np.abs(den) < 1e-300, 1e-300, den)
    v = vb / den
    w = vc / den
    bary = np.stack([1 - v - w, v, w], 1)

    def setb(mask, b0, b1, b2):
        bary[mask, 0], bary[mask, 1], bary[mask, 2] = b0[mask] if np.ndim(b0) else b0, \
            b1[mask] if np.ndim(b1) else b1, b2[mask] if np.ndim(b2) else b2

    with np.errstate(divide="ignore", invalid="ignore"):
        # Ericson tests in reverse priority so that the earliest test wins
        m = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
        t = (d4 - d3) / np.where(m, (d4 - d3) + (d5 - d6), 1.0)
        setb(m, 0.0, 1 - t, t)
        m = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        t = d2 / np.where(m, d2 - d6, 1.0)
        setb(m, 1 - t, 0.0, t)
        m = (d6 >= 0) & (d5 <= d6)
        setb(m, 0.0, 0.0, 1.0)
        m = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        t = d1 / np.where(m, d1 - d3, 1.0)
        setb(m, 1 - t, t, 0.0)
        m = (d3 >= 0) & (d4 <= d3)
        setb(m, 0.0, 1.0, 0.0)
        m = (d1 <= 0) & (d2 <= 0)
        setb(m, 1.0, 0.0, 0.0)
    bary = np.nan_to_num(bary, nan=1.0 / 3)
    Q = bary[:, :1] * A + bary[:, 1:2] * B + bary[:, 2:3] * C
    return Q, bary


class TriMeshQuery:
    def __init__(self, V, F):
        self.V = np.asarray(V, float)
        self.F = np.asarray(F, np.int64)
        T = self.V[self.F]
        self.cent = T.mean(1)
        self.rad = np.linalg.norm(T - self.cent[:, None], axis=2).max(1)
        # KD-tree over sub-triangle centroids so that every sample covers <= r_cover of surface
        self.r_cover = float(np.median(self.rad)) * 0.7 + 1e-15
        pts, own, rc = [], [], 0.0
        m_all = np.maximum(1, np.ceil(self.rad / self.r_cover).astype(int))
        for m in np.unique(m_all):
            ids = np.nonzero(m_all == m)[0]
            if m == 1:
                pts.append(self.cent[ids])
                own.append(ids)
                rc = max(rc, self.rad[ids].max())
                continue
            bs = []
            for i in range(m):
                for j in range(m - i):
                    bs.append(((i + 1 / 3) / m, (j + 1 / 3) / m))
                    if i + j < m - 1:
                        bs.append(((i + 2 / 3) / m, (j + 2 / 3) / m))
            bs = np.array(bs)
            b = np.stack([1 - bs.sum(1), bs[:, 0], bs[:, 1]], 1)        # (S,3)
            P_ = np.einsum("sk,tkd->tsd", b, T[ids]).reshape(-1, 3)
            pts.append(P_)
            own.append(np.repeat(ids, len(b)))
            rc = max(rc, (self.rad[ids] / m).max() * 1.0001)
        self.samples = np.concatenate(pts)
        self.owner = np.concatenate(own)
        self.r_cover = rc
        self.tree = cKDTree(self.samples)
        self.ctree = cKDTree(self.cent)
        self.tri_border, self.vert_border = tri_edge_boundary(self.F, len(self.V))
        self.fn = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
        self.fn /= np.maximum(np.linalg.norm(self.fn, axis=1, keepdims=True), 1e-30)

    def _eval(self, P, cand):
        """P (n,3), cand (n,k) triangle ids -> best (dist, tri, bary)."""
        n, k = cand.shape
        T = self.V[self.F[cand.ravel()]]
        Pr = np.repeat(P, k, axis=0)
        Q, bary = closest_on_triangles(Pr, T[:, 0], T[:, 1], T[:, 2])
        d = np.linalg.norm(Pr - Q, axis=1).reshape(n, k)
        j = np.argmin(d, axis=1)
        r = np.arange(n)
        return d[r, j], cand[r, j], bary.reshape(n, k, 3)[r, j]

    def _knn(self, P, k):
        dk, si = self.tree.query(P, k)
        if k == 1:
            dk, si = dk[:, None], si[:, None]
        return dk, self.owner[si]

    def closest(self, P, k=16, chunk=20000, exact=True):
        """Closest surface point. exact=True (default) proves every answer (see module doc);
        exact=False keeps the k-nearest-sample answer (used for dense texture lookups, where the
        query points lie on/near the surface). Returns dict(dist, tri, bary, point, border)."""
        P = np.asarray(P, float)
        n = len(P)
        ns = len(self.samples)
        dist = np.empty(n)
        tri = np.empty(n, np.int64)
        bary = np.empty((n, 3))
        for s in range(0, n, chunk):
            Ps = P[s:s + chunk]
            kk = min(k, ns)
            dk, cand = self._knn(Ps, kk)
            d, t, b = self._eval(Ps, cand)
            unc = np.nonzero(dk[:, -1] - self.r_cover < d)[0] if (exact and kk < ns) else np.array([], int)
            if len(unc):                                   # radius search with the proven bound
                lists = self.tree.query_ball_point(Ps[unc], d[unc] + self.r_cover + 1e-12)
                cnt = np.array([len(x) for x in lists])
                if cnt.sum():
                    pid = np.repeat(np.arange(len(unc)), cnt)
                    tid = self.owner[np.concatenate([np.asarray(x, np.int64) for x in lists if len(x)])]
                    key = np.unique(pid * len(self.F) + tid)
                    pid, tid = key // len(self.F), key % len(self.F)
                    for c0 in range(0, len(pid), 2000000):
                        pp, tt = pid[c0:c0 + 2000000], tid[c0:c0 + 2000000]
                        T = self.V[self.F[tt]]
                        Q, bb = closest_on_triangles(Ps[unc[pp]], T[:, 0], T[:, 1], T[:, 2])
                        dd = np.linalg.norm(Ps[unc[pp]] - Q, axis=1)
                        order = np.lexsort((dd, pp))
                        first = order[np.r_[True, pp[order][1:] != pp[order][:-1]]]
                        gi = unc[pp[first]]
                        better = dd[first] < d[gi]
                        d[gi[better]], t[gi[better]], b[gi[better]] = dd[first][better], tt[first][better], bb[first][better]
            dist[s:s + chunk], tri[s:s + chunk], bary[s:s + chunk] = d, t, b
        point = (self.V[self.F[tri]] * bary[:, :, None]).sum(1)
        return {"dist": dist, "tri": tri, "bary": bary, "point": point, "border": self.on_border(tri, bary)}

    def on_border(self, tri, bary, eps=1e-9):
        """Closest point lies on a border edge or border vertex of the mesh."""
        z = bary <= eps
        nz = z.sum(1)
        out = np.zeros(len(tri), bool)
        # vertex regions
        m = nz == 2
        if m.any():
            k = np.argmax(~z[m], axis=1)
            out[m] = self.vert_border[self.F[tri[m], k]]
        # edge regions: zero bary at vertex k -> edge (k+1, k+2) = tri edge index (k+1)%3
        m = nz == 1
        if m.any():
            k = np.argmax(z[m], axis=1)
            out[m] = self.tri_border[tri[m], (k + 1) % 3]
        return out

    def interp(self, tri, bary, values):
        return (np.asarray(values)[self.F[tri]] * bary[:, :, None]).sum(1)

    # ------------------------------------------------------------------------- rays
    def ray_nearest_hit(self, O, D, tmax, k=16, chunk=50000):
        """Two-sided ray cast restricted to the k triangles nearest to the origin (the origin is
        assumed to be within ~tmax of the surface). Returns (hit mask, t, tri, bary)."""
        n = len(O)
        hit = np.zeros(n, bool)
        T_ = np.full(n, np.inf)
        tri = np.full(n, -1, np.int64)
        bary = np.zeros((n, 3))
        k = min(k, len(self.F))
        for s in range(0, n, chunk):
            Os, Ds = O[s:s + chunk], D[s:s + chunk]
            _, cand = self.ctree.query(Os, k)
            if k == 1:
                cand = cand[:, None]
            h, t, c, b = moller_trumbore(np.repeat(Os, k, 0), np.repeat(Ds, k, 0), self.V, self.F[cand.ravel()])
            t = np.where(h & (np.abs(t) <= tmax), np.abs(t), np.inf).reshape(-1, k)
            j = np.argmin(t, axis=1)
            r = np.arange(len(Os))
            best = t[r, j]
            ok = np.isfinite(best)
            hit[s:s + chunk] = ok
            T_[s:s + chunk] = best
            tri[s:s + chunk] = np.where(ok, cand[r, j], -1)
            bb = b.reshape(-1, k, 3)[r, j]
            bary[s:s + chunk] = bb
        return hit, T_, tri, bary


def moller_trumbore(O, D, V, Ftri, eps=1e-12):
    """Vectorised two-sided ray/triangle test. Returns (hit, t (signed), _, bary)."""
    A, B, C = V[Ftri[:, 0]], V[Ftri[:, 1]], V[Ftri[:, 2]]
    e1, e2 = B - A, C - A
    pv = np.cross(D, e2)
    det = (e1 * pv).sum(1)
    ok = np.abs(det) > eps
    inv = 1.0 / np.where(ok, det, 1.0)
    tv = O - A
    u = (tv * pv).sum(1) * inv
    qv = np.cross(tv, e1)
    v = (D * qv).sum(1) * inv
    t = (e2 * qv).sum(1) * inv
    hit = ok & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9)
    return hit, t, None, np.stack([1 - u - v, u, v], 1)


def rays_hit_any(O, D, V, F, tmin, tmax, chunk_rays=64):
    """Brute force: does the one-sided ray O + t D, tmin < t < tmax, hit any triangle? (n,) bool."""
    V = np.asarray(V, float)
    A, B, C = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    e1, e2 = B - A, C - A
    out = np.zeros(len(O), bool)
    for s in range(0, len(O), chunk_rays):
        Os, Ds = O[s:s + chunk_rays], D[s:s + chunk_rays]
        pv = np.cross(Ds[:, None, :], e2[None])
        det = (e1[None] * pv).sum(2)
        inv = 1.0 / np.where(np.abs(det) > 1e-15, det, np.inf)
        tv = Os[:, None, :] - A[None]
        u = (tv * pv).sum(2) * inv
        qv = np.cross(tv, e1[None])
        v = (Ds[:, None, :] * qv).sum(2) * inv
        t = (e2[None] * qv).sum(2) * inv
        h = (u >= 0) & (v >= 0) & (u + v <= 1) & (t > tmin) & (t < tmax)
        out[s:s + chunk_rays] = h.any(1)
    return out


# ----------------------------------------------------------------------------- sampling / stats
def sample_surface(V, F, n, seed=0, tri_mask=None):
    rng = np.random.default_rng(seed)
    T = V[F]
    area = 0.5 * np.linalg.norm(np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]), axis=1)
    if tri_mask is not None:
        area = area * tri_mask
    tri = rng.choice(len(F), n, p=area / area.sum())
    r1, r2 = rng.random(n), rng.random(n)
    s = np.sqrt(r1)
    bary = np.stack([1 - s, s * (1 - r2), s * r2], 1)
    return (T[tri] * bary[:, :, None]).sum(1), tri, bary


def stats_mm(d, scale=1000.0):
    d = np.asarray(d) * scale
    if len(d) == 0:
        return {"n": 0}
    return {"n": int(len(d)), "mean": float(d.mean()), "p50": float(np.percentile(d, 50)),
            "p95": float(np.percentile(d, 95)), "max": float(d.max())}


def umeyama(src, dst, w=None, scale=True):
    """Weighted similarity (s, R, t) minimising sum w |s R src + t - dst|^2 (no reflection)."""
    src, dst = np.asarray(src, float), np.asarray(dst, float)
    w = np.ones(len(src)) if w is None else np.asarray(w, float)
    w = w / w.sum()
    ms, md = w @ src, w @ dst
    xs, xd = src - ms, dst - md
    S = (xd * w[:, None]).T @ xs
    U, sig, Vt = np.linalg.svd(S)
    d = np.sign(np.linalg.det(U @ Vt))
    Dm = np.diag([1, 1, d])
    R = U @ Dm @ Vt
    var = (w * (xs ** 2).sum(1)).sum()
    s = (sig * np.diag(Dm)).sum() / var if scale else 1.0
    t = md - s * R @ ms
    return s, R, t
