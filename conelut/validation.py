"""Independent ΔE2000 validation of the generated ICC (spec sections 21, 22, 38).

The reference path is evaluated in float at random camera RGB samples; the
generated path decodes the *written ICC file* (serialization included) and can
additionally be evaluated through a native lcms2 and the legacy 8-bit Pillow
CMM for cross-checking. Metrics: ΔE2000 mean / median / p95 / p99 / max plus
tone/hue region breakdowns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json

import numpy as np

import colour

from .cms import BaseProfile, BaseProfileError, parse_a2b_tag, create_lcms2_backend, lcms2_available
from .colorspaces import xyz_d50_to_lab, lab_to_xyz_d50
from .cube import CubeLUT
from .icc import A2B_TAGS, ICCError, ICCProfile, PCS_LAB, PCS_XYZ
from .pipeline import ConversionParams, camera_grid, reference_transform, sampling_intent

VALIDATION_SEED = 20260918
DEFAULT_RANDOM_SAMPLES = 100000

# Acceptance targets from spec section 22.
IDENTITY_TARGETS = {"mean": 0.10, "p95": 0.25, "max": 1.0}
CREATIVE_TARGETS = {"mean": 0.25, "p95": 0.75, "max": 2.0}


@dataclass
class Metrics:
    samples: int
    mean: float
    median: float
    p95: float
    p99: float
    max: float

    def as_dict(self) -> dict:
        return {
            "samples": self.samples,
            "mean_delta_e_2000": round(self.mean, 4),
            "median_delta_e_2000": round(self.median, 4),
            "p95_delta_e_2000": round(self.p95, 4),
            "p99_delta_e_2000": round(self.p99, 4),
            "max_delta_e_2000": round(self.max, 4),
        }


def _metrics_from(delta_e: np.ndarray) -> Metrics:
    values = np.asarray(delta_e, dtype=np.float64)
    return Metrics(
        samples=int(values.size),
        mean=float(values.mean()),
        median=float(np.median(values)),
        p95=float(np.percentile(values, 95)),
        p99=float(np.percentile(values, 99)),
        max=float(values.max()),
    )


class GeneratedProfile:
    """Evaluator for an ICC we (or anyone) wrote — decodes the file bytes."""

    def __init__(self, data: bytes, intent_tag: bytes = A2B_TAGS[0]):
        self.profile = ICCProfile(data)
        if bytes(self.profile.header[36:40]) != b"acsp":
            raise ICCError("generated file is missing the acsp signature")
        self.pcs = self.profile.require_pcs()
        tag_data = None
        for tag in intent_tag, A2B_TAGS[0], A2B_TAGS[1], A2B_TAGS[2]:
            tag_data = self.profile.tag(tag)
            if tag_data is not None:
                break
        if tag_data is None:
            raise ICCError("generated profile has no A2B tag")
        self.evaluator = parse_a2b_tag(tag_data, self.pcs)

    def evaluate_lab(self, rgb: np.ndarray) -> np.ndarray:
        pcs_values = self.evaluator(rgb)
        if self.pcs == PCS_LAB:
            return pcs_values
        return xyz_d50_to_lab(pcs_values)


def _lab_of(kind: str, values: np.ndarray) -> np.ndarray:
    return values if kind == "Lab" else xyz_d50_to_lab(values)


def compute_metrics(lab_reference: np.ndarray, lab_generated: np.ndarray) -> Metrics:
    delta = colour.delta_E(lab_reference, lab_generated, method="CIE 2000")
    return _metrics_from(delta)


def _region_masks(lab: np.ndarray) -> dict[str, np.ndarray]:
    lightness = lab[:, 0]
    chroma = np.hypot(lab[:, 1], lab[:, 2])
    hue = np.degrees(np.arctan2(lab[:, 2], lab[:, 1])) % 360.0
    return {
        "neutrals": chroma < 2.0,
        "skin_like_hues": (hue >= 20.0) & (hue < 60.0) & (chroma >= 2.0),
        "dark_tones": lightness < 25.0,
        "midtones": (lightness >= 25.0) & (lightness <= 75.0),
        "highlights": lightness > 75.0,
        "high_saturation": chroma > 50.0,
    }


def _cmm8_lab(icc_path: Path, rgb: np.ndarray) -> np.ndarray:
    """Evaluate the generated profile through Pillow's 8-bit LittleCMS path."""
    import io

    from PIL import Image, ImageCms

    lab_profile = ImageCms.createProfile("LAB")
    src = ImageCms.ImageCmsProfile(io.BytesIO(icc_path.read_bytes()))
    transform = ImageCms.buildTransform(src, lab_profile, "RGB", "LAB", renderingIntent=1)
    flat = (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)
    out = ImageCms.applyTransform(Image.fromarray(flat.reshape(1, -1, 3)), transform)
    arr = np.asarray(out).reshape(-1, 3)
    lab = np.empty(arr.shape, dtype=np.float64)
    lab[:, 0] = arr[:, 0] * 100.0 / 255.0
    lab[:, 1:] = arr[:, 1:].astype(np.int8)  # Pillow writes signed a*/b* bytes
    return lab


