"""Capture One helpers: profile directory discovery, intent probe, install.

The intent probe writes a test ICC whose A2B0/A2B1/A2B2 tags
apply clearly different transforms (red / green / blue shifts). Selecting the
probe profile in Capture One reveals which tag the application actually uses
for camera input profiles, which decides the recommended default for
``--icc-intent``.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import numpy as np

from .colorspaces import _D50_XYZ
from .icc import (
    A2B_TAGS,
    PCS_LAB,
    encode_legacy_lab16,
    make_desc,
    make_mft2,
    make_text,
    make_xyz_type,
    write_profile,
)

PROBE_GRID = 9


def _directory_children(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def find_profiles_dir() -> tuple[Path | None, list[str]]:
    """Discover Capture One camera profile directories and their ICC/ICM files."""
    candidates: list[Path] = []
    if os.name == "nt":
        roots = {
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")),
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")),
        }
        installations: list[Path] = []
        for program_files in roots:
            for vendor in _directory_children(program_files):
                normalized = vendor.name.lower().replace(" ", "")
                if not normalized.startswith(("captureone", "phaseone")):
                    continue
                installations.append(vendor)
                installations.extend(
                    child for child in _directory_children(vendor)
                    if child.name.lower().replace(" ", "").startswith("captureone")
                )
        installations.sort(
            key=lambda p: (tuple(int(part) for part in re.findall(r"\d+", p.name)), str(p).casefold()),
            reverse=True,
        )
        for installation in installations:
            candidates.extend([
                installation / "Color Profiles" / "DSLR",
                installation / "Color Profiles" / "Input",
            ])
    else:
        candidates.append(Path(
            "/Applications/Capture One.app/Contents/Frameworks/"
            "ImageProcessing.framework/Versions/A/Resources/Profiles/Input"
        ))
    first_existing: Path | None = None
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        if first_existing is None:
            first_existing = candidate
        profiles = sorted(
            (p.name for p in _directory_children(candidate)
             if p.is_file() and p.suffix.lower() in {".icc", ".icm"}),
            key=str.casefold,
        )
        if profiles:
            return candidate, profiles
    return first_existing, []


def install_profile(source: Path, directory: Path, existing="rename", *, protected=(), used=None, log=lambda _: None) -> Path | None:
    """Copy a finished ICC into a Capture One profile directory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    from .files import destination, matches, atomic_write
    target = directory / Path(source).name
    if matches(target, [source]):
        return Path(source).resolve()
    target = destination(target, existing, used, set(protected) | {source}, log)
    if target is not None:
        atomic_write(target, Path(source).read_bytes())
    return target


def build_intent_probe_profile() -> bytes:
    """Build the Capture One A2B intent probe profile as bytes."""
    grid = PROBE_GRID
    coords = np.linspace(0.0, 1.0, grid)
    rr, gg, bb = np.meshgrid(coords, coords, coords, indexing="ij")
    rgb = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)
    boosts = {
        0: np.array([1.6, 0.5, 0.5]),   # A2B0 -> visibly red
        1: np.array([0.5, 1.6, 0.5]),   # A2B1 -> visibly green
        2: np.array([0.5, 0.5, 1.6]),   # A2B2 -> visibly blue
    }
    header = bytearray(128)
    header[12:16] = b"scnr"
    header[16:20] = b"RGB "
    header[20:24] = PCS_LAB
    header[8] = 0x02  # version 2.1
    header[9] = 0x10
    tags: list[tuple[bytes, bytes]] = [
        (b"desc", make_desc("C1 Intent Probe (A2B0=RED A2B1=GREEN A2B2=BLUE)", (2, 1))),
        (b"cprt", make_text("C-One LUT intent probe")),
        (b"wtpt", make_xyz_type(_D50_XYZ)),
    ]
    for intent, boost in boosts.items():
        shifted = np.clip(rgb * boost, 0.0, 1.0)
        lab = _srgb_encoded_to_lab(shifted)
        clut = encode_legacy_lab16(lab)
        tags.append((A2B_TAGS[intent], make_mft2(clut.reshape(-1), grid)))
    import tempfile

    handle, temp_name = tempfile.mkstemp(suffix=".icc")
    os.close(handle)
    write_profile(temp_name, header, tags)
    data = Path(temp_name).read_bytes()
    Path(temp_name).unlink(missing_ok=True)
    return data


def write_intent_probe(path: str | Path) -> Path:
    """Write the intent probe profile to ``path``."""
    Path(path).write_bytes(build_intent_probe_profile())
    return Path(path)


def _srgb_encoded_to_lab(srgb_encoded: np.ndarray) -> np.ndarray:
    from .colorspaces import decode_transfer, rgb_linear_to_xyz_d50, xyz_d50_to_lab

    linear = decode_transfer("sRGB", srgb_encoded)
    xyz = rgb_linear_to_xyz_d50(linear, "sRGB", "Bradford")
    return xyz_d50_to_lab(xyz)
