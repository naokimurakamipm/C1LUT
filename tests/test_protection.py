"""Regression tests for the external-review findings.

Covers: base-ICC/input overwrite protection, the overwrite policy, and
matrix-shaper (no-A2B) base profiles in the float evaluator.
"""

from __future__ import annotations

import numpy as np
from helpers import identity_cube, make_synthetic_base, write_cube


def test_convert_file_never_overwrites_base(tmp_path):
    from conelut.cms import BaseProfile
    from conelut.convert import convert_file
    from conelut.pipeline import ConversionParams

    base_path = make_synthetic_base(tmp_path / "TestCamera-Generic.icc")
    before = base_path.read_bytes()
    base = BaseProfile(base_path)
    # 'Generic.cube' produces the output name TestCamera-Generic.icc: a
    # deliberate collision with the base profile itself.
    cube = write_cube(tmp_path / "Generic.cube", identity_cube(9))
    for existing in ("rename", "overwrite"):
        result = convert_file(cube, base, ConversionParams(), output_dir=tmp_path,
                              existing=existing, validate=False, log=lambda *_: None)
        assert result.status == "success"
        assert base_path.read_bytes() == before, f"base overwritten under policy {existing!r}"
    outputs = sorted(p.name for p in tmp_path.glob("TestCamera-Generic*.icc"))
    assert outputs == ["TestCamera-Generic (2).icc", "TestCamera-Generic (3).icc",
                       "TestCamera-Generic.icc"]


def test_convert_file_honours_overwrite_policy(tmp_path):
    from conelut.cms import BaseProfile
    from conelut.convert import convert_file
    from conelut.pipeline import ConversionParams

    base = BaseProfile(make_synthetic_base(tmp_path / "base.icc"))
    cube = write_cube(tmp_path / "film.cube", identity_cube(9))
    out = tmp_path / "out"
    first = convert_file(cube, base, ConversionParams(), output_dir=out,
                         existing="overwrite", validate=False, log=lambda *_: None)
    first_bytes = first.output_path.read_bytes()
    second = convert_file(cube, base, ConversionParams(icc_grid=17), output_dir=out,
                          existing="overwrite", validate=False, log=lambda *_: None)
    assert second.output_path == first.output_path
    assert second.output_path.read_bytes() != first_bytes  # genuinely overwrote
    assert [p.name for p in out.glob("*.icc")] == ["TestCamera-film.icc"]


def test_cli_never_overwrites_base(tmp_path, capsys):
    from main import main

    base_path = make_synthetic_base(tmp_path / "TestCamera-Generic.icc")
    before = base_path.read_bytes()
    cube = write_cube(tmp_path / "Generic.cube", identity_cube(9))
    code = main([str(cube), "--base-icc", str(base_path), "--output-dir", str(tmp_path),
                 "--existing", "overwrite", "--no-report-json"])
    assert code == 0
    assert base_path.read_bytes() == before
    assert (tmp_path / "TestCamera-Generic (2).icc").is_file()
    assert "別名で保存" in capsys.readouterr().out


def _write_matrix_profile(tmp_path):
    from PIL import ImageCms

    path = tmp_path / "srgb.icc"
    path.write_bytes(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    return path


def test_parametric_trc_decoding(tmp_path):
    """The parametricCurveType parser handles the lcms sRGB variant.

    Pillow's built-in sRGB stores its TRC as type 3 with five parameters
    (g, a, b, c, d) - the sRGB piecewise curve. It used to be misparsed
    (parameters read from the wrong offset), returning 0 everywhere.
    """
    from conelut.cms import _read_curve

    path = _write_matrix_profile(tmp_path)
    from conelut.icc import ICCProfile

    profile = ICCProfile(path.read_bytes())
    trc = profile.tag(b"rTRC")
    curve = _read_curve(trc)
    values = curve(np.array([1.0, 0.5, 0.04045, 0.0, 0.25]))
    expected = np.array([1.0, 0.21404, 0.0030353, 0.0, 0.0508000])
    assert np.allclose(values, expected, atol=2e-3)


def test_matrix_shaper_base_loads_and_evaluates(tmp_path):
    from conelut.cms import BaseProfile, BaseProfileError

    base = BaseProfile(_write_matrix_profile(tmp_path), precision="float")
    assert base.available_intents == []
    assert base.matrix_shaper is not None
    assert base.pcs == b"XYZ "
    kind, values = base.evaluate(np.array([[1.0, 1.0, 1.0], [0.0, 0.0, 0.0]]), 0)
    assert kind == "pcs"
    # Device white decodes to the D50 PCS white; black stays at zero.
    assert np.allclose(values[0], [0.9642, 1.0, 0.8249], atol=0.01)
    assert np.allclose(values[1], 0.0, atol=1e-9)

    garbage = tmp_path / "garbage.icc"
    garbage.write_bytes(b"not an icc at all")
    import pytest

    with pytest.raises(BaseProfileError):
        BaseProfile(garbage, precision="float")


def test_matrix_shaper_full_conversion(tmp_path):
    from conelut.cms import BaseProfile
    from conelut.convert import convert_file
    from conelut.icc import ICCProfile
    from conelut.pipeline import ConversionParams

    base = BaseProfile(_write_matrix_profile(tmp_path))
    cube = write_cube(tmp_path / "film.cube", identity_cube(9))
    result = convert_file(cube, base, ConversionParams(icc_grid=17), output_dir=tmp_path / "out",
                          validate=False, log=lambda *_: None)
    assert result.status == "success"
    profile = ICCProfile(result.output_path.read_bytes())
    assert profile.pcs == b"XYZ "  # follows the base header PCS
    assert profile.tag(b"A2B0") is not None
