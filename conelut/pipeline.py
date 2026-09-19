"""Reference colour pipeline: Camera RGB -> Base ICC -> LUT -> PCS.

Reference path:

    camera RGB grid
      -> Base ICC A2Bx evaluation (float, intent-matched)   [cms.py]
      -> PCS (D50) -> sRGB linear                           [Bradford CAT]
      -> target input gamut (linear) -> input transfer encode
      -> source CUBE LUT (native grid, tetrahedral)
      -> optional additional midtone gamma (opt-in; default 1.0)
      -> output transfer decode -> output gamut linear
      -> XYZ (D50, Bradford CAT)
      -> PCS (Lab or XYZ, following the base profile header)

The generated profile is meant to be used with Capture One's
"Linear Response" curve; no proprietary curve is emulated here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .colorspaces import (
    D50,
    decode_transfer,
    encode_transfer,
    rgb_linear_to_xyz_d50,
    rgb_to_rgb_linear,
    xyz_d50_to_lab,
)
from .cube import CubeLUT
from .cms import BaseProfile
from .icc import (
    A2B_TAGS,
    PCS_LAB,
    PCS_XYZ,
    encode_legacy_lab16,
    encode_xyz16,
    make_desc,
    make_mft2,
    make_text,
    make_xyz_type,
    write_profile,
)

INTENT_CHOICES = ("perceptual", "relative", "saturation", "mirror")
INTENT_TAG = {"perceptual": 0, "relative": 1, "saturation": 2}
ICC_GRID_CHOICES = (17, 33, 49, 65)
DEFAULT_GRID = 33
# desc tag policy: "base" keeps the base profile's description verbatim, which
# is what Capture One matches camera profiles against; "look" writes
# "<Camera>-<Look>" in Capture One's own naming style.
DESC_MODES = ("base", "look")


@dataclass(frozen=True)
class ConversionParams:
    input_gamut: str = "sRGB"
    input_transfer: str = "sRGB"
    output_gamut: str = "sRGB"
    output_transfer: str = "sRGB"
    midtone_gamma: float = 1.0
    interpolation: str = "tetrahedral"
    icc_grid: int = DEFAULT_GRID
    icc_intent: str = "perceptual"
    cat: str = "Bradford"
    domain_policy: str = "clamp"
    lut_domain_policy: str = "clamp"
    precision: str = "float"
    description: str = ""
    desc_mode: str = "base"

    def validate(self) -> None:
        from .colorspaces import get_colourspace, get_transfer_key, check_cat
        from .cms import PRECISION_CHOICES
        from .interpolation import METHODS
        for gamut in (self.input_gamut, self.output_gamut):
            get_colourspace(gamut)
        for transfer in (self.input_transfer, self.output_transfer):
            get_transfer_key(transfer)
        check_cat(self.cat)
        if self.interpolation not in METHODS or self.precision not in PRECISION_CHOICES:
            raise ValueError("invalid interpolation or CMS precision")
        if self.domain_policy == "extrapolate" and self.interpolation == "nearest":
            raise ValueError("extrapolate requires tetrahedral or trilinear interpolation")
        if self.icc_intent not in INTENT_CHOICES:
            raise ValueError(f"unknown ICC rendering intent: {self.icc_intent!r}")
        if not (2 <= self.icc_grid <= 129):
            raise ValueError("icc_grid must be between 2 and 129")
        if not (np.isfinite(self.midtone_gamma) and self.midtone_gamma > 0):
            raise ValueError("midtone gamma must be a positive finite number")
        if self.domain_policy not in ("clamp", "error", "extrapolate"):
            raise ValueError(f"unknown domain policy: {self.domain_policy!r}")
        if self.lut_domain_policy not in ("clamp", "error"):
            raise ValueError(f"unknown LUT domain policy: {self.lut_domain_policy!r}")
        if self.desc_mode not in DESC_MODES:
            raise ValueError(f"unknown desc mode: {self.desc_mode!r} (expected one of {DESC_MODES})")


def sampling_intent(params: ConversionParams) -> int:
    """Intent used to sample the base profile (equal to the generated tag intent)."""
    if params.icc_intent == "mirror":
        return 0
    return INTENT_TAG[params.icc_intent]


def camera_grid(grid: int) -> np.ndarray:
    """Camera RGB grid points, red varying slowest (ICC CLUT sample order)."""
    x = np.linspace(0.0, 1.0, grid)
    rr, gg, bb = np.meshgrid(x, x, x, indexing="ij")
    return np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)


def reference_transform(
    params: ConversionParams,
    base: BaseProfile,
    lut: CubeLUT,
    rgb: np.ndarray,
    stats: dict | None = None,
) -> tuple[str, np.ndarray]:
    """Evaluate the full reference path for camera RGB samples.

    Returns ``(pcs_kind, values)`` with pcs_kind ``"Lab"`` or ``"XYZ"``.
    ``stats`` accumulates domain/clamp diagnostics for the validation report.
    """
    stats = stats if stats is not None else {}
    intent = base.resolve_intent(sampling_intent(params), stats.setdefault("warnings", []))

    _kind, values = base.evaluate(rgb, intent)
    linear_srgb = _pcs_to_srgb_linear(params, base, values)

    target_linear = linear_srgb
    if params.input_gamut != "sRGB":
        target_linear = rgb_to_rgb_linear(linear_srgb, "sRGB", params.input_gamut, params.cat)

    encoded = _encode_with_policy(params, target_linear, stats)
    lut_out = lut.apply(
        encoded,
        interpolation=params.interpolation,
        domain_policy=params.domain_policy,
        stats=stats,
        lut_domain_policy=params.lut_domain_policy,
    )
    if params.midtone_gamma != 1.0:
        lut_out = np.sign(lut_out) * np.abs(lut_out) ** (1.0 / params.midtone_gamma)

    decoded = decode_transfer(params.output_transfer, lut_out)
    if not np.isfinite(decoded).all():
        raise ValueError("output transfer produced non-finite values; check LUT output encoding")

    xyz_d50 = rgb_linear_to_xyz_d50(decoded, params.output_gamut, params.cat)
    if base.pcs == PCS_LAB:
        return "Lab", xyz_d50_to_lab(xyz_d50)
    return "XYZ", xyz_d50


def _pcs_to_srgb_linear(params: ConversionParams, base: BaseProfile, pcs_values: np.ndarray) -> np.ndarray:
    from .colorspaces import lab_to_xyz_d50, xyz_d50_to_rgb_linear

    if base.pcs == PCS_LAB:
        xyz = lab_to_xyz_d50(pcs_values)
    else:
        xyz = pcs_values
    return xyz_d50_to_rgb_linear(xyz, "sRGB", params.cat)


def _encode_with_policy(params: ConversionParams, linear: np.ndarray, stats: dict) -> np.ndarray:
    negative = linear < 0.0
    if negative.any():
        pct = 100.0 * np.count_nonzero(negative) / linear.size
        _record(stats, "input_encode_negative_pct", pct)
    encoded = encode_transfer(params.input_transfer, linear)
    if not np.isfinite(encoded).all():
        raise ValueError("input transfer produced non-finite values; check LUT input encoding")
    return encoded


def _record(stats: dict, key: str, value: float) -> None:
    stats[key] = stats.get(key, 0.0) + value


def generate_profile(
    cube: CubeLUT,
    base: BaseProfile,
    params: ConversionParams,
    log=print,
    *,
    input_curves=None,
    node_optimize: bool = False,
    node_optimize_samples: int = 4096,
    node_optimize_seed: int = 20260919,
) -> tuple[bytes, dict]:
    """Convert a parsed CUBE against a base profile and return (icc bytes, stats).

    ``input_curves`` (from :mod:`conelut.shaper`) redistributes the CLUT nodes
    toward steep regions via mft2 input tables. ``node_optimize`` additionally
    nudges node values to reduce the between-node error, guarded by an
    independent sample set (see :mod:`conelut.nodeopt`).
    """
    params.validate()
    stats: dict = {"warnings": []}

    grid = params.icc_grid
    if input_curves is not None:
        from .shaper import warped_camera_grid
        rgb = warped_camera_grid(input_curves, grid)
    else:
        rgb = camera_grid(grid)

    pcs_kind, pcs_values = reference_transform(params, base, cube, rgb, stats)
    if not np.isfinite(pcs_values).all():
        raise ValueError("reference transform produced non-finite PCS values")

    if node_optimize:
        clut, opt_report = _optimize_nodes(
            pcs_values.reshape(grid, grid, grid, 3), pcs_kind, params, base, cube,
            input_curves, node_optimize_samples, node_optimize_seed)
        pcs_values = clut.reshape(-1, 3)
        stats["node_optimize"] = opt_report
        if opt_report["improved"]:
            log(f"  Node optimization: independent mean dE00 "
                f"{opt_report['check_mean_dE00_before']:.4f} -> {opt_report['check_mean_dE00_after']:.4f} "
                f"(iteration {opt_report['best_iteration']})")
        else:
            log("  Node optimization: no improvement on the independent set; kept exact node values")

    encoded = encode_legacy_lab16(pcs_values) if pcs_kind == "Lab" else encode_xyz16(pcs_values)
    a2b_data = make_mft2(encoded.reshape(-1), grid, input_curves=input_curves)

    intent_tags: list[bytes]
    if params.icc_intent == "mirror":
        intent_tags = [A2B_TAGS[0], A2B_TAGS[1]]
    else:
        intent_tags = [A2B_TAGS[INTENT_TAG[params.icc_intent]]]

    description = params.description or profile_description(cube, base, params.desc_mode)
    header = bytearray(base.profile.header)
    tags: list[tuple[bytes, bytes]] = []
    for entry in base.profile.tags:
        if entry.signature in A2B_TAGS.values():
            continue
        if entry.signature == b"desc":
            continue
        if entry.signature == b"cprt":
            continue
        tags.append((entry.signature, entry.data))
    original_desc = base.profile.tag(b"desc")
    tags.append((b"desc", original_desc if original_desc is not None and params.desc_mode == "base" and not params.description else make_desc(description, base.profile.version)))
    copyright_text = (
        f"Generated by C-One LUT (COneLUT) | base: {base.path.name} | look: {cube.title}"
    )
    tags.append((b"cprt", make_desc(copyright_text, base.profile.version) if base.profile.version >= (4, 0) else make_text(copyright_text)))
    for tag in intent_tags:
        tags.append((tag, a2b_data))

    import os
    import tempfile

    handle, temp_name = tempfile.mkstemp(suffix=".icc")
    os.close(handle)
    try:
        write_profile(temp_name, header, tags)
        data = Path(temp_name).read_bytes()
    finally:
        Path(temp_name).unlink(missing_ok=True)

    stats["pcs"] = pcs_kind
    stats["icc_grid"] = grid
    stats["intent_tags"] = [t.decode() for t in intent_tags]
    log(f"  PCS: {pcs_kind} | CLUT {grid}^3 | tags: {', '.join(stats['intent_tags'])}")
    return data, stats


def _optimize_nodes(clut, pcs_kind, params, base, cube, input_curves, samples, seed):
    """Nudge CLUT node values; ships only when an independent set improves."""
    import colour

    from .nodeopt import optimize_clut_values

    def metric(ref, gen):
        return colour.delta_E(ref, gen, method="CIE 2000")

    rng = np.random.default_rng(seed)
    train_rgb = rng.random((samples, 3))
    check_rgb = np.random.default_rng(seed + 1).random((samples, 3))
    _train_kind, ref_train = reference_transform(params, base, cube, train_rgb, {})
    _check_kind, ref_check = reference_transform(params, base, cube, check_rgb, {})

    def coords_for(rgb):
        if input_curves is not None:
            warped = np.stack([
                np.interp(rgb[:, c], np.linspace(0.0, 1.0, input_curves[c].size), input_curves[c])
                for c in range(3)
            ], axis=-1)
        else:
            warped = rgb
        return np.clip(warped, 0.0, 1.0) * (clut.shape[0] - 1)

    return optimize_clut_values(
        clut, pcs_kind, ref_train, ref_check,
        coords_for(train_rgb), coords_for(check_rgb),
        metric,
    )


def _sanitize_token(text: str) -> str:
    import re

    return re.sub(r"[^0-9A-Za-z_-]+", "", text).strip("-_") or ""


def camera_name(base: BaseProfile) -> str:
    """Camera token as Capture One writes it.

    Taken from the base profile's own desc ('FujiXT5-Generic' -> 'FujiXT5'),
    falling back to the file stem ('FujiXT5-Generic.icm').
    """
    for source in (base.profile.description(), base.path.stem):
        token = source.split("-")[0].strip()
        if token:
            return token
    return "Camera"


def look_name(cube: CubeLUT) -> str:
    """Look token from the CUBE file name, falling back to TITLE."""
    stem = cube.source_path.stem if cube.source_path is not None else ""
    return _sanitize_token(stem) or _sanitize_token(cube.title) or "LUT"


def output_filename(cube: CubeLUT, base: BaseProfile) -> str:
    """Output file name: '<Camera>-<Look>.icc' (Capture One's own naming style)."""
    return f"{_sanitize_token(camera_name(base)) or 'Camera'}-{look_name(cube)}.icc"


def profile_description(cube: CubeLUT, base: BaseProfile, desc_mode: str = "base") -> str:
    """Description (desc tag) Capture One shows in its profile list.

    ``base`` keeps the base profile's description verbatim — the string
    Capture One uses to associate the profile with the camera. ``look``
    writes ``<Camera>-<Look>`` following Capture One's own naming style.
    """
    if desc_mode == "look":
        return f"{camera_name(base)}-{look_name(cube)}"
    return base.profile.description() or camera_name(base)
