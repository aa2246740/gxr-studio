"""Runtime subset of the hue-ring sector model: kernel weights and the coefficient coupling.

Source: research/hue_ring/sector_model.py
Source SHA-256: d9d98478f2ef7eb86686dfaec53e10a42a55b2aa1d8292f090e6ac65e5bbf143
Verbatim AST segments only (see research/studio_engine_e0/tools/extract_engine.py).
"""
import math

import numpy as np
from scipy.special import ndtr


SIGMA_ZERO = 1e-4  # degrees; "no blur" without a branch


def hue_deg(c):
    return np.degrees(np.angle(c)) % 360


def wrap180(x):
    return (np.asarray(x, dtype=np.float64) + 180) % 360 - 180


def effective(Z, k):
    """Zk = |Z| exp(i k arg Z)."""
    Z = np.asarray(Z, dtype=np.complex128)
    return np.abs(Z) * np.exp(1j * k * np.angle(Z))


def _phi(t):
    return np.exp(-0.5 * t * t) * (1.0 / math.sqrt(2 * math.pi))


def _ramp(d, sigma):
    """E[(d + e)_+], e ~ N(0, sigma^2)."""
    t = d / sigma
    return d * ndtr(t) + sigma * _phi(t)


def _step(d, sigma):
    return ndtr(d / sigma)


_SHIFTS = np.array([-360.0, 0.0, 360.0])


def weights(h, nodes, kernel, sigma):
    """Kernel weights w_s(h), shape (P, 24).

    h: (P,) hue in degrees. nodes: (24,) increasing, n_23 < n_0 + 360.
    sigma: scalar or (P,) Gaussian blur in degrees (>= SIGMA_ZERO).
    """
    n = np.asarray(nodes, dtype=np.float64)
    # hue folded into [n_0, n_0 + 360): three turns then cover every support and blur tail
    h = (np.asarray(h, dtype=np.float64) - n[0]) % 360 + n[0]
    X = h[None, :, None] + _SHIFTS[:, None, None]                      # (3, P, 1)
    sig = np.maximum(np.asarray(sigma, dtype=np.float64), SIGMA_ZERO)
    sig = sig.reshape(1, 1, -1, 1) if sig.ndim else sig
    if kernel == "linear":
        a = np.concatenate([n[-1:] - 360, n[:-1]])   # n_{s-1}
        c = np.concatenate([n[1:], n[:1] + 360])     # n_{s+1}
        ba, cb = n - a, c - n
        R = _ramp(X[None] - np.stack([a, n, c])[:, None, None, :], sig)   # (3 knots, 3 turns, P, 24)
        W = (R[0] / ba - R[1] * (1 / ba + 1 / cb) + R[2] / cb).sum(0)
    elif kernel == "hard":
        hi = np.concatenate([n[1:], n[:1] + 360])
        S = _step(X[None] - np.stack([n, hi])[:, None, None, :], sig)
        W = (S[0] - S[1]).sum(0)
    else:
        raise ValueError(kernel)
    return np.clip(W, 0.0, None)
