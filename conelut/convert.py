"""Single-file conversion orchestration shared by the GUI and batch tools."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .cms import BaseProfile, BaseProfileError
from .colorspaces import ColorspaceError
from .cube import CubeLUT, CubeParseError, parse_cube
from .icc import ICCError
from .pipeline import ConversionParams, generate_profile, output_filename
from .validation import DEFAULT_RANDOM_SAMPLES, validate_conversion
from .files import atomic_write, destination as choose_destination, path_key


@dataclass(frozen=True)
class FileResult:
    input_path: Path
    output_path: Path | None
    report_path: Path | None
    status: str  # success | error | skipped
    message: str
    summary: dict | None = None  # compact validation numbers for run reports/GUI
    meta: dict | None = None  # precision strategy outcome (grid ladder, shaper, node opt)


@dataclass(frozen=True)
class PrecisionOptions:
    """Opt-in precision strategies for the CLUT baking.

    ``grid_policy='auto'`` escalates the ICC grid (starting from
    ``params.icc_grid``) until the *pilot* validation meets the mean/p95
    acceptance thresholds - max stays advisory because steep creative looks
    keep rare worst-case outliers even at 65^3. ``input_shaper`` derives
    error-weighted mft2 input curves from a pilot run, and ``node_optimize``
    nudges node values under independent-set control.
    """

    grid_policy: str = "fixed"  # "fixed" | "auto"
    auto_max_mean: float = 0.05
    auto_max_p95: float = 0.25
    input_shaper: bool = False
    node_optimize: bool = False
    pilot_samples: int = 10000
    shaper_samples: int = 8000

    @property
    def active(self) -> bool:
        return self.grid_policy == "auto" or self.input_shaper or self.node_optimize


PILOT_SEED = 20260920


def _pilot_run(cube, base, params, blob, samples: int, seed: int = PILOT_SEED):
    """Small-sample evaluation: per-sample inputs, dE00 and mean/p95."""
    import colour
    import numpy as np

    from .colorspaces import xyz_d50_to_lab
    from .icc import A2B_TAGS
    from .pipeline import reference_transform, sampling_intent
    from .validation import GeneratedProfile

    generated = GeneratedProfile(blob, A2B_TAGS.get(sampling_intent(params), A2B_TAGS[0]))
    rgb = np.random.default_rng(seed).random((samples, 3))
    kind, ref_pcs = reference_transform(params, base, cube, rgb, {})
    ref_lab = ref_pcs if kind == "Lab" else xyz_d50_to_lab(ref_pcs)
    gen_pcs = generated.evaluator(rgb)
    gen_lab = gen_pcs if generated.pcs == b"Lab " else xyz_d50_to_lab(gen_pcs)
    delta = colour.delta_E(ref_lab, gen_lab, method="CIE 2000")
    return rgb, delta


def convert_file_adaptive(
    cube_path: Path,
    base: BaseProfile,
    params: ConversionParams,
    precision: PrecisionOptions | None = None,
    *,
    output_dir: Path | None = None,
    existing: str = "rename",
    validate: bool = True,
    validation_samples: int = DEFAULT_RANDOM_SAMPLES,
    write_json: bool = True,
    log=lambda _message: None,
    protected=(),
    used: set | None = None,
    report_json: Path | None = None,
    verbose_log: bool = True,
) -> FileResult:
    """convert_file with the precision strategies applied.

    With the default PrecisionOptions this is exactly convert_file. Otherwise
    a pilot run picks the ICC grid (auto policy) and/or derives input shaper
    curves, then the final conversion + full validation runs once.
    """
    import numpy as np
    from dataclasses import replace

    precision = precision or PrecisionOptions()
    if not precision.active:
        return convert_file(
            cube_path, base, params,
            output_dir=output_dir, existing=existing, validate=validate,
            validation_samples=validation_samples, write_json=write_json, log=log,
            protected=protected, used=used, report_json=report_json,
            verbose_log=verbose_log,
        )

    cube = parse_cube(Path(cube_path).resolve())
    ladder = ([g for g in (17, 33, 49, 65) if g >= params.icc_grid]
              if precision.grid_policy == "auto" else [params.icc_grid])
    pilot_samples = max(1000, min(precision.pilot_samples, 20000))

    chosen_grid = params.icc_grid
    curves = None
    ladder_report = []
    for grid in ladder:
        attempt_params = replace(params, icc_grid=grid)
        blob, _stats = generate_profile(cube, base, attempt_params, log=lambda *_: None)
        rgb, delta = _pilot_run(cube, base, attempt_params, blob, pilot_samples)
        mean = float(delta.mean())
        p95 = float(np.percentile(delta, 95))
        ladder_report.append({"grid": grid, "pilot_mean_dE00": round(mean, 5),
                              "pilot_p95_dE00": round(p95, 5)})
        accepted = mean <= precision.auto_max_mean and p95 <= precision.auto_max_p95
        log(f"  Grid {grid}^3 pilot: mean {mean:.4f} / p95 {p95:.4f}"
            + (" -> accepted" if accepted else " -> below target, escalating" if grid != ladder[-1] else ""))
        if precision.input_shaper:
            # A/B on the pilot: keep the warp only when it actually helps this
            # LUT (steep looks gain, shallow looks can lose).
            from .shaper import derive_input_curves
            shaper_rgb, shaper_delta = _pilot_run(
                cube, base, attempt_params, blob, min(precision.shaper_samples, pilot_samples))
            candidate = derive_input_curves(shaper_rgb, shaper_delta)
            shaped_blob, _ = generate_profile(cube, base, attempt_params,
                                              log=lambda *_: None, input_curves=candidate)
            _rgb2, shaped_delta = _pilot_run(cube, base, attempt_params, shaped_blob, pilot_samples)
            if float(shaped_delta.mean()) < mean * 0.98:
                curves = candidate
                log(f"  Input shaper: pilot mean {mean:.4f} -> {float(shaped_delta.mean()):.4f}, adopted")
            else:
                curves = None
                log(f"  Input shaper: pilot mean {mean:.4f} -> {float(shaped_delta.mean()):.4f}, not adopted")
        chosen_grid = grid
        if accepted:
            break

    final_params = replace(params, icc_grid=chosen_grid)
    result = convert_file(
        cube_path, base, final_params,
        output_dir=output_dir, existing=existing, validate=validate,
        validation_samples=validation_samples, write_json=write_json, log=log,
        protected=protected, used=used, report_json=report_json,
        verbose_log=verbose_log,
        input_curves=curves if precision.input_shaper else None,
        node_optimize=precision.node_optimize,
    )
    meta = {
        "grid_policy": precision.grid_policy,
        "icc_grid": chosen_grid,
        "grid_ladder": ladder_report,
        "input_shaper": precision.input_shaper and curves is not None,
        "node_optimize": precision.node_optimize,
    }
    return replace(result, meta=meta)


def _summary_of(report) -> dict:
    """Essential validation numbers, small enough to cross thread boundaries."""
    summary: dict = {
        "samples": report.metrics.samples,
        "mean": report.metrics.mean,
        "median": report.metrics.median,
        "p95": report.metrics.p95,
        "p99": report.metrics.p99,
        "max": report.metrics.max,
        "validation_status": report.validation_status,
        "lut_size": report.lut_size,
    }
    if report.metrics_lcms is not None:
        summary["lcms2_mean"] = report.metrics_lcms.mean
        summary["lcms2_max"] = report.metrics_lcms.max
    if report.independent_error:
        summary["independent_error"] = report.independent_error
    for key, out in (("input_below_domain_pct", "below_pct"),
                     ("input_above_domain_pct", "above_pct")):
        if key in report.domain_stats:
            summary[out] = report.domain_stats[key]
    if report.luminance_metrics:
        summary["luminance_mean_dE"] = {
            name: round(bucket.mean, 4) for name, bucket in report.luminance_metrics.items()
        }
    return summary


def _key(path: Path) -> str:
    return path_key(path)


def convert_file(
    cube_path: Path,
    base: BaseProfile,
    params: ConversionParams,
    output_dir: Path | None = None,
    existing: str = "rename",
    validate: bool = True,
    validation_samples: int = DEFAULT_RANDOM_SAMPLES,
    write_json: bool = True,
    log=lambda _message: None,
    *,
    protected=(),
    used: set | None = None,
    report_json: Path | None = None,
    verbose_log: bool = True,
    input_curves=None,
    node_optimize: bool = False,
) -> FileResult:
    """Convert one CUBE, optionally validate, and write the ICC + report.

    ``verbose_log=False`` suppresses the detailed validation dump (callers
    such as the GUI show ``FileResult.summary`` instead); ``write_json=False``
    skips the per-file ``.validation.json`` (the caller aggregates into a
    per-run report instead). ``input_curves``/``node_optimize`` are the
    precision strategies from :class:`PrecisionOptions`, applied inside
    ``generate_profile``.
    """
    if validation_samples < 0:
        raise ValueError("validation samples must be non-negative (0 = regular grid only)")
    cube_path = Path(cube_path).resolve()
    used = used if used is not None else set()
    protected = set(protected) | {base.path, cube_path}
    cube: CubeLUT = parse_cube(cube_path)
    log(f"LUT: {cube.title} | {cube.size_3d}^3"
        + (f" | 1D shaper {cube.size_1d}" if cube.size_1d else ""))
    hints = cube.metadata_hints
    if "input" in hints:
        log(f"  Metadata hint: Input = {hints['input']} (hint only - transfer stays as selected)")

    directory = Path(output_dir) if output_dir is not None else cube_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    destination = choose_destination(directory / output_filename(cube, base), existing, used, protected, log)
    if destination is None:
        return FileResult(cube_path, None, None, "skipped", "同名ファイルがあるためスキップしました。")
    report_path = None
    if validate and write_json:
        report_path = choose_destination(report_json or destination.with_suffix(".validation.json"), existing, used, protected, log)
    icc_bytes, _stats = generate_profile(cube, base, params, log=log,
                                         input_curves=input_curves,
                                         node_optimize=node_optimize)
    atomic_write(destination, icc_bytes)
    log(f"  Wrote: {destination}")

    summary = None
    if validate:
        report = validate_conversion(
            cube, base, params, destination,
            random_samples=validation_samples, log=log, verbose=verbose_log,
        )
        summary = _summary_of(report)
        if report_path is not None:
            report.to_json(report_path)
            log(f"  Report: {report_path}")
        if not report.meets_targets:
            log(f"  warning: validation status = {report.validation_status}")

    message = f"保存しました: {destination}"
    if report_path is not None:
        message += f"\n検証レポート: {report_path}"
    return FileResult(cube_path, destination, report_path, "success", message, summary)


def _unique(path: Path) -> Path:
    number = 2
    candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
    while candidate.exists():
        number += 1
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
    return candidate


CONVERT_ERRORS = (CubeParseError, BaseProfileError, ColorspaceError, ValueError,
                  NotImplementedError, ICCError, OSError)
