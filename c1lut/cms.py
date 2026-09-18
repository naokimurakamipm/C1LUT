"""High-precision evaluation of the Base ICC input profile (spec section 5).

The legacy pipeline sampled the base profile through Pillow's ImageCms on an
8-bit RGB image, quantizing the camera RGB grid to 256 levels per channel.
This module replaces that with a float evaluation of the profile's A2B tags:

- ``mft2`` (lut16Type) and ``mAB `` (lutAToBType) tags are parsed and evaluated
  directly in float64 (input tables -> CLUT (tetrahedral) -> output tables,
  plus curves/matrix for mAB).
- PCS values are decoded with the ICC legacy 16-bit Lab encoding (L* 100 ->
  0xFF00) or 16-bit XYZ (1.0 -> 0x8000) depending on the profile PCS.
- The legacy 8-bit ImageCms path is still available via ``precision="8bit"``
  for --legacy compat mode.
- An optional ctypes bridge to a system/bundled lcms2 DLL provides an
  independent cross-check backend (``precision="lcms"``). Explicit lcms
  requests fail clearly if the runtime is unavailable.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
from pathlib import Path

import numpy as np

from .colorspaces import lab_to_xyz_d50
from .icc import A2B_TAGS, ICCError, ICCProfile, PCS_LAB, PCS_XYZ, decode_legacy_lab16, decode_xyz16
from .interpolation import interpolate_3d

INTENT_FALLBACK_ORDER = (0, 1, 2)
PRECISION_CHOICES = ("float", "lcms", "8bit")


class BaseProfileError(ValueError):
    pass


class BaseProfile:
    """A camera input profile used as the Base ICC.

    ``evaluate`` returns ``(kind, values)`` where kind is ``"pcs"`` (float Lab
    or XYZ, D50) for the float/lcms2 backends or ``"srgb_encoded"`` for the
    legacy 8-bit ImageCms backend.
    """

    def __init__(self, path: str | Path, precision: str = "float"):
        self.path = Path(path).resolve()
        if precision not in PRECISION_CHOICES:
            raise BaseProfileError(f"unknown CMS precision: {precision!r} (expected one of {PRECISION_CHOICES})")
        self.precision = precision
        try:
            data = self.path.read_bytes()
            self.profile = ICCProfile(data)
        except (OSError, ICCError) as exc:
            raise BaseProfileError(f"cannot parse base ICC {self.path}: {exc}") from exc
        if self.profile.data_space != b"RGB ":
            raise BaseProfileError(
                f"base ICC data colour space must be RGB, got {self.profile.data_space!r}"
            )
        if self.profile.device_class != b"scnr":
            print(
                "warning: base ICC device class is "
                f"{self.profile.device_class!r} (input profiles are usually b'scnr')"
            )
        self.pcs = self.profile.require_pcs()
        self.available_intents = [
            intent for intent in (0, 1, 2) if self.profile.tag(A2B_TAGS[intent]) is not None
        ]
        self.matrix_shaper = None
        if not self.available_intents:
            # No A2B tags: fall back to a matrix-shaper profile (rXYZ/gTRC),
            # which all three precision backends can evaluate. This keeps
            # plain sRGB/AdobeRGB-style profiles usable as a base.
            self.matrix_shaper = _matrix_shaper_evaluator(self.profile)
            if self.matrix_shaper is None:
                raise BaseProfileError(
                    "base ICC contains no A2B0/A2B1/A2B2 tag and no matrix-shaper tags "
                    "(rXYZ/gTRC/bXYZ/bTRC); cannot evaluate it"
                )
            print("warning: base ICC has no A2B tags; evaluating via its matrix/TRC shaper")
        self._parsed_tags: dict[int, object] = {}

    def resolve_intent(self, requested: int, warnings: list[str] | None = None) -> int:
        """Map the requested sampling intent onto a tag the profile actually has.

        Missing tags fall back in A2B0 > A2B1 > A2B2 order (with a warning) so
        that e.g. relative-intent requests work with A2B0-only camera profiles.
        Matrix-shaper profiles have no per-intent tags; intents are ignored.
        """
        if not self.available_intents:
            return 0
        if requested in self.available_intents:
            return requested
        for candidate in INTENT_FALLBACK_ORDER:
            if candidate in self.available_intents:
                note = (
                    f"base ICC has no {A2B_TAGS[requested].decode()}; sampling via "
                    f"{A2B_TAGS[candidate].decode()} instead"
                )
                if warnings is None:
                    print(f"warning: {note}")
                else:
                    warnings.append(note)
                return candidate
        raise BaseProfileError("base ICC has no usable A2B tag")

    def _a2b(self, intent: int):
        if not self.available_intents:
            return self.matrix_shaper
        if intent not in self._parsed_tags:
            data = self.profile.tag(A2B_TAGS[intent])
            self._parsed_tags[intent] = parse_a2b_tag(data, self.pcs)
        return self._parsed_tags[intent]

    def evaluate(self, rgb: np.ndarray, intent: int) -> tuple[str, np.ndarray]:
        """Evaluate the base transform; returns (kind, values)."""
        rgb = np.asarray(rgb, dtype=np.float64)
        if self.precision == "8bit":
            return "srgb_encoded", _evaluate_via_imagecms(self.path, rgb, intent)
        if self.precision == "lcms":
            backend = create_lcms2_backend(self.path, intent)
            if backend is None:
                raise BaseProfileError("native lcms2 is required for precision='lcms'")
            try:
                lab = backend(rgb)
                return "pcs", lab if self.pcs == PCS_LAB else lab_to_xyz_d50(lab)
            finally:
                backend.close()
        return "pcs", self._a2b(intent)(rgb)

    def evaluate_pcs_xyz_d50(self, rgb: np.ndarray, intent: int) -> np.ndarray:
        """Camera RGB -> XYZ (D50) regardless of the profile PCS."""
        from .colorspaces import decode_transfer, rgb_linear_to_xyz_d50

        kind, values = self.evaluate(rgb, intent)
        if kind == "srgb_encoded":
            linear = decode_transfer("sRGB", values)
            return rgb_linear_to_xyz_d50(linear, "sRGB", "Bradford")
        if self.pcs == PCS_LAB:
            return lab_to_xyz_d50(values)
        return values


# -- A2B tag parsing / evaluation -----------------------------------------


def parse_a2b_tag(data: bytes | None, pcs: bytes):
    if data is None:
        raise BaseProfileError("A2B tag missing")
    kind = data[:4]
    if kind == b"mft2":
        return Mft2Tag(data, pcs)
    if kind == b"mAB ":
        return MabTag(data, pcs)
    if kind == b"mft1":
        raise BaseProfileError("8-bit mft1 A2B tags are not supported; use a lut16 base profile")
    raise BaseProfileError(f"unsupported A2B tag type {kind!r}")


def _decode_pcs(u16: np.ndarray, pcs: bytes) -> np.ndarray:
    if pcs == PCS_LAB:
        return decode_legacy_lab16(u16)
    if pcs == PCS_XYZ:
        return decode_xyz16(u16)
    raise BaseProfileError(f"unsupported PCS {pcs!r}")


def _matrix_shaper_evaluator(profile):
    """Build a float RGB -> XYZ (D50) evaluator from rXYZ/gTRC-style tags.

    Display-class profiles without A2B tags (plain sRGB.icc etc.) are valid
    matrix-shaper profiles: PCS = M @ TRC_decode(RGB), with the colorant
    matrix already chromatically adapted to D50. Returns None when the
    required tags are missing or the PCS is not XYZ.
    """
    if profile.pcs != PCS_XYZ:
        return None
    columns = []
    for signature in (b"rXYZ", b"gXYZ", b"bXYZ"):
        data = profile.tag(signature)
        if data is None or len(data) < 20 or data[:4] != b"XYZ ":
            return None
        columns.append([value / 65536.0 for value in struct.unpack_from(">3i", data, 8)])
    curves = []
    for signature in (b"rTRC", b"gTRC", b"bTRC"):
        data = profile.tag(signature)
        if data is None:
            return None
        try:
            curves.append(_read_curve(data))
        except (BaseProfileError, struct.error):
            return None
    matrix = np.column_stack(columns)  # columns are the r/g/b colorants

    def evaluate(rgb: np.ndarray) -> np.ndarray:
        rgb = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, 1.0)
        linear = np.stack([curves[c](rgb[:, c]) for c in range(3)], axis=-1)
        return linear @ matrix.T

    return evaluate


class Mft2Tag:
    """lut16Type (mft2) A2B evaluator in float64.

    Pipeline: input tables -> CLUT (tetrahedral) -> output tables. All 16-bit
    tables/CLUT values live in the 0..65535 encoded domain; PCS decoding
    happens once at the end.
    """

    def __init__(self, data: bytes, pcs: bytes):
        if len(data) < 52:
            raise BaseProfileError("mft2 tag truncated")
        self.pcs = pcs
        self.chan_in, self.chan_out, self.grid = data[8], data[9], data[10]
        if self.chan_in != 3 or self.chan_out != 3:
            raise BaseProfileError(f"mft2 must be 3->3 channels, got {self.chan_in}->{self.chan_out}")
        in_ent, out_ent = struct.unpack_from(">HH", data, 48)
        if self.grid < 2 or min(in_ent, out_ent) < 2:
            raise BaseProfileError("mft2 grid and input/output tables require at least two entries")
        self.in_ent, self.out_ent = in_ent, out_ent
        pos = 52
        self.input_tables = self._read_tables(data, pos, in_ent) if in_ent > 1 else None
        if self.input_tables is not None:
            pos += 6 * in_ent
        count = self.grid**3 * 3
        if pos + count * 2 > len(data):
            raise BaseProfileError("mft2 CLUT extends past the tag end")
        clut = np.frombuffer(data, dtype=">u2", count=count, offset=pos).astype(np.float64)
        pos += count * 2
        self.output_tables = None
        if out_ent > 1:
            self.output_tables = self._read_tables(data, pos, out_ent)
        # ICC CLUT sample order: the first input channel (R) varies SLOWEST,
        # i.e. flat index = r*grid^2 + g*grid + b. Reshaping therefore yields
        # (r, g, b) axes directly, matching the interpolation module.
        self.clut = np.ascontiguousarray(clut.reshape(self.grid, self.grid, self.grid, 3))

    @staticmethod
    def _read_tables(data: bytes, pos: int, entries: int) -> np.ndarray:
        count = 3 * entries
        if pos + count * 2 > len(data):
            raise BaseProfileError("mft2 tables extend past the tag end")
        tables = np.frombuffer(data, dtype=">u2", count=count, offset=pos).astype(np.float64)
        return tables.reshape(3, entries)

    def __call__(self, rgb: np.ndarray) -> np.ndarray:
        rgb = np.asarray(rgb, dtype=np.float64)
        values = np.clip(rgb, 0.0, 1.0) * 65535.0
        if self.input_tables is not None:
            values = np.stack(
                [
                    np.interp(values[:, c], np.linspace(0.0, 65535.0, self.in_ent), self.input_tables[c])
                    for c in range(3)
                ],
                axis=-1,
            )
        coords = values * (self.grid - 1) / 65535.0
        out = interpolate_3d(self.clut, coords, "tetrahedral")
        if self.output_tables is not None:
            out = np.stack(
                [
                    np.interp(out[:, c], np.linspace(0.0, 65535.0, self.out_ent), self.output_tables[c])
                    for c in range(3)
                ],
                axis=-1,
            )
        return _decode_pcs(out, self.pcs)


class MabTag:
    """lutAToBType (mAB) A2B evaluator in float64.

    Processing order (matching lcms2): A curves -> CLUT -> M curves -> matrix
    -> B curves. All stages operate on values normalized to [0, 1]; the final
    value is decoded as 16-bit PCS once.
    """

    def __init__(self, data: bytes, pcs: bytes):
        if len(data) < 32:
            raise BaseProfileError("mAB tag truncated")
        self.pcs = pcs
        self.chan_in, self.chan_out = data[8], data[9]
        if self.chan_in != 3 or self.chan_out != 3:
            raise BaseProfileError(f"mAB must be 3->3 channels, got {self.chan_in}->{self.chan_out}")
        off_b, off_matrix, off_m, off_c, off_a = struct.unpack_from(">5I", data, 12)
        if not off_b or any(o and (o < 32 or o >= len(data)) for o in (off_b, off_matrix, off_m, off_c, off_a)):
            raise BaseProfileError("invalid mAB stage offsets")
        self.curves_a = _read_curves(data, off_a) if off_a else None
        self.clut, self.clut_grid = _read_mab_clut(data, off_c) if off_c else (None, 0)
        self.matrix = None
        if off_matrix:
            if off_matrix + 48 > len(data):
                raise BaseProfileError("mAB matrix truncated")
            values = struct.unpack_from(">9i", data, off_matrix)
            self.matrix = np.array([v / 65536.0 for v in values], dtype=np.float64).reshape(3, 3)
            self.matrix_offset = np.array(struct.unpack_from(">3i", data, off_matrix + 36)) / 65536.0
        self.curves_m = _read_curves(data, off_m) if off_m else None
        self.curves_b = _read_curves(data, off_b) if off_b else None

    @staticmethod
    def _apply_curves(values: np.ndarray, curves) -> np.ndarray:
        if curves is None:
            return values
        return np.stack([curves[c](values[:, c]) for c in range(3)], axis=-1)

    def __call__(self, rgb: np.ndarray) -> np.ndarray:
        values = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, 1.0)
        values = self._apply_curves(values, self.curves_a)
        if self.clut is not None:
            coords = values * (self.clut_grid - 1)
            values = interpolate_3d(self.clut, coords, "tetrahedral") / 65535.0
        values = self._apply_curves(values, self.curves_m)
        if self.matrix is not None:
            values = values @ self.matrix.T + self.matrix_offset
        values = self._apply_curves(values, self.curves_b)
        if self.pcs == PCS_LAB:
            return values * np.array([100.0, 255.0, 255.0]) - np.array([0.0, 128.0, 128.0])
        return values * (65535.0 / 32768.0)


def _read_mab_clut(data: bytes, offset: int):
    if offset + 20 > len(data):
        raise BaseProfileError("mAB CLUT header truncated")
    points = np.array(list(data[offset:offset + 3]), dtype=int)
    precision = data[offset + 16]
    if np.any(points < 2) or precision not in (1, 2):
        raise BaseProfileError("invalid mAB CLUT grid or precision")
    count = int(np.prod(points)) * 3
    if offset + 20 + count * precision > len(data):
        raise BaseProfileError("mAB CLUT data truncated")
    values = np.frombuffer(data, dtype=">u2" if precision == 2 else "u1", count=count, offset=offset + 20).astype(float)
    if precision == 1:
        values *= 257.0
    return values.reshape(tuple(points) + (3,)), points


_PARAM_COUNTS = {0: 1, 1: 3, 2: 4, 3: 5, 4: 7}


def _curve_size(body):
    if len(body) < 12:
        raise BaseProfileError("ICC curve header truncated")
    if body[:4] == b"curv":
        size = 12 + 2 * struct.unpack_from(">I", body, 8)[0]
    elif body[:4] == b"para":
        function = struct.unpack_from(">H", body, 8)[0]
        if function not in _PARAM_COUNTS:
            raise BaseProfileError(f"unsupported parametric curve type {function}")
        size = 12 + 4 * _PARAM_COUNTS[function]
    else:
        raise BaseProfileError(f"unsupported ICC curve type {body[:4]!r}")
    if size > len(body):
        raise BaseProfileError("ICC curve data truncated")
    return size


def _read_curves(data: bytes, offset: int):
    curves = []
    pos = offset
    for index in range(3):
        size = _curve_size(data[pos:])
        curves.append(_read_curve(data[pos:pos + size]))
        nxt = pos + size
        aligned = (nxt + 3) & ~3
        # Standard writers align each curve; tolerate unpadded legacy writers.
        pos = aligned if data[aligned:aligned + 4] in (b"curv", b"para") else nxt
    return curves


def _read_curve(body: bytes):
    _curve_size(body)
    if body[:4] == b"curv":
        entries = struct.unpack_from(">I", body, 8)[0]
        if entries == 0:
            return _identity_curve
        if entries == 1:
            gamma = struct.unpack_from(">H", body, 12)[0] / 256.0
            return lambda x: _signed_power(x, gamma)
        table = np.frombuffer(body, dtype=">u2", count=entries, offset=12).astype(float) / 65535.0
        xp = np.linspace(0.0, 1.0, entries)
        return lambda x: np.interp(np.asarray(x, dtype=float), xp, table)
    function = struct.unpack_from(">H", body, 8)[0]
    params = np.array(struct.unpack_from(f">{_PARAM_COUNTS[function]}i", body, 12)) / 65536.0
    if function in (1, 2) and params[1] == 0:
        raise BaseProfileError("parametric curve has zero a coefficient")
    return lambda x: _eval_parametric(x, function, params)


def _identity_curve(x):
    return np.asarray(x, dtype=np.float64)


def _signed_power(x, exponent):
    x = np.asarray(x, dtype=np.float64)
    return np.sign(x) * np.abs(x) ** exponent


def _eval_parametric(x, function, p):
    """ICC parametricCurveType functions 0 through 4 (ICC.1, table 68)."""
    x = np.asarray(x, dtype=float)
    if function == 0:
        return _signed_power(x, p[0])
    g, a, b = p[:3]
    if function in (1, 2):
        offset = p[3] if function == 2 else 0.0
        return np.where(x >= -b / a, np.maximum(a * x + b, 0.0) ** g + offset, offset)
    c, d = p[3:5]
    e, f = p[5:7] if function == 4 else (0.0, 0.0)
    return np.where(x >= d, np.maximum(a * x + b, 0.0) ** g + e, c * x + f)


# -- legacy 8-bit ImageCms backend -----------------------------------------


def _evaluate_via_imagecms(path: Path, rgb: np.ndarray, intent: int) -> np.ndarray:
    from PIL import Image, ImageCms

    srgb = ImageCms.createProfile("sRGB")
    transform = ImageCms.buildTransform(str(path), srgb, "RGB", "RGB", renderingIntent=intent)
    flat = (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)
    img = Image.fromarray(flat.reshape(1, -1, 3))
    out = ImageCms.applyTransform(img, transform)
    return np.asarray(out).reshape(-1, 3).astype(np.float64) / 255.0


# -- optional lcms2 ctypes bridge ------------------------------------------

# lcms2.h: FLOAT_SH(1) | COLORSPACE_SH(PT_*) | CHANNELS_SH(3) | BYTES_SH(n)
_TYPE_RGB_FLT = 0x44001c
_TYPE_LAB_DBL = 0x4a0018


def lcms2_available() -> bool:
    return _find_lcms2_dll() is not None


def _find_lcms2_dll():
    import sys
    env = os.environ.get("C1LUT_LCMS2_DLL")
    if env:
        try:
            return ctypes.CDLL(env)
        except OSError as exc:
            raise BaseProfileError(f"cannot load C1LUT_LCMS2_DLL: {exc}") from exc
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    candidates = [str(root / "native" / "lcms2.dll"), "lcms2.dll"]
    found = ctypes.util.find_library("lcms2")
    if found:
        candidates.append(found)
    for candidate in candidates:
        try:
            return ctypes.CDLL(candidate)
        except OSError:
            continue
    return None


def create_lcms2_backend(path: Path, intent: int):
    """Return None only for an absent runtime; malformed profiles are errors."""
    dll = _find_lcms2_dll()
    return None if dll is None else _Lcms2Backend(path, intent, dll)


class _Lcms2Backend:
    def __init__(self, path: Path, intent: int, dll=None):
        self.dll = dll or _find_lcms2_dll()
        if self.dll is None:
            raise BaseProfileError("lcms2 DLL not found")
        dll = self.dll
        ptr, uint = ctypes.c_void_p, ctypes.c_uint32
        signatures = {
            "cmsOpenProfileFromMemTHR": (ptr, [ptr, ptr, uint]),
            "cmsCreateLab4ProfileTHR": (ptr, [ptr, ptr]),
            "cmsCreateTransformTHR": (ptr, [ptr, ptr, uint, ptr, uint, uint, uint]),
            "cmsCreateExtendedTransform": (ptr, [ptr, uint, ctypes.POINTER(ptr), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(uint), ctypes.POINTER(ctypes.c_double), ptr, uint, uint, uint, uint]),
            "cmsCloseProfile": (ctypes.c_int, [ptr]),
            "cmsDeleteTransform": (None, [ptr]),
            "cmsDoTransform": (None, [ptr, ptr, ptr, uint]),
        }
        for name, (result, args) in signatures.items():
            function = getattr(dll, name)
            function.restype, function.argtypes = result, args
        self.src = self.dst = self.transform = None
        blob = Path(path).read_bytes()
        self.buffer = ctypes.create_string_buffer(blob)
        try:
            self.src = dll.cmsOpenProfileFromMemTHR(None, self.buffer, len(blob))
            self.dst = dll.cmsCreateLab4ProfileTHR(None, None)  # default D50 xyY
            if not self.src or not self.dst:
                raise BaseProfileError("lcms2 could not open the profiles")
            # Select the requested source A2B, then read its raw PCS via the
            # relative Lab endpoint. A perceptual v4 endpoint would force BPC
            # (even without the BPC flag) and change the sampled tag values.
            # NOOPTIMIZE preserves the independent high precision pipeline.
            self.transform = dll.cmsCreateExtendedTransform(
                None, 2, (ptr * 2)(self.src, self.dst), (ctypes.c_int * 2)(0, 0),
                (uint * 2)(intent, 1), (ctypes.c_double * 2)(1.0, 1.0),
                None, 0, _TYPE_RGB_FLT, _TYPE_LAB_DBL, 0x0100,
            )
            if not self.transform:
                raise BaseProfileError("lcms2 could not build the transform")
        except Exception:
            self.close()
            raise

    def __call__(self, rgb):
        src = np.ascontiguousarray(rgb, dtype=np.float32)
        if src.ndim != 2 or src.shape[1] != 3 or not np.isfinite(src).all():
            raise BaseProfileError("lcms input must be finite RGB samples of shape (N, 3)")
        dst = np.empty(src.shape, dtype=np.float64)
        self.dll.cmsDoTransform(self.transform, src.ctypes.data, dst.ctypes.data, len(src))
        if not np.isfinite(dst).all():
            raise BaseProfileError("lcms2 produced non-finite Lab")
        return dst

    def close(self):
        for attribute, function in (("transform", "cmsDeleteTransform"), ("src", "cmsCloseProfile"), ("dst", "cmsCloseProfile")):
            handle = getattr(self, attribute, None)
            if handle:
                getattr(self.dll, function)(handle)
                setattr(self, attribute, None)
