#!/usr/bin/env python3
"""Folder batch utility: converts every ``cube/*.cube`` into ``icc/*.icc``.

Uses the recommended Rec.709 Gamma 2.4 defaults with ΔE2000 validation.
Adjust the constants below or call the CLI (``python main.py``) for control.
"""

from __future__ import annotations

from pathlib import Path
import sys

from conelut.cms import BaseProfile, BaseProfileError
from conelut.convert import CONVERT_ERRORS, convert_file
from conelut.pipeline import ConversionParams
from conelut.report import RunReport, format_metrics_line, run_report_path, settings_from_params

BASE_ICC = Path("base.icc")  # <- put your camera profile here or edit the path
OUTPUT_DIR = Path("icc")


def main() -> int:
    cube_files = sorted(Path("cube").glob("*.cube"))
    if not cube_files:
        print(f"No .cube files found in {Path('cube').resolve()}")
        return 1
    try:
        base = BaseProfile(BASE_ICC, precision="float")
    except (BaseProfileError, OSError) as exc:
        print(f"error: {exc}")
        return 1
    params = ConversionParams(
        input_gamut="ITU-R BT.709",
        input_transfer="Gamma 2.4",
        output_gamut="ITU-R BT.709",
        output_transfer="Gamma 2.4",
    )
    OUTPUT_DIR.mkdir(exist_ok=True)
    failures = 0
    used = set()
    protected = set(cube_files) | {base.path}
    run_report = RunReport.start(base.path, settings_from_params(params, 50000))
    for cube_path in cube_files:
        print(f"Processing: {cube_path.name}")
        try:
            result = convert_file(cube_path, base, params,
                                  output_dir=OUTPUT_DIR, existing="rename",
                                  validate=True, validation_samples=50000,
                                  write_json=False, used=used, protected=protected)
            print(f"  [{result.status}] {result.message}")
            metrics = format_metrics_line(result.summary)
            if metrics:
                print(f"  {metrics}")
            run_report.add(cube_path, result.output_path, result.status, "",
                           summary=result.summary)
            failures += result.status == "error"
        except CONVERT_ERRORS as exc:
            print(f"  [error] {exc}")
            run_report.add(cube_path, None, "error", str(exc))
            failures += 1
    report_path = run_report.write(run_report_path(OUTPUT_DIR))
    print(f"Run report: {report_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
