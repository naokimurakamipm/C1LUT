"""Shared destination selection and atomic publication for conversion/copy/report."""
from pathlib import Path
import os
import tempfile


def path_key(path):
    return os.path.normcase(str(Path(path).resolve()))


def matches(path, paths):
    key = path_key(path)
    for other in paths:
        if key == path_key(other):
            return True
        try:
            if Path(path).samefile(other):
                return True
        except OSError:
            pass
    return False


def destination(path, policy="rename", used=None, protected=(), log=lambda _: None):
    if policy not in ("rename", "overwrite", "skip"):
        raise ValueError(f"unknown existing-file policy: {policy}")
    path = Path(path).resolve()
    used = used if used is not None else set()
    protected = tuple(protected)
    must_rename = matches(path, protected) or matches(path, used)
    if not must_rename and path.exists() and policy == "skip":
        return None
    candidate = path
    if must_rename or (path.exists() and policy == "rename"):
        number = 2
        while True:
            candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
            if not candidate.exists() and not matches(candidate, used) and not matches(candidate, protected):
                break
            number += 1
        if must_rename:
            log(f"  warning: 入力・バッチ結果を保護して別名で保存: {candidate.name}")
    used.add(path_key(candidate))
    return candidate


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".conelut-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