@dataclass
class ValidationReport:
    source_lut: str
    lut_size: int
    base_icc: str
    input: dict
    output: dict
    capture_one_curve: str
    midtone_gamma: float
    interpolation: str
    icc_grid: int
    icc_intent: str
    cms_precision: str
    cat: str
    metrics: Metrics
    region_metrics: dict[str, Metrics] = field(default_factory=dict)
    metrics_lcms: Metrics | None = None
    metrics_cmm8: Metrics | None = None
    metrics_grid: Metrics | None = None
    domain_stats: dict = field(default_factory=dict)
    lut_is_identity: bool = False
    random_samples: int = DEFAULT_RANDOM_SAMPLES
    seed: int = VALIDATION_SEED
    independent_error: str | None = None
    metrics_legacy: Metrics | None = None

    @property
    def targets(self) -> dict:
        return IDENTITY_TARGETS if self.lut_is_identity else CREATIVE_TARGETS

    @property
    def meets_targets(self) -> bool:
        t = self.targets
        return self.metrics_lcms is not None and all(
            m.mean <= t["mean"] and m.p95 <= t["p95"] and m.max <= t["max"]
            for m in (self.metrics, self.metrics_grid, self.metrics_lcms) if m is not None
        )

    @property
    def validation_status(self) -> str:
        if self.metrics_lcms is None:
            return "UNVERIFIED"
        return "PASS" if self.meets_targets else "REVIEW"

    def summary_lines(self) -> list[str]:
        m = self.metrics
        lines = [
            f"Samples:        {m.samples}",
            f"Mean dE00:      {m.mean:.4f}",
            f"Median dE00:    {m.median:.4f}",
            f"P95 dE00:       {m.p95:.4f}",
            f"P99 dE00:       {m.p99:.4f}",
            f"Max dE00:       {m.max:.4f}",
        ]
        if self.metrics_lcms is not None:
            lines.append(f"lcms2 check:    mean {self.metrics_lcms.mean:.4f} / max {self.metrics_lcms.max:.4f}")
        else:
            lines.append(f"Independent CMM: UNVERIFIED ({self.independent_error or 'disabled'})")
        if self.metrics_legacy is not None:
            lines.append(f"Legacy vs accurate reference: mean {self.metrics_legacy.mean:.4f} / max {self.metrics_legacy.max:.4f}")
        if self.metrics_cmm8 is not None:
            lines.append(f"8-bit CMM:      mean {self.metrics_cmm8.mean:.4f} / max {self.metrics_cmm8.max:.4f} (informational)")
        for name, region in self.region_metrics.items():
            lines.append(f"  {name:<16} mean {region.mean:.4f} / p95 {region.p95:.4f} / max {region.max:.4f}")
        for key in ("input_below_domain_pct", "input_above_domain_pct"):
            if key in self.domain_stats:
                lines.append(f"{key}: {self.domain_stats[key]:.3f}%")
        lines.append(
            f"Targets ({'identity' if self.lut_is_identity else 'creative'}): "
            f"mean<={self.targets['mean']}, p95<={self.targets['p95']}, max<={self.targets['max']} -> "
            + self.validation_status
        )
        return lines

    def as_dict(self) -> dict:
        data = {
            "source_lut": self.source_lut,
            "lut_size": self.lut_size,
            "base_icc": self.base_icc,
            "input": self.input,
            "output": self.output,
            "capture_one_curve": self.capture_one_curve,
            "midtone_gamma": self.midtone_gamma,
            "interpolation": self.interpolation,
            "icc_grid": self.icc_grid,
            "icc_intent": self.icc_intent,
            "cms_precision": self.cms_precision,
            "chromatic_adaptation": self.cat,
            "lut_is_identity": self.lut_is_identity,
            "sampling": {"random_samples": self.random_samples, "seed": self.seed},
            "metrics": self.metrics.as_dict(),
            "meets_targets": self.meets_targets,
            "validation_status": self.validation_status,
            "independent_error": self.independent_error,
            "targets": self.targets,
            "domain_stats": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.domain_stats.items()},
        }
        if self.region_metrics:
            data["region_metrics"] = {name: m.as_dict() for name, m in self.region_metrics.items()}
        if self.metrics_lcms is not None:
            data["metrics_lcms2"] = self.metrics_lcms.as_dict()
        if self.metrics_cmm8 is not None:
            data["metrics_cmm_8bit"] = self.metrics_cmm8.as_dict()
        if self.metrics_grid is not None:
            data["metrics_grid_points"] = self.metrics_grid.as_dict()
        if self.metrics_legacy is not None:
            data["metrics_legacy_vs_accurate_reference"] = self.metrics_legacy.as_dict()
        return data

    def to_json(self, path: str | Path) -> Path:
        path = Path(path)
        from .files import atomic_write
        atomic_write(path, json.dumps(self.as_dict(), indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        return path


def validate_conversion(
    cube: CubeLUT,
    base: BaseProfile,
    params: ConversionParams,
    icc_path: str | Path,
    random_samples: int = DEFAULT_RANDOM_SAMPLES,
    seed: int = VALIDATION_SEED,
    include_lcms: bool | None = None,
    include_cmm8: bool = False,
    log=print,
    verbose: bool = True,
) -> ValidationReport:
    """Measure how well the written ICC reproduces the reference pipeline.

    ``verbose=False`` skips printing the detailed report block; the returned
    report (and its numbers) is identical.
    """
    if random_samples < 0:
        raise ValueError("validation samples must be non-negative")
    icc_path = Path(icc_path)
    data = icc_path.read_bytes()
    generated = GeneratedProfile(data, A2B_TAGS.get(sampling_intent(params), A2B_TAGS[0]))

    rng = np.random.default_rng(seed)
    random_rgb = rng.random((random_samples, 3))
    grid_rgb = camera_grid(33)
    rgb = np.concatenate([random_rgb, grid_rgb], axis=0)

    stats: dict = {}
    lut_data = None
    if params.legacy:
        from .interpolation import resample_3d
        lut_data = resample_3d(cube.data_3d, 33, "nearest")
    pcs_kind, ref_values = reference_transform(params, base, cube, rgb, stats, lut_data=lut_data)
    lab_reference = _lab_of(pcs_kind, ref_values)

    pcs_gen = generated.evaluator(rgb)
    lab_generated = xyz_d50_to_lab(pcs_gen) if generated.pcs == PCS_XYZ else pcs_gen

    delta = colour.delta_E(lab_reference, lab_generated, method="CIE 2000")
    sample_slice = slice(None, random_samples or None)
    metrics = _metrics_from(delta[sample_slice])
    metrics_grid = _metrics_from(delta[random_samples:])

    lab_for_regions = lab_reference[sample_slice]
    region_metrics = {
        name: _metrics_from(delta[sample_slice][mask])
        for name, mask in _region_masks(lab_for_regions).items()
        if mask.any()
    }

    metrics_lcms = None
    independent_error = None
    if include_lcms is not False:
        backend = base_backend = None
        try:
            backend = create_lcms2_backend(icc_path, sampling_intent(params))
            base_backend = create_lcms2_backend(base.path, base.resolve_intent(sampling_intent(params)))
            if backend is None or base_backend is None:
                raise BaseProfileError("native lcms2 runtime unavailable")
            import copy
            independent_base = copy.copy(base)
            if base.precision != "8bit":
                def evaluate_native(samples, intent):
                    lab = base_backend(samples)
                    return "pcs", lab if base.pcs == PCS_LAB else lab_to_xyz_d50(lab)
                independent_base.evaluate = evaluate_native
            kind, ref = reference_transform(params, independent_base, cube, rgb, lut_data=lut_data)
            native_reference = _lab_of(kind, ref)
            native_delta = colour.delta_E(native_reference, backend(rgb), method="CIE 2000")
            # Include regular grid endpoints as well as random samples in the independent gate.
            metrics_lcms = _metrics_from(native_delta)
            region_metrics = {name: _metrics_from(native_delta[mask])
                              for name, mask in _region_masks(native_reference).items() if mask.any()}
            boundary = np.any((rgb <= 1 / 32) | (rgb >= 31 / 32), axis=1)
            region_metrics["near_gamut_boundary"] = _metrics_from(native_delta[boundary])
        except (BaseProfileError, OSError, ValueError) as exc:
            independent_error = str(exc)
        finally:
            for evaluator in (backend, base_backend):
                if evaluator is not None:
                    evaluator.close()
    else:
        independent_error = "independent CMM check disabled"

    metrics_cmm8 = None
    if include_cmm8:
        lab_cmm8 = _cmm8_lab(icc_path, rgb)
        metrics_cmm8 = _metrics_from(
            colour.delta_E(lab_reference, lab_cmm8, method="CIE 2000")[sample_slice]
        )

    report = ValidationReport(
        source_lut=Path(cube.source_path).name if cube.source_path else cube.title,
        lut_size=cube.size_3d,
        base_icc=base.path.name,
        input={"gamut": params.input_gamut, "transfer": params.input_transfer},
        output={"gamut": params.output_gamut, "transfer": params.output_transfer},
        capture_one_curve=params.c1_curve,
        midtone_gamma=params.midtone_gamma,
        interpolation=params.interpolation,
        icc_grid=params.icc_grid,
        icc_intent=params.icc_intent,
        cms_precision=params.precision,
        cat=params.cat,
        metrics=metrics,
        region_metrics=region_metrics,
        metrics_lcms=metrics_lcms,
        metrics_cmm8=metrics_cmm8,
        metrics_grid=metrics_grid,
        domain_stats=stats,
        lut_is_identity=cube.is_identity,
        random_samples=random_samples,
        seed=seed,
        independent_error=independent_error,
    )
    if verbose:
        log("  Validation report")
        for line in report.summary_lines():
            log(f"  {line}")
    return report


def compare_legacy(cube, base, params, random_samples=DEFAULT_RANDOM_SAMPLES, log=print):
    """Measure the legacy preset against the same uncorrected accurate reference.

    The difference includes 8-bit sampling, interpolation and Film Standard
    compensation; it does not measure Capture One's proprietary rendering.
    """
    from dataclasses import replace
    import tempfile
    from .pipeline import generate_profile
    if params.legacy or params.c1_curve != "linear":
        raise ValueError("legacy comparison requires the accurate linear C1 curve")
    legacy_params = replace(params, legacy=True, precision="8bit", c1_curve="film-standard-legacy",
                            cat="CAT02", interpolation="trilinear", icc_grid=33)
    legacy_base = BaseProfile(base.path, precision="8bit")
    blob, _ = generate_profile(cube, legacy_base, legacy_params, log=lambda _: None)
    with tempfile.TemporaryDirectory(prefix="conelut-legacy-compare-") as folder:
        path = Path(folder) / "legacy.icc"
        path.write_bytes(blob)
        report = validate_conversion(cube, base, params, path, random_samples=random_samples, include_lcms=True, log=lambda _: None)
    if report.metrics_lcms is None:
        raise BaseProfileError(f"cannot independently compare legacy: {report.independent_error}")
    m = report.metrics_lcms
    log(f"  Legacy vs accurate reference: mean {m.mean:.4f} / p95 {m.p95:.4f} / max {m.max:.4f}")
    return m
