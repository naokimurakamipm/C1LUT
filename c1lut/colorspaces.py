"""Gamut and transfer function definitions (spec section 11).

Gamut (primaries + white point) and transfer (encoding curve) are modelled
separately: ``Rec.709`` and ``sRGB`` share primaries but differ in transfer
function, so a single "preset" name must never imply both. Presets in
:mod:`c1lut.presets` are a convenience layer on top of this module.
"""

from __future__ import annotations

import numpy as np

import colour

# Curated gamut list; keys are colour-science RGB_COLOURSPACES names.
GAMUT_CHOICES: tuple[str, ...] = (
    "sRGB",
    "ITU-R BT.709",
    "Display P3",
    "P3-D65",
    "DCI-P3",
    "Adobe RGB (1998)",
    "ITU-R BT.2020",
    "ARRI Wide Gamut 3",
    "ARRI Wide Gamut 4",
    "S-Gamut3",
    "S-Gamut3.Cine",
    "F-Gamut",
    "V-Gamut",
    "ProPhoto RGB",
    "ECI RGB v2",
)

# Transfer functions: canonical name -> colour-science cctf function name.
# None means identity (linear).
_TRANSFER_KEYS: dict[str, str | None] = {
    "Linear": None,
    "sRGB": "sRGB",
    "Rec.709 OETF": "ITU-R BT.709",
    "Gamma 2.2": "Gamma 2.2",
    "Gamma 2.4": "Gamma 2.4",
    "Gamma 2.6": "Gamma 2.6",
    "ITU-R BT.1886": "ITU-R BT.1886",
    "ARRI LogC3": "ARRI LogC3",
    "ARRI LogC4": "ARRI LogC4",
    "S-Log3": "S-Log3",
    "F-Log": "F-Log",
    "F-Log2": "F-Log2",
    "V-Log": "V-Log",
}
TRANSFER_CHOICES: tuple[str, ...] = tuple(_TRANSFER_KEYS)

CAT_CHOICES: tuple[str, ...] = ("Bradford", "CAT02", "CAT16", "Von Kries", "XYZ Scaling")
DEFAULT_CAT = "Bradford"

# ICC PCS uses this fixed D50, not the slightly different tabulated CIE xy.
D50 = colour.XYZ_to_xy(np.array([0.9642, 1.0, 0.8249]))


class ColorspaceError(ValueError):
    pass


def get_colourspace(name: str):
    """Resolve a gamut name to a colour-science RGB_Colourspace."""
    try:
        return colour.RGB_COLOURSPACES[canonical_gamut(name)]
    except KeyError as exc:
        raise ColorspaceError(
            f"unknown gamut: {name!r} (available: {', '.join(GAMUT_CHOICES)})"
        ) from exc


def get_transfer_key(name: str) -> str | None:
    """Resolve a transfer name to its colour-science cctf key (None = linear)."""
    try:
        return _TRANSFER_KEYS[canonical_transfer(name)]
    except KeyError as exc:
        raise ColorspaceError(
            f"unknown transfer function: {name!r} (available: {', '.join(TRANSFER_CHOICES)})"
        ) from exc


def canonical_gamut(name: str) -> str:
    aliases = {"bt709": "ITU-R BT.709", "bt.709": "ITU-R BT.709", "rec.709": "ITU-R BT.709",
               "bt2020": "ITU-R BT.2020", "srgb": "sRGB"}
    return aliases.get(name.lower(), name)


def canonical_transfer(name: str) -> str:
    aliases = {"linear": "Linear", "srgb": "sRGB", "gamma2.2": "Gamma 2.2", "gamma2.4": "Gamma 2.4",
               "gamma2.6": "Gamma 2.6", "bt1886": "ITU-R BT.1886", "logc3": "ARRI LogC3",
               "logc4": "ARRI LogC4", "itu-r bt.709": "Rec.709 OETF"}
    return aliases.get(name.lower(), name)


def encode_transfer(name: str, values: np.ndarray) -> np.ndarray:
    key = get_transfer_key(name)
    if key is None:
        return np.asarray(values, dtype=np.float64)
    if key.startswith("Gamma "):
        values = np.asarray(values, dtype=np.float64)
        return np.sign(values) * np.abs(values) ** (1.0 / float(key.split()[1]))
    return colour.cctf_encoding(values, function=key)


def decode_transfer(name: str, values: np.ndarray) -> np.ndarray:
    key = get_transfer_key(name)
    if key is None:
        return np.asarray(values, dtype=np.float64)
    if key.startswith("Gamma "):
        values = np.asarray(values, dtype=np.float64)
        return np.sign(values) * np.abs(values) ** float(key.split()[1])
    return colour.cctf_decoding(values, function=key)


def check_cat(name: str) -> str:
    if name not in CAT_CHOICES:
        raise ColorspaceError(f"unknown chromatic adaptation: {name!r} (available: {', '.join(CAT_CHOICES)})")
    return name


def rgb_to_rgb_linear(rgb: np.ndarray, source_gamut: str, target_gamut: str, cat: str = DEFAULT_CAT) -> np.ndarray:
    """Linear-light gamut conversion including white point adaptation."""
    return colour.RGB_to_RGB(
        rgb,
        get_colourspace(source_gamut),
        get_colourspace(target_gamut),
        chromatic_adaptation_transform=check_cat(cat),
    )


def xyz_adapt(xyz: np.ndarray, xyz_from, xyz_to, cat: str = DEFAULT_CAT) -> np.ndarray:
    """Explicit chromatic adaptation of XYZ arrays between white points."""
    if np.allclose(xyz_from, xyz_to, atol=1e-6):
        return np.asarray(xyz, dtype=np.float64)
    return colour.adaptation.chromatic_adaptation_VonKries(
        xyz, xyz_from, xyz_to, transform=check_cat(cat)
    )


def _whitepoint_xyz(cs) -> np.ndarray:
    """White point as XYZ (colour stores colourspace white points as xy chromaticity)."""
    return colour.xy_to_XYZ(cs.whitepoint)


_D50_XYZ = colour.xy_to_XYZ(D50)


def rgb_linear_to_xyz_d50(rgb: np.ndarray, gamut: str, cat: str = DEFAULT_CAT) -> np.ndarray:
    """Linear RGB (gamut native white point) -> XYZ, adapted to D50."""
    cs = get_colourspace(gamut)
    xyz = colour.RGB_to_XYZ(
        rgb, cs, illuminant=cs.whitepoint, chromatic_adaptation_transform=None
    )
    return xyz_adapt(xyz, _whitepoint_xyz(cs), _D50_XYZ, cat)


def xyz_d50_to_rgb_linear(xyz: np.ndarray, gamut: str, cat: str = DEFAULT_CAT) -> np.ndarray:
    """XYZ (D50) -> linear RGB in the given gamut (D50 -> native white point)."""
    cs = get_colourspace(gamut)
    xyz_native = xyz_adapt(xyz, _D50_XYZ, _whitepoint_xyz(cs), cat)
    return np.asarray(xyz_native) @ np.asarray(cs.matrix_XYZ_to_RGB).T


def xyz_d50_to_lab(xyz: np.ndarray) -> np.ndarray:
    return colour.XYZ_to_Lab(xyz, illuminant=D50)


def lab_to_xyz_d50(lab: np.ndarray) -> np.ndarray:
    return colour.Lab_to_XYZ(lab, illuminant=D50)
