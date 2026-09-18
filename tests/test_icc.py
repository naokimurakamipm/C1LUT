"""ICC encoding / writer tests (spec sections 4, 16, 17, 27)."""

from __future__ import annotations

import io
import struct

import numpy as np
import pytest

from c1lut.icc import (
    ICCError,
    ICCProfile,
    decode_legacy_lab16,
    decode_xyz16,
    encode_legacy_lab16,
    encode_xyz16,
    make_desc,
    make_mft2,
    make_text,
    make_xyz_type,
    write_profile,
)


def test_legacy_lab16_test_points():
    lab = np.array([
        [0.0, 0.0, 0.0],
        [50.0, 0.0, 0.0],
        [100.0, 0.0, 0.0],
        [50.0, -128.0, -128.0],
        [50.0, 127.0, 127.0],
    ])
    encoded = encode_legacy_lab16(lab)
    assert encoded[0, 0] == 0
    assert encoded[1, 0] == round(50.0 * 652.8)
    assert encoded[2, 0] == 0xFF00, "L* 100 must encode to 0xFF00 (65280), not 0xFFFF"
    assert encoded[3, 1] == 0
    assert encoded[3, 2] == 0
    assert encoded[4, 1] == 0xFF00
    assert encoded[4, 2] == 0xFF00
    # a* 0 == 0x8000
    assert encoded[1, 1] == 0x8000


def test_legacy_lab16_round_trip():
    rng = np.random.default_rng(2)
    lab = np.column_stack([
        rng.uniform(0, 100, 1000),
        rng.uniform(-127, 127, 1000),
        rng.uniform(-127, 127, 1000),
    ])
    decoded = decode_legacy_lab16(encode_legacy_lab16(lab))
    assert np.allclose(decoded, lab, atol=0.01)


def test_xyz16_round_trip():
    xyz = np.array([[0.0, 0.0, 0.0], [0.9642, 1.0, 0.8249], [1.5, 1.2, 1.9]])
    decoded = decode_xyz16(encode_xyz16(xyz))
    assert np.allclose(decoded, xyz, atol=1e-4)
    raw = encode_xyz16(np.array([[1.0, 1.0, 1.0]]))
    assert raw[0, 1] == 0x8000


def test_mft2_structure_and_alignment():
    grid = 5
    clut = np.arange(grid**3 * 3, dtype=np.uint16)
    tag = make_mft2(clut, grid)
    assert tag[:4] == b"mft2"
    assert tag[8] == 3 and tag[9] == 3 and tag[10] == grid
    assert struct.unpack_from(">HH", tag, 48) == (256, 256)
    expected_size = 52 + 3 * 256 * 2 + grid**3 * 3 * 2 + 3 * 256 * 2
    assert len(tag) == expected_size


def test_profile_serialization_and_id(tmp_path):
    header = bytearray(128)
    header[12:16] = b"scnr"
    header[16:20] = b"RGB "
    header[20:24] = b"Lab "
    tags = [
        (b"desc", make_desc("test profile", (2, 1))),
        (b"cprt", make_text("licence")),
        (b"wtpt", make_xyz_type((0.9642, 1.0, 0.8249))),
        (b"A2B0", make_mft2(np.zeros(9**3 * 3, dtype=np.uint16), 9)),
    ]
    path = tmp_path / "test.icc"
    size = write_profile(path, header, tags)
    data = path.read_bytes()
    assert len(data) == size
    assert struct.unpack_from(">I", data, 0)[0] == size
    assert data[36:40] == b"acsp"
    profile_id = data[84:100]
    assert profile_id != b"\x00" * 16

    # Re-serialize with identical content: the ID must be stable.
    path2 = tmp_path / "test2.icc"
    write_profile(path2, bytearray(header), tags)
    assert path2.read_bytes()[84:100] == profile_id

    # Modifying any tag changes the ID.
    tags3 = tags[:-1] + [(b"A2B0", make_mft2(np.ones(9**3 * 3, dtype=np.uint16), 9))]
    path3 = tmp_path / "test3.icc"
    write_profile(path3, bytearray(header), tags3)
    assert path3.read_bytes()[84:100] != profile_id

    # The ID matches the ICC recipe: MD5 with flags/intent/ID/reserved zeroed.
    import hashlib

    scrubbed = bytearray(data)
    scrubbed[44:48] = b"\x00" * 4
    scrubbed[64:68] = b"\x00" * 4
    scrubbed[84:100] = b"\x00" * 16
    scrubbed[100:128] = b"\x00" * 28
    assert hashlib.md5(bytes(scrubbed)).digest() == profile_id

    # Reparse
    profile = ICCProfile(data)
    assert profile.pcs == b"Lab "
    assert profile.description() == "test profile"
    assert profile.tag(b"A2B0") is not None


def test_duplicate_tag_rejected(tmp_path):
    header = bytearray(128)
    header[36:40] = b"acsp"
    with pytest.raises(ICCError):
        write_profile(tmp_path / "dup.icc", header, [(b"desc", b"x"), (b"desc", b"y")])


def test_desc_v4_mluc():
    blob = make_desc("v4 name", (4, 3))
    assert blob[:4] == b"mluc"
    assert "v4 name".encode("utf-16-be") in blob


def test_generated_profile_opens_in_littlecms(tmp_path):
    """Smoke test: the generated file must open and transform via LittleCMS."""
    from PIL import Image, ImageCms

    from tests.helpers import make_synthetic_base

    base_path = make_synthetic_base(tmp_path / "base.icc")
    src = ImageCms.ImageCmsProfile(io.BytesIO(base_path.read_bytes()))
    srgb = ImageCms.createProfile("sRGB")
    transform = ImageCms.buildTransform(src, srgb, "RGB", "RGB", renderingIntent=1)
    img = Image.new("RGB", (3, 1), (0, 0, 0))
    img.putpixel((1, 0), (128, 128, 128))
    img.putpixel((2, 0), (255, 255, 255))
    out = ImageCms.applyTransform(img, transform)
    pixels = np.asarray(out).reshape(-1, 3)
    assert np.isfinite(pixels).all()
    # The synthetic base maps camera RGB == gamma-encoded sRGB: near-identity.
    assert int(pixels[0].mean()) < 8
    assert int(pixels[1].mean()) in range(116, 141)
    assert int(pixels[2].mean()) > 247
