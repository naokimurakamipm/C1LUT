"""ICC profile reading, writing and PCS encodings.

Key correctness rules enforced here:

- The A2B CLUT encoding must match the base profile header PCS (bytes 20-23):
  ``b'Lab '`` uses the legacy 16-bit Lab encoding, ``b'XYZ '`` uses 16-bit XYZ
  with 1.0 == 0x8000. Anything else is an error.
- Legacy 16-bit Lab encoding: L* = 100 maps to 0xFF00 (65280), NOT 0xFFFF;
  a*/b* use (value + 128) * 256 with 0 at 0x8000.
- The Profile ID (header bytes 84-99) is recomputed as the MD5 of the profile
  with the profile flags (44-47), rendering intent (64-67) and profile ID
  fields zeroed.
- Tag data is 4-byte aligned and tag signatures are unique.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

PCS_LAB = b"Lab "
PCS_XYZ = b"XYZ "

A2B_TAGS = {0: b"A2B0", 1: b"A2B1", 2: b"A2B2"}
INTENT_NAMES = {0: "perceptual", 1: "relative", 2: "saturation"}


class ICCError(ValueError):
    pass


def s15_fixed16(x: float) -> int:
    return int(round(x * 65536.0)) & 0xFFFFFFFF


@dataclass
class TagEntry:
    signature: bytes
    offset: int
    size: int
    data: bytes


class ICCProfile:
    """A parsed ICC profile: 128-byte header plus tag table."""

    def __init__(self, data: bytes):
        if len(data) < 132:
            raise ICCError("profile is too small to be an ICC file")
        if data[36:40] != b"acsp":
            raise ICCError("ICC profile is missing the acsp signature")
        declared_size = struct.unpack_from(">I", data, 0)[0]
        if declared_size < 132 or declared_size > len(data):
            raise ICCError("invalid ICC declared profile size")
        data = data[:declared_size]
        self.header = bytearray(data[:128])
        (tag_count,) = struct.unpack_from(">I", data, 128)
        if tag_count > (len(data) - 132) // 12:
            raise ICCError("ICC tag table is corrupt (declared count exceeds file size)")
        self.tags: list[TagEntry] = []
        seen: set[bytes] = set()
        for i in range(tag_count):
            sig, offset, size = struct.unpack_from(">4sII", data, 132 + 12 * i)
            if offset < 132 + tag_count * 12 or offset % 4 or offset + size > len(data):
                raise ICCError(f"tag {sig!r} extends past the end of the file")
            if sig in seen:
                raise ICCError(f"duplicate tag signature {sig!r}")
            seen.add(sig)
            self.tags.append(TagEntry(sig, offset, size, bytes(data[offset : offset + size])))

    # -- header accessors ------------------------------------------------

    @property
    def pcs(self) -> bytes:
        return bytes(self.header[20:24])

    @property
    def data_space(self) -> bytes:
        return bytes(self.header[16:20])

    @property
    def device_class(self) -> bytes:
        return bytes(self.header[12:16])

    @property
    def version(self) -> tuple[int, int]:
        return (self.header[8], self.header[9] >> 4)

    def tag(self, signature: bytes) -> bytes | None:
        for entry in self.tags:
            if entry.signature == signature:
                return entry.data
        return None

    def description(self) -> str:
        data = self.tag(b"desc")
        if data is None or len(data) < 12:
            return ""
        if data[:4] == b"desc":
            (count,) = struct.unpack_from(">I", data, 8)
            return data[12 : 12 + max(count - 1, 0)].decode("ascii", "replace")
        if data[:4] == b"mluc":
            if len(data) < 16:
                raise ICCError("mluc header truncated")
            count, record_size = struct.unpack_from(">II", data, 8)
            if record_size < 12 or 16 + count * record_size > len(data):
                raise ICCError("invalid mluc record table")
            records = []
            for i in range(count):
                pos = 16 + i * record_size
                length, offset = struct.unpack_from(">II", data, pos + 4)
                if length % 2 or offset < 16 + count * record_size or offset + length > len(data):
                    raise ICCError("invalid mluc string range")
                text = data[offset:offset + length].decode("utf-16-be").rstrip("\x00")
                records.append((data[pos:pos + 4], text))
            return next((text for locale, text in records if locale == b"enUS"), records[0][1] if records else "")
        return ""

    def require_pcs(self) -> bytes:
        pcs = self.pcs
        if pcs not in (PCS_LAB, PCS_XYZ):
            raise ICCError(
                f"unsupported base profile PCS {pcs!r}: expected b'Lab ' or b'XYZ '"
            )
        return pcs


def read_profile(path: str | Path) -> ICCProfile:
    data = Path(path).read_bytes()
    return ICCProfile(data)


# -- PCS encodings --------------------------------------------------------


def encode_legacy_lab16(lab: np.ndarray) -> np.ndarray:
    """Encode float Lab to ICC legacy 16-bit Lab (lut16Type / mft2).

    L* 100 -> 0xFF00 (65280); a*/b* 0 -> 0x8000, range -128..+127 -> 0..0xFF00.
    """
    lab = np.asarray(lab, dtype=np.float64)
    out = np.empty_like(lab, dtype=np.uint16)
    out[:, 0] = np.clip(np.rint(lab[:, 0] * 652.8), 0, 65535).astype(np.uint16)
    for c in (1, 2):
        out[:, c] = np.clip(np.rint((lab[:, c] + 128.0) * 256.0), 0, 65535).astype(np.uint16)
    return out


def decode_legacy_lab16(encoded: np.ndarray) -> np.ndarray:
    encoded = np.asarray(encoded, dtype=np.float64)
    out = np.empty_like(encoded, dtype=np.float64)
    out[:, 0] = encoded[:, 0] / 652.8
    for c in (1, 2):
        out[:, c] = encoded[:, c] / 256.0 - 128.0
    return out


def encode_xyz16(xyz: np.ndarray) -> np.ndarray:
    """Encode XYZ (D50, Y normalized to 1.0) to 16-bit ICC XYZ (1.0 == 0x8000)."""
    xyz = np.asarray(xyz, dtype=np.float64)
    return np.clip(np.rint(xyz * 32768.0), 0, 65535).astype(np.uint16)


def decode_xyz16(encoded: np.ndarray) -> np.ndarray:
    return np.asarray(encoded, dtype=np.float64) / 32768.0


# -- tag builders ----------------------------------------------------------


def make_mft2(clut_u16_flat: np.ndarray, grid: int, input_curves=None) -> bytes:
    """Build a lut16Type (mft2) A2B tag.

    ``clut_u16_flat`` must be a flat uint16 array in ICC CLUT order: the first
    input channel (R for RGB data) varies SLOWEST, i.e. index =
    r*grid^2 + g*grid + b. Input/output tables are 256-entry ramps by default.

    ``input_curves`` optionally supplies three monotonic warps
    ``[0, 1] -> [0, 1]`` (arrays of equal length >= 2, endpoints 0 and 1) used
    as per-channel input shaper tables: they redistribute the CLUT nodes so
    steep regions of the transform get a finer grid. The CLUT must then be
    sampled at the *inverse* warp positions.
    """
    clut_u16_flat = np.asarray(clut_u16_flat, dtype=np.uint16)
    expected = grid**3 * 3
    if clut_u16_flat.size != expected:
        raise ICCError(f"CLUT has {clut_u16_flat.size} values, expected {expected}")
    out = bytearray(b"mft2" + b"\x00" * 4)
    out += bytes((3, 3, grid, 0))
    out += struct.pack(">9i", s15_fixed16(1.0), 0, 0, 0, s15_fixed16(1.0), 0, 0, 0, s15_fixed16(1.0))
    out += struct.pack(">HH", 256, 256)
    ramp = np.round(np.linspace(0.0, 65535.0, 256)).astype(">u2")
    input_ramps = [ramp] * 3
    if input_curves is not None:
        input_ramps = [_encode_input_curve(curve) for curve in input_curves]
    out += input_ramps[0].tobytes() + input_ramps[1].tobytes() + input_ramps[2].tobytes()
    out += clut_u16_flat.astype(">u2").tobytes()
    out += ramp.tobytes() * 3
    return bytes(out)


def _encode_input_curve(curve: np.ndarray) -> np.ndarray:
    """Resample a monotonic [0,1] warp to the 256-entry mft2 input table."""
    curve = np.asarray(curve, dtype=np.float64)
    if curve.ndim != 1 or curve.size < 2:
        raise ICCError("input shaper curve requires a 1D array with at least two points")
    if curve[0] != 0.0 or curve[-1] != 1.0:
        raise ICCError("input shaper curve must start at 0 and end at 1")
    if np.any(np.diff(curve) < -1e-9):
        raise ICCError("input shaper curve must be non-decreasing")
    x = np.linspace(0.0, 1.0, curve.size)
    sampled = np.interp(np.linspace(0.0, 1.0, 256), x, curve)
    return np.clip(np.rint(sampled * 65535.0), 0, 65535).astype(">u2")


def make_desc(text: str, version: tuple[int, int]) -> bytes:
    """Build a 'desc' tag appropriate for the profile version."""
    if version >= (4, 0):
        return _make_mluc(text)
    return _make_text_description(text)


def _make_text_description(text: str) -> bytes:
    ascii_bytes = text.encode("ascii", "replace") + b"\x00"
    out = bytearray(b"desc" + b"\x00" * 4)
    out += struct.pack(">I", len(ascii_bytes))
    out += ascii_bytes
    out += struct.pack(">II", 0, 0)  # unicode language code, unicode count
    out += struct.pack(">HB", 0, 0)  # scriptcode, macintosh count
    out += b"\x00" * 67  # macintosh description
    return bytes(out)


def _make_mluc(text: str) -> bytes:
    records = text.encode("utf-16-be")
    out = bytearray(b"mluc" + b"\x00" * 4)
    out += struct.pack(">II", 1, 12)
    out += b"en" + b"US"
    out += struct.pack(">II", len(records), 28)
    out += records
    return bytes(out)


def make_text(text: str) -> bytes:
    data = text.encode("ascii", "replace") + b"\x00"
    return b"text" + b"\x00" * 4 + data


def make_xyz_type(xyz) -> bytes:
    return b"XYZ " + b"\x00" * 4 + struct.pack(">3i", *(s15_fixed16(v) for v in xyz))


def compute_profile_id(header: bytearray, file_body: bytes, total_size: int) -> bytes:
    """MD5 profile ID per ICC rules: zero flags, rendering intent, ID, reserved.

    ``file_body`` is everything after the 128-byte header (tag table + data).
    """
    scrubbed = bytearray(header)
    scrubbed[0:4] = struct.pack(">I", total_size)
    scrubbed[44:48] = b"\x00" * 4  # profile flags
    scrubbed[64:68] = b"\x00" * 4  # rendering intent
    scrubbed[84:100] = b"\x00" * 16  # profile ID
    scrubbed[100:128] = b"\x00" * 28  # reserved
    return hashlib.md5(bytes(scrubbed) + file_body).digest()


def write_profile(path: str | Path, header: bytearray, tags: list[tuple[bytes, bytes]]) -> int:
    """Serialize header + tags with 4-byte alignment, recompute the profile ID."""
    if len(header) != 128:
        raise ICCError("ICC header must be exactly 128 bytes")
    seen: set[bytes] = set()
    for sig, _ in tags:
        if sig in seen:
            raise ICCError(f"duplicate tag signature {sig!r}")
        seen.add(sig)

    header_size = 128
    table_size = 4 + 12 * len(tags)
    offset = header_size + table_size
    table = struct.pack(">I", len(tags))
    payload = bytearray()
    for sig, data in tags:
        pad = (-len(data)) % 4
        table += sig + struct.pack(">II", offset, len(data))
        payload += data + b"\x00" * pad
        offset += len(data) + pad
    total_size = offset

    header = bytearray(header)
    header[0:4] = struct.pack(">I", total_size)
    header[36:40] = b"acsp"
    header[100:128] = bytes(28)
    header[84:100] = compute_profile_id(header, bytes(table) + bytes(payload), total_size)

    blob = bytes(header) + table + bytes(payload)
    Path(path).write_bytes(blob)
    return total_size
