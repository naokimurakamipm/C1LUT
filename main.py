#!/usr/bin/env python3
"""C-One LUT — command line interface.

Examples:

    python main.py LC_Spectra_Alliance.cube \\
        --base-icc LeicaSL-Generic.icc \\
        --input-gamut "ITU-R BT.709" --input-transfer "Gamma 2.4" \\
        --output-gamut "ITU-R BT.709" --output-transfer "Gamma 2.4" \\
        --midtone-gamma 1.0 \\
        --lut-interpolation tetrahedral --icc-grid 33 \\
        --icc-intent perceptual --validate

Outputs:
    LeicaSL601-LC_Alliance.icc
    COneLUT-run-YYYYMMDD-HHMMSS.json  (aggregate report for this run)

Detailed per-file reports are opt-in: --per-file-json writes
<name>.validation.json next to every ICC.
Intent probe for Capture One: --probe-intent probe.icc
GUI (default with no arguments): --gui
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from conelut.capture_one import write_intent_probe
from conelut.cms import PRECISION_CHOICES, BaseProfile, BaseProfileError
from conelut.colorspaces import CAT_CHOICES, GAMUT_CHOICES, TRANSFER_CHOICES, ColorspaceError
from conelut.cube import CubeParseError, parse_cube
from conelut.pipeline import (
    DEFAULT_GRID,
    ICC_GRID_CHOICES,
    ConversionParams,
)
from conelut.presets import PRESETS, resolve_preset
from conelut.convert import FileResult, PrecisionOptions, convert_file_adaptive
from conelut.files import destination as choose_destination
from conelut.report import RunReport, run_report_path, settings_from_params
from conelut.validation import DEFAULT_RANDOM_SAMPLES


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conelut",
        description="Convert CUBE LUTs to ICC camera input profiles for Capture One",
        epilog="Run without arguments to open the GUI.",
    )
    parser.add_argument("input_cube", nargs="*", type=Path, help="input .cube files")
    parser.add_argument("--base-icc", type=Path,
                        help="base camera ICC/ICM whose calibration is kept (e.g. LeicaSL-Generic.icc)")
    enc = parser.add_argument_group("LUT input/output encoding")
    enc.add_argument("--input-gamut", metavar="GAMUT",
                     help=f"default: sRGB; one of {', '.join(GAMUT_CHOICES)}")
    enc.add_argument("--input-transfer", metavar="TRANSFER",
                     help=f"default: sRGB; one of {', '.join(TRANSFER_CHOICES)}")
    enc.add_argument("--output-gamut", metavar="GAMUT")
    enc.add_argument("--output-transfer", metavar="TRANSFER")
    enc.add_argument("--preset", choices=sorted(PRESETS),
                     help="convenience preset that sets input AND output encoding")
    iccg = parser.add_argument_group("ICC generation")
    iccg.add_argument("--icc-grid", type=int, default=DEFAULT_GRID, choices=ICC_GRID_CHOICES,
                      help=f"ICC CLUT grid points (default {DEFAULT_GRID})")
    prec = parser.add_argument_group("Precision strategies")
    prec.add_argument("--grid-policy", default="fixed", choices=("fixed", "auto"),
                      help="auto: escalate the ICC grid from --icc-grid (33->49->65) until a pilot "
                           "validation meets --auto-max-mean / --auto-max-p95 (max stays advisory)")
    prec.add_argument("--auto-max-mean", type=float, default=0.05, metavar="DE",
                      help="auto grid acceptance: pilot mean dE00 threshold (default 0.05)")
    prec.add_argument("--auto-max-p95", type=float, default=0.25, metavar="DE",
                      help="auto grid acceptance: pilot p95 dE00 threshold (default 0.25)")
    prec.add_argument("--input-shaper", action="store_true",
                      help="derive error-weighted mft2 input curves from a pilot run; adopted per LUT "
                           "only when the pilot improves (helps steep looks)")
    prec.add_argument("--node-optimize", action="store_true",
                      help="experimental: nudge CLUT node values to reduce between-node error; "
                           "ships only when an independent sample set improves")
    iccg.add_argument("--icc-intent", default="perceptual",
                      choices=("perceptual", "relative", "saturation", "mirror"),
                      help="A2B tag the generated look is written to (default: perceptual; mirror = A2B0+A2B1)")
    iccg.add_argument("--cat", default="Bradford", choices=CAT_CHOICES,
                      help="chromatic adaptation method for the D50 PCS (default: Bradford)")
    lutg = parser.add_argument_group("LUT application")
    lutg.add_argument("--lut-interpolation", default="tetrahedral",
                      choices=("tetrahedral", "trilinear", "nearest"),
                      help="source CUBE interpolation (default: tetrahedral)")
    lutg.add_argument("--domain-policy", default="clamp", choices=("clamp", "error", "extrapolate"),
                      help="policy for inputs outside DOMAIN_MIN/MAX (default: clamp; clamp rate is reported)")
    lutg.add_argument("--lut-domain-policy", default="clamp", choices=("clamp", "error"),
                      help="policy for LUT samples outside the CLUT domain (default: clamp)")
    c1 = parser.add_argument_group("Capture One")
    c1.add_argument("--midtone-gamma", type=float, default=1.0,
                    help="additional midtone compensation; 1.0 = no compensation (default: 1.0)")
    c1.add_argument("--desc-mode", default="base", choices=("base", "look"),
                    help="desc tag shown in Capture One's profile list: 'base' keeps the base profile's "
                         "description verbatim so the profile stays linked to the camera (default); "
                         "'look' writes <Camera>-<Look> for distinguishable entries")
    cms = parser.add_argument_group("CMS precision")
    cms.add_argument("--cms-precision", default="float", choices=PRECISION_CHOICES,
                     help="base ICC sampling precision: float (default), lcms (native lcms2 if present)")
    val = parser.add_argument_group("Validation")
    val.add_argument("--validate", action="store_true",
                     help="measure dE2000 between the reference path and the written ICC")
    val.add_argument("--validation-samples", type=int, default=DEFAULT_RANDOM_SAMPLES,
                     help=f"random validation samples (default {DEFAULT_RANDOM_SAMPLES})")
    val.add_argument("--report-json", type=Path, help="path for the per-file detailed validation JSON")
    val.add_argument("--per-file-json", action="store_true",
                     help="also write the detailed .validation.json next to every ICC "
                          "(default: one aggregated run report per invocation)")
    val.add_argument("--no-report-json", action="store_true",
                     help="deprecated: per-file JSONs are off by default now (use --per-file-json to enable)")
    val.add_argument("--run-report", type=Path, metavar="PATH",
                     help="path for the per-run aggregate JSON report (default: beside the first output)")
    val.add_argument("--no-run-report", action="store_true",
                     help="skip writing the aggregate run report JSON")
    out = parser.add_argument_group("Output")
    out.add_argument("--output-dir", type=Path, help="output directory (default: beside each CUBE)")
    out.add_argument("--existing", default="overwrite", choices=("rename", "skip", "overwrite"),
                     help="behaviour for existing output files (CLI default: overwrite)")
    misc = parser.add_argument_group("Tools")
    misc.add_argument("--probe-intent", type=Path, metavar="PATH",
                      help="write the Capture One A2B intent probe ICC and exit")
    misc.add_argument("--gui", action="store_true", help=argparse.SUPPRESS)
    return parser


def _resolve_encoding(args) -> tuple[str, str, str, str]:
    preset = resolve_preset(args.preset) if args.preset else None
    input_gamut = args.input_gamut or (preset["input_gamut"] if preset else "sRGB")
    input_transfer = args.input_transfer or (preset["input_transfer"] if preset else "sRGB")
    output_gamut = args.output_gamut or (preset["output_gamut"] if preset else input_gamut)
    output_transfer = args.output_transfer or (preset["output_transfer"] if preset else input_transfer)
    from conelut.colorspaces import canonical_gamut, canonical_transfer
    return canonical_gamut(input_gamut), canonical_transfer(input_transfer), canonical_gamut(output_gamut), canonical_transfer(output_transfer)


def _destination(path: Path, policy: str, used: set, protected: set) -> Path | None:
    """Apply the --existing policy; never collide within one batch.

    Files in ``protected`` (the base profile and the input CUBEs) are never
    overwritten regardless of the policy - an output name such as
    <Camera>-Generic.icc can collide with the base profile itself.
    """

    return choose_destination(path, policy, used, protected, print)


def convert_one(cube_path: Path, base: BaseProfile, params: ConversionParams, args, used: set,
                protected: set) -> FileResult:
    precision = PrecisionOptions(
        grid_policy=args.grid_policy,
        auto_max_mean=args.auto_max_mean,
        auto_max_p95=args.auto_max_p95,
        input_shaper=args.input_shaper,
        node_optimize=args.node_optimize,
    )
    result = convert_file_adaptive(
        cube_path, base, params, precision,
        output_dir=args.output_dir, existing=args.existing,
        validate=args.validate, validation_samples=args.validation_samples,
        write_json=args.per_file_json or bool(args.report_json), log=print, used=used,
        protected=protected, report_json=args.report_json,
    )
    if result.status == "skipped":
        print(f"[skip] {cube_path.name}: output already exists")
    return result


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or "--gui" in argv:
        from gui import main as gui_main

        return gui_main([a for a in argv if a != "--gui"])

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.probe_intent:
        path = write_intent_probe(args.probe_intent)
        print(f"Intent probe profile written: {path}")
        print("Select it as the camera ICC in Capture One and inspect the render:")
        print("  reddish  -> Capture One uses A2B0 (perceptual)")
        print("  greenish -> Capture One uses A2B1 (relative colorimetric)")
        print("  blueish  -> Capture One uses A2B2 (saturation)")
        print("Then pass --icc-intent accordingly (or 'mirror' to write A2B0 and A2B1 identically).")
        return 0
    if not args.input_cube:
        parser.error("no input CUBE files (run without arguments for the GUI)")
    if args.base_icc is None:
        parser.error("--base-icc is required (the camera profile whose calibration is kept)")
    if args.validation_samples < 0:
        parser.error("--validation-samples must be non-negative (0 = regular grid only)")

    input_gamut, input_transfer, output_gamut, output_transfer = _resolve_encoding(args)
    try:
        params = ConversionParams(
            input_gamut=input_gamut,
            input_transfer=input_transfer,
            output_gamut=output_gamut,
            output_transfer=output_transfer,
            midtone_gamma=args.midtone_gamma,
            interpolation=args.lut_interpolation,
            icc_grid=args.icc_grid,
            icc_intent=args.icc_intent,
            cat=args.cat,
            domain_policy=args.domain_policy,
            lut_domain_policy=args.lut_domain_policy,
            precision=args.cms_precision,
            desc_mode=args.desc_mode,
        )
        params.validate()
    except (ValueError, ColorspaceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        base = BaseProfile(args.base_icc, precision=params.precision)
    except (BaseProfileError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    intent_names = ["A2B0", "A2B1", "A2B2"]
    tags_desc = ", ".join(intent_names[i] for i in base.available_intents) or "matrix-shaper (no A2B)"
    print(f"Base ICC: {base.path.name} | PCS: {base.pcs.decode()} | A2B tags: {tags_desc}")

    # Inputs and the base profile itself are never valid output destinations.
    protected = {os.path.normcase(str(args.base_icc.resolve()))}
    protected.update(
        os.path.normcase(str(path.resolve())) for path in args.input_cube if path.is_file()
    )
    exit_code = 0
    used: set = set()
    strategy = {
        "grid_policy": args.grid_policy,
        "auto_max_mean_dE00": args.auto_max_mean if args.grid_policy == "auto" else None,
        "auto_max_p95_dE00": args.auto_max_p95 if args.grid_policy == "auto" else None,
        "input_shaper": args.input_shaper,
        "node_optimize": args.node_optimize,
    }
    if not any(strategy.values()):
        strategy = None
    run_report = RunReport.start(base.path, settings_from_params(params, args.validation_samples),
                                 strategy=strategy)
    for cube_path in args.input_cube:
        try:
            if not cube_path.is_file():
                raise CubeParseError(f"CUBE file not found: {cube_path}")
            result = convert_one(cube_path, base, params, args, used, protected)
            run_report.add(result.input_path, result.output_path, result.status,
                           "" if result.status == "success" else result.message,
                           summary=result.summary, meta=result.meta)
        except (CubeParseError, BaseProfileError, ColorspaceError, ValueError, NotImplementedError, OSError) as exc:
            print(f"[error] {cube_path.name}: {exc}", file=sys.stderr)
            run_report.add(cube_path, None, "error", str(exc))
            exit_code = 1
    if not args.no_run_report:
        directory = args.run_report.parent if args.run_report else None
        if directory is None:
            outputs = [e["output_path"] for e in run_report.entries if e["output_path"]]
            first = Path(outputs[0]) if outputs else None
            directory = (args.output_dir or (first.parent if first else None)
                         or args.input_cube[0].parent)
        path = (args.run_report if args.run_report is not None
                else choose_destination(run_report_path(directory), args.existing, used, protected, print))
        if path is not None:
            run_report.write(path)
            print(f"Run report: {path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
