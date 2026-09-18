"""Fetch the pinned MIT-licensed conda-forge Windows x64 LittleCMS runtime.

Build-time dependency: zstandard. No network access is used by the application.
"""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request
import zipfile
import zstandard

URL = "https://api.anaconda.org/download/conda-forge/lcms2/2.17/win-64/lcms2-2.17-hbcf6048_0.conda"
SHA256 = "7712eab5f1a35ca3ea6db48ead49e0d6ac7f96f8560da8023e61b3dbe4f3b25d"


def main():
    data = urllib.request.urlopen(URL, timeout=60).read()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise RuntimeError("LittleCMS package checksum mismatch")
    dest = Path(__file__).resolve().parents[1] / "native"
    dest.mkdir(exist_ok=True)
    found = set()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if not name.endswith(".tar.zst"):
                continue
            with zstandard.ZstdDecompressor().stream_reader(archive.open(name)) as decoded:
                with tarfile.open(fileobj=decoded, mode="r|") as package:
                    for member in package:
                        if member.isfile() and (member.name == "Library/bin/lcms2.dll" or member.name.startswith("info/licenses/")):
                            target = "lcms2.dll" if member.name.endswith("lcms2.dll") else "LCMS-" + Path(member.name).name
                            (dest / target).write_bytes(package.extractfile(member).read())
                            found.add(target)
    if "lcms2.dll" not in found or len(found) < 2:
        raise RuntimeError(f"Missing runtime or license: {found}")
    (dest / "source.json").write_text(json.dumps({"url": URL, "sha256": SHA256, "license": "MIT", "files": sorted(found)}, indent=2), encoding="utf-8")
    print("LittleCMS runtime and license installed:", sorted(found))


if __name__ == "__main__":
    main()
