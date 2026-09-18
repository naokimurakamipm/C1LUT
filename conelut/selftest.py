"""Headless self test used to verify frozen (PyInstaller) builds.

Runs a real conversion (synthetic camera base + identity LUT) through the full
pipeline including ΔE2000 validation, without Tk or any user interaction.
"""

from __future__ import annotations

from pathlib import Path
import tempfile

import numpy as np

from conelut.colorspaces import decode_transfer, rgb_linear_to_xyz_d50, xyz_d50_to_lab
from conelut.convert import convert_file
from conelut.cms import BaseProfile
from conelut.icc import (
    PCS_LAB,
    encode_legacy_lab16,
    make_desc,
    make_mft2,
    make_text,
    make_xyz_type,
    write_profile,
)
from conelut.pipeline import ConversionParams

IDENTITY_MEAN_LIMIT = 0.5  # synthetic base + identity LUT should land far below this


def _identity_cube_bytes(size: int) -> bytes:
    x = np.linspace(0.0, 1.0, size)
    lines = [f'TITLE "selftest"', f"LUT_3D_SIZE {size}"]
    for b in x:
        for g in x:
            for r in x:
                lines.append(f"{r:.6f} {g:.6f} {b:.6f}")
    return ("\n".join(lines) + "\n").encode("ascii")


def _synthetic_base(path: Path, grid: int = 17) -> Path:
    x = np.linspace(0.0, 1.0, grid)
    rr, gg, bb = np.meshgrid(x, x, x, indexing="ij")
    rgb = np.stack([rr, gg, bb], -1).reshape(-1, 3)
    linear = decode_transfer("sRGB", rgb)
    lab = xyz_d50_to_lab(rgb_linear_to_xyz_d50(linear, "sRGB", "Bradford"))
    header = bytearray(128)
    header[12:16] = b"scnr"
    header[16:20] = b"RGB "
    header[20:24] = PCS_LAB
    header[8], header[9] = 0x02, 0x10
    tags = [
        (b"desc", make_desc("SelfTestCamera-Generic", (2, 1))),
        (b"cprt", make_text("C-One LUT selftest")),
        (b"wtpt", make_xyz_type((0.9642, 1.0, 0.8249))),
        (b"A2B0", make_mft2(encode_legacy_lab16(lab).reshape(-1), grid)),
    ]
    write_profile(path, header, tags)
    return path


def run_selftest(log_path: Path | None = None) -> tuple[int, str]:
    """Execute the frozen-build smoke test; returns (exit_code, report text)."""
    lines: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="conelut-selftest-") as temp:
            temp_dir = Path(temp)
            cube_path = temp_dir / "selftest.cube"
            cube_path.write_bytes(_identity_cube_bytes(9))
            base = BaseProfile(_synthetic_base(temp_dir / "base.icc"), precision="float")
            params = ConversionParams(icc_grid=17)
            result = convert_file(
                cube_path, base, params,
                output_dir=temp_dir, existing="overwrite",
                validate=True, validation_samples=2000, write_json=False,
                log=lambda message: lines.append(message),
            )
        lines.append(f"result: {result.status}")
        if result.status != "success" or result.output_path is None:
            return 1, "\n".join(lines)
        mean = _mean_from_log(lines)
        lines.append(f"identity mean dE00: {mean:.4f} (limit {IDENTITY_MEAN_LIMIT})")
        ok = mean < IDENTITY_MEAN_LIMIT and any("-> PASS" in line for line in lines)
        lines.append("SELFTEST " + ("OK" if ok else "FAILED"))
        return (0 if ok else 1), "\n".join(lines)
    except Exception as error:  # noqa: BLE001 - selftest must report, not raise
        lines.append(f"SELFTEST FAILED: {type(error).__name__}: {error}")
        return 1, "\n".join(lines)


def _mean_from_log(lines: list[str]) -> float:
    for line in lines:
        if "Mean dE00:" in line:
            return float(line.split("Mean dE00:")[1].strip().split()[0])
    raise ValueError("validation summary not found in selftest log")
