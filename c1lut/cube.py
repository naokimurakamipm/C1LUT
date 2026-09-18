"""CUBE (.cube) LUT parsing, validation and evaluation (spec sections 6, 14).

Supported directives:

    TITLE "name"
    DOMAIN_MIN r g b
    DOMAIN_MAX r g b
    LUT_1D_SIZE n
    LUT_3D_SIZE n

The parser validates data counts, duplicate declarations, NaN/Inf values and
unsupported directives instead of silently ignoring them. 1D shaper tables are
applied before the 3D table when both are present. DOMAIN_MIN/MAX remap the
input into normalized coordinates before lookup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

import numpy as np

from .interpolation import METHODS, interpolate_1d_table, interpolate_3d

MAX_TABLE_SIZE = 4096


class CubeParseError(ValueError):
    """Raised for malformed or unsupported CUBE files."""


@dataclass
class CubeLUT:
    title: str
    size_3d: int | None  # None for 1D-only files (not convertible)
    domain_min: np.ndarray  # (3,) in R, G, B order
    domain_max: np.ndarray  # (3,) in R, G, B order
    data_3d: np.ndarray | None  # (S, S, S, 3), axes ordered (R, G, B)
    data_1d: np.ndarray | None = None  # (N, 3) shaper
    size_1d: int | None = None
    comments: list[str] = field(default_factory=list)
    source_path: Path | None = None

    @property
    def metadata_hints(self) -> dict[str, str]:
        """Extract hint-style comments such as ``#Input: Rec.709``.

        Comments are hints only; they never decide the transfer function
        (spec section 33.3).
        """
        hints: dict[str, str] = {}
        for comment in self.comments:
            match = re.match(r"\s*([A-Za-z][A-Za-z0-9 _-]*)\s*[:=]\s*(.+?)\s*$", comment)
            if match:
                hints.setdefault(match.group(1).strip().lower(), match.group(2).strip())
        return hints

    @property
    def is_identity(self) -> bool:
        """True when the 3D table is the identity mapping on its domain."""
        if self.data_3d is None or self.size_3d is None:
            return False
        if self.data_1d is not None and not np.allclose(
            self.data_1d, np.linspace(0, 1, len(self.data_1d))[:, None], atol=1e-6
        ):
            return False
        size = self.size_3d
        xr = np.linspace(self.domain_min[0], self.domain_max[0], size)
        xg = np.linspace(self.domain_min[1], self.domain_max[1], size)
        xb = np.linspace(self.domain_min[2], self.domain_max[2], size)
        rr, gg, bb = np.meshgrid(xr, xg, xb, indexing="ij")
        expected = np.stack([rr, gg, bb], axis=-1)
        return bool(np.allclose(self.data_3d, expected, atol=1e-6))

    def apply(
        self,
        rgb: np.ndarray,
        interpolation: str = "tetrahedral",
        domain_policy: str = "clamp",
        stats: dict | None = None,
        lut_domain_policy: str = "clamp",
    ) -> np.ndarray:
        """Apply the LUT to RGB values already expressed in its input encoding.

        ``domain_policy`` controls inputs outside DOMAIN_MIN/MAX: ``clamp``
        (default), ``error`` or ``extrapolate`` (values are allowed past the
        domain edges using the nearest edge cell). Shaper output is handled
        separately by ``lut_domain_policy`` (clamp or error).
        """
        if interpolation not in METHODS:
            raise ValueError(f"unknown interpolation: {interpolation!r}")
        if domain_policy not in ("clamp", "error", "extrapolate"):
            raise ValueError(f"unknown domain policy: {domain_policy!r}")
        if lut_domain_policy not in ("clamp", "error"):
            raise ValueError(f"unknown LUT domain policy: {lut_domain_policy!r}")
        if domain_policy == "extrapolate" and interpolation == "nearest":
            raise ValueError("extrapolate requires tetrahedral or trilinear interpolation")
        if self.data_3d is None or self.size_3d is None:
            raise CubeParseError("this CUBE file has no 3D table and cannot be applied")
        rgb = np.asarray(rgb, dtype=np.float64)
        if not np.isfinite(rgb).all():
            raise ValueError("LUT input must be finite")
        span = self.domain_max - self.domain_min
        if np.any(span <= 0):
            raise CubeParseError("DOMAIN_MAX must be greater than DOMAIN_MIN on every channel")
        normalized = (rgb - self.domain_min) / span

        below = normalized < 0.0
        above = normalized > 1.0
        if stats is not None:
            stats["input_below_domain_pct"] = float(100.0 * np.count_nonzero(below) / normalized.size)
            stats["input_above_domain_pct"] = float(100.0 * np.count_nonzero(above) / normalized.size)
            stats["input_channel_min"] = [float(v) for v in rgb.min(axis=0)]
            stats["input_channel_max"] = [float(v) for v in rgb.max(axis=0)]
        if (below | above).any():
            if domain_policy == "error":
                raise ValueError(
                    "LUT input outside DOMAIN range: "
                    f"{100.0 * np.count_nonzero(below | above) / normalized.size:.3f}% of samples"
                )
            if domain_policy != "extrapolate":
                normalized = np.clip(normalized, 0.0, 1.0)

        if self.data_1d is not None:
            per_channel = [interpolate_1d_table(self.data_1d, normalized[:, c], extrapolate=domain_policy == "extrapolate")[:, c] for c in range(3)]
            shaped = np.stack(per_channel, axis=-1)
            outside = (shaped < 0) | (shaped > 1)
            if outside.any() and lut_domain_policy == "error":
                raise ValueError("shaper output outside 3D LUT domain [0, 1]")
            if stats is not None:
                stats["lut_clamp_pct"] = float(100 * np.mean(np.any(outside, axis=1)))
            shaped = np.clip(shaped, 0, 1)
            coords = shaped * (self.size_3d - 1)
        else:
            coords = normalized * (self.size_3d - 1)
            if stats is not None:
                stats["lut_clamp_pct"] = float(100 * np.mean(np.any(below | above, axis=1))) if domain_policy == "clamp" else 0.0
        flat = interpolate_3d(self.data_3d, coords, interpolation, extrapolate=domain_policy == "extrapolate" and self.data_1d is None)
        return flat


def parse_cube(path: str | Path) -> CubeLUT:
    """Parse and validate a .cube file, raising :class:`CubeParseError` on problems."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise CubeParseError(f"cannot read CUBE file: {path}: {exc}") from exc

    title: str | None = None
    size_1d: int | None = None
    size_3d: int | None = None
    domain_min: np.ndarray | None = None
    domain_max: np.ndarray | None = None
    comments: list[str] = []
    data: list[tuple[int, list[float]]] = []  # (line_number, values)

    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            if line.startswith("#"):
                comments.append(line.lstrip("#").strip())
            continue
        keyword = re.match(r"^[A-Za-z0-9_]+", line)
        head = keyword.group(0).upper() if keyword else ""
        if head == "TITLE":
            if title is not None:
                raise CubeParseError(f"line {line_number}: duplicate TITLE declaration")
            rest = line[len("TITLE"):].strip()
            if not rest:
                raise CubeParseError(f"line {line_number}: TITLE without a name")
            if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
                rest = rest[1:-1]
            title = rest
            continue
        if head in ("DOMAIN_MIN", "DOMAIN_MAX"):
            if head == "DOMAIN_MIN" and domain_min is not None:
                raise CubeParseError(f"line {line_number}: duplicate DOMAIN_MIN declaration")
            if head == "DOMAIN_MAX" and domain_max is not None:
                raise CubeParseError(f"line {line_number}: duplicate DOMAIN_MAX declaration")
            values = _parse_floats(line, head, line_number, 3)
            if head == "DOMAIN_MIN":
                domain_min = np.array(values, dtype=np.float64)
            else:
                domain_max = np.array(values, dtype=np.float64)
            continue
        if head in ("LUT_1D_SIZE", "LUT_3D_SIZE"):
            name, already = ("LUT_1D_SIZE", size_1d) if head == "LUT_1D_SIZE" else ("LUT_3D_SIZE", size_3d)
            if already is not None:
                raise CubeParseError(f"line {line_number}: duplicate {name} declaration")
            try:
                value = int(line[len(name):].strip())
            except ValueError as exc:
                raise CubeParseError(f"line {line_number}: {name} needs an integer size") from exc
            if not 2 <= value <= MAX_TABLE_SIZE:
                raise CubeParseError(f"line {line_number}: {name} {value} outside supported range 2..{MAX_TABLE_SIZE}")
            if head == "LUT_1D_SIZE":
                size_1d = value
            else:
                size_3d = value
            continue
        parts = line.split()
        if len(parts) == 3:
            try:
                values = [float(part) for part in parts]
            except ValueError as exc:
                raise CubeParseError(f"line {line_number}: malformed data line: {line!r}") from exc
            data.append((line_number, values))
            continue
        raise CubeParseError(f"line {line_number}: unsupported directive: {line.split()[0]!r}")

    if not data:
        raise CubeParseError("CUBE file contains no data rows")
    values = np.array([row for _, row in data], dtype=np.float64)
    if not np.isfinite(values).all():
        bad = [ln for ln, row in data if not np.isfinite(row).all()]
        raise CubeParseError(f"CUBE data contains NaN or Inf on lines: {bad[:5]}")

    if domain_min is None:
        domain_min = np.zeros(3)
    if domain_max is None:
        domain_max = np.ones(3)
    if not (np.isfinite(domain_min).all() and np.isfinite(domain_max).all()):
        raise CubeParseError("DOMAIN_MIN/MAX must be finite")
    if np.any(domain_max <= domain_min):
        raise CubeParseError("DOMAIN_MAX must be greater than DOMAIN_MIN on every channel")
    if size_1d is not None and size_3d is None and len(values) != size_1d:
        raise CubeParseError(f"1D data count {len(values)} does not match LUT_1D_SIZE {size_1d}")
    inferred_1d: list[np.ndarray] = []
    if size_1d is not None:
        expected_1d = size_1d
        if len(values) < expected_1d:
            raise CubeParseError(f"1D table has {len(values)} rows, expected {expected_1d}")
        inferred_1d.append(values[:expected_1d])
        values = values[expected_1d:]

    if size_3d is None:
        if len(values) == 0:
            if inferred_1d:
                # 1D-only file: parsed successfully but not convertible.
                return CubeLUT(
                    title=title if title is not None else path.stem,
                    size_3d=None,
                    domain_min=domain_min,
                    domain_max=domain_max,
                    data_3d=None,
                    data_1d=inferred_1d[0],
                    size_1d=size_1d,
                    comments=comments,
                    source_path=path,
                )
            raise CubeParseError("CUBE file contains no data rows")
        guess = round(len(values) ** (1.0 / 3.0))
        if guess < 2 or guess**3 != len(values):
            raise CubeParseError(
                f"3D data count {len(values)} is not a perfect cube and LUT_3D_SIZE is missing"
            )
        size_3d = guess
    expected_3d = size_3d**3
    if len(values) != expected_3d:
        raise CubeParseError(
            f"3D data count {len(values)} does not match LUT_3D_SIZE {size_3d} (expected {expected_3d} rows)"
        )

    if domain_min is None:
        domain_min = np.zeros(3)
    if domain_max is None:
        domain_max = np.ones(3)
    if np.any(domain_max <= domain_min):
        raise CubeParseError("DOMAIN_MAX must be greater than DOMAIN_MIN on every channel")
    if not (np.isfinite(domain_min).all() and np.isfinite(domain_max).all()):
        raise CubeParseError("DOMAIN_MIN/MAX must be finite")

    data_1d = inferred_1d[0] if inferred_1d else None
    # .cube tables are stored with red varying fastest: reshape (B, G, R, 3)
    # and transpose to the (R, G, B) axis order used everywhere else.
    data_3d = values.reshape(size_3d, size_3d, size_3d, 3).transpose(2, 1, 0, 3)
    data_3d = np.ascontiguousarray(data_3d, dtype=np.float64)

    return CubeLUT(
        title=title if title is not None else path.stem,
        size_3d=size_3d,
        domain_min=domain_min,
        domain_max=domain_max,
        data_3d=data_3d,
        data_1d=data_1d,
        size_1d=size_1d if data_1d is not None else None,
        comments=comments,
        source_path=path,
    )


def _parse_floats(line: str, head: str, line_number: int, count: int) -> list[float]:
    parts = line[len(head):].split()
    if len(parts) != count:
        raise CubeParseError(f"line {line_number}: {head} expects {count} numbers")
    try:
        return [float(part) for part in parts]
    except ValueError as exc:
        raise CubeParseError(f"line {line_number}: malformed {head} values: {line!r}") from exc
