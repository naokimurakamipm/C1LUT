# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the C1LUT app (Windows onedir; macOS .app bundle).

Organised as small helpers plus a single build() call so each workaround
this project needs is documented next to the code implementing it.
"""

import pathlib
import sys

import scipy

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

APP_NAME = "C1LUT"
ENTRY_POINT = "gui.py"
ICON = "art/C1LUT.ico"

# Runtime assets shipped with the app: the native lcms2 CMM used by the
# independent dE2000 verification, plus its licence and provenance records.
RUNTIME_BINARIES = [("native/lcms2.dll", "native")]
RUNTIME_DATA = [
    ("art/C1LUT.ico", "art"),
    ("native/LCMS-LICENSE", "native"),
    ("native/source.json", "native"),
]


def colour_runtime_data():
    """colour-science's non-code datasets plus its package metadata."""
    datasets = collect_data_files(
        "colour",
        excludes=["**/tests/**", "**/examples/**", "**/__pycache__/**"],
    )
    return datasets + copy_metadata("colour-science")


def scipy_vendored_modules():
    """List the modules under scipy/_external, a package with no __init__.py.

    PyInstaller 6.17's bundled scipy hook predates this vendoring location;
    without an explicit listing the frozen app dies at import time with
    "No module named scipy._external.array_api_compat.numpy.fft".
    """
    root = pathlib.Path(scipy.__file__).resolve().parent
    vendored = []
    for file in sorted((root / "_external").rglob("*.py")):
        parts = list(file.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        vendored.append("scipy." + ".".join(parts))
    return vendored


def build():
    analysis = Analysis(
        [ENTRY_POINT],
        binaries=RUNTIME_BINARIES,
        datas=colour_runtime_data() + RUNTIME_DATA,
        hiddenimports=["PIL.ImageCms"] + scipy_vendored_modules(),
    )
    archive = PYZ(analysis.pure)
    executable = EXE(
        archive,
        analysis.scripts,
        exclude_binaries=True,
        name=APP_NAME,
        console=False,
        upx=True,
        icon=ICON,
    )
    app_folder = COLLECT(
        executable,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=True,
        name=APP_NAME,
    )
    if sys.platform == "darwin":
        BUNDLE(
            app_folder,
            name=f"{APP_NAME}.app",
            bundle_identifier="com.c1lut.desktop",
        )


build()
