"""DNG colorimetry subset (camera -> XYZ per DNG 1.7 ch. 6) used to put scene light under a log LUT.

Source: research/color_table_fit/dng_scene.py
Source SHA-256: f0e8dd88baed2f6894b2427de7d7342ce37b2105716b2c47111d49ecfda5f59d
Verbatim AST segments only (see research/studio_lut_s8a/tools/extract_lut.py).
"""
import math

import numpy as np


# CIE / DNG SDK constants
D50_XY = np.array([0.3457, 0.3585])          # DNG PCS white (SDK D50_xy_coord)


D65_XY = np.array([0.3127, 0.3290])          # sRGB / BT.709 / F-Gamut white


BRADFORD = np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]])


# DNG SDK dng_camera_profile::IlluminantToTemperature (EXIF LightSource codes)
ILLUMINANT_K = {1: 5500.0, 2: 4150.0, 3: 2850.0, 4: 5500.0, 9: 5500.0, 10: 6500.0, 11: 7500.0,
                12: 6400.0, 13: 5050.0, 14: 4150.0, 15: 3525.0, 16: 2925.0, 17: 2850.0, 18: 5500.0,
                19: 6500.0, 20: 5500.0, 21: 6500.0, 22: 7500.0, 23: 5000.0, 24: 3200.0}


# Robertson (1968) isotemperature lines: mired, u, v, slope (CIE 1960 uv); 325-mired u is the corrected 0.24792.
ROBERTSON = np.array([
    [0, 0.18006, 0.26352, -0.24341], [10, 0.18066, 0.26589, -0.25479], [20, 0.18133, 0.26846, -0.26876],
    [30, 0.18208, 0.27119, -0.28539], [40, 0.18293, 0.27407, -0.30470], [50, 0.18388, 0.27709, -0.32675],
    [60, 0.18494, 0.28021, -0.35156], [70, 0.18611, 0.28342, -0.37915], [80, 0.18740, 0.28668, -0.40955],
    [90, 0.18880, 0.28997, -0.44278], [100, 0.19032, 0.29326, -0.47888], [125, 0.19462, 0.30141, -0.58204],
    [150, 0.19962, 0.30921, -0.70471], [175, 0.20525, 0.31647, -0.84901], [200, 0.21142, 0.32312, -1.0182],
    [225, 0.21807, 0.32909, -1.2168], [250, 0.22511, 0.33439, -1.4512], [275, 0.23247, 0.33904, -1.7298],
    [300, 0.24010, 0.34308, -2.0637], [325, 0.24792, 0.34655, -2.4681], [350, 0.25591, 0.34951, -2.9641],
    [375, 0.26400, 0.35200, -3.5814], [400, 0.27218, 0.35407, -4.3633], [425, 0.28039, 0.35577, -5.3762],
    [450, 0.28863, 0.35714, -6.7262], [475, 0.29685, 0.35823, -8.5955], [500, 0.30505, 0.35907, -11.324],
    [525, 0.31320, 0.35968, -15.628], [550, 0.32129, 0.36011, -23.325], [575, 0.32931, 0.36038, -40.770],
    [600, 0.33724, 0.36051, -116.45]])


TINT_SCALE = -3000.0


def xy_to_xyz(xy):
    x, y = float(xy[0]), float(xy[1])
    return np.array([x / y, 1.0, (1.0 - x - y) / y])


def xyz_to_xy(xyz):
    s = float(np.sum(xyz))
    if s <= 0:
        return D50_XY.copy()
    return np.array([xyz[0] / s, xyz[1] / s])


def bradford(src_xy, dst_xy):
    """Linear Bradford, as DNG SDK MapWhiteMatrix (cone ratios pinned to [0.1, 10])."""
    w1 = np.maximum(BRADFORD @ xy_to_xyz(src_xy), 0.0)
    w2 = np.maximum(BRADFORD @ xy_to_xyz(dst_xy), 0.0)
    a = np.array([np.clip(b / a_ if a_ > 0 else 10.0, 0.1, 10.0) for a_, b in zip(w1, w2)])
    return np.linalg.inv(BRADFORD) @ np.diag(a) @ BRADFORD


def xy_to_cct(xy):
    """Correlated colour temperature and tint, Robertson's method (DNG SDK dng_temperature)."""
    x, y = float(xy[0]), float(xy[1])
    den = 1.5 - x + 6.0 * y
    u, v = 2.0 * x / den, 3.0 * y / den
    last_dt = last_du = last_dv = 0.0
    for i in range(1, 31):
        r1, u1, v1, t1 = ROBERTSON[i]
        ln = math.hypot(1.0, t1)
        du, dv = 1.0 / ln, t1 / ln
        uu, vv = u - u1, v - v1
        dt = -uu * dv + vv * du
        if dt <= 0.0 or i == 30:
            dt = -min(dt, 0.0)
            f = 0.0 if i == 1 else dt / (last_dt + dt)
            r0 = ROBERTSON[i - 1]
            temp = 1.0e6 / (r0[0] * f + r1 * (1.0 - f))
            uu = u - (r0[1] * f + u1 * (1.0 - f))
            vv = v - (r0[2] * f + v1 * (1.0 - f))
            du = du * (1.0 - f) + last_du * f
            dv = dv * (1.0 - f) + last_dv * f
            ln = math.hypot(du, dv)
            du, dv = du / ln, dv / ln
            return temp, (uu * du + vv * dv) * TINT_SCALE
        last_dt, last_du, last_dv = dt, du, dv
    raise AssertionError("unreachable")


