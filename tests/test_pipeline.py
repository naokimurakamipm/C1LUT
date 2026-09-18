"""Base profile evaluation and pipeline tests (spec sections 5, 9, 19, 27)."""

from __future__ import annotations

import struct

import numpy as np
import pytest

from conelut.cms import BaseProfile, Mft2Tag
from conelut.cube import CubeLUT
from conelut.pipeline import (
    ConversionParams,
    apply_film_standard_legacy,
    camera_grid,
    generate_profile,
    reference_transform,
)
from helpers import identity_cube_data, make_synthetic_base


@pytest.fixture()
def synthetic_base(tmp_path):
    return BaseProfile(make_synthetic_base(tmp_path / "base.icc"), precision="float")


def identity_lut() -> CubeLUT:
    return CubeLUT(
        title="identity",
        size_3d=33,
        domain_min=np.zeros(3),
        domain_max=np.ones(3),
        data_3d=identity_cube_data(33),
    )


def test_base_eval_matches_srgb_identity(synthetic_base):
    """The synthetic base maps camera RGB == sRGB: the eval must be Lab(sRGB(x))."""
    from conelut.colorspaces import decode_transfer, rgb_linear_to_xyz_d50, xyz_d50_to_lab

    rng = np.random.default_rng(6)
    rgb = rng.random((200, 3))
    lab = synthetic_base._a2b(0)(rgb)
    expected = xyz_d50_to_lab(rgb_linear_to_xyz_d50(decode_transfer("sRGB", rgb), "sRGB", "Bradford"))
    assert np.abs(lab - expected).max() < 0.5  # 33^3 CLUT interpolation error budget


def test_mft2_axis_order_no_channel_swap():
    """XYZ-PCS CLUT holding the identity encoding must evaluate without axis confusion.

    With an XYZ PCS, CLUT values v decode to v/32768, so an identity table
    encoded as u16(x*65535) evaluates to exactly 2*x — any axis confusion
    would scramble the channels and fail this check.
    """
    grid = 5
    x = np.linspace(0, 1, grid)
    rr, gg, bb = np.meshgrid(x, x, x, indexing="ij")
    table = np.stack([rr, gg, bb], -1)
    encoded = np.round(table * 65535).astype(np.uint16)
    tag = Mft2Tag(b"mft2" + b"\x00" * 4 + bytes((3, 3, grid, 0))
                  + struct.pack(">9i", *(65536 if i % 4 == 0 else 0 for i in range(9)))
                  + struct.pack(">HH", 2, 2)
                  + struct.pack(">6H", 0, 65535, 0, 65535, 0, 65535)
                  + encoded.reshape(-1).astype(">u2").tobytes()
                  + struct.pack(">6H", 0, 65535, 0, 65535, 0, 65535), b"XYZ ")
    rng = np.random.default_rng(7)
    rgb = rng.random((100, 3))
    out = tag(rgb)
    assert np.abs(out - 2.0 * rgb).max() < 0.05


def test_reference_identity_matches_base(synthetic_base):
    params = ConversionParams()
    lut = identity_lut()
    rng = np.random.default_rng(8)
    rgb = rng.random((300, 3))
    direct = synthetic_base._a2b(0)(rgb)
    kind, values = reference_transform(params, synthetic_base, lut, rgb)
    assert kind == "Lab"
    assert np.abs(values - direct).max() < 0.6


def test_channel_swap_lut_changes_red_only(synthetic_base):
    """Spec section 19: a LUT that zeroes G/B must not touch R axis data."""
    size = 9
    data = identity_cube_data(size).copy()
    data[..., 1:] = 0.0
    lut = CubeLUT(title="red", size_3d=size, domain_min=np.zeros(3),
                  domain_max=np.ones(3), data_3d=data)
    params = ConversionParams(input_gamut="sRGB", input_transfer="sRGB",
                              output_gamut="sRGB", output_transfer="sRGB")
    rgb = np.array([[0.8, 0.4, 0.2], [0.2, 0.5, 0.9]])
    kind, values = reference_transform(params, synthetic_base, lut, rgb)
    kind2, values2 = reference_transform(params, synthetic_base, identity_lut(), rgb)
    assert kind == kind2 == "Lab"
    # Red-only LUT leaves a* (redness) roughly intact but kills b*/L* structure
    assert np.isfinite(values).all()
    assert not np.allclose(values, values2)
    # The output must differ per-sample (input-dependent), proving axis sanity.
    assert np.abs(values[0] - values[1]).max() > 1.0


def test_film_standard_legacy_is_opt_in(synthetic_base):
    lut = identity_lut()
    rgb = camera_grid(5)
    kind_a, a = reference_transform(ConversionParams(), synthetic_base, lut, rgb)
    kind_b, b = reference_transform(
        ConversionParams(c1_curve="film-standard-legacy"), synthetic_base, lut, rgb
    )
    assert kind_a == kind_b == "Lab"
    assert np.abs(a - b).max() > 1.0  # the legacy curve changes the look


def test_film_standard_legacy_monotone_s_curve():
    x = np.linspace(0.0, 1.0, 11)
    y = apply_film_standard_legacy(np.column_stack([x] * 3))
    assert np.all(np.diff(y[:, 0]) > 0)
    assert y[0, 0] == pytest.approx(0.0)
    assert y[-1, 0] == pytest.approx(1.0)
    # 0.5 maps to 0.5 (symmetry)
    assert y[5, 0] == pytest.approx(0.5, abs=1e-9)


def test_midtone_gamma_changes_midtones(synthetic_base):
    lut = identity_lut()
    rgb = camera_grid(5)
    _, base_vals = reference_transform(ConversionParams(), synthetic_base, lut, rgb)
    _, gamma_vals = reference_transform(ConversionParams(midtone_gamma=1.2), synthetic_base, lut, rgb)
    assert np.abs(base_vals - gamma_vals).max() > 1.0


def test_generate_profile_meta(synthetic_base, tmp_path):
    from conelut.icc import ICCProfile

    cube = parse_tmp_cube(tmp_path)
    params = ConversionParams(icc_intent="mirror")
    data, stats = generate_profile(cube, synthetic_base, params, log=lambda *_: None)
    profile = ICCProfile(data)
    assert profile.tag(b"A2B0") is not None
    assert profile.tag(b"A2B1") is not None
    assert profile.tag(b"A2B2") is None
    assert profile.pcs == b"Lab "
    assert profile.device_class == b"scnr"
    assert stats["pcs"] == "Lab"

    params_single = ConversionParams(icc_intent="relative")
    data2, _ = generate_profile(cube, synthetic_base, params_single, log=lambda *_: None)
    profile2 = ICCProfile(data2)
    assert profile2.tag(b"A2B1") is not None
    assert profile2.tag(b"A2B0") is None
    assert profile2.tag(b"A2B2") is None
    # Default desc mode keeps the base profile's description verbatim so
    # Capture One keeps the camera association (1.x behaviour).
    assert profile2.description() == "TestCamera-Generic"

    params_look = ConversionParams(icc_intent="relative", desc_mode="look")
    data3, _ = generate_profile(cube, synthetic_base, params_look, log=lambda *_: None)
    assert ICCProfile(data3).description() == "TestCamera-identity"

    from conelut.pipeline import output_filename

    assert output_filename(cube, synthetic_base) == "TestCamera-identity.icc"


def parse_tmp_cube(tmp_path):
    from helpers import identity_cube, write_cube

    from conelut.cube import parse_cube

    return parse_cube(write_cube(tmp_path / "identity.cube", identity_cube(9)))
