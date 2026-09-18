"""Validation pipeline tests: ΔE2000 acceptance criteria (spec sections 21, 22)."""

from __future__ import annotations

import numpy as np
import pytest

from conelut.cms import BaseProfile
from conelut.cube import CubeLUT, parse_cube
from conelut.pipeline import ConversionParams, generate_profile
from conelut.validation import validate_conversion
from helpers import identity_cube, make_synthetic_base, write_cube


@pytest.fixture()
def synthetic_base(tmp_path):
    return BaseProfile(make_synthetic_base(tmp_path / "base.icc"), precision="float")


def _convert_and_validate(tmp_path, synthetic_base, cube, params):
    icc_bytes, _ = generate_profile(cube, synthetic_base, params, log=lambda *_: None)
    icc_path = tmp_path / "out.icc"
    icc_path.write_bytes(icc_bytes)
    return validate_conversion(
        cube, synthetic_base, params, icc_path,
        random_samples=8000, include_lcms=True, include_cmm8=False,
        log=lambda *_: None,
    )


def test_identity_lut_meets_identity_targets(synthetic_base, tmp_path):
    cube = parse_cube(write_cube(tmp_path / "identity.cube", identity_cube(17)))
    assert cube.is_identity
    report = _convert_and_validate(tmp_path, synthetic_base, cube, ConversionParams())
    assert report.lut_is_identity
    assert report.metrics.mean < 0.10
    assert report.metrics.p95 < 0.25
    assert report.metrics.max < 1.0
    assert report.meets_targets


def test_creative_lut_metrics_and_regions(synthetic_base, tmp_path):
    # A moderate gamma-style creative LUT (out = in^1.15 on the encoded domain).
    size = 17
    x = np.linspace(0, 1, size)
    rr, gg, bb = np.meshgrid(x, x, x, indexing="ij")
    lut_table = np.stack([rr, gg, bb], -1) ** 1.15
    cube = CubeLUT(title="creative", size_3d=size, domain_min=np.zeros(3),
                   domain_max=np.ones(3), data_3d=np.ascontiguousarray(lut_table))
    report = _convert_and_validate(tmp_path, synthetic_base, cube, ConversionParams())
    assert not report.lut_is_identity
    assert np.isfinite(report.metrics.mean)
    assert report.metrics.mean < 0.25
    assert set(report.region_metrics) >= {
        "neutrals", "dark_tones", "midtones", "highlights", "high_saturation",
    }
    data = report.as_dict()
    assert data["metrics"]["samples"] == 8000
    assert "mean_delta_e_2000" in data["metrics"]
    # Round-trip through JSON
    import json

    json.dumps(data)


def test_validation_json_file(synthetic_base, tmp_path):
    cube = parse_cube(write_cube(tmp_path / "identity.cube", identity_cube(9)))
    report = _convert_and_validate(tmp_path, synthetic_base, cube, ConversionParams())
    path = report.to_json(tmp_path / "report.json")
    assert path.exists()
    assert "metrics" in path.read_text(encoding="utf-8")