def white_from_neutral(col, neutral):
    n = np.asarray(neutral, float)
    n = n / n.max()
    xy, k, ok = col.neutral_to_xy(n)
    return xy, col.white_info(xy, n, k, ok)


class DngColour:
    """Camera <-> XYZ per DNG 1.7 chapter 6 (DNG SDK dng_color_spec semantics).

    matrix: "interp" (spec: interpolate in 1/CCT), or "cm1" / "cm2" to use one calibration only
    (sensitivity analysis: two independent Ricoh calibrations bracket the matrix uncertainty).
    """

    def __init__(self, dng, matrix="interp"):
        if all(m is None for m in dng.color_matrix):
            raise ValueError("no ColorMatrix")
        if matrix in ("cm1", "cm2"):
            k = 0 if matrix == "cm1" else 1
            if dng.color_matrix[k] is None:
                raise ValueError(f"{matrix} requested but the DNG has no ColorMatrix{k + 1}")
            dng = _OneMatrix(dng, k)
        elif matrix != "interp":
            raise ValueError(matrix)
        self.matrix_mode = matrix
        pcs = xy_to_xyz(D50_XY)

        def norm(m):
            # dng_camera_profile NormalizeColorMatrix: largest camera coordinate of the PCS white = 1
            mx = float(np.max(m @ pcs))
            return m / mx if mx > 0 and (mx < 0.99 or mx > 1.01) else m.copy()
        self.normalised_scale = [None if m is None else float(np.max(m @ pcs)) for m in dng.color_matrix]
        cm = [None if m is None else norm(m) for m in dng.color_matrix]
        cc = [m if m is not None else np.eye(3) for m in dng.camera_calibration]
        fm = dng.forward_matrix
        T = [ILLUMINANT_K.get(i, 0.0) for i in dng.illuminant]
        if cm[1] is None or cm[0] is None or T[0] <= 0 or T[1] <= 0 or T[0] == T[1]:
            k = 0 if cm[0] is not None else 1
            self.single = True
            self.cm = [cm[k]] * 2
            self.T = [T[k] or 5000.0] * 2
            self.cc = [cc[k]] * 2
            self.fm = [fm[k]] * 2
        else:
            self.single = False
            order = [0, 1] if T[0] < T[1] else [1, 0]
            self.cm = [cm[i] for i in order]
            self.T = [T[i] for i in order]
            self.cc = [cc[i] for i in order]
            self.fm = [fm[i] for i in order]
        self.ab = np.diag(dng.analog_balance)
        self.illuminant = list(dng.illuminant)

    def weight(self, xy):
        """Weight g of the lower-temperature calibration at white xy, and the CCT/tint."""
        temp, tint = xy_to_cct(xy)
        if self.single or temp <= self.T[0]:
            g = 1.0
        elif temp >= self.T[1]:
            g = 0.0
        else:
            g = (1.0 / temp - 1.0 / self.T[1]) / (1.0 / self.T[0] - 1.0 / self.T[1])
        return g, temp, tint

    def xyz_to_camera(self, xy):
        g, _, _ = self.weight(xy)
        cm = g * self.cm[0] + (1 - g) * self.cm[1]
        cc = g * self.cc[0] + (1 - g) * self.cc[1]
        return self.ab @ cc @ cm

    def neutral_to_xy(self, neutral, max_pass=30):
        """DNG SDK NeutralToXY: iterate the interpolated matrix from D50 to a fixed point."""
        n = np.asarray(neutral, float)
        last = D50_XY.copy()
        for k in range(max_pass):
            nxt = xyz_to_xy(np.linalg.solve(self.xyz_to_camera(last), n))
            if abs(nxt[0] - last[0]) + abs(nxt[1] - last[1]) < 1e-7:
                return nxt, k + 1, True
            if k == max_pass - 1:
                nxt = 0.5 * (last + nxt)
            last = nxt
        return last, max_pass, False

    def camera_to_pcs(self, white_xy):
        """CameraToPCS (camera -> XYZ adapted to D50) for this white (SDK dng_color_spec::SetWhiteXY)."""
        m = self.xyz_to_camera(white_xy)
        g, _, _ = self.weight(white_xy)
        fm = None
        if self.fm[0] is not None:
            fm = g * self.fm[0] + (1 - g) * self.fm[1] if self.fm[1] is not None else self.fm[0]
        if fm is not None:
            cam_white = m @ xy_to_xyz(white_xy)
            cam_white = np.clip(cam_white / cam_white.max(), 0.001, 1.0)
            cc = g * self.cc[0] + (1 - g) * self.cc[1]
            ind_to_ref = np.linalg.inv(self.ab @ cc)
            ref_white = ind_to_ref @ cam_white
            return fm @ np.diag(1.0 / ref_white) @ ind_to_ref
        p2c = m @ bradford(D50_XY, white_xy)
        p2c = p2c / float(np.max(p2c @ xy_to_xyz(D50_XY)))
        return np.linalg.inv(p2c)

    def camera_to_xyz(self, white_xy, out_white_xy=D65_XY):
        """Camera (normalised raw) -> XYZ chromatically adapted to out_white_xy (Bradford from D50)."""
        return bradford(D50_XY, out_white_xy) @ self.camera_to_pcs(white_xy)

    def white_info(self, white_xy, neutral=None, n_iter=None, converged=None):
        g, temp, tint = self.weight(white_xy)
        return {"white_xy": [float(white_xy[0]), float(white_xy[1])], "cct_K": temp, "tint": tint,
                "weight_cm_low_T": g, "calibration_K": self.T, "illuminants": self.illuminant,
                "neutral": None if neutral is None else [float(v) for v in neutral],
                "iterations": n_iter, "converged": converged}
