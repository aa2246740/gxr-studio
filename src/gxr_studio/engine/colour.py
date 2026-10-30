"""sRGB -> CIELAB, CIEDE2000 and the sector-word codec used by the prediction path.

Source: research/color_table_fit/fit_sectors.py
Source SHA-256: bb6170344ad22c7c490dafbb7f34644e933bfad2c750941f78c81675373988f8
Verbatim AST segments only (see research/studio_engine_e0/tools/extract_engine.py).
"""
import numpy as np


M709 = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750],
                 [0.0193339, 0.1191920, 0.9503041]])


D65 = np.array([0.95047, 1.0, 1.08883])


def srgb_to_linear(v):
    v = np.asarray(v, dtype=np.float64)
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def xyz_to_lab(xyz, white=D65):
    t = np.asarray(xyz, float) / white
    d = 6 / 29
    f = np.where(t > d ** 3, np.cbrt(t), t / (3 * d * d) + 4 / 29)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def lab_from_codes(rgb):
    """sRGB codes 0..255 -> CIELAB D65 (same path as stage 2d/2e)."""
    lin = srgb_to_linear(np.clip(np.asarray(rgb, float), 0, 255) / 255)
    return xyz_to_lab(lin @ M709.T)


def de00_components(lab1, lab2):
    """Residual vector r with |r| = CIEDE2000(lab1, lab2) (rotation term folded in)."""
    lab1 = np.asarray(lab1, float)
    lab2 = np.asarray(lab2, float)
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    C1, C2 = np.hypot(a1, b1), np.hypot(a2, b2)
    cbar = 0.5 * (C1 + C2)
    G = 0.5 * (1 - np.sqrt(cbar ** 7 / (cbar ** 7 + 25 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp, dCp = L2 - L1, C2p - C1p
    both = (C1p * C2p) > 0
    dhp = np.where(both, h2p - h1p, 0.0)
    dhp = np.where(dhp > 180, dhp - 360, dhp)
    dhp = np.where(dhp < -180, dhp + 360, dhp)
    dHp = 2 * np.sqrt(np.maximum(C1p * C2p, 0)) * np.sin(np.radians(dhp) / 2)
    Lbar, Cbarp = 0.5 * (L1 + L2), 0.5 * (C1p + C2p)
    hsum, habs = h1p + h2p, np.abs(h1p - h2p)
    hbar = np.where(~both, hsum, np.where(habs <= 180, hsum / 2, np.where(hsum < 360, (hsum + 360) / 2, (hsum - 360) / 2)))
    T = (1 - 0.17 * np.cos(np.radians(hbar - 30)) + 0.24 * np.cos(np.radians(2 * hbar))
         + 0.32 * np.cos(np.radians(3 * hbar + 6)) - 0.20 * np.cos(np.radians(4 * hbar - 63)))
    dtheta = 30 * np.exp(-((hbar - 275) / 25) ** 2)
    Rc = 2 * np.sqrt(Cbarp ** 7 / (Cbarp ** 7 + 25 ** 7))
    Sl = 1 + 0.015 * (Lbar - 50) ** 2 / np.sqrt(20 + (Lbar - 50) ** 2)
    Sc = 1 + 0.045 * Cbarp
    Sh = 1 + 0.015 * Cbarp * T
    Rt = -np.sin(np.radians(2 * dtheta)) * Rc
    x, y, z = dLp / Sl, dCp / Sc, dHp / Sh
    return np.stack([x, y + 0.5 * Rt * z, np.sqrt(np.maximum(1 - 0.25 * Rt * Rt, 0)) * z], axis=-1)


def delta_e00(lab1, lab2):
    return np.linalg.norm(de00_components(lab1, lab2), axis=-1)


def decode_word(w):
    def dec(v):
        return v if v < 128 else -(v - 128)
    a, b, bx, a2 = w
    if (bx ^ 0x80) != b or a2 != a:
        raise ValueError(w.hex())
    return dec(a), dec(b)


def encode_word(a, b):
    def enc(v):
        v = int(v)
        if not -127 <= v <= 127:
            raise ValueError(v)
        return v if v >= 0 else (0x80 | -v)
    A, B = enc(a), enc(b)
    return bytes((A, B, B ^ 0x80, A))


def Z_of_words(A, B, ctx):
    return ((A + ctx.d0) - 1j * (B + ctx.d1)) / 32 / ctx.S_eff
