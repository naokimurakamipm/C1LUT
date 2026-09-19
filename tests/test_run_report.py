"""Per-run aggregate report: consolidation logic and compact formatting."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import identity_cube, make_synthetic_base, write_cube  # noqa: E402

from conelut.report import (  # noqa: E402
    RunReport,
    format_metrics_line,
    format_overall_line,
    run_report_path,
    settings_from_params,
)


def _summary(mean=0.01, p95=0.02, max=0.03, lcms=True, status="PASS", **extra):
    data = {
        "samples": 100, "mean": mean, "median": mean, "p95": p95, "p99": p95, "max": max,
        "validation_status": status, "lut_size": 33,
    }
    if lcms:
        data["lcms2_mean"] = mean * 1.1
        data["lcms2_max"] = max * 1.1
    else:
        data["independent_error"] = "native lcms2 runtime unavailable"
    data.update(extra)
    return data


def test_totals_and_overall_status():
    report = RunReport.start("base.icc", {"validation_samples": 10})
    report.add("a.cube", "a.icc", "success", summary=_summary())
    report.add("b.cube", "b.icc", "success", summary=_summary(mean=0.4, p95=0.5, max=0.6, status="REVIEW"))
    report.add("c.cube", None, "error", "boom")

    assert report.totals == {"files": 3, "success": 2, "error": 1, "skipped": 0, "cancelled": 0}
    validation = report.validation_summary
    assert validation["validated"] == 2
    assert validation["overall_status"] == "REVIEW"
    assert validation["worst_mean_dE2000"]["cube"] == "b.cube"
    assert validation["worst_max_dE2000"]["cube"] == "b.cube"
    assert validation["by_status"] == {"PASS": 1, "REVIEW": 1}


def test_overall_status_unverified_without_lcms():
    report = RunReport.start("base.icc", {})
    report.add("a.cube", "a.icc", "success", summary=_summary(lcms=False, status="UNVERIFIED"))
    assert report.validation_summary["overall_status"] == "UNVERIFIED"
    entry = report.as_dict()["files"][0]
    assert entry["lcms2_check"]["error"]


def test_no_validation_entries_gives_none():
    report = RunReport.start("base.icc", {})
    report.add("a.cube", "a.icc", "success", "保存しました")
    assert report.validation_summary is None
    assert report.as_dict()["files"][0] == {
        "cube": "a.cube", "input_path": "a.cube", "output_path": "a.icc", "status": "success",
        "message": "保存しました",
    }


def test_write_round_trip(tmp_path):
    report = RunReport.start("base.icc", {"icc_grid": 33})
    report.add("a.cube", "a.icc", "success", summary=_summary(below_pct=1.5, above_pct=0.25))
    path = report.write(run_report_path(tmp_path))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tool"] == "C-One LUT"
    assert data["base_icc"] == "base.icc"
    assert data["settings"] == {"icc_grid": 33}
    entry = data["files"][0]
    assert entry["metrics_dE2000"]["mean"] == pytest.approx(0.01, abs=1e-4)
    assert entry["domain_clamped_pct"] == {"below": 1.5, "above": 0.25}
    assert entry["lut_size"] == 33
    assert run_report_path(tmp_path).name.startswith("COneLUT-run-")


def test_format_metrics_line():
    line = format_metrics_line(_summary(mean=0.0019, p95=0.0041, max=0.0288))
    assert "ΔE2000 平均 0.0019" in line
    assert "P95 0.0041" in line
    assert "lcms2 平均" in line
    assert line.endswith("→ PASS")
    assert format_metrics_line(None) == ""
    assert format_metrics_line({}) == ""
    unverified = format_metrics_line(_summary(lcms=False, status="UNVERIFIED"))
    assert "lcms2 検証なし" in unverified


def test_format_overall_line():
    assert format_overall_line(None) == "検証: なし"
    report = RunReport.start("b", {})
    report.add("a.cube", "a.icc", "success", summary=_summary())
    line = format_overall_line(report.validation_summary)
    assert "1 ファイル" in line and "PASS" in line and "最悪" in line


def test_settings_from_params_covers_all_knobs():
    from conelut.pipeline import ConversionParams

    params = ConversionParams()
    settings = settings_from_params(params, validation_samples=42)
    assert settings["icc_grid"] == params.icc_grid
    assert settings["validation_samples"] == 42
    assert set(settings) == {
        "input_gamut", "input_transfer", "output_gamut", "output_transfer",
        "midtone_gamma", "interpolation", "icc_grid", "icc_intent",
        "chromatic_adaptation", "domain_policy", "lut_domain_policy", "cms_precision",
        "desc_mode", "validation_samples",
    }


def test_convert_file_returns_summary_without_per_file_json(tmp_path):
    from conelut.convert import convert_file
    from conelut.cms import BaseProfile
    from conelut.pipeline import ConversionParams

    base_path = make_synthetic_base(tmp_path / "Cam-Generic.icc")
    cube = write_cube(tmp_path / "film.cube", identity_cube(5))
    logs = []
    result = convert_file(cube, BaseProfile(base_path), ConversionParams(),
                          output_dir=tmp_path / "out", validation_samples=1500,
                          write_json=False, verbose_log=False, log=logs.append)

    assert result.status == "success"
    assert result.summary is not None
    assert result.summary["mean"] < 0.10
    assert "lcms2_mean" in result.summary
    assert result.summary["validation_status"] in {"PASS", "UNVERIFIED"}
    assert not list((tmp_path / "out").glob("*.validation.json"))
    # Quiet mode: no detailed dump, but the run still logs the essential lines.
    assert not any("Samples:" in line for line in logs)
    assert any("Wrote:" in line for line in logs)
