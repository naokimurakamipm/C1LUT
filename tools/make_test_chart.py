"""Generate a test chart and its expected look for on-device verification.

The chart contains camera-RGB gradients, a neutral wedge, skin-like patches
and saturated primaries - the patterns that expose banding and hue shifts in
an ICC profile. Two PNGs are written:

* ``<prefix>_input.png``      the chart as camera RGB (what to feed through
                              the camera/profile path),
* ``<prefix>_expected.png``   the reference pipeline's rendering of that chart
                              (base ICC + LUT -> sRGB display), i.e. what
                              Capture One should approximately show when the
                              generated profile + Linear Response are used.

Usage:
    python tools/make_test_chart.py --base-icc LeicaSL-Generic.icm --lut look.cube \
        --preset "Rec.709 Gamma 2.4" --out chart

Then compare in Capture One per the README's verification protocol.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from conelut.cms import BaseProfile  # noqa: E402
from conelut.colorspaces import encode_transfer, rgb_linear_to_xyz_d50, xyz_d50_to_lab  # noqa: E402
from conelut.cube import parse_cube  # noqa: E402
from conelut.pipeline import ConversionParams, reference_transform  # noqa: E402
from conelut.presets import resolve_preset  # noqa: E402


def build_chart(width: int = 720, height: int = 480) -> np.ndarray:
    """Camera RGB chart: ramps, neutral wedge, skin patches, saturated fields."""
    rgb = np.zeros((height, width, 3))
    band = height // 6

    # Row 0-1: full R/G/B ramps over a gray base (banding probes).
    ramp = np.linspace(0.0, 1.0, width)
    for row, channel in ((0, 0), (1, 1)):
        rgb[row * band:(row + 1) * band, :, channel] = ramp
        rgb[row * band:(row + 1) * band, :, :3] += 0.25 * (1.0 - np.abs(ramp - 0.5))[None, :, None]

    # Row 2: neutral wedge (the classic gray-coloration probe).
    rgb[2 * band:3 * band, :, :] = ramp[None, :, None]

    # Row 3: dark-to-mid smooth gradient (shadow banding probe).
    shadow = (ramp ** 2.2) * 0.35
    rgb[3 * band:4 * band, :, :] = shadow[None, :, None]

    # Row 4: skin-like patches at several lightness levels.
    skins = np.array([
        [0.85, 0.62, 0.50], [0.72, 0.50, 0.40], [0.58, 0.39, 0.30],
        [0.45, 0.29, 0.22], [0.30, 0.19, 0.14], [0.18, 0.11, 0.08],
    ])
    patch = width // skins.shape[0]
    for i, colour in enumerate(skins):
        rgb[4 * band:5 * band, i * patch:(i + 1) * patch, :] = colour

    # Row 5: saturated primaries/secondaries + white/black anchors.
    fields = np.array([
        [1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 0], [0, 1, 1], [1, 0, 1],
        [1, 1, 1], [0.05, 0.05, 0.05],
    ])
    patch = width // fields.shape[0]
    for i, colour in enumerate(fields):
        rgb[5 * band:6 * band, i * patch:(i + 1) * patch, :] = colour
    return np.clip(rgb, 0.0, 1.0)


def to_display_srgb(lab: np.ndarray) -> np.ndarray:
    """Lab (D50) -> displayable sRGB bytes, out-of-gamut clipped."""
    from conelut.colorspaces import lab_to_xyz_d50, xyz_d50_to_rgb_linear

    xyz = lab_to_xyz_d50(lab)
    linear = xyz_d50_to_rgb_linear(xyz, "sRGB", "Bradford")
    return (np.clip(linear, 0.0, 1.0) ** (1.0 / 2.2) * 255.0 + 0.5).astype(np.uint8)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-icc", type=Path, required=True)
    parser.add_argument("--lut", type=Path, required=True)
    parser.add_argument("--preset", help="encoding preset (default Rec.709 Gamma 2.4)")
    parser.add_argument("--icc", type=Path, help="also render the *generated* ICC for A/B "
                                                 "(write it with main.py first, pass its path)")
    parser.add_argument("--out", type=Path, default=Path("chart"), help="output prefix")
    args = parser.parse_args()

    preset = resolve_preset(args.preset or "Rec.709 Gamma 2.4")
    params = ConversionParams(
        input_gamut=preset["input_gamut"], input_transfer=preset["input_transfer"],
        output_gamut=preset["output_gamut"], output_transfer=preset["output_transfer"],
    )
    base = BaseProfile(args.base_icc, precision="float")
    cube = parse_cube(args.lut)

    chart = build_chart()
    flat = chart.reshape(-1, 3)
    kind, pcs = reference_transform(params, base, cube, flat, {})
    lab = pcs if kind == "Lab" else xyz_d50_to_lab(pcs)
    expected = to_display_srgb(lab).reshape(chart.shape)

    Image.fromarray((chart * 255 + 0.5).astype(np.uint8)).save(args.out.with_name(args.out.name + "_input.png"))
    Image.fromarray(expected).save(args.out.with_name(args.out.name + "_expected.png"))
    print(f"wrote {args.out.name}_input.png and {args.out.name}_expected.png")

    if args.icc:
        from conelut.validation import GeneratedProfile

        got = GeneratedProfile(args.icc.read_bytes(), b"A2B0").evaluate_lab(flat)
        rendered = to_display_srgb(got).reshape(chart.shape)
        Image.fromarray(rendered).save(args.out.with_name(args.out.name + "_icc.png"))
        diff = np.abs(rendered.astype(int) - expected.astype(int))
        print(f"wrote {args.out.name}_icc.png  max |diff| {diff.max()}  mean {diff.mean():.2f}/255")
    print("Compare in Capture One: base profile vs generated profile (Curve = Linear Response),")
    print("then check the expected chart for banding / gray colouration / hue twists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
