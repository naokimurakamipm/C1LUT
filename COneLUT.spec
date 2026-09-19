# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the C-One LUT app (Windows onedir; macOS .app bundle).

Organised as small helpers plus a single build() call so each workaround
this project needs is documented next to the code implementing it.
"""

import pathlib
import sys

import scipy

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

APP_NAME = "COneLUT"
ENTRY_POINT = "gui.py"
ICON = "art/COneLUT.ico"


def version_resource():
    """Windows VERSIONINFO file generated from conelut.__version__.

    Without it the exe carries no version resource: Explorer's details tab is
    blank and the Inno Setup script's GetVersionNumbersString() returns an
    empty string (the installer came out as "COneLUT-Setup-.exe").
    """
    sys.path.insert(0, SPECPATH)
    from conelut import __version__

    major, minor, *rest = (int(part) for part in __version__.split("."))
    patch = rest[0] if rest else 0
    lines = [
        "# UTF-8",
        "VSVersionInfo(",
        "  ffi=FixedFileInfo(",
        f"    filevers=({major}, {minor}, {patch}, 0),",
        f"    prodvers=({major}, {minor}, {patch}, 0),",
        "    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,",
        "    date=(0, 0),",
        "  ),",
        "  kids=[",
        "    StringFileInfo(",
        "      [",
        "        StringTable(",
        "          '040904B0',",
        "          [",
        "            StringStruct('CompanyName', 'Naoki Murakami'),",
        "            StringStruct('FileDescription', 'C-One LUT - CUBE LUT to ICC for Capture One'),",
        f"            StringStruct('FileVersion', '{__version__}'),",
        f"            StringStruct('InternalName', '{APP_NAME}'),",
        f"            StringStruct('OriginalFilename', '{APP_NAME}.exe'),",
        "            StringStruct('ProductName', 'C-One LUT'),",
        f"            StringStruct('ProductVersion', '{__version__}'),",
        "          ]",
        "        )",
        "      ]",
        "    ),",
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])]),",
        "  ],",
        ")",
    ]
    path = pathlib.Path(SPECPATH) / "build" / "version_info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)

# Runtime assets shipped with the app: the native lcms2 CMM used by the
# independent dE2000 verification, plus its licence and provenance records.
RUNTIME_BINARIES = [("native/lcms2.dll", "native")]
RUNTIME_DATA = [
    ("art/COneLUT.ico", "art"),
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
        version=version_resource(),
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
            bundle_identifier="com.conelut.desktop",
        )


build()
