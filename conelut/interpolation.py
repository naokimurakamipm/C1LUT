"""3D / 1D LUT interpolation.

Axes convention: a 3D LUT is stored as ``lut[r, g, b] -> (3,)`` with axes in
(R, G, B) channel order. Coordinates are given in grid units, i.e. in
``[0, size - 1]``. Out-of-range coordinates are clamped by the evaluators
themselves; callers report clamp statistics separately.

Tetrahedral interpolation is the default (OpenColorIO uses INTERP_BEST =
tetrahedral for 3D LUTs). ``trilinear`` and ``nearest`` are kept as explicit
lower-quality options for experimentation and cross-checking.
"""

from __future__ import annotations

import numpy as np

METHODS = ("tetrahedral", "trilinear", "nearest")


def interpolate_3d(lut: np.ndarray, coords: np.ndarray, method: str = "tetrahedral", extrapolate: bool = False) -> np.ndarray:
    """Evaluate a 3D LUT at ``coords`` (N, 3) in grid units, channel order (R, G, B)."""
    if lut.ndim != 4 or lut.shape[3] != 3:
        raise ValueError(f"3D LUT must have shape (S, S, S, 3), got {lut.shape}")
    if min(lut.shape[:3]) < 2:
        raise ValueError("3D LUT axes require at least two samples")
    coords = np.asarray(coords, dtype=np.float64)
    if coords.ndim != 2 or coords.shape[1] != 3:
        raise ValueError("coords must have shape (N, 3)")
    if not np.isfinite(coords).all():
        raise ValueError("LUT coordinates must be finite")
    if not extrapolate:
        coords = np.clip(coords, 0.0, np.array(lut.shape[:3]) - 1)
    if method == "tetrahedral":
        return _tetrahedral(lut, coords)
    if method == "trilinear":
        return _trilinear(lut, coords)
    if method == "nearest":
        return _nearest(lut, coords)
    raise ValueError(f"unknown interpolation method: {method!r} (expected one of {METHODS})")


def _corners(lut: np.ndarray, coords: np.ndarray):
    """Return fractional parts plus the eight surrounding corner values."""
    size = np.array(lut.shape[:3])
    c0 = np.clip(np.floor(coords), 0, size - 2).astype(np.int64)
    f = coords - c0
    c1 = c0 + 1
    r0, g0, b0 = c0[:, 0], c0[:, 1], c0[:, 2]
    r1, g1, b1 = c1[:, 0], c1[:, 1], c1[:, 2]

    def corner(hi_r, hi_g, hi_b):
        return lut[
            np.where(hi_r, r1, r0),
            np.where(hi_g, g1, g0),
            np.where(hi_b, b1, b0),
        ]

    c000 = corner(False, False, False)
    c100 = corner(True, False, False)
    c010 = corner(False, True, False)
    c110 = corner(True, True, False)
    c001 = corner(False, False, True)
    c101 = corner(True, False, True)
    c011 = corner(False, True, True)
    c111 = corner(True, True, True)
    # Corner order encodes (hi_r, hi_g, hi_b) as 4*hi_r + 2*hi_g + 1*hi_b to
    # match the code computed in _select_corners.
    stack = np.stack([c000, c001, c010, c011, c100, c101, c110, c111], axis=1)
    return f, stack


def _select_corners(stack: np.ndarray, hi_mask: np.ndarray) -> np.ndarray:
    """Pick the corner value selected by a boolean (N, 3) high/low axis mask."""
    code = hi_mask[:, 0] * 4 + hi_mask[:, 1] * 2 + hi_mask[:, 2]
    return np.take_along_axis(stack, code[:, None, None], axis=1)[:, 0, :]


