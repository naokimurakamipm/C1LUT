"""Regression cases from the September review, using native LittleCMS as oracle."""
import io
import json
import os
import struct
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageCms

from c1lut.cms import BaseProfile, BaseProfileError, MabTag, _read_curve, create_lcms2_backend, _evaluate_via_imagecms
from c1lut.colorspaces import lab_to_xyz_d50, rgb_linear_to_xyz_d50, xyz_d50_to_lab
from c1lut.capture_one import install_profile, build_intent_probe_profile
from c1lut.convert import convert_file
from c1lut.cube import CubeLUT, CubeParseError, parse_cube
from c1lut.files import destination, path_key
from c1lut.icc import ICCProfile, make_desc, make_xyz_type, write_profile
from c1lut.pipeline import ConversionParams, generate_profile, reference_transform
from c1lut.validation import validate_conversion
from helpers import identity_cube, identity_cube_data, make_synthetic_base, write_cube
from main import main, build_arg_parser, _resolve_encoding


def gamma_curve(gamma=2.2):
    return b"curv" + bytes(4) + struct.pack(">IH", 1, round(gamma * 256))


def para_curve(kind, values):
    return b"para" + bytes(4) + struct.pack(">HH", kind, 0) + struct.pack(f">{len(values)}i", *(round(v * 65536) for v in values))


