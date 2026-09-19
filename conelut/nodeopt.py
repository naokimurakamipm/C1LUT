"""Conservative CLUT node-value optimization.

By default every CLUT node stores the reference transform evaluated exactly at
that node's position. When the transform is steep between nodes, a small
damped correction of the node values (driven by the error observed at random
sample points, spread to the tetrahedral corners that influence them) can
reduce the between-node error. Risks are overfitting and banding, so this is
opt-in and guarded:

- a fixed number of damped iterations (gradient-like, not a solve),
- a *separate* independent sample set (different seed) decides which
  iteration ships; if the independent error never improves, the original
  CLUT is returned unchanged.

Everything operates on the CLUT's own grid: ``*_coords`` are sample positions
in grid units (camera RGB mapped through the input shaper when present), and
``ref_*`` are the reference PCS values at the matching camera RGB points.
"""

from __future__ import annotations

import numpy as np

from .interpolation import interpolate_3d, tetrahedral_weights

DEFAULT_ITERATIONS = 3
DEFAULT_DAMPING = 0.6


def optimize_clut_values(
    clut: np.ndarray,
    pcs_kind: str,
    ref_train: np.ndarray,
    ref_check: np.ndarray,
    train_coords: np.ndarray,
    check_coords: np.ndarray,
    metric,
    iterations: int = DEFAULT_ITERATIONS,
    damping: float = DEFAULT_DAMPING,
) -> tuple[np.ndarray, dict]:
    """Return (optimized clut, report).

    ``clut`` is (G, G, G, 3) in the PCS domain (Lab or XYZ floats). ``metric``
    maps (ref, generated) Lab arrays to a per-sample non-negative error such
    as dE00; it is only used to *select* the best iteration, the corrections
    themselves work on PCS differences.
    """
    clut = np.asarray(clut, dtype=np.float64).copy()
    original = clut.copy()
    grid = clut.shape[0]
    from .colorspaces import xyz_d50_to_lab

    def to_lab(values):
        return values if pcs_kind == "Lab" else xyz_d50_to_lab(values)

    def check_score():
        gen = interpolate_3d(clut, check_coords)
        return float(metric(to_lab(ref_check), to_lab(gen)).mean())

    baseline = check_score()
    best = (baseline, 0)
    best_clut = original
    history = [{"iteration": 0, "check_mean_dE00": round(baseline, 5)}]

    for iteration in range(1, max(1, iterations) + 1):
        gen = interpolate_3d(clut, train_coords)
        error = ref_train - gen  # (N, 3) in PCS units
        weights, corners = tetrahedral_weights(train_coords, clut.shape[:3])
        num = np.zeros_like(clut)
        den = np.zeros(clut.shape[:3])
        for corner in range(4):
            w = weights[:, corner]
            np.add.at(num, tuple(corners[:, corner].T), error * w[:, None])
            np.add.at(den, tuple(corners[:, corner].T), w)
        mask = den > 1e-6
        correction = np.zeros_like(num)
        correction[mask] = damping * num[mask] / den[mask][:, None]
        clut = _clip_pcs(clut + correction, pcs_kind)

        check = check_score()
        history.append({"iteration": iteration, "check_mean_dE00": round(check, 5)})
        if check < best[0]:
            best = (check, iteration)
            best_clut = clut.copy()

    improved = best[1] > 0 and best[0] < baseline - 1e-9
    report = {
        "iterations_run": len(history) - 1,
        "best_iteration": best[1],
        "check_mean_dE00_before": round(baseline, 5),
        "check_mean_dE00_after": round(best[0], 5),
        "improved": bool(improved),
        "history": history,
    }
    return (best_clut if improved else original), report


def _clip_pcs(values: np.ndarray, pcs_kind: str) -> np.ndarray:
    values = values.copy()
    if pcs_kind == "Lab":
        values[..., 0] = np.clip(values[..., 0], 0.0, 100.0)
        values[..., 1] = np.clip(values[..., 1], -128.0, 127.0)
        values[..., 2] = np.clip(values[..., 2], -128.0, 127.0)
    else:
        values = np.clip(values, 0.0, 2.0)
    return values
