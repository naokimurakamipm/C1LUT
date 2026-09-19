"""One aggregate JSON report per conversion run (spec sections 21, 22, 38).

Instead of a scatter of per-file ``.validation.json`` files, every conversion
run — GUI batch, CLI invocation, folder batch — produces a single run report
with the essential numbers per file plus batch-level totals. Detailed
per-file reports remain available opt-in via ``convert_file(write_json=True)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import json

TOOL_NAME = "C-One LUT"
_STATUS_SEVERITY = {"PASS": 0, "UNVERIFIED": 1, "REVIEW": 2}


def settings_from_params(params, validation_samples: int | None = None) -> dict:
    """Compact JSON-ready snapshot of the conversion settings."""
    settings = {
        "input_gamut": params.input_gamut,
        "input_transfer": params.input_transfer,
        "output_gamut": params.output_gamut,
        "output_transfer": params.output_transfer,
        "capture_one_curve": params.c1_curve,
        "midtone_gamma": params.midtone_gamma,
        "interpolation": params.interpolation,
        "icc_grid": params.icc_grid,
        "icc_intent": params.icc_intent,
        "chromatic_adaptation": params.cat,
        "domain_policy": params.domain_policy,
        "lut_domain_policy": params.lut_domain_policy,
        "cms_precision": params.precision,
        "legacy_mode": params.legacy,
        "desc_mode": params.desc_mode,
    }
    if validation_samples is not None:
        settings["validation_samples"] = validation_samples
    return settings


@dataclass
class RunReport:
    """Accumulates per-file outcomes; ``write`` emits one JSON per run."""

    base_icc: str
    settings: dict
    started_at: str
    entries: list[dict] = field(default_factory=list)

    @classmethod
    def start(cls, base_icc, settings: dict) -> "RunReport":
        return cls(str(base_icc), settings, datetime.now().isoformat(timespec="seconds"))

    def add(self, cube_path, output_path, status: str, message: str = "",
            summary: dict | None = None) -> None:
        entry: dict = {
            "cube": Path(cube_path).name if cube_path else None,
            "input_path": str(cube_path) if cube_path else None,
            "output_path": str(output_path) if output_path else None,
            "status": status,
        }
        if message:
            entry["message"] = message
        if summary:
            metrics = {key: round(summary[key], 4) for key in
                       ("mean", "median", "p95", "p99", "max") if summary.get(key) is not None}
            if metrics:
                entry["samples"] = summary.get("samples")
                entry["metrics_dE2000"] = metrics
            if summary.get("lcms2_mean") is not None:
                entry["lcms2_check"] = {"mean": round(summary["lcms2_mean"], 4),
                                        "max": round(summary["lcms2_max"], 4)}
            elif summary.get("independent_error"):
                entry["lcms2_check"] = {"error": summary["independent_error"]}
            if summary.get("validation_status"):
                entry["validation_status"] = summary["validation_status"]
            if summary.get("lut_size"):
                entry["lut_size"] = summary["lut_size"]
            if summary.get("below_pct") is not None:
                entry["domain_clamped_pct"] = {
                    "below": round(summary["below_pct"], 3),
                    "above": round(summary["above_pct"], 3),
                }
            if summary.get("legacy_mean") is not None:
                entry["legacy_vs_accurate_reference"] = {
                    "mean": round(summary["legacy_mean"], 4),
                    "max": round(summary["legacy_max"], 4),
                }
        self.entries.append(entry)

    @property
    def totals(self) -> dict:
        counts = {"success": 0, "error": 0, "skipped": 0, "cancelled": 0}
        for entry in self.entries:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        return {"files": len(self.entries), **counts}

    @property
    def validation_summary(self) -> dict | None:
        validated = [e for e in self.entries if "metrics_dE2000" in e]
        if not validated:
            return None
        by_status: dict[str, int] = {}
        for entry in validated:
            status = entry.get("validation_status", "UNVERIFIED")
            by_status[status] = by_status.get(status, 0) + 1
        worst = max(_STATUS_SEVERITY.get(s, 1) for s in by_status)
        overall = next(s for s, rank in _STATUS_SEVERITY.items() if rank == worst)
        worst_mean = max(validated, key=lambda e: e["metrics_dE2000"]["mean"])
        worst_max = max(validated, key=lambda e: e["metrics_dE2000"]["max"])
        return {
            "validated": len(validated),
            "by_status": by_status,
            "overall_status": overall,
            "worst_mean_dE2000": {"value": worst_mean["metrics_dE2000"]["mean"],
                                  "cube": worst_mean["cube"]},
            "worst_max_dE2000": {"value": worst_max["metrics_dE2000"]["max"],
                                 "cube": worst_max["cube"]},
        }

    def as_dict(self) -> dict:
        return {
            "tool": TOOL_NAME,
            "started_at": self.started_at,
            "base_icc": self.base_icc,
            "settings": self.settings,
            "totals": self.totals,
            "validation": self.validation_summary,
            "files": self.entries,
        }

    def write(self, path) -> Path:
        from .files import atomic_write

        path = Path(path)
        atomic_write(path, json.dumps(self.as_dict(), indent=2, ensure_ascii=False,
                                      allow_nan=False).encode("utf-8"))
        return path


def run_report_path(directory) -> Path:
    """Default per-run filename: unique per run, sorts chronologically."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(directory) / f"COneLUT-run-{stamp}.json"


def format_metrics_line(summary: dict) -> str:
    """Compact one-line metrics for logs and the GUI ("必要数値だけ")."""
    if not summary or summary.get("mean") is None:
        return ""
    parts = [f"ΔE2000 平均 {summary['mean']:.4f} / P95 {summary['p95']:.4f} / 最大 {summary['max']:.4f}"]
    if summary.get("lcms2_mean") is not None:
        parts.append(f"lcms2 平均 {summary['lcms2_mean']:.4f} / 最大 {summary['lcms2_max']:.4f}")
    elif summary.get("independent_error"):
        parts.append(f"lcms2 検証なし ({summary['independent_error']})")
    if summary.get("validation_status"):
        parts.append(f"→ {summary['validation_status']}")
    return "  |  ".join(parts)


def format_overall_line(validation_summary: dict | None) -> str:
    if not validation_summary:
        return "検証: なし"
    return (f"検証サマリ: {validation_summary['validated']} ファイル / "
            f"{validation_summary['overall_status']}  |  "
            f"最悪 平均 {validation_summary['worst_mean_dE2000']['value']:.4f}・"
            f"最大 {validation_summary['worst_max_dE2000']['value']:.4f}")
