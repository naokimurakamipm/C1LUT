"""Interpolation tests (spec section 27): identity, ramps, channel swaps, resampling."""

from __future__ import annotations

import numpy as np

from conelut.interpolation import interpolate_1d_table, interpolate_3d, resample_3d
from helpers import identity_cube_data

INTERPOLATING_METHODS = ("tetrahedral", "trilinear")


def test_identity_lut_exact_interpolating_methods():
    data = identity_cube_data(9)
    rng = np.random.default_rng(3)
    pts = rng.random((2000, 3))
    coords = pts * 8.0
    for method in INTERPOLATING_METHODS:
        out = interpolate_3d(data, coords, method)
        assert np.allclose(out, pts, atol=1e-12), method


def test_nearest_returns_grid_values():
    data = identity_cube_data(9)
    rng = np.random.default_rng(3)
    pts = rng.random((500, 3))
    coords = pts * 8.0
    out = interpolate_3d(data, coords, "nearest")
    expected = np.rint(coords) / 8.0
    assert np.allclose(out, expected, atol=1e-12)


def test_linear_ramp_known_values():
    # A separable linear function is reproduced exactly by the interpolating
    # methods at any position inside the grid.
    size = 5
    x = np.linspace(0, 1, size)
    rr, gg, bb = np.meshgrid(x, x, x, indexing="ij")
    data = np.ascontiguousarray(np.stack([(rr + gg + bb) / 3.0] * 3, axis=-1))
    rng = np.random.default_rng(4)
    pts = rng.random((500, 3))
    coords = pts * (size - 1)
    for method in INTERPOLATING_METHODS:
        out = interpolate_3d(data, coords, method)
        expected = pts.mean(axis=1)[:, None] * np.ones((1, 3))
        assert np.allclose(out, expected, atol=1e-12), method


def test_tetrahedral_known_value():
    # A cell filled with distinct corner values: corners reproduce exactly, and
    # a separable combination interpolates to the separable result.
    data = np.zeros((2, 2, 2, 3))
    data[0, 0, 0] = [0.0, 0.0, 0.0]
    data[1, 0, 0] = [0.1, 0.0, 0.0]
    data[0, 1, 0] = [0.0, 0.2, 0.0]
    data[0, 0, 1] = [0.0, 0.0, 0.4]
    data[1, 1, 0] = [0.1, 0.2, 0.0]
    data[1, 0, 1] = [0.1, 0.0, 0.4]
    data[0, 1, 1] = [0.0, 0.2, 0.4]
    data[1, 1, 1] = [0.1, 0.2, 0.4]
    corners = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 1)]
    for c in corners:
        out = interpolate_3d(data, np.array([c], dtype=float))
        assert np.allclose(out[0], data[c])
    vals = data[:, :, :, 0] + data[:, :, :, 1] * 10.0 + data[:, :, :, 2] * 100.0
    data2 = np.stack([vals] * 3, axis=-1)
    mid = interpolate_3d(data2, np.array([[0.25, 0.5, 0.75]]))
    expected = (0.25 * 0.1) + (0.5 * 0.2) * 10 + (0.75 * 0.4) * 100
    assert np.allclose(mid[0, 0], expected)


def test_channel_swap_luts():
    """Spec section 19: out.R = in.R, G=0, B=0 (LUT 1) and the cyclic swap (LUT 2)."""
    size = 5
    x = np.linspace(0, 1, size)
    rr, gg, bb = np.meshgrid(x, x, x, indexing="ij")

    red_only = np.ascontiguousarray(np.stack([rr, np.zeros_like(rr), np.zeros_like(rr)], -1))
    cyclic = np.ascontiguousarray(np.stack([bb, rr, gg], -1))

    rng = np.random.default_rng(5)
    pts = rng.random((500, 3))
    coords = pts * (size - 1)
    for method in INTERPOLATING_METHODS:
        out = interpolate_3d(red_only, coords, method)
        assert np.allclose(out[:, 0], pts[:, 0], atol=1e-12), method
        assert np.allclose(out[:, 1:], 0.0), method
        out2 = interpolate_3d(cyclic, coords, method)
        assert np.allclose(out2[:, 0], pts[:, 2], atol=1e-12), method
        assert np.allclose(out2[:, 1], pts[:, 0], atol=1e-12), method
        assert np.allclose(out2[:, 2], pts[:, 1], atol=1e-12), method


def test_resample_up_and_down():
    identity17 = identity_cube_data(17)
    up = resample_3d(identity17, 33, "tetrahedral")
    assert up.shape == (33, 33, 33, 3)
    assert np.allclose(up, identity_cube_data(33), atol=1e-12)

    identity65 = identity_cube_data(65)
    down = resample_3d(identity65, 33, "tetrahedral")
    assert np.allclose(down, identity_cube_data(33), atol=1e-12)

    # Resampling a smooth function must differ from nearest-index selection
    # (the legacy behaviour) on non-identity data.
    size = 9
    x = np.linspace(0, 1, size)
    rr, gg, bb = np.meshgrid(x, x, x, indexing="ij")
    smooth = np.ascontiguousarray(np.stack([(rr * gg + bb**2) / 2.0] * 3, -1))
    tetra = resample_3d(smooth, 17, "tetrahedral")
    nearest = resample_3d(smooth, 17, "nearest")
    assert not np.allclose(tetra, nearest)
    ref = resample_3d(resample_3d(smooth, 17, "tetrahedral"), 9, "tetrahedral")
    assert np.allclose(ref, smooth, atol=1e-10)


def test_1d_table_interpolation():
    table = np.array([[0.0] * 3, [0.5] * 3, [1.0] * 3])
    out = interpolate_1d_table(table, np.array([0.0, 0.25, 1.0]))
    assert np.allclose(out[:, 0], [0.0, 0.25, 1.0])
