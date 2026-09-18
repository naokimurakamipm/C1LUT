"""Shared test fixtures: CUBE writers and a synthetic camera base profile."""

from __future__ import annotations

from pathlib import Path

import numpy as np


def identity_cube(size: int) -> bytes:
    x = np.linspace(0.0, 1.0, size)
    data = []
    for b in x:
        for g in x:
            for r in x:
                data.append(f"{r:.6f} {g:.6f} {b:.6f}")
    header = f'TITLE "identity{size}"\nLUT_3D_SIZE {size}\n'
    return (header + "\n".join(data) + "\n").encode("ascii")


def write_cube(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def identity_cube_data(size: int) -> np.ndarray:
    """Identity LUT table in the (R, G, B) axis convention."""
    x = np.linspace(0.0, 1.0, size)
    rr, gg, bb = np.meshgrid(x, x, x, indexing="ij")
    return np.ascontiguousarray(np.stack([rr, gg, bb], axis=-1))


def channel_swap_cube(size: int = 5) -> bytes:
    """out.R = in.B, out.G = in.R, out.B = in.G (spec section 19 test LUT 2)."""
    x = np.linspace(0.0, 1.0, size)
    lines = [f'TITLE "swap"\nLUT_3D_SIZE {size}\n']
    for b in x:
        for g in x:
            for r in x:
                lines.append(f"{b:.6f} {r:.6f} {g:.6f}")
    return ("\n".join(lines) + "\n").encode("ascii")


def make_synthetic_base(path: Path, grid: int = 33) -> Path:
    """Build a valid scnr/RGB/Lab v2 profile whose camera RGB equals gamma-encoded sRGB."""
    from conelut.colorspaces import decode_transfer, rgb_linear_to_xyz_d50, xyz_d50_to_lab
    from conelut.icc import (
        PCS_LAB,
        encode_legacy_lab16,
        make_desc,
        make_mft2,
        make_text,
        make_xyz_type,
        write_profile,
    )

    x = np.linspace(0.0, 1.0, grid)
    rr, gg, bb = np.meshgrid(x, x, x, indexing="ij")
    rgb = np.stack([rr, gg, bb], -1).reshape(-1, 3)
    linear = decode_transfer("sRGB", rgb)
    xyz = rgb_linear_to_xyz_d50(linear, "sRGB", "Bradford")
    lab = xyz_d50_to_lab(xyz)
    header = bytearray(128)
    header[12:16] = b"scnr"
    header[16:20] = b"RGB "
    header[20:24] = PCS_LAB
    header[8], header[9] = 0x02, 0x10
    tags = [
        (b"desc", make_desc("TestCamera-Generic", (2, 1))),
        (b"cprt", make_text("COneLUT tests")),
        (b"wtpt", make_xyz_type((0.9642, 1.0, 0.8249))),
        (b"A2B0", make_mft2(encode_legacy_lab16(lab).reshape(-1), grid)),
    ]
    write_profile(path, header, tags)
    return path
