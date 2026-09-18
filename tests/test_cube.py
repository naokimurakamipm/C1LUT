"""CUBE parser tests (spec section 27)."""

from __future__ import annotations

import numpy as np
import pytest

from c1lut.cube import CubeLUT, CubeParseError, parse_cube
from helpers import identity_cube, write_cube


def test_identity_sizes(tmp_path):
    for size in (2, 17, 33, 65):
        path = write_cube(tmp_path / f"identity_{size}.cube", identity_cube(size))
        lut = parse_cube(path)
        assert lut.size_3d == size
        assert lut.is_identity
        assert lut.title == f"identity{size}"


def test_domain_min_max(tmp_path):
    content = (
        'TITLE "domain"\nDOMAIN_MIN 0.1 0.2 0.05\nDOMAIN_MAX 0.9 1.0 0.8\n'
        "LUT_3D_SIZE 2\n"
        "0.10 0.20 0.05\n0.90 0.20 0.05\n0.10 1.00 0.05\n0.90 1.00 0.05\n"
        "0.10 0.20 0.80\n0.90 0.20 0.80\n0.10 1.00 0.80\n0.90 1.00 0.80\n"
    )
    lut = parse_cube(write_cube(tmp_path / "domain.cube", content.encode()))
    assert np.allclose(lut.domain_min, [0.1, 0.2, 0.05])
    assert np.allclose(lut.domain_max, [0.9, 1.0, 0.8])
    out = lut.apply(np.array([[0.0, 0.0, 0.0]]))
    assert np.allclose(out[0], lut.domain_min, atol=1e-6)
    out_hi = lut.apply(np.array([[1.0, 1.0, 1.0]]))
    assert np.allclose(out_hi[0], lut.domain_max, atol=1e-6)


def test_domain_error_policy(tmp_path):
    content = (
        'TITLE "d"\nDOMAIN_MIN 0.2 0.2 0.2\nDOMAIN_MAX 0.8 0.8 0.8\n'
        "LUT_3D_SIZE 2\n"
        "0.2 0.2 0.2\n0.8 0.2 0.2\n0.2 0.8 0.2\n0.8 0.8 0.2\n"
        "0.2 0.2 0.8\n0.8 0.2 0.8\n0.2 0.8 0.8\n0.8 0.8 0.8\n"
    )
    lut = parse_cube(write_cube(tmp_path / "d.cube", content.encode()))
    with pytest.raises(ValueError):
        lut.apply(np.array([[0.0, 0.5, 0.5]]), domain_policy="error")
    out = lut.apply(np.array([[0.0, 0.5, 0.5]]), domain_policy="clamp")
    assert out[0, 0] == pytest.approx(0.2)
    stats: dict = {}
    lut.apply(np.array([[0.0, 0.5, 0.5]]), stats=stats)
    assert stats["input_below_domain_pct"] > 0


def test_1d_only_parses_but_is_not_convertible(tmp_path):
    content = 'TITLE "one"\nLUT_1D_SIZE 3\n0 0 0\n0.5 0.5 0.5\n1 1 1\n'
    lut = parse_cube(write_cube(tmp_path / "one.cube", content.encode()))
    assert lut.size_1d == 3
    assert lut.size_3d is None
    assert lut.data_3d is None
    with pytest.raises(CubeParseError):
        lut.apply(np.array([[0.5, 0.5, 0.5]]))


def test_1d_plus_3d(tmp_path):
    lines = ['TITLE "shaper"', "LUT_1D_SIZE 2", "0 0 0", "1 1 1", "LUT_3D_SIZE 2"]
    x = np.linspace(0, 1, 2)
    for b in x:
        for g in x:
            for r in x:
                lines.append(f"{r * b:.6f} {g * b:.6f} {b:.6f}")
    lut = parse_cube(write_cube(tmp_path / "shaper.cube", ("\n".join(lines) + "\n").encode()))
    assert lut.size_1d == 2 and lut.size_3d == 2
    out = lut.apply(np.array([[0.5, 0.5, 0.5]]))
    assert out.shape == (1, 3)