def _trilinear(lut: np.ndarray, coords: np.ndarray) -> np.ndarray:
    f, stack = _corners(lut, coords)
    wx, wy, wz = (f[:, 0][:, None], f[:, 1][:, None], f[:, 2][:, None])
    # Stack order encodes (hi_r, hi_g, hi_b) as 4*hi_r + 2*hi_g + 1*hi_b.
    c000, c100 = stack[:, 0], stack[:, 4]
    c010, c110 = stack[:, 2], stack[:, 6]
    c001, c101 = stack[:, 1], stack[:, 5]
    c011, c111 = stack[:, 3], stack[:, 7]

    def lerp(a, b, t):
        return a + (b - a) * t

    c00 = lerp(c000, c100, wx)
    c10 = lerp(c010, c110, wx)
    c01 = lerp(c001, c101, wx)
    c11 = lerp(c011, c111, wx)
    c0 = lerp(c00, c10, wy)
    c1 = lerp(c01, c11, wy)
    return lerp(c0, c1, wz)


def _tetrahedral(lut: np.ndarray, coords: np.ndarray) -> np.ndarray:
    f, stack = _corners(lut, coords)

    # Pick one of the six tetrahedra by sorting the fractional parts of the
    # coordinates in descending order; the traversal 000 -> +a1 -> +a1+a2 -> 111
    # then only ever reads corners inside that tetrahedron.
    order = np.argsort(-f, axis=1, kind="stable")
    f1 = np.take_along_axis(f, order[:, 0:1], axis=1)[:, 0][:, None]
    f2 = np.take_along_axis(f, order[:, 1:2], axis=1)[:, 0][:, None]
    f3 = np.take_along_axis(f, order[:, 2:3], axis=1)[:, 0][:, None]

    eye = np.eye(3, dtype=bool)
    hi1 = eye[order[:, 0]]
    hi2 = hi1 | eye[order[:, 1]]
    hi3 = np.ones_like(hi2)

    c000 = stack[:, 0]
    v_a1 = _select_corners(stack, hi1)
    v_a1a2 = _select_corners(stack, hi2)
    v_111 = _select_corners(stack, hi3)

    v1 = c000 + f1 * (v_a1 - c000)
    v2 = v1 + f2 * (v_a1a2 - v_a1)
    return v2 + f3 * (v_111 - v_a1a2)


def _nearest(lut: np.ndarray, coords: np.ndarray) -> np.ndarray:
    size = np.array(lut.shape[:3])
    idx = np.rint(np.clip(coords, 0.0, size - 1.0)).astype(np.int64)
    return lut[idx[:, 0], idx[:, 1], idx[:, 2]]


def resample_3d(lut: np.ndarray, target: int, method: str = "tetrahedral") -> np.ndarray:
    """Resample a 3D LUT to a different grid size via real interpolation.

    The source grid is sampled continuously at the target grid coordinates,
    so 17 -> 33 and 65 -> 33 both produce smooth results (unlike the legacy
    nearest-index selection).
    """
    size = lut.shape[0]
    if target < 2:
        raise ValueError("target grid size must be >= 2")
    if size == target:
        return lut.copy()
    x = np.linspace(0.0, size - 1.0, target)
    rr, gg, bb = np.meshgrid(x, x, x, indexing="ij")
    coords = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    out = interpolate_3d(lut, coords, method)
    return out.reshape(target, target, target, 3)


def interpolate_1d_table(table: np.ndarray, x: np.ndarray, extrapolate: bool = False) -> np.ndarray:
    """Linearly interpolate a 1D shaper table (M, 3) at x (N,) in [0, 1]."""
    if table.ndim != 2 or table.shape[1] != 3:
        raise ValueError(f"1D table must have shape (M, 3), got {table.shape}")
    x = np.asarray(x, dtype=np.float64)
    xp = np.linspace(0.0, 1.0, table.shape[0])
    out = np.stack([np.interp(x, xp, table[:, c]) for c in range(3)], axis=-1)
    if extrapolate:
        for mask, endpoint, origin, slope in (
            (x < 0, 0.0, table[0], table[1] - table[0]),
            (x > 1, 1.0, table[-1], table[-1] - table[-2]),
        ):
            out[mask] = origin + (x[mask, None] - endpoint) * slope * (len(table) - 1)
    return out