def matrix_base(path, curve=None):
    profile = ICCProfile(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    tags = [(tag.signature, curve if curve is not None and tag.signature.endswith(b"TRC") else tag.data) for tag in profile.tags]
    write_profile(path, profile.header, tags)
    return path


def mab_profile(path, stages, pcs=b"XYZ "):
    # Serialized independently of the production mAB parser.
    tag = bytearray(b"mAB " + bytes(4) + bytes([3, 3, 0, 0]) + bytes(20))
    offsets = []
    for stage in stages:  # B, matrix, M, CLUT, A
        if stage is None:
            offsets.append(0)
        else:
            tag += bytes((-len(tag)) % 4)
            offsets.append(len(tag))
            tag += stage
    struct.pack_into(">5I", tag, 12, *offsets)
    header = bytearray(128)
    header[8:12] = bytes([4, 0x40, 0, 0])
    header[12:24] = b"scnrRGB " + pcs
    header[68:80] = struct.pack(">3i", 63190, 65536, 54061)
    write_profile(path, header, [(b"desc", make_desc("mAB oracle", (4, 4))), (b"wtpt", make_xyz_type((0.9642, 1, 0.8249))), (b"A2B0", bytes(tag))])
    return path


def curve_set(curves, padded=True):
    return b"".join(c + bytes((-len(c)) % 4) if padded else c for c in curves)


IDENTITY_CURVE = b"curv" + bytes(4) + struct.pack(">I", 0)


def native_lab(path, rgb, intent=0):
    backend = create_lcms2_backend(path, intent)
    assert backend is not None, "native CMM is required for these regressions"
    try:
        return backend(rgb)
    finally:
        backend.close()


@pytest.mark.parametrize("curve,expected", [
    (gamma_curve(), 0.5 ** (563 / 256)),
    (b"curv" + bytes(4) + struct.pack(">I3H", 3, 0, 16384, 65535), 16384 / 65535),
    (para_curve(4, [2.4, 1/1.055, .055/1.055, 1/12.92, .04045, .01, .02]), .224041),
])
def test_standard_curves(curve, expected):
    assert _read_curve(curve)(np.array([.5]))[0] == pytest.approx(expected, abs=3e-5)


@pytest.mark.parametrize("kind,params", [(0, [2.2]), (1, [2.2, 1, -.1]), (2, [2.2, 1, -.1, .05]),
                                         (3, [2.4, 1/1.055, .055/1.055, 1/12.92, .04045]),
                                         (4, [2.4, 1/1.055, .055/1.055, 1/12.92, .04045, .01, .02])])
def test_parametric_curves_against_native(tmp_path, kind, params):
    path = matrix_base(tmp_path / "base.icc", para_curve(kind, params))
    rgb = np.repeat(np.array([0, .02, .05, .1, .5, 1])[:, None], 3, axis=1)
    expected = lab_to_xyz_d50(native_lab(path, rgb))
    actual = BaseProfile(path).evaluate(rgb, 0)[1]
    assert np.allclose(actual, expected, atol=1e-4)


@pytest.mark.parametrize("padded", [True, False])
def test_mab_curve_alignment(tmp_path, padded):
    curves = curve_set([gamma_curve(1), gamma_curve(2), gamma_curve(3)], padded)
    path = mab_profile(tmp_path / "mab.icc", [curves, None, None, None, None])
    actual = BaseProfile(path).evaluate(np.array([[.5, .5, .5]]), 0)[1]
    assert np.allclose(actual, np.array([[.5, .25, .125]]) * 65535 / 32768)
    if padded:
        assert np.allclose(actual, lab_to_xyz_d50(native_lab(path, np.array([[.5, .5, .5]]))), atol=2e-4)


@pytest.mark.parametrize("precision", [1, 2])
@pytest.mark.parametrize("pcs", [b"XYZ ", b"Lab "])
def test_mab_all_stages_rectangular_against_native(tmp_path, precision, pcs):
    grid = (3, 4, 5)
    mesh = np.meshgrid(*(np.linspace(0, 1, n) for n in grid), indexing="ij")
    table = np.stack(mesh, -1)[..., [2, 0, 1]] * .7 + .1
    top = 255 if precision == 1 else 65535
    clut = bytes(grid) + bytes(13) + bytes([precision, 0, 0, 0]) + np.rint(table * top).astype('u1' if precision == 1 else '>u2').tobytes()
    matrix = np.array([[.7, .1, .02], [.01, .8, .03], [.02, .04, .7]])
    matrix_data = struct.pack(">12i", *(np.rint(np.r_[matrix.ravel(), [.02, .03, .01]] * 65536).astype(int)))
    path = mab_profile(tmp_path / "mab.icc", [curve_set([gamma_curve(1)] * 3), matrix_data,
        curve_set([gamma_curve(1.5)] * 3), clut, curve_set([para_curve(0, [1.3])] * 3)], pcs)
    rgb = np.random.default_rng(31).random((100, 3))
    actual = BaseProfile(path).evaluate(rgb, 0)[1]
    expected_lab = native_lab(path, rgb)
    expected = expected_lab if pcs == b"Lab " else lab_to_xyz_d50(expected_lab)
    assert np.allclose(actual, expected, atol=.01 if pcs == b"Lab " else 2e-4)


def test_native_xyz_base_returns_xyz_not_lab(tmp_path):
    path = matrix_base(tmp_path / "base.icc")
    rgb = np.array([[0, 0, 0], [.5, .5, .5], [1, 1, 1]])
    kind, values = BaseProfile(path, precision="lcms").evaluate(rgb, 0)
    assert kind == "pcs"
    assert np.allclose(values, BaseProfile(path).evaluate(rgb, 0)[1], atol=1e-4)


def test_native_invalid_dll_is_not_silent(tmp_path, monkeypatch):
    path = matrix_base(tmp_path / "base.icc")
    monkeypatch.setenv("C1LUT_LCMS2_DLL", str(tmp_path / "missing.dll"))
    with pytest.raises(BaseProfileError, match="cannot load"):
        BaseProfile(path, precision="lcms").evaluate(np.zeros((1, 3)), 0)


@pytest.mark.parametrize("intent", [0, 1, 2])
def test_8bit_intent_matches_pillow(tmp_path, intent):
    path = tmp_path / "probe.icc"
    path.write_bytes(build_intent_probe_profile())
    sample = np.array([[128, 128, 128]], dtype=np.uint8)
    transform = ImageCms.buildTransform(str(path), ImageCms.createProfile("sRGB"), "RGB", "RGB", renderingIntent=intent)
    expected = np.asarray(ImageCms.applyTransform(Image.fromarray(sample.reshape(1, 1, 3)), transform)).reshape(1, 3) / 255
    assert np.array_equal(_evaluate_via_imagecms(path, sample / 255, intent), expected)


@pytest.mark.parametrize("policy", ["overwrite", "rename", "skip"])
def test_relative_cli_preserves_base(tmp_path, monkeypatch, policy):
    base = make_synthetic_base(tmp_path / "TestCamera-Generic.icc")
    write_cube(tmp_path / "Generic.cube", identity_cube(3))
    original = base.read_bytes()
    monkeypatch.chdir(tmp_path)
    assert main(["Generic.cube", "--base-icc", "TestCamera-Generic.icc", "--existing", policy]) == 0
    assert base.read_bytes() == original
    assert (tmp_path / "TestCamera-Generic (2).icc").is_file()


@pytest.mark.parametrize("policy", ["overwrite", "rename", "skip"])
def test_installer_protects_base_and_honors_policy(tmp_path, policy):
    folder = tmp_path / "install"
    folder.mkdir()
    base = folder / "base.icc"
    base.write_bytes(b"base")
    source = tmp_path / "base.icc"
    source.write_bytes(b"generated")
    target = install_profile(source, folder, policy, protected=[base])
    assert base.read_bytes() == b"base"
    assert target.read_bytes() == b"generated"
    assert install_profile(target, folder, policy, protected=[base]) == target
    other = tmp_path / "other.icc"
    other.write_bytes(b"new")
    (folder / "other.icc").write_bytes(b"old")
    result = install_profile(other, folder, policy)
    if policy == "skip":
        assert result is None
    else:
        assert result.read_bytes() == b"new"
    assert (folder / "other.icc").read_bytes() == (b"new" if policy == "overwrite" else b"old")


def test_hardlink_protection(tmp_path):
    source = tmp_path / "source.icc"
    source.write_bytes(b"base")
    alias = tmp_path / "alias.icc"
    os.link(source, alias)
    assert destination(alias, "overwrite", protected=[source]) != alias


def test_batch_same_names_and_report_protection(tmp_path):
    base = make_synthetic_base(tmp_path / "base.icc")
    original = base.read_bytes()
    cubes = []
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        cubes.append(write_cube(tmp_path / folder / "look.cube", identity_cube(3)))
    out = tmp_path / "out"
    assert main([*(str(c) for c in cubes), "--base-icc", str(base), "--output-dir", str(out),
                 "--validate", "--validation-samples", "0", "--report-json", str(base)]) == 0
    assert len(list(out.glob("*.icc"))) == 2
    assert base.read_bytes() == original
    assert len(list(tmp_path.glob("base (*).icc"))) == 2


def test_negative_validation_samples_do_not_write(tmp_path):
    base = BaseProfile(make_synthetic_base(tmp_path / "base.icc"))
    cube = write_cube(tmp_path / "look.cube", identity_cube(3))
    with pytest.raises(ValueError, match="non-negative"):
        convert_file(cube, base, ConversionParams(), validation_samples=-1)
    assert not (tmp_path / "TestCamera-look.icc").exists()


def test_independent_check_detects_wrong_base_evaluator(tmp_path):
    path = matrix_base(tmp_path / "base.icc", gamma_curve())
    base = BaseProfile(path)
    cube = parse_cube(write_cube(tmp_path / "look.cube", identity_cube(9)))
    params = ConversionParams()
    # Reproduce the former curveType bug while leaving the actual source ICC correct.
    columns = [np.array(struct.unpack_from(">3i", base.profile.tag(sig), 8)) / 65536 for sig in (b"rXYZ", b"gXYZ", b"bXYZ")]
    base.matrix_shaper = lambda rgb: rgb @ np.column_stack(columns).T
    blob, _ = generate_profile(cube, base, params, log=lambda _: None)
    output = tmp_path / "wrong.icc"
    output.write_bytes(blob)
    report = validate_conversion(cube, base, params, output, random_samples=500, log=lambda _: None)
    assert report.metrics.mean < .02  # shared-parser self-consistency still looks excellent
    assert report.metrics_lcms.mean > 10
    assert not report.meets_targets
    assert report.validation_status == "REVIEW"
    skipped = validate_conversion(cube, base, params, output, random_samples=0, include_lcms=False, log=lambda _: None)
    assert skipped.metrics.samples == 33 ** 3
    assert skipped.validation_status == "UNVERIFIED"
    assert not skipped.meets_targets


def test_gamma_base_identity_preserves_render(tmp_path):
    path = matrix_base(tmp_path / "base.icc", gamma_curve())
    cube_path = write_cube(tmp_path / "look.cube", identity_cube(9))
    result = convert_file(cube_path, BaseProfile(path), ConversionParams(), validate=True, validation_samples=1000)
    rgb = np.array([[.5, .5, .5]])
    assert np.allclose(native_lab(path, rgb), native_lab(result.output_path, rgb), atol=.05)
    assert json.loads(result.report_path.read_text(encoding="utf-8"))["validation_status"] == "PASS"


def lut_fixture():
    return CubeLUT("test", 3, np.zeros(3), np.ones(3), identity_cube_data(3))


@pytest.mark.parametrize("method", ["trilinear", "tetrahedral"])
def test_extrapolation_and_boundary_stats(method):
    lut = lut_fixture()
    sample = np.array([[-.2, .3, 1.2], [0, 1, 0]])
    assert np.allclose(lut.apply(sample, method, "extrapolate"), sample)
    stats = {}
    lut.apply(sample[1:], stats=stats)
    assert stats["lut_clamp_pct"] == 0
    lut.apply(sample, stats=stats)
    assert stats["lut_clamp_pct"] == 50


def test_shaper_domain_policy_and_identity():
    lut = replace(lut_fixture(), data_1d=np.full((2, 3), 2.0), size_1d=2)
    assert not lut.is_identity
    with pytest.raises(ValueError, match="shaper output"):
        lut.apply(np.array([[.5] * 3]), lut_domain_policy="error")
    stats = {}
    assert np.allclose(lut.apply(np.array([[.5] * 3]), stats=stats), 1)
    assert stats["lut_clamp_pct"] == 100


def test_pipeline_preserves_negative_linear_lut_output(tmp_path):
    base = BaseProfile(make_synthetic_base(tmp_path / "base.icc"))
    lut = replace(lut_fixture(), data_3d=np.broadcast_to([-.1, .5, .5], (3, 3, 3, 3)).copy())
    kind, values = reference_transform(ConversionParams(output_transfer="Linear"), base, lut, np.array([[.5] * 3]))
    expected = xyz_d50_to_lab(rgb_linear_to_xyz_d50(np.array([[-.1, .5, .5]]), "sRGB"))
    assert kind == "Lab"
    assert np.allclose(values, expected)


@pytest.mark.parametrize("text", ["LUT_1D_SIZE 2\n0 0 0\n1 1 1\n2 2 2\n", "LUT_1D_SIZE 2\nDOMAIN_MIN nan 0 0\n0 0 0\n1 1 1\n",
                                 "LUT_1D_SIZE 2\nDOMAIN_MIN 2 0 0\n0 0 0\n1 1 1\n", "0 0 0\n"])
def test_parser_rejects_invalid_1d_domains_and_excess_rows(tmp_path, text):
    path = tmp_path / "bad.cube"
    path.write_text(text)
    with pytest.raises(CubeParseError):
        parse_cube(path)


def test_1d_only_defaults_validated(tmp_path):
    lut = parse_cube(write_cube(tmp_path / "one.cube", b"LUT_1D_SIZE 2\n0 0 0\n1 1 1\n"))
    assert np.array_equal(lut.domain_min, [0, 0, 0])
    assert np.array_equal(lut.domain_max, [1, 1, 1])


def test_v4_description_round_trip_native(tmp_path):
    path = matrix_base(tmp_path / "base.icc")
    base = BaseProfile(path)
    assert base.profile.version == (4, 4)
    cube = parse_cube(write_cube(tmp_path / "look.cube", identity_cube(3)))
    blob, _ = generate_profile(cube, base, ConversionParams(description="Camera 日本語"), log=lambda _: None)
    assert ICCProfile(blob).description() == "Camera 日本語"
    profile = ImageCms.ImageCmsProfile(io.BytesIO(blob))
    assert ImageCms.getProfileDescription(profile).strip() == "Camera 日本語"
    inherited, _ = generate_profile(cube, base, ConversionParams(), log=lambda _: None)
    assert ICCProfile(inherited).tag(b"desc") == base.profile.tag(b"desc")


def test_legacy_aliases_priority_and_output_default():
    args = build_arg_parser().parse_args(["--legacy", "--preset", "F-Log2", "--target-gamut", "sRGB"])
    assert _resolve_encoding(args) == ("F-Gamut", "F-Log2", "sRGB", "sRGB")
    args = build_arg_parser().parse_args(["--legacy", "--input-transfer", "F-Log2"])
    assert _resolve_encoding(args)[-1] == "sRGB"


def test_spec_encoding_aliases():
    args = build_arg_parser().parse_args(["--input-gamut", "bt709", "--input-transfer", "gamma2.4"])
    assert _resolve_encoding(args) == ("ITU-R BT.709", "Gamma 2.4", "ITU-R BT.709", "Gamma 2.4")


def test_legacy_comparison_uses_same_accurate_reference(tmp_path):
    base = make_synthetic_base(tmp_path / "base.icc")
    cube = write_cube(tmp_path / "identity.cube", identity_cube(9))
    assert main([str(cube), "--base-icc", str(base), "--compare-legacy", "--validation-samples", "500"]) == 0
    report = json.loads((tmp_path / "TestCamera-identity.validation.json").read_text(encoding="utf-8"))
    assert report["validation_status"] == "PASS"
    assert report["metrics_legacy_vs_accurate_reference"]["mean_delta_e_2000"] > report["metrics_lcms2"]["mean_delta_e_2000"] + .1
