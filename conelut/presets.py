"""Convenience presets over the separate gamut/transfer model (spec sections 11, 12).

Presets always set BOTH the input and the output encoding of the LUT; for full
control use the explicit gamut/transfer options. Comment hints in a .cube file
never decide the transfer function automatically.
"""

from __future__ import annotations


def _pair(gamut: str, transfer: str) -> dict[str, str]:
    return {
        "input_gamut": gamut,
        "input_transfer": transfer,
        "output_gamut": gamut,
        "output_transfer": transfer,
    }


PRESETS: dict[str, dict[str, str]] = {
    "sRGB": _pair("sRGB", "sRGB"),
    "Rec.709 Gamma 2.4": _pair("ITU-R BT.709", "Gamma 2.4"),
    "Rec.709 Gamma 2.2": _pair("ITU-R BT.709", "Gamma 2.2"),
    "Rec.709 OETF": _pair("ITU-R BT.709", "Rec.709 OETF"),
    "BT.1886": _pair("ITU-R BT.709", "ITU-R BT.1886"),
    "Linear (BT.709)": _pair("ITU-R BT.709", "Linear"),
    "ARRI LogC3": _pair("ARRI Wide Gamut 3", "ARRI LogC3"),
    "ARRI LogC4": _pair("ARRI Wide Gamut 4", "ARRI LogC4"),
    "Sony S-Log3": _pair("S-Gamut3.Cine", "S-Log3"),
    "Fujifilm F-Log": _pair("F-Gamut", "F-Log"),
    "Fujifilm F-Log2": _pair("F-Gamut", "F-Log2"),
    "Panasonic V-Log": _pair("V-Gamut", "V-Log"),
}

# Legacy preset keys from 1.x legacy kept working for old scripts.
LEGACY_PRESET_ALIASES: dict[str, str] = {
    "sRGB": "sRGB",
    "LogC3": "ARRI LogC3",
    "LogC4": "ARRI LogC4",
    "S-Log3": "Sony S-Log3",
    "F-Log": "Fujifilm F-Log",
    "F-Log2": "Fujifilm F-Log2",
    "V-Log": "Panasonic V-Log",
}

GAMUT_NAME_TAGS: dict[str, str] = {
    "sRGB": "sRGB",
    "ITU-R BT.709": "BT709",
    "Display P3": "P3",
    "P3-D65": "P3D65",
    "DCI-P3": "DCIP3",
    "Adobe RGB (1998)": "AdobeRGB",
    "ITU-R BT.2020": "BT2020",
    "ARRI Wide Gamut 3": "AWG3",
    "ARRI Wide Gamut 4": "AWG4",
    "S-Gamut3": "SG3",
    "S-Gamut3.Cine": "SG3C",
    "F-Gamut": "FG",
    "V-Gamut": "VG",
    "ProPhoto RGB": "ProPhoto",
    "ECI RGB v2": "ECIRGB",
}

TRANSFER_NAME_TAGS: dict[str, str] = {
    "Linear": "Lin",
    "sRGB": "sRGB",
    "Rec.709 OETF": "709OETF",
    "Gamma 2.2": "G22",
    "Gamma 2.4": "G24",
    "Gamma 2.6": "G26",
    "ITU-R BT.1886": "BT1886",
    "ARRI LogC3": "LogC3",
    "ARRI LogC4": "LogC4",
    "S-Log3": "SLog3",
    "F-Log": "FLog",
    "F-Log2": "FLog2",
    "V-Log": "VLog",
}


def resolve_preset(name: str) -> dict[str, str] | None:
    """Resolve a preset by current or legacy name; None when unknown."""
    canonical = LEGACY_PRESET_ALIASES.get(name, name)
    return PRESETS.get(canonical)
