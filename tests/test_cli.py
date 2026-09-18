"""CLI integration tests (spec sections 25, 26, 28)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers import identity_cube, make_synthetic_base, write_cube

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import main  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    base = make_synthetic_base(tmp_path / "TestCamera-Generic.icc")
    cube = write_cube(tmp_path / "film.cube", identity_cube(9))
    return base, cube, tmp_path


def test_basic_conversion_with_validation(env):
    base, cube, tmp = env
    code = main([
        str(cube), "--base-icc", str(base),
        "--input-gamut", "sRGB", "--input-transfer", "sRGB",
        "--output-gamut", "sRGB", "--output-transfer", "sRGB",
        "--validate", "--validation-samples", "4000", "--icc-grid", "33",
        "--output-dir", str(tmp / "out"),
    ])
    assert code == 0
    outputs = list((tmp / "out").glob("*.icc"))
    assert len(outputs) == 1
    reports = list((tmp / "out").glob("*.validation.json"))
    assert len(reports) == 1
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert data["metrics"]["mean_delta_e_2000"] < 0.10
    assert data["meets_targets"] is True
    # Output follows the 1.x style <Camera>-<Look>.icc and keeps the base
    # profile's desc so Capture One links the profile to the camera.
    assert outputs[0].stem == "TestCamera-film"
    from conelut.icc import ICCProfile

    assert ICCProfile(outputs[0].read_bytes()).description() == "TestCamera-Generic"


def test_desc_mode_look_names_profiles_by_camera_and_look(env):
    base, cube, tmp = env
    code = main([
        str(cube), "--base-icc", str(base), "--desc-mode", "look",
        "--output-dir", str(tmp / "lookmode"),
    ])
    assert code == 0
    outputs = list((tmp / "lookmode").glob("*.icc"))
    assert [p.stem for p in outputs] == ["TestCamera-film"]
    from conelut.icc import ICCProfile

    assert ICCProfile(outputs[0].read_bytes()).description() == "TestCamera-film"


def test_legacy_mode(env, capsys):
    base, cube, tmp = env
    code = main([
        str(cube), "--base-icc", str(base), "--legacy",
        "--output-dir", str(tmp / "legacy"),
    ])
    assert code == 0
    assert list((tmp / "legacy").glob("*.icc"))
    err = capsys.readouterr().out
    assert "Legacy compatibility mode" in err


def test_deprecated_aliases(env, capsys):
    base, cube, tmp = env
    code = main([
        str(cube), "--base-icc", str(base),
        "--target-gamut", "sRGB", "--target-curve", "sRGB",
        "--lut-output-gamut", "sRGB", "--lut-output-curve", "sRGB",
        "--gamma", "1.0", "--output-dir", str(tmp / "alias"),
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "--target-gamut" in captured.err


def test_existing_policies(env):
    base, cube, tmp = env
    outdir = tmp / "existing"
    common = ["--base-icc", str(base), "--output-dir", str(outdir)]
    assert main([str(cube), *common, "--existing", "rename"]) == 0
    assert main([str(cube), *common, "--existing", "skip"]) == 0
    assert main([str(cube), *common, "--existing", "rename"]) == 0
    icc_files = list(outdir.glob("*.icc"))
    assert len(icc_files) == 2  # skip produced nothing new


def test_missing_base_icc_fails(env, capsys):
    _, cube, tmp = env
    with pytest.raises(SystemExit):
        main([str(cube)])
    capsys.readouterr()


def test_intent_probe(tmp_path):
    probe = tmp_path / "probe.icc"
    assert main(["--probe-intent", str(probe)]) == 0
    data = probe.read_bytes()
    assert data[36:40] == b"acsp"
    assert b"A2B0" in data[128:512] or data[:4]  # tag table exists
    from conelut.icc import ICCProfile

    profile = ICCProfile(data)
    assert profile.tag(b"A2B0") and profile.tag(b"A2B1") and profile.tag(b"A2B2")
