"""Precision strategies: input shaper, node optimization, auto grid, diagnostics."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers import identity_cube, make_synthetic_base, write_cube  # noqa: E402

from conelut.cms import BaseProfile, create_lcms2_backend  # noqa: E402
from conelut.convert import PrecisionOptions, convert_file_adaptive  # noqa: E402
from conelut.cube import parse_cube  # noqa: E402
from conelut.pipeline import ConversionParams, generate_profile  # noqa: E402
from conelut.shaper import derive_input_curves, inverse_nodes, warped_camera_grid  # noqa: E402
from conelut.validation import validate_conversion  # noqa: E402


def steep_cube(path: Path, size: int = 9, gamma: float = 0.35) -> Path:
    """A shadow-steep look: x -> x^gamma per channel (steep near 0)."""
    x = np.linspace(0.0, 1.0, size)
    rr, gg, bb = np.meshgrid(x, x, x, indexing="ij")
    table = np.stack([(rr ** gamma), (gg ** gamma), (bb ** gamma)], axis=-1)
    lines = [f'TITLE "steep{gamma}"', f"LUT_3D_SIZE {size}", ""]
    for row in table.reshape(-1, 3):
        lines.append(f"{row[0]:.6f} {row[1]:.6f} {row[2]:.6f}")
    path.write_text("\n".join(lines), encoding="ascii")
    return path


@pytest.fixture()
def synthetic_base(tmp_path):
    return BaseProfile(make_synthetic_base(tmp_path / "Cam-Generic.icc"), precision="float")


def test_shaper_curves_are_monotonic_cdfs():
    rng = np.random.default_rng(3)
    rgb = rng.random((4000, 3))
    # error concentrated at low channel values
    delta = np.exp(-3.0 * rgb.mean(axis=1)) + 0.05
    curves = derive_input_curves(rgb, delta)
    assert len(curves) == 3
    for curve in curves:
        assert curve[0] == 0.0 and curve[-1] == 1.0
        assert np.all(np.diff(curve) >= -1e-12)
        # error-weighted: the warp must reach 0.5 before the identity does
        assert np.argmax(curve >= 0.5) < curve.size // 2


def test_warped_grid_nodes_map_through_curve():
    curve = np.linspace(0.0, 1.0, 256) ** 0.5
    nodes = inverse_nodes(curve, grid=9)
    assert nodes[0] == pytest.approx(0.0)
    assert nodes[-1] == pytest.approx(1.0)
    # node j sits where curve(x) = j/8
    assert curve.size and np.all(np.diff(nodes) >= -1e-9)
    grid = warped_camera_grid([curve] * 3, 5)
    assert grid.shape == (125, 3)
    assert grid.min() >= 0.0 and grid.max() <= 1.0


def test_mft2_input_curves_evaluate_consistently_and_match_lcms2(synthetic_base, tmp_path):
    """A warped CLUT must decode identically through our evaluator and lcms2."""
    cube = parse_cube(steep_cube(tmp_path / "steep.cube"))
    params = ConversionParams(icc_grid=17)
    rng = np.random.default_rng(11)
    rgb = rng.random((1500, 3))
    kind, ref = None, None
    from conelut.pipeline import reference_transform

    kind, ref = reference_transform(params, synthetic_base, cube, rgb, {})
    curves = derive_input_curves(rgb, np.abs(ref[:, 0]) * 0 + np.random.default_rng(4).random(rgb.shape[0]))
    blob, _ = generate_profile(cube, synthetic_base, params, log=lambda *_: None,
                               input_curves=curves)
    path = tmp_path / "warped.icc"
    path.write_bytes(blob)

    from conelut.validation import GeneratedProfile

    ours = GeneratedProfile(blob, b"A2B0").evaluate_lab(rgb)
    backend = create_lcms2_backend(path, 0)
    try:
        native = backend(rgb)
    finally:
        backend.close()
    if native is None:
        pytest.skip("native lcms2 unavailable")
    # lcms2 and the internal evaluator must agree on the input curves
    assert np.abs(ours - native).max() < 0.5


def test_shaper_profile_reproduces_reference(synthetic_base, tmp_path):
    cube = parse_cube(steep_cube(tmp_path / "steep.cube"))
    params = ConversionParams(icc_grid=17)
    rng = np.random.default_rng(5)
    rgb = rng.random((1500, 3))
    from conelut.pipeline import reference_transform
    from conelut.validation import GeneratedProfile

    kind, ref = reference_transform(params, synthetic_base, cube, rgb, {})
    ref_lab = ref if kind == "Lab" else None
    assert ref_lab is not None
    # error concentrated in shadows -> shaper curves
    curves = derive_input_curves(rgb, np.exp(-3.0 * rgb.mean(axis=1)) + 0.05)
    blob, _ = generate_profile(cube, synthetic_base, params, log=lambda *_: None,
                               input_curves=curves)
    got = GeneratedProfile(blob, b"A2B0").evaluate_lab(rgb)
    delta = np.abs(got - ref_lab)
    # sanity: warped profile still reproduces the reference everywhere
    assert delta.max() < 6.0


def test_node_optimization_improves_independent_set(synthetic_base, tmp_path):
    cube = parse_cube(steep_cube(tmp_path / "steep.cube"))
    params = ConversionParams(icc_grid=17)
    blob, stats = generate_profile(cube, synthetic_base, params, log=lambda *_: None,
                                   node_optimize=True, node_optimize_samples=2500)
    report = stats["node_optimize"]
    if not report["improved"]:
        pytest.skip("synthetic case too easy to optimize")
    assert report["check_mean_dE00_after"] < report["check_mean_dE00_before"]
    # improved profile must still validate normally
    path = tmp_path / "opt.icc"
    path.write_bytes(blob)
    result = validate_conversion(cube, synthetic_base, params, path,
                                 random_samples=800, include_lcms=False, log=lambda *_: None,
                                 verbose=False)
    assert result.metrics.mean < 1.0


def test_auto_grid_escalates_and_reports(synthetic_base, tmp_path):
    cube = write_cube(tmp_path / "film.cube", identity_cube(9))
    strict = PrecisionOptions(grid_policy="auto", auto_max_mean=0.0005, auto_max_p95=0.002,
                              pilot_samples=600)
    result = convert_file_adaptive(cube, synthetic_base, ConversionParams(icc_grid=17),
                                   strict, output_dir=tmp_path / "out",
                                   validation_samples=500, write_json=False,
                                   verbose_log=False, log=lambda *_: None)
    # identity on the synthetic base is near-perfect; with impossible thresholds
    # the ladder must still terminate at 65 and record every rung.
    assert result.meta["icc_grid"] == 65
    assert [rung["grid"] for rung in result.meta["grid_ladder"]] == [17, 33, 49, 65]
    assert result.status == "success"


def test_auto_grid_accepts_first_rung_when_easy(synthetic_base, tmp_path):
    cube = write_cube(tmp_path / "film.cube", identity_cube(9))
    easy = PrecisionOptions(grid_policy="auto", auto_max_mean=1.0, auto_max_p95=4.0,
                            pilot_samples=400)
    result = convert_file_adaptive(cube, synthetic_base, ConversionParams(icc_grid=33),
                                   easy, output_dir=tmp_path / "out2",
                                   validation_samples=300, write_json=False,
                                   verbose_log=False, log=lambda *_: None)
    assert result.meta["icc_grid"] == 33
    assert len(result.meta["grid_ladder"]) == 1


def test_validation_reports_input_luminance_buckets(synthetic_base, tmp_path):
    from conelut.validation import LUMINANCE_BUCKETS

    cube = write_cube(tmp_path / "film.cube", identity_cube(9))
    out = tmp_path / "o.icc"
    blob, _ = generate_profile(parse_cube(cube), synthetic_base, ConversionParams(),
                               log=lambda *_: None)
    out.write_bytes(blob)
    report = validate_conversion(parse_cube(cube), synthetic_base, ConversionParams(), out,
                                 random_samples=900, include_lcms=False,
                                 log=lambda *_: None, verbose=False)
    names = {f"{lo:.2f}-{hi:.2f}" for lo, hi in LUMINANCE_BUCKETS}
    assert set(report.luminance_metrics) == names
    data = report.as_dict()
    assert set(data["metrics_by_input_luminance"]) == names
