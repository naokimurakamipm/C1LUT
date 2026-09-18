"""Colour transform round-trip tests (spec section 27)."""

from __future__ import annotations

import numpy as np
import pytest

from c1lut.colorspaces import (
    TRANSFER_CHOICES,
    ColorspaceError,
    decode_transfer,
    encode_transfer,
    get_colourspace,
    lab_to_xyz_d50,
    rgb_linear_to_xyz_d50,
    rgb_to_rgb_linear,
    xyz_d50_to_lab,
    xyz_d50_to_rgb_linear,
)

RNG = np.random.default_rng(11)
VALUES = RNG.random((500, 3))


@pytest.mark.parametrize("name", TRANSFER_CHOICES)
def test_transfer_round_trip(name):
    values = np.clip(VALUES, 0.01, 0.99)
    decoded = decode_transfer(name, encode_transfer(name, values))
    assert np.allclose(decoded, values, atol=1e-6), name


def test_rec709_differs_from_srgb():
    values = np.linspace(0.02, 0.98, 50)
    srgb = encode_transfer("sRGB", values)
    oetf = encode_transfer("Rec.709 OETF", values)
    assert not np.allclose(srgb, oetf, atol=1e-3)
    # In the midtones the Rec.709 OETF renders darker than sRGB
    # (video gamma vs web gamma).
    mid = (values > 0.1) & (values < 0.6)
    assert np.all(oetf[mid] < srgb[mid])


def test_gamut_round_trip():
    values = np.clip(VALUES, 0.0, 1.0)
    out = rgb_to_rgb_linear(
        rgb_to_rgb_linear(values, "sRGB", "ITU-R BT.2020"),
        "ITU-R BT.2020",
        "sRGB",
    )
    assert np.allclose(out, values, atol=1e-4)


def test_xyz_lab_round_trip():
    values = np.clip(VALUES, 0.0, 1.0)
    xyz = rgb_linear_to_xyz_d50(values, "sRGB", "Bradford")
    assert np.allclose(lab_to_xyz_d50(xyz_d50_to_lab(xyz)), xyz, atol=1e-9)


def test_d65_d50_adaptation_round_trip():
    from c1lut.colorspaces import _D50_XYZ, _whitepoint_xyz, get_colourspace, xyz_adapt

    xyz = rgb_linear_to_xyz_d50(np.array([[0.2, 0.4, 0.6]]), "sRGB", "Bradford")
    cs = get_colourspace("sRGB")
    back = xyz_adapt(xyz_adapt(xyz, _D50_XYZ, _whitepoint_xyz(cs), "Bradford"),
                     _whitepoint_xyz(cs), _D50_XYZ, "Bradford")
    assert np.allclose(back, xyz, atol=1e-9)


def test_unknown_names_raise():
    with pytest.raises(ColorspaceError):
        get_colourspace("No Such Gamut")
    with pytest.raises(ColorspaceError):
        encode_transfer("No Such Transfer", np.array([0.5]))


def test_cli_style_aliases_resolve():
    """Short CLI spellings resolve to the canonical colour-science names."""
    from c1lut.colorspaces import canonical_gamut, canonical_transfer

    assert canonical_gamut("Rec.709") == "ITU-R BT.709"
    assert canonical_gamut("bt709") == "ITU-R BT.709"
    assert canonical_gamut("srgb") == "sRGB"
    assert canonical_transfer("gamma2.4") == "Gamma 2.4"
    assert canonical_transfer("srgb") == "sRGB"
    assert canonical_transfer("linear") == "Linear"
