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
) -> FileResult:
    """Convert one CUBE, optionally validate, and write the ICC + report.

    ``verbose_log=False`` suppresses the detailed validation dump (callers
    such as the GUI show ``FileResult.summary`` instead); ``write_json=False``
    skips the per-file ``.validation.json`` (the caller aggregates into a
    per-run report instead).
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
    icc_bytes, _stats = generate_profile(cube, base, params, log=log)
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
