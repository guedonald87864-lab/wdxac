"""Check fit_face's analytic Jacobian against finite differences on a small problem."""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "face"))
import fit_face as F  # noqa: E402


def test_projection_jacobian_matches_finite_differences():
    rng = np.random.default_rng(0)
    K, n = 4, 30
    L0 = rng.normal(size=(n, 3)) * 0.5 + np.array([0, 0, 0])
    dL = rng.normal(size=(K, n, 3)) * 0.05
    c = np.array([0.1, 3.1, 0.05, 0.02, -0.03, 12.0, 1500.0])
    uv = F.project(L0, c, 640, 480) + rng.normal(size=(n, 2))
    s = np.ones(n) * 0.7

    def resid(x):
        w, cc = x[:K], x[K:]
        L = L0 + np.tensordot(w, dL, 1)
        return ((F.project(L, cc, 640, 480) - uv) * s[:, None]).ravel()

    x = np.concatenate([rng.uniform(0, 0.5, K), c])
    num = np.zeros((2 * n, len(x)))
    for j in range(len(x)):
        h = 1e-6 * max(1.0, abs(x[j]))
        xp, xm = x.copy(), x.copy()
        xp[j] += h
        xm[j] -= h
        num[:, j] = (resid(xp) - resid(xm)) / (2 * h)
    # analytic pieces, mirroring fit_face.fit.jac
    w, cc = x[:K], x[K:]
    L = L0 + np.tensordot(w, dL, 1)
    R = F.rodrigues(cc[:3])
    X = L @ R.T + cc[3:6]
    f, Z = cc[6], X[:, 2]
    dP = np.zeros((n, 2, 3))
    dP[:, 0, 0] = f / Z
    dP[:, 0, 2] = -f * X[:, 0] / Z ** 2
    dP[:, 1, 1] = f / Z
    dP[:, 1, 2] = -f * X[:, 1] / Z ** 2
    dP *= s[:, None, None]
    Jw = np.einsum("nij,knj->nik", dP, dL @ R.T).reshape(2 * n, K)
    Jt = dP.reshape(2 * n, 3)
    Jf = (np.stack([X[:, 0] / Z, X[:, 1] / Z], 1) * s[:, None]).ravel()
    assert np.allclose(Jw, num[:, :K], rtol=1e-4, atol=1e-4)
    assert np.allclose(Jt, num[:, K + 3:K + 6], rtol=1e-4, atol=1e-4)
    assert np.allclose(Jf, num[:, K + 6], rtol=1e-4, atol=1e-6)