def test_malformed_count(tmp_path):
    content = 'TITLE "bad"\nLUT_3D_SIZE 2\n0 0 0\n0 0 0\n'
    with pytest.raises(CubeParseError):
        parse_cube(write_cube(tmp_path / "bad.cube", content.encode()))


def test_scientific_notation_and_whitespace(tmp_path):
    lines = ['TITLE "sci"', "LUT_3D_SIZE 2"]
    order = [(r, g, b) for b in (0.0, 1.0) for g in (0.0, 1.0) for r in (0.0, 1.0)]
    for r, g, b in order:
        lines.append(f"  {r:.1f}   {g:.1f}\t{b:.1f}  ")
    lut = parse_cube(write_cube(tmp_path / "sci.cube", ("\n".join(lines) + "\n").encode()))
    assert lut.is_identity


def test_comments_preserved_as_hints(tmp_path):
    body = identity_cube(2).decode().split("LUT_3D_SIZE 2\n")[1]
    content = '#Created by: test\nTITLE "hinted"\n#Input: Rec.709\nLUT_3D_SIZE 2\n' + body
    lut = parse_cube(write_cube(tmp_path / "hinted.cube", content.encode()))
    assert lut.metadata_hints.get("input") == "Rec.709"
    assert lut.title == "hinted"


def test_negative_and_above_one_values(tmp_path):
    lines = ['TITLE "range"', "LUT_3D_SIZE 2"]
    grid = [
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (1.0, 1.0, 0.0),
        (0.0, 0.0, 1.0), (1.0, 0.0, 1.0), (0.0, 1.0, 1.0), (1.0, 1.0, 1.0),
    ]
    for i, (r, g, b) in enumerate(grid):
        lines.append(f"{r - 0.05:.3f} {g:.3f} {b + (0.05 if i % 2 else 0):.3f}")
    lut = parse_cube(write_cube(tmp_path / "range.cube", ("\n".join(lines) + "\n").encode()))
    assert lut.data_3d.min() < 0.0
    assert lut.data_3d.max() > 1.0


def test_unsupported_directive(tmp_path):
    body = identity_cube(2).decode().split("LUT_3D_SIZE 2\n")[1]
    content = 'TITLE "x"\nLUT_3D_SIZE 2\nUNKNOWN_TAG 1 2 3\n' + body
    with pytest.raises(CubeParseError):
        parse_cube(write_cube(tmp_path / "x.cube", content.encode()))


def test_duplicate_declarations(tmp_path):
    with pytest.raises(CubeParseError):
        parse_cube(write_cube(tmp_path / "d.cube", b'TITLE "d"\nLUT_3D_SIZE 2\nLUT_3D_SIZE 2\n'))
    body = identity_cube(2).decode().split("LUT_3D_SIZE 2\n")[1]
    content = 'TITLE "d"\nDOMAIN_MIN 0 0 0\nDOMAIN_MIN 0 0 0\nLUT_3D_SIZE 2\n' + body
    with pytest.raises(CubeParseError):
        parse_cube(write_cube(tmp_path / "d2.cube", content.encode()))


def test_nan_rejected(tmp_path):
    body = identity_cube(2).decode().split("LUT_3D_SIZE 2\n")[1].replace("0.000000", "nan", 1)
    content = 'TITLE "nan"\nLUT_3D_SIZE 2\n' + body
    with pytest.raises(CubeParseError):
        parse_cube(write_cube(tmp_path / "nan.cube", content.encode()))


def test_apply_reports_stats(tmp_path):
    lut = parse_cube(write_cube(tmp_path / "i.cube", identity_cube(17)))
    stats: dict = {}
    out = lut.apply(np.array([[0.37, 0.71, 0.12], [1.0, 0.0, 0.5]]), stats=stats)
    assert out.shape == (2, 3)
    assert "input_below_domain_pct" in stats
    assert stats["input_below_domain_pct"] == 0.0
