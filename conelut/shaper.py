"""Error-weighted input shaper curves for the ICC CLUT.

The 33^3 (or larger) CLUT samples camera RGB on a uniform grid. When the
composite transform is steep in the shadows (film looks usually are), a
uniform grid wastes nodes in the highlights. mft2 input tables are per-channel
1D warps, so we can redistribute nodes along each axis: derive an
error-weighted CDF per channel from a pilot validation run and store it as the
input table. The CLUT itself must then be sampled at the *inverse* warp
positions (see :func:`inverse_nodes`).

Curves are monotonic by construction (a CDF), start at 0 and end at 1, and
their dynamic range is capped so no region of the input range is starved.
"""

from __future__ import annotations

import numpy as np

DEFAULT_BINS = 128
# Weights live in [floor*max, max] -> warp slope ratio <= 1/floor. 0.3 measured
# best on steep film looks (gentler warps; 0.1 over-concentrates nodes and
# starves the midtones where most samples live).
DEFAULT_FLOOR_RATIO = 0.3


def derive_input_curves(
    rgb: np.ndarray,
    delta_e: np.ndarray,
    bins: int = DEFAULT_BINS,
    floor_ratio: float = DEFAULT_FLOOR_RATIO,
    curve_points: int = 256,
) -> list[np.ndarray]:
    """Derive one monotonic warp [0,1] -> [0,1] per channel.

    ``rgb`` are pilot sample inputs (N, 3) and ``delta_e`` their dE00 errors.
    The warp for channel c is the CDF of the mean error over that channel's
    value, so grid-node density follows where the error actually lives.
    """
    rgb = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, 1.0)
    delta_e = np.asarray(delta_e, dtype=np.float64)
    if rgb.ndim != 2 or rgb.shape[1] != 3 or delta_e.shape[0] != rgb.shape[0]:
        raise ValueError("rgb must be (N, 3) and delta_e must be (N,)")
    if delta_e.size == 0:
        raise ValueError("pilot set is empty; cannot derive shaper curves")

    curves = []
    for channel in range(3):
        values = rgb[:, channel]
        total, _edges = np.histogram(values, bins=bins, range=(0.0, 1.0), weights=delta_e)
        counts, _ = np.histogram(values, bins=bins, range=(0.0, 1.0))
        mean_error = np.where(counts > 0, total / np.maximum(counts, 1), 0.0)
        weights = _smooth(mean_error, radius=2)
        top = float(weights.max())
        if top <= 0.0:
            weights = np.ones_like(weights)
            top = 1.0
        weights = np.maximum(weights, floor_ratio * top)
        cdf = np.cumsum(weights)
        cdf = cdf / cdf[-1]
        # Bin centers -> warp samples; force exact endpoints.
        x = (np.arange(bins) + 0.5) / bins
        xs = np.concatenate(([0.0], x, [1.0]))
        ys = np.concatenate(([0.0], cdf, [1.0]))
        grid = np.interp(np.linspace(0.0, 1.0, curve_points), xs, ys)
        grid[0], grid[-1] = 0.0, 1.0
        grid = np.maximum.accumulate(grid)  # guarantee monotone after resampling
        curves.append(grid)
    return curves


def inverse_nodes(curve: np.ndarray, grid: int) -> np.ndarray:
    """Camera-RGB positions of the CLUT nodes for one channel.

    Node j sits where warp(x) = j / (grid - 1), i.e. at x = warp^-1(j/(grid-1)).
    """
    curve = np.asarray(curve, dtype=np.float64)
    x = np.linspace(0.0, 1.0, curve.size)
    targets = np.linspace(0.0, 1.0, grid)
    return np.interp(targets, curve, x)


def warped_camera_grid(curves: list[np.ndarray], grid: int) -> np.ndarray:
    """Camera RGB sample grid for warped CLUT nodes (R slowest, ICC order)."""
    if len(curves) != 3:
        raise ValueError("expected one warp curve per channel")
    axes = [inverse_nodes(curve, grid) for curve in curves]
    rr, gg, bb = np.meshgrid(*axes, indexing="ij")
    return np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)


def _smooth(values: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return values.copy()
    kernel = np.ones(2 * radius + 1)
    padded = np.pad(values, radius, mode="edge")
    return np.convolve(padded, kernel, mode="valid") / kernel.size
